"""Start gate of backend/worker.py.

The add-in sends a one-time token on stdin after it has put the worker into its Windows Job Object.
.NET Framework writes a UTF-8 byte order mark before the token when the console code page is UTF-8
(65001), so the gate must accept it. Without arguments the worker stops with the usage code 2
after the gate; a wrong token stops it with 70 before anything is imported.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest

WORKER = Path(__file__).resolve().parents[1] / 'backend' / 'worker.py'
TOKEN = '0123456789abcdef0123456789abcdef'


def run_gate(stdin_bytes, token=TOKEN):
    env = dict(os.environ, SPS_START_GATE=token)
    return subprocess.run([sys.executable, '-u', '-s', str(WORKER)], input=stdin_bytes, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120).returncode


@pytest.mark.parametrize('line', [TOKEN.encode() + b'\r\n', TOKEN.encode() + b'\n', b'\xef\xbb\xbf' + TOKEN.encode() + b'\r\n'],
                         ids=['crlf', 'lf', 'utf8-bom'])
def test_gate_accepts_the_token_with_or_without_byte_order_mark(line):
    assert run_gate(line) == 2


@pytest.mark.parametrize('line', [b'', b'wrong\r\n', b'\xef\xbb\xbf\r\n', b'\xff\xfe' + TOKEN.encode('utf-16-le') + b'\r\n'],
                         ids=['closed', 'wrong', 'bom-only', 'utf16'])
def test_gate_rejects_anything_else(line):
    assert run_gate(line) == 70


def test_empty_token_is_rejected():
    assert run_gate(b'\r\n', token='') == 70
