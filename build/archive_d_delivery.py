"""Archive D only after explicit UI, file-readback and cancellation evidence.

Preparation does not grant acceptance. This program starts no tests or models.
Example (replace the paths and hash with the actually accepted evidence):
  runtime-d/Scripts/python.exe build/archive_d_delivery.py \
    --ui-tested build/stage-d-ui-candidate/SpatialPredictionStudio.esriAddIn \
    --ui-sha256 <verified-UI-package-SHA256> \
    --readback build/arcmap-evidence/artifact-readback-D.json \
    --roundtrip build/arcmap-evidence/cancel-roundtrip-readback-D.json \
    --native-evidence build/native-tests/<final-native-test-directory>

The destination must not already exist. A failed preparation is retained in a
separate staging directory; no earlier accepted stage is changed or deleted.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import uuid
import zipfile
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'build'
DEST = BUILD / 'stage-d-accepted'
SERIES = {
    'core', 'neural', 'comparison', 'tabpfn', 'nested-tabm', 'nested-realmlp',
    'nested-tabpfn', 'publication-tabm', 'publication-realmlp', 'publication-tabpfn', 'package',
}
EXCLUDED_PARTS = {'bin', 'obj', '__pycache__', '.pytest_cache', '.vs', '.git', 'artifacts'}
EXCLUDED_SOURCE_SUFFIXES = {'.dll', '.exe', '.pdb', '.pyc', '.pyo', '.ckpt', '.joblib', '.zip', '.esriaddin'}
NATIVE_PROOFS = (
    'native_contract_result.json', 'test.log', 'panel/panel_contract_result.json',
    'panel.log', 'owned-process/result.json', 'owned-process.log',
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    require(path.is_file(), 'Required evidence is missing: ' + str(path))
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-fA-F]{64}', value) is not None


def local(path):
    path = path.resolve()
    require(path.is_relative_to(ROOT), 'Archive input must be inside the project: ' + str(path))
    return path


def relative(path):
    return local(path).relative_to(ROOT).as_posix()


def safe_member(name):
    require(isinstance(name, str) and name and '\\' not in name and ':' not in name,
            'Invalid archive member name: ' + str(name))
    p = PurePosixPath(name)
    require(not p.is_absolute() and '..' not in p.parts, 'Unsafe archive member: ' + name)


def zip_manifest(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), 'Duplicate package members: ' + str(path))
        for name in names:
            safe_member(name)
        require(archive.testzip() is None, 'Invalid package CRC: ' + str(path))
        manifest = json.loads(archive.read('Install/build-manifest.json'))
        require(manifest.get('version') == '0.4.1', 'Expected the final 0.4.1 package.')
        members = manifest.get('members', {})
        require(set(members) == set(names) - {'Install/build-manifest.json'}, 'Incomplete package member manifest.')
        for name, expected in members.items():
            require(valid_sha(expected) and digest_bytes(archive.read(name)) == expected, 'Changed package member: ' + name)
        require(not any(name.lower().endswith('.ckpt') for name in names), 'Foundation checkpoint must stay external to the add-in.')
        require(not any(PurePosixPath(name).name.lower().startswith('esri.') and name.lower().endswith('.dll') for name in names),
                'Proprietary Esri assemblies must not be redistributed.')
        return manifest


def current_manifest_sources():
    files = set()
    for folder, pattern in (
        ('native', '*.cs'), ('native', '*.csproj'), ('native', '*.sln'),
        ('backend', '*.py'), ('backend', '*.json'), ('backend', '*.R'),
        ('tools', '*.R'), ('tools', '*.py'), ('tools', '*.ps1'), ('tests', '*.py'),
    ):
        for path in (ROOT / folder).rglob(pattern):
            if not any(part.lower() in {'bin', 'obj', '__pycache__'} for part in path.relative_to(ROOT).parts):
                files.add(path)
    files.update((ROOT / 'tests/fixtures').rglob('*'))
    files.update(ROOT / name for name in (
        'Config.xml', 'requirements-lock.txt', 'requirements-d-lock.txt',
        'reference-r-conda-lock.txt', 'reference-r-packages.csv',
        'models/tabpfn-v2/weights-manifest.json', 'models/tabpfn-v2/LICENSE.txt',
    ))
    return {relative(path): sha(path) for path in sorted(files) if path.is_file()}


def verify_packages(tested, expected_ui_sha, final):
    require(valid_sha(expected_ui_sha), 'A separately recorded UI-tested package SHA-256 is required.')
    require(tested.is_file() and sha(tested) == expected_ui_sha.lower(), 'UI-tested package is missing or its SHA-256 changed.')
    old, new = zip_manifest(tested), zip_manifest(final)
    require(old.get('sources') == new.get('sources'), 'Application/test/tool source hashes changed after UI acceptance.')
    require(new.get('sources') == current_manifest_sources(), 'Final package sources no longer match the project or source file inventory.')
    require(old.get('external_assets') == new.get('external_assets'), 'External model assets changed after UI acceptance.')
    for name, expected in new.get('external_assets', {}).items():
        require(valid_sha(expected) and Path(name).is_file() and sha(Path(name)) == expected, 'Changed external model asset: ' + name)
    with zipfile.ZipFile(tested) as before, zipfile.ZipFile(final) as after:
        changed = sorted(name for name in set(before.namelist()) | set(after.namelist())
                         if name not in before.namelist() or name not in after.namelist() or before.read(name) != after.read(name))
        require(all(name.startswith('Install/docs/') or name == 'Install/build-manifest.json' for name in changed),
                'Non-document package changes after UI acceptance: ' + repr(changed))
        for name in after.namelist():
            if name.startswith(('Install/backend/', 'Install/examples/', 'Install/examples-d/', 'Install/licenses/', 'Install/licenses-d/')):
                source = ROOT / name[len('Install/'):]
                require(source.is_file() and sha(source) == digest_bytes(after.read(name)), 'Packaged data/code differs from project: ' + name)
            elif name.startswith('Install/docs/'):
                leaf = name[len('Install/docs/'):]
                source = ROOT / leaf if leaf in {'README.md', 'YONTEM_KILAVUZU.md', 'LISANSLAR.md'} else ROOT / 'docs' / leaf
                require(source.is_file() and sha(source) == digest_bytes(after.read(name)), 'Packaged documentation differs from project: ' + name)
        require(after.read('Config.xml') == (ROOT / 'Config.xml').read_bytes(), 'Config.xml changed after packaging.')
    return new, changed


def verify_backend(directory):
    summary = read(directory / 'summary.json')
    require(isinstance(summary, list) and len(summary) == len(SERIES), 'The backend test series is incomplete.')
    require({row.get('name') for row in summary} == SERIES, 'All 11 final backend groups must be present.')
    for row in summary:
        name = row['name']
        require(row.get('tests', 0) > 0 and all(row.get(key) == 0 for key in ('failures', 'errors', 'skipped')),
                'Backend group did not fully pass: ' + name)
        suites = ET.parse(directory / (name + '.xml')).getroot().findall('.//testsuite')
        require(suites, 'No JUnit suite for ' + name)
        actual = {key: sum(int(suite.get(key, '0')) for suite in suites) for key in ('tests', 'failures', 'errors', 'skipped')}
        require(all(row[key] == actual[key] for key in actual), 'Summary/XML mismatch: ' + name)
        require((directory / (name + '.log')).is_file(), 'Missing backend log: ' + name)
    return summary


def native_directory(argument):
    candidates = [argument] if argument is not None else sorted((BUILD / 'native-tests').glob('*'), reverse=True)
    for directory in candidates:
        if all((directory / name).is_file() for name in NATIVE_PROOFS):
            for name in NATIVE_PROOFS:
                if name.endswith('.json'):
                    require(read(directory / name).get('status') == 'passed', 'Native acceptance did not pass: ' + name)
            return local(directory)
    raise ValueError('A complete final native helper/panel/process-tree evidence directory is required.')


def verify_readback(path, runs_directory):
    record = read(path)
    require(record.get('status') == 'passed_complete_suite', 'The complete D ArcMap output readback has not passed.')
    runs = record.get('runs', [])
    require(len(runs) == 3 and {row.get('model') for row in runs} == {'tabm', 'realmlp', 'tabpfn'}, 'D readback must cover all three actual ArcMap models.')
    require(len({row.get('run_id') for row in runs}) == 3, 'D readback run IDs must be unique.')
    for row in runs:
        require(row.get('status') == 'passed', 'A D ArcMap run did not pass readback.')
        run_id = row.get('run_id')
        require(isinstance(run_id, str) and run_id and Path(run_id).name == run_id, 'Invalid D run ID.')
        output = local(runs_directory / run_id / 'output')
        require(output.is_relative_to(runs_directory.resolve()), 'D run escaped its evidence directory.')
        result = read(output / 'result.json')
        require(result.get('status') == 'completed' and result.get('run_id') == run_id, 'Actual D result is not completed.')
        entries = result.get('outputs', [])
        require(entries and len(entries) == row.get('output_hashes_verified'), 'Output readback count changed.')
        require(len({entry['path'] for entry in entries}) == len(entries), 'Duplicate D output paths.')
        for entry in entries:
            member = local(output / entry['path'])
            require(member.is_relative_to(output) and member.is_file(), 'D output is missing or outside its run.')
            require(valid_sha(entry.get('sha256')) and sha(member) == entry['sha256'], 'D output changed after readback: ' + str(member))
        bundles = [entry for entry in entries if entry.get('kind') == 'model_bundle']
        require(len(bundles) == 1 and bundles[0]['sha256'] == row.get('model_package_sha256'), 'Model package differs from accepted readback.')
        audit = read(output / 'audit.json')
        require(audit.get('sources') and audit.get('source_code'), 'Actual source fingerprints are required.')
        for source in audit['sources'] + audit['source_code']:
            require(Path(source['path']).is_file() and sha(Path(source['path'])) == source['sha256'],
                    'Input data or application code changed since the accepted run: ' + source['path'])
    return record


def verify_roundtrip(path, runs_directory, accepted_ids):
    record = read(path)
    require(record.get('status') == 'passed' and record.get('failed_checks') == [], 'Cancellation/project roundtrip did not pass.')
    checks = record.get('checks', {})
    required = {'cancelled_manifest_status', 'cancelled_outputs_empty', 'all_previous_output_hashes_unchanged',
                'all_persistent_project_fields_match', 'last_manifest_points_to_previous_success'}
    require(required <= set(checks) and all(value is True for value in checks.values()), 'Cancellation/roundtrip evidence is incomplete.')
    previous = record.get('previous_success', {})
    require(previous.get('run_id') in accepted_ids, 'Roundtrip must preserve an accepted D run, not an earlier stage.')
    cancelled = record.get('cancelled_run', {})
    manifest = cancelled.get('manifest', {})
    cancel_path = local(Path(manifest.get('path', '')))
    require(cancel_path.is_relative_to(runs_directory.resolve()), 'Cancellation evidence must belong to D runs.')
    require(valid_sha(manifest.get('sha256')) and cancel_path.is_file() and sha(cancel_path) == manifest['sha256'], 'Cancelled manifest changed.')
    actual = read(cancel_path)
    require(actual.get('status') == 'cancelled' and actual.get('outputs') == [], 'Cancelled D run has successful or partial outputs.')
    def verify_file_records(value):
        if isinstance(value, dict):
            if 'path' in value and 'sha256' in value:
                candidate = Path(value['path'])
                require(valid_sha(value['sha256']) and candidate.is_file() and sha(candidate) == value['sha256'],
                        'Roundtrip proof file changed: ' + str(candidate))
            for child in value.values():
                verify_file_records(child)
        elif isinstance(value, list):
            for child in value:
                verify_file_records(child)
    verify_file_records(record)
    return record


def source_files():
    files = set()
    for folder in ('native', 'backend', 'tools', 'tests', 'example', 'examples', 'examples-d', 'licenses', 'licenses-d', 'docs'):
        for path in (ROOT / folder).rglob('*'):
            parts = {part.lower() for part in path.relative_to(ROOT).parts}
            if path.is_file() and not parts & EXCLUDED_PARTS and path.suffix.lower() not in EXCLUDED_SOURCE_SUFFIXES:
                if folder == 'native' and path.suffix.lower() in {'.log', '.xml'}:
                    continue
                files.add(local(path))
    files.update(ROOT.glob('*.md'))
    files.update(ROOT.glob('*lock.txt'))
    files.update(ROOT / name for name in ('Config.xml', 'reference-r-packages.csv',
                 'models/tabpfn-v2/weights-manifest.json', 'models/tabpfn-v2/LICENSE.txt'))
    files.add(Path(__file__).resolve())
    files.add(BUILD / 'verify_d_acceptance.py')
    files.update(path for path in (BUILD / 'webbrowser-probe').glob('*')
                 if path.is_file() and path.suffix.lower() in {'.cs', '.html'})
    return sorted(files)


def evidence_files(args, native):
    files = set(path for path in args.runs.rglob('*') if path.is_file())
    files.update(path for path in args.backend_evidence.iterdir() if path.is_file() and path.suffix.lower() in {'.xml', '.log', '.json'})
    files.update(native / name for name in NATIVE_PROOFS)
    if (native / 'panel/layout_bounds.json').is_file():
        files.add(native / 'panel/layout_bounds.json')
    for folder in (BUILD / 'd-reports', BUILD / 'arcmap-evidence/d-reports'):
        files.update(path for path in folder.rglob('*') if path.is_file())
    for pattern in ('d-*.json', 'd-*.md', 'd-*.xml', 'd-*.log', 'model-export-*.json', 'model-export-*.log',
                    'publication-*.json', 'publication-*.log', 'publication-*.xml',
                    '*paginat*.json', '*paginat*.xml', '*paginat*.log'):
        files.update(BUILD.glob(pattern))
    for pattern in ('d-*.json', 'd-*.md', '*-D.json', 'd-*.png'):
        files.update((BUILD / 'arcmap-evidence').glob(pattern))
    for name in ('d-paged-exported-tabm.spsmodel.zip', 'd-exported-parameter-page.html',
                 'd-panel-saved.sps.json', 'd-roundtrip.sps.json'):
        path = BUILD / 'arcmap-evidence' / name
        if path.is_file():
            files.add(path)
    files.update((args.readback, args.roundtrip, ROOT / 'ASAMA_D_KABUL.md',
                  Path(__file__).resolve(), BUILD / 'verify_d_acceptance.py',
                  BUILD / 'd-paginated-native-evidence.json'))
    files.update(path for path in (BUILD / 'webbrowser-probe').glob('*')
                 if path.is_file() and path.suffix.lower() in {'.cs', '.html', '.json', '.jsonl', '.log'})
    for path in files:
        require(path.is_file(), 'Required evidence file is missing: ' + str(path))
        local(path)
    return sorted(files)


def snapshot(files):
    return {relative(path): sha(path) for path in files}


def create_archive(path, files, expected):
    with zipfile.ZipFile(path, 'x', zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for file in files:
            archive.write(file, relative(file))
        archive.writestr('archive-index.json', json.dumps({'algorithm': 'SHA-256', 'members': expected}, indent=2))
    with zipfile.ZipFile(path) as archive:
        require(archive.testzip() is None, 'Archive CRC failed: ' + str(path))
        for name, expected_sha in expected.items():
            h = hashlib.sha256()
            with archive.open(name) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(block)
            require(h.hexdigest() == expected_sha, 'Archive readback or concurrent file change: ' + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--ui-tested', required=True, type=Path)
    parser.add_argument('--ui-sha256', required=True)
    parser.add_argument('--readback', required=True, type=Path)
    parser.add_argument('--roundtrip', required=True, type=Path)
    parser.add_argument('--native-evidence', type=Path)
    parser.add_argument('--package', type=Path, default=ROOT / 'dist/SpatialPredictionStudio.esriAddIn')
    parser.add_argument('--runs', type=Path, default=BUILD / 'arcmap-evidence/d-runs')
    parser.add_argument('--backend-evidence', type=Path, default=BUILD / 'backend-041-final')
    args = parser.parse_args()
    for name in ('ui_tested', 'readback', 'roundtrip', 'package', 'runs', 'backend_evidence'):
        setattr(args, name, local(getattr(args, name)))
    require(not DEST.exists(), 'D destination already exists; no accepted archive is overwritten: ' + str(DEST))
    require((ROOT / 'ASAMA_D_KABUL.md').is_file(), 'Final ASAMA_D_KABUL.md must exist before archival.')
    manifest, changes = verify_packages(args.ui_tested, args.ui_sha256, args.package)
    backend = verify_backend(args.backend_evidence)
    native = native_directory(args.native_evidence)
    readback = verify_readback(args.readback, args.runs)
    verify_roundtrip(args.roundtrip, args.runs, {run['run_id'] for run in readback['runs']})
    sources, evidence = source_files(), evidence_files(args, native)
    source_index, evidence_index = snapshot(sources), snapshot(evidence)
    package_sha = sha(args.package)
    staging = BUILD / ('.stage-d-archive-' + uuid.uuid4().hex)
    staging.mkdir()
    try:
        final_copy = staging / 'SpatialPredictionStudio.esriAddIn'
        tested_copy = staging / 'SpatialPredictionStudio.final-ui-tested.esriAddIn'
        shutil.copy2(args.package, final_copy)
        shutil.copy2(args.ui_tested, tested_copy)
        require(sha(final_copy) == package_sha and sha(tested_copy) == args.ui_sha256.lower(), 'Copied package hash mismatch.')
        create_archive(staging / 'source-stage-d.zip', sources, source_index)
        create_archive(staging / 'acceptance-evidence-stage-d.zip', evidence, evidence_index)
        require(snapshot(sources) == source_index and snapshot(evidence) == evidence_index, 'Sources or evidence changed during archival.')
        require(sha(args.package) == package_sha and sha(args.ui_tested) == args.ui_sha256.lower(), 'Package changed during archival.')
        verify_packages(args.ui_tested, args.ui_sha256, args.package)
        verify_readback(args.readback, args.runs)
        verify_roundtrip(args.roundtrip, args.runs, {run['run_id'] for run in readback['runs']})
        record = {
            'schema_version': 1, 'stage': 'D', 'status': 'accepted',
            'verified_at_utc': datetime.now(timezone.utc).isoformat(), 'version': manifest['version'],
            'sha256': package_sha, 'ui_tested_sha256': args.ui_sha256.lower(), 'changes': changes,
            'backend_and_native_identical_to_ui_tested_package': True, 'package_test_passed': True,
            'readback': {'path': relative(args.readback), 'sha256': sha(args.readback)},
            'cancel_roundtrip': {'path': relative(args.roundtrip), 'sha256': sha(args.roundtrip)},
            'native_evidence': relative(native), 'backend_groups': backend,
            'source_archive_sha256': sha(staging / 'source-stage-d.zip'),
            'evidence_archive_sha256': sha(staging / 'acceptance-evidence-stage-d.zip'),
            'source_file_count': len(source_index), 'evidence_file_count': len(evidence_index),
            'external_assets': manifest['external_assets'],
            'source_archive_excludes': ['Python/R interpreters', 'compiled DLL/EXE files', 'TabPFN checkpoint'],
            'scope': 'Actual supplied ArcMap D acceptance plus final backend/native tests; supplemental history can include earlier failed attempts.',
        }
        record_path = staging / 'final-package-record.json'
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        require(read(record_path) == record, 'Final delivery record failed readback.')
        require(not DEST.exists(), 'D destination appeared during archival; nothing is overwritten.')
        staging.rename(DEST)
        print(json.dumps({'destination': str(DEST), **record}, ensure_ascii=False, indent=2))
    except BaseException as exc:
        if staging.is_dir():
            (staging / 'ARCHIVE_INCOMPLETE.json').write_text(json.dumps({'status': 'incomplete', 'error': str(exc)}, indent=2), encoding='utf-8')
        raise


if __name__ == '__main__':
    main()
