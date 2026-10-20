import copy
import csv
import hashlib
import json
from pathlib import Path
import sys
import zipfile

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from sps.contract import Cancelled, JobError
from sps.study_export import create_study_package, inspect_study_package, write_analysis_exports
from test_engine_b import request, run_request


def csv_rows(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


@pytest.fixture(scope='module')
def grid_run(tmp_path_factory):
    folder = tmp_path_factory.mktemp('full-study-grid')
    req = request(folder)
    req['validation'].update(n_splits=2, inner_splits=2)
    req['optimization'] = {'method': 'grid', 'trials': 3, 'spaces': {'alpha': {'type': 'categorical', 'choices': [0.1, 1.0, 10.0]}}}
    req['samples']['excluded'] = [{'id': 'S003', 'reason': 'Synthetic exclusion, not evidence of measurement error'}]
    req['validation']['independent_ids'] = ['S001', 'S002']
    result, settings = run_request(req, folder)
    assert result['status'] == 'completed', result
    return folder / 'run', result, settings


def test_real_grid_exports_raw_values_and_every_trial_partition(grid_run):
    output, result, settings = grid_run
    from sps.data_b import read_samples_b, RasterInputsB
    audit = json.loads((output / 'audit.json').read_text(encoding='utf-8'))
    samples = read_samples_b(audit['settings']['samples'])
    with RasterInputsB(audit['settings']['covariates'], audit['settings']['mapping'], output / 'readback-unused') as rasters:
        expected, _ = rasters.extract(samples)
    dataset = json.loads((output / 'sampled_data.json').read_text(encoding='utf-8'))
    assert [row['sample_id'] for row in dataset['rows']] == samples['ids']
    np.testing.assert_allclose([[row['covariates'][cov['id']] for cov in audit['settings']['covariates']] for row in dataset['rows']], expected, equal_nan=True)
    np.testing.assert_array_equal([row['observed'] for row in dataset['rows']], samples['y'])
    assert len(csv_rows(output / 'sampled_data.csv')) == len(samples['ids'])
    trials = csv_rows(output / 'trial_results.csv')
    assert len(trials) == 9
    assert sum(row['selected'] == 'True' for row in trials) == 3
    for role in {row['role'] for row in trials}:
        assert {float(row['param_model.alpha']) for row in trials if row['role'] == role} == {.1, 1., 10.}
    inner = csv_rows(output / 'inner_fold_results.csv')
    assert len(inner) == 18 and all(row['status'] == 'complete' for row in inner)
    assert all(row['training_metrics_status'] == 'not_computed' for row in inner)
    roles = csv_rows(output / 'data_roles.csv')
    assert len(csv_rows(output / 'role_data.csv')) == len(roles)
    assert 'S003' not in {row['sample_id'] for row in roles}
    independent = {'S001', 'S002'}
    assert {row['sample_id'] for row in roles if row['partition'] == 'independent_test'} == independent
    assert all(row['sample_id'] not in independent for row in roles if row['scope'] in ('inner', 'outer'))
    for row in roles:
        if row['scope'] == 'outer':
            assert row['role'] == 'outer/' + row['repeat'] + '/' + row['fold']
    ev = json.loads((output / 'evaluation.json').read_text())['evaluation']
    for trial in ev['trials']:
        for fold in trial['inner_folds']:
            actual = [r for r in roles if r['scope'] == 'inner' and r['role'] == trial['role'] and int(r['trial']) == trial['trial']
                      and int(r['repeat']) == fold['repeat'] and int(r['fold']) == fold['fold']]
            assert {r['sample_id'] for r in actual if r['partition'] == 'training'} == set(fold['train_ids'])
            assert {r['sample_id'] for r in actual if r['partition'] == 'validation'} == set(fold['test_ids'])
    excluded = json.loads((output / 'excluded_samples.json').read_text())['records']
    assert len(excluded) == 1 and excluded[0]['sample_id'] == 'S003'
    assert excluded[0]['raw_record']['properties']['sample_id'] == 'S003'
    assert all(entry['kind'] == 'study_package' for entry in result['outputs'] if entry['path'].endswith('.spsstudy.zip'))


def test_study_archive_contains_every_output_and_all_local_report_targets(grid_run):
    output, result, _ = grid_run
    package = output / 'study_package.spsstudy.zip'
    info = inspect_study_package(package)
    assert info['status'] == 'verified'
    with zipfile.ZipFile(package) as archive:
        names = set(archive.namelist())
        assert 'model_package.spsmodel.zip' in names and 'request.snapshot.json' in names
        assert not any(name.endswith('.spsstudy.zip') for name in names)
        assert 'result.json' not in names
        snapshot = json.loads(archive.read('result.snapshot.json'))
        assert snapshot['status'] == 'completed' and snapshot['run_id'] == result['run_id']
        assert snapshot['outputs'] == [r for r in result['outputs'] if r['kind'] != 'study_package']
        for entry in snapshot['outputs']:
            assert hashlib.sha256(archive.read(entry['path'])).hexdigest() == entry['sha256']
        assert {'sampled_data.csv', 'role_data.csv', 'trial_results.csv', 'inner_fold_results.csv', 'prediction.tif', 'audit.json'} <= names
        from html.parser import HTMLParser
        class Links(HTMLParser):
            def handle_starttag(self, tag, attrs):
                for key, value in attrs:
                    if key in ('src', 'href') and not value.startswith('#'):
                        assert value in names, value
        for name in names:
            if name.endswith('.html'):
                Links().feed(archive.read(name).decode('utf-8'))


def test_partial_failed_trial_exports_only_actual_scores_and_planned_membership(tmp_path, grid_run):
    output, _, _ = grid_run
    from sps.data_b import read_samples_b, RasterInputsB
    audit = json.loads((output / 'audit.json').read_text())
    samples = read_samples_b(audit['settings']['samples'])
    with RasterInputsB(audit['settings']['covariates'], audit['settings']['mapping'], tmp_path / 'unused') as rasters:
        X, _ = rasters.extract(samples)
    evaluation = json.loads((output / 'evaluation.json').read_text())['evaluation']
    trial = evaluation['trials'][0]
    trial.update(status='failed', selected=False, validation_status='incomplete', validation_metrics=None, value=None, error='fixture fit failure')
    trial['inner_fold_metrics'] = trial['inner_fold_metrics'][:1]
    groups = [audit['groups'][identity] for identity in samples['ids']]
    write_analysis_exports(tmp_path, X, samples, groups, evaluation, audit['settings'], check_cancel=lambda: None)
    rows = [r for r in csv_rows(tmp_path / 'inner_fold_results.csv') if r['role'] == trial['role'] and int(r['trial']) == trial['trial']]
    assert [r['status'] for r in rows] == ['complete', 'not_completed']
    assert rows[0]['trial_status'] == 'failed' and rows[1]['metric.rmse'] == ''
    trials = csv_rows(tmp_path / 'trial_results.csv')
    failed = next(r for r in trials if r['role'] == trial['role'] and int(r['trial']) == trial['trial'])
    assert failed['status'] == 'failed' and failed['value'] == '' and failed['selected'] == 'False'


@pytest.mark.parametrize('failure', [Cancelled(), JobError('STUDY_TEST', 'Packaging test')])
def test_late_study_package_failure_rolls_back_data_reports_models_and_archive(tmp_path, monkeypatch, failure):
    import sps.study_export
    real_create = sps.study_export.create_study_package
    def fail(*args, **kwargs):
        assert (tmp_path / 'run/sampled_data.csv').is_file()
        assert (tmp_path / 'run/study_report_index.html').is_file()
        package = real_create(*args, **kwargs)
        assert package.is_file()
        raise failure
    monkeypatch.setattr(sps.study_export, 'create_study_package', fail)
    result, _ = run_request(request(tmp_path), tmp_path)
    assert result['status'] == ('cancelled' if isinstance(failure, Cancelled) else 'failed')
    assert result['outputs'] == []
    assert all(not (tmp_path / 'run' / name).exists() for name in sps.study_export.DATA_FILES)
    for pattern in ('sampled_data.*', 'data_roles.csv', 'role_data.csv', 'study_report*.html', '*.spsstudy.zip', '*.spsmodel.zip', '*.partial'):
        assert not list((tmp_path / 'run').glob(pattern))


def test_archive_cancel_checks_during_streaming_and_preserves_existing_files(tmp_path):
    (tmp_path / 'large.bin').write_bytes(b'x' * (4 * 1024 * 1024))
    (tmp_path / 'request.snapshot.json').write_text('{}')
    entry = {'kind': 'table', 'path': 'large.bin', 'sha256': hashlib.sha256((tmp_path / 'large.bin').read_bytes()).hexdigest()}
    result = {'run_id': 'cancel-stream', 'status': 'completed', 'outputs': [entry]}
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        if calls == 11:
            assert (tmp_path / 'study_package.spsstudy.zip.partial').is_file()
            raise Cancelled()
    with pytest.raises(Cancelled):
        create_study_package(tmp_path, result, check_cancel=cancel)
    assert calls == 11 and not list(tmp_path.glob('*.zip*'))
    assert (tmp_path / 'large.bin').is_file()


def test_archive_rejects_changed_member_and_path_traversal(tmp_path):
    (tmp_path / 'data.csv').write_text('before')
    (tmp_path / 'request.snapshot.json').write_text('{}')
    result = {'run_id': 'changed', 'status': 'completed', 'outputs': [{'kind': 'table', 'path': 'data.csv', 'sha256': '0' * 64}]}
    with pytest.raises(JobError): create_study_package(tmp_path, result, check_cancel=lambda: None)
    assert not list(tmp_path.glob('*.zip*'))
    result['outputs'][0]['path'] = '../escape.csv'
    with pytest.raises(JobError): create_study_package(tmp_path, result, check_cancel=lambda: None)


def test_temporal_history_dates_missing_values_and_original_text_are_preserved(tmp_path):
    from pyproj import CRS
    from test_temporal_integration_d import case, run
    data = case(periods=24, initial=8, uncertainty='none')
    X, y, ids, coordinates, groups, covariates, settings = data
    X[0, 0] = np.nan
    original = X.copy()
    settings['validation']['independent_ids'] = ids[-2:]
    settings['samples'] = {'target_field': 'target', 'unit': 'g/kg', 'date_field': 'date'}
    settings['covariates'] = covariates
    evaluation = run(data)
    samples = {'ids': ids, 'y': y, 'coordinates': coordinates, 'crs': CRS.from_epsg(32637),
               'props': [{'date': settings['validation']['dates_by_id'][identity], 'label': '=1+1',
                          'nested': {'number': 1, 'text': '01'}} for identity in ids]}
    paths, summary = write_analysis_exports(tmp_path, X, samples, groups, evaluation, settings, check_cancel=lambda: None)
    np.testing.assert_array_equal(X, original)
    raw = json.loads((tmp_path / 'sampled_data.json').read_text(encoding='utf-8'))['rows']
    flat = csv_rows(tmp_path / 'sampled_data.csv')
    assert raw[0]['covariates']['x0'] is None and flat[0]['covariate.x0'] == ''
    assert raw[0]['properties']['label'] == '=1+1' and flat[0]['property.label'] == "'=1+1"
    assert raw[0]['properties']['nested']['text'] == '01'
    assert all(row['observation_date'] == settings['validation']['dates_by_id'][row['sample_id']] for row in raw)
    assert all(row['validation_date'] == settings['validation']['dates_by_id'][row['sample_id']] for row in raw)
    roles = csv_rows(tmp_path / 'data_roles.csv')
    assert {r['sample_id'] for r in roles if r['partition'] == 'initial_training_history'} == set(ids[:16])
    assert {r['sample_id'] for r in roles if r['partition'] == 'independent_test'} == set(ids[-2:])
    assert not any(r['sample_id'] in ids[-2:] for r in roles if r['scope'] in ('outer', 'inner'))
    assert summary['samples'] == 48 and len(paths) == 10
    assert json.loads((tmp_path / 'data_dictionary.json').read_text())['missing_value']['reason']


def test_reopening_archive_detects_changed_member_even_with_valid_zip_crc(tmp_path, grid_run):
    output, _, _ = grid_run
    changed = tmp_path / 'changed.spsstudy.zip'
    with zipfile.ZipFile(output / 'study_package.spsstudy.zip') as source, zipfile.ZipFile(changed, 'w') as target:
        for name in source.namelist():
            value = source.read(name)
            if name == 'sampled_data.csv': value += b'changed'
            target.writestr(name, value)
    with pytest.raises(JobError, match='digest of a study package member differs'):
        inspect_study_package(changed)

