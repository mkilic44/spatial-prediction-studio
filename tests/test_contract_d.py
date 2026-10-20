"""Experimental models keep the public scientific safety restrictions."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from sps.contract import JobError
from sps.contract_b import validate_request_b
from test_engine_b import request

@pytest.mark.parametrize('model',['tabm','realmlp','tabpfn'])
@pytest.mark.parametrize('selector',['sfs','rfe'])
def test_unaccepted_expensive_or_coefficient_selectors_rejected(tmp_path,model,selector):
    req=request(tmp_path)
    req['model']={'id':model,'parameters':{}}
    req['feature_selection']={'method':selector}
    with pytest.raises(JobError) as error:
        validate_request_b(req)
    assert error.value.code=='SELECTOR_MODEL'

def test_spatial_interval_kriging_contract(tmp_path):
    req=request(tmp_path)
    req['model']={'id':'regression_kriging','parameters':{}}
    req['uncertainty']={'method':'spatial_conformal'}
    req['validation']['metric_crs']='EPSG:32637'
    parsed=validate_request_b(req)
    assert parsed['uncertainty']['spatial_bandwidth_m']==1000
    req['target_transform']={'method':'log'}
    with pytest.raises(JobError) as error:
        validate_request_b(req)
    assert error.value.code=='KRIGING_COMPATIBILITY'

def test_tabpfn_approved_fixed_local_configuration(tmp_path):
    req=request(tmp_path)
    req['model']={'id':'tabpfn','parameters':{}}
    parsed=validate_request_b(req)
    assert parsed['model']['parameters']=={'n_estimators':2,'softmax_temperature':.9}
    req['model']['parameters']['model_path']='outside.ckpt'
    with pytest.raises(JobError):validate_request_b(req)

def test_temporal_contract_and_internal_dates_cannot_be_injected(tmp_path):
    req=request(tmp_path);req['validation']={'method':'temporal'}
    with pytest.raises(JobError) as e:validate_request_b(req)
    assert e.value.code=='TEMPORAL_DATE'
    req['samples']['date_field']='date'
    assert validate_request_b(req)['validation']['initial_train_periods']==12
    req['diagnostics']={'applicability':True}
    with pytest.raises(JobError) as e:validate_request_b(req)
    assert e.value.code=='TEMPORAL_APPLICABILITY_UNSUPPORTED'
    req['diagnostics']={};req['validation']['dates_by_id']={}
    with pytest.raises(JobError) as e:validate_request_b(req)
    assert e.value.code=='UNKNOWN_SETTING'
