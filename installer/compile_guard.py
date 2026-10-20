"""Run the frozen installer builder with bounded, owned-process resource use."""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit-mib', type=int, default=768)
    parser.add_argument('--timeout', type=int, default=3600)
    parser.add_argument('--iscc', type=Path, default=ROOT / 'installer/build-tools/Inno7/ISCC.exe')
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, '-u', '-X', 'utf8', str(ROOT / 'installer/compile_entry.py'),
               'compile', '--payload', str(args.payload.resolve()), '--output', str(output),
               '--iscc', str(args.iscc.resolve())]
    started = time.monotonic()
    record = {'status': 'running', 'command': command, 'limit_MiB': args.limit_mib,
              'timeout_seconds': args.timeout, 'peak_owned_tree_rss_MiB': 0.0}
    owned = {}
    compiler_log = (output / 'compiler.log').open('wb')
    process = subprocess.Popen(command, cwd=ROOT, stdout=compiler_log, stderr=subprocess.STDOUT,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    root_process = psutil.Process(process.pid)
    owned[process.pid] = root_process
    try:
        while process.poll() is None:
            try:
                for child in root_process.children(recursive=True):
                    owned[child.pid] = child
            except psutil.NoSuchProcess:
                pass
            rss = 0
            for child in list(owned.values()):
                try:
                    if child.is_running():
                        rss += child.memory_info().rss
                except psutil.NoSuchProcess:
                    pass
            record.update(elapsed_seconds=round(time.monotonic() - started, 3),
                          peak_owned_tree_rss_MiB=max(record['peak_owned_tree_rss_MiB'],
                                                      round(rss / 2**20, 3)))
            (output / 'compiler-resource.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
            if rss > args.limit_mib * 2**20:
                raise MemoryError('Owned compiler process tree exceeded the configured memory budget')
            if time.monotonic() - started > args.timeout:
                raise TimeoutError('Compiler exceeded its configured time budget')
            time.sleep(0.5)
        if process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command)
        record['status'] = 'completed'
    except BaseException as error:
        record.update(status='failed', error=repr(error))
        # psutil Process.kill verifies process identity, including its creation time.
        for child in reversed(list(owned.values())):
            try:
                if child.is_running():
                    child.kill()
            except psutil.NoSuchProcess:
                pass
        process.wait(timeout=20)
        raise
    finally:
        compiler_log.close()
        record['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (output / 'compiler-resource.json').write_text(json.dumps(record, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
