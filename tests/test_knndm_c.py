import sys
from pathlib import Path
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from sps.knndm import cast_knndm
from sps.contract import JobError
FIX=Path(__file__).parent/'fixtures/cast-1.0.4'
def read(name):return np.genfromtxt(FIX/name,delimiter=',',names=True)
def matrix(name):
    r=read(name);return np.column_stack([r[n] for n in r.dtype.names])
def test_real_cast_bridge_matches_reference_folds_and_three_distance_distributions():
    a=cast_knndm(matrix('knndm_train.csv'),matrix('knndm_prediction.csv'),5,.5,42)
    tr=read('knndm_train_result.csv');pp=read('knndm_prediction_result.csv');s=read('knndm_summary.csv')
    np.testing.assert_array_equal(a['fold'],tr['fold'])
    for key in ('Gj','Gjstar'):np.testing.assert_allclose(a[key],tr[key],atol=1e-8)
    np.testing.assert_allclose(a['Gij'],pp['Gij'],atol=1e-8)
    assert a['W']==pytest.approx(float(s['W']),abs=1e-8)
    assert a['q']==str(int(s['q']))
def test_knndm_rejects_invalid_workload_and_fraction_before_process_start():
    with pytest.raises(JobError):cast_knndm(np.ones((2001,2)),np.ones((10,2)),5,.5,42)
    with pytest.raises(JobError):cast_knndm(np.ones((30,2)),np.ones((10,2)),5,.2,42)

def test_real_cast_reads_unicode_work_paths_with_c_parent_locale(monkeypatch,tmp_path_factory):
    import tempfile
    from sps import knndm
    folder=tmp_path_factory.getbasetemp()/'çalışma_ölçüm'
    folder.mkdir()
    monkeypatch.setattr(tempfile,'tempdir',str(folder))
    monkeypatch.setenv('LC_ALL','C');monkeypatch.setenv('LANG','C')
    knndm._CACHE.clear()
    result=cast_knndm(matrix('knndm_train.csv'),matrix('knndm_prediction.csv'),5,.5,42)
    np.testing.assert_array_equal(result['fold'],read('knndm_train_result.csv')['fold'])
    assert result['W']==pytest.approx(float(read('knndm_summary.csv')['W']),abs=1e-8)

def test_folds_preserve_reference_distances_and_reject_group_splitting():
    from sps.validation import make_folds
    xy=matrix('knndm_train.csv');ids=['P%03d'%i for i in range(len(xy))];groups=np.asarray(ids)
    config={'method':'knndm','n_splits':5,'inner_splits':3,'seed':42,'maxp':.5,
        'prediction_coordinates':matrix('knndm_prediction.csv').tolist(),'metric_crs':'EPSG:32637'}
    folds=make_folds(ids,xy,groups,config,crs='EPSG:32637')
    expected=read('knndm_train_result.csv')['fold']
    for f in folds:
        assert f['test_ids']==[ids[i] for i in np.flatnonzero(expected==f['fold'])]
        assert set(f['train_ids']).isdisjoint(f['test_ids'])
    grouped=groups.copy();grouped[1]=grouped[0]
    with pytest.raises(JobError) as exc:make_folds(ids,xy,grouped,config,crs='EPSG:32637')
    assert exc.value.code=='KNNDM_GROUPS'
