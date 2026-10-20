"""Run a small full Ridge workflow using only a relocated installed runtime."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import zipfile

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();root=args.root.resolve();out=args.output.resolve()
    out.mkdir(parents=True,exist_ok=False)
    env=os.environ.copy();env.update({'PYTHONHOME':r'Z:\not-a-python-installation','PYTHONPATH':r'Z:\not-an-import-path','PATH':str(Path(os.environ['SystemRoot'])/'System32'),'TABPFN_DISABLE_TELEMETRY':'1','DO_NOT_TRACK':'1','HF_HUB_OFFLINE':'1'})
    env.pop('SPS_START_GATE',None)
    req={'schema_version':2,'run_id':'portable_preview','task':'regression','action':'preview',
         'samples':{'path':str(root/'examples/samples.geojson'),'id_field':'sample_id','target_field':'soil_value','unit':'g/kg'},
         'covariates':[{'id':'a','name':'Türkçe yükselti ıİğĞşŞ','unit':'m','path':str(root/'examples/covariates.tif'),'band':1,'kind':'continuous'},
                       {'id':'b','name':'Çevre özelliği','unit':'1','path':str(root/'examples/covariates.tif'),'band':2,'kind':'continuous'}],
         'model':{'id':'ridge','parameters':{'alpha':1}},'validation':{'method':'random','n_splits':3,'inner_splits':2,'seed':42},
         'output_dir':str(out/'preview'),'window_size':32}
    started=time.monotonic();logs=[]
    for action in ('preview','run'):
        request_path=out/(action+'.request.json');request_path.write_text(json.dumps(req,ensure_ascii=False,indent=2),encoding='utf-8')
        done=subprocess.run([str(root/'runtime-d/Scripts/python.exe'),'-u',str(root/'backend/worker.py'),str(request_path)],cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        (out/(action+'.stdout.jsonl')).write_text(done.stdout,encoding='utf-8');(out/(action+'.stderr.log')).write_text(done.stderr,encoding='utf-8')
        if done.returncode:raise RuntimeError(done.stderr[-3000:]+done.stdout[-3000:])
        result=json.loads((Path(req['output_dir'])/'result.json').read_text(encoding='utf-8'))
        assert result['status']=='completed',result
        for artifact in result['outputs']:
            path=(Path(req['output_dir'])/artifact['path']).resolve()
            assert path.is_relative_to(Path(req['output_dir'])) and sha(path)==artifact['sha256']
            if path.suffix=='.zip':
                with zipfile.ZipFile(path) as archive:assert archive.testzip() is None
        logs.append({'action':action,'outputs':len(result['outputs']),'manifest':str(Path(req['output_dir'])/'result.json'),'sha256':sha(Path(req['output_dir'])/'result.json')})
        req.update(action='run',run_id='portable_ridge',preview_fingerprint=result['preview_fingerprint'],output_dir=str(out/'run'))
    assert {'model_bundle','study_package'} <= {x['kind'] for x in result['outputs']}
    check='import rasterio,json,sys; p=sys.argv[1]; ds=rasterio.open(p); assert ds.units==("g/kg",); assert ds.read(1,masked=True).count()==12248; print(json.dumps({"valid_pixels":12248,"unit":ds.units[0],"module":rasterio.__file__}))'
    done=subprocess.run([str(root/'runtime-d/Scripts/python.exe'),'-c',check,str(out/'run/prediction.tif')],cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));assert done.returncode==0,done.stderr
    raster=json.loads(done.stdout);assert Path(raster['module']).resolve().is_relative_to(root)
    record={'status':'passed','elapsed_seconds':time.monotonic()-started,'installed_root':str(root),'workflow':logs,'raster':raster,'environment_python_paths_deliberately_invalid':True,'native_launch_arguments_used':True,'real_arcmap_ui_test':False}
    (out/'readback.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(record,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
