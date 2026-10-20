"""Local pinned CAST kNNDM adapter; no coordinate clustering substitute."""
import csv
from copy import deepcopy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import numpy as np
from .contract import JobError

_CACHE={}

def cast_knndm(training,prediction,k,maxp,seed,check_cancel=lambda:None):
    training,prediction=np.asarray(training,dtype=float),np.asarray(prediction,dtype=float)
    if training.ndim!=2 or training.shape[1]!=2 or not 6<=len(training)<=2000 or prediction.ndim!=2 or prediction.shape[1]!=2 or not 5<=len(prediction)<=5000:
        raise JobError('KNNDM_SIZE','kNNDM requires 6-2000 training locations and 5-5000 prediction locations.')
    if not np.isfinite(training).all() or not np.isfinite(prediction).all() or not 2<=k<=len(training)-2 or not 1/k<maxp<1:
        raise JobError('KNNDM_PARAMETER','kNNDM coordinates, number of folds or maximum fold fraction are invalid.')
    check_cancel()
    key=(training.tobytes(),prediction.tobytes(),int(k),float(maxp),int(seed))
    if key in _CACHE:return deepcopy(_CACHE[key])
    root=Path(sys.executable).resolve().parents[2]
    envroot=root/'reference-r';exe=envroot/'lib/R/bin/Rscript.exe'
    if not exe.is_file():raise JobError('CAST_RUNTIME','The pinned local R/CAST runtime environment was not found.')
    env=os.environ.copy();env['R_HOME']=str(envroot/'lib/R')
    env['PATH']=os.pathsep.join(map(str,[envroot/'lib/R/bin/x64',envroot/'Library/bin',envroot/'Scripts']))+os.pathsep+env.get('PATH','')
    # R on Windows needs UTF-8 to resolve non-ASCII installation paths.
    # The English locale also retains the decimal point used by our CSV bridge.
    env['LC_ALL']='en_US.UTF-8';env['LANG']='en_US.UTF-8'
    with tempfile.TemporaryDirectory(prefix='sps_cast_') as folder:
        out=Path(folder)
        for filename,xy in [('train.csv',training),('pred.csv',prediction)]:
            np.savetxt(out/filename,xy,delimiter=',',header='x,y',comments='',fmt='%.17g')
        args=[str(exe),'--vanilla',str(Path(__file__).with_name('knndm_bridge.R')),str(out/'train.csv'),str(out/'pred.csv'),str(out),str(k),str(maxp),str(seed),str(envroot/'library-extra')]
        with (out/'process.log').open('w',encoding='utf-8') as log:
            proc=subprocess.Popen(args,stdout=log,stderr=subprocess.STDOUT,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                import time
                start=time.monotonic()
                while True:
                    try:proc.wait(timeout=.2);break
                    except subprocess.TimeoutExpired:
                        check_cancel()
                        if time.monotonic()-start>180:raise JobError('CAST_TIMEOUT','kNNDM exceeded the time limit.')
            finally:
                if proc.poll() is None:proc.kill();proc.wait()
        if proc.returncode:
            raise JobError('CAST_RUNTIME','CAST process failed: '+(out/'process.log').read_text(encoding='utf-8',errors='replace')[-1800:])
        tr=np.genfromtxt(out/'training.csv',delimiter=',',names=True)
        pred=np.genfromtxt(out/'prediction.csv',delimiter=',',names=True)
        with (out/'summary.csv').open(newline='',encoding='utf-8') as f:summary=next(csv.DictReader(f))
        if summary['CAST']!='1.0.4':raise JobError('CAST_VERSION','The CAST version does not match the record.')
        result={'fold':tr['fold'].astype(int).tolist(),'Gj':tr['Gj'].tolist(),'Gjstar':tr['Gjstar'].tolist(),
                'Gij':pred['Gij'].tolist(),'W':float(summary['W']),'q':summary['q'],'reference':'CAST 1.0.4',
                'implementation':'local CAST::knndm; hierarchical ward.D2; projected Euclidean',
                'seed':int(seed),'maxp':float(maxp),'prediction_points':len(prediction)}
    if len(_CACHE)>=32:_CACHE.pop(next(iter(_CACHE)))
    _CACHE[key]=deepcopy(result)
    return result
