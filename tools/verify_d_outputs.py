"""Independent readback of files produced by actual ArcMap jobs; starts no model."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import zipfile

import numpy as np
import rasterio

ROOT=Path(__file__).resolve().parents[1]


def read(path): return json.loads(path.read_text(encoding='utf-8-sig'))
def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(run):
    result=read(run/'result.json'); audit=read(run/'audit.json')
    ev=read(run/'evaluation.json')['evaluation']; record=read(run/'final_record.json')
    assert result['status']=='completed'
    for entry in result['outputs']:
        assert digest(run/entry['path'])==entry['sha256'],entry['path']
    for source in audit['sources']+audit['source_code']:
        assert digest(Path(source['path']))==source['sha256'],source['path']
    groups=audit['groups']; train=set(record['development_ids']); cal=set(record['calibration_ids'])
    independent=set(record['independent_ids'])
    assert train and train.isdisjoint(cal|independent) and cal.isdisjoint(independent)
    assert set(groups[i] for i in train).isdisjoint(groups[i] for i in cal|independent)
    assert set(groups[i] for i in cal).isdisjoint(groups[i] for i in independent)
    for fold in ev['folds']:
        assert set(fold['train_ids']).isdisjoint(fold['test_ids'])
        assert independent.isdisjoint(fold['train_ids']+fold['test_ids'])
        assert set(groups[i] for i in fold['train_ids']).isdisjoint(groups[i] for i in fold['test_ids'])
    for trial in ev['trials']:
        if trial['status']!='complete':continue
        predictions=trial['validation_predictions']; keys={(r['sample_id'],r['repeat'],r['fold']) for r in predictions}
        assert len(keys)==len(predictions)
        obs=np.array([r['observed'] for r in predictions]);pred=np.array([r['predicted'] for r in predictions])
        assert np.isclose(trial['validation_metrics']['rmse'],np.sqrt(np.mean((pred-obs)**2)),rtol=1e-12)
        for fold in trial['inner_folds']:
            expected=set(fold['test_ids']);actual={r['sample_id'] for r in predictions if (r['repeat'],r['fold'])==(fold['repeat'],fold['fold'])}
            assert expected==actual and expected.isdisjoint(fold['train_ids'])
    oof=ev['oof']; obs=np.array([r['observed'] for r in oof]); pred=np.array([r['predicted'] for r in oof])
    assert np.isclose(ev['metrics']['pooled_oof']['rmse'],np.sqrt(np.mean((pred-obs)**2)),rtol=1e-12)
    assert np.isclose(ev['metrics']['pooled_oof']['mae'],np.mean(np.abs(pred-obs)),rtol=1e-12)
    np.testing.assert_allclose([r['residual'] for r in oof],pred-obs,rtol=0,atol=1e-12)
    settings=audit['settings']
    if settings['validation']['method']=='temporal':
        dates=settings['validation']['dates_by_id']
        for fold in ev['folds']:
            assert max(dates[i] for i in fold['train_ids'])<min(dates[i] for i in fold['test_ids'])
        for row in ev['fold_metrics']:
            rec=row['model_record']
            if rec['calibration_ids']:assert max(dates[i] for i in rec['development_ids'])<min(dates[i] for i in rec['calibration_ids'])
        assert max(dates[i] for i in train)<min(dates[i] for i in cal)
    arrays={}; raster_details={}
    for entry in result['outputs']:
        if not entry['path'].endswith('.tif'):continue
        with rasterio.open(run/entry['path']) as src:
            values=src.read(1,masked=True)
            assert src.tags()['method']==record['model']['id']
            assert src.tags()['run_id']==result['run_id']
            assert src.crs.to_epsg()==32637
            arrays[entry['path']]=values
            raster_details[entry['path']]={'valid_pixels':int(values.count()),'shape':list(values.shape),'units':list(src.units)}
    assert arrays['prediction.tif'].count()==64
    lower=arrays['prediction_lower.tif'];upper=arrays['prediction_upper.tif'];width=arrays['prediction_interval_width.tif']
    np.testing.assert_array_equal(np.ma.getmaskarray(lower),np.ma.getmaskarray(upper))
    np.testing.assert_array_equal(np.ma.getmaskarray(lower),np.ma.getmaskarray(width))
    delta=np.abs((upper-lower-width).compressed())
    assert not len(delta) or float(delta.max())<1e-5
    supported=None
    if 'interval_available.tif' in arrays:
        support=arrays['interval_available.tif'];assert set(support.compressed())<={0,1}
        supported=int(support.sum());assert supported==lower.count()
        assert audit['interval_support']['available_input_pixels']==supported
        assert audit['interval_support']['valid_input_pixels']==64
    package=[r for r in result['outputs'] if r['kind']=='model_bundle'];assert len(package)==1
    with zipfile.ZipFile(run/package[0]['path']) as archive:
        assert archive.testzip() is None
        manifest=json.loads(archive.read('manifest.json'))
        assert set(manifest['members'])==set(archive.namelist())-{'manifest.json'}
        for name,expected in manifest['members'].items():assert hashlib.sha256(archive.read(name)).hexdigest()==expected,name
        assert manifest['model_id']==record['model']['id']
        assert json.loads(archive.read('final_record.json'))==record
        assert 'study/study_summary.json' in archive.namelist()
        if record['model']['id']=='tabpfn':assert 'models/tabpfn-v2/tabpfn-v2-regressor.ckpt' in archive.namelist()
    for name in ('study_report.html','study_summary.json','publication_metrics.csv','hyperparameters.csv','inner_validation_predictions.csv','training_predictions.csv','calibration_predictions.csv'):
        assert (run/name).is_file(),name
    with (run/'inner_validation_predictions.csv').open(encoding='utf-8-sig',newline='') as stream:
        exported=list(csv.DictReader(stream))
    assert len(exported)==sum(len(t.get('validation_predictions',[])) for t in ev['trials'])
    return {'run_id':result['run_id'],'model':record['model']['id'],'validation':settings['validation']['method'],
            'status':'passed','output_hashes_verified':len(result['outputs']), 'source_hashes_verified':len(audit['sources']),
            'code_hashes_verified':len(audit['source_code']),'oof_rows':len(oof),'unique_oof_samples':len({r['sample_id'] for r in oof}),
            'training_n':len(train),'calibration_n':len(cal),'independent_n':len(independent),
            'supported_interval_pixels':supported,'interval_width_max_error':float(delta.max()) if len(delta) else None,
            'rasters':raster_details,'model_package_sha256':package[0]['sha256']}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('runs',nargs='+',type=Path)
    parser.add_argument('--complete-suite',action='store_true')
    args=parser.parse_args();reports=[verify(p.resolve()) for p in args.runs]
    if args.complete_suite:assert {r['model'] for r in reports}=={'tabm','realmlp','tabpfn'}
    status='passed_complete_suite' if args.complete_suite else 'passed_provided_runs_only'
    destination=ROOT/'build/arcmap-evidence/artifact-readback-D.json'
    destination.write_text(json.dumps({'status':status,'runs':reports},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps([{k:v for k,v in r.items() if k!='rasters'} for r in reports],ensure_ascii=False))
