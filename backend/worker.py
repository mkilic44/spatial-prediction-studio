"""Executable entry point for the isolated 64-bit worker."""
import os
import sys

# The native owner assigns its Windows Job Object before releasing this gate.
# Consume the token before importing anything able to start child processes.
start_token = os.environ.pop('SPS_START_GATE', None)
if start_token is not None:
    if not start_token or sys.stdin.readline().rstrip('\r\n') != start_token:
        raise SystemExit(70)
    del start_token

os.environ["PROJ_NETWORK"] = "OFF"
for key in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
    os.environ[key] = '1'

from sps.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
