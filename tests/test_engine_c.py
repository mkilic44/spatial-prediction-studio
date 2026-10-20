import json
from pathlib import Path
import numpy as np
import pytest
import rasterio
from test_engine_b import request,run_request

@pytest.mark.parametrize('model',['ordinary_kriging','regression_kriging'])
def test_spatial_model_raster_reload_and_original_units(tmp_path,model):
    req=request(tmp_path);req['model']={'id':model,'parameters':{'n_closest_points':12}}
    req['validation']['metric_crs']='EPSG:32637';req['vif']={'mode':'off'}
    if model=='regression_kriging':req['uncertainty']={'method':'split_conformal','coverage':.8,'calibration_fraction':.3}
    result,_=run_request(req,tmp_path);assert result['status']=='completed',result
    with rasterio.open(tmp_path/'run/prediction.tif') as src:
        assert src.units==('g/kg',) and src.read(1,masked=True).count()==12248
    ev=json.loads((tmp_path/'run/evaluation.json').read_text(encoding='utf-8'))['evaluation']
    if model=='regression_kriging':assert ev['plain_model_comparison']['pooled_oof_plain']['n']==63

def test_applicability_maps_and_reloaded_numeric_design(tmp_path):
    req=request(tmp_path);req['diagnostics']={'applicability':True,'permutation':True,'permutation_repeats':2,'subgroup_fields':['landuse']}
    req['uncertainty']={'method':'split_conformal','coverage':.8,'calibration_fraction':.3}
    req['model_candidates']=[{'id':'mean','parameters':{}},{'id':'ridge','parameters':{'alpha':1}}]
    samples=json.loads(Path(req['samples']['path']).read_text())
    for i,feature in enumerate(samples['features']):feature['properties']['landuse']='A' if i%2 else 'B'
    path=tmp_path/'samples.geojson';path.write_text(json.dumps(samples));req['samples']['path']=str(path)
    result,_=run_request(req,tmp_path);assert result['status']=='completed',result
    with rasterio.open(tmp_path/'run/applicability.tif') as src:
        mask=src.read(1,masked=True);assert set(np.unique(mask.compressed()))<={0,1}
    with rasterio.open(tmp_path/'run/dissimilarity.tif') as src:
        assert src.read(1,masked=True).count()==mask.count()==12248
    rec=json.loads((tmp_path/'run/final_record.json').read_text(encoding='utf-8'))
    assert rec['applicability']['reference']=='CAST 1.0.4 trainDI/aoa'
    assert rec['model']['id']=='ridge'
    with rasterio.open(tmp_path/'run/prediction.tif') as src:assert src.tags()['method']=='ridge'
    ev=json.loads((tmp_path/'run/evaluation.json').read_text(encoding='utf-8'))['evaluation']
    assert len(ev['permutation_importance']['rows'])==3*2*2
    assert len(ev['subgroup_diagnostics']['per_repeat'])==2
    assert (tmp_path/'run/permutation_importance.csv').is_file()
    assert (tmp_path/'run/subgroup_diagnostics.csv').is_file()

def test_knndm_mapping_domain_and_nested_training(tmp_path):
    req=request(tmp_path);req['validation'].update(method='knndm',metric_crs='EPSG:32637',maxp=.75,prediction_samples=64)
    req['model']={'id':'mean','parameters':{}}
    result,_=run_request(req,tmp_path);assert result['status']=='completed',result
    ev=json.loads((tmp_path/'run/evaluation.json').read_text(encoding='utf-8'))['evaluation']
    assert len(ev['oof'])==len({r['sample_id'] for r in ev['oof']})==63
    for fold in ev['folds']:
        match=fold['diagnostics']['distance_matching']
        assert match['reference']=='CAST 1.0.4' and np.isfinite(match['W'])
        assert 5<=match['prediction_points']<=100
    for item in ev['learning_audit']:
        if item['role'].startswith('outer/0/0/'):
            assert set(item['ids']).isdisjoint(ev['folds'][0]['test_ids'])
