"""Real D estimators through the actual first nested outer evaluation.

The deliberate stop after that outer fold bounds this intervention experiment;
it is not represented as a completed whole-run acceptance.
"""
import copy
import sys
from pathlib import Path
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from sps import evaluation
from sps.modeling import prediction_call
from test_science_b import scientific


@pytest.mark.parametrize('name',['tabm','realmlp','tabpfn'])
def test_real_d_outer_target_intervention_through_nested_search(scientific,monkeypatch,name):
    X,y,ids,xy,groups,covs,settings=copy.deepcopy(scientific)
    X=X[:64,:2];y=y[:64];ids=ids[:64];xy=xy[:64];groups=groups[:64];covs=covs[:2]
    settings['validation'].update(n_splits=2,inner_splits=2)
    settings['vif']={'mode':'off'}
    params={'n_estimators':1} if name=='tabpfn' else {'n_epochs':1,'hidden_size':8,'n_layers':1}
    settings['model']={'id':name,'parameters':params}
    settings['optimization'].update(timeout_seconds=600,trials=1)
    folds=evaluation.make_folds(ids,xy,groups,settings['validation'],crs='EPSG:32637')
    held=folds[0]['test_ids'];indices=[ids.index(v) for v in held]
    captured=[];original=evaluation._fit_partition
    def spy(*args,**kwargs):
        model,record=original(*args,**kwargs)
        captured.append((model,copy.deepcopy(record),copy.deepcopy(kwargs['audit']),copy.deepcopy(kwargs['trials'])))
        return model,record
    monkeypatch.setattr(evaluation,'_fit_partition',spy)
    class FirstFoldFinished(Exception):pass
    def progress(stage,completed,total,message):
        if stage=='evaluation' and completed==1:raise FirstFoldFinished()
    for target in (y, y+np.asarray([10000 if identity in held else 0 for identity in ids])):
        with pytest.raises(FirstFoldFinished):
            evaluation.evaluate_nested(X,target,ids,xy,groups,covs,settings,crs='EPSG:32637',check_cancel=lambda:None,progress=progress)
    a,b=captured
    np.testing.assert_array_equal(prediction_call(a[0],'predict',X[indices],xy[indices]),prediction_call(b[0],'predict',X[indices],xy[indices]))
    for key in ('model','training_ids','development_ids','calibration_ids','selected_features','target_transform'):
        assert a[1].get(key)==b[1].get(key)
    assert a[3][0]['selected'] and b[3][0]['selected']
    for _,_,audit,_ in captured:
        for row in audit:assert not set(row['ids'])&set(held)
