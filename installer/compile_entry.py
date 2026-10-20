"""Run the frozen builder with full ISCC logging, preserving all source checks."""
from pathlib import Path
import runpy
import subprocess
import sys

original_run = subprocess.run


def verbose_compiler(command, *args, **kwargs):
    if isinstance(command, list) and Path(command[0]).name.lower() == 'iscc.exe':
        command = [value for value in command if value != '/Qp']
    return original_run(command, *args, **kwargs)


if __name__ == '__main__':
    subprocess.run = verbose_compiler
    sys.argv[0] = str(Path(__file__).resolve().parents[1] / 'tools/build_installer.py')
    runpy.run_path(sys.argv[0], run_name='__main__')
