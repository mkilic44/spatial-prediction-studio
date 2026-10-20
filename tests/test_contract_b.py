import copy,sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from sps.contract import JobError
from sps.contract_b import validate_request_b
from test_engine_b import request

def test_defaults_and_blank_metric_crs(tmp_path):
    req=validate_request_b(request(tmp_path))
    assert req['preprocessing']['scaling']=='standard'
    assert req['vif']['mode']=='diagnostic'
    assert 'metric_crs' not in req['validation']

@pytest.mark.parametrize('category,value,code',[
 ('optimization',{'method':'optuna','n_jobs':2},'SETTING_VALUE'),
 ('model',{'id':'not_a_model','parameters':{}},'UNSUPPORTED_METHOD'),
 ('model',{'id':'ridge','parameters':{'early_stopping':True}},'UNKNOWN_SETTING'),
 ('uncertainty',{'method':'quantile_forest'},'UNCERTAINTY_MODEL'),
 ('uncertainty',{'method':'split_conformal','coverage':1.1},'SETTING_VALUE'),
 ('mapping',{'physical_min':1,'physical_max':0},'PHYSICAL_RANGE'),
 ('feature_selection',{'method':'invented'},'UNSUPPORTED_METHOD'),
])
def test_bad_settings_fail_closed(tmp_path,category,value,code):
    req=request(tmp_path);req[category]=value
    with pytest.raises(JobError) as exc:validate_request_b(req)
    assert exc.value.code==code

def test_unknown_sample_metadata_not_silently_ignored(tmp_path):
    req=request(tmp_path);req['samples']['fake_support']='a'
    with pytest.raises(JobError):validate_request_b(req)

def test_category_and_target_derived_rejected(tmp_path):
    req=request(tmp_path);req['covariates'][0].update(kind='categorical',sampling='bilinear')
    with pytest.raises(JobError) as exc:validate_request_b(req)
    assert exc.value.code=='CATEGORY_RESAMPLING'
    req=request(tmp_path);req['covariates'][0]['target_derived']=True
    with pytest.raises(JobError) as exc:validate_request_b(req)
    assert exc.value.code=='TARGET_LEAKAGE'

def test_model_family_quantile_compatibility(tmp_path):
    req=request(tmp_path);req['model']={'id':'random_forest','parameters':{}}
    req['uncertainty']={'method':'cqr','quantile_model':'quantile_forest'}
    assert validate_request_b(req)['model']['id']=='random_forest'
