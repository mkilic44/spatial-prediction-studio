"""Executable entry point for the isolated 64-bit worker."""
import os
import sys

# The native owner assigns its Windows Job Object before releasing this gate.
# Consume the token before importing anything able to start child processes.
# .NET Framework writes a UTF-8 byte order mark first when the console code page is UTF-8 (65001).
start_token = os.environ.pop('SPS_START_GATE', None)
if start_token is not None:
    gate_line = sys.stdin.buffer.readline()
    if gate_line.startswith(b'\xef\xbb\xbf'):
        gate_line = gate_line[3:]
    if not start_token or gate_line.rstrip(b'\r\n') != start_token.encode('utf-8'):
        raise SystemExit(70)
    del start_token, gate_line

os.environ["PROJ_NETWORK"] = "OFF"
for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
    os.environ[key] = '1'

from sps.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
