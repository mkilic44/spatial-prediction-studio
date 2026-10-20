import csv
import json
from pathlib import Path
import sys
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from sps.contract import JobError
from sps.data_b import read_samples_b, RasterInputsB
from sps.validation import build_groups


def sample_file(tmp_path, **props):
    features=[]
    for i in range(8):
        p={'id':str(i),'y':float(i),'unit':'g/kg','top':0,'bottom':20,'method':'dry','profile':str(i//2)}
        p.update(props)
        features.append({'type':'Feature','properties':p,'geometry':{'type':'Point','coordinates':[i%4+0.5,3.5-i//4]}})
    path=tmp_path/'samples.geojson'
    path.write_text(json.dumps({'type':'FeatureCollection','crs':{'type':'name','properties':{'name':'EPSG:32637'}},'features':features}))
    return {'path':str(path),'id_field':'id','target_field':'y','unit':'g/kg','unit_field':'unit','depth_top_field':'top','depth_bottom_field':'bottom','method_field':'method','group_fields':['profile'],'support':'point'}


def raster(tmp_path, name='a', shift=0, categorical=False):
    path=tmp_path/(name+'.tif')
    data=np.arange(16,dtype='float32').reshape(4,4)
    with rasterio.open(path,'w',driver='GTiff',width=4,height=4,count=1,dtype='float32',crs='EPSG:32637',transform=from_origin(shift,4,1,1),nodata=-9999) as dst:
        dst.write(data,1)
    return {'id':name,'name':name,'path':str(path),'band':1,'kind':'categorical' if categorical else 'continuous','unit':'1','sampling':'nearest'}


def test_external_independent_file_remains_identifiable(tmp_path):
    spec=sample_file(tmp_path);external=tmp_path/'test.geojson'
    obj=json.loads(Path(spec['path']).read_text());obj['features']=obj['features'][:2]
    for f in obj['features']:f['properties']['id']='test-'+f['properties']['id']
    external.write_text(json.dumps(obj));spec['independent_path']=str(external)
    result=read_samples_b(spec)
    assert len(result['ids'])==10 and result['external_independent_ids']==['test-0','test-1']
    obj['features'][0]['properties']['id']='0';external.write_text(json.dumps(obj))
    with pytest.raises(JobError) as exc:read_samples_b(spec)
    assert exc.value.code=='DUPLICATE_ID'


def test_external_test_depth_cannot_change_target(tmp_path):
    spec=sample_file(tmp_path);external=tmp_path/'test.geojson'
    obj=json.loads(Path(spec['path']).read_text());obj['features']=obj['features'][:1]
    obj['features'][0]['properties'].update(id='held-1',bottom=30)
    external.write_text(json.dumps(obj));spec['independent_path']=str(external)
    with pytest.raises(JobError) as exc:read_samples_b(spec)
    assert exc.value.code=='TEST_TARGET_SCHEMA'


def test_properties_and_duplicate_positions_are_preserved_for_grouping(tmp_path):
    spec=sample_file(tmp_path)
    obj=json.loads(Path(spec['path']).read_text());obj['features'][1]['geometry']=obj['features'][0]['geometry']
    Path(spec['path']).write_text(json.dumps(obj))
    result=read_samples_b(spec)
    assert len(result['ids'])==8 and result['props'][0]['profile']=='0'
    assert result['duplicate_coordinate_count']==1


@pytest.mark.parametrize('field,value,code',[('unit','%', 'TARGET_UNIT_MISMATCH'),('top',-1,'DEPTH'),('bottom',0,'DEPTH'),('method',None,'METHOD')])
def test_metadata_rejects_invalid(tmp_path,field,value,code):
    with pytest.raises(JobError) as err: read_samples_b(sample_file(tmp_path,**{field:value}))
    assert err.value.code==code


def test_mixed_depth_and_method_are_rejected(tmp_path):
    spec=sample_file(tmp_path);path=Path(spec['path']);obj=json.loads(path.read_text())
    obj['features'][1]['properties']['bottom']=30;path.write_text(json.dumps(obj))
    with pytest.raises(JobError,match='derinlik'): read_samples_b(spec)


def test_missing_target_never_imputed(tmp_path):
    with pytest.raises(JobError) as err: read_samples_b(sample_file(tmp_path,y=None))
    assert err.value.code=='MISSING_TARGET'


def test_nearest_and_bilinear_sampling(tmp_path):
    cov=raster(tmp_path);samples=read_samples_b(sample_file(tmp_path))
    with RasterInputsB([cov],{},tmp_path/'work') as inp:
        X,xy=inp.extract(samples)
        np.testing.assert_array_equal(X[:,0],np.arange(8))
    cov['sampling']='bilinear';samples['coordinates']=np.array([[1.,3.],[2.,2.]])
    samples['ids']=['q','r']
    with RasterInputsB([cov],{},tmp_path/'work2') as inp:
        X,_=inp.extract(samples)
        np.testing.assert_allclose(X[:,0],[2.5,7.5])


def test_categorical_bilinear_rejected(tmp_path):
    cov=raster(tmp_path,categorical=True);cov['sampling']='bilinear'
    with pytest.raises(JobError) as err:
        with RasterInputsB([cov],{},tmp_path/'work'): pass
    assert err.value.code=='CATEGORY_RESAMPLING'


def test_alignment_requires_explicit_permission_and_preserves_sources(tmp_path):
    a=raster(tmp_path);b=raster(tmp_path,'b',0.5)
    raw=Path(b['path']).read_bytes()
    with pytest.raises(JobError):
        with RasterInputsB([a,b],{},tmp_path/'blocked'): pass
    with RasterInputsB([a,b],{'align_rasters':True},tmp_path/'aligned') as inp:
        assert inp.datasets[1].transform==inp.reference.transform
        assert inp.datasets[1].name!=b['path']
    assert Path(b['path']).read_bytes()==raw


def test_source_fingerprint_and_band_schema(tmp_path):
    a=raster(tmp_path);a['sha256']='0'*64
    with pytest.raises(JobError) as err:
        with RasterInputsB([a],{},tmp_path/'work'):pass
    assert err.value.code=='SOURCE_FINGERPRINT'


def test_parcel_uses_intersection_area_and_categorical_mode(tmp_path):
    cov=raster(tmp_path);spec=sample_file(tmp_path);samples=read_samples_b(spec)
    features=[{'type':'Feature','properties':{'id':identity},'geometry':{'type':'Polygon','coordinates':[[[0,3],[2,3],[2,4],[0,4],[0,3]]]}} for identity in samples['ids']]
    path=tmp_path/'parcels.geojson';path.write_text(json.dumps({'type':'FeatureCollection','crs':{'type':'name','properties':{'name':'EPSG:32637'}},'features':features}))
    samples.update(support='parcel_mean',polygon_path=str(path),polygon_id_field='id')
    with RasterInputsB([cov],{},tmp_path/'work') as inp:
        X,_=inp.extract(samples)
        np.testing.assert_array_equal(X[:,0],.5)
    cov['kind']='categorical'
    with RasterInputsB([cov],{},tmp_path/'work2') as inp:
        X,_=inp.extract(samples)
        np.testing.assert_array_equal(X[:,0],0.)


def test_explicit_exclusion_requires_existing_identity(tmp_path):
    spec=sample_file(tmp_path);spec['excluded']=[{'id':'0','reason':'verified transcription error'}]
    result=read_samples_b(spec);assert '0' not in result['ids'] and len(result['ids'])==7
    spec['excluded']=[{'id':'missing','reason':'bad'}]
    with pytest.raises(JobError) as err:read_samples_b(spec)
    assert err.value.code=='EXCLUDED_ID'


def independent_csv(tmp_path, spec, profiles):
    path = tmp_path / 'independent.csv'
    with path.open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['id', 'y', 'unit', 'top', 'bottom', 'method', 'profile', 'x', 'north'])
        writer.writeheader()
        for i, profile in enumerate(profiles):
            writer.writerow({'id': 'held-' + str(i), 'y': i, 'unit': 'g/kg', 'top': 0, 'bottom': 20,
                             'method': 'dry', 'profile': profile, 'x': 100 + i, 'north': 100})
    spec.update(independent_path=str(path), independent_x_field='x', independent_y_field='north', independent_crs='EPSG:32637')
    return path


def test_numeric_geojson_and_text_csv_group_schema_mismatch_is_rejected(tmp_path):
    spec = sample_file(tmp_path, profile=1)
    independent_csv(tmp_path, spec, ['1'])
    with pytest.raises(JobError) as error:
        read_samples_b(spec)
    assert error.value.code == 'GROUP_TYPE_MISMATCH'
    assert 'profile' in str(error.value)


@pytest.mark.parametrize('location', ['training', 'independent'])
def test_mixed_group_types_within_each_file_are_rejected(tmp_path, location):
    spec = sample_file(tmp_path)
    path = Path(spec['path'])
    obj = json.loads(path.read_text())
    if location == 'independent':
        path = tmp_path / 'held.geojson'
        spec['independent_path'] = str(path)
        for feature in obj['features']:
            feature['properties']['id'] = 'held-' + feature['properties']['id']
    obj['features'][1]['properties']['profile'] = 1
    path.write_text(json.dumps(obj), encoding='utf-8')
    with pytest.raises(JobError) as error:
        read_samples_b(spec)
    assert error.value.code == 'GROUP_TYPE_MISMATCH'


def test_text_group_identifiers_keep_leading_zero_and_match_across_files(tmp_path):
    spec = sample_file(tmp_path)
    path = Path(spec['path'])
    obj = json.loads(path.read_text())
    for i, feature in enumerate(obj['features']):
        feature['properties']['profile'] = '01' if i < 4 else '1'
    path.write_text(json.dumps(obj), encoding='utf-8')
    external = independent_csv(tmp_path, spec, ['01', '1'])
    before = path.read_bytes(), external.read_bytes()
    samples = read_samples_b(spec)
    assert samples['group_type_schema'] == {'profile': 'text'}
    assert [row['profile'] for row in samples['props']] == ['01'] * 4 + ['1'] * 4 + ['01', '1']
    groups = build_groups(samples['ids'], samples['props'], samples['coordinates'], spec['group_fields'])
    assert groups[0] == groups[8] and groups[4] == groups[9] and groups[0] != groups[4]
    assert (path.read_bytes(), external.read_bytes()) == before


def test_numeric_integer_and_float_group_values_share_schema_and_identity(tmp_path):
    spec = sample_file(tmp_path, profile=1)
    path = Path(spec['path'])
    obj = json.loads(path.read_text())
    obj['features'][1]['properties']['profile'] = 1.0
    path.write_text(json.dumps(obj), encoding='utf-8')
    obj['features'] = obj['features'][:1]
    obj['features'][0]['properties'].update(id='held-numeric', profile=1.0)
    obj['features'][0]['geometry']['coordinates'] = [100, 100]
    external = tmp_path / 'held_numeric.geojson'
    external.write_text(json.dumps(obj), encoding='utf-8')
    spec['independent_path'] = str(external)
    samples = read_samples_b(spec)
    assert samples['group_type_schema'] == {'profile': 'number'}
    assert type(samples['props'][0]['profile']) is int
    assert type(samples['props'][1]['profile']) is float
    assert type(samples['props'][-1]['profile']) is float
    groups = build_groups(samples['ids'], samples['props'], samples['coordinates'], spec['group_fields'])
    assert len(set(groups)) == 1


@pytest.mark.parametrize('value,code', [(None, 'GROUP_MISSING'), ('', 'GROUP_MISSING'), ('  ', 'GROUP_MISSING'),
    ({'id': 1}, 'GROUP_VALUE'), ([1], 'GROUP_VALUE'), (True, 'GROUP_VALUE'), (False, 'GROUP_VALUE'),
    (float('nan'), 'GROUP_MISSING'), (float('inf'), 'GROUP_MISSING')])
def test_selected_group_values_reject_missing_structures_booleans_and_nonfinite(tmp_path, value, code):
    with pytest.raises(JobError) as error:
        read_samples_b(sample_file(tmp_path, profile=value))
    assert error.value.code == code


def test_unselected_property_does_not_define_group_schema(tmp_path):
    spec = sample_file(tmp_path, other={'nested': True})
    independent_csv(tmp_path, spec, ['8'])
    samples = read_samples_b(spec)
    assert samples['group_type_schema'] == {'profile': 'text'}
    assert samples['props'][0]['other'] == {'nested': True}
