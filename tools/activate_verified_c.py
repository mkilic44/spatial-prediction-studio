"""Bind C activation to a complete passing suite and unchanged tested sources."""
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / 'build/c-tested-sources.json'

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def sources():
    paths = [p for folder in ('backend', 'tests') for p in (ROOT/folder).rglob('*')
             if p.is_file() and p.suffix in ('.py', '.R', '.json', '.csv') and '__pycache__' not in p.parts]
    paths += [ROOT/'requirements-lock.txt', ROOT/'reference-r-packages.csv']
    return {p.relative_to(ROOT).as_posix(): digest(p) for p in sorted(paths)}

if sys.argv[1:] == ['--snapshot']:
    SNAPSHOT.parent.mkdir(exist_ok=True)
    SNAPSHOT.write_text(json.dumps(sources(), indent=2), encoding='utf-8')
    print('Recorded source hashes before C acceptance tests')
else:
    evidence = Path(sys.argv[1]).resolve()
    assert json.loads(SNAPSHOT.read_text(encoding='utf-8')) == sources(), 'Tested sources changed'
    suites = list(ET.parse(evidence).getroot().iter('testsuite'))
    count = sum(int(s.get('tests', '0')) for s in suites)
    assert count >= 240, 'Complete C acceptance suite is required'
    assert all(all(int(s.get(k, '0')) == 0 for k in ('failures', 'errors', 'skipped')) for s in suites)
    classes = {c.get('classname').rsplit('.', 1)[-1] for s in suites for c in s.iter('testcase')}
    assert {'test_engine_c', 'test_knndm_c', 'test_applicability_c', 'test_diagnostics_c', 'test_models_c', 'test_kriging_c', 'test_review_c'} <= classes
    path = ROOT/'backend/sps/registry.json'
    registry = json.loads(path.read_text(encoding='utf-8'))
    registry['registry_version'] = '0.3.0'
    for methods in registry['categories'].values():
        for method in methods:
            if method['stage'] == 'C' and method['status'] == 'implementation_testing':
                method.update(enabled=True, status='backend_verified', reason='')
    registry['acceptance_c'] = {'scope': 'Automated backend verified; real ArcMap acceptance separately required',
                                'tests': count, 'evidence_sha256': digest(evidence),
                                'tested_source_manifest_sha256': digest(SNAPSHOT)}
    path.write_text(json.dumps(registry, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(registry['acceptance_c']))
