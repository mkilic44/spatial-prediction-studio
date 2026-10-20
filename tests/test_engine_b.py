import copy,json,sys
from pathlib import Path
import numpy as np
import pytest
import rasterio
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from sps.engine_b import run_job_b
ROOT=Path(__file__).resolve().parents[1]

def request(tmp_path):
    return {'schema_version':2,'run_id':'test_b','task':'regression','action':'preview','samples':{'path':str(ROOT/'examples/samples.geojson'),'id_field':'sample_id','target_field':'soil_value','unit':'g/kg'},'covariates':[{'id':'a','name':'Elev','unit':'m','path':str(ROOT/'examples/covariates.tif'),'band':1,'kind':'continuous'},{'id':'b','name':'Other','unit':'1','path':str(ROOT/'examples/covariates.tif'),'band':2,'kind':'continuous'}],'model':{'id':'ridge','parameters':{'alpha':1}},'validation':{'method':'random','n_splits':3,'inner_splits':2,'seed':42},'output_dir':str(tmp_path/'preview'),'window_size':32}

def run_request(req,tmp_path):
    pre=run_job_b(req);assert pre['status']=='completed',pre
    req=copy.deepcopy(req);req.update(action='run',run_id='model_b',preview_fingerprint=pre['preview_fingerprint'],output_dir=str(tmp_path/'run'))
    return run_job_b(req),req

def test_preview_has_folds_and_no_model(tmp_path):
    req=request(tmp_path);result=run_job_b(req)
    assert result['status']=='completed',result
    assert result['preview_fingerprint']
    assert any(o['kind']=='folds' for o in result['outputs'])
    assert not list((tmp_path/'preview').glob('*.joblib'))

def test_run_requires_current_preview(tmp_path):
    req=request(tmp_path);pre=run_job_b(req);assert pre['status']=='completed',pre
    req.update(action='run',preview_fingerprint=pre['preview_fingerprint'],output_dir=str(tmp_path/'bad'))
    req['validation']['seed']=44
    result=run_job_b(req)
    assert result['status']=='failed' and result['error']['code']=='PREVIEW_STALE'

def test_nested_raster_and_reload_outputs(tmp_path):
    result,req=run_request(request(tmp_path),tmp_path)
    assert result['status']=='completed',result
    assert result['validation']['status']=='nested_cross_validation'
    assert all(not Path(x['path']).is_absolute() for x in result['outputs'])
    with rasterio.open(tmp_path/'run/prediction.tif') as dst:
        assert dst.units==('g/kg',) and dst.tags()['validation_status']=='nested_cross_validation'
        assert dst.read(1,masked=True).count()==12248

def test_conformal_outputs_and_width(tmp_path):
    req=request(tmp_path);req['uncertainty']={'method':'split_conformal','coverage':.8,'calibration_fraction':.3}
    result,_=run_request(req,tmp_path);assert result['status']=='completed',result
    arrays=[]
    for name in ('prediction_lower','prediction_upper','prediction_interval_width'):
        with rasterio.open(tmp_path/'run'/f'{name}.tif') as dst:arrays.append(dst.read(1,masked=True))
    np.testing.assert_allclose(arrays[2].compressed(),(arrays[1]-arrays[0]).compressed(),rtol=1e-5,atol=1e-5)

def test_cancelled_preview_has_no_layers(tmp_path):
    req=request(tmp_path);out=tmp_path/'preview';out.mkdir();(out/'cancel.request').touch()
    result=run_job_b(req)
    assert result['status']=='cancelled' and not result['outputs']

def test_unknown_setting_and_categorical_interpolation(tmp_path):
    req=request(tmp_path);req['invented']=True
    result=run_job_b(req);assert result['status']=='failed' and result['error']['code']=='UNKNOWN_SETTING'


def test_separate_test_file_is_locked_before_folds(tmp_path):
    req=request(tmp_path);obj=json.loads(Path(req['samples']['path']).read_text())
    req['validation']['metric_crs']='EPSG:32637'
    held={**obj,'features':obj['features'][-10:]};development={**obj,'features':obj['features'][:-10]}
    train_path=tmp_path/'development.geojson';test_path=tmp_path/'independent.geojson'
    train_path.write_text(json.dumps(development));test_path.write_text(json.dumps(held))
    req['samples'].update(path=str(train_path),independent_path=str(test_path))
    result,req=run_request(req,tmp_path)
    assert result['status']=='completed',result
    expected={str(f['properties']['sample_id']) for f in held['features']}
    audit=json.loads((tmp_path/'run/audit.json').read_text())
    assert set(audit['settings']['validation']['independent_ids'])==expected
    assert expected.isdisjoint(audit['final_record']['training_ids'])
    for fold in audit['folds']:
        assert expected.isdisjoint(fold['train_ids']+fold['test_ids'])
    preflight=json.loads((tmp_path/'run/preflight.json').read_text())
    assert preflight['independent_test_count']==10
    assert preflight['workload']['estimated_pipeline_fits_upper_bound']>0
    assert all(f['diagnostics']['test_to_train_distance_m']['min']>0 for f in preflight['folds'])


def test_block_sizes_are_equivalent_with_nodata(tmp_path):
    from test_data_b import sample_file,raster
    spec=sample_file(tmp_path);spec['group_fields']=[];cov=raster(tmp_path)
    with rasterio.open(cov['path'],'r+') as dst:
        a=dst.read(1);a[3,3]=-9999;dst.write(a,1)
    saved=[]
    for size in (1,7,256):
        root=tmp_path/str(size);root.mkdir()
        req=request(root);req.update(samples=spec,covariates=[cov],window_size=size)
        req['validation'].update(n_splits=2,inner_splits=2,method='group')
        result,_=run_request(req,root);assert result['status']=='completed',result
        with rasterio.open(root/'run/prediction.tif') as dst:saved.append(dst.read(1,masked=True))
    for actual in saved[1:]:
        np.testing.assert_array_equal(actual.mask,saved[0].mask)
        np.testing.assert_allclose(actual.compressed(),saved[0].compressed(),rtol=1e-6,atol=1e-6)


def test_true_quantile_forest_raster(tmp_path):
    req=request(tmp_path)
    req['model']={'id':'random_forest','parameters':{'n_estimators':10,'max_depth':5,'min_samples_leaf':2}}
    req['uncertainty']={'method':'quantile_forest','coverage':.8}
    result,_=run_request(req,tmp_path);assert result['status']=='completed',result
    audit=json.loads((tmp_path/'run/audit.json').read_text(encoding='utf-8'))
    assert 'quantile_forest.RandomForestQuantileRegressor' in audit['final_record']['quantile_estimator']
    assert any(o['kind']=='width' for o in result['outputs'])


def test_independent_reuse_history_marks_changed_settings(tmp_path):
    from sps.engine_b import record_independent_evaluation
    from sps.data_b import read_samples_b
    req=request(tmp_path);req['validation']['independent_ids']=['S001','S002'];req['preview_fingerprint']='one'
    sample=read_samples_b(req['samples']);out=tmp_path/'jobs'/'r1'/'output';out.mkdir(parents=True)
    first=record_independent_evaluation(out,req,sample);assert first['status']=='first_evaluation'
    req['preview_fingerprint']='two';req['run_id']='different'
    second=record_independent_evaluation(out,req,sample)
    assert second['status']=='reused_after_settings_change' and second['previous_evaluations']==1
