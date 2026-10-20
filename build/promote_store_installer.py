"""Promote the accepted Store-profile installer; retain the previous original."""
from pathlib import Path
import hashlib
import json
import shutil

ROOT = Path(__file__).resolve().parents[1]

def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def main():
    old_sha = '7bb975c4f3668243923909da880c5d38b4e054d3945d2671c29ab57f6b4d39a2'
    new_sha = '4d94912519599091642ed52ff2a6ff569f9cda3f1ed65e25759236b49116930e'
    name = 'SpatialPredictionStudio-0.5.0-Setup.exe'
    target = (ROOT / 'release' / name).resolve(strict=True)
    previous = (ROOT / 'installer/dist' / name).resolve(strict=True)
    source = (ROOT / 'installer/dist-claude-store' / name).resolve(strict=True)
    for path in (target, previous, source):
        if not path.is_relative_to(ROOT):
            raise ValueError('Installer path outside workspace')
    accepted = json.loads((ROOT / 'build/installed-store-release/acceptance.json').read_text(encoding='utf-8-sig'))
    if accepted['status'] != 'passed' or accepted['installer_sha256'] != new_sha:
        raise ValueError('The new real installation has not passed')
    if sha(previous) != old_sha or sha(target) not in (old_sha, new_sha) or sha(source) != new_sha:
        raise ValueError('Reviewed source or existing release bytes changed')
    if sha(target) != new_sha:
        temporary = target.with_suffix('.exe.partial')
        if temporary.exists():
            raise ValueError('Review existing temporary installer before replacement')
        shutil.copy2(source, temporary)
        if sha(temporary) != new_sha or temporary.stat().st_size != 1831716362:
            raise ValueError('Copied installer differs')
        temporary.replace(target)
    if sha(target) != new_sha or sha(previous) != old_sha:
        raise ValueError('Promotion verification failed')
    record = {'status': 'passed', 'target': str(target), 'sha256': new_sha,
              'bytes': target.stat().st_size, 'previous_original_preserved': str(previous),
              'previous_sha256': old_sha}
    (ROOT / 'build/installed-store-release/installer-promotion.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps(record))

if __name__ == '__main__':
    main()
