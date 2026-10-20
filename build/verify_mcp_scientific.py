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
    assert 'Geçmiş başarılı sonuçtan 8 raster eklendi' in ui_log
    assert 'Doğrulanmış yerel rapor ArcMap içinde açıldı: '+str(output/'study_report_index.html') in ui_log
    assert 'Tam çalışma paketi kaydedildi ve dosya özeti doğrulandı: '+str(gui_zip) in ui_log
    cancel_log=ui_log[ui_log.index('İş başlatılıyor: run; '+str(cpath.parent/'request.json')):]
    assert 'İşçi çıkış kodu: 130' in cancel_log
    ui={'log':file_record(ui_log_path),'study_export':file_record(gui_zip),
        'screenshots':[file_record(EVIDENCE/n) for n in ['mcp-report-index.png','mcp-report-parameters.png','mcp-gui-study-saved.png']],
        'report_opened_from_completed_history':True,'parameter_page':7,'page_count':pagination['pages'],
        'screenshot_readback':'Index, parameter page 7/62 and completed export were visually inspected. Closing with X is the parent operator observation.',
        'empty_selection_error_observed': 'Önce tamamlanmış bir işin çıktısını seçin.' in ui_log}
    process={'status':'pending','scope':'No live process query performed by this verifier; root provides targeted observation separately.'}
    if process_file:
        observation=read(process_file)
        logged=[int(x) for x in re.findall(r'İşçi başladı\. Süreç kimliği: (\d+)',ui_log)]
        assert observation['worker_pids_from_ui_log']==logged and len(logged)==4
        assert observation['worker_alive']=={str(pid):False for pid in logged}
        assert observation['currently_matching_child_processes']==[] and observation['historical_child_pid_inventory']=='not_captured'
        process={'status':'passed_logged_workers_only','evidence':file_record(process_file),'observation':observation,
            'cancel_worker_pid':int(re.search(r'İşçi başladı\. Süreç kimliği: (\d+)',cancel_log).group(1)),
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
    metric_rows=[('Nihai eğitim',f['train_metrics']),('Nihai kalibrasyon',f['calibration_metrics']),('Birleşik dış doğrulama',d['metrics']['pooled_oof']),('Bağımsız test',d['metrics']['independent'])]
    lines=['# Sürüm 0.5 kabul raporu','','18 Ekim 2026. Bu kabul gerçek ArcMap oturumuna gönderilmiş Model Context Protocol (MCP) çağrıları ve oluşan dosyalarla sınırlıdır.',
        'MCP, istemcinin yerel ArcMap işlevlerini çağırmasını sağlar. İşletim sistemi süreçlerini sorgulayan yeni bir işlem veya yeni model eğitimi bu dosya denetiminde başlatılmadı.', '',
        'Güvenli özet algoritması 256 bit (SHA-256), dosyaların aynı baytları taşıdığını denetlemek için kullanılmıştır.',
        'Sınanan aday `0.5.0`, ArcMap oturumunda bildirilen sürüm `0.5.0.0` değerindedir. Paket dosyasının SHA-256 özeti: `'+PACKAGE_SHA+'`.', '',
        '- '+link(d['candidate']['path'],'Sınanan eklenti paketi'),'- '+link(ROOT/'build/mcp-scientific-readback.json','Makine okunur bütün bulgular'),'- '+link(ROOT/'build/verify_mcp_scientific.py','Salt okunur denetim betiği'),'',
        '## Gerçek işlem ve kayıt kapsamı','',
        'Gerçek ArcMap oturumu keşfedildi; durum ve katman sorguları yanıt verdi. Hatalı proje açma girişimi hata döndürdü. Önceki projenin kalıcı alanları geri yüklenmiş olarak yeniden okundu.',
        'MCP ile ön gösterim ve model çalışması başlatıldı. Sonuç listesi sayfalar halinde okundu; rapor ve tablo içeriği döndürüldü. Model, çalışma arşivi ve proje dosyası MCP üzerinden kaydedildi.',
        'Çağrı kaydında 15 aracın listelendiği görülür. Bir aracın listelenmesi, her istemci uygulamasında ayrıca sınandığı anlamına gelmez.', '',
        table(['Kanıt','Çağrı sayısı','Durum'],[[x,d['mcp_call_counts'][x],'passed'] for x in ['inventory','rollback','run','cancel']]),'',
        'Tamamlanan iş `'+RUN_ID+'` kimliğini taşır. Son durumda iş sürmüyor, ilerleme yüzde 100 ve model işi tamamlandı olarak kaydedilmiştir. Haritaya sekiz raster eklendiği bildirilmiştir.',
        'Tamamlanmış proje yeniden açıldıktan sonra geçmiş sonuç haritaya eklendi. Rapor dizini ve 62 sayfanın yedincisindeki parametreler ArcMap içinde görüntülendi.',
        'Günlük raporun açılmasını ve çalışma arşivinin arayüzden kaydedilmesini doğrular. İncelenen ekran görüntüleri dizini, parametre sayfasını ve kaydetme sonucunu gösterir.',
        'Rapor penceresinin X ile kapatılması arayüzü kullanan ajanın gözlemidir. Önceki boş sonuç seçimi hata vermiştir; ardından doğru geçmiş projeyle rapor açılmıştır.', '',
        '## Veri ve öğrenmeye katılım','',
        'Veri sentetiktir. `soil_value` hedefi g/kg birimindedir; gerçek bir toprak özelliğinin laboratuvar doğruluğu veya alan başarısı ileri sürülmez.', '',
        table(['Rol','Gözlem sayısı'],[['Kaynak',48],['Modelleme öncesinde dışlanan D000',1],['Tutulan ham model verisi',47],['Dış katta sınanan',45],['Kilitli bağımsız test D046 ve D047',2],['Nihai eğitim',c['final_training']],['Nihai aralık kalibrasyonu',c['final_calibration']]]),'',
        'D000, eğitim/kalibrasyon/kat/öğrenme kayıtlarında bulunmaz. Özgün hedefi ve özellikleri, dışlama gerekçesiyle ayrı dosyada korunur.',
        'D046 ve D047 bağımsız test rolündedir. Bu kimlikler dış veya iç katlara, ön işleme öğrenimine, model eğitimine ve aralık kalibrasyonuna girmemiştir.',
        'Bağımsız test geçmişi `first_evaluation`, önceki değerlendirme sayısı sıfırdır. İki gözlem yalnız yazılım yolunu sınar; güvenilir alan doğruluğu kestirimi sağlamaz.',
        'Gruplar `profile` alanıyla tanımlanmıştır. İki dış kat, iki iç kat, tek tekrar ve başlangıç değeri 42 kullanılmıştır. Bu yeni iş zamansal doğrulama değildir.', '',
        table(['Dosya','Satır','Denetim'],[['sampled_data.json / CSV',47,'Kaynak hedef/özellik/koordinat/tarih eşitliği'],['İki raster bandının örneklenmiş değerleri',94,'Kaynak pikselinden yeniden okuma'],['data_roles.csv / role_data.csv',500,'Rol listesi ve ham veri birleşimi'],['learning_membership.csv',1097,'Gerçek öğrenme kimlikleriyle birebir eşitlik'],['excluded_samples.json / CSV',1,'D000 özgün kaynak kaydı ve gerekçesi']]),'',
        'Rol satırları aynı gözlemi birden çok model/kat için tekrar gösterebilir. 500 rol veya 1097 öğrenme kaydı bağımsız gözlem sayısı değildir.',
        'Örneklenmiş açıklayıcı değerler tamamlama, standartlaştırma ve değişken seçiminden önceki ölçektedir. En yakın piksel örneklemesi 94 değerde kaynak rasterle birebir eşleşti.', '',
        '## Bütün arama denemeleri ve model ayarları','',
        'Ridge regresyonunda alpha için 0.1, 1 ve 10 değerleri ızgara aramasıyla sınandı. Üç ayrı seçim işlemi, nihai model ve iki dış kat için yürütüldü.',
        'Toplam dokuz gerçek deneme ve 18 iç kat değerlendirmesi vardır. Hepsi tamamlandı; başarısız veya başlanmamış deneme bu koşuda üretilmedi.', '',
        'Ortalama kare hatanın karekökü (RMSE), ortalama mutlak hata (MAE) ve ortalama hata (ME) g/kg birimindedir. ME için işaret tahmin eksi gözlemdir.', '',
        table(['Arama rolü','Deneme','alpha','İç doğrulama n','Seçim değeri, RMSE','Seçildi'],[[t['role'],t['trial'],t['model']['parameters']['alpha'],t['validation_metrics']['n'],fmt(t['value']),str(t['selected'])] for t in d['trials']]),'',
        'Seçim değeri ilgili aramanın ölçütüdür; dış veya bağımsız hata yerine kullanılmaz. Tam iç kat sonuçları ve parametreler bağlantılı tablolardadır.',
        'Nihai modelin fiilen kurulmuş parametreleri: `'+json.dumps(f['effective_parameters'],ensure_ascii=False)+'`.',
        'Hedef dönüşümü, değişken seçimi ve çoklu doğrusal bağlantıya göre eleme kapalıdır. Medyan ile tamamlama ve standartlaştırma yalnız ilgili eğitim bölümlerinde kaydedilmiştir.',
        'Kaydedilmiş katsayılar `[0.3239936634998567, -0.5679389113326779]`, sabit terim `8.901995362970752` değerindedir. Değişken sırası `d_cov_1`, `d_cov_2` olarak korunmuştur.',
        'Bu katsayılar standartlaştırılmış açıklayıcılara aittir; ham çevresel değişken birimindeki katsayılar diye sunulamaz. Kaydedilmiş merkez ve ölçeklerle 47 nihai tahmin yeniden üretildi.', '',
        '## Hata ölçütleri ve belirsizlik','',
        'Belirleme katsayısı R², 1 - kare hata toplamı / gözlemlerin ortalamadan kare sapma toplamı ile hesaplandı. Pearson korelasyonunun karesi ayrı tutuldu.',
        'Eğitim hatası yeniden uygulama tanısıdır. Kalibrasyon hatası aralık ayarında kullanılan ayrılmış gözlemlere aittir. Bu iki hata bağımsız başarı sayılmaz.', '',
        table(['Kapsam','n','RMSE','MAE','ME','R²'],[[scope,m['n'],fmt(m['rmse']),fmt(m['mae']),fmt(m['mean_error']),fmt(m['r2'])] for scope,m in metric_rows]),'',
        'Bağımsız testte Pearson korelasyonu 1 olsa da yalnız iki gözlem vardır. Bu değer modelin genel geçerliliğine kanıt olarak yorumlanamaz.', '',
        table(['Dış kat','Eğitim','Kalibrasyon','Test','alpha','RMSE','R²'],[[r['fold'],len(r['model_record']['development_ids']),len(r['model_record']['calibration_ids']),r['metrics']['n'],r['model_record']['model']['parameters']['alpha'],fmt(r['metrics']['rmse']),fmt(r['metrics']['r2'])] for r in d['outer_folds']]),'',
        'Ayrı kalibrasyona dayalı split conformal yöntemiyle nominal yüzde 80 tahmin aralığı üretildi. Nihai kalibrasyon 18 gözlem içerir; seçilen artık sırası 16’dır.',
        'Kalibrasyon artıklarının sırası ve aralık düzeltmesi yeniden hesaplandı. Dış kat aralıkları kendi kalibrasyonlarından, bağımsız test aralıkları nihai kalibrasyondan elde edilir.', '',
        table(['Kapsam','Nominal oran','Aralık var/toplam','Kapsanan/aralık var','Kapsama','Ortalama genişlik, g/kg'],[[x['scope'],fmt(x['nominal_coverage']),str(x['available_n'])+'/'+str(x['n']),str(x['covered_n'])+'/'+str(x['available_n']),fmt(x['coverage']),fmt(x['mean_width'])] for x in d['coverage_recomputed']]),'',
        'Yüzde 80 dış kapsama 35/45, bağımsız kapsama 2/2’dir. Küçük sentetik testteki 2/2 sonucu alanın yüzde 100 kapsandığını göstermez.',
        f"Yayıma yönelik ölçüt tablosunun {c['publication_metric_rows']} satırı, her satırın rol/kat/deneme kapsamıyla yeniden hesaplandı. Eksik veya başarısız değer başarıya dönüştürülmedi.",
        'Artık standart sapması n-1 paydasıyla, Lin uyum katsayısı n paydalı momentlerle hesaplandı. Ortanca mutlak hata, Pearson r/r² ve iki dağılım oranı tam sayısal ekte korunur.',
        'İç kat eğitim hataları ve iç kat aralık ölçütleri bu iş akışında hesaplanmadı. `not_computed` durumu veri sözlüğünde ve iç kat tablosunda açıkça saklandı.', '',
        '## Tam çalışma arşivi ve model kaydı','',
        f"Sonuç bildirimindeki {len(d['outputs'])} ürünün tamamının dosya özeti eşleşti. Çalışma arşivinde {d['study_archive']['members_verified']} üye bulundu; iç bildirimin her üye özeti yeniden doğrulandı.",
        'Arşiv, üretilen tabloları, tam ve sayfalanmış raporları, şekilleri, rasterleri, istek kaydını, denetim kaydını ve model paketini içerir.',
        'Özgün kaynak rasterler arşive kopyalanmaz; gerçek örneklenmiş açıklayıcı değerler dahildir. Sonuç anlık kaydı, kendi arşiv girdisini dışarıda tutarak dosya özeti döngüsünü önler.',
        'MCP ile kaydedilen çalışma arşivi ve model paketi kaynak ürünleriyle bayt düzeyinde aynıdır. Kaydedilen proje de bu tamamlanan sonuca bağlıdır.', '',
        table(['Ürün','SHA-256'],[['Çalışma paketi',d['study_archive']['file']['sha256']],['Model paketi',d['model_package']['file']['sha256']],['Model',d['model_roundtrip_record']['sha256']]]),'',
        'Çalışma kaydı, model yeniden yüklenince tutulan gözlemlerde nokta tahminleri ve kullanılabilir aralıkların aynı olduğunu bildirir. Çalışan paket kodu bu eşitliği tamamlanma öncesinde zorunlu denetler.',
        'Bu dosya incelemesinde serileştirilmiş model yeniden yüklenmedi. Buna ek olarak kaydedilen doğrusal model denklemiyle nihai tahminler bağımsız olarak yeniden üretildi.',
        f"Model denklemiyle en büyük fark {d['model_equation_reproduction_max_error']:.3g} g/kg’dır. Raster aralık genişliği denetimindeki en büyük fark {d['raster_width_max_error']:.3g} g/kg’dır.",
        'Sekiz rasterin her birinde 64 geçerli hücre vardır. Rapor sayfaları ve arşiv üyeleri dosya özetiyle bağlıdır; hücre sayısı doğrulama gözlemi sayısı değildir.', '',
        '## İptal ve önceki ürünlerin korunması','',
        'İptal edilen iş `'+d['cancel']['run_id']+'` olarak kaydedildi. İptal çağrısı kabul edildi; son durum `cancelled`, tamamlanmış ürün listesi boş ve hata kodu `CANCELLED` değerindedir.',
        'İlerleme kaydının son satırı iptaldir; tamamlanma satırı yoktur. Yarım raster, model, rapor arşivi veya `.partial` dosyası bulunmadı.',
        'İstek, ön kontrol, iptal isteği, ilerleme, sonuç ve tamamlanmamışlık kaydı korunmuştur. Bunlar başarılı analiz ürünü değildir.',
        'İptal kaydındaki süre yaklaşık 1.094 saniyedir. Önceki başarılı işin 140 ürün özeti bu yeniden denetimde hâlâ eşleşmiştir.',
        'Canlı hedef süreç sorgusu kök ajan tarafından ayrıca sağlanacaktır. Bu rapor yalnız iptal durumu ve dosya temizliğini doğrular; süreçlerin kapanmasını şu anda doğrulanmış saymaz.' if d['cancel']['process_observation'].get('status')=='pending' else 'Ayrı süreç sorgusu, günlükteki dört işçinin artık çalışmadığını gösterir. İptal işçisi 31628 için günlük çıkış kodu 130’dur.',
        'O anda ilişkilendirilebilen alt süreç bulunmadı. Tarihsel alt süreç kimlikleri toplanmadığından bütün alt süreçlerin geçmişini izleme kabulü yapılmadı.', '',
        '## Otomatik sınamalar ve açık kalan kapsam','',
        'Kaydedilmiş son arka uç sınama özetinde 522 sınama, sıfır hata, sıfır başarısızlık ve sıfır atlama vardır. Önceki kaynak yetersizliği yeniden koşulan başarılı sınamadan ayrı tutulmuştur.',
        'Yeniden okunan yerel sınama kayıtlarında 147 sözleşme ve 491 panel denetimi geçti. Bu sayılar gerçek bilimsel gözlem veya bağımsız tekrar değildir.',
        'Yedi istemcinin her birinden gerçek bağlantı, tarihsel alt süreç envanteri ve gerçek saha verisiyle bilimsel doğrulama bu belgenin açık kapsamıdır.',
        'Eski TabM, RealMLP ve TabPFN arayüz kanıtları farklı 0.4.1 adaylarına aittir. Bunların kapsamı '+link(ROOT/'ASAMA_D_KABUL.md','D raporunda')+' korunur.', '',
        '## Tam dosyalar','']
    files=['study_report_index.html','study_report.html','sampled_data.csv','sampled_data.json','data_dictionary.json','data_roles.csv','role_data.csv','learning_membership.csv','excluded_samples.json','trial_results.csv','inner_fold_results.csv','publication_metrics.csv','hyperparameters.csv','training_predictions.csv','calibration_predictions.csv','inner_validation_predictions.csv','oof_predictions.csv','independent_predictions.csv','coverage_diagnostics.csv','final_record.json','evaluation.json','model_package.spsmodel.zip','study_package.spsstudy.zip','audit.json','result.json']
    evidence_links=d['actual_mcp_evidence']+[d['test_summary_file']]+[r['file'] for r in d['native_tests']]+[d['gui_readback']['log'],d['gui_readback']['study_export']]+d['gui_readback']['screenshots']
    if 'evidence' in d['cancel']['process_observation']:evidence_links.append(d['cancel']['process_observation']['evidence'])
    lines += [table(['Dosya','Bağlantı'],[[name,link(p/name)] for name in files]),'', 'Gerçek çağrı ve sınama kanıtları:', '',*['- '+link(x['path']) for x in evidence_links],'']
    return '\n'.join(lines)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--process-evidence',type=Path);args=parser.parse_args()
    data=verify(args.process_evidence);assert not data['failed_checks']
    (ROOT/'build/mcp-scientific-readback.json').write_text(json.dumps(data,ensure_ascii=False,allow_nan=False,indent=2)+'\n',encoding='utf-8')
    prose=make_report(data)
    for target in re.findall(r'\]\(<([^>]+)>\)',prose):assert Path(target).is_file(),target
    (ROOT/'ASAMA_05_KABUL.md').write_text(prose,encoding='utf-8')
    print(json.dumps({'status':data['status'],'counts':data['counts'],'checks':len(data['point_and_interval_checks']),'archive_members':data['study_archive']['members_verified'],'failed_checks':data['failed_checks']},ensure_ascii=False))
