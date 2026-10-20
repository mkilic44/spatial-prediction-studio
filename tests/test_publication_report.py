"""Study exports preserve actual roles, undefined metrics and recorded settings."""
import copy
import csv
from html.parser import HTMLParser
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from sps.contract import JobError
from sps.publication_report import collect_study, write_publication_reports, FILES
from sps.reporting import build_reports
from test_reporting_b import report_case


def rows(ids, repeat=None, fold=None):
    return [{'sample_id': identity, 'group_id': identity, 'repeat': repeat, 'fold': fold,
             'observed': float(i), 'predicted': float(i + 1), 'residual': 1.0} for i, identity in enumerate(ids)]


def measures(n=3):
    return {'n': n, 'rmse': 1.0, 'mse': 1.0, 'mae': 1.0, 'median_absolute_error': 1.0,
            'mean_error': 1.0, 'residual_std': 0.0, 'r2': -0.5, 'pearson_r': 1.0, 'pearson_r2': 1.0,
            'lin_ccc': 4 / 7, 'rpd': 1.0, 'rpiq': 1.0, 'observed_std': 1.0, 'observed_iqr': 1.0, 'reasons': {}}


@pytest.fixture
def study_case(report_case):
    evaluation, samples, settings = copy.deepcopy(report_case)
    ids = samples['ids']
    settings.update(preprocessing={'imputation': 'median', 'scaling': 'standard'},
                    optimization={'method': 'grid', 'spaces': {'alpha': [0.25, 100.0]}})
    settings['model']['parameters']['alpha'] = 100.0
    record = evaluation['final_record']
    record.update(model={'id': 'ridge', 'parameters': {'alpha': 0.25}}, development_ids=ids[:3],
                  training_predictions=rows(ids[:3]), train_metrics=measures(),
                  calibration_predictions=rows(ids[4:6]), calibration_metrics={'n': 2, 'rmse': 1.0},
                  effective_estimator_class='sklearn.linear_model.Ridge',
                  effective_parameters={'alpha': 0.25, 'fit_intercept': True, 'solver': 'auto'},
                  target_transform={'method': 'none'}, selected_features=['elevation'])
    trial = {'role': 'final/search', 'trial': 0, 'model': record['model'], 'parameters': {'alpha': 0.25},
             'metric': 'rmse', 'value': 1.0, 'status': 'complete', 'validation_status': 'complete',
             'elapsed_seconds': 2.0, 'search_elapsed_seconds': 2.1, 'selected': True,
             'comparison_arm': 'with_selection', 'feature_selection_method': 'mutual_info',
             'validation_predictions': rows(ids[:3], 0, 0), 'validation_metrics': measures(),
             'validation_per_repeat': [{'repeat': 0, **measures()}],
             'inner_fold_metrics': [{'repeat': 0, 'fold': 0, 'metrics': measures(), 'effective_parameters': {'alpha': 0.25}}]}
    evaluation['trials'] = [trial]
    evaluation['study_context'] = {'started_utc': '2026-10-18T12:00:00Z', 'cpu_count': 8,
        'model_artifact': {'package_path': 'model_package.spsmodel.zip', 'sha256': 'a' * 64, 'roundtrip_verified': True},
        'sample_count': len(ids), 'group_count': len(ids), 'depth': [0, 20],
        'descriptive_statistics': [{'scope': 'final_training', 'variable': 'target', 'role': 'target', 'unit': 'g/kg',
            'kind': 'continuous', 'n': 3, 'finite_n': 3, 'missing_n': 0, 'mean': 1.0, 'std': 1.0,
            'q25': .5, 'median': 1.0, 'q75': 1.5, 'min': 0, 'max': 2, 'iqr': 1.0, 'unique_samples': 3, 'groups': 3}]}
    return evaluation, samples, settings


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def test_metric_roles_formulas_and_parameter_provenance_are_distinct(tmp_path, study_case):
    evaluation, samples, settings = study_case
    section = write_publication_reports(tmp_path, evaluation, samples, settings, {}, [], check_cancel=lambda: None)
    actual = read_csv(tmp_path / 'publication_metrics.csv')
    by_key = {(r['scope'], r['metric']): r for r in actual}
    assert float(by_key['final_training', 'r2']['value']) == -0.5
    assert float(by_key['final_training', 'pearson_r2']['value']) == 1.0
    assert float(by_key['inner_validation', 'lin_ccc']['value']) == pytest.approx(4 / 7)
    assert by_key['final_calibration', 'rmse']['scope_note'].startswith('Aralık ayarına')
    assert 'seçim yanlılığı' in by_key['inner_validation', 'r2']['scope_note']
    assert by_key['final_training', 'mse']['unit'] == '(g/kg)²'
    params = read_csv(tmp_path / 'hyperparameters.csv')
    assert any(r['kind'] == 'requested' and r['parameter'] == 'parameters.alpha' and r['value'] == '100.0' for r in params)
    assert any(r['kind'] == 'searched' and r['parameter'] == 'alpha' and r['value'] == '0.25' for r in params)
    assert any(r['kind'] == 'selected' and r['parameter'] == 'alpha' and r['value'] == '0.25' for r in params)
    assert any(r['kind'] == 'effective' and r['parameter'] == 'fit_intercept' and r['value'] == 'True' for r in params)
    assert 'R², Pearson r² değildir' in section
    assert 'böleni n olur' in section and 'h=(n-1)q' in section
    assert 'model_package.spsmodel.zip' in section
    assert 'href="model_package.spsmodel.zip"' not in section
    assert 'Modeli kaydet' in section


def test_predictions_keep_context_and_never_renumber_identity(tmp_path, study_case):
    evaluation, samples, settings = study_case
    write_publication_reports(tmp_path, evaluation, samples, settings, {}, [], check_cancel=lambda: None)
    training = read_csv(tmp_path / 'training_predictions.csv')
    calibration = read_csv(tmp_path / 'calibration_predictions.csv')
    inner = read_csv(tmp_path / 'inner_validation_predictions.csv')
    assert [r['sample_id'] for r in training] == samples['ids'][:3]
    assert [r['sample_id'] for r in calibration] == samples['ids'][4:6]
    assert all(r['scope'] == 'final_training' and r['role'] == 'final' for r in training)
    assert all(r['scope'] == 'inner_validation' and r['role'] == 'final/search' and r['fold'] == '0' and r['trial'] == '0' for r in inner)


def test_failed_trial_retains_partial_rows_without_publishing_success(study_case):
    evaluation, samples, settings = study_case
    trial = evaluation['trials'][0]
    trial.update(status='failed', validation_status='incomplete', validation_reason='second_fold_failed', selected=False)
    summary, predictions = collect_study(evaluation, samples, settings, {}, check_cancel=lambda: None)
    pooled = [r for r in summary['metrics'] if r['scope'] == 'inner_validation']
    assert pooled and all(r['value'] is None and r['reason'] == 'incomplete' for r in pooled)
    assert not any(r['scope'] == 'inner_repeat' for r in summary['metrics'])
    assert len(predictions['inner_validation']) == 3
    assert all(r['validation_status'] == 'incomplete' for r in predictions['inner_validation'])
    assert summary['candidates'][0]['status'] == 'failed'


def test_undefined_metric_reason_and_no_independent_test_are_explicit(tmp_path, study_case):
    evaluation, samples, settings = study_case
    evaluation['independent_predictions'] = []
    evaluation['final_record']['train_metrics'].update(rpd=None, rpiq=None, reasons={'rpd': 'zero_rmse', 'rpiq': 'zero_rmse'})
    write_publication_reports(tmp_path, evaluation, samples, settings, {}, [], check_cancel=lambda: None)
    result = json.loads((tmp_path / 'study_summary.json').read_text(encoding='utf-8'))
    assert result['independent_status'] == 'not_applicable'
    independent = [r for r in result['metrics'] if r['scope'] == 'independent']
    assert all(r['value'] is None and r['reason'] == 'not_applicable' for r in independent)
    assert next(r for r in result['metrics'] if r['scope'] == 'final_training' and r['metric'] == 'rpd')['reason'] == 'zero_rmse'
    assert 'Uygulanmadı; bağımsız test sonucu yoktur' in (tmp_path / 'study_report.html').read_text(encoding='utf-8')


def test_old_records_do_not_invent_parameters_or_hardware(report_case):
    evaluation, samples, settings = report_case
    summary, _ = collect_study(evaluation, samples, settings, {}, check_cancel=lambda: None)
    assert summary['study_context']['cpu_count'] is None
    assert summary['context_status']['cpu_count'] == 'not_recorded'
    assert any(r['kind'] == 'effective' and r['status'] == 'not_recorded' for r in summary['hyperparameters'])
    assert all(r['status'] == 'not_recorded' for r in summary['metrics'] if r['scope'] == 'final_training')


def test_repeats_do_not_inflate_unique_sample_count(study_case):
    evaluation, samples, settings = study_case
    evaluation['oof'] += [{**r, 'repeat': 1} for r in evaluation['oof']]
    evaluation['metrics']['pooled_oof'] = {'n': 12, 'rmse': 0.5}
    summary, _ = collect_study(evaluation, samples, settings, {}, check_cancel=lambda: None)
    row = next(r for r in summary['metrics'] if r['scope'] == 'pooled_oof' and r['metric'] == 'rmse')
    assert row['n'] == 12 and row['n_unique'] == 6 and row['group_count'] == 6
    assert summary['conventions']['repeats_are_independent_samples'] is False


@pytest.mark.parametrize('mutation', ['unknown', 'wrong_role', 'duplicate', 'residual'])
def test_invalid_prediction_records_fail_closed(study_case, mutation):
    evaluation, samples, settings = study_case
    data = evaluation['final_record']['training_predictions']
    if mutation == 'unknown': data[0]['sample_id'] = 'unknown'
    elif mutation == 'wrong_role': data[0]['sample_id'] = samples['ids'][5]
    elif mutation == 'duplicate': data.append(data[0].copy())
    else: data[0]['residual'] = -1.0
    with pytest.raises(JobError) as exc:
        collect_study(evaluation, samples, settings, {}, check_cancel=lambda: None)
    assert exc.value.code == 'REPORT_PUBLICATION_ROW'


def test_html_and_csv_escape_untrusted_names_without_changing_raw_record(tmp_path, study_case):
    evaluation, samples, settings = study_case
    malicious = '<script>bad()</script>'
    settings['samples']['target_field'] = malicious
    evaluation['final_record']['effective_parameters']['formula'] = '=1+1'
    write_publication_reports(tmp_path, evaluation, samples, settings, {}, [], check_cancel=lambda: None)
    page = (tmp_path / 'study_report.html').read_text(encoding='utf-8')
    assert malicious not in page and '&lt;script&gt;' in page
    raw = json.loads((tmp_path / 'study_summary.json').read_text(encoding='utf-8'))
    assert raw['sample_settings']['target_field'] == malicious
    assert any(r['parameter'] == 'formula' and r['value'] == "'=1+1" for r in read_csv(tmp_path / 'hyperparameters.csv'))


def test_report_build_publishes_study_artifacts_and_shared_local_figures(tmp_path, study_case):
    evaluation, samples, settings = study_case
    produced = build_reports(tmp_path, evaluation, samples, settings, check_cancel=lambda: None)
    assert set(FILES) <= {p.name for p in produced}
    page = (tmp_path / 'study_report.html').read_text(encoding='utf-8')
    normal = (tmp_path / 'report.html').read_text(encoding='utf-8')
    assert '<section id="study-report">' in page and '<section id="study-report">' in normal
    class Images(HTMLParser):
        def __init__(self): super().__init__(); self.paths=[]
        def handle_starttag(self, tag, attrs):
            if tag == 'img': self.paths.append(dict(attrs)['src'])
    parser = Images(); parser.feed(page)
    assert parser.paths and all((tmp_path / p).is_file() for p in parser.paths)
    assert 'default-src' in page and '<script' not in page
    assert not list(tmp_path.glob('.report-*'))


def test_study_failure_rolls_back_existing_report_transaction(tmp_path, study_case):
    evaluation, samples, settings = study_case
    evaluation['final_record']['training_predictions'][0]['sample_id'] = 'unknown'
    with pytest.raises(JobError):
        build_reports(tmp_path, evaluation, samples, settings, check_cancel=lambda: None)
    assert list(tmp_path.iterdir()) == []


def test_remote_model_or_figure_link_is_refused(tmp_path, study_case):
    evaluation, samples, settings = study_case
    evaluation['study_context']['model_artifact']['package_path'] = 'https://example.org/model'
    with pytest.raises(JobError) as exc:
        write_publication_reports(tmp_path, evaluation, samples, settings, {}, [], check_cancel=lambda: None)
    assert exc.value.code == 'REPORT_MODEL_LINK'


def test_real_coordinate_reference_object_is_serializable(tmp_path, study_case):
    from pyproj import CRS
    evaluation, samples, settings = study_case
    samples['crs'] = CRS.from_epsg(32637)
    write_publication_reports(tmp_path, evaluation, samples, settings, {}, [], check_cancel=lambda: None)
    summary = json.loads((tmp_path / 'study_summary.json').read_text(encoding='utf-8'))
    assert CRS.from_wkt(summary['sample_description']['crs']) == samples['crs']


@pytest.mark.parametrize('available', [0, 12])
def test_partial_or_unavailable_interval_keeps_both_denominators(study_case, available):
    evaluation, samples, settings = study_case
    interval = {'status': 'partial' if available else 'unavailable', 'n': 20, 'available_n': available,
        'unavailable_n': 20 - available, 'available_group_count': 6 if available else 0,
        'coverage_denominator': available, 'availability_denominator': 20, 'reason_code': 'PARTIAL_SPATIAL_SUPPORT' if available else 'NO_SPATIAL_INTERVALS'}
    if available: interval['available_subset'] = {'coverage': .75, 'mean_width': 2.0, 'interval_score': 3.0}
    evaluation['metrics']['pooled_oof']['interval'] = interval
    summary, _ = collect_study(evaluation, samples, settings, {}, check_cancel=lambda: None)
    row = next(r for r in summary['metrics'] if r['scope'] == 'pooled_oof' and r['metric'] == 'interval.coverage')
    assert row['coverage_denominator'] == available and row['availability_denominator'] == 20
    assert row['interval_scope'] == 'available_intervals_only'
    assert row['value'] == (.75 if available else None)
    assert row['status'] == ('available_subset' if available else 'unavailable')
    assert 'coverage' not in interval


def test_fitted_coefficients_and_actual_producer_parameters_are_exported(study_case):
    evaluation, samples, settings = study_case
    record = evaluation['final_record']
    record['experimental_model_diagnostics'] = {'producer_parameters': {'constructor': {'width': 8}, 'optimizer_defaults': {'lr': .001}}}
    record['fitted_model_attributes'] = {'status': 'available', 'attributes': {'coef_': [1.5], 'intercept_': 3.0}, 'input_features': ['elevation'], 'scope': 'encoded_scaled_inputs'}
    summary, _ = collect_study(evaluation, samples, settings, {}, check_cancel=lambda: None)
    assert any(r['component'] == 'producer' and r['parameter'] == 'optimizer_defaults.lr' and r['value'] == .001 for r in summary['hyperparameters'])
    assert any(r['kind'] == 'fitted_attribute' and r['parameter'] == 'attributes.coef_' and r['value'] == [1.5] for r in summary['hyperparameters'])


def test_actual_ridge_run_exports_study_context_and_recomputable_training_error(tmp_path):
    from test_engine_b import request, run_request
    req = request(tmp_path)
    req['uncertainty'] = {'method': 'split_conformal', 'coverage': .8, 'calibration_fraction': .3}
    result, _ = run_request(req, tmp_path)
    assert result['status'] == 'completed', result
    output = tmp_path / 'run'
    summary = json.loads((output / 'study_summary.json').read_text(encoding='utf-8'))
    assert summary['context_status']['source_code'] == 'recorded'
    assert summary['study_context']['sample_crs']
    assert summary['study_context']['model_artifact']['roundtrip_verified'] is True
    actual = [r for r in read_csv(output / 'training_predictions.csv') if r['scope'] == 'final_training']
    expected = (sum((float(r['predicted']) - float(r['observed'])) ** 2 for r in actual) / len(actual)) ** .5
    metric = next(r for r in summary['metrics'] if r['scope'] == 'final_training' and r['metric'] == 'rmse')
    assert metric['value'] == pytest.approx(expected)
    assert metric['n_unique'] == len(actual)
    assert read_csv(output / 'calibration_predictions.csv')
    assert read_csv(output / 'inner_validation_predictions.csv')
    assert set(FILES) <= {Path(r['path']).name for r in result['outputs']}


@pytest.mark.parametrize('bad', [None, float('nan'), float('inf'), True, '1.0'])
def test_recorded_training_rows_require_finite_numeric_values(study_case, bad):
    evaluation, samples, settings = study_case
    evaluation['final_record']['training_predictions'][0]['observed'] = bad
    with pytest.raises(JobError) as exc:
        collect_study(evaluation, samples, settings, {}, check_cancel=lambda: None)
    assert exc.value.code == 'REPORT_PUBLICATION_ROW'


@pytest.mark.parametrize('role', ['training', 'calibration'])
def test_recorded_prediction_lists_must_cover_the_exact_partition(study_case, role):
    evaluation, samples, settings = study_case
    evaluation['final_record'][role + '_predictions'].pop()
    with pytest.raises(JobError) as exc:
        collect_study(evaluation, samples, settings, {}, check_cancel=lambda: None)
    assert exc.value.code == 'REPORT_PUBLICATION_ROW'


def test_fold_average_has_average_formula_and_defined_fold_count(study_case):
    evaluation, samples, settings = study_case
    evaluation['metrics']['fold_mean'] = {'r2': -.5, 'r2_defined_folds': 2, 'pearson_r': None, 'pearson_r_defined_folds': 0}
    summary, _ = collect_study(evaluation, samples, settings, {}, check_cancel=lambda: None)
    metric = next(r for r in summary['metrics'] if r['scope'] == 'fold_mean' and r['metric'] == 'r2')
    assert metric['formula'] == 'mean(defined outer-fold r2)' and metric['defined_fold_count'] == 2
    missing = next(r for r in summary['metrics'] if r['scope'] == 'fold_mean' and r['metric'] == 'pearson_r')
    assert missing['value'] is None and missing['reason'] == 'no_defined_folds'


class PageContent(HTMLParser):
    """Read public HTML semantics independently of the producer's tree classes."""
    def __init__(self, source):
        super().__init__()
        self.rows, self.total_rows, self.links, self.images = [], 0, [], []
        self.in_body, self.in_cell, self.current_row, self.cell = False, False, [], []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'tbody': self.in_body = True
        if tag == 'tr':
            self.total_rows += 1
            if self.in_body: self.current_row = []
        if tag == 'td' and self.in_body: self.in_cell = True; self.cell = []
        if tag == 'a': self.links.append(attrs.get('href'))
        if tag == 'img': self.images.append(attrs.get('src'))

    def handle_data(self, value):
        if self.in_cell: self.cell.append(value)

    def handle_endtag(self, tag):
        if tag == 'td' and self.in_body:
            self.current_row.append(''.join(self.cell)); self.in_cell = False
        if tag == 'tr' and self.in_body: self.rows.append(self.current_row)
        if tag == 'tbody': self.in_body = False


def test_paged_html_preserves_every_row_and_figure_in_order(tmp_path, study_case):
    from sps.publication_report import PAGE_INDEX, PAGE_ROW_LIMIT
    evaluation, samples, settings = study_case
    # A large, distinct parameter table forces more than one page in a section.
    evaluation['final_record']['effective_parameters'].update({'unique_parameter_%03d' % i: i for i in range(361)})
    artifacts = build_reports(tmp_path, evaluation, samples, settings, check_cancel=lambda: None)
    by_name = {p.name for p in artifacts}
    summary = json.loads((tmp_path / 'study_summary.json').read_text(encoding='utf-8'))
    info = summary['pagination']
    original = PageContent((tmp_path / 'study_report.html').read_text(encoding='utf-8'))
    # report.html embeds the study once; its other tables must also remain reachable.
    main_text = (tmp_path / 'report.html').read_text(encoding='utf-8')
    before, embedded = main_text.split('<section id="study-report">', 1)
    after = embedded.split('</section>', 1)[1]
    supplement = PageContent(before + after)
    page_rows, images = [], []
    assert info['index'] == PAGE_INDEX and info['all_rows_preserved_in_order'] is True
    assert info['source_data_rows'] == len(original.rows) + len(supplement.rows)
    assert info['table_row_limit_including_headers'] == PAGE_ROW_LIMIT == 120
    assert len(info['pages']) > 15
    for i, record in enumerate(info['pages']):
        path = tmp_path / record['file']
        page = PageContent(path.read_text(encoding='utf-8'))
        assert record['file'] in by_name and record['file'] in summary['files']
        assert page.total_rows == record['table_rows'] <= 120
        assert len(page.rows) == record['data_rows']
        assert len(page.images) <= 1
        assert PAGE_INDEX in page.links
        if i: assert info['pages'][i - 1]['file'] in page.links
        if i + 1 < len(info['pages']): assert info['pages'][i + 1]['file'] in page.links
        assert not {'study_report.html', 'report.html'} & set(page.links)
        for target in page.links + page.images:
            assert ':' not in target and '..' not in Path(target).parts
            assert (tmp_path / target).is_file()
        page_rows.extend(page.rows); images.extend(page.images)
    assert page_rows == original.rows + supplement.rows
    assert images == original.images
    index = PageContent((tmp_path / PAGE_INDEX).read_text(encoding='utf-8'))
    assert index.links == [p['file'] for p in info['pages']]
    assert index.images == [] and index.total_rows == 0
    assert (tmp_path / PAGE_INDEX).stat().st_size < 16000


def test_pagination_repeats_headers_and_preserves_context_for_long_table(tmp_path):
    from sps.publication_report import write_paged_study
    table = '<div class="study-table"><table><thead><tr><th>Kimlik</th><th>Değer</th></tr></thead><tbody>' + ''.join(
        '<tr><td>%s</td><td>&lt;değer&amp;%s&gt;</td></tr>' % (i, i) for i in range(244)) + '</tbody></table></div>'
    section = '<section><h2>Çalışma</h2><p>Genel açıklama.</p><h3>İç doğrulama</h3><p>Seçim yanlılığı vardır.</p><h4>Aday 1</h4>' + table + '</section>'
    info = write_paged_study(tmp_path, section, [], check_cancel=lambda: None)
    parts = [p for p in info['pages'] if p['title'] == 'İç doğrulama']
    assert len(parts) == 3
    actual = []
    for part in parts:
        text = (tmp_path / part['file']).read_text(encoding='utf-8')
        assert '<th>Kimlik</th><th>Değer</th>' in text
        assert 'Seçim yanlılığı vardır.' in text and '<h4>Aday 1</h4>' in text
        page = PageContent(text)
        assert page.total_rows <= 120
        actual.extend(page.rows)
    assert actual == [[str(i), '<değer&%d>' % i] for i in range(244)]


def test_main_report_explanations_and_permutation_rows_remain_accessible(tmp_path, study_case):
    evaluation, samples, settings = study_case
    importance = [{'repeat': i, 'feature': 'covariate_%03d' % i, 'metric': 'rmse',
                   'importance_mean': i / 100, 'status': 'available'} for i in range(245)]
    evaluation['permutation_importance'] = {'rows': importance, 'per_repeat': importance}
    build_reports(tmp_path, evaluation, samples, settings, check_cancel=lambda: None)
    summary = json.loads((tmp_path / 'study_summary.json').read_text(encoding='utf-8'))
    records = summary['pagination']['pages']
    parts = [p for p in records if 'Değişkenleri karıştırarak model açıklama' in p['title']]
    assert len(parts) == 3
    rows = []
    for part in parts:
        text = (tmp_path / part['file']).read_text(encoding='utf-8')
        assert 'Sonuç nedensel değildir' in text and 'güven aralığı değildir' in text
        content = PageContent(text)
        assert content.total_rows <= 120
        rows.extend(content.rows)
    assert [r[1] for r in rows] == [r['feature'] for r in importance]
    texts = [(tmp_path / p['file']).read_text(encoding='utf-8') for p in records]
    assert any('Shapley katkı açıklamaları' in text for text in texts)
    assert any('href="final_record.json"' in text for text in texts)
    assert any('href="evaluation.json"' in text for text in texts)
    assert all('<pre>' not in text for text in texts)
    full = (tmp_path / 'report.html').read_text(encoding='utf-8')
    assert '<pre>' in full and 'covariate_244' in full


def test_cancellation_during_pagination_removes_all_staged_reports(tmp_path, study_case, monkeypatch):
    from sps import publication_report
    from sps.contract import Cancelled
    original = publication_report.write_paged_study
    def interrupt(folder, section, figures, *, check_cancel, supplemental_report=''):
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            if any(Path(folder).glob('study_report_page_*.html')): raise Cancelled()
        return original(folder, section, figures, check_cancel=cancel, supplemental_report=supplemental_report)
    monkeypatch.setattr(publication_report, 'write_paged_study', interrupt)
    with pytest.raises(Cancelled):
        build_reports(tmp_path, *study_case, check_cancel=lambda: None)
    assert not list(tmp_path.iterdir())


def test_real_run_pagination_is_hashed_and_model_archive_keeps_summary(tmp_path):
    import hashlib
    import zipfile
    from test_engine_b import request, run_request
    from sps.model_bundle import inspect_model_package
    result, _ = run_request(request(tmp_path), tmp_path)
    assert result['status'] == 'completed', result
    folder = tmp_path / 'run'
    summary = json.loads((folder / 'study_summary.json').read_text(encoding='utf-8'))
    pages = [summary['pagination']['index']] + [r['file'] for r in summary['pagination']['pages']]
    artifacts = {row['path']: row for row in result['outputs']}
    for page in pages:
        assert artifacts[page]['kind'] == 'report'
        assert hashlib.sha256((folder / page).read_bytes()).hexdigest() == artifacts[page]['sha256']
    package = folder / summary['study_context']['model_artifact']['package_path']
    assert inspect_model_package(package)['status'] == 'verified'
    with zipfile.ZipFile(package) as archive:
        packaged = json.loads(archive.read('study/study_summary.json'))
        assert packaged['pagination'] == summary['pagination']
        assert not any(name.endswith('.html') for name in archive.namelist())
        assert 'HTML pages are not embedded' in packaged['pagination']['scope']
