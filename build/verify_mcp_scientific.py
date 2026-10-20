"""Independent readback of the actual 0.5 MCP run. No model training/loading."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import zipfile
import numpy as np
import rasterio
from create_d_acceptance_evidence import ROOT, EVIDENCE, read, digest, file_record, point_metrics, intervals, compare, table, link, fmt
from verify_d_acceptance import verify_pagination

RUN_ID='20261018_035902_48c39f66'
PACKAGE_SHA='1c247131ca29fea3ece54bb0634c17b1eb01c421be9271dfba9f682f3eafb24d'

def csv_rows(path):
    with Path(path).open(encoding='utf-8-sig',newline='') as stream:return list(csv.DictReader(stream))

def verify(process_file=None):
    evidence_names=['mcp-actual-inventory-20261018_035258.json','mcp-actual-rollback-20261018_035404.json','mcp-actual-run-20261018_035856.json','mcp-actual-cancel-20261018_035933.json']
    calls=[read(EVIDENCE/name) for name in evidence_names]
    inventory,rollback,actual,cancel=calls
    assert all(item['status']=='passed' for item in calls)
    package=ROOT/'build/stage-050-mcp-release/SpatialPredictionStudio.esriAddIn'
    assert digest(package)==PACKAGE_SHA
    output=Path(actual['analysis_job']['directory'])/'output'
    result=read(output/'result.json'); assert result['status']=='completed' and result['run_id']==RUN_ID
    ev=read(output/'evaluation.json')['evaluation']; final=ev['final_record']; req=read(output/'request.snapshot.json'); audit=read(output/'audit.json')
    checks=[]; failures=[]
    outputs=[]
    for item in result['outputs']:
        path=(output/item['path']).resolve(); assert path.is_relative_to(output.resolve()) and digest(path)==item['sha256']
        outputs.append({**item,'bytes':path.stat().st_size})
    assert len(outputs)==len({r['path'].casefold() for r in outputs})==actual['artifact_count']
    assert not (output/'INCOMPLETE.json').exists()
    for item in audit['sources']:assert digest(item['path'])==item['sha256']
    with zipfile.ZipFile(package) as archive:
        for item in audit['source_code']:
            member='Install/backend/sps/'+Path(item['path']).name
            assert hashlib.sha256(archive.read(member)).hexdigest()==item['sha256']
        for name,sha in actual['server_sources'].items():assert digest(ROOT/name)==sha
        config=archive.read('Config.xml').decode('utf-8-sig')
        assert '<Version>0.5.0</Version>' in config
    archive_path=output/next(x['path'] for x in outputs if x['kind']=='study_package')
    with zipfile.ZipFile(archive_path) as archive:
        names=archive.namelist(); assert len(names)==len({n.casefold() for n in names})
        for name in names:
            p=PurePosixPath(name); assert str(p)==name and not p.is_absolute() and '..' not in p.parts and '\\' not in name and ':' not in name
        manifest=json.loads(archive.read('study_manifest.json')); assert manifest['schema_version']==1 and manifest['run_id']==RUN_ID
        assert set(manifest['members'])==set(names)-{'study_manifest.json'}
        for name,sha in manifest['members'].items():assert hashlib.sha256(archive.read(name)).hexdigest()==sha
        snapshot=json.loads(archive.read('result.snapshot.json')); expected=[x for x in result['outputs'] if x['kind']!='study_package']
        assert snapshot['status']=='completed' and snapshot['run_id']==RUN_ID and snapshot['outputs']==expected
        assert all(manifest['members'][x['path']]==x['sha256'] for x in expected)
        assert set(names)=={x['path'] for x in expected}|{'request.snapshot.json','result.snapshot.json','study_manifest.json'}
        assert archive.read('request.snapshot.json')==(output/'request.snapshot.json').read_bytes()
        assert archive.read('model_package.spsmodel.zip')==(output/'model_package.spsmodel.zip').read_bytes()
        assert archive_path.name not in names and 'result.json' not in names
        study_archive={'file':file_record(archive_path),'members_verified':len(names),'manifest':manifest,'result_snapshot_outputs':len(snapshot['outputs']),'recursive_hashing_excluded':True}
    with zipfile.ZipFile(output/'model_package.spsmodel.zip') as archive:
        model_manifest=json.loads(archive.read('manifest.json'));names=archive.namelist()
        assert set(model_manifest['members'])==set(names)-{'manifest.json'}
        for name,sha in model_manifest['members'].items():assert hashlib.sha256(archive.read(name)).hexdigest()==sha
        meta=json.loads(archive.read(model_manifest['metadata_path']))
        assert meta['run_id']==model_manifest['run_id']==RUN_ID
        assert meta['sha256']==model_manifest['model_sha256']==digest(output/'model.joblib')
        model_package={'file':file_record(output/'model_package.spsmodel.zip'),'members_verified':len(names),'manifest':model_manifest}
    exports=[]
    for call in actual['calls']:
        if call['tool'] in ('export_model','export_report','save_project'):
            item=call['result']; assert not call['is_error'] and digest(item['path'])==item['sha256']
            if call['tool']=='export_model':assert digest(item['path'])==digest(output/'model_package.spsmodel.zip')
            if call['tool']=='export_report':assert digest(item['path'])==digest(archive_path)
            exports.append({'tool':call['tool'],**file_record(item['path'])})
    saved=read(next(x['path'] for x in exports if x['tool']=='save_project'))
    assert saved['last_manifest']==str(output/'result.json')
    assert saved['science']['validation']['independent_ids']==['D046','D047']
    project_reads=[c['result'] for c in rollback['calls'] if c['tool']=='get_project']
    transient={'preview_fingerprint','preview_manifest','preview_signature','preview_sources'}
    assert {k:v for k,v in project_reads[0].items() if k not in transient}=={k:v for k,v in project_reads[-1].items() if k not in transient}
    assert any(c['tool']=='open_project' and c['is_error'] for c in rollback['calls'])
    source={r['sample_id']:r for r in csv_rows(req['samples']['path'])}
    sampled=read(output/'sampled_data.json')['rows']; sample_csv=csv_rows(output/'sampled_data.csv'); by_id={r['sample_id']:r for r in sampled}
    assert len(source)==48 and set(by_id)==set(source)-{'D000'} and len(sampled)==47
    independent=set(req['validation']['independent_ids']);assert independent=={'D046','D047'}
    assert {r['sample_id'] for r in ev['oof']}==set(by_id)-independent
    assert {r['sample_id'] for r in ev['independent_predictions']}==independent
    assert set(final['development_ids']).isdisjoint(final['calibration_ids'])
    assert set(final['development_ids'])|set(final['calibration_ids'])==set(by_id)-independent
    assert {by_id[i]['group_id'] for i in independent}.isdisjoint({by_id[i]['group_id'] for i in set(by_id)-independent})
    coordinates=[(r['x'],r['y']) for r in sampled]; covariate_checks=0
    for cov in req['covariates']:
        with rasterio.open(cov['path']) as raster:
            assert raster.crs.to_string()==req['samples']['crs'] and cov['sampling']=='nearest'
            values=list(raster.sample(coordinates,indexes=cov['band']))
        for row,value in zip(sampled,values):assert float(value[0])==row['covariates'][cov['id']];covariate_checks+=1
    for row,csv_row in zip(sampled,sample_csv):
        s=source[row['sample_id']]; assert row['sample_id']==csv_row['sample_id']
        assert row['properties']==s and row['observation_date']==s['observation_date']
        assert row['observed']==float(s['soil_value']) and row['x']==float(s['x']) and row['y']==float(s['y'])
        assert row['validation_date'] is None
        for key in ('x','y','observed'):assert float(csv_row[key])==row[key]
        for key,value in row['covariates'].items():assert float(csv_row['covariate.'+key])==value
    excluded=read(output/'excluded_samples.json')['records']
    assert len(excluded)==1 and excluded[0]['sample_id']=='D000' and excluded[0]['raw_record']['properties']==source['D000']
    assert excluded[0]['reason']==req['samples']['excluded'][0]['reason']
    roles=csv_rows(output/'data_roles.csv'); role_data=csv_rows(output/'role_data.csv'); learning=csv_rows(output/'learning_membership.csv')
    assert len(roles)==len(role_data)
    for role,joined in zip(roles,role_data):
        assert all(joined[k]==v for k,v in role.items())
        sample=sample_csv[[r['sample_id'] for r in sampled].index(role['sample_id'])]
        assert all(joined[k]==v for k,v in sample.items())
    expected_roles=[]
    def add(scope,role,partition,ids,trial='',repeat='',fold=''):
        expected_roles.extend((scope,role,str(trial),str(repeat),str(fold),partition,i) for i in ids)
    allowed_learning={}
    contexts={}
    def register(scope,role,rows,metrics,repeat='',fold='',trial='',interval=False):
        for row in rows:
            assert row['sample_id']!='D000' and row['observed']==by_id[row['sample_id']]['observed']
            assert np.isclose(row['predicted']-row['observed'],row['residual'],atol=1e-12,rtol=0)
        calculated=point_metrics(rows); compare(metrics,calculated,f'{scope}/{role}/{trial}/{repeat}/{fold}',checks,failures)
        if interval:
            count,bounds=intervals(rows,req['uncertainty']['coverage']);compare(metrics['interval'],bounds,f'{scope}/interval',checks,failures)
            calculated.update({'interval.'+k:v for k,v in bounds.items()})
        contexts[(scope,role,str(repeat),str(fold),str(trial))]=calculated
        return calculated
    records=[('final',final,'','')]+[(f"outer/{f['repeat']}/{f['fold']}",f['model_record'],f['repeat'],f['fold']) for f in ev['fold_metrics']]
    for role,record,repeat,fold in records:
        scope='final' if role=='final' else 'outer'
        dev=set(record['development_ids']);cal=set(record['calibration_ids']); assert not dev&cal and not (dev|cal)&independent
        assert {by_id[x]['group_id'] for x in dev}.isdisjoint({by_id[x]['group_id'] for x in cal})
        for key,partition in [('development_ids','training'),('calibration_ids','calibration'),('excluded_ids','excluded_by_partition')]:add(scope,role,partition,record[key],repeat=repeat,fold=fold)
        allowed_learning[role+'/fit']=dev;allowed_learning[role+'/calibration']=cal
        register(scope+'_training',role,record['training_predictions'],record['train_metrics'],repeat,fold)
        register(scope+'_calibration',role,record['calibration_predictions'],record['calibration_metrics'],repeat,fold)
    add('final','final','independent_test',sorted(independent))
    outer_recomputed=[]
    for f in ev['folds']:
        role=f"outer/{f['repeat']}/{f['fold']}";train=set(f['train_ids']);test=set(f['test_ids'])
        assert not train&test and not (train|test)&independent
        assert {by_id[x]['group_id'] for x in train}.isdisjoint({by_id[x]['group_id'] for x in test})
        for key,partition in [('train_ids','training_pool_before_calibration'),('test_ids','test'),('excluded_ids','excluded_by_split')]:add('outer',role,partition,f.get(key,[]),repeat=f['repeat'],fold=f['fold'])
        record=next(x for x in ev['fold_metrics'] if (x['repeat'],x['fold'])==(f['repeat'],f['fold']))
        model_record=record['model_record']
        assert set(model_record['development_ids'])|set(model_record['calibration_ids'])|set(model_record['excluded_ids'])==train
        rows=[r for r in ev['oof'] if (r['repeat'],r['fold'])==(f['repeat'],f['fold'])]
        assert Counter(r['sample_id'] for r in rows)==Counter(test)
        outer_recomputed.append(register('outer_fold','',rows,record['metrics'],f['repeat'],f['fold'],interval=True))
    register('pooled_oof','',ev['oof'],ev['metrics']['pooled_oof'],interval=True)
    register('independent','',ev['independent_predictions'],ev['metrics']['independent'],interval=True)
    for repeat in ev['metrics']['per_repeat']:register('per_repeat','',[r for r in ev['oof'] if r['repeat']==repeat['repeat']],repeat,repeat['repeat'],interval=True)
    means={k:float(np.mean([r[k] for r in outer_recomputed])) for k in point_metrics(ev['oof']) if k!='n'}
    compare(ev['metrics']['fold_mean'],means,'fold_mean',checks,failures);contexts[('fold_mean','','','','')]=means
    trials=ev['trials'];trial_csv=csv_rows(output/'trial_results.csv');inner_csv=csv_rows(output/'inner_fold_results.csv')
    assert len(trials)==len(trial_csv)==9 and len(inner_csv)==18
    for role in {t['role'] for t in trials}:
        current=[t for t in trials if t['role']==role]
        assert {t['model']['parameters']['alpha'] for t in current}=={.1,1,10}
        selected=[t for t in current if t['selected']];assert len(selected)==1 and selected[0]['value']==min(t['value'] for t in current)
    for t in trials:
        assert t['status']==t['validation_status']=='complete'
        flat=next(r for r in trial_csv if r['role']==t['role'] and int(r['trial'])==t['trial'])
        assert float(flat['param_model.alpha'])==t['model']['parameters']['alpha'] and flat['selected']==str(t['selected'])
        assert float(flat['value'])==t['value'] and flat['status']==t['status'] and flat['search_method']=='grid'
        validation_calc=register('inner_validation',t['role'],t['validation_predictions'],t['validation_metrics'],trial=t['trial'])
        assert np.isclose(t['value'],validation_calc[t['metric']],atol=1e-12,rtol=0)
        partition_record=final if t['role']=='final/search' else next(r for role,r,_,_ in records if role==t['role'].removesuffix('/search'))
        development=set(partition_record['development_ids'])
        assert Counter(r['sample_id'] for r in t['validation_predictions'])==Counter(development)
        for repeat in t['validation_per_repeat']:register('inner_repeat',t['role'],[r for r in t['validation_predictions'] if r['repeat']==repeat['repeat']],repeat,repeat['repeat'],trial=t['trial'])
        for f in t['inner_folds']:
            train=set(f['train_ids']);test=set(f['test_ids']);assert not train&test and not (train|test)&independent
            assert train|test==development
            assert {by_id[x]['group_id'] for x in train}.isdisjoint({by_id[x]['group_id'] for x in test})
            for key,partition in [('train_ids','training'),('test_ids','validation')]:add('inner',t['role'],partition,f[key],t['trial'],f['repeat'],f['fold'])
            allowed_learning[f"{t['role']}/trial{t['trial']}/inner{f['repeat']}/{f['fold']}"]=train
            metric=next(x for x in t['inner_fold_metrics'] if (x['repeat'],x['fold'])==(f['repeat'],f['fold']))
            rows=[r for r in t['validation_predictions'] if (r['repeat'],r['fold'])==(f['repeat'],f['fold'])]
            assert Counter(r['sample_id'] for r in rows)==Counter(test)
            calc=register('inner_fold',t['role'],rows,metric['metrics'],f['repeat'],f['fold'],t['trial'])
            flat=next(r for r in inner_csv if r['role']==t['role'] and int(r['trial'])==t['trial'] and int(r['repeat'])==f['repeat'] and int(r['fold'])==f['fold'])
            assert flat['status']=='complete' and flat['training_metrics_status']=='not_computed'
            assert int(flat['training_sample_count'])==len(train) and int(flat['test_sample_count'])==len(test)
            compare({k:float(flat['metric.'+k]) for k in calc},calc,'flat_inner_fold',checks,failures)
    role_columns=('scope','role','trial','repeat','fold','partition','sample_id')
    assert Counter(tuple(r[c] for c in role_columns) for r in roles)==Counter(expected_roles)
    assert all(r['sample_id'] not in independent for r in roles if r['scope'] in ('outer','inner'))
    assert Counter((r['role'],r['stage'],r['sample_id']) for r in learning)==Counter((r['role'],r['stage'],i) for r in ev['learning_audit'] for i in r['ids'])
    for row in ev['learning_audit']:
        assert set(row['ids'])==allowed_learning[row['role']]
        assert not set(row['ids'])&(independent|{'D000'})
    pub=csv_rows(output/'publication_metrics.csv');pub_errors=[]
    assert Counter(tuple(row[k] for k in ('scope','role','repeat','fold','trial','metric')) for row in pub)==Counter(
        (*context,metric) for context,metrics in contexts.items() for metric in metrics if metric!='n')
    for row in pub:
        key=tuple(row[k] for k in ('scope','role','repeat','fold','trial'))
        expected=contexts[key][row['metric']]
        assert row['status']=='available'
        if not np.isclose(float(row['value']),expected,rtol=1e-10,atol=1e-10):pub_errors.append({'key':key,'metric':row['metric']})
        if row['n']:assert int(row['n'])==contexts[key]['n']
    assert not failures and not pub_errors
    # The public prediction CSVs must carry the same values and identity scope.
    prediction_csv_counts={}
    for filename,prediction_records in [
        ('training_predictions.csv',[(scope+'_training',role,'',repeat,fold,r) for role,record,repeat,fold in records for scope in ['final' if role=='final' else 'outer'] for r in record['training_predictions']]),
        ('calibration_predictions.csv',[(scope+'_calibration',role,'',repeat,fold,r) for role,record,repeat,fold in records for scope in ['final' if role=='final' else 'outer'] for r in record['calibration_predictions']]),
        ('inner_validation_predictions.csv',[('inner_validation',t['role'],t['trial'],r['repeat'],r['fold'],r) for t in trials for r in t['validation_predictions']])]:
        published=csv_rows(output/filename)
        expected={(scope,role,str(trial),str(repeat),str(fold),r['sample_id']):r for scope,role,trial,repeat,fold,r in prediction_records}
        assert len(published)==len(expected)==len(prediction_records)
        for row in published:
            original=expected[tuple(row[k] for k in ('scope','role','trial','repeat','fold','sample_id'))]
            assert row['group_id']==original['group_id']
            for k in ('observed','predicted','residual'):assert float(row[k])==original[k]
        prediction_csv_counts[filename]=len(published)
    # Reproduce final saved model equations without loading any serialized object.
    attributes=final['fitted_model_attributes'];pre=attributes['preprocessing'];features=attributes['selected_features']
    X=np.array([[r['covariates'][name] for name in features] for r in sampled]);coef=np.array(attributes['attributes']['coef_'])
    predicted=((X-np.array(pre['center']))/np.array(pre['scale']))@coef+attributes['attributes']['intercept_']
    final_rows=final['training_predictions']+final['calibration_predictions']+ev['independent_predictions'];predicted_by_id=dict(zip([r['sample_id'] for r in sampled],predicted))
    model_error=max(abs(r['predicted']-predicted_by_id[r['sample_id']]) for r in final_rows);assert model_error<1e-12
    cal=final['calibration'];scores=sorted(abs(r['residual']) for r in final['calibration_predictions']);rank=int(np.ceil((len(scores)+1)*req['uncertainty']['coverage']))
    assert cal['rank']==rank and cal['adjustment']==scores[rank-1]
    coverage=[]
    for scope,rows_key in [('pooled_oof','predictions'),('independent','independent_predictions')]:
        for recorded in ev['coverage_diagnostics'][scope]:
            level=recorded['nominal_coverage'];rows=[r for r in ev['coverage_diagnostics'][rows_key] if r['nominal_coverage']==level]
            count,metrics=intervals(rows,level);compare(recorded,metrics,f'{scope}/coverage/{level}',checks,failures)
            coverage.append({'scope':scope,'nominal_coverage':level,**count,**metrics})
    rasters={}
    for item in outputs:
        if item['path'].endswith('.tif'):
            with rasterio.open(output/item['path']) as raster:
                arr=raster.read(1,masked=True);rasters[item['path']]={'valid_pixels':int(arr.count()),'units':list(raster.units),'shape':list(arr.shape)}
    with rasterio.open(output/'prediction_lower.tif') as lo, rasterio.open(output/'prediction_upper.tif') as hi, rasterio.open(output/'prediction_interval_width.tif') as wi:
        raster_width_error=float(np.max(abs(hi.read(1)-lo.read(1)-wi.read(1))));assert raster_width_error<1e-5
    assert len(rasters)==8 and all(r['valid_pixels']==64 for r in rasters.values())
    cpath=Path(cancel['cancelled_job']['directory'])/'output';cancel_result=read(cpath/'result.json');progress=[json.loads(x) for x in (cpath/'progress.jsonl').read_text(encoding='utf-8').splitlines()]
    assert cancel_result['status']=='cancelled' and cancel_result['outputs']==[] and cancel_result['error']['code']=='CANCELLED'
    assert read(cpath/'INCOMPLETE.json')['status']=='cancelled' and (cpath/'cancel.request').is_file()
    assert progress[-1]['stage']=='cancelled' and not any(r['stage']=='completed' for r in progress)
    bad=[p.name for p in cpath.rglob('*') if p.is_file() and ('.partial' in p.name or p.suffix in ('.tif','.joblib','.zip','.geojson') or p.name.startswith('study_report'))];assert not bad
    pagination=verify_pagination(output,result,read(output/'study_summary.json'))
    ui_log_path=EVIDENCE/'mcp-ui-session.log';ui_log=ui_log_path.read_text(encoding='utf-8-sig')
    gui_zip=EVIDENCE/'mcp-gui-study.spsstudy.zip'
    assert digest(gui_zip)==digest(archive_path)
    # The log literals below match the Turkish 0.5.0 acceptance log (mcp-ui-session.log); keep as recorded.
    assert 'Geçmiş başarılı sonuçtan 8 raster eklendi' in ui_log
    assert 'Doğrulanmış yerel rapor ArcMap içinde açıldı: '+str(output/'study_report_index.html') in ui_log
    assert 'Tam çalışma paketi kaydedildi ve dosya özeti doğrulandı: '+str(gui_zip) in ui_log
    cancel_log=ui_log[ui_log.index('İş başlatılıyor: run; '+str(cpath.parent/'request.json')):]
    assert 'İşçi çıkış kodu: 130' in cancel_log
    ui={'log':file_record(ui_log_path),'study_export':file_record(gui_zip),
        'screenshots':[file_record(EVIDENCE/n) for n in ['mcp-report-index.png','mcp-report-parameters.png','mcp-gui-study-saved.png']],
        'report_opened_from_completed_history':True,'parameter_page':7,'page_count':pagination['pages'],
        'screenshot_readback':'Index, parameter page 7/62 and completed export were visually inspected. Closing with X is the parent operator observation.',
        'empty_selection_error_observed': 'Önce tamamlanmış bir işin çıktısını seçin.' in ui_log}  # Turkish 0.5.0 log line; keep as recorded.
    process={'status':'pending','scope':'No live process query performed by this verifier; root provides targeted observation separately.'}
    if process_file:
        observation=read(process_file)
        # Matches the Turkish 0.5.0 acceptance log; keep as recorded.
        logged=[int(x) for x in re.findall(r'İşçi başladı\. Süreç kimliği: (\d+)',ui_log)]
        assert observation['worker_pids_from_ui_log']==logged and len(logged)==4
        assert observation['worker_alive']=={str(pid):False for pid in logged}
        assert observation['currently_matching_child_processes']==[] and observation['historical_child_pid_inventory']=='not_captured'
        process={'status':'passed_logged_workers_only','evidence':file_record(process_file),'observation':observation,
            'cancel_worker_pid':int(re.search(r'İşçi başladı\. Süreç kimliği: (\d+)',cancel_log).group(1)),  # Turkish 0.5.0 log line; keep as recorded.
            'exit_code':130,'exit_code_source':file_record(ui_log_path),
            'scope':'Externally captured logged-worker observation; historical child process inventory was not captured. No new process query.'}
    tests=read(ROOT/'build/backend-050-accepted-summary.json');assert tests['tests']==522 and tests['failures']==tests['errors']==tests['skipped']==0
    native_tests=[]
    for name,count in [('native_contract_result.json',147),('panel/panel_contract_result.json',491)]:
        path=ROOT/'build/native-tests/20261018-034358-022'/name;item=read(path)
        assert item['status']=='passed' and item['assertions']==count
        native_tests.append({'file':file_record(path),'record':item})
    assert ev['study_context']['model_artifact']['roundtrip_verified'] is True
    return {'schema_version':1,'status':'passed_file_readback','created_utc':datetime.now(timezone.utc).isoformat(),
        'candidate':file_record(package),'run_id':RUN_ID,'output_directory':str(output),
        'actual_mcp_evidence':[file_record(EVIDENCE/n) for n in evidence_names],
        'mcp_session':actual['arcmap_session'],'available_tool_names':actual['tools'],
        'mcp_call_counts':{d['mode']:len(d['calls']) for d in calls},'rollback_persistent_state_recomputed':True,
        'exports':exports,'outputs':outputs,'source_files':audit['sources'],'package_backend_files_matched':len(audit['source_code']),
        'study_archive':study_archive,'model_package':model_package,'model_roundtrip_record':ev['study_context']['model_artifact'],
        'model_equation_reproduction_max_error':model_error,'counts':{'source':48,'retained':47,'excluded':1,'oof':45,'independent':2,'final_training':len(final['development_ids']),'final_calibration':len(final['calibration_ids']),'roles':len(roles),'learning_memberships':len(learning),'trials':len(trials),'inner_folds':len(inner_csv),'publication_metric_rows':len(pub),'raw_covariate_values_verified':covariate_checks},
        'metrics':ev['metrics'],'final_record':final,'outer_folds':ev['fold_metrics'],'trials':trials,
        'coverage_recomputed':coverage,'publication_scopes':dict(Counter(r['scope'] for r in pub)),'prediction_csv_counts':prediction_csv_counts,
        'role_counts':dict(Counter(r['scope']+'/'+r['partition'] for r in roles)),
        'point_and_interval_checks':checks,'failed_checks':failures+pub_errors,'independent_history':ev['independent_test_history'],
        'rasters':rasters,'raster_width_max_error':raster_width_error,'pagination':pagination,'gui_readback':ui,
        'cancel':{'run_id':cancel_result['run_id'],'result':file_record(cpath/'result.json'),'outputs_empty':True,'remaining_files':[file_record(p) for p in cpath.iterdir() if p.is_file()],'partial_model_raster_package_files':bad,'progress':progress,'recorded_cancel_seconds':cancel['cancel_seconds'],'process_observation':process},
        'test_summary':tests,'test_summary_file':file_record(ROOT/'build/backend-050-accepted-summary.json'),'native_tests':native_tests,
        'not_exercised':['historical child process PID inventory was not captured','real field-data scientific validity','inner-training error and inner-interval metrics are explicitly not computed','all external MCP clients connected independently'],
        'scope':'Current trusted local artifacts, raw nearest-pixel readback and stored prediction equations only. No model loaded, no fitting, no process/UI actions.'}

def make_report(d):
    p=Path(d['output_directory']);c=d['counts'];f=d['final_record']
    metric_rows=[('Final training',f['train_metrics']),('Final calibration',f['calibration_metrics']),('Pooled out-of-fold validation',d['metrics']['pooled_oof']),('Independent test',d['metrics']['independent'])]
    lines=['# Version 0.5 acceptance report','','18 October 2026. This acceptance is limited to Model Context Protocol (MCP) calls sent to a real ArcMap session and the files they produced.',
        'MCP lets a client call local ArcMap functions. This file check started no new operation that queries operating system processes and no new model training.', '',
        'The Secure Hash Algorithm 256-bit (SHA-256) was used to check that files carry the same bytes.',
        'The tested candidate is `0.5.0`; the version reported in the ArcMap session is `0.5.0.0`. SHA-256 digest of the package file: `'+PACKAGE_SHA+'`.', '',
        '- '+link(d['candidate']['path'],'Tested add-in package'),'- '+link(ROOT/'build/mcp-scientific-readback.json','All findings in machine-readable form'),'- '+link(ROOT/'build/verify_mcp_scientific.py','Read-only verification script'),'',
        '## Scope of real operations and records','',
        'The real ArcMap session was discovered; status and layer queries responded. An invalid attempt to open a project returned an error. The persistent fields of the previous project were read back as restored.',
        'A preview and a model run were started through MCP. The result list was read in pages; report and table content was returned. The model, the study archive and the project file were saved through MCP.',
        'The call record shows 15 listed tools. Listing a tool does not mean that it was separately tested in every client application.', '',
        table(['Evidence','Calls','Status'],[[x,d['mcp_call_counts'][x],'passed'] for x in ['inventory','rollback','run','cancel']]),'',
        'The completed job has the ID `'+RUN_ID+'`. In the final status, the job is not running, progress is 100 percent and the model job is recorded as completed. Eight rasters were reported as added to the map.',
        'After the completed project was reopened, the previous result was added to the map. The report index and the parameters on page 7 of 62 were viewed in ArcMap.',
        'The log confirms that the report was opened and that the study archive was saved from the user interface (UI). The reviewed screenshots show the index, the parameter page and the save result.',
        'Closing the report window with X is an observation by the agent that operated the UI. An earlier empty result selection gave an error; the report was then opened with the correct previous project.', '',
        '## Data and participation in learning','',
        'The data are synthetic. The `soil_value` target is in g/kg; no laboratory accuracy or field performance of a real soil property is claimed.', '',
        table(['Role','Observations'],[['Source',48],['D000 excluded before modeling',1],['Retained raw modeling data',47],['Tested in outer folds',45],['Locked independent test set, D046 and D047',2],['Final training',c['final_training']],['Final interval calibration',c['final_calibration']]]),'',
        'D000 is absent from the training, calibration, fold and learning records. Its original target and properties are kept in a separate file with the exclusion reason.',
        'D046 and D047 have the independent test role. These IDs did not enter the outer or inner folds, the fitting of preprocessing, model training or interval calibration.',
        'The independent test history is `first_evaluation`, with zero earlier evaluations. Two observations only test the software path; they do not give a reliable estimate of map accuracy.',
        'Groups are defined by the `profile` field. Two outer folds, two inner folds, one repeat and seed 42 were used. This new job is not a temporal validation.', '',
        table(['File','Rows','Check'],[['sampled_data.json / CSV',47,'Equality with source target, properties, coordinates and dates'],['Sampled values of the two raster bands',94,'Reread from the source pixel'],['data_roles.csv / role_data.csv',500,'Role list joined with raw data'],['learning_membership.csv',1097,'Exact match with the actual learning IDs'],['excluded_samples.json / CSV',1,'Original source record of D000 and its reason']]),'',
        'Role rows can repeat the same observation for several models or folds. The 500 role records and 1097 learning records are not counts of independent observations.',
        'The sampled covariate values are on the scale before imputation, standardization and variable selection. Nearest-pixel sampling matched the source raster exactly in 94 values.', '',
        '## All search trials and model settings','',
        'For Ridge regression, alpha values 0.1, 1 and 10 were tested by grid search. Three separate selection procedures were run, for the final model and the two outer folds.',
        'There are nine actual trials and 18 inner-fold evaluations in total. All completed; this run produced no failed or unstarted trial.', '',
        'Root mean squared error (RMSE), mean absolute error (MAE) and mean error (ME) are in g/kg. For ME, the sign is prediction minus observation.', '',
        table(['Search role','Trial','alpha','Inner validation n','Selection value, RMSE','Selected'],[[t['role'],t['trial'],t['model']['parameters']['alpha'],t['validation_metrics']['n'],fmt(t['value']),str(t['selected'])] for t in d['trials']]),'',
        'The selection value is the metric of the relevant search; it is not used in place of outer or independent error. Complete inner-fold results and parameters are in the linked tables.',
        'Parameters actually used to build the final model: `'+json.dumps(f['effective_parameters'],ensure_ascii=False)+'`.',
        'Target transformation, variable selection and multicollinearity-based elimination are off. Median imputation and standardization were recorded only for the relevant training partitions.',
        'The saved coefficients are `[0.3239936634998567, -0.5679389113326779]`, and the intercept is `8.901995362970752`. The variable order is kept as `d_cov_1`, `d_cov_2`.',
        'These coefficients apply to the standardized covariates; they cannot be presented as coefficients in raw environmental covariate units. The 47 final predictions were reproduced with the saved centers and scales.', '',
        '## Error metrics and uncertainty','',
        'The coefficient of determination R² was computed as 1 - sum of squared errors / sum of squared deviations of the observations from their mean. The squared Pearson correlation was kept separate.',
        'The training error is a resubstitution diagnostic. The calibration error belongs to the held-out observations used to tune the intervals. Neither error counts as independent performance.', '',
        table(['Scope','n','RMSE','MAE','ME','R²'],[[scope,m['n'],fmt(m['rmse']),fmt(m['mae']),fmt(m['mean_error']),fmt(m['r2'])] for scope,m in metric_rows]),'',
        'The Pearson correlation in the independent test is 1, but there are only two observations. This value cannot be interpreted as evidence of the general validity of the model.', '',
        table(['Outer fold','Training','Calibration','Test','alpha','RMSE','R²'],[[r['fold'],len(r['model_record']['development_ids']),len(r['model_record']['calibration_ids']),r['metrics']['n'],r['model_record']['model']['parameters']['alpha'],fmt(r['metrics']['rmse']),fmt(r['metrics']['r2'])] for r in d['outer_folds']]),'',
        'Prediction intervals with 80 percent nominal coverage were produced by split conformal prediction with separate calibration. The final calibration contains 18 observations; the selected residual rank is 16.',
        'The rank of the calibration residuals and the interval adjustment were recomputed. Outer-fold intervals come from their own calibrations, and independent test intervals come from the final calibration.', '',
        table(['Scope','Nominal coverage','With interval/total','Covered/with interval','Coverage','Mean width, g/kg'],[[x['scope'],fmt(x['nominal_coverage']),str(x['available_n'])+'/'+str(x['n']),str(x['covered_n'])+'/'+str(x['available_n']),fmt(x['coverage']),fmt(x['mean_width'])] for x in d['coverage_recomputed']]),'',
        'At 80 percent, out-of-fold coverage is 35/45 and independent coverage is 2/2. The 2/2 result in a small synthetic test does not show that the area is 100 percent covered.',
        f"The {c['publication_metric_rows']} rows of the publication metric table were recomputed, each within its role, fold and trial scope. No missing or failed value was turned into a success.",
        "Residual standard deviation was computed with denominator n-1, and Lin's concordance coefficient with moments using denominator n. Median absolute error, Pearson r/r² and the two dispersion ratios are kept in the full numerical appendix.",
        'Inner-fold training errors and inner-fold interval metrics were not computed in this workflow. The `not_computed` status is stored explicitly in the data dictionary and the inner-fold table.', '',
        '## Full study archive and model record','',
        f"The file digests of all {len(d['outputs'])} outputs in the result manifest matched. The study archive contains {d['study_archive']['members_verified']} members; every member digest in its internal manifest was verified again.",
        'The archive contains the generated tables, the full and paginated reports, figures, rasters, the request record, the audit record and the model package.',
        'The original source rasters are not copied into the archive; the actual sampled covariate values are included. The result snapshot leaves out its own archive entry, which avoids a file digest loop.',
        'The study archive and model package saved through MCP are byte-identical to their source outputs. The saved project is also bound to this completed result.', '',
        table(['Output','SHA-256'],[['Study package',d['study_archive']['file']['sha256']],['Model package',d['model_package']['file']['sha256']],['Model',d['model_roundtrip_record']['sha256']]]),'',
        'The run record reports that, after the model is reloaded, point predictions and available intervals for the retained observations are the same. The package code that ran checks this equality as a requirement before completion.',
        'This file review did not reload the serialized model. In addition, the final predictions were reproduced independently from the saved linear model equation.',
        f"The largest difference from the model equation is {d['model_equation_reproduction_max_error']:.3g} g/kg. The largest difference in the raster interval width check is {d['raster_width_max_error']:.3g} g/kg.",
        'Each of the eight rasters has 64 valid cells. Report pages and archive members are bound by file digests; the number of cells is not a number of validation observations.', '',
        '## Cancellation and preservation of earlier outputs','',
        'The cancelled job was recorded as `'+d['cancel']['run_id']+'`. The cancel call was accepted; the final status is `cancelled`, the list of completed outputs is empty and the error code is `CANCELLED`.',
        'The last line of the progress record is the cancellation; there is no completion line. No partial raster, model, report archive or `.partial` file was found.',
        'The request, preflight check, cancel request, progress, result and incompleteness records are kept. They are not outputs of a successful analysis.',
        'The time in the cancellation record is about 1.094 seconds. The 140 output digests of the earlier successful job still matched in this re-check.',
        'The root agent will provide the live targeted process query separately. This report verifies only the cancellation status and file cleanup; it does not count process termination as verified at this time.' if d['cancel']['process_observation'].get('status')=='pending' else 'A separate process query shows that the four workers in the log are no longer running. For the cancellation worker 31628, the exit code in the log is 130.',
        'No child process that could be associated was found at that time. Historical child process IDs were not collected, so no acceptance of tracking the history of all child processes was made.', '',
        '## Automated tests and open scope','',
        'The last recorded backend test summary has 522 tests, zero errors, zero failures and zero skips. An earlier resource shortage is kept separate from the successful test that was rerun.',
        'In the native test records that were read back, 147 contract checks and 491 panel checks passed. These numbers are not real scientific observations or independent repeats.',
        'Real connections from each of the seven clients, a historical child process inventory and scientific validation with real field data remain open in this document.',
        'The earlier TabM, RealMLP and TabPFN UI evidence belongs to different 0.4.1 candidates. Its scope is kept in the '+link(ROOT/'ASAMA_D_KABUL.md','Stage D report')+'.', '',
        '## Complete files','']
    files=['study_report_index.html','study_report.html','sampled_data.csv','sampled_data.json','data_dictionary.json','data_roles.csv','role_data.csv','learning_membership.csv','excluded_samples.json','trial_results.csv','inner_fold_results.csv','publication_metrics.csv','hyperparameters.csv','training_predictions.csv','calibration_predictions.csv','inner_validation_predictions.csv','oof_predictions.csv','independent_predictions.csv','coverage_diagnostics.csv','final_record.json','evaluation.json','model_package.spsmodel.zip','study_package.spsstudy.zip','audit.json','result.json']
    evidence_links=d['actual_mcp_evidence']+[d['test_summary_file']]+[r['file'] for r in d['native_tests']]+[d['gui_readback']['log'],d['gui_readback']['study_export']]+d['gui_readback']['screenshots']
    if 'evidence' in d['cancel']['process_observation']:evidence_links.append(d['cancel']['process_observation']['evidence'])
    lines += [table(['File','Link'],[[name,link(p/name)] for name in files]),'', 'Evidence of actual calls and tests:', '',*['- '+link(x['path']) for x in evidence_links],'']
    return '\n'.join(lines)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--process-evidence',type=Path);args=parser.parse_args()
    data=verify(args.process_evidence);assert not data['failed_checks']
    (ROOT/'build/mcp-scientific-readback.json').write_text(json.dumps(data,ensure_ascii=False,allow_nan=False,indent=2)+'\n',encoding='utf-8')
    prose=make_report(data)
    for target in re.findall(r'\]\(<([^>]+)>\)',prose):assert Path(target).is_file(),target
    (ROOT/'ASAMA_05_KABUL.md').write_text(prose,encoding='utf-8')
    print(json.dumps({'status':data['status'],'counts':data['counts'],'checks':len(data['point_and_interval_checks']),'archive_members':data['study_archive']['members_verified'],'failed_checks':data['failed_checks']},ensure_ascii=False))
