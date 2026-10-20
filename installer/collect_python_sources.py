"""Add exact Python and bundled native-library sources to the offline delivery."""
from __future__ import annotations
import json
from pathlib import Path
import re
import tarfile
import urllib.request

from collect_sources import ROOT, download, sha

DEST = ROOT / 'installer/corresponding-source'
GCC_SHA = '64f404c1a650f27fc33da242e1f2df54952e3963a49e06e73f6940f3223ac344'
RTOOLS = 'c5344cc8c7e310ee4eee513b2381af2e75d65cb9'


def main():
    manifest_path = DEST / 'source-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest['status'] != 'complete':
        raise ValueError('Complete R source inventory is required first')
    records = [r for r in manifest['components'] if r.get('source_group') != 'python_native']
    added = []
    for name, version in [('certifi', '2026.7.22'), ('tqdm', '4.70.1'), ('numpy', '1.26.4'), ('scipy', '1.13.1')]:
        with urllib.request.urlopen(f'https://pypi.org/pypi/{name}/{version}/json', timeout=60) as stream:
            data = json.load(stream)
        source = next(x for x in data['urls'] if x['packagetype'] == 'sdist')
        archive = download([source['url']], DEST / 'archives', source['digests']['sha256'])
        added.append({'kind': 'Python', 'name': name, 'version': version, 'sources': [archive],
                      'version_evidence': f'requirements-d-lock.txt: {name}=={version}'})
        print(name, version, archive['sha256'], flush=True)
    snapshots = [
        ('OpenMathLib/OpenBLAS', 'c2f4bdbbb43a1d20a7342f40122e18e573ce436a', 'OpenBLAS', '0.3.23-293-gc2f4bdbb'),
        ('OpenMathLib/OpenBLAS', '5fbe25954b0ddf88c6fc4f4644cd17b282e5c1b0', 'OpenBLAS', '0.3.27'),
        ('MacPython/openblas-libs', '1949e84a97655ed586a2cb4b469e98efec8651c0', 'OpenBLAS-build-recipes', 'numpy-1.26.4'),
        ('MacPython/openblas-libs', 'f5833b25d004d5219609c774b38d9bed398d7e5c', 'OpenBLAS-build-recipes', 'scipy-1.13.1'),
        ('r-windows/rtools-ucrt', RTOOLS, 'Rtools40-UCRT-build-recipes', 'gcc-10.3.0'),
    ]
    for repo, commit, name, version in snapshots:
        archive = download([f'https://github.com/{repo}/archive/{commit}.tar.gz'], DEST / 'archives')
        archive['git_commit'] = commit
        with tarfile.open(DEST / archive['path'], 'r:gz') as source:
            root = source.getmembers()[0].name.split('/')[0]
            if not root.endswith(commit): raise ValueError('Source archive commit prefix mismatch')
            if repo == 'r-windows/rtools-ucrt':
                recipe = source.extractfile(root + '/mingw-w64-gcc/PKGBUILD').read().decode('utf-8')
                if not re.search(r'^pkgver=10\.3\.0$', recipe, re.M) or GCC_SHA not in recipe:
                    raise ValueError('GCC source version or recipe checksum mismatch')
        added.append({'kind': 'native_library_source', 'name': name, 'version': version, 'sources': [archive]})
        print(name, version, archive['sha256'], flush=True)
    gcc = download(['https://ftp.gnu.org/gnu/gcc/gcc-10.3.0/gcc-10.3.0.tar.xz'], DEST / 'archives', GCC_SHA)
    added.append({'kind': 'native_library_source', 'name': 'GCC-libquadmath-libgfortran-libgcc', 'version': '10.3.0',
                  'sources': [gcc], 'recipe_commit': RTOOLS,
                  'binary_evidence': 'NumPy and SciPy OpenBLAS DLLs contain GCC 10.3.0 R-project and quadmath symbols; matching source and build recipes included.'})
    for record in added:
        record.update(source_group='python_native', status='matched_sources')
        for archive in record['sources']:
            if sha(DEST / archive['path']) != archive['sha256']: raise ValueError('Source readback failed')
    manifest['components'] = records + added
    manifest['scope'] = ('Conda copyleft source archives with recipes and patches; all installed extra R package sources; '
                         'Cubist, certifi, tqdm, NumPy and SciPy sources; bundled GEOS and OpenBLAS sources; '
                         'matching GCC 10.3.0 runtime source plus Rtools/OpenBLAS build recipes. '
                         'Unmodified binary license records remain installed with their packages.')
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'complete', 'added_components': len(added), 'total_components': len(manifest['components']),
                      'manifest_sha256': sha(manifest_path)}), flush=True)


if __name__ == '__main__': main()
