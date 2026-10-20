"""Read-only R startup probes; no production payload or user environment edits."""
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
installed = ROOT.parent / 'sps-ç/app/versions/0.5.0-6d5fb15e486c'
rr = installed / 'reference-r'
code = 'print(R.home());print(.Library);print(.libPaths());print(Sys.getlocale());print(l10n_info());print(file.exists(file.path(.Library,"compiler","DESCRIPTION")));print(list.files(.Library));print(Sys.getenv(c("R_HOME","R_LIBS","R_LIBS_USER","R_LIBS_SITE")))'
records = []
for label, locale in [('as_installed_C', 'C'), ('utf8', 'en_US.UTF-8'), ('unset', None)]:
    env = os.environ.copy()
    for name in ('PYTHONHOME', 'PYTHONPATH', 'R_HOME', 'R_LIBS', 'R_LIBS_USER', 'R_LIBS_SITE', 'LC_ALL', 'LANG'):
        env.pop(name, None)
    env['R_HOME'] = str(rr / 'lib/R')
    env['PATH'] = os.pathsep.join(map(str, (rr / 'lib/R/bin/x64', rr / 'Library/bin', rr / 'Scripts', Path(os.environ['SystemRoot']) / 'System32')))
    env['R_ENABLE_JIT'] = '0'
    if locale is not None:
        env['LC_ALL'] = env['LANG'] = locale
    result = subprocess.run([str(rr / 'lib/R/bin/Rscript.exe'), '--vanilla', '--default-packages=NULL', '-e', code],
                            env=env, capture_output=True, encoding='utf-8', errors='replace', timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    records.append({'case': label, 'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr})
record = {'installation_root': str(installed), 'production_changed': False, 'cases': records}
(ROOT / 'installer/r-unicode-startup-probe.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(record, ensure_ascii=False, indent=2))
