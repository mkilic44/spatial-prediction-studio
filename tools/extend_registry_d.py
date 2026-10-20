"""Add experimental D schemas without resetting accepted A-C methods."""
import json
from copy import deepcopy
from pathlib import Path
from make_registry import p, enum

ROOT=Path(__file__).resolve().parents[1]
path=ROOT/'backend/sps/registry.json'
obj=json.loads(path.read_text(encoding='utf-8'))
common={'n_epochs':p('Epochs','int',10,min=1,max=200),
        'batch_size':p('Batch size','int',16,min=4,max=512),
        'hidden_size':p('Hidden layer width','int',32,min=8,max=256),
        'n_layers':p('Number of hidden layers','int',2,min=1,max=4)}
schemas={
 'tabm':{**common,'k':p('Number of ensemble members','int',4,min=2,max=32),
         'dropout':p('Dropout rate during training','float',.1,min=0,max=.8),
         'lr':p('Learning rate','float',.001,min=.000001,max=.2),
         'weight_decay':p('Weight decay','float',.0001,min=0,max=.1)},
 'realmlp':{**common,'lr':p('Learning rate','float',.04,min=.000001,max=.2)}}
for row in obj['categories']['model']:
    if row['id'] in schemas:
        row.update(parameters=schemas[row['id']],enabled=False,status='implementation_testing',
                   reason='Stage D comparative acceptance tests are in progress.',
                   label=('TabM' if row['id']=='tabm' else 'RealMLP')+' (experimental)',
                   description='Trained on the local CPU for a fixed number of epochs. Internal validation and early stopping are off. Each training run may use at most 120 seconds and 2 GiB of memory.',
                   cost='At most 2000 training observations and 256 variables after encoding; single thread. It can be slow even on small data.')
row=next(r for r in obj['categories']['uncertainty'] if r['id']=='spatial_conformal')
row.update(enabled=False,status='implementation_testing',reason='Stage D comparative acceptance tests are in progress.',
           label='Spatially weighted conformal (experimental)',
           description='Weights separate calibration residuals by distance. There is no exact coverage guarantee. If local support is insufficient, the point prediction is kept; no interval is produced.',
           parameters={'coverage':p('Nominal coverage','float',.9,min=.5,max=.99),
                       'calibration_fraction':p('Fraction of groups held out for calibration','float',.3,min=.1,max=.5),
                       'spatial_bandwidth_m':p('Predefined bandwidth (m)','float',1000,min=1,max=1000000),
                       'spatial_kernel':enum('Distance weighting','gaussian',[('gaussian','Gaussian'),('compact','Compact support')]),
                       'spatial_min_effective_n':p('Minimum effective calibration size','float',5,min=2,max=10000),
                       'spatial_min_groups':p('Minimum effective calibration groups','int',3,min=2,max=10000)})
row=next(r for r in obj['categories']['model'] if r['id']=='tabpfn')
row.update(enabled=False,status='implementation_testing',reason='Stage D comparative acceptance tests are in progress.',
           label='TabPFN v2 (experimental)',
           description='A pretrained tabular model runs on the local CPU. Built with PriorLabs-TabPFN. Automatic download and telemetry are off.',
           cost='At most 1000 training observations and 256 variables after encoding. Each subprocess may use at most 120 seconds and 2 GiB of memory.',
           parameters={'n_estimators':p('Number of ensemble members','int',2,min=1,max=4),
                       'softmax_temperature':p('Softmax temperature','float',.9,min=.05,max=5)})
base=deepcopy(next(r for r in obj['categories']['validation'] if r['id']=='random')['parameters'])
base.pop('inner_method',None);base.pop('block_size',None)
base['n_splits']['default']=3;base['inner_splits']['default']=2;base['repeats']['max']=1
base.update(temporal_mode=enum('Temporal evaluation purpose','temporal_new_groups',[('temporal_new_groups','New profiles or parcels at later dates')]),
            timezone=p('Time zone (IANA name)','string','UTC'),
            gap_days=p('Gap between roles (calendar days)','int',0,min=0,max=36500),
            initial_train_periods=p('Periods in the first outer training set','int',12,min=1,max=100000),
            inner_initial_train_periods=p('Periods in the first inner training set','int',4,min=1,max=100000),
            selector_initial_train_periods=p('Initial training periods for the sequential selector','int',1,min=1,max=100000))
row=next(r for r in obj['categories']['validation'] if r['id']=='temporal')
row.update(enabled=False,status='implementation_testing',reason='Stage D comparative acceptance tests are in progress.',
           label='Time-ordered validation (new groups)',parameters=base,
           description='A date field is required. Profiles and same-day observations are not split. The training history moves forward; calibration is held out from the last eligible periods. Evaluation with fixed rasters does not validate a map for a specific future date.')
obj['registry_version']='0.4.0-dev'
path.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('D schemas prepared; UI methods remain disabled until acceptance.')
