"""Record the installed isolated R runtime and retain original license texts."""
import csv,hashlib,json,shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
runtime=ROOT/'reference-r'
packages=[json.loads(p.read_text(encoding='utf-8')) for p in sorted((runtime/'conda-meta').glob('*.json'))]
lines=['# Project-local R 4.4.3, win-64. No global PATH change.','@EXPLICIT']
for p in packages:
    url=p['url']
    if not url.startswith('https://conda.anaconda.org/conda-forge/'):raise ValueError('Unexpected package channel')
    lines.append(url+'#'+p['md5'])
(ROOT/'reference-r-conda-lock.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
shutil.copyfile(ROOT/'build/reference-cast-packages-plan.csv',ROOT/'reference-r-packages.csv')
target=ROOT/'licenses/R-CAST';target.mkdir(exist_ok=True)
notices=[]
for library in (runtime/'library-extra',runtime/'lib/R/library'):
    for pkg in sorted(library.iterdir()):
        if not pkg.is_dir():continue
        for source in sorted(pkg.iterdir()):
            if not source.is_file() or source.name not in ('DESCRIPTION','LICENSE','LICENCE','COPYING','COPYRIGHT','NOTICE'):continue
            dest=target/pkg.name/source.name;dest.parent.mkdir(exist_ok=True);shutil.copyfile(source,dest)
            notices.append({'path':dest.relative_to(ROOT).as_posix(),'sha256':hashlib.sha256(dest.read_bytes()).hexdigest()})
for source in (runtime/'lib/R/share/licenses').iterdir():
    if source.is_file() and source.name!='license.db':
        dest=target/'licenses'/source.name;dest.parent.mkdir(exist_ok=True);shutil.copyfile(source,dest)
        notices.append({'path':dest.relative_to(ROOT).as_posix(),'sha256':hashlib.sha256(dest.read_bytes()).hexdigest()})
(target/'inventory.json').write_text(json.dumps({'R':'4.4.3','CAST':'1.0.4','conda_packages':len(packages),'notices':notices},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'conda_packages':len(packages),'R_notices':len(notices)}))
