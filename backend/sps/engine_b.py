"""Schema 2 preview, nested evaluation and atomic block raster publication."""
from __future__ import annotations
from contextlib import ExitStack
from datetime import datetime,timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import sys
import traceback
import numpy as np
from pyproj import CRS,Transformer,network
import rasterio
from rasterio.windows import Window
from scipy.spatial import cKDTree
from threadpoolctl import threadpool_limits
from .contract import JobError,Cancelled
from .contract_b import validate_request_b
from .data import fingerprint
from .data_b import read_samples_b,RasterInputsB
from .engine import reserve_output,write_json
from .validation import build_groups,make_folds,indices_for,_metric_coordinates
from .modeling import prediction_call,bind_runtime_callbacks
from . import __version__


def digest_obj(obj):
    return hashlib.sha256(json.dumps(obj,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()

def preview_signature(req,samples,covs,source_records):
    settings={k:v for k,v in req.items() if k not in ('action','run_id','output_dir','preview_fingerprint','samples','covariates','window_size')}
    spec={k:v for k,v in req['samples'].items() if k not in ('path',)}
    records=[{'id':identity,'y':float(y),'xy':list(xy),'props':p} for identity,y,xy,p in zip(samples['ids'],samples['y'],samples['coordinates'],samples['props'])]
    settings['samples']={'spec':spec,'records':sorted(records,key=lambda r:r['id']),'crs':samples['crs'].to_wkt()}
    bypath={str(Path(s['path']).resolve()).casefold():s for s in source_records}
    settings['covariates']=[{**{k:v for k,v in c.items() if k!='path'},'source_sha256':bypath[str(Path(c['path']).resolve()).casefold()]['sha256']} for c in covs]
    settings['source_files']=[{'path':p,'sha256':bypath[p]['sha256']} for p in sorted(bypath)]
    if req['samples'].get('polygon_path'):settings['polygon_sha256']=bypath[str(Path(req['samples']['polygon_path']).resolve()).casefold()]['sha256']
    return digest_obj(settings)

def write_folds(path,folds,samples,groups,*,independent_ids=()):
    transform=Transformer.from_crs(samples['crs'],'EPSG:4326',always_xy=True)
    x,y=transform.transform(*samples['coordinates'].T,errcheck=True)
    lookup={identity:i for i,identity in enumerate(samples['ids'])};features=[]
    for f in folds:
        for role,key in [('test','test_ids'),('excluded','excluded_ids')]:
            for identity in f.get(key,[]):
                i=lookup[identity]
                features.append({'type':'Feature','geometry':{'type':'Point','coordinates':[float(x[i]),float(y[i])]},'properties':{'sample_id':identity,'repeat':f['repeat'],'fold':f['fold'],'role':role,'group_id':str(groups[i])}})
    evaluated={identity for f in folds for identity in f['test_ids']}
    independent=set(independent_ids)
    for identity in sorted(set(samples['ids'])-evaluated):
        role='independent' if identity in independent else 'initial_training'
        i=lookup[identity];features.append({'type':'Feature','geometry':{'type':'Point','coordinates':[float(x[i]),float(y[i])]},'properties':{'sample_id':identity,'repeat':-1 if role=='independent' else 0,'fold':-1,'role':role,'group_id':str(groups[i])}})
    write_json(path,{'type':'FeatureCollection','features':features})

def windows(ref,size):
    for row in range(0,ref.height,size):
        for col in range(0,ref.width,size):yield Window(col,row,min(size,ref.width-col),min(size,ref.height-row))


def sample_mapping_domain(rasters,config,check_cancel):
    """Regular cell centres from the joint valid mask, independent of targets."""
    ref=rasters.reference;target=config.get('prediction_samples',1000)
    step=max(1,int(math.ceil(math.sqrt(ref.width*ref.height/target))))
    points=[]
    for w in windows(ref,256):
        check_cancel();_,valid=rasters.read_window(w)
        rr,cc=np.indices(valid.shape);rr+=int(w.row_off);cc+=int(w.col_off)
        mask=valid & (rr%step==step//2) & (cc%step==step//2)
        a=ref.transform;x=cc[mask]+.5;y=rr[mask]+.5
        points.extend(np.column_stack((a.a*x+a.b*y+a.c,a.d*x+a.e*y+a.f)).tolist())
    if not 5<=len(points)<=5000:raise JobError('KNNDM_DOMAIN','Geçerli maske üzerinde 5-5000 alan noktası gerekir. Alan örnekleme hedefini değiştirin.')
    # Stable ordering is essential: raster-window layout must not alter the CV.
    config['prediction_coordinates']=sorted(points)
    config['prediction_crs']=ref.crs.to_wkt()
    config['prediction_sample']={'method':'regular_joint_valid_cell_centres','step_cells':step,'count':len(points),'target':target}

def preview_workload(req,folds):
    arms=len(req.get('model_candidates') or [req['model']])*(2 if req['feature_selection'].get('compare_without_selection') else 1)
    trials=arms if req['optimization']['method']=='manual' else req['optimization']['trials']
    inner=req['validation']['inner_splits']
    return {'candidate_arms':arms,'trial_budget':trials,'inner_folds':inner,'outer_folds':len(folds),
            'estimated_pipeline_fits_upper_bound':(len(folds)+1)*(trials*inner+1),
            'extra_selector_fits_not_included':req['feature_selection']['method'] in ('sfs','rfe','boruta','elastic_net'),
            'threads_per_fit':1,'early_stopping':False,
            'note':'Süre tahmini değildir. Seçici ve kantil altmodelleri ek eğitim yapabilir; zaman bütçesi denemeler arasında denetlenir.'}


def record_independent_evaluation(output,req,samples):
    ids=req['validation']['independent_ids']
    if not ids:return {'status':'not_supplied','previous_evaluations':0}
    index=indices_for(samples['ids'],sorted(ids))
    identity=digest_obj({'ids':sorted(ids),'xy':samples['coordinates'][index].tolist(),'crs':samples['crs'].to_wkt(),'target':req['samples']['target_field'],'unit':req['samples']['unit']})
    scientific=req['preview_fingerprint']
    path=output.parent.parent/'.sps-independent-history.json';lock=path.with_suffix('.lock')
    try:
        with lock.open('x') as f:f.write(req['run_id'])
    except FileExistsError:raise JobError('TEST_HISTORY_LOCK','Bağımsız test geçmişi başka bir iş tarafından yazılıyor.')
    try:
        history=json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
        previous=[r for r in history if r['test_fingerprint']==identity]
        changed=any(r['settings_fingerprint']!=scientific for r in previous)
        state='reused_after_settings_change' if changed else 'repeated_same_settings' if previous else 'first_evaluation'
        history.append({'test_fingerprint':identity,'settings_fingerprint':scientific,'run_id':req['run_id'],'finished_utc':datetime.now(timezone.utc).isoformat()})
        write_json(path,history)
        return {'status':state,'previous_evaluations':len(previous),'settings_changed_since_previous':changed,'test_fingerprint':identity,'warning':'Test sonuçları görüldükten sonra ayarlar değiştiyse bu veri yeni bağımsız başarı kanıtı değildir.' if previous else 'İlk kayıt. Olasılıklı alan doğrulaması olduğu varsayılmaz.'}
    finally:lock.unlink(missing_ok=True)

def map_products(output,rasters,model,record,req,samples,check_cancel,progress):
    ref=rasters.reference;unit=req['samples']['unit'];size=req['window_size']
    kinds={'prediction':('prediction.tif',unit),'range_extrapolation':('range_extrapolation.tif','1'),'unknown_category':('unknown_category.tif','1'),'physical_violation':('physical_violation.tif','1')}
    if req['uncertainty']['method']!='none':kinds.update(lower=('prediction_lower.tif',unit),upper=('prediction_upper.tif',unit),width=('prediction_interval_width.tif',unit))
    if req['uncertainty']['method'] in ('quantile_forest','quantile_boosting','cqr'):kinds['quantile_crossing']=('quantile_crossing.tif','1')
    spatial_interval=req['uncertainty']['method']=='spatial_conformal'
    if spatial_interval:kinds.update(interval_available=('interval_available.tif','1'),interval_effective_n=('interval_effective_n.tif','1'),interval_effective_group_n=('interval_effective_group_n.tif','1'))
    if req.get('diagnostics',{}).get('applicability'):kinds.update(applicability=('applicability.tif','1'),dissimilarity=('dissimilarity.tif','1'))
    distance_crs=req['validation'].get('metric_crs') or (ref.crs.to_wkt() if ref.crs.is_projected else None)
    distance_fn=None
    if distance_crs:
        metric=CRS.from_user_input(distance_crs)
        if not metric.is_projected:raise JobError('METRIC_CRS','Uzaklık rasteri için projeksiyon gerekir.')
        factor=np.asarray([a.unit_conversion_factor for a in metric.axis_info[:2]])
        sample_tx=Transformer.from_crs(samples['crs'],metric,always_xy=True);point_tx=Transformer.from_crs(ref.crs,metric,always_xy=True)
        ids=record.get('development_ids')
        if not ids:raise JobError('MODEL_TRAINING_IDS','Nihai modelin eğitim kimlikleri kayıtta bulunamadı.')
        index=indices_for(samples['ids'],ids);coords=samples['coordinates'][index]
        training_xy=np.column_stack(sample_tx.transform(*coords.T,errcheck=True))*factor;tree=cKDTree(training_xy)
        def distance_fn(w,valid):
            rr,cc=np.indices(valid.shape);cc=cc+int(w.col_off)+.5;rr=rr+int(w.row_off)+.5
            a=ref.transform;xx=a.a*cc+a.b*rr+a.c;yy=a.d*cc+a.e*rr+a.f
            xy=np.column_stack(point_tx.transform(xx[valid],yy[valid],errcheck=True))*factor
            return tree.query(xy,k=1)[0]
        kinds['nearest_distance']=('nearest_sample_distance.tif','m')
    profile=dict(driver='GTiff',width=ref.width,height=ref.height,count=1,dtype='float32',crs=ref.crs,transform=ref.transform,nodata=float('nan'),compress='deflate',tiled=True,blockxsize=256,blockysize=256,BIGTIFF='IF_SAFER')
    total=math.ceil(ref.height/size)*math.ceil(ref.width/size);valid_count=0;flag_counts={k:0 for k in ('range_extrapolation','unknown_category','physical_violation','quantile_crossing','applicability') if k in kinds}
    outside_applicability_count=0;available_interval_count=0
    coordinate_tx=Transformer.from_crs(ref.crs,samples['crs'],always_xy=True) if getattr(model,'requires_coordinates',False) else None
    def compute(w):
        values,valid=rasters.read_window(w);outputs={k:np.full(valid.shape,np.nan,dtype='float32') for k in kinds}
        if valid.any():
            X=values[valid];coordinates=None
            if coordinate_tx is not None:
                rr,cc=np.indices(valid.shape);cc=cc+int(w.col_off)+.5;rr=rr+int(w.row_off)+.5
                a=ref.transform;xx=a.a*cc+a.b*rr+a.c;yy=a.d*cc+a.e*rr+a.f
                coordinates=np.column_stack(coordinate_tx.transform(xx[valid],yy[valid],errcheck=True))
            prediction=np.asarray(prediction_call(model,'predict',X,coordinates)).reshape(-1)
            diag=prediction_call(model,'diagnostics',X,coordinates)
            outside=np.asarray(diag['range_extrapolation'],dtype=bool);unknown=np.asarray(diag['unknown_category'],dtype=bool)
            physical=np.zeros(len(prediction),dtype=bool)
            low=req['mapping'].get('physical_min');high=req['mapping'].get('physical_max')
            if low is not None:physical|=prediction<low
            if high is not None:physical|=prediction>high
            computed={'prediction':prediction,'range_extrapolation':outside,'unknown_category':unknown,'physical_violation':physical}
            available_interval=np.ones(len(X),dtype=bool)
            if spatial_interval:
                available_interval=np.asarray(diag.get('interval_available',[]))
                if available_interval.shape!=(len(X),) or not np.isin(available_interval,(0,1)).all():raise JobError('INTERVAL_SUPPORT','Her geçerli hücre için aralık desteği açıkça belirtilmelidir.')
                available_interval=available_interval.astype(bool)
                computed.update(interval_available=available_interval,
                                interval_effective_n=diag.get('interval_effective_n',[]),
                                interval_effective_group_n=diag.get('interval_effective_group_n',[]))
            if 'applicability' in kinds:
                applicable=np.asarray(diag['applicability'])
                if applicable.shape!=(len(X),) or not np.isin(applicable,(0,1)).all():raise JobError('APPLICABILITY_DIAGNOSTIC','Uygulanabilirlik tanısı her geçerli hücre için 0 veya 1 olmalıdır.')
                applicable=applicable.astype(bool)
                computed.update(applicability=applicable,dissimilarity=diag['dissimilarity'])
            if 'quantile_crossing' in kinds:computed['quantile_crossing']=np.asarray(diag.get('quantile_crossing',np.zeros(len(X))),dtype=bool)
            interval=prediction_call(model,'predict_interval',X,coordinates)
            if interval is not None:
                lower,upper=map(np.asarray,interval)
                if spatial_interval:
                    if lower.shape!=(len(X),) or upper.shape!=(len(X),):raise JobError('INTERVAL_SUPPORT','Aralık boyutu hücre sayısıyla uyuşmuyor.')
                    if not (np.isfinite(lower[available_interval]).all() and np.isfinite(upper[available_interval]).all()
                            and np.isnan(lower[~available_interval]).all() and np.isnan(upper[~available_interval]).all()):
                        raise JobError('INTERVAL_SUPPORT','Aralık desteği ile sonlu sınırlar uyuşmuyor. Desteksiz iki sınır da eksik olmalıdır.')
                if (lower>upper).any():raise JobError('QUANTILE_CROSSING','Alt sınır üst sınırı aştı.')
                computed.update(lower=lower,upper=upper,width=upper-lower)
            if 'lower' in kinds and interval is None:raise JobError('INTERVAL_MISSING','Seçili belirsizlik yöntemi aralık üretmedi.')
            if distance_fn:computed['nearest_distance']=distance_fn(w,valid)
            mask=outside|unknown if req['mapping']['mask_extrapolation'] else np.zeros(len(X),dtype=bool)
            if req['mapping']['mask_extrapolation'] and 'applicability' in kinds:mask|=~applicable
            for kind,array in computed.items():
                arr=np.asarray(array,dtype=float)
                expected_finite=available_interval if spatial_interval and kind in ('lower','upper','width') else np.ones(len(X),dtype=bool)
                if arr.shape!=(len(X),) or not np.isfinite(arr[expected_finite]).all() or (np.abs(arr[expected_finite])>np.finfo('float32').max).any():raise JobError('NONFINITE_PREDICTION','Geçersiz veya sayı türünü aşan çıktı: '+kind)
                if kind in ('interval_effective_n','interval_effective_group_n') and (arr<0).any():raise JobError('INTERVAL_SUPPORT','Etkin kalibrasyon sayısı negatif olamaz.')
                arr=arr.astype('float32')
                if kind in ('prediction','lower','upper','width'):arr[mask]=np.nan
                outputs[kind][valid]=arr
        return outputs,valid
    with ExitStack() as stack:
        targets={k:stack.enter_context(rasterio.open(output/(name+'.partial.tif'),'w',**profile)) for k,(name,u) in kinds.items()}
        for kind,dst in targets.items():
            dst.set_band_unit(1,kinds[kind][1]);dst.set_band_description(1,req['samples']['target_field']+' / '+kind)
            dst.update_tags(run_id=req['run_id'],target=req['samples']['target_field'],target_unit=kinds[kind][1],depth=json.dumps(samples['depth']),method=record.get('model',{}).get('id',req['model']['id']),nominal_coverage=str(req['uncertainty'].get('coverage','not_applicable')),validation_status='nested_cross_validation',sampling_support=samples['support'],core_version=__version__,covariate_ids=json.dumps([c['id'] for c in req['covariates']]),uncertainty_method=req['uncertainty']['method'],product=kind)
        for n,w in enumerate(windows(ref,size),1):
            check_cancel();computed,valid=compute(w);valid_count+=int(valid.sum())
            for k,a in computed.items():targets[k].write(a,1,window=w)
            for k in flag_counts:flag_counts[k]+=int(np.nansum(computed[k]))
            if 'applicability' in kinds:outside_applicability_count+=int(np.count_nonzero(computed['applicability'][valid]==0))
            if spatial_interval:available_interval_count+=int(np.nansum(computed['interval_available']))
            progress('mapping',n,total,'Tahmin ve tanı rasterleri yazılıyor.')
    paths=[]
    # Verify every product before any of the final names is published.
    with ExitStack() as stack:
        saved={k:stack.enter_context(rasterio.open(output/(name+'.partial.tif'))) for k,(name,u) in kinds.items()}
        for k,dst in saved.items():
            if (dst.width,dst.height,dst.crs,dst.transform)!=(ref.width,ref.height,ref.crs,ref.transform) or dst.units!=(kinds[k][1],):raise JobError('OUTPUT_METADATA','Kaydedilen raster gridi veya birimi farklı.')
        for n,w in enumerate(windows(ref,size),1):
            check_cancel();computed,_=compute(w)
            for kind,dst in saved.items():
                if not np.array_equal(dst.read(1,window=w),computed[kind],equal_nan=True):raise JobError('OUTPUT_READBACK','Kaydedilen raster tahminle uyuşmuyor: '+kind)
            progress('verification',n,total,'Kaydedilen rasterler yeniden okunarak doğrulanıyor.')
    for kind,(name,u) in kinds.items():
        (output/(name+'.partial.tif')).replace(output/name);paths.append((kind,output/name))
    audit={'valid_input_pixels':valid_count,'diagnostic_pixels':flag_counts,'nearest_distance_crs':distance_crs,'output_raster_readback_verified':True}
    if spatial_interval:
        audit['interval_support']={'valid_input_pixels':valid_count,'available_input_pixels':available_interval_count,
            'unavailable_input_pixels':valid_count-available_interval_count,
            'available_fraction':available_interval_count/valid_count if valid_count else None,
            'denominator':'valid_input_pixels_before_prediction_mask',
            'scope':'Yerel kalibrasyon desteği bulunmayan hücrelerde yalnız aralık sınırları ve genişliği NoData olur. Bu pay kapsama başarısı değildir.'}
    if 'applicability' in kinds:
        inside=flag_counts['applicability'];flag_counts['outside_applicability']=outside_applicability_count
        projected=bool(ref.crs.is_projected)
        audit['applicability_coverage']={
            'status':'available' if valid_count else 'no_valid_input_pixels',
            'denominator':'valid_input_pixels_before_prediction_mask',
            'valid_input_pixels':valid_count,'inside_pixels':inside,'outside_pixels':outside_applicability_count,
            'inside_fraction':inside/valid_count if valid_count else None,
            'outside_fraction':outside_applicability_count/valid_count if valid_count else None,
            'measure':'projected_grid_plane_area_fraction' if projected else 'grid_cell_fraction',
            'measure_label':'Geçerli hücrelerin grid düzlemindeki alan payı' if projected else 'Geçerli hücrelerin payı (coğrafi alan oranı değildir)',
            'grid_crs':ref.crs.to_wkt(),'grid_crs_is_projected':projected,'grid_crs_is_geographic':bool(ref.crs.is_geographic),
            'constant_cell_area_grid_units_squared':abs(ref.transform.a*ref.transform.e-ref.transform.b*ref.transform.d) if projected else None,
            'prediction_mask_includes_outside_applicability':bool(req['mapping']['mask_extrapolation']),
            'scope_limit':'Payda, tahmin maskesi uygulanmadan önce geçerli girdileri olan hücrelerdir. Veri olmayan hücreler ve raster kapsamı dışı yerler dahil değildir.',
            'area_limit':('Sabit büyüklükteki hücreler projeksiyonun grid düzleminde eşit alanlıdır. Bu pay yeryüzündeki veya elipsoit üzerindeki alan payı olarak yorumlanmaz.'
                          if projected else 'Coğrafi gridde hücrelerin yeryüzündeki alanları eşit kabul edilmedi. Alan ağırlığı veya jeodezik alan hesabı yapılmadı; yalnız hücre payı verilir.')}
    return paths,audit


def run_job_b(request,emit=lambda event:None):
    run_id=request.get('run_id','unknown') if isinstance(request,dict) else 'unknown'
    result={'schema_version':2,'run_id':run_id,'status':'failed','outputs':[],'stage':'B'}
    output=None;published_reports=[];published_data=[];started=datetime.now(timezone.utc).isoformat()
    def check_cancel():
        if output and (output/'cancel.request').exists():raise Cancelled()
    def progress(stage,completed,total,message):
        event=dict(event='progress',run_id=run_id,stage=stage,completed=int(completed),total=int(total),message=message)
        if output:
            with (output/'progress.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(event,ensure_ascii=False)+'\n')
        emit(event)
    try:
        network.set_network_enabled(False);output=reserve_output(request)
        write_json(output/'INCOMPLETE.json',{'status':'running','run_id':run_id,'message':'Sonuç kaydı completed olmadıkça bu çalışma tamamlanmış değildir.'})
        check_cancel();req=validate_request_b(request)
        c_models={'lightgbm','catboost','cubist','ordinary_kriging','regression_kriging'}
        if req['validation']['method']=='knndm' or any(m['id'] in c_models for m in (req.get('model_candidates') or [req['model']])) or any(req['diagnostics'][k] for k in ('applicability','permutation','subgroup_fields')):result['stage']='C'
        if req['validation']['method']=='temporal' or req['uncertainty']['method']=='spatial_conformal' or any(m['id'] in ('tabm','realmlp','tabpfn') for m in (req.get('model_candidates') or [req['model']])):result['stage']='D'
        write_json(output/'request.snapshot.json',req);progress('preflight',0,1,'Birimler, derinlikler, kaynaklar ve katlar denetleniyor.')
        samples=read_samples_b(req['samples'],check_cancel)
        req['validation']['independent_ids']=sorted(set(req['validation']['independent_ids'])|set(samples['external_independent_ids']))
        if req['validation']['method']=='temporal':
            from .temporal_d import normalize_dates
            req['validation']['dates_by_id']=normalize_dates(samples['ids'],samples['props'],req['samples']['date_field'],req['validation']['timezone'])
        write_json(output/'request.snapshot.json',req)
        groups=build_groups(samples['ids'],samples['props'],samples['coordinates'],req['samples']['group_fields'],crs=samples['crs'])
        with RasterInputsB(req['covariates'],req['mapping'],output/'working_rasters',check_cancel) as rasters:
            paths=sorted(set(rasters.source_paths()+[req['samples']['path']]+[req['samples'][k] for k in ('polygon_path','independent_path') if req['samples'].get(k)]))
            sources=[fingerprint(p,check_cancel) for p in paths]
            X,map_coords=rasters.extract(samples,check_cancel)
            if req['validation']['method']=='knndm':
                sample_mapping_domain(rasters,req['validation'],check_cancel)
                write_json(output/'request.snapshot.json',req)
            missing=np.mean(~np.isfinite(X),axis=0)
            if np.any(missing>req['mapping']['max_missing_fraction']) or np.any(np.all(~np.isfinite(X),axis=0)):raise JobError('MISSING_COVARIATE','Eksik girdi oranı sınırı aşıldı veya tüm sütun eksik.')
            folds=make_folds(samples['ids'],samples['coordinates'],groups,req['validation'],crs=samples['crs'],check_cancel=check_cancel)
            metric,metric_crs=_metric_coordinates(samples['coordinates'],samples['crs'],req['validation'],bool(req['validation'].get('metric_crs')) or samples['crs'].is_projected)
            for f in folds:
                tr=indices_for(samples['ids'],f['train_ids']);te=indices_for(samples['ids'],f['test_ids'])
                f['environmental_conditions']=[]
                for j,cov in enumerate(req['covariates']):
                    a=X[tr,j][np.isfinite(X[tr,j])];b=X[te,j][np.isfinite(X[te,j])]
                    if not len(a) or not len(b):continue
                    condition={'id':cov['id'],'kind':cov['kind']}
                    if cov['kind']=='categorical':condition.update(train_categories=np.unique(a).tolist(),test_categories=np.unique(b).tolist(),unknown_test_categories=np.setdiff1d(b,a).tolist())
                    else:condition.update(train_min=float(a.min()),train_max=float(a.max()),test_min=float(b.min()),test_max=float(b.max()))
                    f['environmental_conditions'].append(condition)
                if metric is not None:
                    distances=cKDTree(metric[tr]).query(metric[te],k=1)[0]
                    f['diagnostics'].update(test_to_train_distance_m={'min':float(distances.min()),'median':float(np.median(distances)),'max':float(distances.max())},metric_crs=metric_crs)
            signature=preview_signature(req,samples,req['covariates'],sources)
            result['preview_fingerprint']=signature
            if req['action']=='run' and req['preview_fingerprint']!=signature:raise JobError('PREVIEW_STALE','Veri veya ayarlar ön gösterimden sonra değişti. Katları yeniden gösterin.')
            write_folds(output/'folds.geojson',folds,samples,groups,independent_ids=req['validation']['independent_ids'])
            preview={'sample_count':len(samples['ids']),'independent_group_count':len(set(groups)),'independent_test_count':len(req['validation']['independent_ids']),'folds':folds,'missing_fraction':missing.tolist(),'depth':samples['depth'],'laboratory_method':samples['laboratory_method'],'support':samples['support'],'alignment':rasters.alignment,'preview_fingerprint':signature,'workload':preview_workload(req,folds),'warnings':(['Uzaklık tanısı hesaplanmadı: metrik projeksiyon seçilmelidir.'] if metric is None else [])+(['Parsel alanı ortalamalarıyla kurulan model hücrelere uygulanır; hücre ölçüm desteği ayrıca doğrulanmış değildir.'] if samples['support']=='parcel_mean' else [])}
            if req['validation']['method']=='temporal':
                tested={i for f in folds for i in f['test_ids']}
                preview['initial_training_count']=len(set(samples['ids'])-tested-set(req['validation']['independent_ids']))
                preview['outer_tested_count']=len(tested)
                preview['warnings'].append('İlk eğitim geçmişi dış katta sınanmaz. Sabit rasterler, belirli bir gelecek tarihin tahmin haritasını doğrulamaz.')
            write_json(output/'preflight.json',preview);products=[('folds',output/'folds.geojson'),('audit',output/'preflight.json')]
            progress('preflight',1,1,'Gözlem kimlikleri ve katlar hazırlandı.')
            if req['action']=='run':
                from .evaluation import evaluate_nested,save_trusted_model,load_trusted_model
                from .reporting import build_reports
                with threadpool_limits(limits=1):
                    ev=evaluate_nested(X,samples['y'],samples['ids'],samples['coordinates'],groups,req['covariates'],req,crs=samples['crs'],check_cancel=check_cancel,progress=progress)
                    from .diagnostics_c import subgroup_diagnostics
                    d=req['diagnostics']
                    ev['subgroup_diagnostics']=subgroup_diagnostics(ev,samples,fields=d['subgroup_fields'],coverage=req['uncertainty'].get('coverage',.9),
                        min_samples=d['min_subgroup_samples'],min_groups=d['min_subgroup_groups'],check_cancel=check_cancel)
                    check_cancel();model=ev['final_model'];record=ev['final_record']
                    model_path=output/('TabPFN-sps-model.joblib' if record.get('model',{}).get('id')=='tabpfn' else 'model.joblib')
                    model_files=save_trusted_model(model,model_path,req['run_id'],req['covariates'])
                    restored=load_trusted_model(model_path,expected_run_id=req['run_id'],covariates=req['covariates'],expected_sha256=model_files['sha256'])
                    bind_runtime_callbacks(restored,check_cancel)
                    if not np.array_equal(prediction_call(model,'predict',X,samples['coordinates']),prediction_call(restored,'predict',X,samples['coordinates'])):raise JobError('MODEL_ROUNDTRIP','Kaydedilen modelin nokta tahmini farklı.')
                    old_interval=prediction_call(model,'predict_interval',X,samples['coordinates']);new_interval=prediction_call(restored,'predict_interval',X,samples['coordinates'])
                    if old_interval is not None and any(not np.array_equal(a,b,equal_nan=True) for a,b in zip(old_interval,new_interval)):raise JobError('MODEL_ROUNDTRIP','Kaydedilen modelin aralığı farklı.')
                    products.extend([('model',model_path),('model',Path(model_files['metadata_path']))])
                    map_paths,map_audit=map_products(output,rasters,restored,record,req,samples,check_cancel,progress);products.extend(map_paths)
                    ev['mapping_diagnostics']=map_audit
                    ev['independent_test_history']=record_independent_evaluation(output,req,samples)
                    ev['audit_path']='audit.json'
                    from .study_context import build_study_context
                    ev['study_context']=build_study_context(X,samples,groups,req,ev,sources,started,model_files,check_cancel=check_cancel)
                    from .study_export import write_analysis_exports
                    data_paths,data_summary=write_analysis_exports(output,X,samples,groups,ev,req,check_cancel=check_cancel)
                    published_data=list(data_paths);products.extend(('table',p) for p in data_paths)
                    ev['study_context']['analysis_data']=data_summary
                    report_paths=build_reports(output,ev,samples,req,check_cancel=check_cancel)
                    published_reports=list(map(Path,report_paths))
                    products.extend(('report' if p.suffix in ('.html','.svg','.png') else 'table',p) for p in map(Path,report_paths))
                    from .model_bundle import create_model_package
                    package_path=create_model_package(output,model_files,req,record,ev['study_context'],check_cancel=check_cancel)
                    products.append(('model_bundle',package_path))
                result['validation']={'status':'nested_cross_validation','independent_test_count':len(req['validation']['independent_ids']),'map_accuracy_design_based':False}
            else:map_audit={};record={};result['validation']={'status':'preview_only','model_fitted':False}
            check_cancel()
            current_raster_paths=set()
            for cov in req['covariates']:
                with rasterio.open(cov['path']) as current:current_raster_paths.update(str(Path(p).resolve()) for p in current.files)
            if current_raster_paths!=set(rasters.source_paths()) or sources!=[fingerprint(p,check_cancel) for p in paths]:raise JobError('SOURCE_CHANGED','İş sırasında kaynak değişti; sonuç yayımlanmadı.')
            audit={'run_id':run_id,'started_utc':started,'finished_utc':datetime.now(timezone.utc).isoformat(),'sources':sources,'settings':req,'groups':dict(zip(samples['ids'],map(str,groups))),'folds':folds,'final_record':record,'python':sys.version,'executable':sys.executable,'platform':platform.platform(),'cpu_count':os.cpu_count(),'packages':{n:importlib.metadata.version(n) for n in ['numpy','scipy','scikit-learn','rasterio','pyproj','xgboost','optuna','quantile-forest','Boruta','matplotlib']},'source_code':[fingerprint(p) for p in sorted(Path(__file__).parent.glob('*.py'))],'proj_network_enabled':network.is_network_enabled(),'validation':result['validation'],**map_audit}
            audit['coordinate_grouping_tolerance_m']=1e-5
            audit['packages'].update({n:importlib.metadata.version(n) for n in ('lightgbm','catboost','cubist','PyKrige')})
            for name in ('torch','tabm','pytabkit','tabpfn'):
                try:audit['packages'][name]=importlib.metadata.version(name)
                except importlib.metadata.PackageNotFoundError:pass
            audit['source_code'].extend(fingerprint(p) for p in sorted(Path(__file__).parent.glob('*.R')))
            write_json(output/'audit.json',audit);products.append(('audit',output/'audit.json'))
            result['outputs']=[{'kind':k,'path':p.relative_to(output).as_posix(),'sha256':fingerprint(p,check_cancel)['sha256']} for k,p in products]
            result['status']='completed'
            if req['action']=='run':
                from .study_export import create_study_package
                study_package=create_study_package(output,result,check_cancel=check_cancel)
                result['outputs'].append({'kind':'study_package','path':study_package.name,'sha256':fingerprint(study_package,check_cancel)['sha256']})
            check_cancel();write_json(output/'result.json',result)
            (output/'INCOMPLETE.json').unlink(missing_ok=True)
            progress('completed',1,1,'Kat ön gösterimi tamamlandı.' if req['action']=='preview' else 'Dış değerlendirme ve haritalama tamamlandı.')
            return result
    except Exception as exc:
        result['status']='cancelled' if isinstance(exc,Cancelled) else 'failed';result['outputs']=[]
        result['error']={'code':exc.code if isinstance(exc,JobError) else 'WORKER_ERROR','message':str(exc)}
        if output:
            for p in published_reports+published_data:
                if p.resolve().is_relative_to(output.resolve()):
                    try:p.unlink(missing_ok=True)
                    except OSError as cleanup_error:result.setdefault('cleanup_errors',[]).append(str(cleanup_error))
            try:write_json(output/'INCOMPLETE.json',{'status':result['status'],'run_id':run_id,'error':result['error']})
            except OSError:pass
            # Only this run owns the freshly reserved directory; preserve audit logs.
            for p in output.rglob('*'):
                if p.is_file() and ('.partial' in p.name or p.suffix in ('.tif','.joblib','.geojson') or p.name.endswith(('.spsmodel.zip','.spsstudy.zip'))):
                    try:p.unlink()
                    except OSError:pass
            if not isinstance(exc,JobError):(output/'error.log').write_text(traceback.format_exc(),encoding='utf-8')
            try:write_json(output/'result.json',result);progress(result['status'],0,1,str(exc))
            except OSError as err:result['manifest_write_error']=str(err)
        return result
    finally:
        if output:
            try:(output/'.run.lock').unlink(missing_ok=True)
            except OSError:pass
