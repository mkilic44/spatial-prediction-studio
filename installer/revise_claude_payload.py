"""Create a separate Claude-profile revision without changing scientific bytes."""
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
PARENT_SHA = '70573b90e438cc5a875f5a61c9c27548975938597260512a719c27ad3a89bd2c'
FROZEN = ('backend/', 'runtime-d/', 'runtime-mcp/', 'reference-r/', 'models/', 'package/')
CHANGES = ('mcp-server/register_clients.py', 'mcp-server/tests/test_claude_profiles.py',
           'mcp-server/README.md', 'installer/deploy.py', 'docs/KURULUM_REHBERI.md')


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    source = ROOT / 'installer/payload-050-rutf8'
    target = ROOT / 'installer/payload-050-claude-store'
    old_path = source / 'payload-manifest.json'
    if sha(old_path) != PARENT_SHA:
        raise ValueError('Accepted parent manifest changed')
    if target.exists():
        raise FileExistsError('Do not overwrite a prepared candidate')
    manifest = json.loads(old_path.read_text(encoding='utf-8'))
    observed = {p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_file()}
    if observed != set(manifest['files']) | {'payload-manifest.json'}:
        raise ValueError('Accepted parent file set changed')
    for name, item in manifest['files'].items():
        if sha(source / name) != item['sha256']:
            raise ValueError('Accepted parent file changed: ' + name)
    shutil.copytree(source, target)
    manifest.pop('final_binding')
    revision = []
    for name in CHANGES:
        old = manifest['files'].get(name)
        destination = target / name
        if old and sha(destination) != old['sha256']:
            raise ValueError('Copy changed: ' + name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)
        digest = sha(destination)
        if digest != sha(ROOT / name):
            raise ValueError('Revised file copy mismatch: ' + name)
        manifest['files'][name] = {'sha256': digest, 'bytes': destination.stat().st_size,
                                  'component': 'mcp' if name.startswith('mcp-server/') else 'core'}
        revision.append({'path': name, 'before_sha256': old['sha256'] if old else None,
                         'after_sha256': digest})
    old_manifest = json.loads(old_path.read_text(encoding='utf-8'))
    frozen = {name: item for name, item in old_manifest['files'].items() if name.startswith(FROZEN)}
    new_frozen = {name: item for name, item in manifest['files'].items() if name.startswith(FROZEN)}
    if frozen != new_frozen:
        raise ValueError('Scientific component manifest changed')
    for name, item in manifest['files'].items():
        if sha(target / name) != item['sha256']:
            raise ValueError('Candidate file readback failed: ' + name)
    manifest['revision'] = {'parent_manifest_sha256': PARENT_SHA, 'changes': revision,
        'scope': 'Claude classic and Store profile registration/removal, installer integration and user instructions only.'}
    out = target / 'payload-manifest.json'
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    record = {'status': 'prepared_not_bound', 'payload': str(target), 'manifest_sha256': sha(out),
              **manifest['revision'], 'scientific_component_files_identical': len(frozen),
              'frozen_prefixes': list(FROZEN), 'all_parent_and_candidate_files_sha256_read_back': True}
    (ROOT / 'installer/claude-store-payload-revision.json').write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
