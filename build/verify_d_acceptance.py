"""Read actual D files, project roundtrip, model export and scoped cancellation proof.

Starts no application, model, process query or job. Only the two requested evidence
JSON files are written. The package-covered tools/ verifier is imported read-only.
Required paths must identify observed artifacts; missing evidence never passes.

Process evidence schema (external, scoped read-only observation):
  {run_id, worker_pid, exit_code, exit_code_source:{path,kind:'log'|'ui_screenshot'},
   process_observation:{observed_at_utc,status:'completed',
      scope:'captured_worker_tree_and_run_id',run_id,
      captured_process_ids:[worker_pid,...],remaining_processes:[]}}
Never supply unrelated system process command lines. A screenshot's exit code is
an external UI observation, not an OCR result produced by this program.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import date, datetime, timezone
import hashlib
import html
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile
from urllib.parse import unquote, urlsplit

import numpy as np
import rasterio

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / 'build/arcmap-evidence'
MODELS = {'tabm', 'realmlp', 'tabpfn'}
TRANSIENT = {'preview_fingerprint', 'preview_manifest', 'preview_signature', 'preview_sources'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'),
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON: ' + value)))


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def snapshot(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': digest(path), 'size_bytes': path.stat().st_size,
            'modified_utc': datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()}


def load_helper(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def output_directory(path):
    path = Path(path).resolve()
    return path if (path / 'result.json').is_file() else path / 'output'


def local_member(directory, name):
    require(isinstance(name, str) and name and '\\' not in name and ':' not in name, 'Invalid relative member: ' + str(name))
    relative = PurePosixPath(name)
    require(not relative.is_absolute() and '..' not in relative.parts, 'Unsafe relative member: ' + name)
    resolved = (directory / name).resolve()
    require(resolved.is_relative_to(directory.resolve()) and resolved.is_file(), 'Missing or escaped member: ' + name)
    return resolved


def verify_output_hashes(output):
    result = read(output / 'result.json')
    entries = result.get('outputs', [])
    require(entries and len({e['path'] for e in entries}) == len(entries), 'Output manifest is empty or duplicated.')
    rows = []
    for entry in entries:
        path = local_member(output, entry['path'])
        actual = digest(path)
        require(actual == entry['sha256'], 'Changed output: ' + str(path))
        rows.append({'path': entry['path'], 'expected_sha256': entry['sha256'], 'actual_sha256': actual,
                     'matches': True, 'exists': True, 'within_output_directory': True})
    return result, rows


def point_metrics(rows, recorded):
    require(rows and recorded is not None, 'Point metric evidence is missing.')
    observed = np.asarray([r['observed'] for r in rows], dtype=float)
    predicted = np.asarray([r['predicted'] for r in rows], dtype=float)
    require(np.isfinite(observed).all() and np.isfinite(predicted).all(), 'Nonfinite point predictions.')
    residual = predicted - observed
    np.testing.assert_allclose([r['residual'] for r in rows], residual, rtol=0, atol=1e-12)
    require(recorded['n'] == len(rows), 'Point metric denominator differs from prediction rows.')
    squared = np.mean(residual ** 2)
    expected = {'mse': squared, 'rmse': np.sqrt(squared), 'mae': np.mean(np.abs(residual)),
                'mean_error': residual.mean(), 'median_absolute_error': np.median(np.abs(residual)),
                'observed_iqr': np.diff(np.percentile(observed, [25, 75], method='linear'))[0]}
    if len(rows) > 1:
        expected.update(residual_std=np.std(residual, ddof=1), observed_std=np.std(observed, ddof=1))
        vy, vp = np.var(observed), np.var(predicted)
        if vy:
            expected['r2'] = 1 - np.sum(residual ** 2) / np.sum((observed - observed.mean()) ** 2)
        if vy and vp:
            expected['pearson_r'] = np.corrcoef(observed, predicted)[0, 1]
            expected['pearson_r2'] = expected['pearson_r'] ** 2
        denominator = vy + vp + (observed.mean() - predicted.mean()) ** 2
        if denominator:
            expected['lin_ccc'] = 2 * np.mean((observed - observed.mean()) * (predicted - predicted.mean())) / denominator
        if squared:
            expected.update(rpd=expected['observed_std'] / np.sqrt(squared), rpiq=expected['observed_iqr'] / np.sqrt(squared))
    for name, value in expected.items():
        require(recorded.get(name) is not None and np.isclose(recorded[name], value, rtol=1e-9, atol=1e-12), 'Point metric differs: ' + name)
    for name in ('r2', 'residual_std', 'observed_std', 'pearson_r', 'pearson_r2', 'lin_ccc', 'rpd', 'rpiq'):
        if name not in expected:
            require(recorded.get(name) is None and bool(recorded.get('reasons', {}).get(name)), 'Undefined metric lacks explanation: ' + name)


def interval_summary(rows, recorded):
    available = [r for r in rows if r.get('lower') is not None]
    for row in rows:
        if row.get('lower') is None:
            require(row.get('upper') is None and row.get('interval_width') is None and row.get('interval_status') == 'unavailable'
                    and bool(row.get('reason_code')), 'Unavailable interval lacks paired nulls and reason.')
        else:
            require(np.isfinite([row['lower'], row['upper']]).all() and row['lower'] <= row['upper'], 'Invalid interval bounds.')
            if 'interval_width' in row:
                require(np.isclose(row['interval_width'], row['upper'] - row['lower']), 'Interval width differs.')
    if 'available_n' in recorded:
        require(recorded['n'] == len(rows) and recorded['available_n'] == len(available)
                and recorded['unavailable_n'] == len(rows) - len(available)
                and recorded['coverage_denominator'] == len(available)
                and recorded['availability_denominator'] == len(rows), 'Interval denominators differ.')
        require(np.isclose(recorded['available_fraction'], len(available) / len(rows)), 'Interval availability differs.')
        if len(available) < len(rows):
            require('coverage' not in recorded and recorded['status'] == ('partial' if available else 'unavailable'), 'Partial intervals presented as complete coverage.')
    values = recorded.get('available_subset', recorded)
    if available:
        covered = np.mean([r['lower'] <= r['observed'] <= r['upper'] for r in available])
        require(np.isclose(values['coverage'], covered), 'Empirical interval coverage differs.')


def chronology(left, right, dates, gap):
    require(left and right, 'Temporal partition is empty.')
    require((min(dates[i] for i in right) - max(dates[i] for i in left)).days > gap,
            'Temporal order or calendar gap violated.')


class ReportReadback(HTMLParser):
    """Read generated rows independently of the production HTML tree."""
    def __init__(self, source, *, exclude_study=False):
        super().__init__(convert_charrefs=False)
        self.exclude_study = exclude_study
        self.skipped_sections = 0
        self.in_body = False
        self.row = self.paragraph = None
        self.rows, self.paragraphs, self.links, self.images = [], [], [], []
        self.total_rows = 0
        self.feed(source)
        self.close()
        require(self.row is None and not self.in_body and not self.skipped_sections, 'Incomplete report table or section.')

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == 'section' and (self.skipped_sections or (self.exclude_study and attributes.get('id') == 'study-report')):
            self.skipped_sections += 1
        if self.skipped_sections:
            return
        if tag == 'tbody':
            self.in_body = True
        if tag == 'tr':
            self.total_rows += 1
            if self.in_body:
                require(self.row is None, 'Nested data row.')
                self.row = []
        if self.row is not None:
            self.row.append('<' + tag + ''.join(' ' + key + ('="' + html.escape(value, quote=True) + '"' if value is not None else '')
                                               for key, value in attrs) + '>')
        if tag == 'p':
            self.paragraph = []
        if tag == 'a':
            self.links.append(attributes.get('href', ''))
        if tag == 'img':
            self.images.append(attributes.get('src', ''))

    def handle_endtag(self, tag):
        if self.skipped_sections:
            if tag == 'section':
                self.skipped_sections -= 1
            return
        if self.row is not None:
            self.row.append('</' + tag + '>')
        if tag == 'tr' and self.row is not None:
            self.rows.append(''.join(self.row))
            self.row = None
        if tag == 'tbody':
            self.in_body = False
        if tag == 'p' and self.paragraph is not None:
            self.paragraphs.append(html.unescape(''.join(self.paragraph)))
            self.paragraph = None

    def handle_data(self, value):
        if self.skipped_sections:
            return
        if self.row is not None:
            self.row.append(value)
        if self.paragraph is not None:
            self.paragraph.append(value)

    def handle_entityref(self, value):
        self.handle_data('&' + value + ';')

    def handle_charref(self, value):
        self.handle_data('&#' + value + ';')


def verify_pagination(output, result, study):
    info = study['pagination']
    require(info['all_rows_preserved_in_order'] is True and info['table_row_limit_including_headers'] == 120,
            'Pagination preservation/row-limit metadata differs.')
    require(info['index'] == 'study_report_index.html' and info['pages'], 'Missing report index or pages.')
    names = [row['file'] for row in info['pages']]
    require(names == ['study_report_page_%03d.html' % (i + 1) for i in range(len(names))], 'Page order/name sequence differs.')
    entries = {entry['path']: entry for entry in result['outputs']}
    listed = {name for name in entries if name.startswith('study_report_page_') and name.endswith('.html')}
    actual = {path.name for path in output.glob('study_report_page_*.html')}
    require(set(names) == listed == actual, 'Page metadata, result manifest and files differ.')
    require(set([info['index']] + names) <= set(study['files']), 'Study file list omits index/pages.')
    verified_targets = set()

    def verify_member(name, report=False):
        require(name in entries, 'Report target is outside result manifest: ' + name)
        require(not report or entries[name]['kind'] == 'report', 'HTML page has wrong product kind.')
        path = local_member(output, name)
        if name not in verified_targets:
            require(digest(path) == entries[name]['sha256'], 'Report link target digest differs: ' + name)
            verified_targets.add(name)
        return path

    def document(name):
        path = verify_member(name, report=True)
        parsed = ReportReadback(path.read_text(encoding='utf-8'))
        for target in parsed.links + parsed.images:
            if target.startswith('#'):
                continue
            url = urlsplit(target)
            require(target and not url.scheme and not url.netloc and not url.query, 'Nonlocal report link: ' + target)
            verify_member(unquote(url.path))
        return path, parsed

    _, index = document(info['index'])
    require(index.links == names and not index.rows and not index.images, 'Index navigation differs from page metadata.')
    paged_rows, paragraphs, images, file_sizes = [], set(), [], []
    for i, row in enumerate(info['pages']):
        path, parsed = document(row['file'])
        # Producer records UTF-8 text before Windows newline translation.
        require(len(path.read_text(encoding='utf-8').encode('utf-8')) == row['bytes'], 'Page normalized text-byte count differs.')
        file_sizes.append(path.stat().st_size)
        require(parsed.total_rows == row['table_rows'] <= 120 and len(parsed.rows) == row['data_rows'], 'Page row counts differ.')
        require(len(parsed.images) <= 1 and not {'study_report.html', 'report.html'} & set(parsed.links), 'Page reopens full HTML or combines figures.')
        require(info['index'] in parsed.links, 'Page lacks index navigation.')
        require(not i or names[i - 1] in parsed.links, 'Page lacks previous navigation.')
        require(i + 1 == len(names) or names[i + 1] in parsed.links, 'Page lacks next navigation.')
        paged_rows.extend(parsed.rows)
        paragraphs.update(parsed.paragraphs)
        images.extend(parsed.images)
    full = ReportReadback(verify_member('study_report.html', True).read_text(encoding='utf-8'))
    supplement = ReportReadback(verify_member('report.html', True).read_text(encoding='utf-8'), exclude_study=True)
    source_rows = full.rows + supplement.rows
    require(info['sources'] == [{'file': 'study_report.html', 'data_rows': len(full.rows)},
        {'file': 'report.html', 'scope': 'excluding the repeated study section and figures', 'data_rows': len(supplement.rows)}],
        'Page source metadata omits the original report or differs in row count.')
    require(paged_rows == source_rows and len(source_rows) == info['source_data_rows'], 'Page data rows were dropped or reordered.')
    require(hashlib.sha256(json.dumps(source_rows, ensure_ascii=False).encode('utf-8')).hexdigest() == info['source_rows_sha256'],
            'Source data-row digest differs.')
    require(images == full.images, 'Paginated figures differ from full study report.')
    require(set(full.paragraphs + supplement.paragraphs) <= paragraphs, 'An original scientific explanation is missing from paginated reports.')
    return {'status': 'passed', 'pages': len(names), 'data_rows': len(source_rows),
            'manifest_targets_verified': len(verified_targets), 'source_rows_sha256': info['source_rows_sha256'],
            'all_data_rows_preserved_in_order': True, 'original_report_explanations_preserved': True,
            'max_actual_page_bytes': max(file_sizes),
            'metadata_byte_scope': 'UTF-8 text with normalized line endings; actual Windows file sizes separately measured',
            'index': snapshot(output / info['index'])}


def verify_run(output, base_verifier):
    output = output_directory(output)
    result, output_hashes = verify_output_hashes(output)
    require(result.get('stage') == 'D' and result['status'] == 'completed', 'This is not a completed D job.')
    require(not (output / 'INCOMPLETE.json').exists(), 'Completed job still carries INCOMPLETE.json.')
    summary = base_verifier.verify(output)
    audit, record = read(output / 'audit.json'), read(output / 'final_record.json')
    exported = read(output / 'evaluation.json')
    ev, settings = exported['evaluation'], audit['settings']
    require(record == ev['final_record'] == audit['final_record'], 'Final model records differ.')
    name = record['model']['id']
    require(name in MODELS, 'Unexpected D acceptance model.')
    temporal = name != 'tabpfn'
    require(settings['validation']['method'] == ('temporal' if temporal else 'group'), 'Wrong fixture validation method.')
    require(settings['uncertainty']['method'] == ('spatial_conformal' if temporal else 'split_conformal'), 'Wrong fixture uncertainty method.')
    require(settings['samples']['unit'] == 'g/kg' and settings['samples']['target_field'] == 'soil_value', 'Fixture target/unit changed.')
    require(settings['validation']['independent_ids'] == [] and ev['independent_predictions'] == [], 'Unexpected independent fixture subset.')
    require(ev['metrics']['independent'] is None, 'Absent independent data cannot have metrics.')
    with Path(settings['samples']['path']).open(encoding='utf-8-sig', newline='') as stream:
        samples = list(csv.DictReader(stream))
    lookup = {r['sample_id']: r for r in samples}
    require(len(samples) == len(lookup) == 48, 'D fixture must have 48 distinct observations.')
    require(len({r['observation_date'] for r in samples}) == 24, 'D fixture must have 24 dates.')
    require(set(audit['groups']) == set(lookup), 'Group map does not cover fixture IDs.')
    fixture_hashes = {Path(r['path']).name: r['sha256'] for r in read(ROOT / 'examples-d/fixture-manifest.json')['sources']}
    for source in audit['sources']:
        if Path(source['path']).name in fixture_hashes:
            require(source['sha256'] == fixture_hashes[Path(source['path']).name], 'Fixture content changed.')
    dates = {i: date.fromisoformat(r['observation_date']) for i, r in lookup.items()}
    gap = settings['validation'].get('gap_days', 0)
    groups = audit['groups']
    fold_lookup = {(f['repeat'], f['fold']): f for f in ev['folds']}
    require(len(fold_lookup) == len(ev['folds']) == 2, 'Expected two unique outer folds.')
    expected_oof = {(f['repeat'], f['fold'], i) for f in ev['folds'] for i in f['test_ids']}
    require(len(ev['oof']) == len(expected_oof) and {(r['repeat'], r['fold'], r['sample_id']) for r in ev['oof']} == expected_oof, 'Outer predictions do not match held-out IDs.')
    require(len(ev['oof']) == (24 if temporal else 48), 'Unexpected OOF coverage for the fixture.')
    point_metrics(ev['oof'], ev['metrics']['pooled_oof'])
    interval_summary(ev['oof'], ev['metrics']['pooled_oof']['interval'])
    for repeat in ev['metrics']['per_repeat']:
        repeated_rows = [r for r in ev['oof'] if r['repeat'] == repeat['repeat']]
        point_metrics(repeated_rows, repeat)
        interval_summary(repeated_rows, repeat['interval'])
    for row in ev['oof']:
        require(row['group_id'] == groups[row['sample_id']] and row['observed'] == float(lookup[row['sample_id']]['soil_value']), 'OOF source identity/value differs.')
    records = {'final': record}
    for item in ev['fold_metrics']:
        role = 'outer/%s/%s' % (item['repeat'], item['fold'])
        records[role] = item['model_record']
        fold = fold_lookup[item['repeat'], item['fold']]
        used = set(item['model_record']['development_ids'] + item['model_record']['calibration_ids'])
        require(used <= set(fold['train_ids']) and used.isdisjoint(fold['test_ids']), 'Outer training/calibration reaches its held-out fold.')
        if temporal:
            chronology(fold['train_ids'], fold['test_ids'], dates, gap)
        rows = [r for r in ev['oof'] if (r['repeat'], r['fold']) == (item['repeat'], item['fold'])]
        point_metrics(rows, item['metrics'])
        interval_summary(rows, item['metrics']['interval'])
    for role, rec in records.items():
        train, cal = set(rec['development_ids']), set(rec['calibration_ids'])
        require(train and cal and train.isdisjoint(cal), 'Training/calibration separation is missing.')
        require({groups[i] for i in train}.isdisjoint(groups[i] for i in cal), 'Training/calibration groups overlap.')
        for field, identities, metric, scope in (('training_predictions', train, 'train_metrics', 'resubstitution'),
                                                ('calibration_predictions', cal, 'calibration_metrics', 'calibration_diagnostic')):
            rows = rec[field]
            require(len(rows) == len(identities) and {r['sample_id'] for r in rows} == identities, 'Diagnostic prediction identities differ.')
            require(all(r['scope'] == scope and 'lower' not in r and r['group_id'] == groups[r['sample_id']] for r in rows), 'Point diagnostics have wrong roles or intervals.')
            point_metrics(rows, rec[metric])
        require(rec.get('effective_parameters') and rec.get('experimental_model_diagnostics', {}).get('producer_parameters'), 'Actual producer parameters are missing.')
        if temporal:
            chronology(train, cal, dates, gap)
            require(rec['temporal']['development_samples'] == len(train) and rec['temporal']['calibration_samples'] == len(cal), 'Temporal model counts differ.')
    for trial in ev['trials']:
        role = trial['role'].removesuffix('/search')
        require(role in records, 'Unbound inner search role.')
        development = set(records[role]['development_ids'])
        if trial['status'] != 'complete':
            require(trial['validation_metrics'] is None and trial['validation_status'] == 'incomplete' and not trial['selected'], 'Failed trial reports complete success.')
            continue
        require(trial['validation_status'] == 'complete', 'Complete trial lacks complete validation status.')
        point_metrics(trial['validation_predictions'], trial['validation_metrics'])
        inner_records = {(r['repeat'], r['fold']): r for r in trial['inner_fold_metrics']}
        require(len(inner_records) == len(trial['inner_folds']), 'Inner metric/fold counts differ.')
        for fold in trial['inner_folds']:
            require(set(fold['train_ids'] + fold['test_ids']) <= development, 'Inner fold leaks beyond development IDs.')
            require({groups[i] for i in fold['train_ids']}.isdisjoint(groups[i] for i in fold['test_ids']), 'Inner fold shares groups.')
            metric = inner_records[fold['repeat'], fold['fold']]
            rows = [r for r in trial['validation_predictions'] if (r['repeat'], r['fold']) == (fold['repeat'], fold['fold'])]
            point_metrics(rows, metric['metrics'])
            require(metric['training_sample_count'] == len(fold['train_ids'])
                    and metric['test_sample_count'] == len(fold['test_ids'])
                    and metric['training_group_count'] == len({groups[i] for i in fold['train_ids']})
                    and metric['test_group_count'] == len({groups[i] for i in fold['test_ids']}), 'Inner metric population differs.')
            if temporal:
                chronology(fold['train_ids'], fold['test_ids'], dates, gap)
        for repeat in trial['validation_per_repeat']:
            rows = [r for r in trial['validation_predictions'] if r['repeat'] == repeat['repeat']]
            point_metrics(rows, repeat)
    roles = Counter(f['properties']['role'] for f in read(output / 'folds.geojson')['features'])
    require(roles['independent'] == 0 and roles['test'] == len(ev['oof']), 'Map fold roles differ.')
    if temporal:
        require(roles['initial_training'] == 24, 'Initial history is not identified separately.')
    arrays, raster_metadata = {}, {}
    with rasterio.open(settings['covariates'][0]['path']) as reference:
        grid = (reference.width, reference.height, reference.crs, reference.transform)
    for entry in result['outputs']:
        if not entry['path'].endswith('.tif'):
            continue
        with rasterio.open(output / entry['path']) as src:
            tag = src.tags()
            require((src.width, src.height, src.crs, src.transform) == grid and src.count == 1, 'Output grid differs.')
            require(src.nodata is not None and np.isnan(src.nodata), 'Expected explicit NaN NoData.')
            product = tag['product']
            unit = 'g/kg' if product in ('prediction', 'lower', 'upper', 'width') else 'm' if product == 'nearest_distance' else '1'
            require(src.units == (unit,) and tag['target_unit'] == unit, 'Raster units differ.')
            require(tag['target'] == 'soil_value' and tag['uncertainty_method'] == settings['uncertainty']['method'], 'Raster scientific labels differ.')
            require(src.descriptions == ('soil_value / ' + product,), 'Raster band label differs.')
            require(tag['core_version'] == '0.4.1', 'Acceptance raster is not from core 0.4.1.')
            value = src.read(1, masked=True)
            require(np.isfinite(value.compressed()).all(), 'Infinite raster values.')
            arrays[product] = value
            raster_metadata[product] = {'units': unit, 'label': src.descriptions[0], 'valid_pixels': int(value.count())}
    require(arrays['prediction'].count() == 64, 'Point predictions do not cover all 64 valid fixture cells.')
    lower, upper, width = arrays['lower'], arrays['upper'], arrays['width']
    require(np.all((upper - lower).compressed() >= 0), 'Reversed raster intervals.')
    if temporal:
        support = arrays['interval_available']
        require(support.count() == 64, 'Support raster masks valid input cells.')
        available = support.data == 1
        np.testing.assert_array_equal(np.ma.getmaskarray(lower), ~available)
        require(np.all(np.isfinite(arrays['prediction'].data[~available])), 'Unsupported interval erased point predictions.')
        support_record = audit['interval_support']
        require(support_record['unavailable_input_pixels'] == int((~available).sum())
                and support_record['denominator'] == 'valid_input_pixels_before_prediction_mask', 'Spatial support denominator differs.')
        for key in ('interval_effective_n', 'interval_effective_group_n'):
            require(arrays[key].count() == 64 and np.all(arrays[key].compressed() >= 0), 'Effective local counts are invalid.')
    else:
        require(lower.count() == upper.count() == width.count() == 64, 'Global conformal intervals are unexpectedly missing.')
    bundle = next(e for e in result['outputs'] if e['kind'] == 'model_bundle')
    with zipfile.ZipFile(output / bundle['path']) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        metadata = json.loads(archive.read(manifest['metadata_path']))
        model_hash = manifest['members'][manifest['model_path']]
        require(manifest['run_id'] == metadata['run_id'] == result['run_id'], 'Package model run IDs differ.')
        require(model_hash == manifest['model_sha256'] == metadata['sha256'], 'Package model digest binding differs.')
        require(json.loads(archive.read('settings.json')) == settings, 'Packaged settings differ.')
        require(json.loads(archive.read('study/study_summary.json')) == read(output / 'study_summary.json'),
                'Packaged study summary or pagination metadata differs.')
    study = read(output / 'study_summary.json')
    pagination = verify_pagination(output, result, study)
    require(study['run_id'] == result['run_id'] and study['independent_status'] == 'not_applicable', 'Study role/run metadata differs.')
    require(study['study_context']['model_artifact']['sha256'] == model_hash, 'Study model digest differs.')
    with (output / 'publication_metrics.csv').open(encoding='utf-8-sig', newline='') as stream:
        publication = list(csv.DictReader(stream))
    require(len(publication) == len(study['metrics']), 'Publication CSV/JSON metric counts differ.')
    for row, original in zip(publication, study['metrics']):
        require(row['scope'] == original['scope'] and row['metric'] == original['metric'], 'Publication metric role/order differs.')
        require(row['value'] == '' if original['value'] is None else np.isclose(float(row['value']), original['value']), 'Publication metric value differs.')
    report = html.unescape((output / 'report.html').read_text(encoding='utf-8'))
    detail = html.unescape((output / 'study_report.html').read_text(encoding='utf-8'))
    for phrase in ('R², Pearson r² değildir', 'İç doğrulama model ve parametre seçimine katılır', 'bağımsız doğrulama başarısı değildir', 'bağımsız test sonucu yoktur'):
        require(phrase in detail, 'Study HTML omits scientific meaning: ' + phrase)
    if temporal:
        for phrase in ('İlk eğitim geçmişi dış katlarda sınanmaz', 'Mekânsal kapsama güvencesi vermez', 'Yerel destek yoksa nokta tahmini korunur'):
            require(phrase in report, 'Temporal/spatial HTML omits scientific meaning: ' + phrase)
    else:
        require('Built with PriorLabs-TabPFN' in report and 'Prior Labs License 1.1' in report, 'TabPFN attribution missing.')
    summary.update(stage='D', manifest=snapshot(output / 'result.json'), model_sha256=model_hash,
                   report=snapshot(output / 'study_report.html'), publication_metric_rows=len(publication),
                   raster_metadata=raster_metadata, extended_readback='passed',
                   pagination=pagination,
                   pooled_oof_metrics=ev['metrics']['pooled_oof'],
                   unavailable_interval_oof_rows=sum(r.get('lower') is None for r in ev['oof']),
                   unsupported_map_cells=64 - int(lower.count()),
                   unsupported_map_point_preservation=('verified_on_actual_unsupported_cells' if lower.count() < 64 else
                       'not_exercised; all final map cells have interval support'))
    return summary, output_hashes


def verify_roundtrip(args, run_summaries, output_rows, project_helper):
    checks, details = {}, {}
    def check(name, condition):
        checks[name] = bool(condition)
    cancelled = output_directory(args.cancel_run)
    result = read(cancelled / 'result.json')
    check('cancelled_manifest_status', result.get('status') == 'cancelled' and result.get('stage') == 'D')
    check('cancelled_outputs_empty', result.get('outputs') == [])
    check('cancelled_error_code', result.get('error', {}).get('code') == 'CANCELLED')
    check('cancellation_request_present', (cancelled / 'cancel.request').is_file())
    check('cancelled_incomplete_record_present', (cancelled / 'INCOMPLETE.json').is_file()
          and read(cancelled / 'INCOMPLETE.json').get('status') == 'cancelled')
    files = sorted(p for p in cancelled.rglob('*') if p.is_file())
    forbidden = [p.relative_to(cancelled).as_posix() for p in files if
                 p.suffix.lower() in ('.tif', '.joblib', '.pickle', '.pkl', '.onnx', '.ckpt', '.pth', '.zip')
                 or any(t in p.name.lower() for t in ('.partial', '.tmp', '.joblib.meta.json'))
                 or p.name in ('report.html', 'study_report.html') or any(part.startswith('.report-') for part in p.parts)]
    check('no_partial_raster_or_model_artifacts', not forbidden)
    progress_path = cancelled / 'progress.jsonl'
    progress = [json.loads(line) for line in progress_path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    check('progress_ends_cancelled', bool(progress) and progress[-1].get('stage') == 'cancelled')
    previous = output_directory(args.previous_run)
    prior, prior_hashes = verify_output_hashes(previous)
    accepted_ids = {r['run_id'] for r in run_summaries}
    check('previous_run_completed', prior['status'] == 'completed' and prior['run_id'] in accepted_ids)
    check('all_previous_output_hashes_unchanged', all(row['matches'] for rows in output_rows.values() for row in rows) and bool(prior_hashes))
    original, roundtrip = read(args.saved_project), read(args.roundtrip_project)
    stable_changes = project_helper.differences({k: v for k, v in original.items() if k not in TRANSIENT},
                                              {k: v for k, v in roundtrip.items() if k not in TRANSIENT})
    for field in ('science', 'samples', 'covariates', 'output_root', 'window_size', 'last_manifest',
                  'input_type', 'csv_path', 'point_source', 'point_name', 'point_scope', 'selected_only'):
        check('project_' + field + '_matches', field in original and field in roundtrip and original[field] == roundtrip[field])
    check('project_unit_matches', bool(original.get('samples', {}).get('unit'))
          and original['samples']['unit'] == roundtrip.get('samples', {}).get('unit'))
    check('all_persistent_project_fields_match', not stable_changes)
    check('last_manifest_points_to_previous_success', Path(roundtrip.get('last_manifest', '')).resolve() == (previous / 'result.json').resolve())
    preview_audits = []
    for label, project in (('original', original), ('roundtrip', roundtrip)):
        audit_path = Path(project['preview_manifest']).parent / 'audit.json'
        entries = read(audit_path)['sources']
        check('preview_' + label + '_source_hashes_match', bool(entries) and all(digest(e['path']) == e['sha256'] for e in entries))
        preview_audits.append({'project': label, 'audit': snapshot(audit_path),
                               'sources': {str(Path(e['path']).resolve()).casefold(): e['sha256'] for e in entries}})
    check('preview_input_content_hashes_unchanged', preview_audits[0]['sources'] == preview_audits[1]['sources'])
    export_run = output_directory(args.export_run or args.previous_run)
    export_result = read(export_run / 'result.json')
    bundle = [e for e in export_result['outputs'] if e['kind'] == 'model_bundle']
    check('export_belongs_to_completed_D_run', export_result['status'] == 'completed' and export_result['run_id'] in accepted_ids and len(bundle) == 1)
    require(len(bundle) == 1, 'Exactly one exported model source is required.')
    source = local_member(export_run, bundle[0]['path'])
    exported_model = Path(args.exported_model).resolve()
    check('exported_model_is_distinct_copy', exported_model != source)
    check('exported_model_sha256_matches', digest(exported_model) == digest(source) == bundle[0]['sha256'])
    observation = read(args.process_evidence)
    require(set(observation) <= {'schema_version', 'run_id', 'worker_pid', 'exit_code', 'exit_code_source', 'process_observation'},
            'Unexpected process evidence fields; keep unrelated application data out.')
    process = observation['process_observation']
    require(set(process) <= {'observed_at_utc', 'status', 'scope', 'run_id', 'captured_process_ids', 'remaining_processes'},
            'Process observation must contain only target-scoped fields.')
    pids = process.get('captured_process_ids', [])
    check('process_evidence_matches_cancel_run', observation.get('run_id') == process.get('run_id') == result['run_id'])
    check('target_process_ids_recorded', type(observation.get('worker_pid')) is int and observation['worker_pid'] > 0
          and observation['worker_pid'] in pids and all(type(pid) is int and pid > 0 for pid in pids))
    check('target_process_query_completed', process.get('status') == 'completed' and process.get('scope') == 'captured_worker_tree_and_run_id')
    check('no_observed_target_worker_or_descendants', process.get('remaining_processes') == [])
    observed_at = datetime.fromisoformat(process['observed_at_utc'].replace('Z', '+00:00'))
    check('process_observation_after_cancel_result', observed_at.tzinfo is not None
          and observed_at.timestamp() >= (cancelled / 'result.json').stat().st_mtime - 5
          and observed_at.timestamp() <= datetime.now(timezone.utc).timestamp() + 5)
    exit_source = observation['exit_code_source']
    require(exit_source.get('kind') in ('log', 'ui_screenshot'), 'Exit observation source must be log or UI screenshot.')
    check('recorded_exit_code_is_130', observation.get('exit_code') == 130)
    if exit_source['kind'] == 'log':
        text = Path(exit_source['path']).read_text(encoding='utf-8-sig')
        check('exit_code_log_contains_130', re.search(r'(?:İşçi çıkış kodu|exit_code)\s*[:=]\s*130\b', text, re.I) is not None)
    details['process_observation'] = {'evidence': snapshot(args.process_evidence),
        'worker_pid': observation['worker_pid'], 'exit_code': observation['exit_code'],
        'exit_code_source': snapshot(exit_source['path']), 'exit_code_source_kind': exit_source['kind'],
        'exit_code_scope': 'external UI observation; screenshot not interpreted by this program' if exit_source['kind'] == 'ui_screenshot' else 'matched exit-code text in supplied log',
        'target_query': process}
    failed = [name for name, passed in checks.items() if not passed]
    return {'schema_version': 1, 'stage': 'D', 'verified_at_utc': datetime.now(timezone.utc).isoformat(),
        'status': 'passed' if not failed else 'failed', 'checks': checks, 'failed_checks': failed,
        'cancelled_run': {'run_id': result['run_id'], 'manifest': snapshot(cancelled / 'result.json'), 'outputs': result['outputs'],
                          'files': [snapshot(p) | {'relative_path': p.relative_to(cancelled).as_posix()} for p in files],
                          'forbidden_artifacts': forbidden, 'progress_stages': [r.get('stage') for r in progress]},
        'previous_success': {'run_id': prior['run_id'], 'manifest': snapshot(previous / 'result.json'),
            'baseline': 'Successful result.json output digests, rechecked after cancellation.',
            'output_count': len(prior_hashes), 'unchanged_count': len(prior_hashes), 'output_hashes': prior_hashes},
        'previous_successes': [{'run_id': key, 'unchanged_count': len(rows)} for key, rows in output_rows.items()],
        'project_roundtrip': {'original': snapshot(args.saved_project), 'roundtrip': snapshot(args.roundtrip_project),
            'persistent_differences': stable_changes, 'excluded_transient_fields': sorted(TRANSIENT),
            'transient_differences': project_helper.differences({k: original.get(k) for k in TRANSIENT}, {k: roundtrip.get(k) for k in TRANSIENT}),
            'exclusion_reason': 'Only session preview records are excluded. Scientific settings, input paths, units and last_manifest remain compared.',
            'preview_source_audits': preview_audits},
        'model_export': {'source_run_id': export_result['run_id'], 'source': snapshot(source), 'exported': snapshot(exported_model),
                         'sha256_matches': checks['exported_model_sha256_matches']},
        'screenshots_present': [snapshot(path) for path in args.screenshot], **details,
        'scope': 'Independent file readback plus externally supplied targeted process/UI observations; no jobs, models or UI actions started.'}


def write_new(path, value):
    path = Path(path).resolve()
    require(path.is_relative_to(ROOT / 'build'), 'Acceptance evidence must stay under build/.')
    require(not path.exists(), 'Evidence already exists; choose a new output path: ' + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + '.partial')
    try:
        with partial.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        require(read(partial) == value, 'Evidence JSON readback differs.')
        require(not path.exists(), 'Evidence destination appeared during writing.')
        partial.rename(path)
    finally:
        partial.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--run', type=Path, action='append', required=True, help='Actual completed run directory or its output directory; repeat exactly three times.')
    parser.add_argument('--cancel-run', type=Path, required=True)
    parser.add_argument('--previous-run', type=Path, required=True)
    parser.add_argument('--saved-project', type=Path, required=True)
    parser.add_argument('--roundtrip-project', type=Path, required=True)
    parser.add_argument('--exported-model', type=Path, required=True)
    parser.add_argument('--export-run', type=Path)
    parser.add_argument('--process-evidence', type=Path, required=True)
    parser.add_argument('--ui-tested-package', type=Path, required=True)
    parser.add_argument('--ui-package-sha256', required=True)
    parser.add_argument('--screenshot', type=Path, action='append', default=[])
    parser.add_argument('--readback-output', type=Path, default=EVIDENCE / 'artifact-readback-D.json')
    parser.add_argument('--roundtrip-output', type=Path, default=EVIDENCE / 'cancel-roundtrip-readback-D.json')
    args = parser.parse_args()
    require(sys.flags.optimize == 0, 'Python -O disables the existing verifier assertions; do not use it.')
    require(len(args.run) == 3, 'Exactly three completed D run paths are required.')
    require(not args.readback_output.exists() and not args.roundtrip_output.exists(), 'Evidence destination already exists; use new output paths.')
    require(re.fullmatch(r'[0-9a-f]{64}', args.ui_package_sha256) is not None
            and digest(args.ui_tested_package) == args.ui_package_sha256, 'UI-tested package digest differs.')
    candidate = snapshot(args.ui_tested_package)
    candidate['scope'] = 'Operator-identified package used for these three actual UI model runs and cancellation/roundtrip; later native metadata changes are not covered.'
    with zipfile.ZipFile(args.ui_tested_package) as archive:
        require(archive.testzip() is None, 'UI-tested package CRC failed.')
        for path in args.run:
            for source in read(output_directory(path) / 'audit.json')['source_code']:
                member = 'Install/backend/sps/' + Path(source['path']).name
                require(hashlib.sha256(archive.read(member)).hexdigest() == source['sha256'],
                        'Run backend differs from its recorded UI package: ' + member)
    base = load_helper('sps_D_artifact_readback', ROOT / 'tools/verify_d_outputs.py')
    project = load_helper('sps_C_project_comparison', ROOT / 'tools/verify_c_cancel_roundtrip.py')
    summaries, hashes = [], {}
    for path in args.run:
        summary, rows = verify_run(path, base)
        summaries.append(summary)
        hashes[summary['run_id']] = rows
    require(len(hashes) == 3 and {r['model'] for r in summaries} == MODELS, 'Three unique runs and all three D families are required.')
    readback = {'schema_version': 1, 'stage': 'D', 'status': 'passed_complete_suite',
                'verified_at_utc': datetime.now(timezone.utc).isoformat(), 'runs': summaries,
                'ui_tested_package': candidate,
                'verifier': snapshot(Path(__file__)), 'scope': 'Actual supplied completed jobs only; no model refitting or UI automation.'}
    roundtrip = verify_roundtrip(args, summaries, hashes, project)
    roundtrip['ui_tested_package'] = candidate
    # The run readback is independent of a failed cancellation/roundtrip check.
    write_new(args.readback_output, readback)
    write_new(args.roundtrip_output, roundtrip)
    print(json.dumps({'readback': readback['status'], 'roundtrip': roundtrip['status'],
                      'failed_checks': roundtrip['failed_checks'], 'runs': [r['run_id'] for r in summaries]}, ensure_ascii=False))
    return 0 if roundtrip['status'] == 'passed' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, AssertionError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'incomplete_or_failed', 'error': str(exc),
                          'acceptance_claim': False}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
