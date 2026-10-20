"""Verified per-user activation for an already extracted, private payload.

--verify-only reads files only. --staging redirects AddIns into an explicitly
supplied test directory and never changes the real ArcMap installation.
"""
from __future__ import annotations
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import struct
import subprocess
import sys
import uuid
import zipfile

GUID = '{DC2D1F95-349B-4F9A-A681-202610170101}'
PRODUCT = 'SpatialPredictionStudio'
CLIENTS = ('codex', 'chatgpt_desktop', 'antigravity', 'claude_desktop', 'claude_code', 'opencode', 'pycharm')

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''): h.update(block)
    return h.hexdigest()

def read(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def inside(root, relative):
    item = PurePosixPath(relative)
    if item.is_absolute() or '..' in item.parts or ':' in relative or '\\' in relative: raise ValueError('Unsafe manifest path: ' + relative)
    path = (root / Path(*item.parts)).resolve()
    if not path.is_relative_to(root.resolve()): raise ValueError('Path escapes owned installation')
    return path

def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        if read(temporary) != value: raise IOError('JSON readback mismatch')
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()

def documents():
    buffer = ctypes.create_unicode_buffer(32768)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer): raise OSError('Documents folder cannot be resolved')
    return Path(buffer.value)

def file_version(path):
    library = ctypes.windll.version
    size = library.GetFileVersionInfoSizeW(str(path), None)
    if not size: raise OSError('Executable version resource missing')
    data = ctypes.create_string_buffer(size)
    if not library.GetFileVersionInfoW(str(path), 0, size, data): raise OSError('Executable version cannot be read')
    value = ctypes.c_void_p(); length = ctypes.c_uint()
    if not library.VerQueryValueW(data, '\\', ctypes.byref(value), ctypes.byref(length)) or length.value < 52: raise OSError('Invalid executable version')
    fields = ctypes.cast(value, ctypes.POINTER(ctypes.c_uint32))
    if fields[0] != 0xFEEF04BD: raise OSError('Invalid fixed version signature')
    return (fields[2] >> 16, fields[2] & 65535, fields[3] >> 16, fields[3] & 65535)

def preflight(staging=False):
    if sys.platform != 'win32' or struct.calcsize('P') != 8: raise RuntimeError('64-bit Windows and private 64-bit Python are required')
    if staging: return {'mode': 'isolated_staging', 'real_arcmap_registration': False}
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\ESRI\Desktop10.8', 0, winreg.KEY_READ | winreg.KEY_WOW64_32KEY) as key:
        directory = Path(winreg.QueryValueEx(key, 'InstallDir')[0])
    if not (directory / 'bin/ArcMap.exe').is_file(): raise RuntimeError('ArcMap Desktop 10.8 family is required')
    version = file_version(directory / 'bin/ArcMap.exe')
    if version[:2] != (10, 8): raise RuntimeError('Installed ArcMap executable is outside the supported 10.8 family')
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full', 0, winreg.KEY_READ | winreg.KEY_WOW64_32KEY) as key:
        if int(winreg.QueryValueEx(key, 'Release')[0]) < 528040: raise RuntimeError('.NET Framework 4.8 is required')
    processes = subprocess.run(['tasklist.exe', '/FI', 'IMAGENAME eq ArcMap.exe', '/FO', 'CSV', '/NH'], capture_output=True, text=True, check=True, creationflags=0x08000000).stdout
    if 'arcmap.exe' in processes.lower(): raise RuntimeError('Close ArcMap before installing or uninstalling; the installer never closes it')
    return {'mode': 'per_user', 'arcmap_path': str(directory / 'bin/ArcMap.exe'), 'actual_version': '.'.join(map(str, version)), 'supported_family': '10.8', 'tested_version': '10.8.1.14362'}

def verify_payload(root, components, expected_manifest=None):
    manifest_path = root / 'payload-manifest.json'
    if expected_manifest and sha(manifest_path) != expected_manifest.lower(): raise ValueError('Payload manifest fingerprint mismatch')
    manifest = read(manifest_path)
    if manifest.get('product') != PRODUCT or manifest.get('schema_version') != 1: raise ValueError('Unknown installation manifest')
    unknown = set(components) - set(manifest['components'])
    if unknown or 'core' not in components: raise ValueError('Invalid component selection: ' + repr(unknown))
    verified = 0
    for name, record in manifest['files'].items():
        if record['component'] not in components: continue
        path = inside(root, name)
        if not path.is_file() or sha(path) != record['sha256']: raise ValueError('Payload file changed or missing: ' + name)
        verified += 1
    return manifest, verified

def child_environment(root):
    env = os.environ.copy()
    env.pop('PYTHONHOME', None); env.pop('PYTHONPATH', None)
    env['PATH'] = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32')
    env.update({'TABPFN_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1', 'HF_HUB_OFFLINE': '1', 'PROJ_NETWORK': 'OFF', 'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1', 'SPS_MODELS_DIR': str(root / 'models')})
    return env

def probe_runtime(root, components):
    python = root / 'runtime-d/Scripts/python.exe'
    args = [str(python), '-X', 'utf8', '-I', str(root / 'installer/verify_runtime.py'), '--root', str(root), '--components', ','.join(components)]
    done = subprocess.run(args, cwd=root, env=child_environment(root), capture_output=True, text=True, encoding='utf-8', timeout=180, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if done.returncode: raise RuntimeError('Private runtime verification failed: ' + done.stdout[-3000:] + done.stderr[-3000:])
    return json.loads(done.stdout)

def configure_clients(root, clients, *, staging_root=None, remove=False):
    if set(clients) - set(CLIENTS): raise ValueError('Unknown MCP client selection')
    output = root / ('client-removal' if remove else 'client-configurations')
    command = [str(root / 'runtime-mcp/Scripts/python.exe'), '-X', 'utf8', str(root / 'mcp-server/register_clients.py'), '--root', str(root), '--output-dir', str(output)]
    if clients:
        command.append('--remove' if remove else '--apply')
        if remove:
            command.extend(['--registration-record', str(root / 'client-configurations/registration-result.json')])
        for client in clients: command.extend(['--client', client])
    env = child_environment(root)
    if staging_root is not None:
        home = staging_root.resolve() / 'ClientHome'
        env.update({'USERPROFILE': str(home), 'HOME': str(home), 'APPDATA': str(home / 'AppData/Roaming'),
                    'LOCALAPPDATA': str(home / 'AppData/Local'), 'CODEX_HOME': str(home / '.codex')})
        for name in ('OPENCODE_CONFIG', 'OPENCODE_CONFIG_DIR', 'XDG_CONFIG_HOME'): env.pop(name, None)
    done = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, encoding='utf-8', timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try: result = json.loads(done.stdout)
    except (ValueError, TypeError): result = {'status': 'manual_action_required', 'error': (done.stderr or '')[-2000:] or (done.stdout or '')[-2000:]}
    result['helper_exit_code'] = done.returncode
    result['selected_clients'] = list(clients)
    result['client_connection_tested'] = False
    if remove and done.returncode:
        raise RuntimeError('Owned client removal did not complete; application files are retained: ' + json.dumps(result))
    return result

def relocate_reference(root, components):
    if 'reference_r' not in components: return
    scripts = root / 'reference-r/Scripts'
    unpack = next((scripts / name for name in ('conda-unpack-script.py', 'conda-unpack') if (scripts / name).is_file()), None)
    if unpack is None: raise FileNotFoundError('Packed reference R relocation script is missing')
    done = subprocess.run([str(root / 'runtime-d/Scripts/python.exe'), str(unpack)], cwd=root / 'reference-r', env=child_environment(root), capture_output=True, text=True, timeout=180, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if done.returncode: raise RuntimeError('Reference R relocation failed: ' + done.stdout[-1500:] + done.stderr[-1500:])

def relocate_examples(root, source_root):
    if not source_root: return []
    source = PureWindowsPath(source_root)
    def replace(value):
        if isinstance(value, dict): return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list): return [replace(item) for item in value]
        if isinstance(value, str):
            try: relative = PureWindowsPath(value).relative_to(source)
            except ValueError: return value
            return str(inside(root, relative.as_posix()))
        return value
    changed = []
    for path in sorted((root / 'examples-d').glob('*.json')):
        old = read(path); new = replace(old)
        if new != old:
            before = sha(path); atomic_json(path, new)
            changed.append({'path': path.relative_to(root).as_posix(), 'original_sha256': before, 'installed_sha256': sha(path)})
    return changed

def verify_installed(root):
    record = read(root / 'deployment-record.json')
    path = root / 'installed-files.json'
    if record.get('product') != PRODUCT or record.get('installed_file_manifest_sha256') != sha(path): raise ValueError('Installed-file record mismatch')
    files = read(path)['files']
    for name, expected in files.items():
        if sha(inside(root, name)) != expected: raise ValueError('Installed file changed: ' + name)
    return {'status': 'installed_files_verified', 'files': len(files), 'writes': False}

def relocated_addin(root, source, destination):
    runtime = {'python_executable': str(root / 'runtime-d/Scripts/python.exe'), 'worker_path': str(root / 'backend/worker.py'), 'examples_dir': str(root / 'examples'), 'models_dir': str(root / 'models')}
    with zipfile.ZipFile(source) as archive:
        manifest = json.loads(archive.read('Install/build-manifest.json'))
        names = archive.namelist()
        if len(names) != len(set(names)): raise ValueError('Duplicate add-in archive members')
        for name in names: inside(root, name)
        if set(names) != set(manifest['members']) | {'Install/build-manifest.json'}:
            raise ValueError('Source add-in manifest member set mismatch')
        for name, expected in manifest['members'].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected: raise ValueError('Source add-in fingerprint mismatch: ' + name)
        members = {name: archive.read(name) for name in names}
    members['Install/runtime.json'] = json.dumps(runtime, ensure_ascii=False, indent=2).encode('utf-8')
    manifest['members']['Install/runtime.json'] = hashlib.sha256(members['Install/runtime.json']).hexdigest()
    manifest['external_assets'] = {str(p): sha(p) for p in sorted((root / 'models/tabpfn-v2').glob('*')) if p.is_file()}
    manifest['installation'] = {'relocated': True, 'source_addin_sha256': sha(source), 'modified_members': ['Install/runtime.json', 'Install/build-manifest.json']}
    members['Install/build-manifest.json'] = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8')
    with zipfile.ZipFile(destination, 'x', zipfile.ZIP_DEFLATED) as archive:
        for name, value in sorted(members.items()): archive.writestr(name, value)
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip(): raise IOError('Relocated add-in CRC mismatch')
        if json.loads(archive.read('Install/runtime.json')) != runtime: raise IOError('Relocated runtime readback mismatch')
    return runtime

def activate(root, components, document_root, *, staging=False, expected_manifest=None, run_probe=True, fail_after_activation=False, clients=()):
    if clients and 'mcp' not in components: raise ValueError('MCP clients require the MCP component')
    mode = preflight(staging)
    manifest, checked = verify_payload(root, components, expected_manifest)
    if (root / 'deployment-record.json').exists(): raise FileExistsError('This version directory has already been activated')
    relocate_reference(root, components)
    relocated_examples = relocate_examples(root, manifest.get('build_source_root'))
    runtime_probe = probe_runtime(root, components) if run_probe else {'status': 'test_fixture_only'}
    if not run_probe and not staging: raise ValueError('A real installation cannot skip runtime verification')
    installed_files = {name: sha(inside(root, name)) for name, item in manifest['files'].items() if item['component'] in components}
    atomic_json(root / 'installed-files.json', {'schema_version': 1, 'product': PRODUCT, 'files': installed_files})
    target = document_root / 'ArcGIS/AddIns/Desktop10.8' / GUID / 'SpatialPredictionStudio.esriAddIn'
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.new')
    backup = root / 'activation-backup.esriAddIn'
    previous_sha = sha(target) if target.exists() else None
    if previous_sha:
        shutil.copy2(target, backup)
        if sha(backup) != previous_sha: raise IOError('Previous add-in backup did not verify')
    activated = False
    try:
        runtime = relocated_addin(root, root / 'package/SpatialPredictionStudio.esriAddIn', temporary)
        next_sha = sha(temporary)
        if (sha(target) if target.exists() else None) != previous_sha: raise IOError('Add-in changed during installation')
        os.replace(temporary, target); activated = True
        if sha(target) != next_sha: raise IOError('Installed add-in readback failed')
        if fail_after_activation:
            if not staging: raise ValueError('Failure injection is restricted to staging')
            raise RuntimeError('Injected transaction failure')
        record = {'schema_version': 1, 'status': 'installed', 'product': PRODUCT, 'installation_root': str(root), 'components': components,
                  'staging': staging, 'documents': str(document_root), 'addin_path': str(target), 'addin_sha256': next_sha,
                  'previous_addin_sha256': previous_sha, 'source_addin_sha256': manifest['source_addin_sha256'], 'payload_manifest_sha256': sha(root / 'payload-manifest.json'),
                  'verified_files': checked, 'runtime': runtime, 'runtime_probe': runtime_probe, 'preflight': mode,
                  'relocated_examples': relocated_examples, 'installed_file_manifest_sha256': sha(root / 'installed-files.json')}
        atomic_json(root / 'deployment-record.json', record)
    except BaseException:
        if activated:
            if target.exists() and sha(target) != next_sha: raise IOError('Activated add-in was changed concurrently; rollback does not overwrite that file')
            if previous_sha:
                restore = target.with_name(target.name + '.' + uuid.uuid4().hex + '.rollback')
                shutil.copy2(backup, restore); os.replace(restore, target)
            elif target.exists(): target.unlink()
        raise
    finally:
        if temporary.exists(): temporary.unlink()
    # Client configuration is a separate, explicitly selected operation. A merge
    # conflict never rolls back an already verified application or another client.
    if 'mcp' in components:
        try: record['mcp_registration'] = configure_clients(root, clients, staging_root=document_root.parent if staging else None)
        except (OSError, subprocess.SubprocessError) as error:
            record['mcp_registration'] = {'status': 'manual_action_required', 'error': str(error), 'selected_clients': list(clients)}
        record['selected_clients'] = list(clients)
        atomic_json(root / 'deployment-record.json', record)
    return record

def uninstall(root, document_root, staging=False):
    preflight(staging)
    record = read(root / 'deployment-record.json')
    target = document_root / 'ArcGIS/AddIns/Desktop10.8' / GUID / 'SpatialPredictionStudio.esriAddIn'
    if record.get('product') != PRODUCT or Path(record.get('installation_root', '')).resolve() != root.resolve() or Path(record.get('addin_path', '')).resolve() != target.resolve(): raise ValueError('Uninstall ownership does not match this installation')
    client_result = None
    if 'mcp' in record.get('components', []) and record.get('selected_clients'):
        client_result = configure_clients(root, record['selected_clients'], staging_root=document_root.parent if staging else None, remove=True)
    if target.exists() and sha(target) != record['addin_sha256']: return {'status': 'preserved_modified_addin', 'path': str(target), 'client_removal': client_result}
    restoration = 'not_required'
    if target.exists():
        previous = record.get('previous_addin_sha256'); backup = root / 'activation-backup.esriAddIn'
        if previous:
            if not backup.is_file() or sha(backup) != previous: raise ValueError('Previous add-in backup changed; current add-in is retained')
            previous_runtime_available = True
            try:
                with zipfile.ZipFile(backup) as archive:
                    old_manifest = json.loads(archive.read('Install/build-manifest.json'))
                    if old_manifest.get('installation', {}).get('relocated'):
                        old_runtime = json.loads(archive.read('Install/runtime.json'))
                        previous_runtime_available = all(Path(old_runtime[key]).is_file() for key in ('python_executable', 'worker_path'))
            except (zipfile.BadZipFile, KeyError, ValueError): pass  # Preserve an unrecognized pre-existing package exactly.
            if previous_runtime_available:
                temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.restore'); shutil.copy2(backup, temporary); os.replace(temporary, target)
                restoration = 'restored'
            else:
                target.unlink(); restoration = 'skipped_previous_runtime_missing'
        else: target.unlink()
    return {'status': 'unregistered_owned_addin', 'previous_addin_restoration': restoration, 'user_projects_and_results_preserved': True, 'client_removal': client_result}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True); parser.add_argument('--components', default='core,reference_r')
    parser.add_argument('--manifest-sha256'); parser.add_argument('--verify-only', action='store_true'); parser.add_argument('--preflight-only', action='store_true'); parser.add_argument('--staging', type=Path)
    parser.add_argument('--verify-installed', action='store_true')
    parser.add_argument('--uninstall', action='store_true'); parser.add_argument('--accept-tabpfn-license', action='store_true')
    parser.add_argument('--client', action='append', choices=CLIENTS, default=[])
    args = parser.parse_args(); root = args.root.resolve(); components = [x for x in args.components.split(',') if x]
    if args.staging:
        staging = args.staging.resolve()
        if not root.is_relative_to(staging): raise ValueError('Isolated installation must stay inside the explicitly supplied staging root')
        document_root = staging / 'Documents'
    else: document_root = documents()
    if args.preflight_only: result = preflight(bool(args.staging))
    elif args.verify_installed: result = verify_installed(root)
    elif args.verify_only:
        manifest, count = verify_payload(root, components, args.manifest_sha256); result = {'status': 'verified_only', 'files': count, 'writes': False, 'components': components}
    elif args.uninstall: result = uninstall(root, document_root, bool(args.staging))
    else:
        if 'tabpfn' in components and not args.accept_tabpfn_license: raise ValueError('TabPFN component requires explicit license acceptance')
        try: result = activate(root, components, document_root, staging=bool(args.staging), expected_manifest=args.manifest_sha256, clients=args.client)
        except Exception as error:
            atomic_json(root / 'deployment-error.json', {'status': 'failed', 'error_type': type(error).__name__, 'message': str(error), 'components': components})
            raise
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == '__main__': main()
