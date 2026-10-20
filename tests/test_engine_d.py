"""End-to-end D products, using real bounded local scientific workflows."""
import copy
from datetime import date,timedelta
import json
from pathlib import Path
import numpy as np
import rasterio
from test_engine_b import request,run_request
from sps.engine_b import run_job_b


def temporal_request(tmp_path):
    req=request(tmp_path)
    obj=json.loads(Path(req['samples']['path']).read_text(encoding='utf-8'))
    for i,feature in enumerate(obj['features']):
        feature['properties']['observation_date']=(date(2020,1,1)+timedelta(days=(i//2)*3)).isoformat()
    path=tmp_path/'dated.geojson';path.write_text(json.dumps(obj),encoding='utf-8')
    req['samples'].update(path=str(path),date_field='observation_date')
    req['validation']={'method':'temporal','n_splits':2,'inner_splits':2,'initial_train_periods':16,'inner_initial_train_periods':4}
    req['uncertainty']={'method':'split_conformal','coverage':.8,'calibration_fraction':.3}
    return req


def test_temporal_end_to_end_records_and_initial_history(tmp_path):
    result,req=run_request(temporal_request(tmp_path),tmp_path)
    assert result['status']=='completed',result
    assert result['stage']=='D'
    ev=json.loads((tmp_path/'run/evaluation.json').read_text(encoding='utf-8'))['evaluation']
    assert len(ev['oof'])==31
    assert ev['final_record']['temporal']['calibration_dates']['start']>ev['final_record']['temporal']['development_dates']['end']
    preview=json.loads((tmp_path/'run/preflight.json').read_text(encoding='utf-8'))
    assert preview['initial_training_count']==32 and preview['outer_tested_count']==31
    assert preview['independent_test_count']==0
    geo=json.loads((tmp_path/'run/folds.geojson').read_text(encoding='utf-8'))
    assert not any(v['properties']['role']=='independent' for v in geo['features'])
    assert 'Son kullanılan tarih' in (tmp_path/'run/report.html').read_text(encoding='utf-8')


def test_temporal_date_or_gap_change_invalidates_preview(tmp_path):
    req=temporal_request(tmp_path);pre=run_job_b(req);assert pre['status']=='completed',pre
    req.update(action='run',preview_fingerprint=pre['preview_fingerprint'],output_dir=str(tmp_path/'bad'))
    req['validation']['gap_days']=1
    changed=run_job_b(req)
    assert changed['error']['code']=='PREVIEW_STALE'
    req['output_dir']=str(tmp_path/'changed_date');req['validation']['gap_days']=0
    obj=json.loads(Path(req['samples']['path']).read_text());obj['features'][-1]['properties']['observation_date']='2020-05-01'
    Path(req['samples']['path']).write_text(json.dumps(obj))
    changed=run_job_b(req)
    assert changed['error']['code']=='PREVIEW_STALE'


def test_spatial_interval_real_raster_missing_support(tmp_path):
    req=request(tmp_path)
    req['validation']['metric_crs']='EPSG:32637'
    req['uncertainty']={'method':'spatial_conformal','coverage':.8,'calibration_fraction':.4,
                        'spatial_bandwidth_m':750,'spatial_min_effective_n':2,'spatial_min_groups':2}
    result,_=run_request(req,tmp_path)
    assert result['status']=='completed',result
    arrays={}
    for name in ('prediction','interval_available','prediction_lower','prediction_upper','prediction_interval_width'):
        with rasterio.open(tmp_path/'run'/f'{name}.tif') as dst:arrays[name]=dst.read(1,masked=True)
    valid=~arrays['prediction'].mask
    support=arrays['interval_available'].data.astype(bool)&valid
    assert np.count_nonzero(valid)==12248
    assert np.any(valid&~support) and np.any(support)
    np.testing.assert_array_equal(arrays['prediction_lower'].mask,~support)
    np.testing.assert_array_equal(arrays['prediction_upper'].mask,~support)
    audit=json.loads((tmp_path/'run/audit.json').read_text(encoding='utf-8'))
    assert audit['interval_support']['available_input_pixels']==np.count_nonzero(support)
    assert audit['interval_support']['valid_input_pixels']==12248
    ev=json.loads((tmp_path/'run/evaluation.json').read_text(encoding='utf-8'))['evaluation']
    assert len(ev['oof'])==63
    for row in ev['oof']:
        if row['interval_status']=='unavailable':assert row['lower'] is None and row['upper'] is None and row['reason_code']
    assert 'Yerel destek bulunmayan hücre' in (tmp_path/'run/report.html').read_text(encoding='utf-8')
