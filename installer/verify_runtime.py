"""Read back relocated dependencies and a tiny CPU/raster operation."""
import argparse
import importlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time

parser = argparse.ArgumentParser(); parser.add_argument('--root', type=Path, required=True); parser.add_argument('--components', required=True)
args = parser.parse_args(); root = args.root.resolve(); components = args.components.split(',')
assert sys.version_info[:3] == (3, 12, 3) and struct.calcsize('P') == 8
assert Path(sys.executable).resolve().parents[2] == root
assert sys.flags.isolated and sys.flags.no_user_site
for name in ('PYTHONHOME', 'PYTHONPATH'): os.environ.pop(name, None)
os.environ.update({'TABPFN_DISABLE_TELEMETRY': '1', 'DO_NOT_TRACK': '1', 'HF_HUB_OFFLINE': '1', 'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1', 'PROJ_NETWORK': 'OFF'})
versions = {}
for line in (root / 'requirements-d-lock.txt').read_text().splitlines():
    if '==' not in line: continue
    name, expected = line.strip().split('==', 1); actual = metadata.version(name)
    assert actual == expected, (name, expected, actual); versions[name] = actual
modules = {}
for name in ('torch', 'numpy', 'scipy', 'sklearn', 'rasterio', 'pyproj', 'xgboost', 'lightgbm', 'catboost', 'cubist', 'optuna', 'quantile_forest', 'tabm', 'pytabkit', 'tabpfn'):
    module = importlib.import_module(name); path = Path(module.__file__).resolve()
    assert path.is_relative_to(root), (name, path); modules[name] = str(path.relative_to(root))
import numpy as np
from sklearn.linear_model import Ridge
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
x = np.arange(32, dtype=float).reshape(16, 2); y = x[:, 0] * .5 + 2
prediction = Ridge(alpha=1).fit(x, y).predict(x)
assert np.isfinite(prediction).all()
with MemoryFile() as memory:
    with memory.open(driver='GTiff', width=4, height=4, count=1, dtype='float64', crs='EPSG:32637', transform=from_origin(500000, 4200000, 30, 30)) as dst: dst.write(prediction.reshape(4, 4), 1)
    with memory.open() as src: assert np.array_equal(src.read(1).ravel(), prediction)
import psutil
crt_paths = sorted({Path(item.path).resolve() for item in psutil.Process().memory_maps()
                    if any(name in item.path.lower() for name in ('msvcp140', 'vcruntime140', 'concrt140'))})
assert crt_paths and all(path.is_relative_to(root) for path in crt_paths), crt_paths
result = {'status': 'passed', 'python': sys.version, 'package_versions': versions, 'module_paths': modules,
          'private_cpp_runtime_paths': [str(path.relative_to(root)) for path in crt_paths],
          'ridge_raster_readback': True, 'parent_installation_used': False}
if 'reference_r' in components:
    env = os.environ.copy(); rr = root / 'reference-r'; env.update({'R_HOME': str(rr / 'lib/R'), 'LC_ALL': 'en_US.UTF-8', 'LANG': 'en_US.UTF-8'})
    env['PATH'] = os.pathsep.join(map(str, (rr / 'lib/R/bin/x64', rr / 'Library/bin', rr / 'Scripts'))) + os.pathsep + env.get('PATH', '')
    process = subprocess.Popen([str(rr / 'lib/R/bin/Rscript.exe'), '--vanilla', str(root / 'installer/verify_r.R'), str(root)], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    started = time.monotonic(); r_crt_paths = set(); owned = {}
    try:
        primary = psutil.Process(process.pid); owned[primary.pid] = primary.create_time()
        while process.poll() is None:
            try:
                processes = [primary, *primary.children(recursive=True)]
                for child in processes:
                    owned[child.pid] = child.create_time()
                    r_crt_paths.update(Path(item.path).resolve() for item in child.memory_maps()
                                       if any(name in item.path.lower() for name in ('msvcp140', 'vcruntime140', 'concrt140')))
            except psutil.NoSuchProcess: pass
            if time.monotonic() - started > 120: raise TimeoutError('Private R verification timed out')
            time.sleep(.2)
        stdout, stderr = process.communicate(timeout=10)
    except BaseException:
        for pid, created in reversed(list(owned.items())):
            try:
                child = psutil.Process(pid)
                if child.create_time() == created: child.kill()
            except psutil.NoSuchProcess: pass
        process.wait(timeout=10); raise
    assert process.returncode == 0, stdout + stderr
    assert 'SPS_REFERENCE_R_OK' in stdout; result['reference_r'] = stdout.strip()
    # Windows searches System32 before PATH, so a C++ runtime installed in Windows is loaded first. Each
    # runtime DLL must come from the private environment or from System32, and the environment must hold
    # its own copies in Library\bin (on PATH for the kNNDM bridge) for computers without a system runtime.
    system = Path(os.environ.get('SystemRoot', r'C:\Windows')).resolve() / 'System32'
    assert r_crt_paths and all(path.is_relative_to(rr) or path.parent == system for path in r_crt_paths), r_crt_paths
    assert all((rr / 'Library/bin' / name).is_file() for name in ('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll'))
    result['private_r_cpp_runtime_paths'] = [str(path.relative_to(root)) if path.is_relative_to(rr) else str(path)
                                             for path in sorted(r_crt_paths)]
if 'tabpfn' in components:
    # Payload fingerprints already cover checkpoint and license; no fit or network.
    manifest = json.loads((root / 'models/tabpfn-v2/weights-manifest.json').read_text(encoding='utf-8'))
    result['tabpfn_weight_manifest_present'] = bool(manifest)
if 'mcp' in components:
    exe = root / 'runtime-mcp/Scripts/python.exe'
    script = '''import mcp,sys,struct,json,importlib.metadata as metadata
from pathlib import Path
assert struct.calcsize("P")==8 and sys.flags.isolated
versions={}
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if "==" not in line: continue
    name,expected=line.strip().split("==",1);expected=expected.split()[0]
    actual=metadata.version(name);assert actual==expected,(name,expected,actual)
    versions[name]=actual
print(json.dumps({"module":mcp.__file__,"versions":versions,"stdout_encoding":sys.stdout.encoding}))'''
    check = subprocess.run([str(exe), '-X', 'utf8', '-I', '-c', script, str(root / 'requirements-mcp-lock.txt')], capture_output=True, text=True, encoding='utf-8', timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert check.returncode == 0, check.stderr
    mcp_record=json.loads(check.stdout)
    assert Path(mcp_record['module']).resolve().is_relative_to(root) and mcp_record['stdout_encoding']=='utf-8'
    result['mcp_private_import'] = True; result['mcp_package_versions']=mcp_record['versions']
print(json.dumps(result, ensure_ascii=False))
