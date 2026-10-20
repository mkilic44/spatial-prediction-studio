import csv,sys
from pathlib import Path
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from sps.applicability import Applicability
from sps.contract import JobError
from test_science_b import scientific,nested
FIX=Path(__file__).parent/'fixtures/cast-1.0.4'
def read(name):return np.genfromtxt(FIX/name,delimiter=',',names=True,encoding='utf-8')
def matrix(name):
    a=read(name);return np.column_stack([a[n] for n in a.dtype.names])
def reference():
    x=matrix('aoa_train.csv');fold=read('aoa_train_di.csv')['fold'];ids=[str(i) for i in range(len(x))]
    folds=[{'train_ids':[ids[i] for i in np.flatnonzero(fold!=f)],'test_ids':[ids[i] for i in np.flatnonzero(fold==f)]} for f in range(5)]
    return x,ids,folds
def test_cast_104_intermediate_distances_threshold_and_mask():
    x,ids,folds=reference();a=Applicability().fit(x,ids,folds,weights=[1,.5,2])
    par=read('aoa_parameters.csv');summary=read('aoa_summary.csv');expected=read('aoa_result.csv')
    np.testing.assert_allclose(a.center,par['center'],atol=1e-13)
    np.testing.assert_allclose(a.scale,par['scale'],atol=1e-13)
    assert a.mean_distance==pytest.approx(float(summary['mean_distance']),abs=1e-12)
    np.testing.assert_allclose(a.train_di,read('aoa_train_di.csv')['di'],atol=1e-12)
    assert a.threshold==pytest.approx(float(summary['threshold']),abs=1e-12)
    di,mask=a.predict(matrix('aoa_prediction.csv'))
    np.testing.assert_allclose(di,expected['di'],atol=1e-12)
    np.testing.assert_array_equal(mask,expected['applicability'])
def test_applicability_prediction_does_not_change_training_threshold():
    x,ids,folds=reference();a=Applicability().fit(x,ids,folds)
    before=a.threshold;di,mask=a.predict(np.full((3,3),1e6))
    assert not mask.any() and a.threshold==before
    chunks=[a.predict(x[:7])[0],a.predict(x[7:])[0]]
    np.testing.assert_array_equal(a.predict(x)[0],np.concatenate(chunks))
def test_applicability_rejects_repeated_or_invalid_folds_and_constant_design():
    x,ids,folds=reference()
    with pytest.raises(JobError,match='tam bir kez'):Applicability().fit(x,ids,folds+folds)
    with pytest.raises(JobError,match='varyans'):Applicability().fit(np.ones_like(x),ids,folds)
    with pytest.raises(JobError,match='ağırlık'):Applicability().fit(x,ids,folds,weights=[-1,1,1])

def test_outer_and_calibration_ids_never_fit_applicability(scientific):
    from sps.modeling import prediction_call
    X,y,ids,xy,groups,cov,settings=scientific
    settings['diagnostics']={'applicability':True}
    settings['uncertainty']={'method':'split_conformal','coverage':.8,'calibration_fraction':.3}
    result=nested(scientific)
    for fold,metric in zip(result['folds'],result['fold_metrics']):
        r=metric['model_record'];used=set(r['applicability']['training_ids'])
        assert used==set(r['development_ids'])
        assert used.isdisjoint(fold['test_ids']+r['calibration_ids'])
    assert set(result['final_record']['applicability']['training_ids']).isdisjoint(result['final_record']['calibration_ids'])
    assert np.isfinite(prediction_call(result['final_model'],'diagnostics',X,xy)['dissimilarity']).all()
