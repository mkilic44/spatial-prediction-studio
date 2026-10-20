"""Read back actual ArcMap-produced C artifacts; does not start a job."""
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
import rasterio

ROOT=Path(__file__).resolve().parents[1]
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def read(path):return json.loads(path.read_text(encoding='utf-8'))

reports=[]
for argument in sys.argv[1:]:
    run=Path(argument).resolve()
    result=read(run/'result.json');audit=read(run/'audit.json');record=read(run/'final_record.json')
    assert result['status']=='completed' and result['stage']=='C'
    for item in result['outputs']:
        assert digest(run/item['path'])==item['sha256'],item['path']
    for source in audit['sources']:assert digest(Path(source['path']))==source['sha256']
    training=set(record['development_ids']);calibration=set(record['calibration_ids'])
    assert training and calibration and training.isdisjoint(calibration)
    ev=read(run/'evaluation.json')['evaluation']
    assert len(ev['oof'])==len({r['sample_id'] for r in ev['oof']})==63
    for fold in ev['folds']:assert set(fold['train_ids']).isdisjoint(fold['test_ids'])
    rasters={}
    for item in result['outputs']:
        if not item['path'].endswith('.tif'):continue
        with rasterio.open(run/item['path']) as source:
            arr=source.read(1,masked=True)
            assert source.tags()['method']==record['model']['id']
            assert source.tags()['run_id']==result['run_id']
            rasters[item['path']]={'valid_pixels':int(arr.count()),'units':source.units,'tags':source.tags()}
    arrays={}
    for name in ('prediction_lower.tif','prediction_upper.tif','prediction_interval_width.tif','applicability.tif','dissimilarity.tif'):
        with rasterio.open(run/name) as source:arrays[name]=source.read(1,masked=True)
    error=np.abs(arrays['prediction_upper.tif']-arrays['prediction_lower.tif']-arrays['prediction_interval_width.tif'])
    assert float(error.max())<1e-5
    mask=arrays['applicability.tif'];assert set(np.unique(mask.compressed())) <= {0,1}
    assert mask.count()==arrays['dissimilarity.tif'].count()==12248
    assert ev['permutation_importance']['rows'] and ev['subgroup_diagnostics']['per_repeat']
    if record['model']['id']=='regression_kriging':assert ev['plain_model_comparison']['pooled_oof_plain']['n']==63
    if audit['settings']['validation']['method']=='knndm':
        assert all(f['diagnostics']['distance_matching']['reference']=='CAST 1.0.4' for f in ev['folds'])
    reports.append({'run_id':result['run_id'],'status':'passed','model':record['model']['id'],
        'validation':audit['settings']['validation']['method'],'output_hashes_verified':len(result['outputs']),
        'source_hashes_verified':len(audit['sources']),'training_n':len(training),'calibration_n':len(calibration),
        'raster_count':len(rasters),'rasters':rasters,'interval_width_max_error':float(error.max()),
        'applicability_inside_pixels':int(mask.sum()),'applicability_valid_pixels':int(mask.count())})
assert len(reports)==2 and {r['model'] for r in reports}=={'lightgbm','regression_kriging'}
destination=ROOT/'build/arcmap-evidence/artifact-readback-C.json'
destination.write_text(json.dumps({'status':'passed','runs':reports},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps([{k:v for k,v in r.items() if k!='rasters'} for r in reports],ensure_ascii=False))
