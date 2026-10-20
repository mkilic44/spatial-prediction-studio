"""Explicit synthetic dated fixture and reviewable native project settings."""
from copy import deepcopy
import csv
from datetime import date,timedelta
import hashlib
import json
from pathlib import Path
import sys
import numpy as np
import rasterio
from rasterio.transform import from_origin

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from sps.contract_b import parameters,validate_request_b


def main():
    dest=ROOT/'examples-d';dest.mkdir(parents=True,exist_ok=True)
    rng=np.random.default_rng(461)
    rr,cc=np.indices((8,8));a=(cc+rr*.3)/8.;b=rng.normal(size=(8,8))
    transform=from_origin(500000,4200800,100,100)
    raster=dest/'synthetic-covariates.tif'
    with rasterio.open(raster,'w',driver='GTiff',height=8,width=8,count=2,dtype='float32',crs='EPSG:32637',transform=transform,nodata=-9999) as dst:
        dst.write(a.astype('float32'),1);dst.write(b.astype('float32'),2)
        dst.set_band_description(1,'Sentetik çevre 1');dst.set_band_description(2,'Sentetik çevre 2')
        dst.set_band_unit(1,'1');dst.set_band_unit(2,'1')
        dst.update_tags(provenance='SYNTHETIC SOFTWARE ACCEPTANCE FIXTURE; NOT OBSERVED SOIL DATA')
    sample=dest/'synthetic-dated-samples.csv';rows=[]
    for i,index in enumerate(rng.choice(64,48,replace=False)):
        r,c=divmod(int(index),8);x,y=rasterio.transform.xy(transform,r,c)
        rows.append(dict(sample_id='D%03d'%i,x=x,y=y,soil_value=8+1.2*a[r,c]-.6*b[r,c]+rng.normal(0,.1),
                         observation_date=(date(2020,1,1)+timedelta(days=3*(i//2))).isoformat(),
                         profile='profile%03d'%i,landuse='sentetik_A' if i%2 else 'sentetik_B'))
    with sample.open('w',newline='',encoding='utf-8-sig') as stream:
        w=csv.DictWriter(stream,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    project={'schema_version':2,'project_type':'SpatialPredictionStudio','input_type':'csv',
             'point_source':'','point_name':'','point_scope':None,'selected_only':False,'custom_fold_draft':[],
             'samples':{'id_field':'sample_id','target_field':'soil_value','unit':'g/kg','group_fields':[],
                        'support':'point','support_note':'','x_field':'x','y_field':'y','crs':'EPSG:32637'},
             'science':{'preprocessing':parameters('preprocessing','standard_pipeline',{}),
                        'target_transform':{'method':'none'},'feature_selection':{'method':'none'},'vif':{'mode':'off'},
                        'optimization':{'method':'manual',**parameters('optimization','manual',{}),'spaces':{}},
                        'mapping':{'align_rasters':False,'reference_id':'d_cov_1','mask_extrapolation':False,'figure_dpi':150,'max_missing_fraction':.5},
                        'diagnostics':{'applicability':False,'permutation':False,'permutation_repeats':3,
                                       'subgroup_fields':['landuse'],'min_subgroup_samples':5,'min_subgroup_groups':3}},
             'covariates':[{'id':'d_cov_%d'%band,'name':'Sentetik çevre %d'%band,'path':str(raster),'band':band,
                            'unit':'1','kind':'continuous','sampling':'nearest'} for band in (1,2)]}
    project.update(csv_path=str(sample),output_root=str(ROOT/'build/arcmap-evidence/d-runs'),window_size=256,
                   last_manifest='',preview_fingerprint='',preview_manifest='',preview_signature='',preview_sources='',
                   registry_digest=hashlib.sha256((ROOT/'backend/sps/registry.json').read_bytes()).hexdigest())
    project['samples'].update(crs='EPSG:32637',date_field='observation_date',group_fields=['profile'])
    for j,cov in enumerate(project['covariates']):
        cov.update(path=str(raster),id='d_cov_%d'%(j+1),name='Sentetik çevre %d'%(j+1),unit='1')
    science=project['science']
    science['mapping']['reference_id']='d_cov_1'
    science['diagnostics'].update(applicability=False,permutation=False,subgroup_fields=['landuse'])
    science['validation']={'method':'temporal',**parameters('validation','temporal',{'n_splits':2,'inner_splits':2,'initial_train_periods':12,'inner_initial_train_periods':4}),
                           'purpose':'within_area','independent_ids':[]}
    science['vif']={'mode':'off'}
    science['uncertainty']={'method':'spatial_conformal',**parameters('uncertainty','spatial_conformal',
                           {'coverage':.8,'calibration_fraction':.4,'spatial_bandwidth_m':300,'spatial_min_effective_n':2,'spatial_min_groups':2})}
    for name in ('tabm','realmlp','tabpfn'):
        value=deepcopy(project)
        params={'n_estimators':1} if name=='tabpfn' else {'n_epochs':2,'hidden_size':16,'n_layers':1}
        value['science']['model']={'id':name,'parameters':parameters('model',name,params)}
        if name=='tabpfn':
            value['science']['validation']={'method':'group',**parameters('validation','group',{'n_splits':2,'inner_splits':2}),
                                            'purpose':'within_area','independent_ids':[]}
            value['science']['uncertainty']={'method':'split_conformal',**parameters('uncertainty','split_conformal',{'coverage':.8,'calibration_fraction':.4})}
        req={'schema_version':2,'run_id':'d_fixture_'+name,'task':'regression','action':'preview',
             'samples':{**value['samples'],'path':str(sample)},'covariates':value['covariates'],**value['science'],
             'window_size':256,'output_dir':str(dest/('preview_'+name))}
        validate_request_b(req)
        (dest/(name+'.sps.json')).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
        (dest/(name+'.request.json')).write_text(json.dumps(req,ensure_ascii=False,indent=2),encoding='utf-8')
    (dest/'fixture-manifest.json').write_text(json.dumps({'purpose':'synthetic software acceptance only','seed':461,'sample_count':48,
        'date_periods':24,'raster_cells':64,'sources':[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in (sample,raster)]},indent=2),encoding='utf-8')
    print(dest)

if __name__=='__main__':main()
