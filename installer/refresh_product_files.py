"""Refresh small, frozen product files in an uninstalled build payload."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload',type=Path,required=True)
    args=parser.parse_args();payload=args.payload.resolve()
    if not payload.is_relative_to(ROOT/'installer') or (payload/'deployment-record.json').exists():
        raise ValueError('Only an uninstalled, project-owned installer payload can be refreshed')
    path=payload/'payload-manifest.json';manifest=json.loads(path.read_text(encoding='utf-8'))
    if manifest.get('final_binding'):raise ValueError('Final bound payload is immutable; prepare another payload')
    updates=[]
    sources=[]
    source_manifest=json.loads((ROOT/'installer/corresponding-source/source-manifest.json').read_text(encoding='utf-8'))
    if source_manifest['status']!='complete':raise ValueError('Source inventory is incomplete')
    for record in source_manifest['components']:
        for archive in record.get('sources',[]):
            if sha(ROOT/'installer/corresponding-source'/archive['path'])!=archive['sha256']:
                raise ValueError('Source archive readback failed: '+archive['path'])
    for tree,component in [('mcp-server','mcp'),('docs','core')]:
        for source in sorted((ROOT/tree).rglob('*')):
            if source.is_file() and '__pycache__' not in source.parts and source.suffix!='.pyc':
                sources.append((source,source.relative_to(ROOT).as_posix(),component))
    for source in sorted((ROOT/'installer/corresponding-source').rglob('*')):
        if source.is_file():
            sources.append((source,'corresponding-source/'+source.relative_to(ROOT/'installer/corresponding-source').as_posix(),'core'))
    for name in ('deploy.py','verify_runtime.py','verify_r.R','THIRD_PARTY_NOTICE.txt','native-library-probe.json'):
        sources.append((ROOT/'installer'/name,'installer/'+name,'core'))
    sources.extend((ROOT/name,name,'core') for name in ('README.md','docs/THIRD_PARTY_LICENSES.md'))
    sources.append((ROOT/'requirements-mcp-lock.txt','requirements-mcp-lock.txt','mcp'))
    sources.append((ROOT/'runtime-d/Lib/site-packages/sklearn/.libs/msvcp140.dll','runtime-d/Scripts/msvcp140.dll','core'))
    for source in (ROOT/'reference-r/Library/bin').glob('*.dll'):
        if source.name.lower().startswith(('msvcp140','vcruntime140','concrt140','vcomp140')):
            for relative in ('lib/R/bin','lib/R/bin/x64'):
                sources.append((source,'reference-r/'+relative+'/'+source.name,'reference_r'))
    for source,name,component in sources:
        dest=(payload/name).resolve()
        if not dest.is_relative_to(payload):raise ValueError('Unsafe build destination')
        old=manifest['files'].get(name)
        if old and sha(dest)!=old['sha256']:raise ValueError('Prepared file unexpectedly changed: '+name)
        digest=sha(source)
        if not old or digest!=old['sha256']:
            dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
            if sha(dest)!=digest:raise IOError('Copied file readback failed: '+name)
            manifest['files'][name]={'sha256':digest,'bytes':dest.stat().st_size,'component':component}
            updates.append(name)
    manifest['build_source_root']=str(ROOT)
    manifest['product_refresh']={'files':updates,'backend_and_dependencies_changed':False}
    path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':'refreshed','files':updates,'manifest_sha256':sha(path)},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
