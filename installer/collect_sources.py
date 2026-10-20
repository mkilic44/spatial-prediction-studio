"""Fetch exact source archives plus conda recipes/patches for redistribution.

No future written offer is substituted for source files. A missing archive leaves
the manifest incomplete and blocks the reference-R installer component.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
import shutil
import tarfile
import urllib.request
import urllib.parse
import yaml

ROOT = Path(__file__).resolve().parents[1]

def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1048576), b''): h.update(block)
    return h.hexdigest()

def download(urls, folder, expected=None):
    errors = []
    for url in urls:
        if url.startswith('http://'): url = 'https://' + url[7:]
        if not url.startswith('https://'): errors.append('HTTPS source required: ' + url); continue
        name = (expected[:16] + '-' if expected else '') + urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit('/', 1)[-1])
        if not name or '/' in name or '\\' in name: raise ValueError('Invalid source archive name')
        path = folder / name; path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if not path.exists():
                temporary = path.with_suffix(path.suffix + '.download')
                request = urllib.request.Request(url, headers={'User-Agent': 'SpatialPredictionStudio-source-packaging/0.4.1'})
                with urllib.request.urlopen(request, timeout=90) as stream, temporary.open('wb') as output: shutil.copyfileobj(stream, output)
                if expected and sha(temporary) != expected: temporary.unlink(); raise ValueError('Source SHA-256 mismatch: ' + url)
                temporary.replace(path)
            actual = sha(path)
            if expected and actual != expected: raise ValueError('Cached source SHA-256 mismatch: ' + str(path))
            return {'url': url, 'path': str(path.relative_to(folder.parent)), 'sha256': actual, 'bytes': path.stat().st_size, 'upstream_recipe_sha256': expected}
        except Exception as exc: errors.append(str(exc))
    raise RuntimeError('; '.join(errors))

def source_specs(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == 'source':
                if isinstance(item, dict): yield item
                elif isinstance(item, list):
                    for spec in item:
                        if isinstance(spec, dict): yield spec
            elif isinstance(item, (dict, list)): yield from source_specs(item)
    elif isinstance(value, list):
        for item in value: yield from source_specs(item)

def dcf(text):
    result = {}; current = None
    for line in text.splitlines():
        if line.startswith((' ', '\t')) and current: result[current] += '\n' + line.strip()
        elif ':' in line: current, value = line.split(':', 1); result[current] = value.strip()
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--destination', type=Path, default=ROOT / 'installer/corresponding-source')
    args = parser.parse_args(); dest = args.destination.resolve(); dest.mkdir(parents=True, exist_ok=True)
    records = []; failures = []
    for meta in sorted((ROOT / 'reference-r/conda-meta').glob('*.json')):
        data = json.loads(meta.read_text()); name = data['name']; recipe_root = Path(data['link']['source']) / 'info/recipe'
        record = {'kind': 'conda', 'name': name, 'version': data['version'], 'build': data['build'], 'license': data.get('license'), 'sources': []}
        required = re.search(r'\b(?:A?GPL|LGPL|MPL|EPL)(?:[-(\s]|$)', str(data.get('license', '')), re.IGNORECASE) is not None
        try:
            licenses = recipe_root.parent / 'licenses'
            if licenses.is_dir(): shutil.copytree(licenses, dest / 'licenses' / meta.stem, dirs_exist_ok=True)
            if recipe_root.is_dir():
                target = dest / 'recipes' / meta.stem; shutil.copytree(recipe_root, target, dirs_exist_ok=True)
                recipe_file = next((recipe_root / f for f in ('meta.yaml', 'rendered_recipe.yaml') if (recipe_root / f).is_file()), None)
                if recipe_file:
                    parsed = yaml.safe_load(recipe_file.read_text(encoding='utf-8'))
                    if 'recipe' in parsed: parsed = parsed['recipe']
                    specs = list(source_specs(parsed))
                    for spec in specs:
                        if required and 'git_url' in spec:
                            repo = spec['git_url'].removesuffix('.git').removeprefix('https://github.com/')
                            revision = spec.get('git_rev', '')
                            if not spec['git_url'].startswith('https://github.com/') or not re.fullmatch(r'[0-9a-fA-F]{7,40}', revision): raise ValueError('Pinned GitHub source revision required')
                            with urllib.request.urlopen('https://api.github.com/repos/' + repo + '/commits/' + revision, timeout=60) as stream: commit = json.load(stream)['sha']
                            if not commit.startswith(revision): raise ValueError('Source commit does not match recipe revision')
                            archive = download(['https://github.com/' + repo + '/archive/' + commit + '.tar.gz'], dest / 'archives')
                            archive['git_commit'] = commit; archive['recipe_git_revision'] = revision; record['sources'].append(archive)
                        if 'url' not in spec: continue
                        urls = spec['url'] if isinstance(spec['url'], list) else [spec['url']]
                        if required:
                            if not spec.get('sha256'): raise ValueError('Required source lacks recipe SHA-256')
                            record['sources'].append(download(urls, dest / 'archives', spec['sha256']))
                    record['recipe'] = str(target.relative_to(dest))
            if required and not record['sources']: raise ValueError('Copyleft component has no resolved matching source archive')
            record['status'] = 'matched_sources' if record['sources'] else 'non_copyleft_recipe_and_license_record'
        except Exception as exc: record['status'] = 'incomplete'; record['error'] = str(exc); failures.append(name + ': ' + str(exc))
        records.append(record); print(name, record['status'], flush=True)
    for package in sorted((ROOT / 'reference-r/library-extra').iterdir()):
        if not package.is_dir(): continue
        description = package / 'DESCRIPTION'
        if not description.is_file(): continue
        fields = dcf(description.read_text(encoding='utf-8', errors='strict')); name, version = fields['Package'], fields['Version']
        record = {'kind': 'R', 'name': name, 'version': version, 'license': fields.get('License')}
        try:
            filename = name + '_' + version + '.tar.gz'
            archive = download(['https://cran.r-project.org/src/contrib/' + filename, 'https://cran.r-project.org/src/contrib/Archive/' + name + '/' + filename], dest / 'archives')
            with tarfile.open(dest / archive['path'], 'r:gz') as source:
                data = dcf(source.extractfile(name + '/DESCRIPTION').read().decode('utf-8'))
                if data['Package'] != name or data['Version'] != version: raise ValueError('R source DESCRIPTION version mismatch')
            record.update({'status': 'matched_sources', 'sources': [archive], 'installed_description_sha256': sha(description)})
        except Exception as exc: record.update({'status': 'incomplete', 'error': str(exc)}); failures.append(name + ': ' + str(exc))
        records.append(record); print(name, record['status'], flush=True)
    # Python wrapper includes Cubist C code under GPL; retain its exact sdist.
    try:
        with urllib.request.urlopen('https://pypi.org/pypi/cubist/0.1.2/json', timeout=60) as stream: metadata = json.load(stream)
        sdist = next(x for x in metadata['urls'] if x['packagetype'] == 'sdist')
        records.append({'kind': 'Python', 'name': 'cubist', 'version': '0.1.2', 'status': 'matched_sources', 'sources': [download([sdist['url']], dest / 'archives', sdist['digests']['sha256'])]})
    except Exception as exc: failures.append('cubist: ' + str(exc))
    for version in ('3.13.0', '3.13.1'):
        try:
            archive = download(['https://download.osgeo.org/geos/geos-' + version + '.tar.bz2'], dest / 'archives')
            with tarfile.open(dest / archive['path'], 'r:bz2') as source:
                if not any(m.name.startswith('geos-' + version + '/') for m in source.getmembers()): raise ValueError('GEOS archive version mismatch')
            records.append({'kind': 'native_library', 'name': 'GEOS', 'version': version, 'status': 'matched_sources', 'sources': [archive], 'binary_version_evidence': 'installer/native-library-probe.json'})
        except Exception as exc: failures.append('GEOS ' + version + ': ' + str(exc))
    manifest = {'schema_version': 1, 'status': 'complete' if not failures else 'incomplete', 'components': records, 'failed': failures,
                'scope': 'Installed conda copyleft source archives with recipes and patches, all installed extra R package source archives, Cubist Python source; license records stay with binaries.'}
    (dest / 'source-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': manifest['status'], 'components': len(records), 'failed': failures}, ensure_ascii=False), flush=True)
    if failures: raise SystemExit(1)

if __name__ == '__main__': main()
