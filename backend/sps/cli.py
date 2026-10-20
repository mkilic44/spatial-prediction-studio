"""Command-line boundary used only by the ArcMap JobController."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from .data import read_json
from .engine import run_job


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    if len(args) != 1 or not Path(args[0]).is_absolute():
        print("Usage: python worker.py ABSOLUTE_REQUEST_JSON", file=sys.stderr)
        return 2
    try:
        request = read_json(args[0])
    except Exception as exc:
        print("REQUEST_READ: " + str(exc), file=sys.stderr)
        return 2
    def emit(event):
        print(json.dumps(event, ensure_ascii=False, allow_nan=False), flush=True)
    if isinstance(request, dict) and request.get('schema_version') == 2:
        from .engine_b import run_job_b
        result = run_job_b(request, emit)
    else:
        result = run_job(request, emit)
    if result["status"] == "completed":
        return 0
    print(json.dumps(result, ensure_ascii=False, allow_nan=False), file=sys.stderr, flush=True)
    return 130 if result["status"] == "cancelled" else 1
