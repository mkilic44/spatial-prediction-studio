"""Read metadata UI evidence without starting a model, process query or application.

Native rejection observation schema:
 {run_root, before:{observed_at_utc,run_directories:[names]},
  after:{observed_at_utc,run_directories:[names]},
  preview:{status:'rejected',error_code:'TARGET_LEAKAGE',evidence:{path,kind:'log'|'ui_screenshot'}},
  run:{status:'disabled_after_preview_rejection',evidence:{path,kind:'ui_screenshot'}}}
The run rejection is separately bound to the actual native contract JSON/log.
Screenshots represent external UI observations; this helper performs no OCR.
Only the requested new evidence JSON is written. Existing evidence is not replaced.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('d_acceptance_helpers', ROOT / 'build/verify_d_acceptance.py')
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)
read, snapshot, require = helpers.read, helpers.snapshot, helpers.require
FIELDS = ('resampling', 'sha256', 'band_description', 'time_representation',
          'original_resolution', 'provenance', 'target_derived')


def typed_equal(left, right):
    """JSON number is one type; booleans never compare equal to numbers."""
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return set(left) == set(right) and all(typed_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(typed_equal(a, b) for a, b in zip(left, right))
    return left == right


def metadata_matches(source, other):
    originals = source['covariates']
    targets = other['covariates']
    if [r['id'] for r in originals] != [r['id'] for r in targets]:
        return False
    return all(typed_equal({k: row[k] for k in FIELDS if k in row},
                          {k: target[k] for k in FIELDS if k in target})
               for row, target in zip(originals, targets))


def evaluation_ids(value):
    """Only declared observation roles; excluded-source reasons are not model input."""
    keys = {'sample_ids', 'ids', 'development_ids', 'calibration_ids', 'test_ids',
            'train_ids', 'independent_ids', 'training_ids'}
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'sample_id':
                yield str(child)
            elif key in keys and isinstance(child, list):
                yield from (str(item) for item in child)
            else:
                yield from evaluation_ids(child)
    elif isinstance(value, list):
        for child in value:
            yield from evaluation_ids(child)


def verify(args):
    checks = {}
    def check(name, condition):
        checks[name] = bool(condition)
    original, saved = read(args.source_project), read(args.saved_project)
    blocked, blocked_saved = read(args.blocked_project), read(args.blocked_saved_project)
    output = helpers.output_directory(args.run)
    result, output_hashes = helpers.verify_output_hashes(output)
    request = read(output / 'request.snapshot.json')
    audit = read(output / 'audit.json')
    evaluation = read(output / 'evaluation.json')['evaluation']
    preflight = read(output / 'preflight.json')
    check('accepted_result_completed', result.get('status') == 'completed' and result.get('stage') in ('B', 'C', 'D'))
    check('accepted_model_is_ridge', evaluation['final_record']['model']['id'] == 'ridge')
    check('accepted_run_has_no_incomplete_marker', not (output / 'INCOMPLETE.json').exists())
    check('fixture_exercises_all_seven_metadata_fields', all(set(FIELDS) <= set(row) for row in original['covariates']))
    check('fixture_uses_nested_object_and_list', any(isinstance(row.get('provenance'), dict) and
          isinstance(row.get('original_resolution'), list) for row in original['covariates']))
    check('accepted_source_target_flags_are_false_booleans', all(row.get('target_derived') is False for row in original['covariates']))
    for label, value in (('saved', saved), ('request', request), ('audit', audit['settings'])):
        check('typed_covariates_' + label + '_match_source', metadata_matches(original, value))
        check('excluded_' + label + '_matches_source', typed_equal(original['samples'].get('excluded'), value['samples'].get('excluded')))
    check('blocked_flag_is_true_boolean', any(row.get('target_derived') is True for row in blocked['covariates']))
    check('blocked_gui_save_preserves_typed_metadata', metadata_matches(blocked, blocked_saved))
    check('blocked_gui_save_preserves_exclusions', typed_equal(blocked['samples'].get('excluded'), blocked_saved['samples'].get('excluded')))
    source_ids = []
    with Path(original['csv_path']).open(encoding='utf-8-sig', newline='') as stream:
        source_ids = [row[original['samples']['id_field']] for row in csv.DictReader(stream)]
    excluded = {row['id'] for row in original['samples']['excluded']}
    retained = set(source_ids) - excluded
    check('excluded_ids_are_real_unique_source_observations', bool(excluded) and excluded <= set(source_ids) and len(source_ids) == len(set(source_ids)))
    check('audit_groups_cover_only_retained_observations', set(audit['groups']) == retained)
    check('preflight_count_excludes_removed_observations', preflight['sample_count'] == len(retained))
    check('excluded_ids_absent_from_learning_validation_and_predictions', excluded.isdisjoint(evaluation_ids(evaluation)))
    check('group_oof_covers_exact_retained_observations', {r['sample_id'] for r in evaluation['oof']} == retained)
    for source in audit['sources'] + audit['source_code']:
        require(helpers.digest(source['path']) == source['sha256'], 'Source changed: ' + source['path'])
    check('run_source_hashes_match', True)
    for row in original['covariates']:
        require(helpers.digest(row['path']) == row['sha256'], 'Covariate metadata fingerprint differs.')
    check('covariate_metadata_fingerprints_match_actual_rasters', True)

    observation = read(args.blocked_observation)
    run_root = Path(observation['run_root']).resolve()
    before, after = observation['before'], observation['after']
    require(set(observation) <= {'schema_version', 'run_root', 'before', 'after', 'preview', 'run'}, 'Unexpected UI observation fields.')
    for record in (before, after):
        require(set(record) <= {'observed_at_utc', 'run_directories'}, 'Only scoped run-directory names are allowed.')
        require(all(isinstance(name, str) and Path(name).name == name and name not in ('.', '..') for name in record['run_directories']), 'Invalid run-directory name.')
        require(len(record['run_directories']) == len(set(record['run_directories'])), 'Duplicate observed directory.')
    start = datetime.fromisoformat(before['observed_at_utc'].replace('Z', '+00:00'))
    end = datetime.fromisoformat(after['observed_at_utc'].replace('Z', '+00:00'))
    check('rejection_inventory_has_ordered_timestamps', start.tzinfo is not None and end.tzinfo is not None
          and start <= end <= datetime.now(timezone.utc))
    check('rejection_root_matches_project', run_root == Path(blocked['output_root']).resolve())
    check('preview_rejection_created_no_job_directory', sorted(before['run_directories']) == sorted(after['run_directories']))
    check('current_run_inventory_matches_after_rejection', sorted(p.name for p in run_root.iterdir() if p.is_dir()) == sorted(after['run_directories']))
    preview, run = observation['preview'], observation['run']
    check('ui_preview_rejected_for_target_leakage', preview.get('status') == 'rejected' and preview.get('error_code') == 'TARGET_LEAKAGE')
    check('ui_run_stayed_disabled_after_rejection', run.get('status') == 'disabled_after_preview_rejection')
    observation_files = {}
    for label, value in (('preview', preview), ('run', run)):
        evidence = value['evidence']
        require(evidence['kind'] in ('log', 'ui_screenshot'), 'UI evidence must identify a log or screenshot.')
        if label == 'preview' and evidence['kind'] == 'log':
            check('ui_log_records_target_leakage', 'TARGET_LEAKAGE' in Path(evidence['path']).read_text(encoding='utf-8-sig'))
        if label == 'run':
            require(evidence['kind'] == 'ui_screenshot', 'Disabled button requires the observed UI screenshot.')
        observation_files[label] = snapshot(evidence['path']) | {'kind': evidence['kind']}
    native = read(args.native_contract)
    check('native_contract_passed', native.get('status') == 'passed')
    for key in ('preview_target_leakage_rejected_before_output', 'run_target_leakage_rejected_before_output'):
        check('native_' + key, native.get(key) is True)
    native_log = Path(args.native_log).read_text(encoding='utf-8-sig')
    for action in ('preview', 'run'):
        check('native_log_' + action + '_rejection', 'PASS TARGET_LEAKAGE rejects ' + action + ' before request or output directory creation' in native_log)

    require(helpers.digest(args.ui_tested_package) == args.ui_package_sha256, 'Metadata UI package digest differs.')
    with zipfile.ZipFile(args.ui_tested_package) as archive:
        require(archive.testzip() is None, 'Metadata UI package CRC differs.')
        for source in audit['source_code']:
            content = archive.read('Install/backend/sps/' + Path(source['path']).name)
            require(helpers.hashlib.sha256(content).hexdigest() == source['sha256'], 'Ridge backend differs from metadata UI package.')
    check('metadata_ui_package_matches_recorded_sha_and_backend', True)
    failed = [key for key, value in checks.items() if not value]
    return {'schema_version': 1, 'stage': 'D', 'status': 'failed' if failed else 'passed',
        'verified_at_utc': datetime.now(timezone.utc).isoformat(), 'failed_checks': failed, 'checks': checks,
        'source_project': snapshot(args.source_project), 'saved_project': snapshot(args.saved_project),
        'typed_optional_fields': list(FIELDS), 'json_type_scope': 'JSON numbers share one type; booleans, strings, arrays, objects and null remain distinct',
        'accepted_run': {'run_id': result['run_id'], 'request': snapshot(output / 'request.snapshot.json'),
            'manifest': snapshot(output / 'result.json'), 'output_hashes_verified': len(output_hashes),
            'source_count': len(source_ids), 'excluded_ids': sorted(excluded), 'retained_count': len(retained),
            'unique_oof_samples': len({r['sample_id'] for r in evaluation['oof']})},
        'blocked': {'source_project': snapshot(args.blocked_project), 'saved_project': snapshot(args.blocked_saved_project),
            'ui_observation': snapshot(args.blocked_observation), 'observation_files': observation_files,
            'native_contract': snapshot(args.native_contract), 'native_log': snapshot(args.native_log),
            'observation_scope': 'Externally observed preview failure and disabled Run button; screenshots are not OCR-interpreted. Both PrepareRequest actions independently checked by native contract.'},
        'ui_tested_package': snapshot(args.ui_tested_package), 'verifier': snapshot(Path(__file__)),
        'scope': 'Separate metadata-preservation UI/Ridge evidence. This does not replace or relabel the three model runs made with the earlier paginated UI package.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ('source-project', 'saved-project', 'run', 'blocked-project', 'blocked-saved-project',
                 'blocked-observation', 'native-contract', 'native-log', 'ui-tested-package'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--ui-package-sha256', required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'build/arcmap-evidence/metadata-readback-D.json')
    args = parser.parse_args()
    require(not args.output.exists(), 'Metadata readback already exists; choose a new output path.')
    evidence = verify(args)
    helpers.write_new(args.output, evidence)
    print(json.dumps({'status': evidence['status'], 'failed_checks': evidence['failed_checks'],
                      'output': str(args.output)}, ensure_ascii=True))
    return 0 if evidence['status'] == 'passed' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, AssertionError, OSError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'incomplete_or_failed', 'acceptance_claim': False, 'error': str(exc)}, ensure_ascii=True), file=sys.stderr)
        raise SystemExit(2)
