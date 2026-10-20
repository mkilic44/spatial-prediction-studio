"""Offline, pinned TabPFN adapter, with actual CPU acceptance tests."""
from pathlib import Path
import importlib
import importlib.util
import hashlib
import json
import sys

import joblib
import numpy as np
import pytest
from sklearn.base import clone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from sps.contract import JobError


def module():
    return importlib.import_module('sps.tabpfn_d')


def available():
    return importlib.util.find_spec('tabpfn') is not None


def data():
    rng = np.random.default_rng(461)
    X = rng.normal(size=(64, 5))
    y = 2 * X[:, 0] - X[:, 1] + .15 * rng.normal(size=64)
    return X, y


def test_cloneable_factory_does_not_load_optional_model():
    before = 'tabpfn' in sys.modules
    m = module().make_estimator({}, 19)
    assert clone(m).get_params() == m.get_params()
    assert ('tabpfn' in sys.modules) == before


@pytest.mark.parametrize('params', [
    {'device': 'cuda'}, {'n_jobs': 2}, {'model_path': 'other.ckpt'},
    {'n_estimators': 0}, {'n_estimators': 5}, {'n_estimators': True},
    {'softmax_temperature': 0}, {'softmax_temperature': float('nan')},
    {'ignore_pretraining_limits': True}, {'fit_mode': 'low_memory'},
])
def test_unsupported_or_invalid_parameters_rejected(params):
    with pytest.raises(JobError, match='parametre'):
        module().make_estimator(params, 42)


@pytest.mark.parametrize('shape', [(2001, 1), (64, 257), (1, 1)])
def test_budget_rejects_before_process_or_import(shape):
    with pytest.raises(JobError) as failure:
        module().make_estimator({}, 42).fit(np.ones(shape), np.ones(shape[0]))
    assert failure.value.code == 'D_MODEL_BUDGET'


def test_approved_weight_and_license_readback():
    record = module().verify_weights()
    assert record['sha256'] == module().WEIGHT_SHA256
    assert record['bytes'] == 44390977
    assert record['revision'] == module().WEIGHT_REVISION
    assert record['license'] == 'Prior Labs License 1.1'


def test_external_models_directory_is_used_without_fallback(monkeypatch, tmp_path):
    d = module()
    root = Path(__file__).resolve().parents[1] / 'models'
    monkeypatch.setenv('SPS_MODELS_DIR', str(root))
    assert Path(d.verify_weights()['path']) == root / 'tabpfn-v2' / d.WEIGHT_NAME
    monkeypatch.setenv('SPS_MODELS_DIR', str(tmp_path))
    with pytest.raises(JobError) as missing:
        d.verify_weights()
    assert missing.value.code == 'TABPFN_WEIGHTS'
    monkeypatch.setenv('SPS_MODELS_DIR', 'relative/models')
    with pytest.raises(JobError) as relative:
        d.verify_weights()
    assert relative.value.code == 'TABPFN_WEIGHTS'


def test_missing_and_modified_weight_fail_before_import_or_download(tmp_path):
    d = module()
    with pytest.raises(JobError) as missing:
        d.verify_weights(tmp_path)
    assert missing.value.code == 'TABPFN_WEIGHTS'
    (tmp_path / d.WEIGHT_NAME).write_bytes(b'changed')
    (tmp_path / 'weights-manifest.json').write_text('{}')
    (tmp_path / 'LICENSE.txt').write_text('not the approved license')
    with pytest.raises(JobError) as modified:
        d.verify_weights(tmp_path)
    assert modified.value.code == 'TABPFN_WEIGHTS'
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([d.WEIGHT_NAME, 'weights-manifest.json', 'LICENSE.txt'])


def test_offline_guard_blocks_a_real_socket_connection_without_contact():
    import socket
    with module().offline_network():
        assert __import__('os').environ['TABPFN_DISABLE_TELEMETRY'] == '1'
        with socket.socket() as connection, pytest.raises(OSError, match='kapalı'):
            connection.connect(('127.0.0.1', 9))
        with pytest.raises(OSError, match='kapalı'):
            socket.getaddrinfo('example.com', 443)
        with socket.socket(type=socket.SOCK_DGRAM) as connection, pytest.raises(OSError, match='kapalı'):
            connection.sendto(b'never sent', ('127.0.0.1', 9))


def test_predict_requires_successful_fit():
    with pytest.raises(JobError) as failure:
        module().make_estimator({}, 42).predict(np.ones((3, 2)))
    assert failure.value.code == 'MODEL_NOT_FITTED'


def test_same_matrix_reuses_one_process_but_rechecks_weights_and_cancel(monkeypatch, tmp_path):
    d = module()
    resource = importlib.import_module('sps.models_d')
    calls, checks, cancels = [], [], []
    def verify():
        checks.append(1)
        return {'path': str(tmp_path / d.WEIGHT_NAME)}
    def bounded(target, args, **kwargs):
        calls.append(args[0])
        joblib.dump({'prediction': args[3][:, 0].copy()}, args[6])
        return {'duration_seconds': .1, 'peak_rss_bytes': 1000}
    monkeypatch.setattr(d, 'verify_weights', verify)
    monkeypatch.setattr(resource, '_bounded_child', bounded)
    model = d.make_estimator({}, 42)
    model.payload_ = b'locally-created-test-state'
    model.payload_sha256_ = hashlib.sha256(model.payload_).hexdigest()
    model.n_features_in_ = 2
    callback = lambda: cancels.append(1)
    X = np.arange(12, dtype=float).reshape(6, 2)
    first = model.predict(X, check_cancel=callback)
    first[0] = -999
    np.testing.assert_array_equal(model.predict(X.copy(), check_cancel=callback), X[:, 0])
    assert len(calls) == 1 and len(checks) == 2 and len(cancels) >= 2
    changed = X.copy(); changed[0, 0] = 77
    np.testing.assert_array_equal(model.predict(changed), changed[:, 0])
    assert len(calls) == 2
    path = tmp_path / 'owned-test.joblib'
    joblib.dump(model, path)
    restored = joblib.load(path)
    assert not hasattr(restored, '_prediction_cache_')
    np.testing.assert_array_equal(restored.predict(changed), changed[:, 0])
    assert len(calls) == 3
    from sps.contract import Cancelled
    def cancelled():
        raise Cancelled()
    with pytest.raises(Cancelled):
        model.predict(changed, check_cancel=cancelled)
    assert len(calls) == 3
    with pytest.raises(JobError):
        model.fit(np.ones((1, 2)), np.ones(1))
    assert not hasattr(model, '_prediction_cache_') and not hasattr(model, 'payload_')


@pytest.mark.skipif(not available(), reason='Dedicated D runtime is required for real CPU acceptance')
def test_actual_fit_reload_and_prediction_window_equivalence(tmp_path):
    d = module()
    X, y = data()
    model = d.make_estimator({'n_estimators': 2}, 19).fit(X, y)
    prediction = model.predict(X[:16])
    assert prediction.shape == (16,) and np.isfinite(prediction).all()
    assert np.std(prediction) > 1e-5
    fit = model.fit_diagnostics_
    assert fit['training_rows'] == 64 and fit['validation_rows'] == 0
    assert fit['device'] == 'cpu' and fit['threads'] == 1
    assert fit['versions']['tabpfn'] == '2.2.1'
    assert fit['weight']['sha256'] == d.WEIGHT_SHA256
    assert 0 < fit['peak_rss_bytes'] <= 2 * 1024**3
    assert fit['duration_seconds'] < 120
    path = tmp_path / 'TabPFN-sps-model.joblib'
    joblib.dump(model, path)
    restored = joblib.load(path)
    np.testing.assert_array_equal(prediction, restored.predict(X[:16]))
    pieces = np.concatenate([restored.predict(X[:7]), restored.predict(X[7:16])])
    np.testing.assert_allclose(prediction, pieces, rtol=0, atol=1e-5)
    assert restored.prediction_diagnostics_['duration_seconds'] < 120
    assert restored.prediction_diagnostics_['peak_rss_bytes'] <= 2 * 1024**3


@pytest.mark.skipif(not available(), reason='Dedicated D runtime is required for real CPU acceptance')
def test_actual_heldout_target_intervention_does_not_change_fit():
    X, y = data()
    train, held = np.arange(32), np.arange(32, 40)
    changed = y.copy()
    changed[held] += 10000
    d = module()
    first = d.make_estimator({'n_estimators': 1}, 42).fit(X[train], y[train])
    second = d.make_estimator({'n_estimators': 1}, 42).fit(X[train], changed[train])
    np.testing.assert_array_equal(first.predict(X[held]), second.predict(X[held]))
