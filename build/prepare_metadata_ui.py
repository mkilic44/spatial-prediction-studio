"""Prepare synthetic fixtures for the native metadata regression acceptance."""
from pathlib import Path
import copy
import csv
import hashlib
import json
import rasterio

root = Path(__file__).resolve().parents[1]
out = root / 'build' / 'arcmap-evidence'
project = json.loads((root / 'examples-d' / 'tabpfn.sps.json').read_text(encoding='utf-8'))
project['science']['model'] = {'id': 'ridge', 'parameters': {'alpha': 1.0}}
with open(project['csv_path'], encoding='utf-8-sig', newline='') as stream:
    first = next(csv.DictReader(stream))
project['samples']['excluded'] = [{'id': first['sample_id'], 'reason': 'Sentetik kayıt koruma sınaması; gerçek ölçüm hatası değildir.'}]
for cov in project['covariates']:
    path = Path(cov['path'])
    with rasterio.open(path) as ds:
        description = ds.descriptions[cov['band'] - 1]
        resolution = list(ds.res)
    cov.update({'resampling': 'bilinear', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'band_description': description, 'time_representation': '2020 sentetik dönem',
                'original_resolution': resolution,
                'provenance': {'source': 'local synthetic fixture', 'band': cov['band'], 'observations_uploaded': False},
                'target_derived': False})
for name, value in [('d-metadata-input.sps.json', project), ('d-metadata-blocked.sps.json', copy.deepcopy(project))]:
    if 'blocked' in name:
        value['covariates'][0]['target_derived'] = True
    path = out / name
    if path.exists():
        raise FileExistsError(path)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    print(path)
