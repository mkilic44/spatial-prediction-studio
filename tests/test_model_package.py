import hashlib
import json
from pathlib import Path
import sys
import zipfile

import numpy as np
import pytest
from sklearn.linear_model import Ridge

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'backend'))
from sps.contract import Cancelled, JobError
from sps.evaluation import load_trusted_model, save_trusted_model
from sps.model_bundle import create_model_package, inspect_model_package


def fixture(tmp_path):
    X = np.arange(36, dtype=float).reshape(12, 3)
    model = Ridge(alpha=2).fit(X, 3+X[:, 0]*.6)
    schema = [{'id': str(i), 'unit': '1', 'band': i+1, 'path': 'local.tif'} for i in range(3)]
    files = save_trusted_model(model, tmp_path/'model.joblib', 'saved_run', schema)
    settings = {'run_id': 'saved_run', 'covariates': schema}
    record = {'model': {'id': 'ridge', 'parameters': {'alpha': 2}}, 'training_ids': list(map(str, range(12)))}
    context = {'model_artifact': {'package_path': 'model_package.spsmodel.zip'}}
    return X, model, files, settings, record, context


def test_real_saved_package_readback_and_restored_predictions(tmp_path, monkeypatch):
    X, model, files, settings, record, context = fixture(tmp_path)
    path = create_model_package(tmp_path, files, settings, record, context, check_cancel=lambda: None)
    checked = inspect_model_package(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    assert checked['status'] == 'verified'
    dest = tmp_path/'trusted_restore'; dest.mkdir()
    with zipfile.ZipFile(path) as archive:
        assert 'backend/sps/modeling.py' in archive.namelist()
        assert 'environment/requirements-d-lock.txt' in archive.namelist()
        for name in ('model.joblib', 'model.joblib.meta.json'):
            (dest/name).write_bytes(archive.read(name))
        assert json.loads(archive.read('settings.json')) == settings
    restored = load_trusted_model(dest/'model.joblib', 'saved_run', settings['covariates'], files['sha256'])
    np.testing.assert_array_equal(model.predict(X), restored.predict(X))
    import joblib
    def forbidden(*a, **kw): raise AssertionError('Inspection must never deserialize')
    monkeypatch.setattr(joblib, 'load', forbidden)
    assert inspect_model_package(path)['status'] == 'verified'


def test_source_corruption_is_rejected_before_publishing(tmp_path):
    _, _, files, settings, record, context = fixture(tmp_path)
    Path(files['path']).write_bytes(b'changed')
    with pytest.raises(JobError, match='kaynağı'):
        create_model_package(tmp_path, files, settings, record, context, check_cancel=lambda: None)
    assert not list(tmp_path.glob('*.zip*'))


def test_archive_member_corruption_and_external_digest_are_rejected(tmp_path):
    _, _, files, settings, record, context = fixture(tmp_path)
    path = create_model_package(tmp_path, files, settings, record, context, check_cancel=lambda: None)
    with pytest.raises(JobError) as error:
        inspect_model_package(path, expected_sha256='0'*64)
    assert error.value.code == 'MODEL_PACKAGE_HASH'
    changed = tmp_path/'changed.zip'
    with zipfile.ZipFile(path) as original, zipfile.ZipFile(changed, 'w') as dst:
        for name in original.namelist():
            dst.writestr(name, b'changed' if name == 'settings.json' else original.read(name))
    with pytest.raises(JobError) as error: inspect_model_package(changed)
    assert error.value.code == 'MODEL_PACKAGE_MEMBER_HASH'


def test_cancelled_package_preserves_model_and_removes_partial_archive(tmp_path):
    _, _, files, settings, record, context = fixture(tmp_path)
    count = 0
    def cancel():
        nonlocal count
        count += 1
        if count == 8: raise Cancelled()
    with pytest.raises(Cancelled):
        create_model_package(tmp_path, files, settings, record, context, check_cancel=cancel)
    assert not list(tmp_path.glob('*.zip*'))
    assert hashlib.sha256(Path(files['path']).read_bytes()).hexdigest() == files['sha256']


def test_existing_saved_model_package_is_never_overwritten(tmp_path):
    _, _, files, settings, record, context = fixture(tmp_path)
    path = create_model_package(tmp_path, files, settings, record, context, check_cancel=lambda: None)
    before = path.read_bytes()
    with pytest.raises(JobError) as error:
        create_model_package(tmp_path, files, settings, record, context, check_cancel=lambda: None)
    assert error.value.code == 'MODEL_PACKAGE_EXISTS'
    assert path.read_bytes() == before


def test_consistent_member_hashes_do_not_hide_wrong_model_provenance(tmp_path):
    _, _, files, settings, record, context = fixture(tmp_path)
    path = create_model_package(tmp_path, files, settings, record, context, check_cancel=lambda: None)
    changed = tmp_path/'wrong-provenance.zip'
    with zipfile.ZipFile(path) as src:
        members = {n:src.read(n) for n in src.namelist()}
    manifest=json.loads(members['manifest.json'])
    metadata=json.loads(members[manifest['metadata_path']]);metadata['run_id']='another_run'
    members[manifest['metadata_path']]=json.dumps(metadata).encode()
    manifest['members'][manifest['metadata_path']]=hashlib.sha256(members[manifest['metadata_path']]).hexdigest()
    members['manifest.json']=json.dumps(manifest).encode()
    with zipfile.ZipFile(changed,'w') as dst:
        for name,value in members.items():dst.writestr(name,value)
    with pytest.raises(JobError) as error:inspect_model_package(changed)
    assert error.value.code=='MODEL_PACKAGE_PROVENANCE'
