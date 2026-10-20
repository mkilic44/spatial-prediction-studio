"""Prepare synthetic C acceptance projects; execution occurs through ArcMap UI."""
import copy
import csv
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
evidence=ROOT/'build/arcmap-evidence'
project=json.loads((evidence/'b-roundtrip-panel-saved.sps.json').read_text(encoding='utf-8'))
with (evidence/'b-samples.csv').open(encoding='utf-8-sig',newline='') as stream:
    reader=csv.DictReader(stream);fields=reader.fieldnames;rows=list(reader)
for i,row in enumerate(rows):row['landuse']='sentetik_A' if i%2 else 'sentetik_B'
path=evidence/'c-samples.csv'
with path.open('w',encoding='utf-8-sig',newline='') as stream:
    writer=csv.DictWriter(stream,fieldnames=fields+['landuse']);writer.writeheader();writer.writerows(rows)
project['csv_path']=str(path)
project['output_root']=str(evidence/'c-runs')
for key in ('last_manifest','preview_manifest','preview_signature','preview_sources','preview_fingerprint','registry_digest'):project[key]=''
science=project['science']
science['validation'].update(method='knndm',n_splits=3,inner_splits=3,maxp=.75,prediction_samples=64)
science['validation'].pop('inner_method',None)
science['model']={'id':'lightgbm','parameters':{'n_estimators':40,'num_leaves':7,'min_child_samples':5,'learning_rate':.1}}
science['uncertainty'].update(coverage=.8,calibration_fraction=.3)
science['diagnostics']={'applicability':True,'permutation':True,'permutation_repeats':3,
    'subgroup_fields':['landuse'],'min_subgroup_samples':5,'min_subgroup_groups':3}
(evidence/'c-knndm.sps.json').write_text(json.dumps(project,ensure_ascii=False,indent=2),encoding='utf-8')
hybrid=copy.deepcopy(project)
hybrid['science']['validation'].update(method='spatial_block',block_size=1000)
for key in ('maxp','prediction_samples'):hybrid['science']['validation'].pop(key,None)
hybrid['science']['model']={'id':'regression_kriging','parameters':{'base_model':'ridge','n_closest_points':12,'variogram_model':'spherical'}}
hybrid['science']['vif']={'mode':'off'}
(evidence/'c-kriging.sps.json').write_text(json.dumps(hybrid,ensure_ascii=False,indent=2),encoding='utf-8')
print('Prepared two synthetic ArcMap acceptance projects with',len(rows),'observations')
