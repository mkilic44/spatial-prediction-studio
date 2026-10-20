"""Small, offline TabPFN prediction-cost pilot; never a large-map benchmark."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import statistics
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def digest(values):
    return hashlib.sha256(values).hexdigest()


def save_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'build' / 'd-tabpfn-cost.json')
    args = parser.parse_args()
    for key, value in {
        'TABPFN_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1',
        'HF_HUB_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
        'TRANSFORMERS_OFFLINE': '1', 'WANDB_DISABLED': 'true',
        'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1',
    }.items():
        os.environ[key] = value
    temporary_root = ROOT / 'build' / 'd-tabpfn-cost-temp'
    temporary_root.mkdir(parents=True, exist_ok=True)
    os.environ['TMP'] = os.environ['TEMP'] = str(temporary_root)
    sys.path.insert(0, str(ROOT / 'backend'))
    import joblib
    import numpy as np
    import psutil
    from sps.tabpfn_d import make_estimator, verify_weights

    record = {
        'status': 'started', 'started_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'synthetic CPU prediction pilot, not a large raster or scientific accuracy validation',
        'training_rows': 64, 'features': 5, 'seed': 42, 'parameters': {'n_estimators': 1},
        'query_rows': [128, 512], 'uncached_repeats_per_size': 3,
        'python': sys.executable, 'python_version': sys.version,
        'source_sha256': {
            name: digest((ROOT / name).read_bytes()) for name in (
                'backend/sps/tabpfn_d.py', 'backend/sps/models_d.py', 'requirements-d-lock.txt',
                'tools/benchmark_tabpfn_d.py',
            )
        },
        'memory_definition': 'peak_rss_bytes is the monitored owned compute child and its descendants; excludes the caller and ArcMap',
        'cache_policy': 'each call reloads the same locally created fitted state; its prediction cache is excluded by serialization',
        'observations': [],
        'limitations': [
            'Only 128 and 512 synthetic rows were predicted; no million-cell raster was run.',
            'The linear estimates repeat each measured window size including process startup; they are rough arithmetic extrapolations, not validated runtimes.',
            'Raster reading/writing, covariate extraction, full pipeline preprocessing, diagnostics and GIS display are excluded.',
            'Training context size, feature count, ensemble size, hardware, window size and process overhead affect actual cost.',
            'Measured memory is not extrapolated to one million rows; internal prediction batches contain at most 128 rows.',
            'Three repetitions at each size do not establish a timing distribution or operational service guarantee.',
        ],
    }
    save_record(args.output, record)
    try:
        available = psutil.virtual_memory().available
        record['available_memory_bytes_before_fit'] = available
        if available < int(2.5 * 1024**3):
            raise RuntimeError('At least 2.5 GiB available memory is required; the threshold is not relaxed.')
        record['weight'] = verify_weights()
        rng = np.random.default_rng(42)
        X = rng.normal(size=(64, 5))
        y = 2 * X[:, 0] - X[:, 1] + .15 * rng.normal(size=64)
        queries = rng.normal(size=(512, 5))
        record['synthetic_input_sha256'] = {
            'X': digest(X.tobytes()), 'y': digest(y.tobytes()), 'queries': digest(queries.tobytes()),
        }
        before = time.perf_counter()
        model = make_estimator({'n_estimators': 1}, 42).fit(X, y)
        record['fit_wall_seconds'] = time.perf_counter() - before
        record['fit_diagnostics'] = model.fit_diagnostics_
        stream = io.BytesIO()
        joblib.dump(model, stream)
        locally_created_state = stream.getvalue()
        record['local_serialized_model_sha256'] = digest(locally_created_state)
        save_record(args.output, record)
        print('fit completed', flush=True)
        reference_predictions = {}
        for rows in (128, 512):
            for repeat in range(1, 4):
                restored = joblib.load(io.BytesIO(locally_created_state))
                if hasattr(restored, '_prediction_cache_'):
                    raise AssertionError('A reloaded benchmark model must have no prediction cache.')
                parent_rss = psutil.Process().memory_info().rss
                before = time.perf_counter()
                prediction = restored.predict(queries[:rows])
                elapsed = time.perf_counter() - before
                if prediction.shape != (rows,) or not np.isfinite(prediction).all():
                    raise AssertionError('The actual prediction is not finite or has an invalid shape.')
                if rows in reference_predictions:
                    np.testing.assert_array_equal(prediction, reference_predictions[rows])
                else:
                    reference_predictions[rows] = prediction.copy()
                diagnostic = dict(restored.prediction_diagnostics_)
                if diagnostic.get('rows') != rows or not 0 < diagnostic['peak_rss_bytes'] <= 2 * 1024**3:
                    raise AssertionError('A real, resource-bounded prediction diagnostic is required.')
                record['observations'].append({
                    'rows': rows, 'repeat': repeat, 'wall_seconds': elapsed,
                    'prediction_diagnostics': diagnostic, 'caller_rss_bytes_before_prediction': parent_rss,
                    'prediction_sha256': digest(prediction.tobytes()),
                })
                save_record(args.output, record)
                print(f'predict rows={rows} repeat={repeat}: {elapsed:.3f}s', flush=True)
                del restored
        np.testing.assert_allclose(reference_predictions[128], reference_predictions[512][:128], rtol=0, atol=1e-5)
        record['window_equivalence_atol'] = 1e-5
        record['summary'] = []
        for rows in (128, 512):
            observations = [item for item in record['observations'] if item['rows'] == rows]
            times = [item['wall_seconds'] for item in observations]
            median = statistics.median(times)
            record['summary'].append({
                'rows': rows, 'median_wall_seconds': median,
                'min_wall_seconds': min(times), 'max_wall_seconds': max(times),
                'max_compute_child_peak_rss_bytes': max(item['prediction_diagnostics']['peak_rss_bytes'] for item in observations),
                'linear_rough_million_cell_seconds': median * (1_000_000 / rows),
                'linear_formula': 'median_wall_seconds * (1000000 / measured_rows)',
                'million_cell_run_performed': False,
            })
        record['status'] = 'completed'
        record['finished_utc'] = datetime.now(timezone.utc).isoformat()
        save_record(args.output, record)
        print(json.dumps(record['summary'], indent=2), flush=True)
        return 0
    except BaseException as exc:
        record['status'] = 'failed'
        record['error'] = {'type': type(exc).__name__, 'message': str(exc)}
        record['finished_utc'] = datetime.now(timezone.utc).isoformat()
        save_record(args.output, record)
        raise


if __name__ == '__main__':
    raise SystemExit(main())
