import copy
from pathlib import Path
import numpy as np
import pytest
from pyproj import Transformer
from test_engine_b import request
from sps.contract import JobError
from sps.contract_b import validate_request_b
from sps.data_b import read_samples_b
from sps.data import fingerprint
from sps.validation import build_groups,make_folds
from sps.engine_b import preview_signature

def test_projection_roundtrip_cannot_disguise_independent_duplicate():
    original=np.array([37.123,38.234])
    x,y=Transformer.from_crs(4326,32637,always_xy=True).transform(*original)
    returned=np.array(Transformer.from_crs(32637,4326,always_xy=True).transform(x,y))
    xy=np.vstack([original,returned,[37.13,38.23],[37.14,38.24],[37.15,38.25],[37.16,38.26]])
    ids=['train','held','c','d','e','f'];groups=build_groups(ids,[{}]*6,xy,[],crs='EPSG:4326')
    assert groups[0]==groups[1]
    np.testing.assert_array_equal(xy[1],returned)
    with pytest.raises(JobError) as exc:make_folds(ids,xy,groups,{'method':'random','n_splits':2,'independent_ids':['held']},crs='EPSG:4326')
    assert exc.value.code=='INDEPENDENT_GROUP_LEAKAGE'

def test_sidecar_change_or_addition_invalidates_preview(tmp_path):
    req=validate_request_b(request(tmp_path));samples=read_samples_b(req['samples'])
    records=[fingerprint(p) for p in {req['samples']['path'],req['covariates'][0]['path']}]
    first=preview_signature(req,samples,req['covariates'],records)
    sidecar={'path':req['covariates'][0]['path']+'.msk','sha256':'first-mask'}
    second=preview_signature(req,samples,req['covariates'],records+[sidecar])
    third=preview_signature(req,samples,req['covariates'],records+[{**sidecar,'sha256':'second-mask'}])
    assert len({first,second,third})==3
    assert third==preview_signature(req,samples,req['covariates'],list(reversed(records))+[{**sidecar,'sha256':'second-mask'}])
