"""Native process-contract fixture. Does not exercise ArcObjects or scientific models."""
import json
import os
import pathlib
import sys
import time

start_token = os.environ.pop('SPS_START_GATE', None)
if start_token is not None and sys.stdin.readline().rstrip('\r\n') != start_token:
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
