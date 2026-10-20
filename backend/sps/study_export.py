"""Export actual sampled inputs, declared memberships and completed analysis files.

This module never fits, imputes or re-extracts model data. Input rasters stay at
their source; the exact extracted matrix used by the completed workflow is saved.
"""
from __future__ import annotations

from copy import deepcopy
import csv
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import zipfile

import numpy as np

from .contract import JobError

DATA_FILES = ['sampled_data.csv', 'sampled_data.json', 'data_dictionary.json', 'data_roles.csv',
              'role_data.csv', 'learning_membership.csv', 'excluded_samples.csv', 'excluded_samples.json',
              'trial_results.csv', 'inner_fold_results.csv']
PACKAGE_NAME = 'study_package.spsstudy.zip'


def _require(condition, message):
    if not condition:
        raise JobError('STUDY_EXPORT', message)


def _normal(value):
    if isinstance(value, np.generic): value = value.item()
    if isinstance(value, float) and not math.isfinite(value): return None
    if isinstance(value, dict): return {str(k): _normal(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)): return [_normal(v) for v in value]
    return value


def _json(value):
    return (json.dumps(_normal(value), ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')


def _cell(value):
    value = _normal(value)
    if value is None: return ''
    if isinstance(value, (dict, list)): return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r', '\n')): return "'" + value
    return value


def _csv(path, rows, fields, check_cancel):
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='raise')
        writer.writeheader()
        for number, row in enumerate(rows):
            if number % 128 == 0: check_cancel()
            writer.writerow({key: _cell(row.get(key)) for key in fields})
    check_cancel()


def _fields(rows, initial=()):
    return list(dict.fromkeys([*initial, *(key for row in rows for key in row)]))


def _role_rows(evaluation, ids, check_cancel):
    known = set(ids)
    rows = []
    def add(values, scope, role, partition, **context):
        _require(len(values) == len(set(values)) and set(values) <= known, 'IDs in a data role are inconsistent.')
        for identity in values:
            rows.append({'scope': scope, 'role': role, 'partition': partition, 'sample_id': identity, **context})
    records = [('final', evaluation['final_record'], {})]
    for item in evaluation.get('fold_metrics', []):
        records.append(('outer/%s/%s' % (item['repeat'], item['fold']), item['model_record'],
                        {'repeat': item['repeat'], 'fold': item['fold']}))
    independent = set(evaluation['final_record'].get('independent_ids', []))
    for fold in evaluation.get('folds', []):
        check_cancel()
        role = 'outer/%s/%s' % (fold['repeat'], fold['fold'])
        for key, partition in [('train_ids', 'training_pool_before_calibration'), ('test_ids', 'test'), ('excluded_ids', 'excluded_by_split')]:
            add(fold.get(key, []), 'outer', role, partition, repeat=fold['repeat'], fold=fold['fold'], status='declared_outer_split')
        # Current validation folds call their excluded list excluded_ids.
        if 'excluded_ids' not in fold:
            add(fold.get('excluded', []), 'outer', role, 'excluded_by_split', repeat=fold['repeat'], fold=fold['fold'], status='declared_outer_split')
    for role, record, position in records:
        context = {'model_id': record.get('model', {}).get('id'), 'status': 'fitted_partition', **position}
        for key, partition in [('development_ids', 'training'), ('calibration_ids', 'calibration'), ('excluded_ids', 'excluded_by_partition')]:
            add(record.get(key, []), 'final' if role == 'final' else 'outer', role, partition, **context)
    add(sorted(independent), 'final', 'final', 'independent_test', status='held_out')
    tested = {row['sample_id'] for row in evaluation.get('oof', [])}
    temporal = evaluation['final_record'].get('temporal')
    if temporal:
        add(sorted(known - tested - independent), 'initial_history', 'outer', 'initial_training_history', status='not_outer_tested')
    for trial in evaluation.get('trials', []):
        complete = {(f['repeat'], f['fold']) for f in trial.get('inner_fold_metrics', [])}
        for fold in trial.get('inner_folds', []):
            check_cancel()
            context = {'trial': trial['trial'], 'repeat': fold['repeat'], 'fold': fold['fold'],
                       'model_id': trial.get('model', {}).get('id'), 'trial_status': trial.get('status'),
                       'status': 'completed_fold' if (fold['repeat'], fold['fold']) in complete else 'planned_partition_not_completed'}
            for key, partition in [('train_ids', 'training'), ('test_ids', 'validation')]:
                add(fold[key], 'inner', trial['role'], partition, **context)
    return rows


def _trial_rows(evaluation, check_cancel):
    trials, folds = [], []
    for trial in evaluation.get('trials', []):
        check_cancel()
        common = {key: trial.get(key) for key in ('role', 'trial', 'status', 'validation_status', 'selected', 'metric', 'value',
                  'error', 'validation_reason', 'elapsed_seconds', 'validation_completed_folds', 'validation_expected_rows',
                  'comparison_arm', 'feature_selection_method')}
        common['model_id'] = trial.get('model', {}).get('id')
        common['search_method'] = trial.get('search', {}).get('method')
        common['stop_reason'] = trial.get('search_summary', {}).get('stop_reason')
        common.update({'param_model.' + key: value for key, value in trial.get('model', {}).get('parameters', {}).items()})
        common.update({'param_search.' + key: value for key, value in trial.get('parameters', {}).items()})
        trials.append(common)
        actual = {(f['repeat'], f['fold']): f for f in trial.get('inner_fold_metrics', [])}
        for fold in trial.get('inner_folds', []):
            recorded = actual.get((fold['repeat'], fold['fold']))
            row = {**common, 'trial_status': trial.get('status'), 'repeat': fold['repeat'], 'fold': fold['fold'],
                   'status': 'complete' if recorded else 'not_completed', 'planned_training_samples': len(fold['train_ids']),
                   'planned_validation_samples': len(fold['test_ids']), 'training_metrics_status': 'not_computed',
                   'training_metrics_reason': 'Only the actual inner held-out predictions were evaluated; no extra model evaluation was requested.'}
            if recorded:
                row.update({key: recorded.get(key) for key in ('training_sample_count', 'training_group_count', 'test_sample_count',
                            'test_group_count', 'selected_features', 'effective_parameters', 'target_transform')})
                for key, value in recorded['metrics'].items():
                    if key == 'reasons': row.update({'metric_reason.' + k: v for k, v in value.items()})
                    else: row['metric.' + key] = value
            folds.append(row)
    return trials, folds


def _excluded_records(settings, check_cancel):
    spec = settings['samples']
    excluded = {row['id']: row['reason'] for row in spec.get('excluded', [])}
    if not excluded: return []
    path = Path(spec['path'])
    records = []
    def append(identity, raw):
        if identity in excluded:
            records.append({'sample_id': identity, 'reason': excluded[identity], 'status': 'excluded_before_modeling',
                            'source_path': str(path), 'raw_record_status': 'recorded', 'raw_record': raw})
    if path.suffix.lower() == '.csv':
        with path.open(encoding='utf-8-sig', newline='') as stream:
            for row in csv.DictReader(stream):
                check_cancel(); append(str(row.get(spec['id_field'], '')).strip(), {'properties': row})
    else:
        obj = json.loads(path.read_text(encoding='utf-8-sig'))
        for feature in obj['features']:
            check_cancel(); append(str(feature.get('properties', {}).get(spec['id_field'], '')).strip(), feature)
    _require({row['sample_id'] for row in records} == set(excluded), 'The raw record of an excluded observation could not be read from the source.')
    return records


def write_analysis_exports(output, X, samples, groups, evaluation, settings, *, check_cancel):
    """Publish complete sampled inputs and roles as one reversible transaction."""
    output = Path(output).resolve(); output.mkdir(parents=True, exist_ok=True)
    ids = list(samples['ids']); X = np.asarray(X, dtype=float)
    _require(X.shape == (len(ids), len(settings['covariates'])) and len(groups) == len(ids) and not np.isinf(X).any(), 'The data matrix to export is inconsistent.')
    _require(len(set(ids)) == len(ids), 'Observation IDs to export must be unique.')
    _require(not any((output / name).exists() for name in DATA_FILES), 'Existing analysis data are not overwritten.')
    stage = Path(tempfile.mkdtemp(prefix='.study-data-', dir=output)); published = []
    try:
        covariates = settings['covariates']; date_field = settings['samples'].get('date_field')
        external = set(samples.get('external_independent_ids', []))
        rows, flat = [], []
        for i, identity in enumerate(ids):
            if i % 128 == 0: check_cancel()
            props = samples['props'][i]
            values = {cov['id']: _normal(X[i, j]) for j, cov in enumerate(covariates)}
            row = {'sample_id': identity, 'group_id': str(groups[i]), 'x': float(samples['coordinates'][i, 0]),
                   'y': float(samples['coordinates'][i, 1]), 'observed': float(samples['y'][i]),
                   'observation_date': props.get(date_field) if date_field else None,
                   'validation_date': settings.get('validation', {}).get('dates_by_id', {}).get(identity),
                   'source_role': 'external_independent_source' if identity in external else 'development_source',
                   'covariates': values, 'properties': props}
            rows.append(row)
            flat.append({**{key: value for key, value in row.items() if key not in ('covariates', 'properties')},
                         **{'covariate.' + key: value for key, value in values.items()},
                         **{'property.' + key: value for key, value in props.items()}})
        data_fields = _fields(flat)
        dictionary = {'schema_version': 1, 'target': {'column': 'observed', 'source_field': settings['samples']['target_field'], 'unit': settings['samples']['unit']},
            'coordinate_crs': samples['crs'].to_wkt(), 'coordinate_scope': 'samples CRS; external independent coordinates transformed to this CRS',
            'date_scope': 'observation_date is the source value; validation_date is the normalized calendar date actually used by temporal validation, otherwise null.',
            'covariates': [{'column': 'covariate.' + cov['id'], **cov} for cov in covariates],
            'covariate_value_scope': 'Exact extracted values passed to evaluation, before imputation, scaling, encoding or feature selection.',
            'missing_value': {'json': None, 'csv': '', 'reason': 'missing extracted covariate value; not imputed in this export'},
            'properties_scope': 'Original retained source attributes; property columns are not additional model covariates.',
            'csv_text_escape': 'Potential spreadsheet formula strings are prefixed by an apostrophe; sampled_data.json preserves exact original strings.',
            'role_scope': 'Membership rows join sampled_data by sample_id; repeated roles are not additional independent observations.',
            'inner_training_metrics': {'status': 'not_computed', 'reason': 'Only inner held-out point predictions were calculated by the selection workflow.'},
            'inner_interval_metrics': {'status': 'not_computed', 'reason': 'Separate calibration is outside inner candidate fitting; no interval result is invented.'}}
        (stage / 'sampled_data.json').write_bytes(_json({'schema_version': 1, 'scope': dictionary['covariate_value_scope'], 'rows': rows}))
        (stage / 'data_dictionary.json').write_bytes(_json(dictionary))
        _csv(stage / 'sampled_data.csv', flat, data_fields, check_cancel)
        roles = _role_rows(evaluation, ids, check_cancel)
        role_fields = _fields(roles, ('scope', 'role', 'trial', 'repeat', 'fold', 'partition', 'sample_id', 'status'))
        _csv(stage / 'data_roles.csv', roles, role_fields, check_cancel)
        lookup = dict(zip(ids, flat))
        _csv(stage / 'role_data.csv', ({**lookup[row['sample_id']], **row} for row in roles), list(dict.fromkeys(role_fields + data_fields)), check_cancel)
        learning = [{'role': row['role'], 'stage': row['stage'], 'sample_id': identity} for row in evaluation.get('learning_audit', []) for identity in row['ids']]
        _require(all(row['sample_id'] in lookup for row in learning), 'The learning record contains an unknown observation.')
        _csv(stage / 'learning_membership.csv', learning, ['role', 'stage', 'sample_id'], check_cancel)
        excluded = _excluded_records(settings, check_cancel)
        _require(set(ids).isdisjoint(row['sample_id'] for row in excluded), 'An excluded observation cannot be present in the model data.')
        (stage / 'excluded_samples.json').write_bytes(_json({'schema_version': 1, 'scope': 'Excluded before extraction/modeling; no fitted values are assigned.', 'records': excluded}))
        _csv(stage / 'excluded_samples.csv', excluded, ['sample_id', 'reason', 'status', 'source_path', 'raw_record_status', 'raw_record'], check_cancel)
        trials, folds = _trial_rows(evaluation, check_cancel)
        _csv(stage / 'trial_results.csv', trials, _fields(trials, ('role', 'trial', 'status', 'selected')), check_cancel)
        _csv(stage / 'inner_fold_results.csv', folds, _fields(folds, ('role', 'trial', 'repeat', 'fold', 'status', 'metric.rmse')), check_cancel)
        for name in DATA_FILES:
            check_cancel(); destination = output / name
            _require(not destination.exists(), 'A destination file appeared while the analysis data were being published.')
            (stage / name).rename(destination); published.append(destination)
        return published, {'schema_version': 1, 'files': list(DATA_FILES), 'samples': len(ids), 'memberships': len(roles),
            'learning_memberships': len(learning), 'excluded_samples': len(excluded), 'actual_trials': len(trials),
            'declared_inner_folds': len(folds), 'scope': dictionary['covariate_value_scope'],
            'inner_training_metrics_status': 'not_computed', 'inner_interval_metrics_status': 'not_computed',
            'study_package': PACKAGE_NAME}
    except Exception:
        for path in published: path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(stage)


def _member(name):
    _require(isinstance(name, str) and bool(name), 'A file path in the study package is invalid.')
    path = PurePosixPath(name)
    _require(str(path) == name and not path.is_absolute() and '..' not in path.parts and '\\' not in name
             and ':' not in name and not name.endswith('/'), 'A file path in the study package is invalid.')


def _hash(stream, check_cancel):
    digest = hashlib.sha256()
    while True:
        check_cancel(); block = stream.read(1024 * 1024)
        if not block: return digest.hexdigest()
        digest.update(block)


def inspect_study_package(path, *, expected_sha256=None, check_cancel=lambda: None):
    path = Path(path)
    with path.open('rb') as stream: digest = _hash(stream, check_cancel)
    _require(expected_sha256 is None or digest == expected_sha256, 'The file digest of the study package does not match.')
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        _require(len(names) == len({name.casefold() for name in names}), 'The study package contains a duplicate file.')
        for name in names: _member(name)
        manifest = json.loads(archive.read('study_manifest.json'))
        _require(manifest.get('schema_version') == 1 and set(manifest.get('members', {})) == set(names) - {'study_manifest.json'}, 'The file list of the study package does not match.')
        for name, expected in manifest['members'].items():
            with archive.open(name) as stream: actual = _hash(stream, check_cancel)
            _require(actual == expected, 'The digest of a study package member differs: ' + name)
        snapshot = json.loads(archive.read('result.snapshot.json'))
        _require(snapshot.get('status') == 'completed' and snapshot.get('run_id') == manifest.get('run_id'), 'The run ID in the study package result is inconsistent.')
        _require(len({entry['path'] for entry in snapshot['outputs']}) == len(snapshot['outputs']), 'The result record of the study package contains a duplicate file.')
        for entry in snapshot['outputs']:
            _require(entry['kind'] != 'study_package' and manifest['members'].get(entry['path']) == entry['sha256'], 'The result record of the study package does not match its content.')
    return {'status': 'verified', 'sha256': digest, 'manifest': manifest, 'members': len(names), 'scope': 'file integrity only; no model loaded'}


def create_study_package(output, result, *, check_cancel):
    """Archive all verified products and the request; no recursive archive hashes."""
    output = Path(output).resolve(); destination = output / PACKAGE_NAME
    partial = output / (PACKAGE_NAME + '.partial')
    _require(not destination.exists() and not partial.exists(), 'An existing study package is not overwritten.')
    _require(result.get('status') == 'completed' and result.get('outputs'), 'A study package requires completed analysis outputs.')
    sources = {}
    for entry in result['outputs']:
        name = entry['path']; _member(name)
        path = (output / name).resolve()
        _require(path.is_relative_to(output) and path.is_file() and name not in sources
                 and name not in ('result.json', 'result.snapshot.json', 'study_manifest.json') and not name.endswith('.spsstudy.zip'), 'The source list of the study package is invalid.')
        sources[name] = (path, entry['sha256'])
    request = output / 'request.snapshot.json'
    _require(request.is_file(), 'The request record for the study package was not found.')
    if request.name not in sources:
        with request.open('rb') as stream: sources[request.name] = (request, _hash(stream, check_cancel))
    snapshot = deepcopy(result)
    snapshot['archive_snapshot_scope'] = 'Completed analysis outputs before adding this study archive; excludes its own archive entry to avoid recursive hashing.'
    generated = {'result.snapshot.json': _json(snapshot)}
    hashes = {}
    try:
        with zipfile.ZipFile(partial, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            for name, (path, expected) in sorted(sources.items()):
                check_cancel()
                with path.open('rb') as stream: before = _hash(stream, check_cancel)
                _require(before == expected, 'An analysis output changed before packaging: ' + name)
                actual = hashlib.sha256()
                with path.open('rb') as src, archive.open(name, 'w', force_zip64=True) as dst:
                    while True:
                        check_cancel(); block = src.read(1024 * 1024)
                        if not block: break
                        actual.update(block); dst.write(block)
                with path.open('rb') as stream: after = _hash(stream, check_cancel)
                _require(actual.hexdigest() == after == expected, 'An analysis output changed during packaging: ' + name)
                hashes[name] = expected
            for name, content in generated.items():
                check_cancel(); archive.writestr(name, content); hashes[name] = hashlib.sha256(content).hexdigest()
            manifest = {'schema_version': 1, 'run_id': result['run_id'], 'algorithm': 'SHA-256', 'members': hashes,
                'entry_point': 'study_report_index.html', 'scope': 'All produced analysis products, models, maps, tables, HTML, figures and request snapshot.',
                'excluded': ['Original input rasters and other source files; sampled_data.json/CSV contain the actual extracted model inputs.',
                             'Live progress logs and final result.json; result.snapshot.json excludes only this archive entry.',
                             'This archive itself and study_manifest.json cannot hash themselves.'],
                'trust_scope': 'Hashes establish consistency, not trust in an unknown model. No model is loaded during verification.'}
            archive.writestr('study_manifest.json', _json(manifest))
        inspect_study_package(partial, check_cancel=check_cancel)
        check_cancel(); partial.rename(destination)
        return destination
    except Exception:
        partial.unlink(missing_ok=True)
        raise
