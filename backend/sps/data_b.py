"""Audited observation support and read-only source rasters for schema 2."""
from __future__ import annotations
from contextlib import ExitStack
import csv
from pathlib import Path
import math
import numpy as np
from pyproj import CRS, Transformer
import rasterio
from rasterio.windows import Window
from rasterio.warp import reproject, Resampling
from .contract import JobError
from .data import read_json, fingerprint


def _group_type_schema(properties, fields, path, check_cancel):
    """Validate selected source fields without coercing identifiers or values.

    CSV values remain text, including numeric-looking values and leading zeros.
    JSON integers and finite floats share the number schema. Check the complete
    source file, before explicit observation exclusions, so exclusions cannot
    silently change a field's declared-by-values type.
    """
    schema = {}
    for row_number, row in enumerate(properties, start=1):
        check_cancel()
        for field in fields:
            if not isinstance(row, dict):
                raise JobError('GROUP_VALUE', 'For group fields, observation properties must be a dictionary.')
            value = row.get(field)
            context = field + '; ' + path.name + '; row ' + str(row_number)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise JobError('GROUP_MISSING', 'Group field is empty: ' + context)
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise JobError('GROUP_VALUE', 'Group field must be a single text or number value; Boolean values are not accepted: ' + context)
            if isinstance(value, float) and not math.isfinite(value):
                raise JobError('GROUP_MISSING', 'Nonfinite value in group field: ' + context)
            kind = 'text' if isinstance(value, str) else 'number'
            if field in schema and schema[field] != kind:
                raise JobError('GROUP_TYPE_MISMATCH', 'Group field contains both numbers and text in the same file: ' + context)
            schema[field] = kind
    return schema


def _read_samples_file(spec, check_cancel=lambda:None, minimum=4):
    path=Path(spec['path'])
    if path.suffix.lower()=='.csv':
        with path.open(encoding='utf-8-sig',newline='') as f: props=list(csv.DictReader(f))
        coords=[[p.get(spec['x_field']),p.get(spec['y_field'])] for p in props]
        crs=CRS.from_user_input(spec['crs'])
    else:
        obj=read_json(path)
        if obj.get('type')!='FeatureCollection': raise JobError('SAMPLE_FORMAT','A GeoJSON FeatureCollection is required.')
        declared=obj.get('crs',{}).get('properties',{}).get('name')
        crs=CRS.from_user_input(spec.get('crs') or declared or 'EPSG:4326')
        if declared and not crs.equals(CRS.from_user_input(declared),ignore_axis_order=True):
            raise JobError('SAMPLE_CRS_CONFLICT','The sample coordinate reference systems do not match.')
        props=[];coords=[]
        for feat in obj.get('features',[]):
            geom=feat.get('geometry') or {}
            if geom.get('type')!='Point': raise JobError('SAMPLE_GEOMETRY','Point geometry is required.')
            props.append(feat.get('properties') or {});coords.append(geom.get('coordinates'))
    group_type_schema = _group_type_schema(props, spec.get('group_fields', []), path, check_cancel)
    excluded={str(e['id']):e['reason'] for e in spec.get('excluded',[])}
    ids=[];ys=[];xy=[];properties=[];seen=set();depths=set();methods=set()
    for p,c in zip(props,coords):
        check_cancel(); identity=p.get(spec['id_field'])
        if identity is None or isinstance(identity,(bool,dict,list)) or not str(identity).strip():
            raise JobError('MISSING_ID','Each observation requires a unique ID.')
        identity=str(identity).strip()
        if identity in seen: raise JobError('DUPLICATE_ID','Duplicate observation ID: '+identity)
        seen.add(identity)
        if identity in excluded: continue
        if spec.get('unit_field') and p.get(spec['unit_field'])!=spec['unit']:
            raise JobError('TARGET_UNIT_MISMATCH','The observation unit does not match the target unit: '+identity)
        try:
            y=p.get(spec['target_field'])
            if isinstance(y,bool) or y is None: raise ValueError()
            y=float(y)
            if not math.isfinite(y): raise ValueError()
        except (ValueError,TypeError): raise JobError('MISSING_TARGET','A missing target is not imputed: '+identity)
        try:
            if not isinstance(c,(list,tuple)) or len(c)<2: raise ValueError()
            pair=tuple(float(v) for v in c[:2])
            if not all(math.isfinite(v) for v in pair):raise ValueError()
        except (ValueError,TypeError): raise JobError('SAMPLE_GEOMETRY','Invalid coordinate: '+identity)
        if spec.get('depth_top_field') or spec.get('depth_bottom_field'):
            try:
                top=float(p[spec['depth_top_field']]);bottom=float(p[spec['depth_bottom_field']])
                if not (math.isfinite(top) and math.isfinite(bottom) and 0<=top<bottom):raise ValueError()
            except (KeyError,TypeError,ValueError): raise JobError('DEPTH','Invalid sampling depth: '+identity)
            depths.add((top,bottom))
        if spec.get('method_field'):
            value=p.get(spec['method_field'])
            if value is None or not str(value).strip():raise JobError('METHOD','Laboratory method is empty: '+identity)
            methods.add(str(value).strip())
        ids.append(identity);ys.append(y);xy.append(pair);properties.append(p)
    if set(excluded)-seen:raise JobError('EXCLUDED_ID','An excluded ID was not found in the dataset.')
    if len(depths)>1:raise JobError('DEPTH_MIX','Different sampling depths cannot be combined in one target.')
    if len(methods)>1:raise JobError('METHOD_MIX','Different laboratory methods cannot be combined in one target.')
    if len(ids)<minimum:raise JobError('SAMPLE_COUNT','The dataset has too few observations; fold requirements are checked separately.')
    return {'ids':ids,'y':np.asarray(ys),'coordinates':np.asarray(xy),'crs':crs,'props':properties,
            'duplicate_coordinate_count':len(xy)-len(set(xy)),'depth':list(next(iter(depths))) if depths else None,
            'laboratory_method':next(iter(methods)) if methods else None,'support':spec.get('support','point'),
            'support_note':spec.get('support_note',''),'excluded':excluded,'group_type_schema':group_type_schema,
            'polygon_path':spec.get('polygon_path'),'polygon_id_field':spec.get('polygon_id_field')}


def read_samples_b(spec, check_cancel=lambda:None):
    samples=_read_samples_file(spec,check_cancel)
    samples['external_independent_ids']=[]
    if not spec.get('independent_path'):return samples
    external={**spec,'path':spec['independent_path'],'excluded':[]}
    for key in ('x_field','y_field','crs'):
        external.pop(key,None)
        if spec.get('independent_'+key):external[key]=spec['independent_'+key]
    if Path(external['path']).suffix.lower()=='.csv' and any(not external.get(k) for k in ('x_field','y_field','crs')):
        raise JobError('INDEPENDENT_COORDINATES','The independent CSV requires coordinate fields and a coordinate reference system.')
    held=_read_samples_file(external,check_cancel,minimum=1)
    for field in spec.get('group_fields', []):
        if samples['group_type_schema'][field] != held['group_type_schema'][field]:
            labels = {'number': 'number', 'text': 'text'}
            raise JobError('GROUP_TYPE_MISMATCH', 'Group field types differ between the training and independent test files: ' + field +
                           ' (' + labels[samples['group_type_schema'][field]] + ' / ' + labels[held['group_type_schema'][field]] + ').')
    if set(samples['ids']) & set(held['ids']):
        raise JobError('DUPLICATE_ID','The training and independent test files cannot contain the same IDs.')
    if samples['depth']!=held['depth'] or samples['laboratory_method']!=held['laboratory_method']:
        raise JobError('TEST_TARGET_SCHEMA','The independent test must have the same depth and laboratory method as the training data.')
    coordinates=held['coordinates']
    if not samples['crs'].equals(held['crs'],ignore_axis_order=True):
        tx=Transformer.from_crs(held['crs'],samples['crs'],always_xy=True)
        coordinates=np.column_stack(tx.transform(*coordinates.T,errcheck=True))
    samples['ids']+=held['ids'];samples['props']+=held['props']
    samples['y']=np.concatenate([samples['y'],held['y']])
    samples['coordinates']=np.vstack([samples['coordinates'],coordinates])
    samples['duplicate_coordinate_count']=len(samples['ids'])-len(set(map(tuple,samples['coordinates'])))
    samples['external_independent_ids']=held['ids']
    return samples


class RasterInputsB:
    def __init__(self,covariates,mapping,workdir,check_cancel=lambda:None):
        self.covariates=covariates;self.mapping=mapping;self.workdir=Path(workdir)
        self.check_cancel=check_cancel;self.stack=ExitStack();self.datasets=[];self.originals=[];self.sources=[];self.alignment=[];self.bands=[]
    def __enter__(self):
        try:
            for cov in self.covariates:
                self.check_cancel();src=self.stack.enter_context(rasterio.open(cov['path']))
                if src.driver!='GTiff' or src.crs is None:raise JobError('RASTER_CRS','A GeoTIFF with a defined coordinate reference system is required.')
                b=cov['band']
                if b<1 or b>src.count:raise JobError('BAND_RANGE','Raster band not found.')
                if np.issubdtype(np.dtype(src.dtypes[b-1]),np.complexfloating):raise JobError('RASTER_TYPE','Complex-valued rasters are not supported.')
                if cov.get('kind')=='categorical' and (cov.get('sampling','nearest')!='nearest' or cov.get('resampling','nearest')!='nearest'):
                    raise JobError('CATEGORY_RESAMPLING','A categorical raster can use only the nearest cell method.')
                if 'band_description' in cov and cov['band_description']!=src.descriptions[b-1]:raise JobError('BAND_SCHEMA','The raster band description has changed.')
                if src.units[b-1] and src.units[b-1]!=cov['unit']:raise JobError('COVARIATE_UNIT','The raster unit does not match the variable schema.')
                digest=fingerprint(cov['path'],self.check_cancel)
                if cov.get('sha256') and cov['sha256']!=digest['sha256']:raise JobError('SOURCE_FINGERPRINT','The raster content differs from the recorded schema.')
                self.originals.append(src)
            reference_id=self.mapping.get('reference_id') or self.covariates[0]['id']
            ref_index=next((i for i,c in enumerate(self.covariates) if c['id']==reference_id),None)
            if ref_index is None:raise JobError('REFERENCE_GRID','The reference grid variable is not selected.')
            self.reference=self.originals[ref_index]
            for i,(cov,src) in enumerate(zip(self.covariates,self.originals)):
                ref=self.reference;b=cov['band'];same=(src.width,src.height,src.crs)==(ref.width,ref.height,ref.crs) and np.allclose(src.transform,ref.transform,rtol=0,atol=1e-9)
                scaled=src.scales[b-1]!=1 or src.offsets[b-1]!=0
                if not same and not self.mapping.get('align_rasters',False):raise JobError('GRID_MISMATCH','The grids differ. Alignment requires the working copy option.')
                if not same or scaled:
                    self.workdir.mkdir(parents=True,exist_ok=True);dstpath=self.workdir/('aligned_'+str(i)+'.tif')
                    profile=dict(driver='GTiff',width=ref.width,height=ref.height,count=1,dtype='float64',crs=ref.crs,transform=ref.transform,nodata=float('nan'),compress='deflate',tiled=True)
                    with rasterio.open(dstpath,'w',**profile) as dst:
                        reproject(rasterio.band(src,b),rasterio.band(dst,1),src_nodata=src.nodata,dst_nodata=float('nan'),resampling=getattr(Resampling,cov.get('resampling','nearest')),warp_mem_limit=64,num_threads=1)
                    if scaled:
                        with rasterio.open(dstpath,'r+') as dst:
                            for _,w in dst.block_windows(1):
                                self.check_cancel();arr=dst.read(1,window=w);dst.write(arr*src.scales[b-1]+src.offsets[b-1],1,window=w)
                    effective=self.stack.enter_context(rasterio.open(dstpath));effective_band=1
                    self.alignment.append({'id':cov['id'],'source':cov['path'],'copy':str(dstpath),'resampling':cov.get('resampling','nearest'),'scale':src.scales[b-1],'offset':src.offsets[b-1]})
                else: effective=src;effective_band=b
                self.datasets.append(effective)
                self.bands.append(effective_band)
            self.sources=sorted({str(Path(p).resolve()) for src in self.originals for p in src.files})
            return self
        except Exception:
            self.stack.close();raise
    def __exit__(self,*args):
        return self.stack.__exit__(*args)
    def source_paths(self):return self.sources
    def extract(self,samples,check_cancel=None):
        check_cancel=check_cancel or self.check_cancel
        tx=Transformer.from_crs(samples['crs'],self.reference.crs,always_xy=True)
        xx,yy=tx.transform(*samples['coordinates'].T,errcheck=True);coords=np.column_stack([xx,yy]);columns=[]
        if samples.get('support')=='parcel_mean':
            return self._extract_polygons(samples,check_cancel),coords
        for cov,src,b in zip(self.covariates,self.datasets,self.bands):
            values=[]
            for identity,(x,y) in zip(samples['ids'],coords):
                check_cancel();r,c=src.index(x,y)
                if not(0<=r<src.height and 0<=c<src.width):raise JobError('SAMPLE_OUTSIDE','Observation outside the raster: '+identity)
                if cov.get('sampling','nearest')=='bilinear':
                    fc,fr=(~src.transform)@(x,y);fc-=.5;fr-=.5;c0=math.floor(fc);r0=math.floor(fr)
                    if c0<0 or r0<0 or c0+1>=src.width or r0+1>=src.height:raise JobError('BILINEAR_EDGE','Missing neighbor at the raster edge for bilinear interpolation: '+identity)
                    a=src.read(b,window=Window(c0,r0,2,2),masked=True);dx=fc-c0;dy=fr-r0
                    w=np.array([[(1-dx)*(1-dy),dx*(1-dy)],[(1-dx)*dy,dx*dy]])
                    value=np.nan if (np.ma.getmaskarray(a)&(w>1e-12)).any() else float((a.data*w).sum())
                else:
                    a=src.read(b,window=Window(c,r,1,1),masked=True)
                    value=float(a[0,0]) if not np.ma.getmaskarray(a)[0,0] else np.nan
                if not np.isfinite(value):value=np.nan
                if cov['kind']=='categorical' and np.isfinite(value) and value!=round(value):raise JobError('CATEGORY_CODE','Categorical raster values must be integer codes.')
                values.append(value)
            columns.append(values)
        return np.asarray(columns,dtype=float).T,coords
    def _extract_polygons(self,samples,check_cancel):
        from shapely.geometry import shape,Polygon
        from shapely.ops import transform
        if not self.reference.crs.is_projected:raise JobError('SUPPORT_CRS','A projected raster grid is required for parcel summaries.')
        obj=read_json(samples['polygon_path'])
        if obj.get('type')!='FeatureCollection':raise JobError('SUPPORT_GEOMETRY','Parcels must be a GeoJSON FeatureCollection.')
        source_crs=obj.get('crs',{}).get('properties',{}).get('name') or 'EPSG:4326'
        tx=Transformer.from_crs(source_crs,self.reference.crs,always_xy=True);polygons={}
        for feature in obj['features']:
            identity=str(feature.get('properties',{}).get(samples['polygon_id_field'],''))
            if not identity or identity in polygons:raise JobError('SUPPORT_ID','A parcel ID is missing or duplicated.')
            geom=transform(tx.transform,shape(feature['geometry']))
            if geom.geom_type not in ('Polygon','MultiPolygon') or not geom.is_valid or geom.area<=0:raise JobError('SUPPORT_GEOMETRY','Invalid parcel geometry: '+identity)
            polygons[identity]=geom
        rows=[]
        for identity in samples['ids']:
            check_cancel()
            if identity not in polygons:raise JobError('SUPPORT_ID','Parcel geometry not found for observation: '+identity)
            poly=polygons[identity];minx,miny,maxx,maxy=poly.bounds
            inverse=~self.reference.transform
            corners=[inverse@(x,y) for x,y in [(minx,miny),(minx,maxy),(maxx,miny),(maxx,maxy)]]
            c0=max(0,math.floor(min(p[0] for p in corners)));c1=min(self.reference.width,math.ceil(max(p[0] for p in corners)))
            r0=max(0,math.floor(min(p[1] for p in corners)));r1=min(self.reference.height,math.ceil(max(p[1] for p in corners)))
            if (c1-c0)*(r1-r0)>1_000_000:raise JobError('SUPPORT_MEMORY','A single parcel summary exceeds one million cells.')
            if c1<=c0 or r1<=r0:raise JobError('SAMPLE_OUTSIDE','Parcel outside the raster: '+identity)
            w=Window(c0,r0,c1-c0,r1-r0);weights=np.zeros((r1-r0,c1-c0))
            for r in range(r0,r1):
                check_cancel()
                for c in range(c0,c1):
                    cell=Polygon([self.reference.transform@(cx,cy) for cx,cy in [(c,r),(c+1,r),(c+1,r+1),(c,r+1)]])
                    weights[r-r0,c-c0]=poly.intersection(cell).area
            if not math.isclose(float(weights.sum()),poly.area,rel_tol=1e-6,abs_tol=1e-6):raise JobError('SUPPORT_OUTSIDE','The parcel is not entirely within the raster grid: '+identity)
            values=[]
            for cov,src,b in zip(self.covariates,self.datasets,self.bands):
                arr=src.read(b,window=w,masked=True);valid=(weights>0)&~np.ma.getmaskarray(arr)&np.isfinite(arr.data)
                if not valid.any():values.append(np.nan);continue
                if not np.isclose(weights[valid].sum(),weights.sum(),rtol=1e-6):raise JobError('SUPPORT_NODATA','Missing raster cell within parcel: '+identity)
                if cov['kind']=='categorical':
                    unique=np.unique(arr.data[valid]);values.append(float(max(unique,key=lambda v:weights[valid&(arr.data==v)].sum())))
                else:values.append(float(np.sum(arr.data[valid]*weights[valid])/weights[valid].sum()))
            rows.append(values)
        return np.asarray(rows,dtype=float)
    def read_window(self,window):
        arrays=[src.read(b,window=window,masked=True,out_dtype='float64') for src,b in zip(self.datasets,self.bands)]
        valid=np.ones(arrays[0].shape,dtype=bool)
        for arr in arrays:valid &= ~np.ma.getmaskarray(arr)&np.isfinite(arr.data)
        return np.stack([a.data for a in arrays],axis=-1),valid
