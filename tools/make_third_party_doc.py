"""Write docs/THIRD_PARTY_LICENSES.md from the metadata of the pinned Windows wheels.

Usage: python tools/make_third_party_doc.py --wheels DIR [--wheels DIR ...] [--conda-licenses JSON]

The folders must hold the win_amd64 / cp312 wheels of every package in requirements-d-lock.txt
and requirements-mcp-lock.txt, for example from
  pip download --no-deps --only-binary=:all: --platform win_amd64 --python-version 3.12 -r <lock>
The license text is what each package declares in its own metadata; it is not interpreted here.
The R section lists reference-r-conda-lock.txt and reference-r-packages.csv. --conda-licenses names a
JSON file {"packages": {name: [version, license]}} taken from the conda-forge index (repodata.json)
for the locked conda packages; the default is tools/reference-r-conda-licenses.json.
"""
from __future__ import annotations

import argparse
import csv
from email.parser import Parser
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'docs/THIRD_PARTY_LICENSES.md'


def normalize(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def pins(path):
    result = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.split('--hash')[0].strip()
        if '==' in line and not line.startswith('#'):
            name, version = line.split('==', 1)
            result[normalize(name)] = (name, version.strip())
    return result


def wheel_metadata(folders):
    found = {}
    for folder in folders:
        for wheel in sorted(Path(folder).glob('*.whl')):
            with zipfile.ZipFile(wheel) as archive:
                name = next(n for n in archive.namelist() if n.endswith('.dist-info/METADATA') and n.count('/') == 1)
                message = Parser().parsestr(archive.read(name).decode('utf-8', 'replace'))
            key = normalize(message['Name'])
            found[(key, message['Version'])] = message
    return found


def license_of(message):
    expression = message.get('License-Expression')
    if expression:
        return expression.strip()
    classifiers = [c.split('::')[-1].strip() for c in message.get_all('Classifier') or [] if c.startswith('License ::')]
    declared = (message.get('License') or '').strip()
    if declared and len(declared) <= 60 and '\n' not in declared:
        return declared if not classifiers or declared in classifiers else declared + ' (' + '; '.join(classifiers) + ')'
    lines = [line.strip() for line in declared.splitlines() if line.strip()]
    if lines and len(lines[0]) <= 60:
        title = lines[0]
        if len(lines) > 1 and lines[1].lower().startswith('version'):
            title += ' ' + lines[1].split(',')[0]
        return title
    if classifiers:
        return '; '.join(classifiers)
    return 'see the package metadata' if not declared else 'see the license text in the package'


def table(title, intro, locked, metadata):
    lines = ['## ' + title, '', intro, '', '| Package | Version | Declared license |', '|---|---|---|']
    for key, (name, version) in sorted(locked.items()):
        message = metadata.get((key, version)) or metadata.get((key, version.split('+')[0]))
        lines.append('| %s | %s | %s |' % (name, version, license_of(message).replace('|', '/') if message else 'not checked'))
    return lines + ['']


def conda_lock_packages(path):
    """(name, version) of every package URL in an explicit conda lock file."""
    result = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.startswith('https://'):
            stem = line.split('#')[0].rsplit('/', 1)[1]
            stem = stem[:-len('.tar.bz2')] if stem.endswith('.tar.bz2') else stem.rsplit('.', 1)[0]
            name, version, _ = stem.rsplit('-', 2)
            result.append((name, version))
    return result


def r_section(conda_licenses):
    micromamba = json.loads((ROOT / 'installer/pins.json').read_text(encoding='utf-8'))['micromamba']
    lines = ['## R and CAST (optional)', '',
             'Installed only with `Install.cmd -WithR`, for the kNNDM validation design. micromamba %s '
             '(conda-forge, license %s) creates R 4.4.3 from the conda-forge packages in '
             '`reference-r-conda-lock.txt`. CAST 1.0.4 and its dependencies are the CRAN Windows binaries listed in '
             '`reference-r-packages.csv`. Licenses are as declared in the conda-forge index and in the CRAN package '
             'index.' % (micromamba['version'], micromamba['license']), '',
             '### conda-forge packages', '', '| Package | Version | Declared license |', '|---|---|---|']
    for name, version in sorted(conda_lock_packages(ROOT / 'reference-r-conda-lock.txt')):
        found = conda_licenses.get(name)
        if found and found[0] == version:
            license_text = found[1] or 'no license field in the conda-forge index'
        else:
            license_text = 'not checked'
        lines.append('| %s | %s | %s |' % (name, version, license_text.replace('|', '/')))
    lines += ['', '### CRAN packages', '', '| Package | Version | Declared license |', '|---|---|---|']
    with (ROOT / 'reference-r-packages.csv').open(newline='', encoding='utf-8') as stream:
        for row in sorted(csv.DictReader(stream), key=lambda row: row['Package'].lower()):
            lines.append('| %s | %s | %s |' % (row['Package'], row['Version'], row['License'].replace('|', '/')))
    return lines + ['']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheels', action='append', required=True)
    parser.add_argument('--conda-licenses', help='JSON {name: [version, license]} from the conda-forge index')
    args = parser.parse_args(argv)
    source = Path(args.conda_licenses) if args.conda_licenses else ROOT / 'tools/reference-r-conda-licenses.json'
    conda_licenses = json.loads(source.read_text(encoding='utf-8'))['packages'] if source.is_file() else {}
    metadata = wheel_metadata(args.wheels)
    worker = pins(ROOT / 'requirements-d-lock.txt')
    mcp = pins(ROOT / 'requirements-mcp-lock.txt')
    lines = ['# Third-party components', '',
             'Install.cmd downloads the packages below from their public sources (python.org builds on nuget.org, '
             'the Python Package Index and download.pytorch.org; with -WithR also conda-forge and CRAN). Each package '
             'keeps its own license. The licenses '
             'listed here are the ones each package declares in its own metadata. Read the full license text in the '
             'installed package folder before you redistribute anything.', '',
             'Generated by `tools/make_third_party_doc.py` from the Windows wheels of the pinned versions.', '',
             '## Runtime and build tools', '',
             '| Component | Version | Source | License |', '|---|---|---|---|',
             '| CPython (private runtime) | 3.12.3 | nuget.org package `python` | Python Software Foundation License (`LICENSE.txt` in the runtime folder) |',
             '| Roslyn C# compiler | 4.14.0 | nuget.org package `Microsoft.Net.Compilers.Toolset` | MIT |',
             '| .NET Framework 4.8 reference assemblies | 1.0.3 | nuget.org package `Microsoft.NETFramework.ReferenceAssemblies.net48` | as stated by the package: https://github.com/Microsoft/dotnet/blob/master/LICENSE |',
             '', 'The compiler and the reference assemblies are used only on your computer to build the add-in; they are not copied into the add-in.', '',
             '## Model weights', '',
             'TabPFN v2 regression weights (Prior Labs, revision pinned in `tools/setup-tabpfn-weights.py`) are '
             'downloaded only when you run `Install.cmd -WithTabPFN -AcceptTabPFNLicense`. They are licensed under the '
             'Prior Labs License 1.1, which the installer saves next to the weights. The `tabpfn` Python package itself '
             'is installed in every installation under the same license. Paragraph 10 of that license asks products that '
             'contain TabPFN source or weights to display "Built with PriorLabs-TabPFN" in their documentation and user '
             'interface. The README, the method description in the panel and the reports display it.', '']
    lines += table('Worker runtime', 'Packages installed into `runtime-d` from `requirements-lock.txt` and `requirements-d-lock.txt`.', worker, metadata)
    lines += table('MCP server runtime (optional)', 'Packages installed into `runtime-mcp` from `requirements-mcp-lock.txt` when you install with -WithMcp.', mcp, metadata)
    lines += r_section(conda_licenses)
    TARGET.write_text('\n'.join(lines).rstrip() + '\n', encoding='utf-8')
    missing = [name for key, (name, version) in {**worker, **mcp}.items()
               if (key, version) not in metadata and (key, version.split('+')[0]) not in metadata]
    print(TARGET, 'missing:', missing)


if __name__ == '__main__':
    main()
