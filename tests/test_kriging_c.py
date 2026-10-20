"""Spatial models must learn residuals and variograms inside each training fold."""
import copy
from pathlib import Path
import sys
import numpy as np
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from test_science_b import scientific, nested, fit
from sps.contract import JobError


def prepare(case, family='regression_kriging'):
    case[-1]['model'] = {'id': family, 'parameters': {'variogram_model': 'spherical',
        'nlags': 6, 'n_closest_points': 12, 'base_model': 'ridge', 'base_alpha': 1.}}
    case[-1]['target_transform'] = {'method': 'none'}
    case[-1]['vif'] = {'mode': 'off'}
    case[-1]['validation']['metric_crs'] = 'EPSG:32637'
    return case


@pytest.mark.parametrize('family', ['ordinary_kriging', 'regression_kriging'])
def test_kriging_outer_targets_do_not_change_fitted_surface(scientific, family):
    case = prepare(scientific, family)
    first = nested(case)
    changed = copy.deepcopy(case)
    held = first['folds'][0]['test_ids']
    changed[1][[case[2].index(i) for i in held]] += 1000
    second = nested(changed)
    a = [r['predicted'] for r in first['oof'] if r['fold'] == 0]
    b = [r['predicted'] for r in second['oof'] if r['fold'] == 0]
    np.testing.assert_array_equal(a, b)
    records = [x for x in first['learning_audit'] if x['stage'] == 'variogram']
    assert len(records) > 4
    for row in records:
        if row['role'].startswith('outer/0/0/'):
            assert not set(row['ids']) & set(held)
    if family == 'regression_kriging':
        assert all('plain_predicted' in r for r in first['oof'])
        assert 'plain_model_comparison' in first


def test_spatial_predictions_are_invariant_to_window_partition(scientific):
    from sps.modeling import prediction_call
    model = fit(prepare(scientific))
    X, _, _, xy = scientific[:4]
    full = prediction_call(model, 'predict', X, xy)
    parts = np.concatenate([prediction_call(model, 'predict', X[i:i+7], xy[i:i+7]) for i in range(0,len(X),7)])
    # BLAS reductions can differ at 1.8e-15 between matrix block lengths.
    np.testing.assert_allclose(full, parts, rtol=0, atol=1e-12)
    with pytest.raises(JobError, match='coordinate'):
        prediction_call(model, 'predict', X)


def test_kriging_conformal_calibration_cannot_change_base_fit(scientific):
    from sps.modeling import prediction_call
    case = prepare(scientific)
    case[-1]['uncertainty']['method'] = 'split_conformal'
    result = nested(case)
    changed = copy.deepcopy(case)
    calibration = result['final_record']['calibration_ids']
    changed[1][[case[2].index(i) for i in calibration]] += 500
    other = nested(changed)
    np.testing.assert_array_equal(
        prediction_call(result['final_model'], 'predict', case[0], case[3]),
        prediction_call(other['final_model'], 'predict', case[0], case[3]))
    lo,hi = prediction_call(result['final_model'], 'predict_interval', case[0], case[3])
    lo2,hi2 = prediction_call(other['final_model'], 'predict_interval', case[0], case[3])
    assert np.mean(hi2-lo2)>np.mean(hi-lo)+500


def test_constant_kriging_and_resource_boundary(scientific):
    from sps.spatial_models import KrigingSurface
    s=KrigingSurface({'variogram_model':'spherical','nlags':6,'n_closest_points':6})
    xy=np.column_stack((np.arange(8),np.arange(8)**2))
    s.fit(xy,np.ones(8)*3)
    np.testing.assert_array_equal(s.predict(xy),np.ones(8)*3)
    assert s.record['constant_field'] is True
    with pytest.raises(JobError):
        KrigingSurface({}).fit(np.column_stack((np.arange(2001),np.arange(2001))),np.arange(2001))
