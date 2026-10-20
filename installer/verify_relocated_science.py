"""Small offline checks using only the actually installed Python/R/model paths."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--fixtures', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    assert Path(sys.executable).resolve() == root / 'runtime-d/Scripts/python.exe'
    output.mkdir(parents=True, exist_ok=False)
    temporary = output / 'temporary'
    temporary.mkdir()
    os.environ.update({'PATH': str(Path(os.environ['SystemRoot']) / 'System32'),
        'TMP': str(temporary), 'TEMP': str(temporary), 'SPS_MODELS_DIR': str(root / 'models'),
        'TABPFN_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1', 'HF_HUB_OFFLINE': '1',
        'HF_HUB_DISABLE_TELEMETRY': '1', 'TRANSFORMERS_OFFLINE': '1',
        'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1'})
    os.environ.pop('PYTHONHOME', None)
    os.environ.pop('PYTHONPATH', None)
    import tempfile
    tempfile.tempdir = str(temporary)
    import joblib
    import numpy as np
    import psutil
    from sps import knndm, tabpfn_d
    assert Path(knndm.__file__).resolve().is_relative_to(root)
    assert Path(tabpfn_d.__file__).resolve().is_relative_to(root)
    available = psutil.virtual_memory().available
    started = time.monotonic()
    record = {'status': 'running', 'python': sys.executable, 'installation_root': str(root),
              'development_runtime_used': False, 'synthetic_smoke_only': True,
              'available_memory_before_smoke_bytes': available, 'minimum_available_memory_bytes': int(2.5 * 1024**3)}
    def save():
        (output / 'readback.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    save()
    try:
        if available < int(2.5 * 1024**3):
            raise RuntimeError('At least 2.5 GiB available memory is required; the guard is not lowered.')
        def matrix(name):
            values = np.genfromtxt(args.fixtures / name, delimiter=',', names=True)
            return values, np.column_stack([values[n] for n in values.dtype.names])
        _, training = matrix('knndm_train.csv')
        _, prediction = matrix('knndm_prediction.csv')
        reference, _ = matrix('knndm_train_result.csv')
        expected_prediction, _ = matrix('knndm_prediction_result.csv')
        expected_summary, _ = matrix('knndm_summary.csv')
        actual = knndm.cast_knndm(training, prediction, 5, .5, 42)
        np.testing.assert_array_equal(actual['fold'], reference['fold'])
        for key in ('Gj', 'Gjstar'):
            np.testing.assert_allclose(actual[key], reference[key], atol=1e-8)
        np.testing.assert_allclose(actual['Gij'], expected_prediction['Gij'], atol=1e-8)
        assert abs(actual['W'] - float(expected_summary['W'])) <= 1e-8
        record['cast_reference_match'] = {'status': 'passed', 'training_points': len(training),
            'prediction_points': len(prediction), 'parameters': {'k': 5, 'maxp': .5, 'seed': 42},
            'actual_numeric_result': actual, 'reference': actual['reference'],
            'fixture_sha256': {name: hashlib.sha256((args.fixtures / name).read_bytes()).hexdigest()
                for name in ('knndm_train.csv', 'knndm_prediction.csv', 'knndm_train_result.csv',
                             'knndm_prediction_result.csv', 'knndm_summary.csv')},
            'rscript': str(root / 'reference-r/lib/R/bin/Rscript.exe')}
        save()
        rng = np.random.default_rng(42)
        X = rng.normal(size=(32, 5))
        y = 2 * X[:, 0] - X[:, 1] + .15 * rng.normal(size=32)
        queries = rng.normal(size=(8, 5))
        with tabpfn_d.offline_network():
            weight = tabpfn_d.verify_weights()
            assert Path(weight['path']).is_relative_to(root / 'models')
            model = tabpfn_d.make_estimator({'n_estimators': 1}, 42).fit(X, y)
            predicted = model.predict(queries)
            assert predicted.shape == (8,) and np.isfinite(predicted).all()
            saved = output / 'locally-created-tabpfn.joblib'
            joblib.dump(model, saved)
            restored = joblib.load(saved)
            np.testing.assert_allclose(restored.predict(queries), predicted, rtol=0, atol=1e-5)
        record['tabpfn'] = {'status': 'passed', 'weight': weight,
            'training_rows': 32, 'query_rows': 8, 'features': 5,
            'parameters': {'n_estimators': 1, 'softmax_temperature': .9}, 'seed': 42,
            'data_generator': 'numpy.default_rng(42); normal X[32,5]; y=2*X[:,0]-X[:,1]+0.15*normal(32); normal query[8,5]',
            'input_sha256': {name: hashlib.sha256(value.tobytes()).hexdigest()
                             for name, value in [('X', X), ('y', y), ('query', queries)]},
            'query_values': queries.tolist(), 'prediction_values': predicted.tolist(),
            'fit': model.fit_diagnostics_, 'prediction': model.prediction_diagnostics_,
            'prediction_sha256': hashlib.sha256(predicted.tobytes()).hexdigest(),
            'locally_created_state_reload_equal': True, 'offline_network_guard_active': True}
        record.update(status='passed', elapsed_seconds=time.monotonic() - started)
    except BaseException as error:
        record.update(status='failed', error=repr(error), elapsed_seconds=time.monotonic() - started)
        save()
        raise
    save()
    print(json.dumps({'status': record['status'], 'evidence': str(output / 'readback.json')}))


if __name__ == '__main__':
    main()
