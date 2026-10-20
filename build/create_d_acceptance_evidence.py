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
    if value is None:return 'Üretilmedi'
    if isinstance(value,int):return str(value)
    return format(value,'.8g')

def link(path,label=None):
    return f'[{label or Path(path).name}](<{Path(path).resolve().as_posix()}>)'

def table(headers,rows):
    return '\n'.join(['| '+' | '.join(headers)+' |','| '+' | '.join(['---']*len(headers))+' |']+['| '+' | '.join(map(str,row))+' |' for row in rows])

def report(data):
    lines=['# Aşama D kabul raporu','', '18 Ekim 2026. İncelenen gerçek ArcMap işleri 17 Ekim 2026 tarihinde çalıştırıldı.', '',
        'Üç deneysel modelin ArcMap içinden çalışması, kayıtlı 0.4.1 adayı için doğrulandı. Daha sonraki 0.5 sürümünün Model Context Protocol (MCP) kabulü ayrı adayla yapıldı.',
        'Yeni çalışmanın kanıtı ve açık sınırları: '+link(ROOT/'ASAMA_05_KABUL.md','0.5 kabul raporu')+'.', '',
        'Bu belge sentetik yazılım kabulünü raporlar. Hedef `soil_value`, birimi g/kg olan sentetik bir değişkendir. Sonuçlar gerçek toprak özelliğinin doğruluğu veya pedolojik geçerlilik kanıtı değildir.', '',
        'Buradaki yeniden denetim yalnız mevcut dosyaları okudu. Yeni eğitim, arayüz işlemi veya canlı süreç sınaması başlatılmadı. Sayılar kayıtlı tahminlerden bağımsız formüllerle yeniden hesaplandı.', '',
        '- '+link(ROOT/'build/d-final-scientific-evidence.json','Sayısal kayıtlar ve denetim'),
        '- '+link(ROOT/'build/create_d_acceptance_evidence.py','Yeniden okuma betiği'), '',
        '## Adaylar ve kanıt sınırı','',
        'Güvenli özet algoritması 256 bit (SHA-256) dosyanın baytlarını karşılaştırmak için kullanıldı. Özet eşitliği farklı yazılım adaylarının davranış bakımından eşdeğer olduğunu göstermez.', '',
        table(['Kapsam','Sürüm','Paket SHA-256','Durum'],[
            ['TabM, RealMLP, TabPFN ve iptal/yeniden açma',data['ui_tested_packages']['three_model_runs']['version'],data['ui_tested_packages']['three_model_runs']['sha256'],'Gerçek arayüz çalışmaları mevcut'],
            ['Ek bilgi korunması ve Ridge',data['ui_tested_packages']['metadata_ridge']['version'],data['ui_tested_packages']['metadata_ridge']['sha256'],'Ayrı gerçek arayüz adayı'],
            ['Yeni 0.5 Model Context Protocol (MCP) bağlantısı','0.5','Ayrı rapordaki 1c247131... adayı','Yeni kabul raporuna bakınız']]),'',
        'MCP, istemcinin yerel ArcMap işlemlerini çağırmasını sağlayan protokoldür. Eski model sonuçları bu yeni bağlantının doğrulandığı anlamına gelmez.', '',
        'Özgün denetimler:', '',
        *['- '+link(EVIDENCE/name) for name in ['artifact-readback-D.json','cancel-roundtrip-readback-D.json','metadata-readback-D.json']], '',
        '## Veri ve doğrulama tasarımı','',
        'Üç çalışma aynı 48 sentetik gözlemi ve iki raster bandını kullandı. `profile` alanında 48 ayrı grup, `observation_date` alanında 24 tarih dönemi vardır.',
        'Gözlemler nokta desteğindedir. Koordinat referansı EPSG:32637, uzaklık birimi metredir. Derinlik aralığı ve laboratuvar yöntemi kaydedilmemiştir.',
        'İki açıklayıcı değişkenin eksik oranı sıfırdır. Medyan ile tamamlama ve standartlaştırma seçilmiştir. Hedef dönüşümü, değişken seçimi ve çoklu doğrusal bağlantıya göre eleme kapalıdır.', '',
        'Dış kat tahminleri (out-of-fold, OOF), her gözlem için o gözlemi eğitimde kullanmayan dış kat modelinin tahminidir. Tek tekrar vardır; tekrarlar arasında kararlılık sınanmamıştır.', '',
        table(['Model','İş kimliği','Dış/iç kat','Dışta sınanan','Yalnız ilk geçmiş','Son eğitim/kalibrasyon','Bağımsız test'],[
            [r['model'],r['run_id'],'2 / 2',r['counts']['oof_unique_samples'],r['preflight'].get('initial_training_count',0),f"{len(r['final_record']['development_ids'])} / {len(r['final_record']['calibration_ids'])}",0] for r in data['runs']]),'',
        'TabM ve RealMLP, yeni grupların sonraki tarihlerde sınandığı zamansal bölmeyi kullandı. İlk eğitim geçmişi 12 dönemdir. İç katlarda başlangıç geçmişi dört dönemdir.',
        'Takvim boşluğu ve mekânsal tampon sıfırdır. İlk 24 gözlem dış hata hesabına girmez. Bu gözlemler sonraki dönemlerin eğitim geçmişinde kullanılabilir.',
        'TabPFN iki katlı grup bölmesi kullandı ve 48 gözlemin tamamı dış katta sınandı. Bu tasarım zamansal aktarım doğrulaması değildir.',
        'Kayıtlı eğitim, kalibrasyon ve test kimlikleri ile grup ayrımları yeniden denetlendi. Zamansal çalışmalarda dış ve iç katların eğitim tarihleri test tarihlerinden öncedir.', '',
        'TabM ve RealMLP için tarih aralıkları aynıdır:', '',
        table(['Bölüm','Eğitim tarihleri','Kalibrasyon tarihleri','Dış test tarihleri'],[
            ['Dış kat '+str(f['fold']),f['model_record']['temporal']['development_dates']['start']+' / '+f['model_record']['temporal']['development_dates']['end'],
             f['model_record']['temporal']['calibration_dates']['start']+' / '+f['model_record']['temporal']['calibration_dates']['end'],
             data['runs'][0]['outer_folds'][i]['diagnostics']['temporal']['test_start']+' / '+data['runs'][0]['outer_folds'][i]['diagnostics']['temporal']['test_end']]
            for i,f in enumerate(data['runs'][0]['outer_fold_records'])]+[
            ['Nihai model','2020-01-01 / 2020-02-09','2020-02-12 / 2020-03-10','Bağımsız test yok']]), '',
        'Girdi dosyaları:', '',
        '- '+link(ROOT/'examples-d/synthetic-dated-samples.csv','48 sentetik gözlem'),
        '- '+link(ROOT/'examples-d/synthetic-covariates.tif','İki bantlı sentetik raster'), '',
        '## Gerçek model ayarları','',
        'Hiperparametreler, eğitim başlamadan seçilen model ayarlarıdır. Bu üç çalışmada elle belirlenmiş tek aday kullanıldı; ızgara araması yapılmadı.',
        '`optimization.trials=10` kayıtlı üst ayardır. Gerçek ön kontrol bütçesi her arama için birdir. Her çalışmada üç arama kaydı ve altı iç kat değerlendirmesi bulunur.',
        'Üç arama, nihai model ile iki dış katın ayrı seçim işlemleridir. Bunlar üç farklı model ayarı anlamına gelmez. Bütün gerçek denemeler tamamlandı; başarısız deneme yoktur.', '',
        table(['Model','İstenen parametreler','Fiilen kurulan sarmalayıcı parametreleri'],[[r['model'],'`'+json.dumps(r['final_record']['model']['parameters'],ensure_ascii=False)+'`','`'+json.dumps(r['final_record']['effective_parameters'],ensure_ascii=False)+'`'] for r in data['runs']]),'',
        'Her üç modelde işlemci kullanılmıştır, iş parçacığı sayısı bir ve rastgele başlangıç değeri 42’dir. Bu işler grafik işlemci kabulü sayılmaz.',
        'TabM ve RealMLP iki eğitim dönemi tamamladı. Üreticinin rastgele doğrulama bölmesi ve erken durdurması kullanılmadı. Bu kısa eğitimler yazılım kabulü içindir.',
        'TabPFN kayıtlarında gradyan güncellemesi sıfırdır. Önceden eğitilmiş ağırlıklar sabittir; yalnız ilgili eğitim bölümünün verisi model bağlamına verilmiştir.', '',
        table(['Model','Üretici sürümü','PyTorch','Son eğitim tepe belleği, bayt','Son işlem süresi, saniye'],[[r['model'],r['final_record']['experimental_model_diagnostics']['versions'].get('tabm' if r['model']=='tabm' else 'pytabkit' if r['model']=='realmlp' else 'tabpfn'),r['final_record']['experimental_model_diagnostics']['versions']['torch'],r['final_record']['experimental_model_diagnostics']['peak_rss_bytes'],fmt(r['final_record']['experimental_model_diagnostics']['duration_seconds'])] for r in data['runs']]),'',
        'Bellek ve süre nihai model işlemine aittir; bütün işin toplamı değildir. Ölçülen performans bu küçük veri ve işlemci koşuluyla sınırlıdır.',
        'Üreticilerin bütün kurucu parametreleri, RealMLP yapılandırması, TabM optimizasyon varsayılanları ve TabPFN çıkarım ayarları, model kayıtları ile hiperparametre tablolarında korunur.', '']
    weight=data['runs'][2]['final_record']['experimental_model_diagnostics']['weight']
    lines += [f"TabPFN ağırlığı `{weight['filename']}`, {weight['bytes']} bayttır. Revizyon `{weight['revision']}`; SHA-256 `{weight['sha256']}`.",
        f"Kayıtlı lisans `{weight['license']}`, gerekli atıf metni `{weight['required_attribution']}`. Bu bilgi çalışma kaydından alınmıştır; yeni lisans kabulü yapılmamıştır.", '',
        '## Hata ölçütlerinin kapsamı','',
        'Ortalama kare hatanın karekökü (RMSE), ortalama mutlak hata (MAE) ve ortalama hata (ME) özgün hedef birimi g/kg ile verilir. ME için işaret tahmin eksi gözlemdir.',
        'Belirleme katsayısı R² = 1 - kare hata toplamı / gözlemlerin ortalamadan kare sapma toplamıdır. Negatif değerler korunmuştur. Pearson korelasyonunun karesi bu ölçütle aynı değildir.',
        'Nihai eğitim hatası, modelin gördüğü eğitim gözlemlerinde hesaplanmıştır. Kalibrasyon hatası, aralık kurulurken kullanılan ayrılmış gözlemlerdeki tanıdır. İkisi de bağımsız başarı değildir.', '',
        table(['Model','Kapsam','n','RMSE','MAE','ME','R²'],[[r['model'],scope,m['n'],fmt(m['rmse']),fmt(m['mae']),fmt(m['mean_error']),fmt(m['r2'])] for r in data['runs'] for scope,m in [('Nihai eğitim',r['final_record']['train_metrics']),('Nihai kalibrasyon',r['final_record']['calibration_metrics']),('Birleşik dış tahmin',r['metrics']['pooled_oof'])]]), '',
        'TabM’nin birleşik dış R² değeri yaklaşık -0.00000185’tir; sıfıra yuvarlanıp olumlu başarı gibi sunulmamıştır. İlk dış katta TabM ve RealMLP için de R² negatiftir.', '',
        table(['Model','Dış kat','Eğitim','Kalibrasyon','Test','RMSE','MAE','R²'],[[r['model'],f['fold'],len(f['model_record']['development_ids']),len(f['model_record']['calibration_ids']),f['metrics']['n'],fmt(f['metrics']['rmse']),fmt(f['metrics']['mae']),fmt(f['metrics']['r2'])] for r in data['runs'] for f in r['outer_fold_records']]),'',
        'Dış kat modellerinin eğitim ve kalibrasyon tanıları da ayrı kaydedildi:', '',
        table(['Model','Dış kat','Kapsam','n','RMSE','MAE','R²'],[[r['model'],f['fold'],scope,m['n'],fmt(m['rmse']),fmt(m['mae']),fmt(m['r2'])] for r in data['runs'] for f in r['outer_fold_records'] for scope,m in [('Eğitim',f['model_record']['train_metrics']),('Kalibrasyon',f['model_record']['calibration_metrics'])]]), '',
        'Kat numaraları özgün kayıtta olduğu gibi sıfırdan başlar. Katların ortalama RMSE değeri ile bütün dış tahminlerden hesaplanan RMSE ayrı büyüklüklerdir.', '',
        table(['Model','Kat RMSE ortalaması','Birleşik dış RMSE','Kat R² ortalaması','Birleşik dış R²'],[[r['model'],fmt(r['metrics']['fold_mean']['rmse']),fmt(r['metrics']['pooled_oof']['rmse']),fmt(r['metrics']['fold_mean']['r2']),fmt(r['metrics']['pooled_oof']['r2'])] for r in data['runs']]),'',
        'Bağımsız test kimliği ve bağımsız tahmin satırı üç çalışmada da yoktur. `independent=null` ve `not_supplied` korunmuştur. Bağımsız hata, kapsama veya dış bölge başarısı üretilemez.', '',
        '## İç doğrulama kayıtları','',
        'Aşağıdaki hata, ilgili aramanın iç kat tahminlerinden hesaplanmıştır. Ayar seçimi için kullanılır; dış doğrulama hatasının yerine geçmez.', '',
        table(['Model','Arama rolü','Deneme','İç tahmin n','RMSE','MAE','R²','Durum'],[[r['model'],t['role'],t['trial'],t['validation_metrics']['n'],fmt(t['validation_metrics']['rmse']),fmt(t['validation_metrics']['mae']),fmt(t['validation_metrics']['r2']),t['validation_status']] for r in data['runs'] for t in r['trials']]),'',
        table(['Model','Arama rolü','İç kat','Eğitim/test n','RMSE','MAE','R²'],[[r['model'],t['role'],f['fold'],str(f['training_sample_count'])+' / '+str(f['test_sample_count']),fmt(f['metrics']['rmse']),fmt(f['metrics']['mae']),fmt(f['metrics']['r2'])] for r in data['runs'] for t in r['trials'] for f in t['inner_fold_metrics']]), '',
        'Her iç katın eğitim/test sayısı, grup sayısı, seçilen değişkenleri ve gerçek model ayarları `evaluation.json` içinde bulunur. İç kat eğitim tahminleri bu eski işlerde hesaplanmamıştır.',
        'Bu nedenle iç eğitim hatası sonradan uydurulmamıştır. İç test ölçütleri, dış eğitim/kalibrasyon ölçütleri ve nihai eğitim/kalibrasyon ölçütleri ayrı kaynaklarda mevcuttur.', '',
        '## Ek doğruluk ölçütleri','',
        'Pearson r doğrusal ilişkiyi, Lin uyum korelasyon katsayısı (CCC) gözlem ve tahminin uyumunu özetler. CCC hesabında n paydalı varyans ve kovaryans kullanılmıştır.',
        'Gözlem standart sapmasının RMSE’ye oranı (RPD) ve gözlem çeyrekler arası açıklığının RMSE’ye oranı (RPIQ) ayrıca kaydedilmiştir. Bu oranlara evrensel başarı sınıfı atanmamıştır.',
        'Artık ve gözlem standart sapmalarında n-1 paydası kullanılmıştır. Çeyrekler doğrusal enterpolasyonla hesaplanmıştır. Tanımsız ölçütler gerekçeleriyle boş bırakılır; bu birleşik dış ölçütlerde tanımsızlık yoktur.', '',
        table(['Model','Ortalama kare hata','Mutlak hata ortancası','Artık standart sapması','Pearson r','Pearson r²','CCC','RPD','RPIQ'],[[r['model']]+[fmt(r['metrics']['pooled_oof'][k]) for k in ['mse','median_absolute_error','residual_std','pearson_r','pearson_r2','lin_ccc','rpd','rpiq']] for r in data['runs']]),'',
        'Ortalama kare hatanın birimi (g/kg)²’dir. Mutlak hata ortancası ve artık standart sapması g/kg, diğer sütunlar birimsizdir. Bütün rollerin ek ölçütleri sayısal ekte korunmuştur.', '',
        '## Tahmin aralıkları ve eksik yerel destek','',
        'TabM ve RealMLP ayrı kalibrasyon verisiyle deneysel mekânsal conformal prediction kullandı. Bu yöntem, kalibrasyon artıklarını tahmin konumuna uzaklıklarına göre ağırlıklandırır.',
        'Gauss çekirdeği, önceden sabitlenmiş 300 m bant genişliği ve yüzde 80 nominal kapsama kullanıldı. Asgari etkin gözlem sayısı iki, asgari grup sayısı ikidir.',
        'Kayıtlı yöntem kesin sonlu örneklem veya mekânsal kapsama güvencesi ileri sürmez. Sabit rasterler de belirli bir gelecek tarihin haritasını doğrulamaz.',
        'TabPFN ayrı kalibrasyona dayalı split conformal kullandı. Nihai kalibrasyonda 20 gözlem, yüzde 80 için 17. sıra ve 0.17716868137517494 g/kg düzeltme kaydedildi.',
        'Bu nihai düzeltme dış kat modellerinin aralık genişliği değildir. Dış kat kapsamaları kendi kalibrasyon kümeleriyle hesaplandı.', '',
        table(['Model','Nominal oran','Aralık var/toplam','Kapsanan/aralık var','Gözlenen kapsama','Ortalama genişlik, g/kg','Durum'],[[r['model'],fmt(c['nominal_coverage']),f"{c['available_n']}/{c['n']}",f"{c.get('covered_n',0)}/{c['available_n']}",fmt(c.get('coverage')),fmt(c.get('mean_width')),c['status']] for r in data['runs'] for c in r['coverage_recomputed']]),'',
        'Yüzde 80 düzeyinde TabM ve RealMLP için yedi gözlemde yerel kalibrasyon ağırlığı sonlu aralık üretmeye yetmedi. Neden kodu `SPATIAL_FINITE_MASS` olarak kaydedildi.',
        'Bu yedi gözlem nokta hata hesabında kaldı, kapsama paydasına girmedi. TabM’nin 16/17 ve RealMLP’nin 17/17 oranı 24 gözlemin tamamının kapsaması değildir.',
        'Yüzde 90 düzeyinde bu iki modelde yalnız bir gözleme aralık üretildi. 1/1 sonucu genel kapsama başarısı olarak yorumlanamaz.',
        'Arazi kullanımı altgruplarında eksik aralık varsa tam altgrup kapsaması `INCOMPLETE_INTERVALS` ile kullanılamaz bırakılmıştır. Kısmi altgrup kapsamı türetilmemiştir.', '',
        '## Rasterler, raporlar ve model kaydı','',
        table(['Model','Doğrulanan çıktı','Raster','Geçerli nokta tahmin hücresi','Aralık hücresi','Aralık dışı açıklayıcı hücre','Rapor sayfası','Rapor veri satırı'],[[r['model'],r['current_readback']['outputs_verified'],len(r['historical_ui_readback']['rasters']),r['historical_ui_readback']['rasters']['prediction.tif']['valid_pixels'],r['historical_ui_readback']['rasters']['prediction_lower.tif']['valid_pixels'],r['mapping_diagnostics']['diagnostic_pixels']['range_extrapolation'],len(r['pagination']['pages']),r['pagination']['source_data_rows']] for r in data['runs']]),'',
        'Rasterler 8 × 8 hücredir. Nokta tahmini, alt/üst sınır ve genişlik g/kg; uzaklık rasteri metre birimindedir. Tanı rasterleri birimsizdir.',
        'Haritadaki 64 hücrenin tamamında nominal aralık üretildi. Bu nedenle desteksiz harita hücresinde nokta tahmininin korunması bu üç gerçek işte fiilen sınanmadı.',
        'Geçerli aralık üretilebilmesi, hücrenin gerçek hedef değerini kapsadığını göstermez. Raster hücrelerinde doğrulama hedefi bulunmamaktadır.',
        'Üst sınır eksi alt sınır ile genişlik rasteri arasındaki en büyük fark TabM ve RealMLP’de 7.152557373046875e-07 g/kg, TabPFN’de 3.2782554626464844e-07 g/kg’dır.',
        'Sayfalanmış raporlarda başlık dahil en çok 120 tablo satırı vardır. Özgün raporların veri satırları sırayla korunmuştur. Bütün sayfalar sonuç bildiriminin dosya özeti kapsamındadır.',
        'Model paketleri kaydedildi. Ön işleme, seçilen değişkenler, hedef dönüşümü, model ve kalibrasyon kayıtları aynı çalışmaya bağlandı. Modeli yeniden yükleyip yeni bilimsel çalışma yapmak bu raporun sınaması değildir.', '',
        table(['Model','Model paketi SHA-256'],[[r['model'],r['historical_ui_readback']['model_package_sha256']] for r in data['runs']]),'',
        '## İptal, yeniden açma ve ek bilgi kabulü','',
        'İptal edilen iş `20261017_221257_a318fea3` için sonuç durumu iptal, çıkış kodu 130’dur. Tamamlanmış çıktı listesi boştur; yarım raster veya model kalıntısı bulunmamıştır.',
        'Kaydedilmiş işçi ve alt süreç kimliklerini hedefleyen son süreç sorgusu boş dönmüştür. Bu, kaydın gözlem anına aittir; rapor hazırlanırken yeni süreç sorgusu yapılmadı.',
        'Önceki üç başarılı çalışmanın 121, 131 ve 116 çıktısı değişmemiştir. Toplam 368 dosya bu üç çalışmanın ürün sayısıdır; bağımsız gözlem sayısı değildir.',
        'Proje yeniden açılıp kaydedildiğinde 13 kalıcı alan aynı kaldı. Ön gösterim oturumuna ait dört geçici alan kapsam dışında tutuldu; gerçek fark yalnız ön gösterim bildirim yolundaydı.',
        'Arayüzden dışa aktarılan TabM model paketinin özeti kaynak paketle aynıdır: `00fda14fd026301ba3f764f6f27740e224bdd0bb1ac7386da52194f3089ae530`.',
        'Sonraki ve ayrı adayda yedi raster ek bilgi alanının türü korunmuştur. `samples.excluded` içindeki D000 dışlaması kaynak, arayüz kaydı ve iş isteğinde aynı kalmıştır.',
        'Bu ayrı Ridge işi `20261017_224753_617460c0`, 47 tutulan gözlemle tamamlanmıştır. D000 eğitim, doğrulama ve tahmin kayıtlarında yoktur. 111 ürünün özeti doğrulanmıştır.',
        'Hedef gözlemlerinden türetilmiş rasterı gösteren `target_derived=true`, ön gösterim isteği dosyası oluşmadan `TARGET_LEAKAGE` ile reddedilmiştir. Çalıştırma düğmesi devre dışı kalmıştır.',
        'İptal/yeniden açma kanıtında 37, ayrı ek bilgi kanıtında 34 denetim olumlu kaydedilmiştir. Bu sayılar arayüz görevlerinin denetimleridir; bilimsel örneklem büyüklüğü değildir.', '',
        '## Dosyalara erişim','',
        'Aşağıdaki bağlantılar özgün tam dosyalara gider. Virgülle ayrılmış değerler (CSV) tabloları rol ve kat bilgisini içerir. JavaScript Object Notation (JSON) kayıtları iç içe model ve ayar ayrıntılarını korur.', '']
    for r in data['runs']:
        path=Path(r['output_directory'])
        lines += [f"### {r['model']}: {r['run_id']}",'',table(['İçerik','Tam dosya'],[
            ['Rapor dizini',link(path/'study_report_index.html')],['Tam çalışma raporu',link(path/'study_report.html')],
            ['Bütün rol ölçütleri',link(path/'publication_metrics.csv')],['Bütün hiperparametre kayıtları',link(path/'hyperparameters.csv')],
            ['Gerçek arama denemeleri',link(path/'optimization_trials.csv')+'; '+link(path/'optimization_trials.json')],
            ['İç doğrulama tahminleri',link(path/'inner_validation_predictions.csv')],['Dış tahminler',link(path/'oof_predictions.csv')],
            ['Eğitim ve kalibrasyon tahminleri',link(path/'training_predictions.csv')+'; '+link(path/'calibration_predictions.csv')],
            ['Dış kat ölçütleri',link(path/'fold_metrics.csv')],['Kapsama ve aralık satırları',link(path/'coverage_diagnostics.csv')+'; '+link(path/'coverage_predictions.csv')],
            ['Altgrup tanıları',link(path/'subgroup_diagnostics.csv')],['Bağımsız tahmin dosyası, sıfır satır',link(path/'independent_predictions.csv')],
            ['Nihai model kaydı',link(path/'final_record.json')],['Dış ve iç bütün model kayıtları',link(path/'evaluation.json')],
            ['Kat kimlikleri ve öğrenme denetimi',link(path/'validation_folds.json')+'; '+link(path/'learning_audit.json')],
            ['İstek, kaynak ve paket bilgisi',link(path/'request.snapshot.json')+'; '+link(path/'audit.json')],
            ['Model paketi',link(path/next(item['path'] for item in r['current_readback']['outputs'] if item['kind']=='model_bundle'))],['Bütün ürünlerin bildirimi',link(path/'result.json')]]) ,'']
    lines += ['## Açık kalan kabul ve bilimsel sınırlar','',
        '- Yeni adayın bağlantı, iptal ve dışa aktarma sonuçları: '+link(ROOT/'ASAMA_05_KABUL.md','0.5 kabul raporu')+'.',
        '- Gerçek toprak verisi, bağımsız test verisi ve farklı bölgeye aktarım bu üç sentetik işte sınanmadı.',
        '- TabPFN ile diğer iki model aynı dış bölmeleri kullanmadı. Bu sonuçlardan üçlü model üstünlüğü çıkarılamaz.',
        '- Izgara araması, birden çok hiperparametre adayı ve başarısız deneme akışı bu üç gerçek işte çalıştırılmadı.',
        '- İç kat eğitim hata kayıtları, özgün birimde örneklenmiş bütün açıklayıcı değerleri içeren rol tablosu ve tam çalışma arşivi eski çıktılarda yoktur.',
        '- Daha sonra eklenen `study_package.spsstudy.zip` özelliğinin yeni adayda ayrıca sınanması 0.5 raporundadır. Eski üç çalışmada bu arşiv üretilmedi.',
        '- Permütasyon önemi ve seçicisiz karşılaştırma bu işlerde istenmedi. Boş tablolar hesaplanmış bulgu sayılmadı.',
        '- Mekânsal aralıkların desteksiz raster hücresindeki davranışı ve grafik işlemcide çalışma bu gerçek işler tarafından kapsanmıyor.',
        '- Sabit iki dönem eğitimi, tek başlangıç değeri ve küçük sentetik veri, TabM veya RealMLP için bilimsel yeterlilik kanıtı değildir.', '',
        'Bütün sayısal değerler özgün dosyalarda korunmuştur. Bu rapordaki tablolar okunabilirlik için en çok sekiz anlamlı basamak gösterir; makine okunur ek tam hassasiyeti taşır.', '']
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
