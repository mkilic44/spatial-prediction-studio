"""Native process-contract fixture. Does not exercise ArcObjects or scientific models."""
import json
import os
import pathlib
import sys
import time

# Same start gate as backend/worker.py, including the optional UTF-8 byte order mark.
start_token = os.environ.pop('SPS_START_GATE', None)
if start_token is not None:
    gate_line = sys.stdin.buffer.readline()
    if gate_line.startswith(b'\xef\xbb\xbf'):
        gate_line = gate_line[3:]
    if gate_line.rstrip(b'\r\n') != start_token.encode('utf-8'):
        raise SystemExit(70)

request = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
output = pathlib.Path(request["output_dir"])
output.mkdir(parents=True, exist_ok=True)
(output / "pid.txt").write_text(str(os.getpid()), encoding="utf-8")
print(json.dumps({"event": "progress", "run_id": request["run_id"], "stage": "fixture", "completed": 0, "total": 1, "message": "Fixture started"}), flush=True)
if request["mode"] == "hold":
    time.sleep(120)
else:
    (output / "result.json").write_text(json.dumps({"schema_version": 1 if request["mode"] == "wrong_schema" else request.get("schema_version", 1), "run_id": request["run_id"], "status": "completed", "outputs": []}), encoding="utf-8")
