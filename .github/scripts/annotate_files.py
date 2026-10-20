"""Repeat small generated files as workflow annotations: a gzip tar, base64 encoded, in chunks.

Job logs and artifacts are stored outside api.github.com and are not readable from every
environment; annotations are, through the check-runs API. To restore the files, join the chunks in
order, base64-decode, check the SHA-256 in the title and extract the tar.

Usage: python annotate_files.py <title> <path> [<path> ...]   (missing paths are skipped)
"""
import base64
import hashlib
import io
from pathlib import Path
import sys
import tarfile

CHUNK = 4000          # characters per annotation
MAX_ANNOTATIONS = 10  # GitHub keeps 10 notices per step


def escape_property(text):
    return text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A').replace(':', '%3A').replace(',', '%2C')


def main(argv):
    if len(argv) < 2:
        print('usage: annotate_files.py <title> <path> [<path> ...]', file=sys.stderr)
        return 2
    title, paths = argv[0], [Path(item) for item in argv[1:]]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz', format=tarfile.PAX_FORMAT) as archive:
        for path in paths:
            if path.exists():
                archive.add(path, arcname=path.as_posix())
    payload = buffer.getvalue()
    text = base64.b64encode(payload).decode('ascii')
    chunks = [text[start:start + CHUNK] for start in range(0, len(text), CHUNK)]
    if len(chunks) > MAX_ANNOTATIONS:
        print('::warning title=%s::%d chunks exceed the annotation limit; download the artifact instead.'
              % (escape_property(title), len(chunks)))
        return 0
    digest = hashlib.sha256(payload).hexdigest()
    for number, chunk in enumerate(chunks, 1):
        label = '%s %d/%d sha256 %s' % (title, number, len(chunks), digest)
        print('::notice title=%s::%s' % (escape_property(label), chunk), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
