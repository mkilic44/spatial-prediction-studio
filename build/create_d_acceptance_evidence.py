"""Read historical D artifacts only. No model imports, fitting or source mutation."""
from __future__ import annotations
import csv
import hashlib
import json
import math
import re
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'build/arcmap-evidence'

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def file_record(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': digest(path), 'bytes': path.stat().st_size}

def point_metrics(rows):
    y = np.array([r['observed'] for r in rows], dtype=float)
    p = np.array([r['predicted'] for r in rows], dtype=float)
    assert len(y) and np.isfinite(y).all() and np.isfinite(p).all()
    e = p-y
    mse = float(np.mean(e**2)); rmse = math.sqrt(mse)
    variance = float(np.mean((y-y.mean())**2))
    correlation = float(np.corrcoef(y, p)[0, 1]) if np.ptp(y) and np.ptp(p) else None
    ccc_den = variance + float(np.var(p)) + float((y.mean()-p.mean())**2)
    sd = float(np.std(y, ddof=1)) if len(y)>1 else None
    iqr = float(np.percentile(y, 75, method='linear')-np.percentile(y, 25, method='linear'))
    return {'n':len(y), 'mse':mse, 'rmse':rmse, 'mae':float(np.mean(abs(e))),
        'mean_error':float(e.mean()), 'median_absolute_error':float(np.median(abs(e))),
        'r2':None if not variance else 1-mse/variance,
        'residual_std':float(np.std(e, ddof=1)) if len(y)>1 else None,
        'observed_std':sd, 'observed_iqr':iqr, 'pearson_r':correlation,
        'pearson_r2':None if correlation is None else correlation**2,
        'lin_ccc':None if not ccc_den else float(2*np.mean((y-y.mean())*(p-p.mean()))/ccc_den),
        'rpd':None if not rmse or sd is None else sd/rmse,
        'rpiq':None if not rmse else iqr/rmse}

def compare(expected, calculated, where, checks, failures):
    errors = {}
    for key,value in calculated.items():
        actual = expected.get(key)
        if actual is None or value is None:
            passed = actual is value
        else:
            passed = math.isclose(actual,value,rel_tol=1e-10,abs_tol=1e-10)
            errors[key] = abs(actual-value)
        if not passed: failures.append({'scope':where,'metric':key,'recorded':actual,'recomputed':value})
    checks.append({'scope':where,'metric_count':len(calculated),'max_absolute_error':max(errors.values(),default=0)})

def intervals(rows, level):
    available = [r for r in rows if r.get('lower') is not None and r.get('upper') is not None]
    for r in rows:
        assert (r.get('lower') is None) == (r.get('upper') is None)
        if r.get('lower') is None:
            assert r.get('interval_status')=='unavailable' and r.get('reason_code')
    if not available: return {'n':len(rows),'available_n':0,'unavailable_n':len(rows)}, {}
    y=np.array([r['observed'] for r in available]); lo=np.array([r['lower'] for r in available]); hi=np.array([r['upper'] for r in available])
    assert np.isfinite(y).all() and np.isfinite(lo).all() and np.isfinite(hi).all() and (hi>=lo).all()
    alpha=1-level; width=hi-lo
    pinball=lambda q,z:float(np.mean(np.maximum(q*z,(q-1)*z)))
    return {'n':len(rows),'available_n':len(available),'unavailable_n':len(rows)-len(available),
        'covered_n':int(((lo<=y)&(y<=hi)).sum()),'coverage_denominator':len(available)}, {
        'coverage':float(np.mean((lo<=y)&(y<=hi))), 'mean_width':float(width.mean()),
        'median_width':float(np.median(width)), 'below_lower_fraction':float(np.mean(y<lo)),
        'above_upper_fraction':float(np.mean(y>hi)), 'pinball_lower':pinball(alpha/2,y-lo),
        'pinball_upper':pinball(1-alpha/2,y-hi),
        'interval_score':float(np.mean(width+2/alpha*np.maximum(lo-y,0)+2/alpha*np.maximum(y-hi,0)))}

def extract():
    original = read(EVIDENCE/'artifact-readback-D.json')
    cancel = read(EVIDENCE/'cancel-roundtrip-readback-D.json')
    metadata = read(EVIDENCE/'metadata-readback-D.json')
    result = {'schema_version':1,'created_utc':datetime.now(timezone.utc).isoformat(),
        'scope':'Historical actual ArcMap D runs only. No new model jobs, code-package equivalence claim, or live MCP acceptance.',
        'source_evidence':[file_record(EVIDENCE/n) for n in ['artifact-readback-D.json','cancel-roundtrip-readback-D.json','metadata-readback-D.json']],
        'ui_tested_packages':{}, 'runs':[], 'failed_checks':[],
        'cancel_roundtrip':cancel, 'metadata_acceptance':metadata,
        'new_0_5_mcp_acceptance':{'status':'see_separate_acceptance','report':str(ROOT/'ASAMA_05_KABUL.md'),'evidence':str(ROOT/'build/mcp-scientific-readback.json'),'scope':'Different 0.5 candidate, actual MCP run; not retroactive proof for historical D jobs.'}}
    for label,record in [('three_model_runs',original['ui_tested_package']),('metadata_ridge',metadata['ui_tested_package'])]:
        path=Path(record['path']); actual=digest(path); assert actual==record['sha256']
        with zipfile.ZipFile(path) as archive:
            config=archive.read('Config.xml').decode('utf-8-sig')
        version=re.search(r'<Version>(.*?)</Version>',config).group(1)
        result['ui_tested_packages'][label]={**file_record(path),'version':version,'recorded_sha256_matches':True}
    for original_run in original['runs']:
        path=Path(original_run['manifest']['path']).parent
        manifest=read(path/'result.json'); full=read(path/'evaluation.json'); ev=full['evaluation']
        final=read(path/'final_record.json'); request=read(path/'request.snapshot.json'); audit=read(path/'audit.json')
        assert final==ev['final_record'] and manifest['status']=='completed'
        checked=[]
        for item in manifest['outputs']:
            target=(path/item['path']).resolve(); assert target.is_relative_to(path.resolve())
            actual=digest(target); assert actual==item['sha256'],str(target)
            checked.append({'path':item['path'],'sha256':actual,'kind':item['kind'],'bytes':target.stat().st_size})
        for source in audit['sources']: assert digest(source['path'])==source['sha256']
        with Path(request['samples']['path']).open(encoding='utf-8-sig',newline='') as stream: source_rows=list(csv.DictReader(stream))
        id_field=request['samples']['id_field']; target_field=request['samples']['target_field']; date_field=request['samples']['date_field']
        by_id={r[id_field]:r for r in source_rows}; dates={k:v[date_field] for k,v in by_id.items()}
        group=lambda ids: {tuple(by_id[i][f] for f in request['samples']['group_fields']) for i in ids}
        checks=[]; failures=[]
        def verify_rows(rows,metrics,scope):
            for row in rows:
                assert math.isclose(row['observed'],float(by_id[row['sample_id']][target_field]),rel_tol=0,abs_tol=1e-12)
                assert math.isclose(row['residual'],row['predicted']-row['observed'],rel_tol=0,abs_tol=1e-12)
            compare(metrics,point_metrics(rows),scope,checks,failures)
        verify_rows(ev['oof'],ev['metrics']['pooled_oof'],'pooled_oof')
        for repeat in ev['metrics']['per_repeat']:
            verify_rows([r for r in ev['oof'] if r['repeat']==repeat['repeat']],repeat,'repeat/'+str(repeat['repeat']))
        records=[('final',final)]+[(f"outer/{f['repeat']}/{f['fold']}",f['model_record']) for f in ev['fold_metrics']]
        for role,rec in records:
            dev=set(rec['development_ids']);cal=set(rec['calibration_ids'])
            assert not dev&cal and not group(dev)&group(cal)
            assert {r['sample_id'] for r in rec['training_predictions']}==dev
            assert {r['sample_id'] for r in rec['calibration_predictions']}==cal
            verify_rows(rec['training_predictions'],rec['train_metrics'],role+'/training')
            verify_rows(rec['calibration_predictions'],rec['calibration_metrics'],role+'/calibration')
            if request['validation']['method']=='temporal': assert max(dates[i] for i in dev)<min(dates[i] for i in cal)
        for f in ev['fold_metrics']:
            key=(f['repeat'],f['fold']); outer=next(x for x in ev['folds'] if (x['repeat'],x['fold'])==key)
            train=set(outer['train_ids']);test=set(outer['test_ids']);rec=f['model_record']
            assert not train&test and not group(train)&group(test)
            assert set(rec['development_ids'])|set(rec['calibration_ids'])<=train
            rows=[r for r in ev['oof'] if (r['repeat'],r['fold'])==key]
            assert {r['sample_id'] for r in rows}==test
            verify_rows(rows,f['metrics'],f'outer/{key}/test')
            if request['validation']['method']=='temporal': assert max(dates[i] for i in train)<min(dates[i] for i in test)
        for trial in ev['trials']:
            assert trial['status']=='complete' and trial['validation_status']=='complete'
            verify_rows(trial['validation_predictions'],trial['validation_metrics'],trial['role']+'/validation')
            for fm in trial['inner_fold_metrics']:
                fold=next(f for f in trial['inner_folds'] if (f['repeat'],f['fold'])==(fm['repeat'],fm['fold']))
                assert not set(fold['train_ids'])&set(fold['test_ids']) and not group(fold['train_ids'])&group(fold['test_ids'])
                assert fm['training_sample_count']==len(fold['train_ids']) and fm['test_sample_count']==len(fold['test_ids'])
                rows=[r for r in trial['validation_predictions'] if (r['repeat'],r['fold'])==(fm['repeat'],fm['fold'])]
                verify_rows(rows,fm['metrics'],trial['role']+f"/inner/{fm['repeat']}/{fm['fold']}")
                if request['validation']['method']=='temporal': assert max(dates[i] for i in fold['train_ids'])<min(dates[i] for i in fold['test_ids'])
        coverage=[]
        for row in ev['coverage_diagnostics']['pooled_oof']:
            level=row['nominal_coverage']; rows=[r for r in ev['coverage_diagnostics']['predictions'] if r['nominal_coverage']==level]
            counts,metrics=intervals(rows,level); expected=row.get('available_subset',row)
            compare(expected,metrics,f'coverage/{level}',checks,failures)
            if 'available_n' in row: assert counts['available_n']==row['available_n'] and counts['unavailable_n']==row['unavailable_n']
            coverage.append({'nominal_coverage':level,'status':row['status'],**counts,**metrics})
        assert ev['metrics']['independent'] is None and ev['independent_predictions']==[]
        tables={}
        for file in sorted(path.glob('*.csv')):
            with file.open(encoding='utf-8-sig',newline='') as stream: rows=list(csv.DictReader(stream))
            tables[file.name]={**file_record(file),'data_rows':len(rows),'fields':list(rows[0]) if rows else file.read_text(encoding='utf-8-sig').splitlines()[0].split(',')}
        rr={'run_id':manifest['run_id'],'model':final['model']['id'],'output_directory':str(path),
            'historical_ui_readback':original_run,'manifest':file_record(path/'result.json'),
            'current_readback':{'outputs_verified':len(checked),'source_files_verified':len(audit['sources']),
                'point_metric_groups_recomputed':sum(c['metric_count']==15 for c in checks),
                'all_recorded_point_metrics_recomputed':not failures,'recorded_group_and_time_separation_verified':True,
                'checks':checks,'outputs':checked,'failures':failures},
            'source_inputs':audit['sources'],'request':request,'counts':full['counts'],
            'metrics':ev['metrics'],'final_record':final,'outer_fold_records':ev['fold_metrics'],
            'trials':ev['trials'],'outer_folds':ev['folds'],'coverage_recomputed':coverage,
            'coverage_diagnostics':ev['coverage_diagnostics'], 'subgroup_diagnostics':ev['subgroup_diagnostics'],
            'mapping_diagnostics':ev['mapping_diagnostics'],'preflight':read(path/'preflight.json'),
            'independent_test_history':ev['independent_test_history'],'tables':tables,
            'pagination':read(path/'study_summary.json')['pagination'],
            'unavailable_outputs':{'independent_metrics':'not supplied','inner_training_errors':'not computed in these runs',
                'grid_search':'not run; manual one candidate per search', 'held_out_permutation':'not requested',
                'raw_sampled_covariate_table':'not present in historical outputs',
                'study_package_archive':'not present in historical outputs; added later'}}
        result['runs'].append(rr); result['failed_checks'].extend(failures)
    assert not result['failed_checks']
    result['status']='passed_historical_artifact_readback; newer_MCP_acceptance_reported_separately'
    return result

def fmt(value):
    if value is None:return 'Not produced'
    if isinstance(value,int):return str(value)
    return format(value,'.8g')

def link(path,label=None):
    return f'[{label or Path(path).name}](<{Path(path).resolve().as_posix()}>)'

def table(headers,rows):
    return '\n'.join(['| '+' | '.join(headers)+' |','| '+' | '.join(['---']*len(headers))+' |']+['| '+' | '.join(map(str,row))+' |' for row in rows])

def report(data):
    lines=['# Stage D acceptance report','', '18 October 2026. The real ArcMap jobs reviewed here were run on 17 October 2026.', '',
        'Running the three experimental models from within ArcMap was verified for the recorded 0.4.1 candidate. The Model Context Protocol (MCP) acceptance of the later version 0.5 used a separate candidate.',
        'Evidence and open limits of the new run: '+link(ROOT/'ASAMA_05_KABUL.md','0.5 acceptance report')+'.', '',
        'This document reports a synthetic software acceptance. The target `soil_value` is a synthetic variable in g/kg. The results are not evidence of the accuracy of a real soil property or of pedological validity.', '',
        'The re-check described here only read existing files. No new training, user interface (UI) action or live process test was started. The numbers were recomputed from the recorded predictions with independent formulas.', '',
        '- '+link(ROOT/'build/d-final-scientific-evidence.json','Numerical records and checks'),
        '- '+link(ROOT/'build/create_d_acceptance_evidence.py','Readback script'), '',
        '## Candidates and limits of the evidence','',
        'The Secure Hash Algorithm 256-bit (SHA-256) was used to compare file bytes. Equal digests do not show that different software candidates behave equivalently.', '',
        table(['Scope','Version','Package SHA-256','Status'],[
            ['TabM, RealMLP, TabPFN and cancellation/reopening',data['ui_tested_packages']['three_model_runs']['version'],data['ui_tested_packages']['three_model_runs']['sha256'],'Real UI runs exist'],
            ['Metadata preservation and Ridge',data['ui_tested_packages']['metadata_ridge']['version'],data['ui_tested_packages']['metadata_ridge']['sha256'],'Separate real UI candidate'],
            ['New 0.5 Model Context Protocol (MCP) connection','0.5','Candidate 1c247131... in the separate report','See the new acceptance report']]),'',
        'MCP is the protocol that lets a client call local ArcMap operations. The earlier model results do not mean that this new connection was verified.', '',
        'Original checks:', '',
        *['- '+link(EVIDENCE/name) for name in ['artifact-readback-D.json','cancel-roundtrip-readback-D.json','metadata-readback-D.json']], '',
        '## Data and validation design','',
        'The three runs used the same 48 synthetic observations and two raster bands. The `profile` field has 48 separate groups, and the `observation_date` field has 24 date periods.',
        'The observations have point support. The coordinate reference system (CRS) is EPSG:32637, and the distance unit is meters. Depth interval and laboratory method were not recorded.',
        'The missing-value fraction of both covariates is zero. Median imputation and standardization were selected. Target transformation, variable selection and multicollinearity-based elimination are off.', '',
        'For each observation, the out-of-fold (OOF) prediction comes from the outer-fold model that did not use that observation in training. There is one repeat; stability across repeats was not tested.', '',
        table(['Model','Job ID','Outer/inner folds','Tested in outer folds','Initial history only','Final training/calibration','Independent test'],[
            [r['model'],r['run_id'],'2 / 2',r['counts']['oof_unique_samples'],r['preflight'].get('initial_training_count',0),f"{len(r['final_record']['development_ids'])} / {len(r['final_record']['calibration_ids'])}",0] for r in data['runs']]),'',
        'TabM and RealMLP used a temporal split in which new groups are tested on later dates. The initial training history is 12 periods. In the inner folds, the initial history is four periods.',
        'The calendar gap and the spatial buffer are zero. The first 24 observations do not enter the outer error calculation. These observations can be used in the training history of later periods.',
        'TabPFN used a two-fold group split, and all 48 observations were tested in an outer fold. This design is not a validation of temporal transfer.',
        'The recorded training, calibration and test IDs and the group separations were rechecked. In the temporal runs, the training dates of the outer and inner folds precede the test dates.', '',
        'The date ranges are the same for TabM and RealMLP:', '',
        table(['Partition','Training dates','Calibration dates','Outer test dates'],[
            ['Outer fold '+str(f['fold']),f['model_record']['temporal']['development_dates']['start']+' / '+f['model_record']['temporal']['development_dates']['end'],
             f['model_record']['temporal']['calibration_dates']['start']+' / '+f['model_record']['temporal']['calibration_dates']['end'],
             data['runs'][0]['outer_folds'][i]['diagnostics']['temporal']['test_start']+' / '+data['runs'][0]['outer_folds'][i]['diagnostics']['temporal']['test_end']]
            for i,f in enumerate(data['runs'][0]['outer_fold_records'])]+[
            ['Final model','2020-01-01 / 2020-02-09','2020-02-12 / 2020-03-10','No independent test']]), '',
        'Input files:', '',
        '- '+link(ROOT/'examples-d/synthetic-dated-samples.csv','48 synthetic observations'),
        '- '+link(ROOT/'examples-d/synthetic-covariates.tif','Two-band synthetic raster'), '',
        '## Actual model settings','',
        'Hyperparameters are model settings chosen before training starts. These three runs used a single manually specified candidate; no grid search was done.',
        '`optimization.trials=10` is the recorded top-level setting. The actual budget in the preflight check is one per search. Each run has three search records and six inner-fold evaluations.',
        'The three searches are the separate selection procedures for the final model and the two outer folds. They do not mean three different model settings. All actual trials completed; there is no failed trial.', '',
        table(['Model','Requested parameters','Wrapper parameters actually constructed'],[[r['model'],'`'+json.dumps(r['final_record']['model']['parameters'],ensure_ascii=False)+'`','`'+json.dumps(r['final_record']['effective_parameters'],ensure_ascii=False)+'`'] for r in data['runs']]),'',
        'All three models used the central processing unit (CPU), one thread and random seed 42. These jobs do not count as graphics processing unit (GPU) acceptance.',
        'TabM and RealMLP completed two epochs. The random validation split and early stopping of the upstream library were not used. These short training runs are for software acceptance.',
        'The TabPFN records show zero gradient updates. The pretrained weights are fixed; only the data of the relevant training partition were given to the model context.', '',
        table(['Model','Upstream library version','PyTorch','Peak memory of final training, bytes','Duration of final operation, seconds'],[[r['model'],r['final_record']['experimental_model_diagnostics']['versions'].get('tabm' if r['model']=='tabm' else 'pytabkit' if r['model']=='realmlp' else 'tabpfn'),r['final_record']['experimental_model_diagnostics']['versions']['torch'],r['final_record']['experimental_model_diagnostics']['peak_rss_bytes'],fmt(r['final_record']['experimental_model_diagnostics']['duration_seconds'])] for r in data['runs']]),'',
        'Memory and duration refer to the final model operation, not to the whole job. The measured performance is limited to this small dataset and this CPU setting.',
        'All constructor parameters of the upstream libraries, the RealMLP configuration, the TabM optimization defaults and the TabPFN inference settings are kept in the model records and hyperparameter tables.', '']
    weight=data['runs'][2]['final_record']['experimental_model_diagnostics']['weight']
    lines += [f"The TabPFN weight file `{weight['filename']}` has {weight['bytes']} bytes. Revision `{weight['revision']}`; SHA-256 `{weight['sha256']}`.",
        f"The recorded license is `{weight['license']}`, and the required attribution text is `{weight['required_attribution']}`. This information comes from the run record; no new license acceptance was made.", '',
        '## Scope of the error metrics','',
        'Root mean squared error (RMSE), mean absolute error (MAE) and mean error (ME) are given in the original target unit, g/kg. For ME, the sign is prediction minus observation.',
        'The coefficient of determination is R² = 1 - sum of squared errors / sum of squared deviations of the observations from their mean. Negative values are kept. The squared Pearson correlation is not the same as this metric.',
        'The final training error was computed on training observations that the model saw. The calibration error is a diagnostic on the held-out observations used to build the intervals. Neither is independent performance.', '',
        table(['Model','Scope','n','RMSE','MAE','ME','R²'],[[r['model'],scope,m['n'],fmt(m['rmse']),fmt(m['mae']),fmt(m['mean_error']),fmt(m['r2'])] for r in data['runs'] for scope,m in [('Final training',r['final_record']['train_metrics']),('Final calibration',r['final_record']['calibration_metrics']),('Pooled out-of-fold',r['metrics']['pooled_oof'])]]), '',
        'The pooled out-of-fold R² of TabM is about -0.00000185; it was not rounded to zero and presented as positive performance. In the first outer fold, R² is also negative for TabM and RealMLP.', '',
        table(['Model','Outer fold','Training','Calibration','Test','RMSE','MAE','R²'],[[r['model'],f['fold'],len(f['model_record']['development_ids']),len(f['model_record']['calibration_ids']),f['metrics']['n'],fmt(f['metrics']['rmse']),fmt(f['metrics']['mae']),fmt(f['metrics']['r2'])] for r in data['runs'] for f in r['outer_fold_records']]),'',
        'The training and calibration diagnostics of the outer-fold models were also recorded separately:', '',
        table(['Model','Outer fold','Scope','n','RMSE','MAE','R²'],[[r['model'],f['fold'],scope,m['n'],fmt(m['rmse']),fmt(m['mae']),fmt(m['r2'])] for r in data['runs'] for f in r['outer_fold_records'] for scope,m in [('Training',f['model_record']['train_metrics']),('Calibration',f['model_record']['calibration_metrics'])]]), '',
        'Fold numbers start at zero, as in the original record. The mean of the fold RMSE values and the RMSE computed from all out-of-fold predictions are different quantities.', '',
        table(['Model','Mean of fold RMSE','Pooled out-of-fold RMSE','Mean of fold R²','Pooled out-of-fold R²'],[[r['model'],fmt(r['metrics']['fold_mean']['rmse']),fmt(r['metrics']['pooled_oof']['rmse']),fmt(r['metrics']['fold_mean']['r2']),fmt(r['metrics']['pooled_oof']['r2'])] for r in data['runs']]),'',
        'None of the three runs has an independent test ID or an independent prediction row. `independent=null` and `not_supplied` are kept. No independent error, coverage or performance in an external region can be produced.', '',
        '## Inner validation records','',
        'The error below was computed from the inner-fold predictions of each search. It is used to select settings and does not replace the outer validation error.', '',
        table(['Model','Search role','Trial','Inner prediction n','RMSE','MAE','R²','Status'],[[r['model'],t['role'],t['trial'],t['validation_metrics']['n'],fmt(t['validation_metrics']['rmse']),fmt(t['validation_metrics']['mae']),fmt(t['validation_metrics']['r2']),t['validation_status']] for r in data['runs'] for t in r['trials']]),'',
        table(['Model','Search role','Inner fold','Training/test n','RMSE','MAE','R²'],[[r['model'],t['role'],f['fold'],str(f['training_sample_count'])+' / '+str(f['test_sample_count']),fmt(f['metrics']['rmse']),fmt(f['metrics']['mae']),fmt(f['metrics']['r2'])] for r in data['runs'] for t in r['trials'] for f in t['inner_fold_metrics']]), '',
        'For each inner fold, the training/test counts, the number of groups, the selected variables and the actual model settings are in `evaluation.json`. Inner-fold training predictions were not computed in these earlier jobs.',
        'Therefore no inner training error was fabricated afterwards. Inner test metrics, outer training/calibration metrics and final training/calibration metrics are available in separate sources.', '',
        '## Additional accuracy metrics','',
        "Pearson r summarizes the linear relationship, and Lin's concordance correlation coefficient (CCC) summarizes the agreement between observation and prediction. CCC uses variance and covariance with denominator n.",
        'The ratio of the observed standard deviation to RMSE (RPD) and the ratio of the observed interquartile range to RMSE (RPIQ) were also recorded. No universal performance classes were assigned to these ratios.',
        'Residual and observed standard deviations use denominator n-1. Quartiles were computed by linear interpolation. Undefined metrics are left empty with their reasons; none of these pooled out-of-fold metrics is undefined.', '',
        table(['Model','Mean squared error','Median absolute error','Residual standard deviation','Pearson r','Pearson r²','CCC','RPD','RPIQ'],[[r['model']]+[fmt(r['metrics']['pooled_oof'][k]) for k in ['mse','median_absolute_error','residual_std','pearson_r','pearson_r2','lin_ccc','rpd','rpiq']] for r in data['runs']]),'',
        'Mean squared error is in (g/kg)². Median absolute error and residual standard deviation are in g/kg; the other columns are unitless. The additional metrics of all roles are kept in the numerical appendix.', '',
        '## Prediction intervals and missing local support','',
        'TabM and RealMLP used experimental spatially weighted conformal prediction with separate calibration data. This method weights calibration residuals by their distance to the prediction location.',
        'A Gaussian kernel, a prespecified 300 m bandwidth and 80 percent nominal coverage were used. The minimum effective number of observations is two, and the minimum number of groups is two.',
        'The recorded method claims no exact finite-sample or spatial coverage guarantee. The static rasters also do not validate a map for any specific future date.',
        'TabPFN used split conformal prediction based on separate calibration. The final calibration recorded 20 observations, rank 17 for 80 percent and an adjustment of 0.17716868137517494 g/kg.',
        'This final adjustment is not the interval width of the outer-fold models. Outer-fold coverage was computed with the calibration set of each fold.', '',
        table(['Model','Nominal coverage','With interval/total','Covered/with interval','Empirical coverage','Mean width, g/kg','Status'],[[r['model'],fmt(c['nominal_coverage']),f"{c['available_n']}/{c['n']}",f"{c.get('covered_n',0)}/{c['available_n']}",fmt(c.get('coverage')),fmt(c.get('mean_width')),c['status']] for r in data['runs'] for c in r['coverage_recomputed']]),'',
        'At the 80 percent level, the local calibration weight was not enough to produce a finite interval for seven observations with TabM and RealMLP. The reason code was recorded as `SPATIAL_FINITE_MASS`.',
        'These seven observations stayed in the point error calculation but did not enter the coverage denominator. The ratios 16/17 for TabM and 17/17 for RealMLP are not coverage of all 24 observations.',
        'At the 90 percent level, these two models produced an interval for only one observation. The 1/1 result cannot be interpreted as overall coverage performance.',
        'Where a land-use subgroup has missing intervals, its full subgroup coverage was left unavailable with `INCOMPLETE_INTERVALS`. No partial subgroup coverage was derived.', '',
        '## Rasters, reports and model record','',
        table(['Model','Verified outputs','Rasters','Valid point prediction cells','Interval cells','Cells with covariates out of range','Report pages','Report data rows'],[[r['model'],r['current_readback']['outputs_verified'],len(r['historical_ui_readback']['rasters']),r['historical_ui_readback']['rasters']['prediction.tif']['valid_pixels'],r['historical_ui_readback']['rasters']['prediction_lower.tif']['valid_pixels'],r['mapping_diagnostics']['diagnostic_pixels']['range_extrapolation'],len(r['pagination']['pages']),r['pagination']['source_data_rows']] for r in data['runs']]),'',
        'The rasters have 8 × 8 cells. Point prediction, lower/upper bound and width are in g/kg; the distance raster is in meters. Diagnostic rasters are unitless.',
        'A nominal interval was produced for all 64 map cells. Therefore, keeping the point prediction in an unsupported map cell was not actually tested in these three real jobs.',
        'Producing a valid interval does not show that it covers the true target value of the cell. The raster cells have no validation targets.',
        'The largest difference between upper bound minus lower bound and the width raster is 7.152557373046875e-07 g/kg for TabM and RealMLP, and 3.2782554626464844e-07 g/kg for TabPFN.',
        'The paginated reports have at most 120 table rows, including the header. The data rows of the original reports are kept in order. All pages are covered by the file digests of the result manifest.',
        'The model packages were saved. Preprocessing, selected variables, target transformation, model and calibration records were bound to the same run. Reloading the model for a new scientific study is not tested in this report.', '',
        table(['Model','Model package SHA-256'],[[r['model'],r['historical_ui_readback']['model_package_sha256']] for r in data['runs']]),'',
        '## Cancellation, reopening and metadata acceptance','',
        'For the cancelled job `20261017_221257_a318fea3`, the result status is cancelled and the exit code is 130. The list of completed outputs is empty; no partial raster or model residue was found.',
        'The last process query targeting the recorded worker and child process IDs returned nothing. This applies to the moment of that observation; no new process query was made while preparing this report.',
        'The 121, 131 and 116 outputs of the three earlier successful runs did not change. The total of 368 files is the number of outputs of these three runs, not a number of independent observations.',
        'When the project was reopened and saved, 13 persistent fields stayed the same. Four transient fields of the preview session were excluded from scope; the only actual difference was in the preview manifest path.',
        'The digest of the TabM model package exported from the UI equals that of the source package: `00fda14fd026301ba3f764f6f27740e224bdd0bb1ac7386da52194f3089ae530`.',
        'In the later, separate candidate, the types of seven raster metadata fields were preserved. The D000 exclusion in `samples.excluded` stayed the same in the source, the UI save and the job request.',
        'This separate Ridge job, `20261017_224753_617460c0`, completed with 47 retained observations. D000 is absent from the training, validation and prediction records. The digests of 111 outputs were verified.',
        '`target_derived=true`, which marks a raster derived from target observations, was rejected with `TARGET_LEAKAGE` before a preview request file was created. The Run model button stayed disabled.',
        'The cancellation/reopening evidence recorded 37 passed checks, and the separate metadata evidence recorded 34. These numbers are checks of UI tasks, not scientific sample sizes.', '',
        '## Access to files','',
        'The links below lead to the complete original files. The comma-separated values (CSV) tables contain role and fold information. The JavaScript Object Notation (JSON) records keep nested model and setting details.', '']
    for r in data['runs']:
        path=Path(r['output_directory'])
        lines += [f"### {r['model']}: {r['run_id']}",'',table(['Content','Complete file'],[
            ['Report index',link(path/'study_report_index.html')],['Full study report',link(path/'study_report.html')],
            ['All role metrics',link(path/'publication_metrics.csv')],['All hyperparameter records',link(path/'hyperparameters.csv')],
            ['Actual search trials',link(path/'optimization_trials.csv')+'; '+link(path/'optimization_trials.json')],
            ['Inner validation predictions',link(path/'inner_validation_predictions.csv')],['Out-of-fold predictions',link(path/'oof_predictions.csv')],
            ['Training and calibration predictions',link(path/'training_predictions.csv')+'; '+link(path/'calibration_predictions.csv')],
            ['Outer-fold metrics',link(path/'fold_metrics.csv')],['Coverage and interval rows',link(path/'coverage_diagnostics.csv')+'; '+link(path/'coverage_predictions.csv')],
            ['Subgroup diagnostics',link(path/'subgroup_diagnostics.csv')],['Independent prediction file, zero rows',link(path/'independent_predictions.csv')],
            ['Final model record',link(path/'final_record.json')],['All outer and inner model records',link(path/'evaluation.json')],
            ['Fold IDs and learning audit',link(path/'validation_folds.json')+'; '+link(path/'learning_audit.json')],
            ['Request, source and package information',link(path/'request.snapshot.json')+'; '+link(path/'audit.json')],
            ['Model package',link(path/next(item['path'] for item in r['current_readback']['outputs'] if item['kind']=='model_bundle'))],['Manifest of all outputs',link(path/'result.json')]]) ,'']
    lines += ['## Open acceptance items and scientific limits','',
        '- Connection, cancellation and export results of the new candidate: '+link(ROOT/'ASAMA_05_KABUL.md','0.5 acceptance report')+'.',
        '- Real soil data, independent test data and transfer to another area were not tested in these three synthetic jobs.',
        '- TabPFN and the other two models did not use the same outer splits. No superiority among the three models can be inferred from these results.',
        '- Grid search, multiple hyperparameter candidates and the failed-trial path were not run in these three real jobs.',
        '- Inner-fold training error records, a role table with all sampled covariate values in original units and a full study archive are absent from the earlier outputs.',
        '- The separate test of the later `study_package.spsstudy.zip` feature on the new candidate is in the 0.5 report. The three earlier runs did not produce this archive.',
        '- Permutation importance and the comparison without variable selection were not requested in these jobs. Empty tables were not counted as computed findings.',
        '- The behavior of spatial intervals in unsupported raster cells and running on a GPU are not covered by these real jobs.',
        '- Fixed two-epoch training, a single seed and small synthetic data are not evidence of scientific adequacy for TabM or RealMLP.', '',
        'All numerical values are kept in the original files. For readability, the tables in this report show at most eight significant digits; the machine-readable appendix carries full precision.', '']
    return '\n'.join(lines)

if __name__=='__main__':
    evidence=extract()
    target=ROOT/'build/d-final-scientific-evidence.json'
    target.write_text(json.dumps(evidence,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    report_text=report(evidence)
    for target_link in re.findall(r'\]\(<([^>]+)>\)',report_text):
        assert Path(target_link).is_file(),target_link
    (ROOT/'ASAMA_D_KABUL.md').write_text(report_text,encoding='utf-8')
    assert read(target)==evidence
    print(json.dumps({'status':evidence['status'],'runs':len(evidence['runs']),
        'output_hashes_rechecked':sum(r['current_readback']['outputs_verified'] for r in evidence['runs']),
        'point_metric_groups_recomputed':sum(r['current_readback']['point_metric_groups_recomputed'] for r in evidence['runs']),
        'failed_checks':evidence['failed_checks'],'report':str(ROOT/'ASAMA_D_KABUL.md')},ensure_ascii=False))
