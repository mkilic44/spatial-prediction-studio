"""Write and inspect an integrity-checked local model archive without loading it."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import zipfile

from .contract import JobError


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False).encode('utf-8')


def _hash_stream(stream, check_cancel):
    digest = hashlib.sha256()
    while True:
        check_cancel()
        block = stream.read(1024 * 1024)
        if not block: return digest.hexdigest()
        digest.update(block)


def inspect_model_package(path, *, expected_sha256=None, check_cancel=lambda: None):
    """Validate bytes and safe relative members. A matching digest does not establish trust."""
    path = Path(path)
    with path.open('rb') as stream: digest = _hash_stream(stream, check_cancel)
    if expected_sha256 is not None and digest != expected_sha256:
        raise JobError('MODEL_PACKAGE_HASH', 'The model package file digest does not match.')
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise JobError('MODEL_PACKAGE_MEMBERS', 'The model package contains a duplicate file name.')
        for name in names:
            parsed = PurePosixPath(name)
            if parsed.is_absolute() or '..' in parsed.parts or '\\' in name or ':' in name or name.endswith('/'):
                raise JobError('MODEL_PACKAGE_PATH', 'The model package contains an invalid file path.')
        try: manifest = json.loads(archive.read('manifest.json'))
        except (KeyError, ValueError) as exc:
            raise JobError('MODEL_PACKAGE_MANIFEST', 'The model package contents could not be verified.') from exc
        if manifest.get('schema_version') != 1 or set(manifest.get('members', {})) != set(names)-{'manifest.json'}:
            raise JobError('MODEL_PACKAGE_MANIFEST', 'The model package file list does not match.')
        for name, expected in manifest['members'].items():
            with archive.open(name) as stream: actual = _hash_stream(stream, check_cancel)
            if actual != expected:
                raise JobError('MODEL_PACKAGE_MEMBER_HASH', 'Model package member digest does not match: '+name)
        if manifest.get('model_path') not in manifest['members'] or manifest.get('metadata_path') not in manifest['members']:
            raise JobError('MODEL_PACKAGE_MANIFEST', 'The model or metadata file is missing.')
        metadata = json.loads(archive.read(manifest['metadata_path']))
        model_hash = manifest['members'][manifest['model_path']]
        if (manifest.get('model_sha256') != model_hash or metadata.get('sha256') != model_hash
                or metadata.get('run_id') != manifest.get('run_id') or metadata.get('schema_version') != 1):
            raise JobError('MODEL_PACKAGE_PROVENANCE', 'The model package and the model metadata record do not match.')
    return {'status': 'verified', 'sha256': digest, 'members': len(names), 'manifest': manifest,
            'trust_scope': 'integrity only; no model deserialization performed'}


def create_model_package(output, model_files, settings, record, study_context, *, check_cancel):
    output = Path(output)
    destination = output / study_context['model_artifact']['package_path']
    partial = destination.with_name(destination.name+'.partial')
    if destination.exists() or partial.exists():
        raise JobError('MODEL_PACKAGE_EXISTS', 'An existing model package is not overwritten.')
    model_path, metadata_path = Path(model_files['path']), Path(model_files['metadata_path'])
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    with model_path.open('rb') as stream: model_digest = _hash_stream(stream, check_cancel)
    if model_digest != model_files['sha256'] or metadata.get('sha256') != model_digest or metadata.get('run_id') != settings['run_id']:
        raise JobError('MODEL_PACKAGE_SOURCE', 'The source of the model to be saved could not be verified.')
    sources = {model_path.name: model_path, metadata_path.name: metadata_path}
    for name in ('study_summary.json', 'final_record.json', 'selected_features.csv', 'hyperparameters.csv'):
        if (output/name).is_file(): sources['study/'+name] = output/name
    core = Path(__file__).parent
    for path in core.iterdir():
        if path.is_file() and path.suffix in ('.py', '.R', '.json'):
            sources['backend/sps/'+path.name] = path
    root_candidates = [Path(sys.prefix).parent, core.parents[1]]
    project = next((p for p in root_candidates if (p/'requirements-d-lock.txt').is_file()), None)
    if project:
        for name in ('requirements-lock.txt', 'requirements-d-lock.txt', 'reference-r-conda-lock.txt', 'reference-r-packages.csv'):
            if (project/name).is_file(): sources['environment/'+name] = project/name
    if record.get('model', {}).get('id') == 'tabpfn':
        from .tabpfn_d import verify_weights
        weight = Path(verify_weights()['path'])
        for name in (weight.name, 'LICENSE.txt', 'weights-manifest.json'):
            source = weight.parent/name
            if not source.is_file(): raise JobError('MODEL_PACKAGE_WEIGHT', 'The TabPFN weights or license record is missing.')
            sources['models/tabpfn-v2/'+name] = source
    readme = '''# Saved model package

The package contains the trained model of this run. The model object stores preprocessing, target transformation, variable selection and, if used, uncertainty calibration together.
`settings.json` contains the user settings, `final_record.json` the training as performed, and `provenance.json` the source and version records.
The `environment/` files pin the software versions. `backend/sps/` is the core that created this package. The Python interpreter and Esri libraries are not embedded.
Raster inputs are not copied into the package. The recorded variable order, bands and units must be preserved. This package does not prove prediction performance in a new area.

## Reuse

Load the model only from this local run, whose source is known. A matching file digest does not prove that an unknown file is trustworthy.
`sps.model_bundle.inspect_model_package` checks the digests of the package files without running the model.
Keep the model file of a trusted package together with the `.meta.json` file next to it. `sps.evaluation.load_trusted_model` checks the run ID, the expected model digest, the variable schema and the environment versions.
Schema and version information is in `manifest.json` and in the model metadata file. Loading a model does not retrain it. Do not open an arbitrary `joblib` file.
If TabPFN was used, the local weights, version record and license are in `models/tabpfn-v2/`. `SPS_MODELS_DIR` must point to the `models/` folder of the extracted package.
Built with PriorLabs-TabPFN if the model is TabPFN.
'''
    generated = {'README.md': readme.encode('utf-8'), 'settings.json': _json(settings),
                 'final_record.json': _json(record), 'provenance.json': _json(study_context)}
    hashes = {}
    try:
        with zipfile.ZipFile(partial, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in sorted(generated.items()):
                check_cancel(); archive.writestr(name, value); hashes[name] = hashlib.sha256(value).hexdigest()
            for name, source in sorted(sources.items()):
                with source.open('rb') as stream: before = _hash_stream(stream, check_cancel)
                with archive.open(name, 'w') as dst, source.open('rb') as src:
                    while True:
                        check_cancel(); chunk = src.read(1024*1024)
                        if not chunk: break
                        dst.write(chunk)
                with source.open('rb') as stream: after = _hash_stream(stream, check_cancel)
                if before != after: raise JobError('MODEL_PACKAGE_SOURCE_CHANGED', 'A source changed while the model was being packaged.')
                hashes[name] = before
            manifest = {'schema_version': 1, 'run_id': settings['run_id'],
                        'model_id': record.get('model', {}).get('id'),
                        'model_path': model_path.name, 'metadata_path': metadata_path.name,
                        'model_sha256': model_digest, 'members': hashes,
                        'trust_scope': 'export of the current verified local run; hashes are not a trust authority'}
            archive.writestr('manifest.json', _json(manifest))
        inspect_model_package(partial, check_cancel=check_cancel)
        check_cancel(); partial.rename(destination)
        return destination
    except Exception:
        partial.unlink(missing_ok=True)
        raise
