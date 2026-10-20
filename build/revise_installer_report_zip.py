"""Replace only the approved report and its indexed checksum in the evidence ZIP."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OLD = '9cdc0cbdf4ef69ecef51ddb14ca85e092ec4ea13bb64dfcdfe94b5eea661ab35'
NAME = 'installer/CLAUDE_STORE_ACCEPTANCE.md'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    archive = ROOT / 'installer/dist-claude-store/SpatialPredictionStudio-0.5.0-installer-source-evidence.zip'
    report = ROOT / NAME
    record_path = archive.parent / 'installer-source-evidence-record.json'
    record = json.loads(record_path.read_text(encoding='utf-8'))
    assert sha(archive) == OLD == record['sha256']
    prior = ROOT / 'build/claude-store-report-before-terms.zip'
    assert not prior.exists()
    shutil.copy2(archive, prior)
    assert sha(prior) == OLD
    temporary = archive.with_suffix('.report-revision.tmp')
    assert not temporary.exists()
    before = {}
    with zipfile.ZipFile(archive) as original:
        assert len(original.namelist()) == len(set(original.namelist()))
        index = json.loads(original.read('archive-manifest.json'))
        for name, item in index.items():
            assert hashlib.sha256(original.read(name)).hexdigest() == item['sha256'], name
        index[NAME].update(sha256=sha(report), bytes=report.stat().st_size)
        with zipfile.ZipFile(temporary, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as updated:
            for info in original.infolist():
                data = original.read(info.filename)
                before[info.filename] = hashlib.sha256(data).hexdigest()
                if info.filename == NAME:
                    data = report.read_bytes()
                elif info.filename == 'archive-manifest.json':
                    data = json.dumps(index, indent=2).encode('utf-8')
                updated.writestr(info, data)
    with zipfile.ZipFile(temporary) as updated:
        assert updated.testzip() is None
        assert set(updated.namelist()) == set(before)
        changed = sorted(n for n in before if hashlib.sha256(updated.read(n)).hexdigest() != before[n])
        assert changed == ['archive-manifest.json', NAME], changed
        for name, item in index.items():
            data = updated.read(name)
            assert hashlib.sha256(data).hexdigest() == item['sha256'] and len(data) == item['bytes'], name
    os.replace(temporary, archive)
    record.update(sha256=sha(archive), bytes=archive.stat().st_size)
    record_path.write_text(json.dumps(record, indent=2), encoding='utf-8')
    proof = {'status': 'verified', 'changed_zip_members': changed,
             'previous_archive_sha256': OLD, 'previous_archive_copy': str(prior),
             'archive_sha256': sha(archive), 'archive_bytes': archive.stat().st_size,
             'report_sha256': sha(report), 'verified_indexed_members': len(index),
             'installer_sha256': sha(Path(json.loads((archive.parent / 'installer-build-record.json').read_text())['installer'])),
             'payload_manifest_sha256': sha(ROOT / 'installer/payload-050-claude-store/payload-manifest.json')}
    assert proof['installer_sha256'] == '4d94912519599091642ed52ff2a6ff569f9cda3f1ed65e25759236b49116930e'
    assert proof['payload_manifest_sha256'] == '7ea94d60f575c973975af06f8460ece04933be34392380fa515b3b3b2fd7a7c4'
    (ROOT / 'build/claude-store-report-terms-readback.json').write_text(json.dumps(proof, indent=2), encoding='utf-8')
    print(json.dumps(proof, indent=2))


if __name__ == '__main__':
    main()
