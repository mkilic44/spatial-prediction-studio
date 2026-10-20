"""Pinned TabPFN v2 regression, offline and bounded on CPU.

Foundation weights are verified before optional imports. Each fit and prediction
uses an owned, monitored child; no foundation model is imported into the worker.
The serialized estimator is created locally by this module, never accepted as
an external model input. Automatic model acquisition is not supported.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import random
import socket
import tempfile
import traceback
from unittest.mock import patch

import joblib
import numpy as np
from sklearn.base import BaseEstimator, RegressorMixin

from .contract import JobError

PACKAGE_VERSION = '2.2.1'
WEIGHT_REVISION = '4972a65a1b30806315c6f92499959ffbfc69a673'
WEIGHT_NAME = 'tabpfn-v2-regressor.ckpt'
WEIGHT_SHA256 = '2ab5a07d5c41dfe6db9aa7ae106fc6de898326c2765be66505a07e2868c10736'
WEIGHT_BYTES = 44390977
FIT_SECONDS = 120.0
FIT_MEMORY = 2 * 1024**3
PREDICT_MATRIX_BYTES = 256 * 1024**2
PREDICT_BATCH_SIZE = 128


def _digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def verify_weights(directory=None):
    if directory is None:
        models = Path(os.environ.get('SPS_MODELS_DIR', str(Path(__file__).resolve().parents[2] / 'models')))
        if not models.is_absolute():
            raise JobError('TABPFN_WEIGHTS', 'SPS_MODELS_DIR tam yerel dizin yolu olmalıdır.')
        directory = models / 'tabpfn-v2'
    directory = Path(directory)
    weight = directory / WEIGHT_NAME
    manifest = directory / 'weights-manifest.json'
    license_path = directory / 'LICENSE.txt'
    try:
        if not weight.is_file() or not manifest.is_file() or not license_path.is_file():
            raise ValueError('Dosya, lisans veya kayıt bulunamadı.')
        record = json.loads(manifest.read_text(encoding='utf-8'))
        if not isinstance(record, dict):
            raise ValueError('Ağırlık kaydı nesne olmalıdır.')
        expected = {'package_version': PACKAGE_VERSION, 'revision': WEIGHT_REVISION,
                    'filename': WEIGHT_NAME, 'bytes': WEIGHT_BYTES, 'sha256': WEIGHT_SHA256,
                    'license': 'Prior Labs License 1.1'}
        if any(record.get(key) != value for key, value in expected.items()):
            raise ValueError('Ağırlık kaydı onaylanan sürümle eşleşmiyor.')
        if weight.stat().st_size != WEIGHT_BYTES or _digest(weight) != WEIGHT_SHA256:
            raise ValueError('Ağırlık boyutu veya dosya özeti değişmiş.')
        if _digest(license_path) != record.get('license_sha256'):
            raise ValueError('Lisans dosyası özeti değişmiş.')
        return {**expected, 'path': str(weight.resolve()), 'license_sha256': record['license_sha256'],
                'required_attribution': 'Built with PriorLabs-TabPFN'}
    except (OSError, ValueError, TypeError) as exc:
        raise JobError('TABPFN_WEIGHTS', 'Yerel TabPFN ağırlığı doğrulanamadı; otomatik indirme yapılmaz. ' + str(exc)) from exc


def _parameters(parameters):
    if not isinstance(parameters, dict):
        raise JobError('MODEL_PARAMETER', 'TabPFN parametreleri nesne olmalıdır.')
    defaults = {'n_estimators': 2, 'softmax_temperature': .9}
    if set(parameters) - set(defaults):
        raise JobError('MODEL_PARAMETER', 'Desteklenmeyen TabPFN parametresi: ' + sorted(set(parameters) - set(defaults))[0])
    defaults.update(parameters)
    if type(defaults['n_estimators']) is not int or not 1 <= defaults['n_estimators'] <= 4:
        raise JobError('MODEL_PARAMETER', 'n_estimators parametresi 1-4 arasında tamsayı olmalıdır.')
    value = defaults['softmax_temperature']
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not .05 <= value <= 5:
        raise JobError('MODEL_PARAMETER', 'softmax_temperature parametresi 0.05-5 aralığında olmalıdır.')
    return defaults


@contextlib.contextmanager
def offline_network():
    # Apply before importing Torch, TabPFN or its telemetry dependencies.
    for key, value in {'TABPFN_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1',
                       'HF_HUB_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
                       'TRANSFORMERS_OFFLINE': '1', 'WANDB_DISABLED': 'true',
                       'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
                       'OPENBLAS_NUM_THREADS': '1'}.items():
        os.environ[key] = value
    def denied(*args, **kwargs):
        raise OSError('TabPFN hesaplama sürecinde ağ bağlantısı kapalıdır.')
    with contextlib.ExitStack() as stack:
        for attribute in ('connect', 'connect_ex', 'sendto', 'sendmsg'):
            if hasattr(socket.socket, attribute):
                stack.enter_context(patch.object(socket.socket, attribute, denied))
        for attribute in ('create_connection', 'getaddrinfo', 'gethostbyname', 'gethostbyname_ex', 'gethostbyaddr'):
            stack.enter_context(patch.object(socket, attribute, denied))
        yield


def _runtime():
    from .models_d import _torch_cpu
    try:
        version = importlib.metadata.version('tabpfn')
    except importlib.metadata.PackageNotFoundError as exc:
        raise JobError('TABPFN_RUNTIME', 'TabPFN 2.2.1 yerel D çalışma ortamında bulunamadı.') from exc
    if version != PACKAGE_VERSION:
        raise JobError('TABPFN_RUNTIME', 'Yalnız doğrulanan TabPFN 2.2.1 sürümü kullanılabilir.')
    torch = _torch_cpu()
    from tabpfn import TabPFNRegressor
    return torch, TabPFNRegressor


def _versions():
    return {key: importlib.metadata.version(key) for key in
            ('tabpfn', 'torch', 'numpy', 'scikit-learn', 'tabpfn-common-utils')}


def _child(operation, parameters, seed, values, target, state_path, result_path, weight_directory):
    result = Path(result_path)
    try:
        with open(str(result) + '.log', 'w', encoding='utf-8') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log), offline_network():
            weight = verify_weights(weight_directory)
            torch, factory = _runtime()
            random.seed(seed)
            np.random.seed(seed)
            torch.manual_seed(seed)
            if operation == 'fit':
                model = factory(n_estimators=parameters['n_estimators'],
                                softmax_temperature=parameters['softmax_temperature'],
                                model_path=weight['path'], device='cpu', n_jobs=1,
                                random_state=seed, fit_mode='fit_preprocessors',
                                inference_precision=torch.float32, memory_saving_mode=True,
                                ignore_pretraining_limits=False)
                model.fit(values, target)
                serialized = io.BytesIO()
                joblib.dump(model, serialized)
                payload = serialized.getvalue()
                diagnostic = {'model': 'tabpfn', 'training_rows': len(values),
                              'encoded_features': values.shape[1], 'validation_rows': 0,
                              'training_policy': 'frozen_foundation_weights_training_partition_context_only',
                              'gradient_updates': 0, 'device': 'cpu', 'threads': 1, 'seed': seed,
                              'parameters': parameters, 'versions': _versions(), 'weight': weight,
                              'fit_mode': 'fit_preprocessors', 'prediction_batch_size': PREDICT_BATCH_SIZE,
                              'point_estimand': 'mean', 'network': 'disabled_in_owned_child',
                              'telemetry': 'disabled_before_import',
                              'resource_limits': {'seconds_per_operation': FIT_SECONDS, 'bytes_per_child': FIT_MEMORY}}
                from .modeling import _producer_parameters
                diagnostic['producer_parameters'] = _producer_parameters(model, inference=True)
                joblib.dump({'payload': payload, 'payload_sha256': hashlib.sha256(payload).hexdigest(), 'diagnostics': diagnostic}, result)
            elif operation == 'predict':
                # This state file was just materialized by our parent from its own
                # fitted payload. No externally supplied pickle path reaches here.
                model = joblib.load(state_path)
                pieces = []
                with torch.inference_mode():
                    for start in range(0, len(values), PREDICT_BATCH_SIZE):
                        pieces.append(np.asarray(model.predict(values[start:start + PREDICT_BATCH_SIZE], output_type='mean'), dtype=np.float64))
                prediction = np.concatenate(pieces)
                if prediction.shape != (len(values),) or not np.isfinite(prediction).all():
                    raise JobError('MODEL_NONFINITE', 'TabPFN sonlu ve doğru boyutta tahmin üretmedi.')
                joblib.dump({'prediction': prediction}, result)
            else:
                raise JobError('TABPFN_OPERATION', 'Bilinmeyen TabPFN işlemi.')
    except BaseException as exc:
        Path(str(result) + '.error.json').write_text(json.dumps({
            'code': getattr(exc, 'code', 'TABPFN_COMPUTE'), 'message': str(exc),
            'traceback': traceback.format_exc()}, ensure_ascii=False), encoding='utf-8')


def _read_result(path):
    error = Path(str(path) + '.error.json')
    if error.exists():
        failure = json.loads(error.read_text(encoding='utf-8'))
        raise JobError(failure['code'], failure['message'])
    if not path.is_file():
        raise JobError('D_MODEL_CHILD', 'TabPFN süreci sonuç üretmedi.')
    # The outer payload contains primitives and arrays, never a TabPFN instance.
    return joblib.load(path)


class BoundedTabPFNRegressor(RegressorMixin, BaseEstimator):
    def __init__(self, parameters=None, seed=42):
        self.parameters = parameters
        self.seed = seed

    def __getstate__(self):
        result = self.__dict__.copy()
        result.pop('_check_cancel_', None)
        result.pop('_prediction_cache_', None)
        return result

    def fit(self, X, y, *, check_cancel=lambda: None):
        for key in ('payload_', 'payload_sha256_', 'fit_diagnostics_', 'prediction_diagnostics_', 'n_features_in_', '_prediction_cache_'):
            self.__dict__.pop(key, None)
        parameters = _parameters(self.parameters or {})
        values, target = np.asarray(X), np.asarray(y)
        if values.ndim != 2 or not 2 <= len(values) <= 2000 or not 1 <= values.shape[1] <= 256:
            raise JobError('D_MODEL_BUDGET', 'TabPFN eğitimi 2-2000 satır ve 1-256 kodlanmış değişken gerektirir.')
        if len(values) > 1000:
            raise JobError('D_MODEL_BUDGET', 'TabPFN 2.2.1 işlemci sınırı 1000 eğitim satırıdır; üretici sınırı kapatılmaz.')
        if target.shape != (len(values),):
            raise JobError('TARGET', 'Hedef boyutu eğitim satırlarıyla uyuşmuyor.')
        if not np.isfinite(values).all() or not np.isfinite(target).all():
            raise JobError('MODEL_NONFINITE', 'TabPFN girdileri ve hedef sonlu olmalıdır.')
        weight = verify_weights()
        import psutil
        if psutil.virtual_memory().available < FIT_MEMORY + 512 * 1024**2:
            raise JobError('D_MODEL_MEMORY', 'TabPFN için en az 2.5 GiB kullanılabilir bellek gereklidir.')
        from .models_d import _bounded_child
        self._check_cancel_ = check_cancel
        with tempfile.TemporaryDirectory(prefix='sps-tabpfn-') as folder:
            result = Path(folder) / 'result.joblib'
            observed = _bounded_child(_child, ('fit', parameters, self.seed,
                np.asarray(values, dtype=np.float64), np.asarray(target, dtype=np.float64), None,
                str(result), str(Path(weight['path']).parent)),
                timeout_seconds=FIT_SECONDS, memory_bytes=FIT_MEMORY, check_cancel=check_cancel)
            check_cancel()
            learned = _read_result(result)
        self.payload_ = learned['payload']
        self.payload_sha256_ = learned['payload_sha256']
        self.n_features_in_ = values.shape[1]
        self.fit_diagnostics_ = {**learned['diagnostics'], **observed}
        return self

    def predict(self, X, *, check_cancel=None):
        if not hasattr(self, 'payload_'):
            raise JobError('MODEL_NOT_FITTED', 'Tahmin öncesinde TabPFN eğitilmelidir.')
        values = np.asarray(X)
        if values.ndim != 2 or values.shape[1] != self.n_features_in_:
            raise JobError('MODEL_FEATURES', 'Tahmin değişkenleri eğitimle uyuşmuyor.')
        if values.size * 8 > PREDICT_MATRIX_BYTES:
            raise JobError('D_MODEL_BUDGET', 'Tahmin matrisi 256 MiB sınırını aşıyor; raster penceresini küçültün.')
        if not np.isfinite(values).all():
            raise JobError('MODEL_NONFINITE', 'Tahmin girdileri sonlu olmalıdır.')
        if hashlib.sha256(self.payload_).hexdigest() != self.payload_sha256_:
            raise JobError('TABPFN_STATE', 'Kaydedilen TabPFN modelinin dosya özeti değişmiş.')
        callback = check_cancel or getattr(self, '_check_cancel_', lambda: None)
        callback()
        weight = verify_weights()
        if len(values) == 0:
            return np.empty(0, dtype=float)
        matrix = np.ascontiguousarray(values, dtype=np.float64)
        key = hashlib.sha256()
        key.update(json.dumps({'shape': list(values.shape), 'dtype': str(values.dtype),
                               'payload': self.payload_sha256_}, sort_keys=True).encode('ascii'))
        key.update(matrix.tobytes())
        signature = key.hexdigest()
        cached = getattr(self, '_prediction_cache_', None)
        if cached is not None and cached[0] == signature:
            return cached[1].copy()
        from .models_d import _bounded_child
        with tempfile.TemporaryDirectory(prefix='sps-tabpfn-predict-') as folder:
            result = Path(folder) / 'result.joblib'
            state = Path(folder) / 'owned-model.joblib'
            state.write_bytes(self.payload_)
            observed = _bounded_child(_child, ('predict', {}, self.seed,
                matrix, None, str(state), str(result), str(Path(weight['path']).parent)),
                timeout_seconds=FIT_SECONDS, memory_bytes=FIT_MEMORY, check_cancel=callback)
            callback()
            prediction = _read_result(result)['prediction']
        self.prediction_diagnostics_ = {**observed, 'rows': len(values), 'batch_size': PREDICT_BATCH_SIZE,
                                        'device': 'cpu', 'threads': 1}
        self._prediction_cache_ = (signature, prediction.copy())
        return prediction.copy()


def make_estimator(params, seed):
    return BoundedTabPFNRegressor(_parameters(params), seed)
