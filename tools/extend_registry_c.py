"""Extend accepted B registry without resetting its verified methods."""
import json
from copy import deepcopy
from pathlib import Path
from make_registry import p,enum
ROOT=Path(__file__).resolve().parents[1]
path=ROOT/'backend/sps/registry.json'
obj=json.loads(path.read_text(encoding='utf-8'));cats=obj['categories']
schemas={
 'lightgbm':{'n_estimators':p('Number of trees','int',100,min=10,max=2000),'num_leaves':p('Number of leaves','int',31,min=2,max=255),'max_depth':p('Maximum depth (-1 = no limit)','int',-1,min=-1,max=50),'learning_rate':p('Learning rate','float',.1,min=.0001,max=1),'min_child_samples':p('Minimum samples per leaf','int',10,min=1,max=200),'colsample_bytree':p('Fraction of variables','float',1,min=.1,max=1),'reg_alpha':p('Absolute penalty','float',0,min=0,max=1000),'reg_lambda':p('Squared penalty','float',0,min=0,max=1000)},
 'catboost':{'iterations':p('Iterations','int',100,min=10,max=2000),'depth':p('Depth','int',6,min=1,max=12),'learning_rate':p('Learning rate','float',.1,min=.0001,max=1),'l2_leaf_reg':p('Regularization','float',3,min=0,max=1000),'random_strength':p('Randomness strength','float',1,min=0,max=100)},
 'cubist':{'n_committees':p('Number of committees','int',1,min=1,max=100),'n_rules':p('Rule limit','int',100,min=1,max=500),'neighbors':p('Correction neighbors (0 = off)','int',0,min=0,max=9),'unbiased':p('Bias correction','bool',False),'extrapolation':p('Extrapolation fraction','float',.05,min=0,max=1)}}
kp={'variogram_model':enum('Semivariogram model','spherical',[('spherical','Spherical'),('exponential','Exponential'),('gaussian','Gaussian')]),'nlags':p('Number of lag classes','int',6,min=3,max=30),'weight':p('More weight on short distances','bool',False),'n_closest_points':p('Number of local neighbors','int',12,min=3,max=200)}
schemas['ordinary_kriging']=kp
schemas['regression_kriging']={**kp,'base_model':enum('Base model','ridge',[('ridge','Ridge'),('random_forest','Random forest')]),'base_alpha':p('Ridge penalty','float',1,min=.000001,max=1000000),'base_trees':p('Number of forest trees','int',100,min=10,max=2000),'base_depth':p('Forest depth','int',10,min=1,max=100)}
for row in cats['model']:
 if row['id'] in schemas:
  row.update(parameters=schemas[row['id']],enabled=False,status='implementation_testing',reason='Stage C integration and ArcMap acceptance tests are in progress.')
  row['description']=('The semivariogram is fitted only in the relevant training fold. Kriging variance is not the total uncertainty; training uses at most 2000 observations.' if 'kriging' in row['id'] else "A pinned version of the actual package runs on the local CPU. Early stopping and the model's own cross-validation are off.")
kn=next(r for r in cats['validation'] if r['id']=='knndm')
kn.update(parameters={**deepcopy(cats['validation'][0]['parameters']),'maxp':p('Maximum fraction of observations in one fold','float',.75,min=.1,max=.99),'prediction_samples':p('Target number of area samples','int',1000,min=25,max=4000)},enabled=False,status='implementation_testing',reason='Stage C integration and ArcMap acceptance tests are in progress.',description='Distance matching with local CAST 1.0.4. This version supports only unique independent observations, metric coordinates and at most 2000 training points.')
obj['registry_version']='0.3.0-dev'
path.write_text(json.dumps(obj,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('C schemas extended; accepted B status preserved.')
