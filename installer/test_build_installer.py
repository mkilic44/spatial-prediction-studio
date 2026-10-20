import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sps_build_installer', ROOT / 'tools/build_installer.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_compile_rejects_unlisted_payload_file_before_compiler(tmp_path):
    payload = tmp_path / 'payload'; payload.mkdir()
    (payload / 'unlisted.txt').write_text('not verified')
    (payload / 'payload-manifest.json').write_text(json.dumps({'components': ['core', 'reference_r'], 'files': {},
        'version': '0.5.0', 'final_binding': {'backend_members_identical': True}}))
    with pytest.raises(ValueError, match='Unlisted or missing payload'):
        builder.compile_installer(SimpleNamespace(payload=payload, output=tmp_path / 'out', iscc=tmp_path / 'missing.exe'))


def test_bind_rejects_private_backend_file_absent_from_addin(tmp_path):
    payload = tmp_path / 'payload'; payload.mkdir()
    (payload / 'payload-manifest.json').write_text(json.dumps({'files': {'backend/orphan.py': {'component': 'core'}}}))
    source = tmp_path / 'candidate.esriAddIn'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('Install/build-manifest.json', json.dumps({'members': {}}))
    with pytest.raises(ValueError, match='Backend member set'):
        builder.bind_addin(SimpleNamespace(payload=payload, addin=source, expect_sha256=builder.sha(source)))


def test_bind_rejects_duplicate_archive_members(tmp_path):
    payload = tmp_path / 'payload'; payload.mkdir()
    (payload / 'payload-manifest.json').write_text(json.dumps({'files': {}}))
    source = tmp_path / 'candidate.esriAddIn'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('Install/build-manifest.json', json.dumps({'members': {}}))
        with pytest.warns(UserWarning):
            archive.writestr('Install/build-manifest.json', json.dumps({'members': {}}))
    with pytest.raises(ValueError, match='Duplicate add-in'):
        builder.bind_addin(SimpleNamespace(payload=payload, addin=source, expect_sha256=builder.sha(source)))
