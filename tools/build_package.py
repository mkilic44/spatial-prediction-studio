"""Build a source-accounted Esri add-in; never bundle Esri runtime assemblies."""
import hashlib
import json
import struct
import zlib
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]

def digest(data):
    return hashlib.sha256(data).hexdigest()

def icon_png():
    def chunk(kind, value):
        return struct.pack('>I', len(value)) + kind + value + struct.pack('>I', zlib.crc32(kind + value))
    rows = bytearray()
    for y in range(32):
        rows.append(0)
        for x in range(32):
            grid = x in (10, 21) or y in (10, 21)
            dot = any((x-cx)**2+(y-cy)**2 <= 6 for cx,cy in [(6,7),(16,24),(26,14)])
            rows.extend((245,180,65,255) if dot else (120,180,166,255) if grid else (28,75,68,255))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR',struct.pack('>IIBBBBB',32,32,8,6,0,0,0)) + chunk(b'IDAT',zlib.compress(bytes(rows))) + chunk(b'IEND',b'')

def main():
    dll = ROOT / 'native/SpatialPredictionStudio/bin/x86/Release/SpatialPredictionStudio.dll'
    if not dll.is_file():
        dll = ROOT / 'native/SpatialPredictionStudio/bin/Release/SpatialPredictionStudio.dll'
    if not dll.is_file():
        raise FileNotFoundError('Build native/SpatialPredictionStudio.sln first')
    members = {'Config.xml':(ROOT/'Config.xml').read_bytes(), 'Images/studio.png':icon_png(),
               'Install/SpatialPredictionStudio.dll':dll.read_bytes()}
    runtime = {'python_executable':str(ROOT/'runtime-d/Scripts/python.exe'),
               'models_dir':str(ROOT/'models'),
               'worker_path':'backend/worker.py','examples_dir':'examples'}
    members['Install/runtime.json'] = json.dumps(runtime,ensure_ascii=False,indent=2).encode('utf-8')
    for path in sorted((ROOT/'backend').rglob('*.py')):
        members['Install/'+path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for path in sorted((ROOT/'backend').rglob('*.json')):
        members['Install/'+path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for path in sorted((ROOT/'backend').rglob('*.R')):
        members['Install/'+path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for name in ('samples.geojson','covariates.tif'):
        members['Install/examples/'+name] = (ROOT/'examples'/name).read_bytes()
    for path in sorted((ROOT/'examples-d').glob('*')):
        if path.is_file():members['Install/examples-d/'+path.name]=path.read_bytes()
    for path in sorted((ROOT/'licenses').rglob('*')):
        if path.is_file():members['Install/'+path.relative_to(ROOT).as_posix()]=path.read_bytes()
    for path in sorted((ROOT/'licenses-d').rglob('*')):
        if path.is_file():members['Install/'+path.relative_to(ROOT).as_posix()]=path.read_bytes()
    if (ROOT/'docs/LISANSLAR_D.md').is_file():members['Install/docs/LISANSLAR_D.md']=(ROOT/'docs/LISANSLAR_D.md').read_bytes()
    if (ROOT/'docs/RAPOR_VE_MODEL.md').is_file():members['Install/docs/RAPOR_VE_MODEL.md']=(ROOT/'docs/RAPOR_VE_MODEL.md').read_bytes()
    for name in ('README.md','YONTEM_KILAVUZU.md','LISANSLAR.md'):
        if (ROOT/name).is_file():members['Install/docs/'+name]=(ROOT/name).read_bytes()
    sources = {}
    for folder, extension in [('native','*.cs'),('native','*.csproj'),('native','*.sln'),('backend','*.py'),('backend','*.json'),('backend','*.R'),('tools','*.R'),('tools','*.py'),('tools','*.ps1'),('tests','*.py')]:
        for path in sorted((ROOT/folder).rglob(extension)):
            if any(p in ('bin','obj','__pycache__') for p in path.parts):
                continue
            sources[path.relative_to(ROOT).as_posix()] = digest(path.read_bytes())
    for name in ('Config.xml','requirements-lock.txt','requirements-d-lock.txt','reference-r-conda-lock.txt','reference-r-packages.csv','models/tabpfn-v2/weights-manifest.json','models/tabpfn-v2/LICENSE.txt'):
        sources[name] = digest((ROOT/name).read_bytes())
    for path in sorted((ROOT/'tests/fixtures').rglob('*')):
        if path.is_file():sources[path.relative_to(ROOT).as_posix()]=digest(path.read_bytes())
    config=ET.fromstring(members['Config.xml'])
    version=config.find('{http://schemas.esri.com/Desktop/AddIns}Version').text
    external_assets={str(p):digest(p.read_bytes()) for p in sorted((ROOT/'models/tabpfn-v2').glob('*')) if p.is_file()}
    manifest={'version':version,'sources':sources,'external_assets':external_assets,'members':{k:digest(v) for k,v in members.items()}}
    members['Install/build-manifest.json']=json.dumps(manifest,ensure_ascii=False,indent=2,sort_keys=True).encode('utf-8')
    destination=ROOT/'dist/SpatialPredictionStudio.esriAddIn'
    destination.parent.mkdir(exist_ok=True)
    temporary=destination.with_suffix('.tmp')
    with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
        for name, value in sorted(members.items()):
            archive.writestr(name,value)
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip():
            raise ValueError('Package CRC check failed')
    temporary.replace(destination)
    print(json.dumps({'package':str(destination),'sha256':digest(destination.read_bytes()),'members':len(members)},ensure_ascii=False))

if __name__ == '__main__':
    main()
