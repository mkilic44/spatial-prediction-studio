"""Clone an immutable payload, changing only the approved R child locale files."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    original = ROOT / 'installer/payload-050'
    destination = ROOT / 'installer/payload-050-rutf8'
    manifest_path = original / 'payload-manifest.json'
    previous_hash = sha(manifest_path)
    if previous_hash != '6d5fb15e486cd3d9c5c542bca9f915acb591aaf338b0bad370f7edc6267c019a':
        raise ValueError('The approved parent payload changed')
    if destination.exists():
        raise FileExistsError('Do not overwrite a prepared revision')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    observed = {p.relative_to(original).as_posix() for p in original.rglob('*') if p.is_file()}
    if observed != set(manifest['files']) | {'payload-manifest.json'}:
        raise ValueError('Parent payload contains unlisted or missing files')
    changes = ['backend/sps/knndm.py', 'installer/verify_runtime.py', 'docs/KURULUM_REHBERI.md']
    for name in changes:
        source = ROOT / name
        if source.suffix == '.py' and 'en_US.UTF-8' not in source.read_text(encoding='utf-8'):
            raise ValueError('The approved Unicode locale fix is not ready: ' + name)
    shutil.copytree(original, destination)
    del manifest['final_binding']
    revision = []
    for name in changes:
        target = destination / name
        before = manifest['files'][name]['sha256']
        if sha(target) != before:
            raise IOError('Parent file copy changed: ' + name)
        shutil.copy2(ROOT / name, target)
        after = sha(target)
        if after != sha(ROOT / name):
            raise IOError('Revised source did not copy exactly: ' + name)
        manifest['files'][name].update(sha256=after, bytes=target.stat().st_size)
        revision.append({'path': name, 'before_sha256': before, 'after_sha256': after})
    manifest['revision'] = {'parent_manifest_sha256': previous_hash, 'changes': revision,
                          'scope': 'Explicit UTF-8 locale in R child processes; interpreter, dependency and model bytes unchanged.'}
    out = destination / 'payload-manifest.json'
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    record = {'status': 'prepared_not_bound', 'payload': str(destination),
              'manifest_sha256': sha(out), **manifest['revision']}
    (ROOT / 'installer/rutf8-payload-revision.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps(record, indent=2))


if __name__ == '__main__':
    main()
