"""Collect installed distribution notices without modifying third-party text."""
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parents[1]

def main():
    destination=ROOT/'licenses';destination.mkdir(exist_ok=True)
    inventory=[]
    for dist in sorted(metadata.distributions(),key=lambda d:d.metadata['Name'].lower()):
        name=dist.metadata['Name'];version=dist.version
        target=destination/(name+'-'+version);target.mkdir(exist_ok=True)
        notices=[]
        for f in dist.files or []:
            base=Path(f).name.lower()
            if not any(base.startswith(p) for p in ('license','licence','copying','notice','copyright')):continue
            source=Path(dist.locate_file(f))
            if not source.is_file():continue
            # Record a unique path, preserving the original content verbatim.
            relative=Path(*[p for p in Path(f).parts if p not in ('..','.')])
            out=target/relative;out.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,out)
            notices.append({'path':out.relative_to(destination).as_posix(),'sha256':hashlib.sha256(out.read_bytes()).hexdigest()})
        (target/'DISTRIBUTION-METADATA.txt').write_text(dist.read_text('METADATA') or '',encoding='utf-8')
        if name.lower()=='xgboost' and version=='3.0.2':
            source=target/'LICENSE'
            expected='b96f9cead5f4ea4c0a217bd9d879427443a353598fb72694f2d8c4428b53a6af'
            if source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest()==expected:
                notices.append({'path':source.relative_to(destination).as_posix(),'sha256':expected,
                                'source':'https://raw.githubusercontent.com/dmlc/xgboost/v3.0.2/LICENSE'})
        if name.lower()=='catboost' and version=='1.2.8':
            source=target/'LICENSE';expected='8362a19526dda0e75fab0466a4ce4c426f9e84f833bbf18406345643cad00e9d'
            if source.is_file() and hashlib.sha256(source.read_bytes()).hexdigest()==expected:
                notices.append({'path':source.relative_to(destination).as_posix(),'sha256':expected,
                                'source':'https://raw.githubusercontent.com/catboost/catboost/v1.2.8/LICENSE'})
        inventory.append({'name':name,'version':version,'license_expression':dist.metadata.get('License-Expression'),
                          'license_field':dist.metadata.get('License'),'classifiers':[c for c in dist.metadata.get_all('Classifier',[]) if c.startswith('License')],
                          'notices':notices,'status':'notice_files_copied' if notices else 'metadata_only_review_required'})
    (destination/'inventory.json').write_text(json.dumps(inventory,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'distributions':len(inventory),'notice_files':sum(len(r['notices']) for r in inventory),
                      'metadata_only':[r['name'] for r in inventory if not r['notices']]}))

if __name__=='__main__':main()
