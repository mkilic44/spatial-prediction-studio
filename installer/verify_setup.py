"""Exercise the final Setup.exe only inside a new project-owned staging root."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile
import winreg

import psutil

ROOT = Path(__file__).resolve().parents[1]
GUID = '{DC2D1F95-349B-4F9A-A681-202610170101}'
CLIENTS = 'codex,chatgpt_desktop,antigravity,claude_desktop,claude_code,opencode,pycharm'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path): return json.loads(path.read_text(encoding='utf-8-sig'))


def registered_paths():
    result = {}
    for label, hive, key in (
        ('user', winreg.HKEY_CURRENT_USER, 'Environment'),
        ('machine', winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment')):
        try:
            with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as handle:
                value, kind = winreg.QueryValueEx(handle, 'Path')
                result[label] = {'value': value, 'type': kind}
        except FileNotFoundError:
            result[label] = None
    return result


def run(command, log, timeout=900):
    owned = {}; started = time.monotonic()
    with log.open('wb') as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            primary = psutil.Process(process.pid); owned[primary.pid] = primary.create_time()
            while process.poll() is None:
                try:
                    for child in primary.children(recursive=True): owned[child.pid] = child.create_time()
                except psutil.NoSuchProcess: pass
                if time.monotonic() - started > timeout: raise TimeoutError('Owned setup process exceeded its test budget')
                time.sleep(0.5)
        except BaseException:
            for pid, created in reversed(list(owned.items())):
                try:
                    child = psutil.Process(pid)
                    if child.create_time() == created: child.kill()
                except psutil.NoSuchProcess: pass
            process.wait(timeout=20)
            raise
    return {'exit_code': process.returncode, 'elapsed_seconds': time.monotonic() - started, 'log': str(log)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-record', type=Path, required=True)
    parser.add_argument('--staging', type=Path, required=True)
    parser.add_argument('--payload', type=Path, default=ROOT / 'installer/payload-050')
    args = parser.parse_args(); stage = args.staging.resolve(); build = read(args.build_record)
    payload = args.payload.resolve()
    if not payload.is_relative_to(ROOT / 'installer'):
        raise ValueError('Expected an installer-owned source payload')
    # sps-ç and sps-ç2 are the staging folders recorded during the Turkish 0.5.0 acceptance; keep as recorded.
    allowed_stage = stage.is_relative_to(ROOT / 'installer') or stage in (ROOT.parent / 'sps-ç', ROOT.parent / 'sps-ç2', ROOT.parent / 'sps-cs')
    if not allowed_stage or stage.exists():
        raise ValueError('A fresh installer-owned staging directory or the exact workspace sps-ç directory is required')
    exe = Path(build['installer']).resolve()
    if sha(exe) != build['sha256']: raise ValueError('Final setup checksum mismatch')
    stage.mkdir(parents=True)
    record = {'status': 'running', 'installer': str(exe), 'installer_sha256': sha(exe),
              'build_id': build['build_id'], 'real_user_registration_changed': False,
              'real_arcmap_ui_test': False, 'client_connection_tested': False, 'source_payload': str(payload)}
    record_path = stage / 'setup-acceptance.json'
    def save(): record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    try:
        environment_paths = registered_paths()
        common = ['/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-']
        preflight = stage / 'preflight.json'
        record['preflight'] = run([str(exe), *common, '/VERIFYONLY=1', '/VERIFYREPORT=' + str(preflight),
                                  '/LOG=' + str(stage / 'preflight.log')], stage / 'preflight.stdout.log', 60)
        assert record['preflight']['exit_code'] == 1 and read(preflight)['status'] == 'verified_preflight_only'
        target = stage / 'Documents/ArcGIS/AddIns/Desktop10.8' / GUID / 'SpatialPredictionStudio.esriAddIn'
        target.parent.mkdir(parents=True)
        fixture_manifest = payload / 'payload-manifest.json'
        assert sha(fixture_manifest) == build['manifest_sha256']
        fixture_package = payload / 'package/SpatialPredictionStudio.esriAddIn'
        assert sha(fixture_package) == read(fixture_manifest)['source_addin_sha256']
        original = fixture_package.read_bytes(); target.write_bytes(original)
        record['previous_addin_fixture'] = {'path': str(fixture_package), 'sha256': sha(fixture_package), 'real_package_in_isolated_documents': True}
        # The Turkish file names, values and comments below are Unicode test data; keep them as is.
        sentinel = stage / 'UserStudies/korunan-çalışma.txt'; sentinel.parent.mkdir(); sentinel.write_text('Kullanıcı verisi ıİğĞşŞ', encoding='utf-8')
        sentinel_hash = sha(sentinel)
        opencode = stage / 'ClientHome/.config/opencode/opencode.jsonc'; opencode.parent.mkdir(parents=True)
        opencode.write_bytes(b'\xef\xbb\xbf' + '{\n  // Türkçe açıklama korunmalı\n  "theme": "system",\n  "mcp": {"unrelated": {"type":"remote","url":"https://example.invalid"}}\n}\n'.encode('utf-8'))
        claude_profiles = [stage / 'ClientHome/AppData/Roaming/Claude/claude_desktop_config.json',
            stage / 'ClientHome/AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude/claude_desktop_config.json']
        claude_originals = {}
        for number, path in enumerate(claude_profiles):
            path.parent.mkdir(parents=True, exist_ok=True)
            document = {'theme': 'Türkçe profil ' + str(number),
                        'mcpServers': {'unrelated': {'command': 'unrelated', 'args': ['ıİğĞşŞ']}}}
            data = b'\xef\xbb\xbf' + json.dumps(document, ensure_ascii=False, indent=2).encode('utf-8')
            path.write_bytes(data)
            claude_originals[str(path)] = {'sha256': sha(path), 'value': document}
        app = stage / 'app'
        record['install'] = run([str(exe), *common, '/STAGINGROOT=' + str(stage), '/DIR=' + str(app),
            '/COMPONENTS=core,reference_r,tabpfn,mcp', '/TASKS=' + CLIENTS, '/ACCEPTTABPFNLICENSE=yes',
            '/LOG=' + str(stage / 'install.log')], stage / 'install.stdout.log')
        assert record['install']['exit_code'] == 0, record['install']
        installed = app / 'versions' / build['build_id']
        deployment = read(installed / 'deployment-record.json')
        assert deployment['status'] == 'installed' and deployment['staging']
        assert deployment['previous_addin_sha256'] == hashlib.sha256(original).hexdigest()
        assert sha(target) == deployment['addin_sha256']
        with zipfile.ZipFile(fixture_package) as before, zipfile.ZipFile(target) as after:
            assert set(before.namelist()) == set(after.namelist())
            changed = sorted(name for name in before.namelist() if before.read(name) != after.read(name))
            assert changed == ['Install/build-manifest.json', 'Install/runtime.json'], changed
            after_manifest = json.loads(after.read('Install/build-manifest.json'))
            assert after_manifest['sources'] == json.loads(before.read('Install/build-manifest.json'))['sources']
            for name, expected in after_manifest['members'].items():
                assert hashlib.sha256(after.read(name)).hexdigest() == expected, name
            relocated = json.loads(after.read('Install/runtime.json'))
            assert all(Path(value).resolve().is_relative_to(installed.resolve()) for value in relocated.values())
            record['addin_member_readback'] = {'status': 'passed', 'changed_members': changed,
                'native_and_backend_members_identical': True, 'source_manifest_identical': True,
                'all_runtime_paths_inside_installed_version': True}
        clients = deployment['mcp_registration']['clients']
        assert len(clients) == 7
        assert all(x['status'] == ('manual_import_required' if x['client'] == 'pycharm' else 'registered_restart_required') for x in clients)
        claude = next(x for x in clients if x['client'] == 'claude_desktop')
        assert {item['path'] for item in claude['profiles']} == set(claude_originals)
        for item in claude['profiles']:
            assert item['status'] == 'registered_restart_required'
            path = Path(item['path']); original_profile = claude_originals[str(path)]
            assert sha(Path(item['backup'])) == original_profile['sha256']
            assert sha(path) == item['sha256'] and path.read_bytes().startswith(b'\xef\xbb\xbf')
            current = read(path)
            assert current['theme'] == original_profile['value']['theme']
            assert current['mcpServers']['unrelated'] == original_profile['value']['mcpServers']['unrelated']
            assert current['mcpServers']['spatial_prediction_studio']['command'] == str(installed / 'runtime-mcp/Scripts/python.exe')
        record['claude_profile_registration'] = {'status': 'passed', 'profiles': claude['profiles'],
            'originals': claude_originals, 'both_existing_profiles_registered': True,
            'unrelated_values_bom_and_exact_backups_preserved': True}
        record['deployment'] = deployment; save()
        private_python = installed / 'runtime-d/Scripts/python.exe'
        record['installed_readback'] = run([str(private_python), '-X', 'utf8', str(installed / 'installer/deploy.py'),
            '--root', str(installed), '--staging', str(stage), '--verify-installed'], stage / 'installed-readback.json')
        assert record['installed_readback']['exit_code'] == 0
        record['relocated_science'] = run([str(private_python), '-B', '-X', 'utf8',
            str(ROOT / 'installer/verify_relocated_science.py'), '--root', str(installed),
            '--fixtures', str(ROOT / 'tests/fixtures/cast-1.0.4'),
            '--output', str(stage / 'relocated-science')], stage / 'relocated-science.log', 360)
        assert record['relocated_science']['exit_code'] == 0
        record['relocated_science_readback'] = read(stage / 'relocated-science/readback.json')
        assert record['relocated_science_readback']['status'] == 'passed'
        save()
        record['worker'] = run([sys.executable, '-X', 'utf8', str(ROOT / 'installer/verify_portable_worker.py'),
            '--root', str(installed), '--output', str(stage / 'portable-workflow')], stage / 'portable-workflow.log', 240)
        assert record['worker']['exit_code'] == 0
        record['worker_readback'] = read(stage / 'portable-workflow/readback.json'); save()
        uninstaller = installed / 'unins000.exe'
        record['uninstall'] = run([str(uninstaller), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
            '/STAGINGROOT=' + str(stage), '/LOG=' + str(stage / 'uninstall.log')], stage / 'uninstall.stdout.log', 300)
        assert record['uninstall']['exit_code'] == 0
        assert target.read_bytes() == original and sha(sentinel) == sentinel_hash
        assert not private_python.exists()
        registered_files = read(fixture_manifest)['files']
        remaining_registered = [name for name in registered_files if (installed/name).exists()]
        assert not remaining_registered, remaining_registered[:10]
        record['uninstalled_registered_files'] = len(registered_files)
        record['remaining_generated_files'] = len([path for path in installed.rglob('*') if path.is_file()])
        for item in clients:
            if item['client'] == 'pycharm': continue
            for profile in item.get('profiles', [item]):
                path = Path(profile['path'])
                assert path.is_relative_to(stage)
                assert 'spatial_prediction_studio' not in path.read_text(encoding='utf-8-sig')
        for path in claude_profiles:
            assert read(path) == claude_originals[str(path)]['value']
            assert path.read_bytes().startswith(b'\xef\xbb\xbf')
        text = opencode.read_text(encoding='utf-8-sig')
        assert 'Türkçe açıklama korunmalı' in text and '"theme": "system"' in text and '"unrelated"' in text
        assert opencode.read_bytes().startswith(b'\xef\xbb\xbf')
        assert registered_paths() == environment_paths
        record.update(status='passed', previous_addin_restored_exactly=True, user_study_preserved=True,
                      all_registered_application_files_removed=True, selected_owned_client_entries_removed=True,
                      unrelated_jsonc_values_comments_bom_preserved=True, jsonc_whitespace_may_change=True,
                      claude_both_profiles_owned_entries_removed=True,
                      claude_unrelated_values_and_bom_preserved=True,
                      user_and_machine_path_registry_unchanged=True,
                      pycharm_manual_import_not_performed=True)
    except BaseException as error:
        record.update(status='failed', error_type=type(error).__name__, error=str(error)); save(); raise
    save(); print(json.dumps({'status': record['status'], 'evidence': str(record_path)}, ensure_ascii=False))


if __name__ == '__main__': main()
