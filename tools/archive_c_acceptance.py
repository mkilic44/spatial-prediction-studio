"""Archive the accepted C source and evidence without bundling interpreters."""
from pathlib import Path
import hashlib
import json
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / 'build/stage-c-accepted'

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def archive(path, files):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for file in sorted(set(files)):
            if file.is_file():
                z.write(file, file.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None

def main():
    DEST.mkdir(exist_ok=True)
    package = ROOT / 'dist/SpatialPredictionStudio.esriAddIn'
    tested = DEST / 'SpatialPredictionStudio.final-ui-tested.esriAddIn'
    assert sha(tested) == '1e21cdb9182f72360788a0fb5d7241b1dc78e0b92b96f6f2b4c0f6d1bf18dabf'
    with zipfile.ZipFile(tested) as old, zipfile.ZipFile(package) as new:
        assert set(old.namelist()) == set(new.namelist())
        changes = [n for n in old.namelist() if old.read(n) != new.read(n)]
        assert all(n.startswith('Install/docs/') or n == 'Install/build-manifest.json' for n in changes), changes
    readback = json.loads((ROOT/'build/arcmap-evidence/artifact-readback-C.json').read_text(encoding='utf-8'))
    assert readback['status'] == 'passed'
    roundtrip = json.loads((ROOT/'build/arcmap-evidence/cancel-roundtrip-readback-C.json').read_text(encoding='utf-8'))
    assert roundtrip['status'] == 'passed'
    shutil.copy2(package, DEST / 'SpatialPredictionStudio.esriAddIn')
    sources = []
    for folder in ('native','backend','tools','tests','examples','licenses'):
        sources += [p for p in (ROOT/folder).rglob('*') if not set(p.relative_to(ROOT).parts) & {'bin','obj','__pycache__','.pytest_cache'} and p.is_file()]
    sources += list(ROOT.glob('*.md')) + list(ROOT.glob('*lock.txt'))
    sources += [ROOT/'Config.xml', ROOT/'reference-r-packages.csv']
    archive(DEST/'source-stage-c.zip', sources)
    evidence = list((ROOT/'build/arcmap-evidence/c-runs').rglob('*'))
    evidence += list((ROOT/'build/native-tests/20261017-201057-343').rglob('*'))
    evidence += list((ROOT/'build/arcmap-evidence').glob('c-*'))
    evidence += list((ROOT/'build/arcmap-evidence').glob('*-C.json'))
    evidence += list((ROOT/'build/arcmap-evidence/logs').glob('*_42532.log'))
    evidence += list((ROOT/'build/arcmap-evidence/logs').glob('*_35072.log'))
    evidence += list((ROOT/'build').glob('*c*.xml'))
    # ASAMA_C_KABUL.md is the recorded Turkish stage C acceptance report; keep the file name as recorded.
    evidence += [ROOT/'build/c-review-final.md',ROOT/'build/c-tested-sources.json',ROOT/'ASAMA_C_KABUL.md']
    archive(DEST/'acceptance-evidence-stage-c.zip', evidence)
    record = {'sha256':sha(package),'ui_tested_sha256':sha(tested),'changes':changes,
              'backend_and_native_identical':True,'package_test_passed':True,
              'source_archive_sha256':sha(DEST/'source-stage-c.zip'),
              'evidence_archive_sha256':sha(DEST/'acceptance-evidence-stage-c.zip')}
    (DEST/'final-package-record.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
    print(json.dumps(record))

if __name__ == '__main__':
    main()
