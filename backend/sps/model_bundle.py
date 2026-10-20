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
        raise JobError('MODEL_PACKAGE_HASH', 'Model paketinin dosya özeti uyuşmuyor.')
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise JobError('MODEL_PACKAGE_MEMBERS', 'Model paketinde yinelenen dosya adı var.')
        for name in names:
            parsed = PurePosixPath(name)
            if parsed.is_absolute() or '..' in parsed.parts or '\\' in name or ':' in name or name.endswith('/'):
                raise JobError('MODEL_PACKAGE_PATH', 'Model paketinde geçersiz dosya yolu var.')
        try: manifest = json.loads(archive.read('manifest.json'))
        except (KeyError, ValueError) as exc:
            raise JobError('MODEL_PACKAGE_MANIFEST', 'Model paketinin içeriği doğrulanamadı.') from exc
        if manifest.get('schema_version') != 1 or set(manifest.get('members', {})) != set(names)-{'manifest.json'}:
            raise JobError('MODEL_PACKAGE_MANIFEST', 'Model paketi dosya listesi uyuşmuyor.')
        for name, expected in manifest['members'].items():
            with archive.open(name) as stream: actual = _hash_stream(stream, check_cancel)
            if actual != expected:
                raise JobError('MODEL_PACKAGE_MEMBER_HASH', 'Model paketi dosya özeti uyuşmuyor: '+name)
        if manifest.get('model_path') not in manifest['members'] or manifest.get('metadata_path') not in manifest['members']:
            raise JobError('MODEL_PACKAGE_MANIFEST', 'Model veya üst veri dosyası yok.')
        metadata = json.loads(archive.read(manifest['metadata_path']))
        model_hash = manifest['members'][manifest['model_path']]
        if (manifest.get('model_sha256') != model_hash or metadata.get('sha256') != model_hash
                or metadata.get('run_id') != manifest.get('run_id') or metadata.get('schema_version') != 1):
            raise JobError('MODEL_PACKAGE_PROVENANCE', 'Model paketi ile modelin üst veri kaydı uyuşmuyor.')
    return {'status': 'verified', 'sha256': digest, 'members': len(names), 'manifest': manifest,
            'trust_scope': 'integrity only; no model deserialization performed'}


def create_model_package(output, model_files, settings, record, study_context, *, check_cancel):
    output = Path(output)
    destination = output / study_context['model_artifact']['package_path']
    partial = destination.with_name(destination.name+'.partial')
    if destination.exists() or partial.exists():
        raise JobError('MODEL_PACKAGE_EXISTS', 'Mevcut model paketinin üzerine yazılmaz.')
    model_path, metadata_path = Path(model_files['path']), Path(model_files['metadata_path'])
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    with model_path.open('rb') as stream: model_digest = _hash_stream(stream, check_cancel)
    if model_digest != model_files['sha256'] or metadata.get('sha256') != model_digest or metadata.get('run_id') != settings['run_id']:
        raise JobError('MODEL_PACKAGE_SOURCE', 'Kaydedilecek modelin kaynağı doğrulanamadı.')
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
            if not source.is_file(): raise JobError('MODEL_PACKAGE_WEIGHT', 'TabPFN ağırlığı veya lisans kaydı eksik.')
            sources['models/tabpfn-v2/'+name] = source
    readme = '''# Kaydedilmiş model paketi

Paket, bu çalışmanın eğitilmiş modelini içerir. Model nesnesi ön işlemeyi, hedef dönüşümünü, değişken seçimini ve varsa belirsizlik kalibrasyonunu birlikte saklar.
`settings.json` kullanıcı ayarlarını, `final_record.json` gerçekleşen eğitimi, `provenance.json` kaynak ve sürüm kayıtlarını içerir.
`environment/` dosyaları yazılım sürümlerini sabitler. `backend/sps/` bu paketi oluşturan çekirdektir. Python yorumlayıcısı ve Esri kitaplıkları gömülmez.
Raster girdileri pakete kopyalanmaz. Kaydedilen değişken sırası, bantlar ve birimler korunmalıdır. Yeni alandaki tahmin başarısı bu paketle kanıtlanmış değildir.

## Yeniden kullanım

Model yalnız kaynağı bilinen bu yerel çalışmadan yüklenmelidir. Bir dosya özetinin eşleşmesi, bilinmeyen dosyanın güvenilir olduğunu kanıtlamaz.
`sps.model_bundle.inspect_model_package` paket dosyalarının özetlerini modeli çalıştırmadan denetler.
Güvenilir paketin model dosyası ve yanındaki `.meta.json` birlikte tutulur. `sps.evaluation.load_trusted_model`, çalışma kimliğini, beklenen model özetini, değişken şemasını ve ortam sürümlerini denetler.
Şema ve sürüm bilgileri `manifest.json` ile modelin üst veri dosyasındadır. Model yüklemek yeniden eğitim yapmaz. Rastgele bir `joblib` dosyası açılmamalıdır.
TabPFN kullanılmışsa yerel ağırlık, sürüm kaydı ve lisansı `models/tabpfn-v2/` içindedir. `SPS_MODELS_DIR`, açılmış paketin `models/` klasörünü göstermelidir.
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
                if before != after: raise JobError('MODEL_PACKAGE_SOURCE_CHANGED', 'Model paketlenirken kaynak değişti.')
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
