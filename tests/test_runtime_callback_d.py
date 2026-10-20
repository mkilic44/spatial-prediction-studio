"""Deserialized TabPFN predictions retain cooperative cancellation."""
from pathlib import Path
import sys
from types import SimpleNamespace
import joblib
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from sps.contract import Cancelled
from sps.modeling import bind_runtime_callbacks
from sps.tabpfn_d import make_estimator


def test_restored_wrapped_tabpfn_checks_current_cancellation(tmp_path,monkeypatch):
    import sps.tabpfn_d as module
    model=make_estimator({},42)
    model.payload_=b'own fixture';model.payload_sha256_=module.hashlib.sha256(model.payload_).hexdigest()
    model.n_features_in_=1
    model._check_cancel_=lambda:None
    wrapped=SimpleNamespace(record={'model':{'id':'tabpfn'}},base=SimpleNamespace(base=SimpleNamespace(estimator=model)))
    path=tmp_path/'owned-fixture.joblib';joblib.dump(wrapped,path)
    restored=joblib.load(path)
    assert not hasattr(restored.base.base.estimator,'_check_cancel_')
    called=[]
    def cancel():called.append(True);raise Cancelled()
    bind_runtime_callbacks(restored,cancel)
    monkeypatch.setattr(module,'verify_weights',lambda *a,**k:pytest.fail('Cancellation must precede weight access and process start'))
    with pytest.raises(Cancelled):restored.base.base.estimator.predict(np.ones((2,1)))
    assert called==[True]
