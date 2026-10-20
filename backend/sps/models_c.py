"""Local Stage C estimators; no hidden CV, telemetry, writes or early stopping."""
from .contract import JobError

PARAMETERS = {
    'lightgbm': {'n_estimators', 'num_leaves', 'max_depth', 'learning_rate', 'min_child_samples',
                 'colsample_bytree', 'reg_alpha', 'reg_lambda'},
    'catboost': {'iterations', 'depth', 'learning_rate', 'l2_leaf_reg', 'random_strength'},
    'cubist': {'n_committees', 'n_rules', 'neighbors', 'unbiased', 'extrapolation'},
}


def make_estimator(name, parameters, seed):
    unknown = set(parameters) - PARAMETERS[name]
    if unknown:
        raise JobError('MODEL_PARAMETER', name + ': desteklenmeyen parametre: ' + sorted(unknown)[0])
    try:
        if name == 'lightgbm':
            from lightgbm import LGBMRegressor
            return LGBMRegressor(**parameters, objective='regression', random_state=seed,
                                 n_jobs=1, deterministic=True, force_col_wise=True, verbosity=-1)
        if name == 'catboost':
            from catboost import CatBoostRegressor
            return CatBoostRegressor(**parameters, loss_function='RMSE', random_seed=seed,
                                     thread_count=1, task_type='CPU', allow_writing_files=False,
                                     use_best_model=False, verbose=False)
        from cubist import Cubist
        return Cubist(**parameters, random_state=seed, cv=None, auto=False, verbose=0)
    except ImportError as exc:
        raise JobError('DEPENDENCY', name + ' bağımlılığı bu Python ortamında bulunamadı.') from exc
