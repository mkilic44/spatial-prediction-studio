"""Run a test command in CI and, if it fails, repeat the end of its output as workflow annotations.

Job logs of GitHub Actions are stored outside api.github.com and are not readable from every
environment; annotations are, through the check-runs API. The command's output is still printed
to the job log as it runs.

Usage: python annotate_failure.py <title> <command> [arguments...]
The title is one argument. No '--' separator is used, because Windows PowerShell 5.1 may drop it.
Standard library only, so it runs with any of the installed runtimes.
"""
import subprocess
import sys

CHUNK = 4000          # characters per annotation
MAX_ANNOTATIONS = 9   # GitHub keeps 10 error annotations per step; one is used by the runner


def escape_data(text):
    return text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def escape_property(text):
    return escape_data(text).replace(':', '%3A').replace(',', '%2C')


def main(argv):
    if len(argv) < 2:
        print('usage: annotate_failure.py <title> <command> [arguments...]', file=sys.stderr)
        return 2
    title, command = argv[0], argv[1:]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    lines = []
    for raw in process.stdout:
        line = raw.decode('utf-8', errors='replace')
        sys.stdout.write(line)
        sys.stdout.flush()
        lines.append(line)
    code = process.wait()
    if code != 0:
        tail = ''.join(lines)[-CHUNK * MAX_ANNOTATIONS:]
        chunks = [tail[start:start + CHUNK] for start in range(0, len(tail), CHUNK)] or ['(no output)']
        for number, chunk in enumerate(chunks, 1):
            label = '{} failed with exit code {} ({}/{})'.format(title, code, number, len(chunks))
            print('::error title={}::{}'.format(escape_property(label), escape_data(chunk)), flush=True)
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
