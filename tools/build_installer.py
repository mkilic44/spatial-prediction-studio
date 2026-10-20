"""Prepare or compile the separate distributable installer without installing it."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]

def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''): h.update(block)
    return h.hexdigest()

def bind_addin(args):
    payload = args.payload.resolve(); source = args.addin.resolve()
    if sha(source) != args.expect_sha256.lower(): raise ValueError('Final add-in fingerprint differs from approved candidate')
    manifest_path = payload / 'payload-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    with zipfile.ZipFile(source) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())): raise ValueError('Duplicate add-in archive members')
        record = json.loads(archive.read('Install/build-manifest.json'))
        if set(archive.namelist()) != set(record['members']) | {'Install/build-manifest.json'}: raise ValueError('Add-in member set mismatch')
        private_backend = {name for name in manifest['files'] if name.startswith('backend/')}
        packaged_backend = {name.removeprefix('Install/') for name in record['members'] if name.startswith('Install/backend/')}
        if private_backend != packaged_backend: raise ValueError('Backend member set differs between private payload and final add-in')
        for name, expected in record['members'].items():
            data = archive.read(name)
            if hashlib.sha256(data).hexdigest() != expected: raise ValueError('Add-in member changed: ' + name)
            if name.startswith('Install/backend/'):
                external = (payload / name.removeprefix('Install/')).resolve()
                if not external.is_relative_to(payload) or not external.is_file() or sha(external) != expected:
                    raise ValueError('Private backend differs from final add-in: ' + name)
    target = payload / 'package/SpatialPredictionStudio.esriAddIn'
    temporary = target.with_suffix('.verified-new')
    if temporary.exists(): raise FileExistsError(temporary)
    shutil.copy2(source, temporary)
    if sha(temporary) != args.expect_sha256.lower(): raise IOError('Final add-in copy readback failed')
    previous = manifest['source_addin_sha256']
    os.replace(temporary, target)
    manifest['files']['package/SpatialPredictionStudio.esriAddIn'].update({'sha256': sha(target), 'bytes': target.stat().st_size})
    manifest['source_addin_sha256'] = sha(target)
    manifest['final_binding'] = {'previous_addin_sha256': previous, 'final_addin_sha256': sha(target), 'backend_members_identical': True}
    manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':'bound_final_addin','source_addin_sha256':sha(target),'manifest_sha256':sha(manifest_path)},indent=2))

def compile_installer(args):
    payload = args.payload.resolve(); manifest_path = payload / 'payload-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not {'core', 'reference_r'} <= set(manifest['components']): raise ValueError('Offline release setup requires the matching private Python and R environments')
    if not manifest.get('final_binding', {}).get('backend_members_identical'):
        raise ValueError('Bind the approved final add-in before compiling a release installer')
    observed = {path.relative_to(payload).as_posix() for path in payload.rglob('*') if path.is_file()} - {'payload-manifest.json'}
    if observed != set(manifest['files']): raise ValueError('Unlisted or missing payload files: ' + repr(sorted(observed ^ set(manifest['files']))[:10]))
    for name, record in manifest['files'].items():
        path = (payload / name).resolve()
        if not path.is_relative_to(payload) or not path.is_file() or sha(path) != record['sha256']: raise ValueError('Payload differs from manifest: ' + name)
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    build_id = manifest['version'] + '-' + sha(manifest_path)[:12]
    define = output / 'build-defines.iss'; file_list = output / 'payload-files.iss'
    define.write_text('#define BuildId "' + build_id + '"\n#define ManifestHash "' + sha(manifest_path) + '"\n' + ('#define HasMcp\n' if 'mcp' in manifest['components'] else ''), encoding='utf-8')
    top_levels = {}
    for name, record in manifest['files'].items():
        first = name.split('/')[0]
        if first in top_levels and top_levels[first] != record['component']: raise ValueError('A top-level directory cannot mix installer components')
        top_levels[first] = record['component']
    entries = []
    for top, component in sorted(top_levels.items()):
        source = payload / top
        if source.is_dir(): entries.append(f'Source: "{source}\\*"; DestDir: "{{app}}\\versions\\{build_id}\\{top}"; Components: {component}; Flags: ignoreversion recursesubdirs createallsubdirs')
        else: entries.append(f'Source: "{source}"; DestDir: "{{app}}\\versions\\{build_id}"; Components: {component}; Flags: ignoreversion')
    entries.append(f'Source: "{manifest_path}"; DestDir: "{{app}}\\versions\\{build_id}"; Flags: ignoreversion')
    file_list.write_text('\n'.join(entries) + '\n', encoding='utf-8')
    command = [str(args.iscc), '/Qp', '/DPayload=' + str(payload), '/DOutputFolder=' + str(output), '/DIncludeFile=' + str(define), '/DPayloadFiles=' + str(file_list), str(ROOT / 'installer/setup.iss')]
    subprocess.run(command, check=True)
    exe = output / 'SpatialPredictionStudio-0.5.0-Setup.exe'
    record = {'status': 'compiled_not_installed', 'installer': str(exe), 'sha256': sha(exe), 'bytes': exe.stat().st_size, 'manifest_sha256': sha(manifest_path), 'build_id': build_id, 'components': manifest['components'], 'compiler': str(args.iscc)}
    (output / 'installer-build-record.json').write_text(json.dumps(record, indent=2), encoding='utf-8'); print(json.dumps(record, indent=2))

def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'prepare':
        subprocess.run([sys.executable, str(ROOT / 'installer/prepare_payload.py'), *sys.argv[2:]], check=True); return
    if len(sys.argv) > 1 and sys.argv[1] == 'bind-addin':
        parser=argparse.ArgumentParser(description='Bind the final native candidate only when its backend matches the private payload')
        parser.add_argument('--payload',type=Path,required=True);parser.add_argument('--addin',type=Path,required=True);parser.add_argument('--expect-sha256',required=True)
        bind_addin(parser.parse_args(sys.argv[2:]));return
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('command', choices=['compile'])
    parser.add_argument('--payload', type=Path, required=True); parser.add_argument('--output', type=Path, default=ROOT / 'installer/dist')
    parser.add_argument('--iscc', type=Path, default=Path.home() / 'AppData/Local/Programs/Inno Setup 6/ISCC.exe')
    compile_installer(parser.parse_args())

if __name__ == '__main__': main()
