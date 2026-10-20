"""Validated schema 2; no silent method or parameter fallback."""
from copy import deepcopy
import math
from pathlib import Path
from .contract import JobError, validate_request, required_text, local_file
from .data import read_json

CATALOG=read_json(Path(__file__).with_name('registry.json'))['categories']

def object_keys(obj,allowed,where):
    if not isinstance(obj,dict):raise JobError('REQUEST_TYPE',where+' nesne olmalıdır.')
    extra=set(obj)-set(allowed)
    if extra:raise JobError('UNKNOWN_SETTING',where+': tanınmayan ayar '+', '.join(sorted(extra)))

def choice(value,choices,name):
    if value not in choices:raise JobError('SETTING_VALUE',name+' için desteklenmeyen değer: '+str(value))
    return value

def number(value,lo,hi,name,integer=False):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not lo<=value<=hi or (integer and type(value)!=int):
        raise JobError('SETTING_VALUE',name+' geçerli aralıkta sayı olmalıdır.')
    return value

def parameters(category,identity,values,*,fill=True):
    entry=next((r for r in CATALOG[category] if r['id']==identity),None)
    if not entry or (entry['stage']!='B' and entry.get('status') not in ('implementation_testing','backend_verified','accepted')):raise JobError('UNSUPPORTED_METHOD',category+': yöntem henüz uygulanmadı: '+str(identity))
    schema=entry['parameters'];object_keys(values,schema,category)
    values=deepcopy(values)
    for key,s in schema.items():
        if key not in values:
            if not fill:continue
            values[key]=deepcopy(s['default'])
        val=values[key];typ=s['type']
        if typ in ('int','float'):number(val,s.get('min',-1e300),s.get('max',1e300),key,typ=='int')
        elif typ=='enum':choice(val,[c['id'] if isinstance(c,dict) else c for c in s['choices']],key)
        elif typ=='bool':
            if type(val)!=bool:raise JobError('SETTING_VALUE',key+' mantıksal değer olmalıdır.')
        elif typ in ('ids','fields'):
            if not isinstance(val,list) or any(not isinstance(x,str) or not x.strip() for x in val) or len(set(val))!=len(val):raise JobError('SETTING_VALUE',key+' tekil metin listesi olmalıdır.')
        elif not isinstance(val,str):raise JobError('SETTING_VALUE',key+' metin olmalıdır.')
    return values

def validate_request_b(raw):
    allowed={'schema_version','run_id','task','action','samples','covariates','model','model_candidates','output_dir','window_size','validation','preview_fingerprint','preprocessing','target_transform','feature_selection','vif','optimization','uncertainty','mapping','diagnostics'}
    object_keys(raw,allowed,'İş')
    if raw.get('schema_version')!=2:raise JobError('SCHEMA_VERSION','Bilimsel iş için şema 2 gereklidir.')
    req=deepcopy(raw)
    # Reuse Stage A's proven ID, path, band and memory checks without changing it.
    common={k:deepcopy(req[k]) for k in ('run_id','task','samples','covariates','output_dir','window_size') if k in req}
    common['schema_version']=1;common['model']={'id':'mean','parameters':{}}
    for c in common.get('covariates',[]):c['kind']='continuous'
    checked=validate_request(common)
    for key in ('run_id','samples','window_size'):req[key]=checked[key]
    req['action']=choice(req.get('action','run'),('run','preview'),'action')
    s=req['samples'];object_keys(s,{'path','independent_path','independent_x_field','independent_y_field','independent_crs','id_field','target_field','unit','unit_field','x_field','y_field','crs','group_fields','depth_top_field','depth_bottom_field','method_field','date_field','support','support_note','excluded','polygon_path','polygon_id_field'},'samples')
    if s.get('independent_path'):
        s['independent_path']=local_file(s['independent_path'])
        if Path(s['independent_path']).suffix.lower() not in ('.csv','.json','.geojson'):
            raise JobError('SAMPLE_FORMAT','Bağımsız test CSV veya GeoJSON olmalıdır.')
        if Path(s['independent_path']).suffix.lower()=='.csv':
            for key in ('independent_x_field','independent_y_field','independent_crs'):
                required_text(s.get(key),'INDEPENDENT_COORDINATES','Bağımsız CSV için koordinat alanları ve koordinat sistemi ayrıca belirtilmelidir.')
    s.setdefault('group_fields',[])
    if not isinstance(s['group_fields'],list) or any(not isinstance(x,str) or not x.strip() for x in s['group_fields']):raise JobError('GROUP_FIELD','Grup alanları metin listesi olmalıdır.')
    choice(s.setdefault('support','point'),('point','composite','parcel_mean'),'support')
    if s['support']=='parcel_mean':
        s['polygon_path']=local_file(s.get('polygon_path'));required_text(s.get('polygon_id_field'),'SUPPORT','Parsel kimlik alanı gereklidir.')
    if bool(s.get('depth_top_field'))!=bool(s.get('depth_bottom_field')):raise JobError('DEPTH','Alt ve üst derinlik alanları birlikte seçilmelidir.')
    if s['support']=='composite' and not s.get('support_note'):raise JobError('SUPPORT','Kompozit örneğin temsil ettiği alan açıklanmalıdır.')
    for e in s.get('excluded',[]):
        object_keys(e,{'id','reason'},'excluded');required_text(e.get('id'),'EXCLUDED_ID','Dışlanan gözlem kimliği gereklidir.');required_text(e.get('reason'),'EXCLUDED_REASON','Dışlama gerekçesi gereklidir.')
    for i,cov in enumerate(req['covariates']):
        object_keys(cov,{'id','name','path','band','unit','kind','sampling','resampling','sha256','band_description','time_representation','original_resolution','provenance','target_derived'},'covariates')
        cov['path']=checked['covariates'][i]['path'];choice(cov.get('kind'),('continuous','categorical'),'kind')
        for k in ('sampling','resampling'):choice(cov.setdefault(k,'nearest'),('nearest','bilinear'),k)
        if cov['kind']=='categorical' and (cov['sampling']!='nearest' or cov['resampling']!='nearest'):raise JobError('CATEGORY_RESAMPLING','Kategorik raster iki doğrusal enterpolasyonla işlenemez.')
        if cov.get('target_derived'):raise JobError('TARGET_LEAKAGE','Hedef gözlemlerinden türetilen raster, bağımsızlık kanıtı olmadan kullanılamaz.')
    v=req.setdefault('validation',{});object_keys(v,{'method','n_splits','inner_splits','repeats','seed','block_size','buffer_distance','metric_crs','custom_folds','independent_ids','purpose','inner_method','min_train_groups','maxp','prediction_samples','temporal_mode','timezone','gap_days','initial_train_periods','inner_initial_train_periods','selector_initial_train_periods'},'validation')
    method=v.get('method','group' if s['group_fields'] else 'random')
    extras={k:v[k] for k in ('custom_folds','independent_ids','purpose') if k in v}
    vals=parameters('validation',method,{k:x for k,x in v.items() if k not in ('method',*extras)})
    req['validation']={'method':method,**vals,**extras}
    if not req['validation'].get('metric_crs'):req['validation'].pop('metric_crs',None)
    if method=='knndm':
        if req['validation']['maxp']<=max(1/req['validation']['n_splits'],1/req['validation']['inner_splits']):raise JobError('KNNDM_PARAMETER','Azami kat oranı hem dış hem iç kat sayısının tersinden büyük olmalıdır.')
        if req['validation']['buffer_distance']!=0:raise JobError('KNNDM_BUFFER','kNNDM sonrasında tampon eklenemez.')
    req['validation'].setdefault('independent_ids',[]);req['validation'].setdefault('purpose','within_area')
    choice(req['validation']['purpose'],('within_area','transfer','independent'),'purpose')
    if method!='repeated' and req['validation']['repeats']!=1:raise JobError('REPEATS','Tekrar sayısı yalnız tekrarlı yöntem içindir.')
    if method=='temporal':
        required_text(s.get('date_field'),'TEMPORAL_DATE','Zaman sıralı doğrulama için tarih alanı seçilmelidir.')
        from .temporal_d import normalize_dates
        normalize_dates([],[],s['date_field'],req['validation']['timezone'])
    req['preprocessing']=parameters('preprocessing','standard_pipeline',req.get('preprocessing',{}))
    d=req.setdefault('diagnostics',{});object_keys(d,{'applicability','permutation','permutation_repeats','subgroup_fields','min_subgroup_samples','min_subgroup_groups'},'diagnostics')
    for key in ('applicability','permutation'):
        if type(d.setdefault(key,False))!=bool:raise JobError('SETTING_VALUE',key+' mantıksal olmalıdır.')
    number(d.setdefault('permutation_repeats',5),2,100,'permutation_repeats',True)
    number(d.setdefault('min_subgroup_samples',10),3,1000,'min_subgroup_samples',True)
    number(d.setdefault('min_subgroup_groups',3),2,1000,'min_subgroup_groups',True)
    fields=d.setdefault('subgroup_fields',[])
    if not isinstance(fields,list) or any(not isinstance(x,str) or not x.strip() for x in fields) or len(set(fields))!=len(fields):raise JobError('SETTING_VALUE','Altgrup alanları tekil alan adları olmalıdır.')
    if d['applicability'] and method=='repeated' and req['validation']['repeats']>1:raise JobError('AOA_REPEATS','CAST ile doğrulanan eşik bu sürümde tek tekrarlı kat düzeni gerektirir.')
    if d['applicability'] and method=='temporal':raise JobError('TEMPORAL_APPLICABILITY_UNSUPPORTED','Zaman sıralı ilk eğitim geçmişi için uygulanabilirlik eşiği bu sürümde doğrulanmadı. Bu tanıyı kapatın.')
    for key,default,selector in [('target_transform','none','method'),('feature_selection','none','method'),('vif','diagnostic','mode'),('uncertainty','none','method')]:
        value=req.get(key,{})
        if not isinstance(value,dict):raise JobError('SETTING_VALUE',key+' nesne olmalıdır.')
        ident=value.get(selector,default);params=parameters(key,ident,{k:v for k,v in value.items() if k!=selector})
        req[key]={selector:ident,**params}
    models=req.get('model_candidates') or [req.get('model',{'id':'ridge','parameters':{'alpha':1.}})]
    if method=='temporal' and req['feature_selection']['method']=='sfs':
        v=req['validation']
        if v['inner_initial_train_periods']<v['selector_initial_train_periods']+v['inner_splits']:
            raise JobError('TEMPORAL_SELECTOR_PERIODS','İlk iç eğitim geçmişi, ardışık seçicinin başlangıç ve sınama dönemlerinin toplamından küçük olamaz.')
    if not isinstance(models,list) or not models:raise JobError('MODEL','Model listesi boş olamaz.')
    for model in models:
        object_keys(model,{'id','parameters'},'model');model['parameters']=parameters('model',model.get('id'),model.get('parameters',{}))
        if model['id'] in ('ordinary_kriging','regression_kriging'):
            if req['target_transform']['method']!='none' or req['uncertainty']['method'] not in ('none','split_conformal','spatial_conformal'):
                raise JobError('KRIGING_COMPATIBILITY','Kriging özgün hedef biriminde, aralıksız veya bölünmüş konformal yöntemle çalışır.')
            if req['feature_selection']['method'] in ('sfs','rfe'):raise JobError('KRIGING_SELECTOR','Krigingde ardışık ve özyinelemeli seçim desteklenmiyor.')
            if model['id']=='ordinary_kriging' and (req['feature_selection']['method']!='none' or req['vif']['mode']!='off' or d['applicability']):
                raise JobError('KRIGING_SELECTOR','Adi krigingde çevresel değişken seçimi, varyans şişirme tanısı ve çevresel uygulanabilirlik kapalı olmalıdır.')
        if model['id']=='cubist' and req['feature_selection']['method']=='rfe':raise JobError('SELECTOR_MODEL','Cubist için özyinelemeli eleme desteklenmiyor.')
        if model['id'] in ('tabm','realmlp','tabpfn') and req['feature_selection']['method'] in ('sfs','rfe'):
            raise JobError('SELECTOR_MODEL','Deneysel tablo modellerinde ardışık ve özyinelemeli seçim bu sürümde desteklenmiyor.')
        unc=req['uncertainty'];um=unc['method'];qm=unc.get('quantile_model') if um=='cqr' else um
        if qm in ('quantile_forest','quantile_boosting'):
            expected='random_forest' if qm=='quantile_forest' else 'hist_gradient_boosting'
            if model['id']!=expected:raise JobError('UNCERTAINTY_MODEL',um+' için '+expected+' modeli gereklidir.')
        if req['feature_selection']['method']=='rfe' and model['id'] in ('mean','median','hist_gradient_boosting'):
            raise JobError('SELECTOR_MODEL','Özyinelemeli eleme seçili modelde desteklenmiyor.')
        if req['feature_selection']['method']=='rfe' and model['id']=='svr' and model['parameters'].get('kernel')!='linear':raise JobError('SELECTOR_MODEL','Özyinelemeli eleme doğrusal çekirdek gerektirir.')
    req['model']=deepcopy(models[0])
    if 'model_candidates' in req:req['model_candidates']=models
    o=req.get('optimization',{});om=o.get('method','manual');spaces=o.get('spaces',{})
    object_keys(o,{'method','spaces','trials','timeout_seconds','seed','metric','n_jobs'},'optimization')
    req['optimization']={'method':om,**parameters('optimization',om,{k:v for k,v in o.items() if k not in ('method','spaces')}),'spaces':spaces}
    if not isinstance(spaces,dict):raise JobError('SEARCH_SPACE','Arama uzayı nesne olmalıdır.')
    for name,space in spaces.items():
        object_keys(space,{'type','choices','low','high','log'},'spaces.'+name)
        choice(space.get('type'),('categorical','int','float'),name)
        if space['type']=='categorical':
            if not isinstance(space.get('choices'),list) or not space['choices']:raise JobError('SEARCH_SPACE','Aday değerler boş olamaz.')
        else:
            number(space.get('low'),-1e12,1e12,name,space['type']=='int');number(space.get('high'),space['low'],1e12,name,space['type']=='int')
            if space.get('log') and space['low']<=0:raise JobError('SEARCH_SPACE','Logaritmik aramada alt sınır pozitif olmalıdır.')
    m=req.setdefault('mapping',{});object_keys(m,{'mask_extrapolation','physical_min','physical_max','figure_dpi','align_rasters','reference_id','max_missing_fraction'},'mapping')
    for k in ('mask_extrapolation','align_rasters'):
        if type(m.setdefault(k,False))!=bool:raise JobError('SETTING_VALUE',k+' mantıksal olmalıdır.')
    choice(m.setdefault('figure_dpi',150),(150,900),'figure_dpi');number(m.setdefault('max_missing_fraction',0.5),0,1,'max_missing_fraction')
    for k in ('physical_min','physical_max'):
        if k in m and m[k] is not None:number(m[k],-1e300,1e300,k)
    if m.get('physical_min') is not None and m.get('physical_max') is not None and m['physical_min']>=m['physical_max']:raise JobError('PHYSICAL_RANGE','Fiziksel sınırların sırası geçersiz.')
    if req['action']=='run':required_text(req.get('preview_fingerprint'),'PREVIEW_REQUIRED','Modelden önce güncel kat ön gösterimi gereklidir.')
    return req
