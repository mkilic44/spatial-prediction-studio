"""Local CPU neural regressors, with isolated and bounded training.

Only the training partition reaches the estimator. Epochs are selected by the
external inner folds; neither implementation creates a hidden validation set.
"""
from __future__ import annotations

import contextlib
import importlib.metadata
import json
import math
import multiprocessing
import os
from pathlib import Path
import random
import tempfile
import time
import traceback

import joblib
import numpy as np
from sklearn.base import BaseEstimator, RegressorMixin

from .contract import JobError

FIT_SECONDS = 120.0
FIT_MEMORY = 2 * 1024**3
PREDICT_MATRIX_BYTES = 256 * 1024**2
PREDICT_BATCH_SIZE = 512


def _parameters(name, params):
    if name not in ('tabm', 'realmlp'):
        raise JobError('MODEL', 'Desteklenmeyen Aşama D modeli: ' + str(name))
    if not isinstance(params, dict):
        raise JobError('MODEL_PARAMETER', 'Model parametreleri nesne olmalıdır.')
    defaults = dict(n_epochs=10, batch_size=16, hidden_size=32, n_layers=2,
                    dropout=.1 if name == 'tabm' else .15,
                    lr=.001 if name == 'tabm' else .04,
                    weight_decay=.0001 if name == 'tabm' else 0.)
    if name == 'tabm':
        defaults['k'] = 4
    unknown = set(params) - set(defaults)
    if unknown:
        raise JobError('MODEL_PARAMETER', 'Desteklenmeyen model parametresi: ' + sorted(unknown)[0])
    defaults.update(params)
    for key, low, high in [('n_epochs', 1, 200), ('batch_size', 4, 512),
                           ('hidden_size', 8, 256), ('n_layers', 1, 4), ('k', 2, 32)]:
        if key in defaults and (type(defaults[key]) is not int or not low <= defaults[key] <= high):
            raise JobError('MODEL_PARAMETER', key + ' izin verilen tamsayı aralığı dışında.')
    for key, low, high in [('dropout', 0., .8), ('lr', .000001, .2), ('weight_decay', 0., .1)]:
        value = defaults[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise JobError('MODEL_PARAMETER', key + ' izin verilen sayısal aralık dışında.')
    return defaults


def _versions():
    return {name: importlib.metadata.version(name) for name in
            ('numpy', 'scikit-learn', 'torch', 'tabm', 'pytabkit', 'pytorch-lightning', 'psutil')}


def _torch_cpu():
    # These switches precede every optional package import, including deserialization.
    os.environ['TABPFN_DISABLE_TELEMETRY'] = '1'
    os.environ['DO_NOT_TRACK'] = '1'
    os.environ['HF_HUB_OFFLINE'] = '1'
    for name in ('OMP_NUM_THREADS', 'MKL_NUM_THREADS', 'OPENBLAS_NUM_THREADS'):
        os.environ[name] = '1'
    import torch
    if torch.__version__ != '2.8.0+cpu' or torch.version.cuda is not None:
        raise JobError('D_MODEL_RUNTIME', 'Aşama D, kilitli PyTorch 2.8.0+cpu ortamını gerektirir.')
    torch.set_num_threads(1)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        if torch.get_num_interop_threads() != 1:
            raise JobError('D_MODEL_RUNTIME', 'PyTorch iş parçacığı sınırı uygulanamadı.')
    torch.use_deterministic_algorithms(True)
    return torch


def _bounded_child(target, args, *, timeout_seconds, memory_bytes, check_cancel):
    """Monitor actual owned-child working set; always reap it on every exit path."""
    import psutil
    check_cancel()
    child = multiprocessing.get_context('spawn').Process(target=target, args=args)
    started = time.monotonic()
    peak = 0
    child.start()
    process = psutil.Process(child.pid)
    tracked_descendants = {}
    try:
        while True:
            check_cancel()
            try:
                family = [process] + process.children(recursive=True)
                for item in family[1:]:
                    tracked_descendants[item.pid] = item
                total = 0
                for item in family:
                    try:
                        info = item.memory_info()
                        total += max(info.rss, getattr(info, 'peak_wset', info.rss))
                    except psutil.NoSuchProcess:
                        pass
                peak = max(peak, total)
            except psutil.NoSuchProcess:
                pass
            if peak > memory_bytes:
                raise JobError('D_MODEL_MEMORY', 'Model eğitimi için bellek sınırı aşıldı.')
            if time.monotonic() - started > timeout_seconds:
                raise JobError('D_MODEL_TIMEOUT', 'Model eğitimi için süre sınırı aşıldı.')
            if not child.is_alive():
                break
            child.join(.05)
        child.join()
        if child.exitcode != 0:
            raise JobError('D_MODEL_CHILD', 'Model eğitim süreci başarısız oldu: ' + str(child.exitcode))
        return {'duration_seconds': time.monotonic() - started, 'peak_rss_bytes': peak}
    finally:
        if child.is_alive():
            try:
                for item in process.children(recursive=True):
                    tracked_descendants[item.pid] = item
            except psutil.NoSuchProcess:
                pass
            child.kill()
            child.join(5)
        # Also reap already observed descendants if their parent exited first.
        for item in tracked_descendants.values():
            try:
                item.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(list(tracked_descendants.values()), timeout=5)
        child.close()


def _fit_in_child(name, params, seed, X, y, result_path):
    """The path is created by our parent, never taken from an external model."""
    path = Path(result_path)
    try:
        # Keep the JSONL worker protocol free from third-party progress output.
        with open(str(path) + '.log', 'w', encoding='utf-8') as log:
            with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                torch = _torch_cpu()
                random.seed(seed)
                np.random.seed(seed)
                torch.manual_seed(seed)
                batch_size = min(params['batch_size'], len(X))
                details = {'effective_batch_size': batch_size, 'validation_rows': 0,
                           'validation_policy': 'none_fixed_epochs', 'device': 'cpu', 'threads': 1,
                           'seed': seed, 'training_rows': len(X), 'encoded_features': X.shape[1],
                           'parameters': params, 'versions': _versions()}
                if name == 'tabm':
                    from tabm import TabM
                    constructor = dict(n_num_features=X.shape[1], cat_cardinalities=None,
                                       d_out=1, n_blocks=params['n_layers'], d_block=params['hidden_size'],
                                       k=params['k'], dropout=params['dropout'], arch_type='tabm')
                    model = TabM.make(**constructor)
                    center = float(np.mean(y, dtype=np.float64))
                    scale = float(np.std(y, dtype=np.float64)) or 1.
                    xt = torch.from_numpy(np.asarray(X, dtype=np.float32))
                    yt = torch.from_numpy(np.asarray((y - center) / scale, dtype=np.float32))
                    optimizer = torch.optim.AdamW(model.parameters(), lr=params['lr'], weight_decay=params['weight_decay'])
                    model.train()
                    epochs = 0
                    steps = 0
                    for epoch in range(params['n_epochs']):
                        for indices in torch.randperm(len(xt)).split(batch_size):
                            optimizer.zero_grad(set_to_none=True)
                            outputs = model(xt[indices], None).squeeze(-1)
                            loss = ((outputs - yt[indices, None]) ** 2).mean()
                            if not torch.isfinite(loss):
                                raise JobError('MODEL_NONFINITE', 'TabM eğitim kaybı sonlu değil.')
                            loss.backward()
                            optimizer.step()
                            steps += 1
                        epochs += 1
                    model.eval()
                    state = {'model': model.cpu(), 'target_center': center, 'target_scale': scale}
                    from .modeling import _parameter_value
                    details['producer_parameters'] = {'constructor': _parameter_value(constructor),
                        'estimator_class': type(model).__module__ + '.' + type(model).__qualname__,
                        'optimizer_class': type(optimizer).__module__ + '.' + type(optimizer).__qualname__,
                        'optimizer_defaults': _parameter_value(optimizer.defaults)}
                    details.update(epochs_completed=epochs, optimizer_steps=steps,
                                   target_standardization='fit_rows_only', target_center=center,
                                   target_scale=scale, architecture='tabm', numeric_embeddings='none',
                                   point_estimand='arithmetic_mean_of_k_member_predictions')
                else:
                    from pytabkit import RealMLP_TD_Regressor
                    model = RealMLP_TD_Regressor(
                        device='cpu', n_threads=1, random_state=seed, n_cv=1, n_refit=0,
                        n_repeats=1, n_ens=1, val_fraction=0., use_early_stopping=False,
                        n_epochs=params['n_epochs'], batch_size=batch_size,
                        predict_batch_size=PREDICT_BATCH_SIZE,
                        hidden_sizes=[params['hidden_size']] * params['n_layers'],
                        p_drop=params['dropout'], wd=params['weight_decay'], lr=params['lr'],
                        verbosity=0)
                    model.fit(X, y, val_idxs=np.empty(0, dtype=np.int64))
                    trained = model.alg_interface_.model
                    if trained.creator.is_cv:
                        raise JobError('D_MODEL_VALIDATION', 'RealMLP beklenmeyen doğrulama altkümesi oluşturdu.')
                    epochs = float(trained.progress.epoch_float)
                    if not math.isclose(epochs, params['n_epochs'], abs_tol=1e-6):
                        raise JobError('D_MODEL_EPOCHS', 'RealMLP istenen eğitim dönemlerini tamamlamadı.')
                    state = {'model': model}
                    from .modeling import _producer_parameters
                    details['producer_parameters'] = _producer_parameters(model, config=True)
                    details.update(epochs_completed=int(round(epochs)),
                                   training_samples_iterated=int(trained.progress.total_samples),
                                   internal_preprocessing=model.get_config().get('tfms'),
                                   output_normalization=model.get_config().get('normalize_output'),
                                   output_clamping_to_training_range=model.get_config().get('clamp_output'),
                                   learning_rate_origin='application_default_or_user_parameter',
                                   parameter_mapping={'dropout': 'p_drop', 'weight_decay': 'wd', 'hidden_size_n_layers': 'hidden_sizes'},
                                   point_estimand='single_network_regression_prediction')
                details['model'] = name
                joblib.dump({'state': state, 'diagnostics': details}, path)
    except BaseException as exc:
        Path(str(path) + '.error.json').write_text(json.dumps({
            'code': getattr(exc, 'code', 'D_MODEL_FIT'), 'message': str(exc),
            'traceback': traceback.format_exc()}, ensure_ascii=False), encoding='utf-8')


class BoundedNeuralRegressor(RegressorMixin, BaseEstimator):
    def __init__(self, name, parameters=None, seed=42):
        self.name = name
        self.parameters = parameters
        self.seed = seed

    def fit(self, X, y, *, check_cancel=lambda: None):
        started = time.monotonic()
        # Any failed refit invalidates the preceding learned state.
        for key in ('state_', 'fit_diagnostics_', 'n_features_in_'):
            self.__dict__.pop(key, None)
        params = _parameters(self.name, self.parameters or {})
        values = np.asarray(X)
        target = np.asarray(y)
        if values.ndim != 2 or not 2 <= values.shape[0] <= 2000 or not 1 <= values.shape[1] <= 256:
            raise JobError('D_MODEL_BUDGET', 'D modeli eğitimi 2-2000 satır ve 1-256 kodlanmış değişken gerektirir.')
        if target.shape != (len(values),):
            raise JobError('TARGET', 'Hedef boyutu eğitim satırlarıyla uyuşmuyor.')
        if not np.isfinite(values).all() or not np.isfinite(target).all():
            raise JobError('MODEL_NONFINITE', 'Model girdileri ve hedef sonlu olmalıdır.')
        import psutil
        if psutil.virtual_memory().available < FIT_MEMORY + 512 * 1024**2:
            raise JobError('D_MODEL_MEMORY', 'D eğitimi için en az 2.5 GiB kullanılabilir bellek gereklidir.')
        check_cancel()
        with tempfile.TemporaryDirectory(prefix='sps-d-') as folder:
            result = Path(folder) / 'model.joblib'
            observed = _bounded_child(_fit_in_child,
                (self.name, params, self.seed, np.asarray(values, dtype=np.float64),
                 np.asarray(target, dtype=np.float64), str(result)),
                timeout_seconds=FIT_SECONDS, memory_bytes=FIT_MEMORY, check_cancel=check_cancel)
            check_cancel()
            error = Path(str(result) + '.error.json')
            if error.exists():
                failure = json.loads(error.read_text(encoding='utf-8'))
                raise JobError(failure['code'], failure['message'])
            if not result.is_file():
                raise JobError('D_MODEL_CHILD', 'Eğitim süreci model dosyası üretmedi.')
            _torch_cpu()
            learned = joblib.load(result)
        self.state_ = learned['state']
        self.n_features_in_ = values.shape[1]
        self.fit_diagnostics_ = {**learned['diagnostics'], **observed}
        self.fit_diagnostics_.update(child_duration_seconds=observed['duration_seconds'],
            duration_seconds=time.monotonic() - started, time_limit_seconds=FIT_SECONDS,
            memory_limit_bytes=FIT_MEMORY, time_limit_scope='owned_training_child')
        return self

    def predict(self, X):
        if not hasattr(self, 'state_'):
            raise JobError('MODEL_NOT_FITTED', 'Tahmin öncesinde model eğitilmelidir.')
        values = np.asarray(X)
        if values.ndim != 2 or values.shape[1] != self.n_features_in_:
            raise JobError('MODEL_FEATURES', 'Tahmin değişkenlerinin boyutu eğitimle uyuşmuyor.')
        if values.size * 8 > PREDICT_MATRIX_BYTES:
            raise JobError('D_MODEL_BUDGET', 'Tahmin matrisi 256 MiB sınırını aşıyor; daha küçük raster penceresi kullanın.')
        if not np.isfinite(values).all():
            raise JobError('MODEL_NONFINITE', 'Tahmin girdileri sonlu olmalıdır.')
        if len(values) == 0:
            return np.empty(0, dtype=float)
        torch = _torch_cpu()
        model = self.state_['model']
        pieces = []
        with torch.inference_mode():
            for start in range(0, len(values), PREDICT_BATCH_SIZE):
                batch = values[start:start + PREDICT_BATCH_SIZE]
                if self.name == 'tabm':
                    model.eval()
                    pred = model(torch.from_numpy(np.asarray(batch, dtype=np.float32)), None).squeeze(-1).mean(1).numpy()
                    pred = pred.astype(np.float64) * self.state_['target_scale'] + self.state_['target_center']
                else:
                    pred = np.asarray(model.predict(batch), dtype=np.float64)
                pieces.append(pred.reshape(-1))
        prediction = np.concatenate(pieces)
        if prediction.shape != (len(values),) or not np.isfinite(prediction).all():
            raise JobError('MODEL_NONFINITE', 'Model sonlu ve doğru boyutta tahmin üretmedi.')
        return prediction


def make_estimator(name, params, seed):
    return BoundedNeuralRegressor(name, _parameters(name, params), seed)
