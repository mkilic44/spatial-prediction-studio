"""Real process tree used only by the managed lifecycle contract test."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

gate = os.environ.pop('SPS_START_GATE', None)
if gate is not None and sys.stdin.readline().rstrip('\r\n') != gate:
    raise SystemExit(70)
request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig'))
output = Path(request['output_dir'])
output.mkdir(parents=True, exist_ok=True)
(output / 'gate.txt').write_text('passed' if gate else 'missing')
(output / 'models.txt').write_text(os.environ.get('SPS_MODELS_DIR', 'missing'))
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(40)'])
(output / 'child.pid').write_text(str(child.pid))
if request['mode'] == 'normal':
    (output / 'result.json').write_text(json.dumps({
        'schema_version': request['schema_version'], 'run_id': request['run_id'], 'status': 'completed'}))
else:
    time.sleep(40)
