"""Stage D uses real local CPU models behind bounded child training."""
from pathlib import Path
import importlib
import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
import time

import joblib
import numpy as np
import pytest
from sklearn.base import clone
from sklearn.linear_model import Ridge
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import KFold
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from sps.contract import JobError
from sps.engine import Cancelled


def module():
    assert importlib.util.find_spec("sps.models_d") is not None, "Stage D estimator module is missing"
    return importlib.import_module("sps.models_d")


def data():
    rng = np.random.default_rng(42)
    X = rng.normal(size=(64, 5))
    y = 2 * X[:, 0] - X[:, 1] + .2 * rng.normal(size=64)
    return X, y


@pytest.mark.parametrize("name", ["tabm", "realmlp"])
def test_factory_contract_is_cloneable(name):
    m = module().make_estimator(name, {"n_epochs": 2}, 11)
    other = clone(m)
    assert other.get_params() == m.get_params()
    assert other.parameters["n_epochs"] == 2
    assert other.seed == 11


def test_factory_does_not_import_optional_neural_runtime():
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'backend'))
    result = subprocess.run([sys.executable, '-c',
        "import sys; from sps.models_d import make_estimator; make_estimator('tabm',{},42); assert 'torch' not in sys.modules; assert 'pytabkit' not in sys.modules"],
        env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("params", [
    {"device": "cuda"}, {"n_threads": 4}, {"n_cv": 5},
    {"use_early_stopping": True}, {"val_fraction": .2},
    {"n_epochs": 0}, {"batch_size": 3}, {"hidden_size": 257},
    {"n_layers": 5}, {"lr": 0}, {"weight_decay": .2},
])
def test_hidden_validation_and_invalid_parameters_are_rejected(params):
    with pytest.raises(JobError) as error:
        module().make_estimator("tabm", params, 42)
    assert error.value.code == "MODEL_PARAMETER"


@pytest.mark.parametrize("shape", [(2001, 1), (64, 257)])
def test_training_budget_rejects_before_child(shape):
    m = module().make_estimator("tabm", {}, 42)
    with pytest.raises(JobError) as error:
        m.fit(np.ones(shape), np.ones(shape[0]))
    assert error.value.code == "D_MODEL_BUDGET"


def child_sleep(result_path):
    time.sleep(10)


def test_owned_child_is_stopped_on_real_timeout(tmp_path):
    d = module()
    with pytest.raises(JobError) as error:
        d._bounded_child(child_sleep, (str(tmp_path / "result"),),
                         timeout_seconds=.5, memory_bytes=2 * 1024**3,
                         check_cancel=lambda: None)
    assert error.value.code == "D_MODEL_TIMEOUT"
    assert not (tmp_path / "result").exists()
    assert not multiprocessing.active_children()


def test_owned_child_is_stopped_on_cancellation(tmp_path):
    d = module()
    start = time.monotonic()
    def cancel():
        if time.monotonic() - start > .25:
            raise Cancelled()
    with pytest.raises(Cancelled):
        d._bounded_child(child_sleep, (str(tmp_path / "result"),),
                         timeout_seconds=10, memory_bytes=2 * 1024**3,
                         check_cancel=cancel)
    assert not (tmp_path / "result").exists()
    assert not multiprocessing.active_children()


def test_owned_child_is_stopped_on_actual_memory_limit(tmp_path):
    d = module()
    with pytest.raises(JobError) as error:
        d._bounded_child(child_sleep, (str(tmp_path / "result"),),
                         timeout_seconds=10, memory_bytes=1024,
                         check_cancel=lambda: None)
    assert error.value.code == "D_MODEL_MEMORY"
    assert not multiprocessing.active_children()


@pytest.mark.parametrize("name", ["tabm", "realmlp"])
def test_actual_fixed_epoch_fit_reload_and_window_predictions(tmp_path, name):
    X, y = data()
    model = module().make_estimator(name, {"n_epochs": 2}, 42).fit(X, y)
    prediction = model.predict(X)
    assert prediction.shape == (64,) and np.isfinite(prediction).all()
    assert np.std(prediction) > 1e-5
    assert model.fit_diagnostics_["training_rows"] == 64
    assert model.fit_diagnostics_["epochs_completed"] == 2
    assert model.fit_diagnostics_["validation_rows"] == 0
    assert model.fit_diagnostics_["device"] == "cpu"
    assert model.fit_diagnostics_["threads"] == 1
    assert 0 < model.fit_diagnostics_["peak_rss_bytes"] <= 2 * 1024**3
    assert model.fit_diagnostics_["duration_seconds"] < 120
    path = tmp_path / "model.joblib"
    joblib.dump(model, path)
    saved = joblib.load(path)
    np.testing.assert_array_equal(prediction, saved.predict(X))
    pieces = np.concatenate([saved.predict(X[i:i + 7]) for i in range(0, len(X), 7)])
    np.testing.assert_allclose(prediction, pieces, rtol=0, atol=1e-5)
    large = np.tile(X, (33, 1))
    complete = saved.predict(large)
    chunks = np.concatenate([saved.predict(large[i:i+511]) for i in range(0, len(large), 511)])
    np.testing.assert_allclose(complete, chunks, rtol=0, atol=1e-5)
    singles = np.concatenate([saved.predict(X[i:i+1]) for i in range(3)])
    np.testing.assert_allclose(prediction[:3], singles, rtol=0, atol=1e-5)
    assert saved.fit_diagnostics_["versions"]["torch"] == "2.8.0+cpu"
    assert saved.predict(X[:0]).shape == (0,)
    with pytest.raises(JobError, match='dimensions'):
        saved.predict(X[:, :4])
    with pytest.raises(JobError) as error:
        saved.predict(np.broadcast_to(0., (7_000_000, 5)))
    assert error.value.code == 'D_MODEL_BUDGET'
    with pytest.raises(JobError):
        saved.fit(np.ones((2001, 5)), np.ones(2001))
    with pytest.raises(JobError) as error:
        saved.predict(X)
    assert error.value.code == 'MODEL_NOT_FITTED'


@pytest.mark.parametrize("name", ["tabm", "realmlp"])
def test_actual_heldout_targets_do_not_change_fit_or_predictions(name):
    X, y = data()
    train, held = next(KFold(2, shuffle=True, random_state=42).split(X))
    other = y.copy()
    other[held] += 10000
    d = module()
    first = d.make_estimator(name, {"n_epochs": 2}, 19).fit(X[train], y[train])
    second = d.make_estimator(name, {"n_epochs": 2}, 19).fit(X[train], other[train])
    np.testing.assert_array_equal(first.predict(X[held]), second.predict(X[held]))


def test_prediction_shape_and_memory_budget_reject_invalid_inputs():
    d = module()
    m = d.make_estimator("tabm", {}, 42)
    with pytest.raises(JobError) as error:
        m.predict(np.zeros((1, 5)))
    assert error.value.code == "MODEL_NOT_FITTED"


def comparison_case(name, X_train, y_train, X_held, result_path):
    """Release each optional runtime before comparing the next model family."""
    if name == 'ridge':
        model = Ridge(alpha=1.)
    elif name == 'random_forest':
        model = RandomForestRegressor(n_estimators=40, random_state=42, n_jobs=1)
    elif name == 'tabpfn':
        from sps.tabpfn_d import make_estimator
        model = make_estimator({}, 42)
    else:
        model = module().make_estimator(name, {'n_epochs': 5}, 42)
    start = time.monotonic()
    model.fit(X_train, y_train)
    prediction = model.predict(X_held)
    assert np.isfinite(prediction).all()
    Path(result_path).write_text(json.dumps({
        'predictions': prediction.tolist(), 'fit_predict_seconds': time.monotonic() - start,
        'fit_diagnostics': getattr(model, 'fit_diagnostics_', None)}, allow_nan=False), encoding='utf-8')


def test_actual_models_and_baselines_share_identical_heldout_folds(tmp_path):
    X, y = data()
    folds = list(KFold(2, shuffle=True, random_state=71).split(X))
    rows = []
    for fold, (train, held) in enumerate(folds):
        scaler = StandardScaler().fit(X[train])
        for name in ('ridge', 'random_forest', 'tabm', 'realmlp', 'tabpfn'):
            result_path = tmp_path / ('%s-%d.json' % (name, fold))
            module()._bounded_child(comparison_case,
                (name, scaler.transform(X[train]), y[train], scaler.transform(X[held]), str(result_path)),
                timeout_seconds=180, memory_bytes=2*1024**3, check_cancel=lambda: None)
            observed = json.loads(result_path.read_text(encoding='utf-8'))
            prediction = np.asarray(observed['predictions'])
            rows.append({'model': name, 'fold': fold, 'train_rows': train.tolist(),
                         'heldout_rows': held.tolist(), 'predictions': prediction.tolist(),
                         'observed': y[held].tolist(),
                         'rmse': float(np.sqrt(np.mean((prediction - y[held]) ** 2))),
                         'fit_predict_seconds': observed['fit_predict_seconds'],
                         'fit_diagnostics': observed['fit_diagnostics']})
    evidence = {'scope': 'synthetic_software_comparison_not_spatial_or_field_validation',
                'seed': 42, 'rows': 64, 'features': 5, 'n_splits': 2,
                'tabm_realmlp_fixed_epochs': 5, 'preprocessing': 'standard_scaler_fit_on_fold_training_only',
                'execution': 'one_owned_process_per_model_fold_to_release_optional_runtimes',
                'results': rows}
    (Path(__file__).resolve().parents[1] / 'build' / 'd-model-comparison.json').write_text(
        json.dumps(evidence, indent=2), encoding='utf-8')
    assert len(rows) == 10

