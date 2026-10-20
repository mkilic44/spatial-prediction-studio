"""Enable implemented B methods only after a clean backend acceptance run."""
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
evidence=Path(sys.argv[1]).resolve()
tree=ET.parse(evidence)
suites=list(tree.getroot().iter('testsuite'))
assert suites and sum(int(s.get('tests','0')) for s in suites)>=150,'Acceptance suite is incomplete'
assert all(int(s.get('failures','0'))==int(s.get('errors','0'))==int(s.get('skipped','0'))==0 for s in suites),'Acceptance must pass without skipped tests'
path=ROOT/'backend/sps/registry.json'
registry=json.loads(path.read_text(encoding='utf-8'))
registry['registry_version']='0.2.0'
for methods in registry['categories'].values():
    for method in methods:
        if method['stage']=='B':method.update(enabled=True,status='backend_verified',reason='')
registry['acceptance']={'scope':'automated_backend_tests; real ArcMap acceptance is separately recorded',
                        'tests':sum(int(s.get('tests','0')) for s in suites),'evidence_sha256':hashlib.sha256(evidence.read_bytes()).hexdigest()}
path.write_text(json.dumps(registry,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(registry['acceptance']))
