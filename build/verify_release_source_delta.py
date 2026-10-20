"""Bind the final Unicode fix to the preceding accepted add-in without rebuilding native code."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    old = ROOT / 'build/stage-050-mcp-release/SpatialPredictionStudio.esriAddIn'
    new = ROOT / 'build/stage-050-utf8-release/SpatialPredictionStudio.esriAddIn'
    with zipfile.ZipFile(old) as a, zipfile.ZipFile(new) as b:
        first = json.loads(a.read('Install/build-manifest.json'))
        last = json.loads(b.read('Install/build-manifest.json'))
        changed = sorted(name for name in set(first['sources']) | set(last['sources'])
                         if first['sources'].get(name) != last['sources'].get(name))
        assert changed == ['backend/sps/knndm.py', 'tests/test_knndm_c.py'], changed
        assert a.read('Install/SpatialPredictionStudio.dll') == b.read('Install/SpatialPredictionStudio.dll')
        assert first['external_assets'] == last['external_assets']
        for name, expected in last['sources'].items():
            assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected, name
        for name, expected in last['members'].items():
            assert hashlib.sha256(b.read(name)).hexdigest() == expected, name
        changed_members = sorted(name for name in set(a.namelist()) | set(b.namelist())
                                 if a.read(name) != b.read(name))
        assert changed_members == ['Install/backend/sps/knndm.py', 'Install/build-manifest.json']
        dll = hashlib.sha256(b.read('Install/SpatialPredictionStudio.dll')).hexdigest()
    xml = ET.parse(ROOT / 'build/cast-utf8-final.xml')
    suites = list(xml.getroot().iter('testsuite'))
    counts = {key: sum(int(s.attrib.get(key, 0)) for s in suites)
              for key in ('tests', 'failures', 'errors', 'skipped')}
    assert counts == {'tests': 5, 'failures': 0, 'errors': 0, 'skipped': 0}
    record = {'status': 'passed', 'previous_addin_sha256': hashlib.sha256(old.read_bytes()).hexdigest(),
              'final_source_addin_sha256': hashlib.sha256(new.read_bytes()).hexdigest(),
              'changed_sources': changed, 'changed_members': changed_members,
              'unchanged_native_dll_sha256': dll, 'final_manifest_current': True,
              'previous_backend_tests': 522, 'affected_regression_tests': counts,
              'test_count_note': 'Affected tests overlap the previous suite; the counts are not added.'}
    target = ROOT / 'build/release-source-delta.json'
    target.write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps(record))


if __name__ == '__main__':
    main()
