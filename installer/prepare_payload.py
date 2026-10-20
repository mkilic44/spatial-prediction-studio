"""Prepare an application-owned payload; never change a development runtime."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EMBED_SHA = '38b265fc0612027a126ae54d2485101f041b61893e41ef4f421dee6ac618a99e'
PYTHON_SOURCE_SHA = '56bfef1fdfc1221ce6720e43a661e3eb41785dd914ce99698d8c7896af4bdaa1'

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): h.update(block)
    return h.hexdigest()

def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

def copy_tree(source, target):
    shutil.copytree(source, target, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache'))

def packed_reference(build_python):
    source = ROOT / 'reference-r'
    files = {p.relative_to(source).as_posix(): sha(p) for p in sorted(source.rglob('*')) if p.is_file()}
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    archive = ROOT / ('installer/cache/reference-r-' + fingerprint[:16] + '.zip')
    record_path = archive.with_suffix('.json')
    if archive.exists():
        record = json.loads(record_path.read_text(encoding='utf-8'))
        if record['source_fingerprint'] != fingerprint or record['archive_sha256'] != sha(archive):
            raise ValueError('Cached R environment changed')
    else:
        subprocess.run([str(build_python), '-c', 'import conda_pack,sys;conda_pack.pack(prefix=sys.argv[1],output=sys.argv[2],format="zip")', str(source), str(archive)], check=True)
        write(record_path, {'source_fingerprint': fingerprint, 'source_files': files, 'archive_sha256': sha(archive)})
    return archive

def embedded_runtime(target, environment, embed, source_archive):
    if sha(embed) != EMBED_SHA: raise ValueError('Python embedded archive fingerprint mismatch')
    if sha(source_archive) != PYTHON_SOURCE_SHA: raise ValueError('Python standard-library source fingerprint mismatch')
    scripts = target / 'Scripts'; scripts.mkdir(parents=True)
    with zipfile.ZipFile(embed) as archive: archive.extractall(scripts)
    library = target / 'Lib'; library.mkdir()
    # Matching standard-library sources support inspect.getsource used by PyTorch.
    with tarfile.open(source_archive, 'r:*') as archive:
        for member in archive.getmembers():
            if not member.name.startswith('Python-3.12.3/Lib/') or not member.isfile(): continue
            relative = Path(member.name.removeprefix('Python-3.12.3/Lib/'))
            if '..' in relative.parts or relative.is_absolute(): raise ValueError('Unsafe Python source archive')
            if relative.parts[0] in ('test', 'idlelib', 'tkinter', 'ensurepip'): continue
            dest = library / relative; dest.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as stream, dest.open('wb') as out: shutil.copyfileobj(stream, out)
    copy_tree(environment / 'Lib/site-packages', library / 'site-packages')
    # The pinned scikit-learn wheel already redistributes this unchanged MSVC
    # library. Torch must find it even when imported before scikit-learn.
    cpp_runtime = library / 'site-packages/sklearn/.libs/msvcp140.dll'
    if cpp_runtime.is_file():
        shutil.copy2(cpp_runtime, scripts / cpp_runtime.name)
        if sha(scripts / cpp_runtime.name) != sha(cpp_runtime): raise IOError('App-local C++ runtime copy differs')
    (scripts / 'python312._pth').write_text('../Lib\npython312.zip\n.\n../Lib/site-packages\n../../backend\n../../mcp-server\nimport site\n', encoding='utf-8')
    # Embedded Python's prefix is Scripts. Explicitly process only the private
    # package directory so pywin32.pth can load its own DLLs and win32 modules.
    (scripts / 'sitecustomize.py').write_text('import site, sys\nfrom pathlib import Path\nsite.addsitedir(str(Path(__file__).resolve().parent.parent / "Lib" / "site-packages"))\nfor stream in (sys.stdin, sys.stdout, sys.stderr):\n    if hasattr(stream, "reconfigure"): stream.reconfigure(encoding="utf-8")\n', encoding='utf-8')
    if (target / 'pyvenv.cfg').exists(): raise ValueError('Portable runtime must not be a venv')

def prepare(args):
    payload = args.destination.resolve()
    if payload.exists(): raise FileExistsError('A prepared payload is never overwritten: ' + str(payload))
    payload.mkdir(parents=True)
    component_roots = {}
    def component(name, destination): component_roots[destination] = name
    embedded_runtime(payload / 'runtime-d', ROOT / 'runtime-d', args.embed, args.python_source)
    component('core', 'runtime-d')
    for folder in ('backend', 'examples', 'examples-d', 'licenses', 'licenses-d', 'docs'):
        copy_tree(ROOT / folder, payload / folder); component('core', folder)
    for name in ('requirements-d-lock.txt', 'requirements-lock.txt', 'reference-r-packages.csv', 'reference-r-conda-lock.txt', 'README.md'):
        shutil.copy2(ROOT / name, payload / name); component('core', name)
    for name in ('deploy.py', 'verify_runtime.py', 'verify_r.R', 'THIRD_PARTY_NOTICE.txt', 'native-library-probe.json'):
        (payload / 'installer').mkdir(exist_ok=True); shutil.copy2(ROOT / 'installer' / name, payload / 'installer' / name)
    component('core', 'installer')
    (payload / 'package').mkdir(); shutil.copy2(args.addin, payload / 'package/SpatialPredictionStudio.esriAddIn'); component('core', 'package')
    if args.reference_r:
        if not args.r_sources.is_dir() or not (args.r_sources / 'source-manifest.json').is_file():
            raise ValueError('Matching actual R/conda source archives and recipes are required')
        source_record = json.loads((args.r_sources / 'source-manifest.json').read_text(encoding='utf-8'))
        if source_record.get('status') != 'complete': raise ValueError('R corresponding source collection is incomplete')
        for record in source_record['components']:
            for item in record.get('sources', []):
                path = (args.r_sources / item['path']).resolve()
                if not path.is_relative_to(args.r_sources.resolve()) or not path.is_file() or sha(path) != item['sha256']:
                    raise ValueError('Corresponding source archive failed readback: ' + item['path'])
        copy_tree(args.r_sources, payload / 'corresponding-source'); component('core', 'corresponding-source')
        r_archive = packed_reference(args.build_python)
        with zipfile.ZipFile(r_archive) as archive: archive.extractall(payload / 'reference-r')
        # Windows searches System32 before PATH. Keep the already bundled,
        # unchanged conda C++ runtime beside both R launch executables.
        for source in (payload / 'reference-r/Library/bin').glob('*.dll'):
            if source.name.lower().startswith(('msvcp140', 'vcruntime140', 'concrt140', 'vcomp140')):
                for relative in ('lib/R/bin', 'lib/R/bin/x64'):
                    shutil.copy2(source, payload / 'reference-r' / relative / source.name)
        component('reference_r', 'reference-r')
    copy_tree(ROOT / 'models/tabpfn-v2', payload / 'models/tabpfn-v2'); component('tabpfn', 'models')
    if args.extension:
        extension = json.loads(args.extension.read_text(encoding='utf-8'))
        for item in extension.get('trees', []):
            dest = Path(item['destination'])
            if dest.is_absolute() or '..' in dest.parts or dest.parts[0] in component_roots: raise ValueError('Invalid extension destination')
            copy_tree(Path(item['source']), payload / dest); component(item['component'], dest.as_posix())
        for item in extension.get('private_runtimes', []):
            dest = Path(item['destination'])
            if dest.is_absolute() or '..' in dest.parts or dest.parts[0] in component_roots: raise ValueError('Invalid private runtime destination')
            embedded_runtime(payload / dest, Path(item['venv']), args.embed, args.python_source); component(item['component'], dest.as_posix())
        for item in extension.get('files', []):
            dest = Path(item['destination'])
            if dest.is_absolute() or '..' in dest.parts or dest.parts[0] in component_roots: raise ValueError('Invalid extension file destination')
            (payload / dest).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item['source'], payload / dest); component(item['component'], dest.as_posix())
    files = {}
    for path in sorted(payload.rglob('*')):
        if not path.is_file(): continue
        relative = path.relative_to(payload).as_posix()
        owner = max((prefix for prefix in component_roots if relative == prefix or relative.startswith(prefix + '/')), key=len)
        files[relative] = {'sha256': sha(path), 'bytes': path.stat().st_size, 'component': component_roots[owner]}
    manifest = {'schema_version': 1, 'product': 'SpatialPredictionStudio', 'version': '0.5.0', 'files': files,
                'build_source_root': str(ROOT),
                'components': sorted(set(component_roots.values())), 'source_addin_sha256': sha(args.addin),
                'python_distribution': {'version': '3.12.3', 'architecture': 'x64', 'archive_sha256': sha(args.embed), 'source_archive_sha256': sha(args.python_source)},
                'original_development_runtimes_modified': False}
    write(payload / 'payload-manifest.json', manifest)
    print(json.dumps({'payload': str(payload), 'files': len(files), 'bytes': sum(v['bytes'] for v in files.values()), 'manifest_sha256': sha(payload / 'payload-manifest.json')}, indent=2), flush=True)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--destination', type=Path, required=True)
    p.add_argument('--addin', type=Path, required=True)
    p.add_argument('--embed', type=Path, default=ROOT / 'installer/cache/python-3.12.3-embed-amd64.zip')
    p.add_argument('--python-source', type=Path, default=ROOT / 'installer/cache/Python-3.12.3.tar.xz')
    p.add_argument('--build-python', type=Path, default=Path(sys.executable))
    p.add_argument('--reference-r', action='store_true')
    p.add_argument('--r-sources', type=Path, default=ROOT / 'installer/corresponding-source')
    p.add_argument('--extension', type=Path)
    prepare(p.parse_args())

if __name__ == '__main__': main()
