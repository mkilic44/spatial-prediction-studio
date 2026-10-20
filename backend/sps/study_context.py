"""Post-evaluation descriptive evidence; never used for fitting or selection."""
from datetime import datetime, timezone
import importlib.metadata
import os
from pathlib import Path
import platform
import sys

import numpy as np

from .data import fingerprint


def numeric_summary(values):
    values = np.asarray(values, dtype=float)
    finite = values[np.isfinite(values)]
    result = {'n': int(len(values)), 'finite_n': int(len(finite)),
              'missing_n': int(len(values) - len(finite))}
    if not len(finite):
        return {**result, 'status': 'unavailable', 'reason': 'no_finite_values'}
    q25, median, q75 = np.quantile(finite, [.25, .5, .75], method='linear')
    return {**result, 'status': 'available', 'mean': float(finite.mean()),
            'std': float(finite.std(ddof=1)) if len(finite) > 1 else None,
            'std_reason': None if len(finite) > 1 else 'fewer_than_two_observations',
            'min': float(finite.min()), 'q25': float(q25), 'median': float(median),
            'q75': float(q75), 'max': float(finite.max()),
            'iqr': float(q75 - q25), 'quantile_rule': 'linear', 'std_ddof': 1}


def build_study_context(X, samples, groups, settings, evaluation, sources, started_utc,
                        model_files, *, check_cancel):
    check_cancel()
    ids = list(samples['ids'])
    lookup = {identity: i for i, identity in enumerate(ids)}
    record = evaluation['final_record']
    scopes = {'all_retained': ids,
              'final_training': record['development_ids'],
              'final_calibration': record['calibration_ids'],
              'independent_test': record.get('independent_ids', []),
              'outer_tested_unique': sorted({row['sample_id'] for row in evaluation['oof']})}
    descriptive = []
    for scope, values in scopes.items():
        check_cancel()
        index = np.asarray([lookup[identity] for identity in values], dtype=int)
        common = {'scope': scope, 'unique_samples': len(values),
                  'unique_groups': len(set(str(groups[i]) for i in index))}
        descriptive.append({**common, 'variable': settings['samples']['target_field'],
                            'role': 'target', 'unit': settings['samples']['unit'],
                            **numeric_summary(np.asarray(samples['y'])[index])})
        for j, cov in enumerate(settings['covariates']):
            if j % 64 == 0: check_cancel()
            raw = np.asarray(X)[index, j]
            row = {**common, 'variable': cov['id'], 'name': cov.get('name'),
                   'role': 'covariate', 'unit': cov['unit'], 'kind': cov['kind']}
            if cov['kind'] == 'categorical':
                valid = raw[np.isfinite(raw)]
                cats, counts = np.unique(valid, return_counts=True)
                row.update(n=len(raw), finite_n=len(valid), missing_n=len(raw)-len(valid),
                           categories=[{'value': float(v), 'count': int(n)} for v, n in zip(cats, counts)],
                           status='available' if len(valid) else 'unavailable')
            else:
                row.update(numeric_summary(raw))
            descriptive.append(row)
    packages = {}
    for name in ('numpy','scipy','scikit-learn','rasterio','pyproj','xgboost','optuna',
                 'quantile-forest','Boruta','matplotlib','joblib','lightgbm','catboost',
                 'cubist','PyKrige','torch','tabm','pytabkit','tabpfn','tabpfn-common-utils'):
        try: packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: pass
    finished = datetime.now(timezone.utc)
    model_name = Path(model_files['path']).name
    package_name = ('TabPFN-' if record.get('model', {}).get('id') == 'tabpfn' else '') + 'model_package.spsmodel.zip'
    result = {'schema_version': 1, 'started_utc': started_utc,
              'finished_analysis_utc': finished.isoformat(),
              'elapsed_analysis_seconds': (finished-datetime.fromisoformat(started_utc)).total_seconds(),
              'elapsed_scope': 'through evaluation, model roundtrip and raster mapping; excludes report and package writing',
              'sources': sources, 'packages': packages, 'platform': platform.platform(),
              'python': sys.version, 'executable': sys.executable, 'cpu_count': os.cpu_count(),
              'processor': platform.processor() or 'not_recorded',
              'source_code': [fingerprint(p, check_cancel) for p in sorted(Path(__file__).parent.iterdir())
                              if p.suffix in ('.py', '.R')],
              'descriptive_statistics': descriptive, 'covariates': settings['covariates'],
              'sample_crs': samples['crs'].to_wkt(), 'depth': samples.get('depth'),
              'laboratory_method': samples.get('laboratory_method'), 'sampling_support': samples.get('support'),
              'description_scope': 'raw extracted predictors, before imputation or encoding; after all model choices are fixed',
              'sample_count': len(ids), 'group_count': len(set(map(str, groups))),
              'retained_observation_ids': ids,
              'model_artifact': {'path': model_name, 'metadata_path': Path(model_files['metadata_path']).name,
                                 'sha256': model_files['sha256'], 'package_path': package_name,
                                 'roundtrip_verified': True,
                                 'roundtrip_scope': 'identical point predictions and available intervals on retained samples'}}
    try:
        import psutil
        mem = psutil.virtual_memory()
        result['memory'] = {'total_bytes': int(mem.total), 'available_bytes_at_report': int(mem.available)}
    except ImportError:
        result['memory'] = {'status': 'not_recorded'}
    return result
