"""Rebuild the private R environment of the kNNDM bridge and regenerate its CAST reference data.

Windows only, because reference-r-conda-lock.txt pins win-64 packages. Run from any folder with a
64-bit Python 3.12; outputs are written into this repository for review with git diff:

    python tools/cast_reference.py [--plan] [--fixtures]

1. Creates reference-r\\ from reference-r-conda-lock.txt with the pinned micromamba (installer/pins.json).
2. --plan lists CAST 1.0.4 and its dependencies from CRAN's R 4.4 Windows binaries again and writes
   reference-r-packages.csv with versions and the SHA-256 digest of every binary. Without --plan the
   committed list is used; when the list is missing, it is created.
3. Downloads the binaries, checks their SHA-256 digests and installs them into
   reference-r\\library-extra, as Install.cmd -WithR does.
4. --fixtures writes tests/fixtures/cast-1.0.4 with CAST itself (tools/create_cast_reference.R).

The last line of the output is a JSON summary with the longest file path inside reference-r, which
installer/sps_install.py uses for its Windows path length check.
"""
import argparse
import csv
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('sps_install', ROOT / 'installer/sps_install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def longest_relative_path(prefix):
    """Longest path below prefix, counted as the installer counts it (relative, with backslashes)."""
    base = '\\\\?\\' + str(prefix.resolve()) if sys.platform == 'win32' else str(prefix.resolve())
    longest, name = 0, ''
    for folder, _, files in os.walk(base):
        for file in files:
            relative = os.path.relpath(os.path.join(folder, file), base)
            if len(relative) > longest:
                longest, name = len(relative), relative
    return longest, name


def pin_binaries(plan_csv, target_csv, cache, log):
    """Download every planned binary once and record its SHA-256 digest."""
    with Path(plan_csv).open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    folder = Path(cache) / 'cran'
    folder.mkdir(parents=True, exist_ok=True)
    for row in rows:
        name = '%s_%s.zip' % (row['Package'], row['Version'])
        url = row['Repository'].rstrip('/') + '/' + name
        log('    pinning ' + url)
        request = urllib.request.Request(url, headers={'User-Agent': installer.USER_AGENT})
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
        (folder / name).write_bytes(data)
        row['SHA256'] = hashlib.sha256(data).hexdigest()
    with Path(target_csv).open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=['Package', 'Version', 'License', 'Repository', 'SHA256'],
                                quoting=csv.QUOTE_ALL, lineterminator='\n')
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--plan', action='store_true', help='list the CRAN packages again')
    parser.add_argument('--fixtures', action='store_true', help='write tests/fixtures/cast-1.0.4')
    parser.add_argument('--cache', default=str(ROOT / 'build/cast-cache'), help='download cache')
    args = parser.parse_args(argv)
    if sys.platform != 'win32':
        raise SystemExit('reference-r-conda-lock.txt pins Windows (win-64) packages; run this on Windows.')
    log = installer.Log(ROOT / 'build/cast-reference.log')
    pins = installer.read_json(ROOT / 'installer/pins.json')
    prefix = ROOT / 'reference-r'
    if prefix.exists():
        installer.remove_tree(prefix, ROOT)
    log('Creating reference-r from reference-r-conda-lock.txt')
    rscript = installer.create_r_environment(prefix, Path(args.cache), ROOT, pins, log)
    env = installer.r_environment(prefix)
    plan = args.plan or not (ROOT / 'reference-r-packages.csv').is_file()
    if plan:
        log('Listing CAST 1.0.4 and its dependencies from CRAN')
        installer.run([rscript, '--vanilla', ROOT / 'tools/plan_cast_reference.R'], log, cwd=ROOT, timeout=600, env=env)
        pin_binaries(ROOT / 'build/reference-cast-packages-plan.csv', ROOT / 'reference-r-packages.csv', args.cache, log)
    log('Installing the planned packages into reference-r/library-extra')
    binaries = installer.cran_binaries(ROOT / 'reference-r-packages.csv', Path(args.cache), log)
    installer.run([rscript, '--vanilla', ROOT / 'tools/install_cast_reference.R', binaries], log, cwd=ROOT,
                  timeout=3600, env=env)
    if args.fixtures:
        log('Writing tests/fixtures/cast-1.0.4 with CAST 1.0.4')
        installer.run([rscript, '--vanilla', ROOT / 'tools/create_cast_reference.R'], log, cwd=ROOT, timeout=600, env=env)
    longest, name = longest_relative_path(prefix)
    packages = len((ROOT / 'reference-r-packages.csv').read_text(encoding='utf-8').splitlines()) - 1
    conda_bytes = sum(path.stat().st_size for path in (Path(args.cache) / 'mamba/pkgs').glob('*')
                      if path.name.endswith(('.conda', '.tar.bz2')))
    cran_bytes = sum(path.stat().st_size for path in binaries.glob('*.zip'))
    summary = {'planned': plan, 'fixtures': args.fixtures, 'cran_packages': packages,
               'download_mb': {'conda': round(conda_bytes / 1e6, 1), 'cran': round(cran_bytes / 1e6, 1)},
               'longest_reference_r_path': longest, 'longest_reference_r_file': name}
    (ROOT / 'build/cast-reference-summary.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(summary))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except installer.InstallError as error:
        print('ERROR: ' + str(error), file=sys.stderr)
        sys.exit(1)
