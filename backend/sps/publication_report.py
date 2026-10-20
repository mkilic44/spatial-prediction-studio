"""Readable study records from evaluated data. Never fit, select or impute evidence."""
from __future__ import annotations

from collections import Counter
import csv
import hashlib
import html
from html.parser import HTMLParser
import json
import math
from pathlib import Path

from .contract import JobError


METRICS = {
    "rmse": ("Ortalama kare hatanın karekökü (RMSE)", "sqrt(sum(e_i^2)/n)", "target"),
    "mse": ("Ortalama kare hata (MSE)", "sum(e_i^2)/n", "squared"),
    "mae": ("Ortalama mutlak hata (MAE)", "sum(abs(e_i))/n", "target"),
    "median_absolute_error": ("Medyan mutlak hata", "median(abs(e_i))", "target"),
    "mean_error": ("Ortalama hata", "sum(e_i)/n", "target"),
    "residual_std": ("Artıkların örnek standart sapması", "sqrt(sum((e_i-mean(e))^2)/(n-1))", "target"),
    "r2": ("Belirleme katsayısı (R²)", "1-sum((p_i-y_i)^2)/sum((y_i-mean(y))^2)", "dimensionless"),
    "pearson_r": ("Pearson korelasyon katsayısı (r)", "sum((y_i-mean(y))*(p_i-mean(p)))/sqrt(sum((y_i-mean(y))^2)*sum((p_i-mean(p))^2))", "dimensionless"),
    "pearson_r2": ("Pearson korelasyon katsayısının karesi (r²)", "pearson_r^2", "dimensionless"),
    "lin_ccc": ("Lin uyum korelasyon katsayısı (CCC)", "2*cov_n(y,p)/(var_n(y)+var_n(p)+(mean(y)-mean(p))^2)", "dimensionless"),
    "rpd": ("Gözlenen örnek standart sapmasının RMSE değerine oranı (RPD)", "s_y/RMSE; s_y=sqrt(sum((y_i-mean(y))^2)/(n-1))", "dimensionless"),
    "rpiq": ("Çeyrekler arası aralığın RMSE değerine oranı (RPIQ)", "(Q_0.75(y)-Q_0.25(y))/RMSE", "dimensionless"),
    "observed_std": ("Gözlenen örnek standart sapması", "sqrt(sum((y_i-mean(y))^2)/(n-1))", "target"),
    "observed_iqr": ("Gözlenen çeyrekler arası aralık", "Q_0.75(y)-Q_0.25(y)", "target"),
}
INTERVAL_METRICS = {
    "coverage": ("Ampirik kapsama oranı", "sum(I(lower_i<=y_i<=upper_i))/n", "dimensionless"),
    "mean_width": ("Ortalama tahmin aralığı genişliği", "sum(upper_i-lower_i)/n", "target"),
    "median_width": ("Medyan tahmin aralığı genişliği", "median(upper_i-lower_i)", "target"),
    "below_lower_fraction": ("Alt sınırın altında kalma oranı", "sum(I(y_i<lower_i))/n", "dimensionless"),
    "above_upper_fraction": ("Üst sınırın üstünde kalma oranı", "sum(I(y_i>upper_i))/n", "dimensionless"),
    "interval_score": ("Aralık skoru", "mean((upper-lower)+(2/alpha)*(lower-y)*I(y<lower)+(2/alpha)*(y-upper)*I(y>upper)); alpha=1-coverage_requested", "target"),
    "pinball_lower": ("Alt kantil kaybı", "mean(max(tau*(y-q),(tau-1)*(y-q))); tau=alpha/2", "target"),
    "pinball_upper": ("Üst kantil kaybı", "mean(max(tau*(y-q),(tau-1)*(y-q))); tau=1-alpha/2", "target"),
}
SCOPE = {
    "outer_training": "Dış kat modelinin eğitim verisi", "final_training": "Nihai modelin eğitim verisi",
    "outer_calibration": "Dış kat modelinin kalibrasyon tanısı", "final_calibration": "Nihai modelin kalibrasyon tanısı",
    "inner_validation": "İç doğrulama: denemenin birleşik sonucu", "inner_repeat": "İç doğrulama: tekrar",
    "inner_fold": "İç doğrulama: kat", "outer_fold": "Dış değerlendirme: kat",
    "per_repeat": "Dış değerlendirme: tekrar", "pooled_oof": "Dış değerlendirme: birleştirilmiş kayıtlar",
    "fold_mean": "Dış kat ölçütlerinin aritmetik ortalaması", "independent": "Kilitli bağımsız test",
}
SCOPE_NOTES = {
    "training": "Eğitim verisine yeniden uygulama; doğrulama başarısı değildir.",
    "calibration": "Aralık ayarına katılan hedeflerde tanı; bağımsız doğrulama değildir.",
    "inner": "Model ve ayar seçimine katılmıştır; seçim yanlılığı nedeniyle nihai başarı değildir.",
    "pooled_oof": "Tekrarlanan kimliklerin tahminleri ayrı kayıttır; bağımsız gözlem sayısını artırmaz.",
    "fold_mean": "Kat ölçütlerinin ortalamasıdır; birleşik tahminlerden hesaplanan ölçüt değildir.",
    "independent": "Kilitli test; yeniden kullanım ve ayar değişikliği geçmişi ayrıca incelenmelidir.",
}
LABELS = {
    "method": "Yöntem", "id": "Kimlik", "unit": "Birim", "target_field": "Hedef alanı", "id_field": "Kimlik alanı",
    "group_fields": "Gruplama alanları", "date_field": "Tarih alanı", "depth_top_field": "Üst derinlik alanı",
    "depth_bottom_field": "Alt derinlik alanı", "method_field": "Laboratuvar yöntemi alanı", "support": "Örnek desteği",
    "support_note": "Örnek desteğinin açıklaması", "n_splits": "Dış kat sayısı", "inner_splits": "İç kat sayısı",
    "repeats": "Tekrar sayısı", "seed": "Rastgelelik tohumu", "block_size": "Blok büyüklüğü",
    "buffer_distance": "Tampon uzaklığı", "gap_days": "Takvim boşluğu (gün)", "timezone": "Saat dilimi",
    "crs": "Koordinat sistemi", "metric_crs": "Uzaklık hesabının koordinat sistemi", "purpose": "Doğrulama amacı",
    "initial_train_periods": "Başlangıç eğitim dönemi sayısı", "inner_initial_train_periods": "İç başlangıç dönem sayısı",
    "selector_initial_train_periods": "Seçici başlangıç dönem sayısı", "temporal_mode": "Zamansal değerlendirme türü",
    "coverage": "İstenen kapsama oranı", "calibration_fraction": "İstenen kalibrasyon payı",
    "imputation": "Eksik değer tamamlama", "scaling": "Ölçekleme", "unknown_category": "Bilinmeyen kategori davranışı",
    "mode": "Çalışma biçimi", "threshold": "Eşik", "min_train_groups": "En az eğitim grubu",
    "started_utc": "Başlangıç zamanı (UTC)", "finished_analysis_utc": "Analiz bitiş zamanı (UTC)",
    "elapsed_analysis_seconds": "Analiz süresi (s)", "cpu_count": "Mantıksal işlemci sayısı",
    "python": "Python sürümü", "executable": "Python yorumlayıcısı", "platform": "İşletim sistemi",
    "path": "Dosya yolu", "sha256": "SHA-256 dosya özeti", "version": "Sürüm", "status": "Durum",
    "n": "Kayıt sayısı", "mean": "Ortalama", "std": "Standart sapma", "min": "En küçük", "max": "En büyük",
    "median": "Medyan", "q25": "Birinci çeyrek", "q75": "Üçüncü çeyrek", "missing": "Eksik kayıt",
}
REASONS = {
    "not_recorded": "Kaydedilmedi", "not_applicable": "Uygulanmadı", "no_observations": "Gözlem yok",
    "constant_observed_target": "Gözlenen hedef sabit", "constant_prediction": "Tahminler sabit",
    "constant_predictions": "Tahminler sabit", "numerical_overflow": "Sayısal taşma",
    "zero_ccc_denominator": "Lin katsayısının paydası sıfır",
    "fewer_than_two_observations": "İkiden az gözlem", "zero_rmse": "Ortalama kare hatanın karekökü sıfır",
    "incomplete": "Deneme tamamlanmadı", "nonfinite": "Sonlu olmayan değer",
}
FILES = ["study_report.html", "study_summary.json", "publication_metrics.csv", "hyperparameters.csv",
         "inner_validation_predictions.csv", "training_predictions.csv", "calibration_predictions.csv"]
PAGE_INDEX = 'study_report_index.html'
PAGE_ROW_LIMIT = 120
PAGE_SOFT_BYTES = 64 * 1024


def _text(value):
    if value is None:
        return "Kaydedilmedi veya tanımsız"
    if isinstance(value, bool):
        return "Evet" if value else "Hayır"
    if isinstance(value, float):
        return format(value, ".10g")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    return str(value)


def _table(headers, rows):
    return '<div class="study-table"><table><thead><tr>' + ''.join('<th>' + html.escape(str(h)) + '</th>' for h in headers) + \
        '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + html.escape(_text(v)) + '</td>' for v in row) + '</tr>' for row in rows) + '</tbody></table></div>'


def _flatten(value, prefix=""):
    if isinstance(value, dict):
        if not value:
            yield prefix, {}
        for key, item in value.items():
            yield from _flatten(item, prefix + "." + str(key) if prefix else str(key))
    elif isinstance(value, list) and any(isinstance(v, dict) for v in value):
        for i, item in enumerate(value):
            yield from _flatten(item, prefix + "[%d]" % i)
    else:
        yield prefix, value


def _kv(value):
    if value is None:
        return '<p>Kaydedilmedi (not_recorded).</p>'
    rows = []
    for key, item in _flatten(value):
        if isinstance(item, list) and len(item) > 24:
            item = ", ".join(_text(v) for v in item[:24]) + " ... (%d kayıt; tam liste study_summary.json ve evaluation.json içinde)" % len(item)
        rows.append((" / ".join(LABELS.get(part, part) for part in key.split('.')), item))
    return _table(["Alan", "Kaydedilen değer"], rows)


def _write_csv(path, rows, fields):
    columns = list(fields)
    for row in rows:
        columns.extend(key for key in row if key not in columns)
    def cell(value):
        if value is None:
            return ""
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r', '\n')):
            return "'" + value
        return value
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows({key: cell(value) for key, value in row.items()} for row in rows)


def _model_records(evaluation):
    for fold in evaluation.get('fold_metrics', []):
        if fold.get('model_record'):
            yield {'scope': 'outer', 'role': 'outer/%s/%s' % (fold.get('repeat'), fold.get('fold')),
                   'repeat': fold.get('repeat'), 'fold': fold.get('fold')}, fold['model_record']
    if evaluation.get('final_record'):
        yield {'scope': 'final', 'role': 'final', 'repeat': None, 'fold': None}, evaluation['final_record']


def _note(scope):
    return next((value for key, value in SCOPE_NOTES.items() if key in scope), "Yalnız belirtilen bölümde hesaplanan sonuç.")


def _metric_rows(metrics, context, unit, predictions=(), missing_reason='not_recorded'):
    metrics = metrics or {}
    reason_map = metrics.get('reasons') or {}
    for key, (label, formula, scale) in METRICS.items():
        alias = 'me' if key == 'mean_error' and 'mean_error' not in metrics else key
        value = metrics.get(alias)
        reason = reason_map.get(key) or (metrics.get('r2_reason') if key == 'r2' else None)
        defined_folds = metrics.get(key + '_defined_folds') if context['scope'] == 'fold_mean' else None
        if value is None:
            reason = reason or ('no_defined_folds' if defined_folds == 0 else missing_reason)
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and not math.isfinite(value):
            value, reason = None, 'nonfinite'
        yield {**context, 'metric': key, 'label': label, 'value': value, 'unit': unit if scale == 'target' else ('(' + unit + ')²' if scale == 'squared' else '1'),
               'status': 'available' if value is not None else reason, 'reason': reason,
               'formula': 'mean(defined outer-fold ' + key + ')' if context['scope'] == 'fold_mean' else formula,
               'defined_fold_count': defined_folds,
               'n': metrics.get('n', len(predictions) if predictions else None),
               'n_unique': len({r['sample_id'] for r in predictions}) if predictions else metrics.get('n_unique'),
               'group_count': len({r['group_id'] for r in predictions if r.get('group_id') is not None}) if predictions else None,
               'scope_note': _note(context['scope'])}
    interval = metrics.get('interval') or {}
    subset = interval.get('available_subset')
    partial = isinstance(subset, dict)
    interval_values = subset if partial else interval
    unavailable = interval.get('status') == 'unavailable'
    for key, (label, formula, scale) in INTERVAL_METRICS.items():
        if key not in interval_values and not unavailable:
            continue
        value = None if unavailable else interval_values[key]
        reason = interval.get('reason_code') or interval.get('reason') or 'not_recorded' if value is None else None
        denominator = interval.get('coverage_denominator', interval.get('available_n') if partial else interval.get('n', metrics.get('n')))
        yield {**context, 'metric': 'interval.' + key, 'label': label, 'value': value,
               'unit': unit if scale == 'target' else '1', 'status': 'available_subset' if partial and value is not None else 'available' if value is not None else 'unavailable',
               'reason': reason, 'formula': formula,
               'n': denominator, 'n_unique': None, 'group_count': interval.get('available_group_count') if partial else interval.get('group_count'),
               'interval_scope': 'available_intervals_only' if partial or unavailable else 'all_evaluated_intervals',
               'interval_status': interval.get('status', 'available'), 'coverage_denominator': denominator,
               'availability_denominator': interval.get('availability_denominator', interval.get('n', metrics.get('n'))),
               'available_n': interval.get('available_n'), 'unavailable_n': interval.get('unavailable_n'),
               'scope_note': _note(context['scope']) + (' Aralık ölçütleri yalnız aralığı hesaplanan gözlemlere aittir.' if partial or unavailable else '')}


def _prediction_rows(rows, context, known, expected=None):
    seen = set()
    for original in rows:
        row = {**original, **context}
        # Inner repeat/fold refer to the inner split; outer scope remains in role.
        if context.get('scope') == 'inner_validation':
            row['repeat'], row['fold'] = original.get('repeat'), original.get('fold')
        identity = row.get('sample_id')
        key = (row.get('repeat'), row.get('fold'), identity)
        if identity not in known or key in seen or (expected is not None and identity not in expected):
            raise JobError('REPORT_PUBLICATION_ROW', 'Ayrıntılı raporda tahmin kimliği veya bölüm üyeliği tutarsız.')
        seen.add(key)
        values = [row.get(k) for k in ('observed', 'predicted', 'residual')]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values):
            raise JobError('REPORT_PUBLICATION_ROW', 'Kaydedilmiş nokta tahmininde gözlem, tahmin ve artık sonlu sayılar olmalıdır.')
        if not math.isclose(values[2], values[1] - values[0], rel_tol=1e-9, abs_tol=1e-10):
            raise JobError('REPORT_PUBLICATION_ROW', 'Ayrıntılı raporda artık, tahmin eksi gözleme eşit değil.')
        yield row
    if expected is not None and {key[2] for key in seen} != expected:
        raise JobError('REPORT_PUBLICATION_ROW', 'Kaydedilmiş nokta tahminleri ilgili bölümün bütün kimliklerini içermelidir.')


def _parameter_rows(value, context, kind, component):
    if value is None:
        yield {**context, 'kind': kind, 'component': component, 'parameter': None, 'value': None, 'status': 'not_recorded'}
    elif not value:
        yield {**context, 'kind': kind, 'component': component, 'parameter': None, 'value': value, 'status': 'recorded_empty'}
    else:
        for key, item in _flatten(value):
            yield {**context, 'kind': kind, 'component': component, 'parameter': key, 'value': item, 'status': 'recorded'}


def collect_study(evaluation, samples, settings, counts, *, check_cancel):
    """Build tables from records. Missing old-schema evidence stays not_recorded."""
    unit = str(settings.get('samples', {}).get('unit', 'birim kaydedilmedi'))
    known = set(samples.get('ids', []))
    metrics, parameters, candidates = [], [], []
    predictions = {'training': [], 'calibration': [], 'inner_validation': []}
    records = list(_model_records(evaluation))
    for context, record in records:
        check_cancel()
        model_id = record.get('model', {}).get('id')
        for name, key in [('training', 'train_metrics'), ('calibration', 'calibration_metrics')]:
            row_context = {**context, 'scope': context['scope'] + '_' + name, 'model_id': model_id}
            ids_key = 'development_ids' if name == 'training' else 'calibration_ids'
            expected = set(record[ids_key]) if ids_key in record and name + '_predictions' in record else None
            rows = list(_prediction_rows(record.get(name + '_predictions', []), row_context, known, expected))
            predictions[name].extend(rows)
            reason = 'not_applicable' if name == 'calibration' and expected == set() else 'not_recorded'
            metrics.extend(_metric_rows(record.get(key), row_context, unit, rows, reason))
        context = {**context, 'model_id': model_id}
        for component, value in [('model', record.get('model', {}).get('parameters', record.get('parameters'))),
                                 ('target_transform', record.get('target_transform')), ('preprocessing', record.get('preprocessing')),
                                 ('selection_method', record.get('selection_method')), ('selected_features', record.get('selected_features', record.get('selected_ids')))]:
            parameters.extend(_parameter_rows(value, context, 'selected', component))
        for component, key in [('estimator', 'effective_parameters'), ('quantile_estimators', 'quantile_effective_parameters')]:
            parameters.extend(_parameter_rows(record.get(key), {**context, 'estimator_class': record.get('effective_estimator_class'),
                'parameter_source': record.get('effective_parameters_source'), 'parameter_scope': record.get('effective_parameters_scope')}, 'effective', component))
        parameters.extend(_parameter_rows(record.get('experimental_model_diagnostics', {}).get('producer_parameters'), context, 'effective', 'producer'))
        parameters.extend(_parameter_rows(record.get('fitted_model_attributes'), context, 'fitted_attribute', 'fitted_model'))
    for component in ('model', 'model_candidates', 'preprocessing', 'target_transform', 'vif', 'feature_selection', 'validation', 'uncertainty', 'diagnostics', 'mapping'):
        parameters.extend(_parameter_rows(settings.get(component), {'scope': 'requested', 'role': 'request'}, 'requested', component))
    parameters.extend(_parameter_rows(settings.get('optimization', {}).get('spaces'), {'scope': 'requested', 'role': 'request'}, 'search_space', 'optimization'))
    for trial in evaluation.get('trials', []):
        check_cancel()
        context = {'scope': 'inner_validation', 'role': trial.get('role'), 'trial': trial.get('trial'), 'model_id': trial.get('model', {}).get('id')}
        rows = list(_prediction_rows(trial.get('validation_predictions', []), context, known))
        predictions['inner_validation'].extend({**r, 'validation_status': trial.get('validation_status', 'not_recorded'), 'selected': trial.get('selected')} for r in rows)
        incomplete = trial.get('validation_status') == 'incomplete' or trial.get('status') == 'failed'
        metrics.extend(_metric_rows(None if incomplete else trial.get('validation_metrics'), context, unit, rows, 'incomplete' if incomplete else 'not_recorded'))
        if not incomplete:
            for row in trial.get('validation_per_repeat', []):
                relevant = [r for r in rows if r.get('repeat') == row.get('repeat')]
                metrics.extend(_metric_rows(row, {**context, 'scope': 'inner_repeat', 'repeat': row.get('repeat')}, unit, relevant))
        for fold in trial.get('inner_fold_metrics', []):
            fold_context = {**context, 'scope': 'inner_fold', 'repeat': fold.get('repeat'), 'fold': fold.get('fold')}
            relevant = [r for r in rows if r.get('repeat') == fold.get('repeat') and r.get('fold') == fold.get('fold')]
            metrics.extend(_metric_rows(fold.get('metrics'), fold_context, unit, relevant))
            parameters.extend(_parameter_rows(fold.get('effective_parameters'), {**fold_context, 'estimator_class': fold.get('effective_estimator_class'),
                'parameter_source': fold.get('effective_parameters_source'), 'parameter_scope': fold.get('effective_parameters_scope')}, 'effective', 'estimator'))
            parameters.extend(_parameter_rows(fold.get('quantile_effective_parameters'), fold_context, 'effective', 'quantile_estimators'))
            parameters.extend(_parameter_rows(fold.get('experimental_model_diagnostics', {}).get('producer_parameters'), fold_context, 'effective', 'producer'))
            parameters.extend(_parameter_rows(fold.get('fitted_model_attributes'), fold_context, 'fitted_attribute', 'fitted_model'))
        parameters.extend(_parameter_rows(trial.get('parameters'), context, 'searched', 'optimization'))
        parameters.extend(_parameter_rows(trial.get('model', {}).get('parameters'), context, 'evaluated', 'model'))
        candidates.append({**context, **{key: trial.get(key) for key in ('status', 'validation_status', 'validation_reason', 'metric', 'value', 'selected',
            'elapsed_seconds', 'search_elapsed_seconds', 'feature_selection_method', 'vif_mode', 'comparison_scope', 'comparison_arm', 'error', 'search_summary')}})
    for fold in evaluation.get('fold_metrics', []):
        context = {'scope': 'outer_fold', 'repeat': fold.get('repeat'), 'fold': fold.get('fold')}
        rows = [r for r in evaluation.get('oof', []) if r.get('repeat') == context['repeat'] and r.get('fold') == context['fold']]
        metrics.extend(_metric_rows(fold.get('metrics', fold), context, unit, rows))
    for scope in ('pooled_oof', 'per_repeat', 'fold_mean', 'independent'):
        value = evaluation.get('metrics', {}).get(scope)
        items = value if isinstance(value, list) else [value]
        for item in items:
            repeat = item.get('repeat') if isinstance(item, dict) else None
            rows = evaluation.get('independent_predictions' if scope == 'independent' else 'oof', [])
            if scope == 'per_repeat':
                rows = [r for r in rows if r.get('repeat') == repeat]
            if scope == 'fold_mean':
                rows = []
            reason = 'not_applicable' if scope == 'independent' and not rows else 'not_recorded'
            metrics.extend(_metric_rows(None if scope == 'independent' and not rows else item, {'scope': scope, 'repeat': repeat}, unit, rows, reason))
    feature_records = [record for ctx, record in records if ctx['scope'] == 'outer' and ('selected_features' in record or 'selected_ids' in record)]
    frequency = Counter(feature for record in feature_records for feature in set(record.get('selected_features', record.get('selected_ids', []))))
    context = evaluation.get('study_context') or {}
    context_keys = ('started_utc', 'finished_analysis_utc', 'elapsed_analysis_seconds', 'sources', 'packages', 'platform', 'python',
                    'executable', 'cpu_count', 'source_code', 'descriptive_statistics', 'covariates', 'model_artifact',
                    'sample_crs', 'depth', 'laboratory_method', 'sampling_support', 'sample_count', 'group_count')
    context_keys = tuple(dict.fromkeys((*context_keys, *context.keys())))
    sample_description = {key: samples.get(key) for key in ('crs', 'support', 'depth', 'laboratory_method', 'excluded')}
    if sample_description['crs'] is not None:
        crs = sample_description['crs']
        sample_description['crs'] = crs.to_wkt() if hasattr(crs, 'to_wkt') else str(crs)
    summary = {'schema_version': 1, 'run_id': settings.get('run_id'), 'status': 'recorded', 'counts': counts,
        'study_context': {key: context.get(key) for key in context_keys},
        'context_status': {key: 'recorded' if context.get(key) is not None else 'not_recorded' for key in context_keys},
        'sample_description': sample_description,
        'sample_settings': settings.get('samples', {}), 'requested_settings': settings,
        'independent_status': 'recorded' if evaluation.get('independent_predictions') else 'not_applicable',
        'independent_test_history': evaluation.get('independent_test_history'),
        'prediction_status': {key: 'recorded' if rows else 'not_recorded' for key, rows in predictions.items()},
        'metric_definitions': {key: {'label': v[0], 'formula': v[1], 'unit_type': v[2]} for key, v in {**METRICS, **INTERVAL_METRICS}.items()},
        'conventions': {'residual': 'prediction_minus_observation', 'sample_std_ddof': 1, 'ccc_moments_divisor': 'n',
            'quantiles': 'numpy.percentile(method=linear)', 'repeats_are_independent_samples': False,
            'undefined_value': None, 'missing_record_status': 'not_recorded', 'csv_null': 'empty_field_with_status_and_reason'},
        'metrics': metrics, 'hyperparameters': parameters, 'candidates': candidates,
        'selection_frequency': [{'feature': feature, 'selected_models': n, 'recorded_outer_models': len(feature_records), 'fraction': n / len(feature_records)} for feature, n in sorted(frequency.items())],
        'selection_frequency_scope': 'outer_models_with_recorded_selection; model counts are not independent sample size',
        'missing_outer_selection_records': sum(ctx['scope'] == 'outer' for ctx, _ in records) - len(feature_records),
        'model_records': [{**ctx, 'record': {key: value for key, value in record.items() if key not in ('training_predictions', 'calibration_predictions')}} for ctx, record in records],
        'preflight': evaluation.get('preflight'), 'coverage_diagnostics': {key: value for key, value in evaluation.get('coverage_diagnostics', {}).items() if 'predictions' not in key},
        'subgroup_diagnostics': evaluation.get('subgroup_diagnostics'), 'mapping_diagnostics': evaluation.get('mapping_diagnostics'),
        'permutation_importance': evaluation.get('permutation_importance'), 'plain_model_comparison': evaluation.get('plain_model_comparison'),
        'raw_evaluation_file': 'evaluation.json', 'files': FILES}
    return summary, predictions


def _descriptive_html(context):
    values = context.get('descriptive_statistics')
    if not isinstance(values, list):
        return _kv(values)
    rows, categories = [], []
    for value in values:
        rows.append(tuple(value.get(key) for key in ('scope', 'variable', 'role', 'unit', 'kind', 'n', 'finite_n', 'missing_n', 'unique_samples')) +
                    (value.get('unique_groups', value.get('groups')), ) + tuple(value.get(key) for key in ('mean', 'std', 'q25', 'median', 'q75', 'min', 'max', 'iqr')))
        for category in value.get('categories', []):
            categories.append((value.get('scope'), value.get('variable'), category.get('value'), category.get('count')))
    result = _table(['Bölüm', 'Değişken', 'Rol', 'Birim', 'Tür', 'Kayıt', 'Sonlu', 'Eksik', 'Tekil gözlem', 'Grup', 'Ortalama', 'Standart sapma', 'Birinci çeyrek', 'Medyan', 'Üçüncü çeyrek', 'En küçük', 'En büyük', 'Çeyrekler arası aralık'], rows)
    if categories:
        result += _table(['Bölüm', 'Değişken', 'Kategori', 'Sayı'], categories)
    return result


def _fold_html(preflight):
    if not preflight:
        return '<p>Kat uzaklıkları ve ön kontrol tanıları kaydedilmedi (not_recorded).</p>'
    rows = []
    for fold in preflight.get('folds', []):
        diag = fold.get('diagnostics') or {}
        distance = diag.get('test_to_train_distance_m') or {}
        rows.append((fold.get('repeat'), fold.get('fold'), len(fold.get('train_ids', [])), len(fold.get('test_ids', [])),
                     len(fold.get('excluded_ids', [])), diag.get('train_groups'), diag.get('test_groups'),
                     distance.get('min', diag.get('minimum_train_test_distance_m')), distance.get('median'), distance.get('max')))
    return _table(['Tekrar', 'Kat', 'Eğitim', 'Test', 'Dışlanan', 'Eğitim grubu', 'Test grubu', 'En kısa uzaklık (m)', 'Medyan uzaklık (m)', 'En uzun uzaklık (m)'], rows) + \
        '<p>Uzaklıklar her test gözleminin en yakın eğitim gözlemine uzaklığıdır. Kaydedilmeyen uzaklık sıfır sayılmaz. Tam kat üyelikleri validation_folds.csv içinde korunur.</p>' + _kv({'warnings': preflight.get('warnings'), 'workload': preflight.get('workload')})


def _study_html(summary):
    settings = summary['requested_settings']
    context = summary['study_context']
    reused = (summary.get('independent_test_history') or {}).get('settings_changed_since_previous', False)
    parts = ['<section id="study-report"><h2>Ayrıntılı çalışma ve model kaydı</h2>',
        '<p>Bu rapor, tamamlanan hesabın kayıtlarından üretilir. Eksik kayıtlar tamamlanmış işlem olarak gösterilmez. Makale metni veya pedolojik yorum yerine geçmez.</p>',
        '<h3>Veri, derinlik, birim, laboratuvar yöntemi ve örnek desteği</h3>', _kv(summary['sample_settings']), _kv(summary['sample_description']), _kv(summary['counts']),
        '<h3>Betimsel istatistikler ve yardımcı değişkenler</h3>', _descriptive_html(context), _kv(context['covariates']),
        '<h3>Doğrulama, gruplar, uzaklıklar ve tarihler</h3>', _kv(settings.get('validation')),
        _fold_html(summary['preflight']),
        '<p>Tekrarlı değerlendirmede aynı gözlem birden çok tahmin kaydı taşıyabilir. Tekil kimlik sayısı ile tahmin kaydı sayısı ayrıdır. Kat veya model sayısı bağımsız örnek sayısı değildir.</p>',
        '<p>Kilitli bağımsız test: ' + ('Kaydedildi; kullanım geçmişi aşağıdadır.' if summary['independent_status'] == 'recorded' else 'Uygulanmadı; bağımsız test sonucu yoktur.') + '</p>', _kv(summary['independent_test_history']),
        '<p>Test verisi ayarlar değiştikten sonra yeniden kullanıldı. Bu sonuç yeni ve bağımsız bir nihai başarı ölçümü değildir.</p>' if reused else '',
        '<h3>Ön işleme, hedef dönüşümü ve değişken seçimi</h3>']
    for component, title in [('preprocessing', 'Ön işleme'), ('target_transform', 'Hedef dönüşümü'), ('vif', 'Varyans şişirme faktörü ile bağlantı denetimi'), ('feature_selection', 'Değişken seçimi')]:
        parts.extend(['<h4>' + title + '</h4>', _kv(settings.get(component))])
    parts.extend(['<h4>Dış kat modellerinde seçilme sıklığı</h4>',
        '<p>Payda, seçim kaydı bulunan dış kat modelleridir. Nihai model bu sıklığa katılmaz. Bu sıklık değişkenin nedensel etkisi değildir.</p>',
        _table(['Özellik', 'Seçen model', 'Kayıtlı dış model', 'Pay'], [(r['feature'], r['selected_models'], r['recorded_outer_models'], r['fraction']) for r in summary['selection_frequency']]),
        '<p>Seçim kaydı bulunmayan dış model sayısı: ' + str(summary['missing_outer_selection_records']) + '.</p>',
        '<h3>Model adayları ve gerçekleşen arama</h3>', _kv(settings.get('optimization')),
        _table(['Arama', 'Deneme', 'Model', 'Karşılaştırma', 'Seçici', 'Durum', 'Ölçüt', 'Kayıp', 'Süre (s)', 'Seçildi', 'Hata'],
            [(r['role'], r['trial'], r['model_id'], r['comparison_arm'], r['feature_selection_method'], r['status'], r['metric'], r['value'], r['elapsed_seconds'], r['selected'], r['error']) for r in summary['candidates']]),
        '<p>Başarısız denemeler tabloda korunur. Tamamlanmayan denemenin kısmi tahminleri dosyada bulunabilir; bunlardan birleşik başarı sonucu üretilmez. Seçilen deneme kendi iç aramasına aittir.</p>',
        '<h3>İstenen, taranan, seçilen ve etkin parametreler</h3>',
        '<p>Etkin parametreler eğitilmiş tahmincinin kaydından gelir. Eski kayıtta bulunmayan varsayılan değerler tahmin edilmez. Tam alan adları indirilebilir tabloda korunur.</p>',
        _table(['Kapsam', 'Arama', 'Tekrar', 'Kat', 'Deneme', 'Kayıt türü', 'Bileşen', 'Parametre', 'Değer', 'Durum'],
            [(r.get('scope'), r.get('role'), r.get('repeat'), r.get('fold'), r.get('trial'), r['kind'], r['component'], r['parameter'], r['value'], r['status']) for r in summary['hyperparameters']]),
        '<h3>Eğitim, kalibrasyon ve doğrulama sonuçları</h3>',
        '<p>Eğitim hatası, modelin eğitim verisine yeniden uygulanmasıyla hesaplanır. Kalibrasyon tanısı, aralık ayarında kullanılan hedefleri içerir. İkisi de bağımsız doğrulama başarısı değildir.</p>',
        '<p>İç doğrulama model ve parametre seçimine katılır. Seçim yanlılığı nedeniyle nihai başarı olarak yorumlanmaz. Dış kat, tekrar, birleşik sonuç ve bağımsız test ayrı gösterilir.</p>'])
    for scope in SCOPE:
        rows = [r for r in summary['metrics'] if r['scope'] == scope]
        if not rows:
            continue
        title = 'Ayar değişikliği sonrası yeniden kullanılan test' if scope == 'independent' and reused else SCOPE[scope]
        parts.extend(['<h4>' + title + '</h4>',
            _table(['Arama', 'Deneme', 'Tekrar', 'Kat', 'Ölçüt', 'Değer', 'Birim', 'Kayıt', 'Tekil kimlik', 'Grup', 'Aralıklı kayıt / toplam', 'Durum / neden'],
                [(r.get('role'), r.get('trial'), r.get('repeat'), r.get('fold'), r['label'], r['value'], r['unit'], r['n'], r['n_unique'], r['group_count'],
                  str(r.get('coverage_denominator')) + ' / ' + str(r.get('availability_denominator')) if r.get('interval_scope') else None,
                  REASONS.get(r['reason'], r['reason']) if r['reason'] else 'Yalnız aralığı hesaplanan altküme' if r['status'] == 'available_subset' else 'Hesaplandı') for r in rows])])
    parts.extend(['<h3>Ölçütlerin tanımları ve hesaplama kuralları</h3>',
        '<p>y gözlenen, p tahmin edilen değer; e=p-y; n ilgili bölümdeki kayıt sayısıdır. mean aritmetik ortalamayı, I koşul sağlandığında 1 değerini belirtir. Hata ölçütleri özgün hedef ölçeğindedir.</p>',
        '<p>R², Pearson r² değildir ve negatif olabilir. Lin katsayısında varyans ve kovaryans böleni n olur. Standart sapmalarda bölen n-1 olur.</p>',
        '<p>Çeyrekler doğrusal ara değerle hesaplanır: sıralı veride h=(n-1)q, j=floor(h), Qq=y[j]+(h-j)*(y[min(j+1,n-1)]-y[j]); indeks sıfırdan başlar.</p>',
        '<p>Sıfır payda, yetersiz kayıt veya sabit değişken nedeniyle tanımsız sonuç null ve neden koduyla saklanır. Sıfır hata için dağılım oranlarına sonsuz değer atanmaz.</p>',
        _table(['Ölçüt', 'Tam formül', 'Birim türü'], [(v['label'], v['formula'], {'target': 'Hedef birimi', 'squared': 'Hedef biriminin karesi', 'dimensionless': 'Boyutsuz'}[v['unit_type']]) for v in summary['metric_definitions'].values()]),
        '<h3>Belirsizlik, kalibrasyon ve altgruplar</h3>', _kv(settings.get('uncertainty')), _kv(summary['coverage_diagnostics']), _kv(summary['subgroup_diagnostics']),
        '<p>Tahmin aralığı güven aralığı değildir. Yerel aralık yalnız bazı gözlemlerde hesaplandıysa kapsama paydası bu altkümedir. Aralığı olmayan gözlemler sıfır genişlikle doldurulmaz.</p>',
        '<h3>Haritalama, uygulanabilirlik alanı ve artık kriging</h3>', _kv(settings.get('mapping')), _kv(summary['mapping_diagnostics']), _kv(summary['plain_model_comparison']),
        '<p>Uygulanabilirlik alanı çevresel temsil tanısıdır; alan içindeki her hücrenin doğru olduğunu göstermez. Hücre payı, coğrafi alan payı olarak yorumlanmaz.</p>',
        '<h3>Nihai model, öğrenilen dönüşümler ve katsayı kayıtları</h3>'])
    for item in summary['model_records']:
        record = item['record']
        reduced = {key: value for key, value in record.items() if key not in ('train_metrics', 'calibration_metrics', 'training_ids', 'development_ids', 'calibration_ids', 'excluded_ids')}
        parts.extend(['<h4>' + html.escape(str(item['role'])) + '</h4>', _kv(reduced)])
    parts.extend(['<h3>Çalışma zamanı, donanım, sürümler ve dosya özetleri</h3>',
        _kv({key: {'status': summary['context_status'][key], 'value': context[key]} for key in context if key not in ('descriptive_statistics', 'covariates')}),
        '<p>UTC, eşgüdümlü evrensel zamandır. SHA-256, dosya içeriğinin kriptografik özetidir. Analiz süresi rapor dosyalarını yazma süresini içermeyebilir.</p>',
        '<h3>Varsayımlar ve yorum sınırları</h3>',
        '<p>Grup alanları, kaydedilmiş bağımlılığı korur; bilinmeyen bağımlılığı ortadan kaldırmaz. Mekânsal katlar bütün çalışma alanı için tasarıma dayalı doğruluk güvencesi değildir.</p>',
        '<p>Zamansal değerlendirmede başlangıç geçmişi dış test sayılmaz. Sabit rasterler geleceğe ait harita doğruluğunu kanıtlamaz. Deneysel yöntemlerin sonuçları yalnız kaydedilmiş veri ve ayarlara aittir.</p>',
        '<p>Değişken önemi nedensel etki değildir. Fiziksel sınırlar, derinlik, laboratuvar yöntemi ve örnek desteği yorumdan önce incelenmelidir. Eksik bilgi tamamlanmış kabul edilmez.</p>',
        '<h3>Ayrıntılı dosyalar</h3><ul>'])
    data_files = context.get('analysis_data', {}).get('files', [])
    parts.extend('<li><a href="' + name + '">' + name + '</a></li>' for name in FILES + data_files + ['evaluation.json', 'optimization_trials.json', 'final_record.json', 'learning_audit.json', 'figure_manifest.json'])
    artifact = context.get('model_artifact') or {}
    model_path = artifact.get('package_path')
    if model_path:
        if Path(model_path).is_absolute() or '..' in Path(model_path).parts or ':' in model_path:
            raise JobError('REPORT_MODEL_LINK', 'Model paketinin bağlantısı yerel ve göreli olmalıdır.')
        parts.append('<li>Kaydedilmiş model paketi: <code>' + html.escape(model_path) + '</code>. '
                     'ArcMap panelinde <strong>10 Rapor ve günlük &gt; Modeli kaydet</strong> yoluyla dışa aktarın.</li>')
    if data_files:
        parts.append('<li>Tüm çalışma paketi: <code>study_package.spsstudy.zip</code>. Raporlar, veri tabloları, haritalar ve model paketi birlikte saklanır.</li>')
    parts.append('</ul>')
    if data_files:
        parts.append('<p>sampled_data.csv, modele verilen özgün çıkarılmış değişken değerlerini içerir. '
                     'role_data.csv aynı değerleri eğitim, kalibrasyon ve sınama üyelikleriyle birleştirir. '
                     'Eksik değişkenler burada doldurulmaz. Dışlanan ham gözlemler ayrı dosyada gerekçeleriyle korunur.</p>')
        parts.append('<p>trial_results.csv yalnız gerçekten başlatılan denemeleri içerir. '
                     'inner_fold_results.csv tamamlanmayan katları boş ölçütlerle ayırır. '
                     'İç kat modellerinin eğitim hatası ve iç aralık ölçütleri hesaplanmadı; yeni sonuç gibi üretilmez.</p>')
    parts.append('<p>Tam ham kayıt evaluation.json içinde korunur. Tablolarda boş sayısal alan null değerini taşır; neden ve durum sütunları ayrı verilir.</p></section>')
    return '\n'.join(parts)


class _HtmlNode:
    """Small tree for our own generated HTML, not a general HTML sanitizer."""
    def __init__(self, tag, attrs=(), children=()):
        self.tag, self.attrs, self.children = tag, list(attrs), list(children)

    def render(self):
        content = ''.join(child.render() if isinstance(child, _HtmlNode) else child for child in self.children)
        if self.tag == 'root':
            return content
        start = '<' + self.tag + ''.join(' ' + key + ('="' + html.escape(value, quote=True) + '"' if value is not None else '') for key, value in self.attrs) + '>'
        return start if self.tag in ('img', 'br', 'hr') else start + content + '</' + self.tag + '>'

    def text(self):
        return ''.join(child.text() if isinstance(child, _HtmlNode) else html.unescape(child) for child in self.children)


class _ReportTree(HTMLParser):
    def __init__(self, content):
        super().__init__(convert_charrefs=False)
        self.root = _HtmlNode('root')
        self.stack = [self.root]
        self.feed(content)
        self.close()
        if len(self.stack) != 1:
            raise JobError('REPORT_PAGE_STRUCTURE', 'Rapor bölümlerinin HTML yapısı kapanmamış.')

    def handle_starttag(self, tag, attrs):
        node = _HtmlNode(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in ('img', 'br', 'hr'):
            self.stack.append(node)

    def handle_endtag(self, tag):
        if len(self.stack) == 1 or self.stack[-1].tag != tag:
            raise JobError('REPORT_PAGE_STRUCTURE', 'Rapor bölümlerinin HTML yapısı tutarsız.')
        self.stack.pop()

    def handle_data(self, value):
        self.stack[-1].children.append(value)

    def handle_entityref(self, name):
        self.stack[-1].children.append('&' + name + ';')

    def handle_charref(self, name):
        self.stack[-1].children.append('&#' + name + ';')


def _nodes(node, tag):
    if node.tag == tag:
        yield node
    for child in node.children:
        if isinstance(child, _HtmlNode):
            yield from _nodes(child, tag)


def _data_rows(node):
    return [row.render() for body in _nodes(node, 'tbody') for row in body.children if isinstance(row, _HtmlNode) and row.tag == 'tr']


def _panel_safe_links(node):
    # Full publication HTML stays on disk, but must not reopen in the ArcMap host.
    if node.tag == 'a' and dict(node.attrs).get('href') in ('study_report.html', 'report.html'):
        node.tag, node.attrs = 'code', []
    for child in node.children:
        if isinstance(child, _HtmlNode):
            _panel_safe_links(child)


def _table_chunks(block):
    tables = list(_nodes(block, 'table'))
    if not tables:
        yield block
        return
    if len(tables) != 1:
        raise JobError('REPORT_PAGE_STRUCTURE', 'Sayfalanacak blok tek tablo içermelidir.')
    table = tables[0]
    heads = [node for node in table.children if isinstance(node, _HtmlNode) and node.tag == 'thead']
    bodies = [node for node in table.children if isinstance(node, _HtmlNode) and node.tag == 'tbody']
    if len(heads) != 1 or len(bodies) != 1:
        raise JobError('REPORT_PAGE_STRUCTURE', 'Sayfalanacak tablonun başlığı veya gövdesi eksik.')
    head_rows = sum(1 for _ in _nodes(heads[0], 'tr'))
    if head_rows >= PAGE_ROW_LIMIT:
        raise JobError('REPORT_PAGE_STRUCTURE', 'Tablo başlığı sayfa satır sınırını aşıyor.')
    rows = [node for node in bodies[0].children if isinstance(node, _HtmlNode) and node.tag == 'tr']
    chunks, current, size = [], [], len(heads[0].render().encode('utf-8'))
    for row in rows:
        row_size = len(row.render().encode('utf-8'))
        if current and (len(current) + head_rows >= PAGE_ROW_LIMIT or size + row_size > PAGE_SOFT_BYTES):
            chunks.append(current); current = []; size = len(heads[0].render().encode('utf-8'))
        current.append(row); size += row_size
    if current or not rows:
        chunks.append(current)
    for chunk in chunks:
        rebuilt = _HtmlNode('table', table.attrs, [heads[0], _HtmlNode('tbody', bodies[0].attrs, chunk)])
        yield _HtmlNode('div', block.attrs, [rebuilt]) if block.tag == 'div' else rebuilt


def _page_document(title, content, navigation):
    return '<!doctype html><html lang="tr"><head><meta charset="utf-8">' + \
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' data:; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">' + \
        '<title>' + html.escape(title) + '</title><style>' + \
        'body{font-family:Segoe UI,Arial,sans-serif;line-height:1.45;color:#18222c;margin:1rem}table{border-collapse:collapse;width:100%}td,th{border:1px solid #cad1d7;padding:.35rem;text-align:left;vertical-align:top;word-wrap:break-word}th{background:#eef2f5}.study-table{overflow-x:auto}img{max-width:100%;height:auto}a{color:#005983}nav{padding:.7rem;background:#eef2f5;margin:.6rem 0}h1{font-size:1.35rem}h2{font-size:1.15rem}</style></head><body>' + \
        navigation + '<h1>' + html.escape(title) + '</h1>' + content + navigation + '</body></html>'


def _report_supplement(document):
    """Keep the original report's unique content accessible in the panel."""
    body = document.partition('</head><body>')[2].rpartition('</body></html>')[0]
    if not body:
        raise JobError('REPORT_PAGE_STRUCTURE', 'Ana raporun gövdesi bulunamadı.')
    tree = _ReportTree(body).root
    blocks, heading = [], None
    for node in tree.children:
        if not isinstance(node, _HtmlNode):
            if node.strip():
                raise JobError('REPORT_PAGE_STRUCTURE', 'Ana raporun bölüm dışında metni var.')
            continue
        if node.tag in ('h1', 'figure'):
            continue
        if node.tag == 'section':
            raise JobError('REPORT_PAGE_STRUCTURE', 'Ana rapor ekinde ayrıntılı bölüm yinelenemez.')
        if node.tag == 'pre':
            if heading == 'Nihai model kaydı':
                target, label = 'final_record.json', 'Nihai modelin tam kaydı'
            elif heading == 'Kullanılan ayarlar':
                target, label = 'evaluation.json', 'Kullanılan ayarları da içeren tam değerlendirme kaydı'
            else:
                raise JobError('REPORT_PAGE_STRUCTURE', 'Ana raporda bilinmeyen ham kayıt bloğu var.')
            node = _HtmlNode('p', children=[_HtmlNode('a', [('href', target)], [label]), '. Büyük ham kayıt yerel veri dosyasında eksiksiz korunur.'])
        elif node.tag == 'h2':
            heading = node.text()
            node.tag = 'h3'
            node.children = ['Değerlendirme: '] + node.children
        elif node.tag == 'h3':
            node.tag = 'h4'
        blocks.append(node)
    return '<section><h3>Değerlendirme: çalışma kimliği</h3>' + ''.join(node.render() for node in blocks) + '</section>'


def write_paged_study(folder, section, figure_fragments, *, check_cancel, supplemental_report=''):
    """Split our rendered study without dropping, reordering or truncating rows."""
    tree = _ReportTree(section).root
    roots = [node for node in tree.children if isinstance(node, _HtmlNode) and node.tag == 'section']
    if len(roots) != 1:
        raise JobError('REPORT_PAGE_STRUCTURE', 'Ayrıntılı çalışma bölümü tek olmalıdır.')
    sources = [{'file': 'study_report.html', 'data_rows': len(_data_rows(tree))}]
    if supplemental_report:
        supplemental = _ReportTree(_report_supplement(supplemental_report)).root
        roots.extend(node for node in supplemental.children if isinstance(node, _HtmlNode) and node.tag == 'section')
        tree.children.extend(supplemental.children)
        sources.append({'file': 'report.html', 'scope': 'excluding the repeated study section and figures', 'data_rows': len(_data_rows(supplemental))})
    source_rows = _data_rows(tree)
    _panel_safe_links(tree)
    sections = [('Çalışma raporunun kapsamı', [])]
    for node in (node for root in roots for node in root.children):
        if not isinstance(node, _HtmlNode):
            if node.strip():
                raise JobError('REPORT_PAGE_STRUCTURE', 'Bölüm dışında rapor metni bulundu.')
            continue
        if node.tag == 'h3':
            sections.append((node.text(), []))
        elif node.tag != 'h2':
            sections[-1][1].append(node)
    pages = []
    for title, blocks in sections:
        check_cancel()
        notes = [node for node in blocks if node.tag == 'p']
        prefix = ''.join(node.render() for node in notes)
        current, row_count, byte_count, subheading = [], 0, len(prefix.encode('utf-8')), None
        def flush():
            nonlocal current, row_count, byte_count
            if current or not pages or pages[-1]['title'] != title:
                pages.append({'title': title, 'content': prefix + ''.join(node.render() for node in current), 'table_rows': row_count})
            current, row_count, byte_count = [], 0, len(prefix.encode('utf-8'))
        for block in blocks:
            if block.tag == 'p':
                continue
            if block.tag == 'h4':
                subheading = block
            for chunk in _table_chunks(block):
                check_cancel()
                size = len(chunk.render().encode('utf-8'))
                rows = sum(1 for _ in _nodes(chunk, 'tr'))
                if current and (row_count + rows > PAGE_ROW_LIMIT or (row_count and byte_count + size > PAGE_SOFT_BYTES)):
                    flush()
                    if subheading is not None and chunk is not subheading:
                        current.append(subheading)
                current.append(chunk); row_count += rows; byte_count += size
        flush()
    for i, fragment in enumerate(figure_fragments):
        check_cancel()
        pages.append({'title': 'Yerel grafik %d' % (i + 1), 'content': fragment, 'table_rows': 0})
    actual_rows = [row for page in pages for row in _data_rows(_ReportTree(page['content']).root)]
    if source_rows != actual_rows or any(page['table_rows'] > PAGE_ROW_LIMIT for page in pages):
        raise JobError('REPORT_PAGE_ROWS', 'Rapor sayfaları bütün tablo satırlarını aynı sırayla korumalıdır.')
    metadata = []
    for i, page in enumerate(pages):
        check_cancel()
        filename = 'study_report_page_%03d.html' % (i + 1)
        links = ['<a href="' + PAGE_INDEX + '">İçindekiler</a>']
        if i:
            links.append('<a href="study_report_page_%03d.html">Önceki</a>' % i)
        if i + 1 < len(pages):
            links.append('<a href="study_report_page_%03d.html">Sonraki</a>' % (i + 2))
        nav = '<nav>' + ' | '.join(links) + ' <span>Sayfa %d / %d</span></nav>' % (i + 1, len(pages))
        content = _page_document(page['title'], page['content'], nav)
        (folder / filename).write_text(content, encoding='utf-8')
        metadata.append({'file': filename, 'title': page['title'], 'table_rows': page['table_rows'],
                         'data_rows': len(_data_rows(_ReportTree(page['content']).root)), 'bytes': len(content.encode('utf-8'))})
    index = ['<p>Ayrıntılı çalışma raporu yerel bölümlere ayrılmıştır. Tablo satırları eksiksiz korunur. Bölümler arasında bu içindekiler veya önceki ve sonraki bağlantılarıyla geçebilirsiniz.</p>',
             '<p>Her sayfada başlıklarla birlikte en çok %d tablo satırı bulunur. Grafikler ayrı sayfalardadır.</p>' % PAGE_ROW_LIMIT,
             '<p>Yayın ve arşiv için tam rapor dosyaları: <code>study_report.html</code> ve <code>report.html</code>. Panel, tam dosyalar yerine aşağıdaki bölümleri açar.</p><ol>']
    for page in metadata:
        index.append('<li><a href="' + page['file'] + '">' + html.escape(page['title']) + '</a> (%d veri satırı)</li>' % page['data_rows'])
    index.append('</ol>')
    check_cancel()
    (folder / PAGE_INDEX).write_text(_page_document('Ayrıntılı çalışma raporu: içindekiler', ''.join(index), ''), encoding='utf-8')
    return {'index': PAGE_INDEX, 'pages': metadata, 'table_row_limit_including_headers': PAGE_ROW_LIMIT,
            'soft_content_byte_limit': PAGE_SOFT_BYTES, 'source_data_rows': len(source_rows), 'sources': sources,
            'source_rows_sha256': hashlib.sha256(json.dumps(source_rows, ensure_ascii=False).encode('utf-8')).hexdigest(),
            'all_rows_preserved_in_order': True, 'scope': 'run output pages; HTML pages are not embedded in the model archive'}


def write_publication_reports(folder, evaluation, samples, settings, counts, figures, *, check_cancel, supplemental_report=''):
    """Write into the caller's temporary folder; return the shared HTML section."""
    folder = Path(folder)
    summary, predictions = collect_study(evaluation, samples, settings, counts, check_cancel=check_cancel)
    check_cancel()
    _write_csv(folder / 'publication_metrics.csv', summary['metrics'], ['scope', 'role', 'repeat', 'fold', 'trial', 'model_id', 'metric', 'label', 'value', 'unit', 'status', 'reason', 'n', 'n_unique', 'group_count', 'scope_note', 'formula'])
    _write_csv(folder / 'hyperparameters.csv', summary['hyperparameters'], ['scope', 'role', 'repeat', 'fold', 'trial', 'model_id', 'kind', 'component', 'parameter', 'value', 'status'])
    for key, rows in predictions.items():
        check_cancel()
        _write_csv(folder / (key + '_predictions.csv'), rows, ['scope', 'role', 'trial', 'repeat', 'fold', 'model_id', 'sample_id', 'group_id', 'observed', 'predicted', 'residual'])
    section = _study_html(summary)
    document = ['<!doctype html><html lang="tr"><head><meta charset="utf-8">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' data:; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
        '<title>Ayrıntılı çalışma raporu</title><style>body{font-family:Segoe UI,Arial,sans-serif;line-height:1.5;color:#18222c;max-width:1400px;margin:2rem auto;padding:1rem}table{border-collapse:collapse;width:100%;margin:1rem 0}td,th{border:1px solid #cad1d7;padding:.4rem;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#eef2f5}.study-table{overflow-x:auto}img{max-width:100%;height:auto}a{color:#005983}@media print{.study-table{overflow:visible}h3{break-after:avoid}}</style></head><body><h1>Ayrıntılı çalışma raporu</h1>', section, '<h2>Yerel grafikler</h2>']
    figure_fragments = []
    for figure in figures:
        paths = figure.get('files', [])
        if len(paths) < 2 or any(Path(p).is_absolute() or '..' in Path(p).parts or ':' in p or not p.startswith('figures/') for p in paths):
            raise JobError('REPORT_PUBLICATION_FIGURE', 'Ayrıntılı rapor yalnız kendi yerel grafiklerini kullanabilir.')
        caption = html.escape(str(figure.get('caption', 'Grafik')))
        fragment = '<figure><img src="' + html.escape(paths[1], quote=True) + '" alt="' + caption + '"><figcaption>' + caption + ' <a href="' + html.escape(paths[0], quote=True) + '">Vektörel grafik</a></figcaption></figure>'
        document.append(fragment)
        figure_fragments.append(fragment)
    document.append('</body></html>')
    check_cancel()
    (folder / 'study_report.html').write_text('\n'.join(document), encoding='utf-8')
    summary['pagination'] = write_paged_study(folder, section, figure_fragments, check_cancel=check_cancel, supplemental_report=supplemental_report)
    summary['files'] = FILES + summary['study_context'].get('analysis_data', {}).get('files', []) + [PAGE_INDEX] + [page['file'] for page in summary['pagination']['pages']]
    check_cancel()
    (folder / 'study_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return section
