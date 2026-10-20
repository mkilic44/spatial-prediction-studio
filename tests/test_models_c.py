"""Stage C uses real estimators with held-out identity interventions."""
import copy
from pathlib import Path
import sys
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from sps.contract import JobError
from sps.modeling import make_point_estimator
from test_science_b import scientific, nested


@pytest.mark.parametrize('name,params', [
    ('lightgbm', {'n_estimators': 15, 'num_leaves': 5, 'min_child_samples': 3}),
    ('catboost', {'iterations': 15, 'depth': 3}),
    ('cubist', {'n_committees': 2, 'neighbors': 3}),
])
def test_real_c_model_outer_target_is_not_used(scientific, name, params):
    scientific[-1]['model'] = {'id': name, 'parameters': params}
    result = nested(scientific)
    held = result['folds'][0]['test_ids']
    altered = copy.deepcopy(scientific)
    altered[1][[altered[2].index(i) for i in held]] += 5000
    other = nested(altered)
    left = [r['predicted'] for r in result['oof'] if r['fold'] == 0]
    right = [r['predicted'] for r in other['oof'] if r['fold'] == 0]
    np.testing.assert_array_equal(left, right)
    assert np.isfinite(left).all()
    model = result['final_model']
    assert model.estimator.__class__.__module__.split('.')[0] in ('lightgbm', 'catboost', 'cubist')
    for item in result['learning_audit']:
        if item['role'].startswith('outer/0/0/'):
            assert not set(item['ids']) & set(held)


@pytest.mark.parametrize('name,params', [
    ('cubist', {'cv': 5}), ('cubist', {'auto': True}),
    ('catboost', {'task_type': 'GPU'}), ('catboost', {'allow_writing_files': True}),
    ('lightgbm', {'n_jobs': 8}), ('lightgbm', {'early_stopping_rounds': 5}),
])
def test_c_models_reject_hidden_validation_threads_and_writes(name, params):
    with pytest.raises(JobError) as err:
        make_point_estimator({'id': name, 'parameters': params})
    assert err.value.code == 'MODEL_PARAMETER'


def test_c_fixed_reproducibility_configuration():
    cat = make_point_estimator({'id': 'catboost', 'parameters': {}}).get_params()
    lgb = make_point_estimator({'id': 'lightgbm', 'parameters': {}}).get_params()
    cub = make_point_estimator({'id': 'cubist', 'parameters': {}}).get_params()
    assert cat['thread_count'] == 1 and cat['allow_writing_files'] is False
    assert cat['use_best_model'] is False and cat['task_type'] == 'CPU'
    assert lgb['n_jobs'] == 1 and lgb['deterministic'] is True
    assert cub['cv'] is None and cub['auto'] is False
