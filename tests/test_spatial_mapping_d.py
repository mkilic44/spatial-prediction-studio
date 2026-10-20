"""Absent local interval support must never erase valid point predictions."""
import sys
from pathlib import Path
import numpy as np
import pytest
import rasterio
from pyproj import CRS
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from sps.contract import JobError
from sps.engine_b import map_products
from test_applicability_mapping_c import SmallRasterInputs, SmallModel

class LocalSupportModel(SmallModel):
    def predict_interval(self, X):
        low,high=super().predict_interval(X)
        low[X[:,0]==1]=np.nan;high[X[:,0]==1]=np.nan
        return low,high
    def diagnostics(self,X):
        return {**super().diagnostics(X),'interval_available':X[:,0]!=1,
                'interval_effective_n':np.where(X[:,0]==1,2.,10.),
                'interval_effective_group_n':np.where(X[:,0]==1,1.,5.)}

def run_mapping(tmp_path,size,model=None):
    tmp_path.mkdir(parents=True,exist_ok=True)
    rasters=SmallRasterInputs()
    samples={'ids':['s1'],'coordinates':np.array([rasters.reference.transform*(.5,.5)]),
             'crs':CRS.from_epsg(32637),'depth':[0,20],'support':'point'}
    req={'run_id':'d_local_support','window_size':size,'samples':{'unit':'g/kg','target_field':'value'},
         'covariates':[{'id':'x'}],'model':{'id':'ridge'},'validation':{},
         'uncertainty':{'method':'spatial_conformal','coverage':.8},
         'diagnostics':{},'mapping':{'mask_extrapolation':False}}
    paths,audit=map_products(tmp_path,rasters,model or LocalSupportModel(),{'development_ids':['s1']},req,samples,lambda:None,lambda *a:None)
    arrays={}
    for kind,path in paths:
        with rasterio.open(path) as f:arrays[kind]=f.read(1)
    return arrays,audit

def test_insufficient_local_support_is_explicit_and_window_invariant(tmp_path):
    first,record=run_mapping(tmp_path/'one',1)
    other,other_record=run_mapping(tmp_path/'whole',256)
    assert first['prediction'][0,1]==101
    assert first['interval_available'][0,1]==0
    for kind in ('lower','upper','width'):
        assert np.isnan(first[kind][0,1])
        assert np.isfinite(first[kind][0,0])
    assert record['interval_support']['available_input_pixels']==6
    assert record['interval_support']['unavailable_input_pixels']==1
    assert record['interval_support']['valid_input_pixels']==7
    assert record==other_record
    for kind in first:np.testing.assert_array_equal(first[kind],other[kind])

@pytest.mark.parametrize('failure',['nan_with_available','finite_with_unavailable','infinity'])
def test_invalid_interval_is_not_mislabeled_as_absent_support(tmp_path,failure):
    class Broken(LocalSupportModel):
        def predict_interval(self,X):
            low,high=super().predict_interval(X)
            if failure=='finite_with_unavailable':low[X[:,0]==1]=1;high[X[:,0]==1]=2
            if failure=='nan_with_available':low[X[:,0]==0]=np.nan;high[X[:,0]==0]=np.nan
            if failure=='infinity':high[X[:,0]==1]=np.inf
            return low,high
    with pytest.raises(JobError) as error:run_mapping(tmp_path/'bad',256,Broken())
    assert error.value.code=='INTERVAL_SUPPORT'
