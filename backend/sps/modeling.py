"""Actual regression estimators and serializable fitted scientific workflows."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import fields, is_dataclass
from enum import Enum
import inspect
from pathlib import Path
import random
import numpy as np
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import ElasticNet, Ridge
from sklearn.preprocessing import PowerTransformer
from sklearn.svm import SVR

from .contract import JobError
from .pipeline import FeaturePipeline, audit, sequential_selection


def prediction_call(model, method, X, coordinates=None, *args):
    """Pass locations only to spatial workflows; keep existing model contracts."""
    if getattr(model, 'requires_coordinates', False):
        return getattr(model, method)(X, *args, coordinates=coordinates)
    return getattr(model, method)(X, *args)


def bind_runtime_callbacks(model, check_cancel):
    """Restore transient cancellation after trusted local deserialization."""
    from .spatial_uncertainty_d import SpatialCalibratedModel
    current=model
    while True:
        if isinstance(current,SpatialCalibratedModel):current._check_cancel_=check_cancel
        if not hasattr(current,'base'):break
        current=current.base
    if model.record.get('model',{}).get('id')=='tabpfn':
        current.estimator._check_cancel_=check_cancel
    return model


def _parameter_value(value):
    """Record parameters without pickling objects or losing nonfinite sentinels."""
    if isinstance(value, np.generic):
        return _parameter_value(value.item())
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if np.isfinite(value):
            return value
        return {"type": "nonfinite_float", "value": "NaN" if np.isnan(value) else "Infinity" if value > 0 else "-Infinity"}
    if isinstance(value, dict):
        return {str(key): _parameter_value(item) for key, item in value.items()}
    if isinstance(value, np.ndarray):
        return _parameter_value(value.tolist())
    if isinstance(value, (tuple, list)):
        return [_parameter_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        import json
        return sorted((_parameter_value(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, np.random.Generator):
        return {"type": "numpy.random.Generator", "state": _parameter_value(value.bit_generator.state)}
    if isinstance(value, np.random.RandomState):
        return {"type": "numpy.random.RandomState", "state": _parameter_value(value.get_state())}
    if isinstance(value, random.Random):
        return {"type": "random.Random", "state": _parameter_value(value.getstate())}
    if isinstance(value, Enum):
        return {"type": type(value).__module__ + "." + type(value).__qualname__, "name": value.name, "value": _parameter_value(value.value)}
    if is_dataclass(value) and not isinstance(value, type):
        return {"type": type(value).__module__ + "." + type(value).__qualname__,
                "fields": {field.name: _parameter_value(getattr(value, field.name)) for field in fields(value)}}
    if type(value).__module__ == "torch" and type(value).__name__ in ("device", "dtype"):
        return {"type": "torch." + type(value).__name__, "value": str(value)}
    if isinstance(value, np.dtype):
        return {"type": "numpy.dtype", "value": str(value)}
    if hasattr(value, "get_params") and not isinstance(value, type):
        return {"type": type(value).__module__ + "." + type(value).__qualname__, "parameters": _parameter_value(value.get_params(deep=False))}
    if callable(value):
        return {"type": "callable", "name": getattr(value, "__module__", "") + "." + getattr(value, "__qualname__", type(value).__qualname__)}
    return {"type": type(value).__module__ + "." + type(value).__qualname__, "serialization": "type_only_not_exposed"}


def _effective_estimator_record(estimator):
    parameters = estimator.get_params(deep=False)
    source = "estimator.get_params(deep=False)"
    # CatBoost's get_params returns only specified values. Its fitted API also
    # exposes defaults resolved from this training partition.
    if type(estimator).__module__.startswith("catboost"):
        parameters = {**parameters, **estimator.get_all_params()}
        source += "; fitted estimator.get_all_params()"
    return {"effective_estimator_class": type(estimator).__module__ + "." + type(estimator).__qualname__,
            "effective_parameters": _parameter_value(parameters), "effective_parameters_source": source,
            "effective_parameters_scope": "fitted estimator constructor parameters; includes exposed defaults"}


def _producer_parameters(estimator, *, config=False, inference=False):
    """Called inside the existing training child after fitting, never refits."""
    result = {"estimator_class": type(estimator).__module__ + "." + type(estimator).__qualname__,
              "get_params": _parameter_value(estimator.get_params(deep=False))}
    if config:
        result["get_config"] = _parameter_value(estimator.get_config())
    if inference:
        result["inference_config"] = _parameter_value(getattr(estimator, "interface_config_", getattr(estimator, "inference_config", None)))
        result["inference_config_source"] = "interface_config_" if hasattr(estimator, "interface_config_") else "inference_config"
    return result


def _fitted_model_attributes(estimator, pipeline, target):
    encoder = pipeline.encoder
    encoded = [{"source_id": encoder.covariates[i]["id"], "kind": "continuous"} for i in encoder.numeric]
    if encoder.encoder is not None:
        for i, categories in zip(encoder.categorical, encoder.encoder.categories_):
            encoded.extend({"source_id": encoder.covariates[i]["id"], "kind": "one_hot", "category": _parameter_value(category)} for category in categories)
    inputs = ([{"kind": "principal_component", "component": i + 1} for i in range(pipeline.pca.n_components_)]
              if pipeline.pca is not None else [encoded[i] for i in pipeline.columns])
    attributes = {}
    for name in ("coef_", "intercept_", "feature_importances_"):
        found = getattr(estimator, name, None)
        if found is not None:
            attributes[name] = _parameter_value(found)
    structure = {}
    for name in ("n_features_in_", "n_outputs_", "n_estimators_", "n_estimators", "n_iter_", "tree_count_", "n_support_"):
        found = getattr(estimator, name, None)
        if found is not None:
            structure[name] = _parameter_value(found)
    return {"status": "available" if attributes else "not_available", "attributes": attributes, "structure": structure,
            "reason": None if attributes else "estimator_does_not_expose_coefficients_or_feature_importances",
            "input_features": inputs, "selected_features": list(pipeline.record["selected_features"]),
            "preprocessing": deepcopy(pipeline.record.get("preprocessing", {})), "target_transform": target.record(),
            "scope": "fitted estimator on encoded/scaled features and transformed target; not raw soil-unit coefficients; not held-out permutation importance"}


def _record_spatial_parameters(fitted):
    surface = fitted.surface
    if surface.constant is None:
        constructor = inspect.signature(type(surface.model).__init__).parameters
        parameters = {name: item.default for name, item in constructor.items()
                      if item.default is not inspect.Parameter.empty}
        parameters.update(variogram_model=surface.parameters.get("variogram_model", "spherical"),
            nlags=surface.parameters.get("nlags", 6), weight=surface.parameters.get("weight", False),
            coordinates_type="euclidean", exact_values=True, pseudo_inv=True, verbose=False,
            enable_plotting=False, enable_statistics=False)
        kriging = {"constructor": _parameter_value(parameters), "prediction": {
            "style": "points", "backend": "loop", "n_closest_points": min(surface.n, surface.parameters.get("n_closest_points", 12))},
            "fitted_variogram_parameters": _parameter_value(surface.model.variogram_model_parameters)}
        source = "base estimator get_params; PyKrige constructor defaults and explicit call arguments; fitted variogram parameters"
    else:
        kriging = {"constant_field": True, "constant_value": float(surface.constant), "variogram_fitted": False}
        source = "base estimator get_params; constant surface, no PyKrige estimator fitted"
    fitted.record.update(effective_parameters={"base_estimator": deepcopy(fitted.base.record["effective_parameters"]), "kriging": kriging},
        effective_estimator_class=type(fitted).__module__ + "." + type(fitted).__qualname__,
        effective_parameters_source=source,
        effective_parameters_scope="spatial workflow components; ordinary kriging base estimator is used for masking only",
        base_effective_estimator_class=fitted.base.record["effective_estimator_class"])
    fitted.record["fitted_model_attributes"] = {"status": "not_available", "reason": "kriging_has_no_global_regression_coefficients_or_feature_importances",
        "base_model_attributes": deepcopy(fitted.base.record["fitted_model_attributes"]) if fitted.record["model"]["id"] == "regression_kriging" else None,
        "scope": "base attributes describe only the regression component; fitted variogram is recorded separately in kriging"}
    return fitted


def make_point_estimator(model, seed=42):
    name = model["id"]
    params = deepcopy(model.get("parameters", {}))
    if name == 'tabpfn':
        from .tabpfn_d import make_estimator
        return make_estimator(params, seed)
    if name in ('tabm', 'realmlp'):
        from .models_d import make_estimator
        return make_estimator(name, params, seed)
    if name in ('lightgbm', 'catboost', 'cubist'):
        from .models_c import make_estimator
        return make_estimator(name, params, seed)
    factories = {"mean": lambda: DummyRegressor(strategy="mean"), "median": lambda: DummyRegressor(strategy="median"),
        "ridge": lambda: Ridge(alpha=1.), "elastic_net": lambda: ElasticNet(alpha=.1, l1_ratio=.5, max_iter=10000, random_state=seed),
        "random_forest": lambda: RandomForestRegressor(n_estimators=100, random_state=seed, n_jobs=1),
        "extra_trees": lambda: ExtraTreesRegressor(n_estimators=100, random_state=seed, n_jobs=1),
        "svr": lambda: SVR(), "hist_gradient_boosting": lambda: HistGradientBoostingRegressor(early_stopping=False, random_state=seed)}
    if name == "xgboost":
        from xgboost import XGBRegressor
        estimator = XGBRegressor(n_estimators=100, max_depth=6, objective="reg:squarederror", random_state=seed, n_jobs=1, device="cpu", tree_method="hist")
    elif name in factories:
        estimator = factories[name]()
    else:
        raise JobError("MODEL", "Desteklenmeyen regresyon modeli: " + name)
    if any(key in params for key in ("early_stopping_rounds", "eval_set", "callbacks")) or params.get("early_stopping", False) not in (False, None):
        raise JobError("EARLY_STOPPING", "Erken durdurma bu aşamada kapalıdır; yineleme sayısı iç katlarda seçilir.")
    if "n_jobs" in params and params["n_jobs"] != 1:
        raise JobError("MODEL_PARALLELISM", "Model içi işçi sayısı 1 olmalıdır; paralellik arama düzeyinde seçilir.")
    unknown = set(params) - set(estimator.get_params())
    if unknown:
        raise JobError("MODEL_PARAMETER", "Bilinmeyen model parametresi: " + sorted(unknown)[0])
    if name in ("mean", "median") and "strategy" in params:
        raise JobError("MODEL_PARAMETER", "Başvuru modelinin stratejisi model kimliğiyle sabittir.")
    if name == "xgboost" and (params.get("objective", "reg:squarederror") != "reg:squarederror" or params.get("device", "cpu") != "cpu"):
        raise JobError("MODEL_PARAMETER", "XGBoost bu aşamada CPU ve reg:squarederror kullanır.")
    estimator.set_params(**params)
    return estimator


class TargetMap:
    def __init__(self, config):
        self.config = deepcopy(config)
        self.method = config.get("method", "none")
        self.smearing = 1.
        self.transformer = None

    def fit_transform(self, y):
        y = np.asarray(y, dtype=float)
        if y.ndim != 1 or not np.isfinite(y).all():
            raise JobError("TARGET", "Hedef değerler sonlu olmalıdır; eksik hedefler doldurulamaz.")
        if self.method == "none":
            return y.copy()
        if self.method == "log":
            if np.any(y <= 0):
                raise JobError("LOG_TARGET", "Logaritmik dönüşüm için bütün eğitim hedefleri pozitif olmalıdır.")
            return np.log(y)
        if self.method == "yeo_johnson":
            if np.ptp(y) == 0:
                raise JobError("CONSTANT_TARGET_TRANSFORM", "Sabit hedef için Yeo-Johnson dönüşümü tanımlanmıyor.")
            self.transformer = PowerTransformer(method="yeo-johnson", standardize=True).fit(y[:, None])
            return self.transformer.transform(y[:, None])[:, 0]
        raise JobError("TARGET_TRANSFORM", "Desteklenmeyen hedef dönüşümü.")

    def inverse(self, values, *, point=False):
        values = np.asarray(values, dtype=float)
        shape = values.shape
        if self.method == "log":
            with np.errstate(over="ignore", invalid="ignore"):
                result = np.exp(values) * (self.smearing if point else 1.)
        elif self.transformer:
            result = self.transformer.inverse_transform(values.reshape(-1, 1)).reshape(shape)
        else:
            result = values.copy()
        if not np.isfinite(result).all():
            raise JobError("INVERSE_TRANSFORM", "Hedef ters dönüşümü sonlu tahmin üretmedi.")
        return result

    def record(self):
        result = {"method": self.method, "smearing_factor": float(self.smearing)}
        if self.method == "log":
            result["point_estimand"] = self.config.get("log_estimand", "median")
            result["assumption"] = "median requires zero conditional median log error; global smearing mean assumes transferable residual distribution"
        elif self.method == "yeo_johnson":
            result["lambda"] = float(self.transformer.lambdas_[0])
            result["point_estimand"] = "inverse_transformed_regression; not an unbiased conditional mean"
        return result


class FittedWorkflow:
    def __init__(self, pipeline, target_map, estimator, settings, record, quantile_estimators=None, quantile_forest=False):
        self.pipeline = pipeline
        self.target_map = target_map
        self.estimator = estimator
        self.settings = deepcopy(settings)
        self.record = record
        self.quantile_estimators = quantile_estimators
        self.quantile_forest = quantile_forest

    def predict(self, X):
        features = self.pipeline.transform(X)
        values = self.estimator.predict(features, quantiles="mean") if self.quantile_forest else self.estimator.predict(features)
        return self.target_map.inverse(np.asarray(values).reshape(-1), point=True)

    def _raw_interval(self, X, coverage=None):
        if not self.quantile_forest and self.quantile_estimators is None:
            return None
        nominal = float(self.settings.get("uncertainty", {}).get("coverage", .9))
        coverage = nominal if coverage is None else float(coverage)
        if not 0 < coverage < 1:
            raise JobError("COVERAGE_LEVEL", "Kapsama düzeyi 0 ile 1 arasında olmalıdır.")
        if self.quantile_estimators is not None and not np.isclose(coverage, nominal, rtol=0, atol=1e-12):
            raise JobError("QUANTILE_LEVEL_NOT_FITTED", "Kantil güçlendirme bu düzeyde eğitilmedi; yeniden eğitim olmadan aralık üretilemez.")
        features = self.pipeline.transform(X)
        alpha = 1 - coverage
        if self.quantile_forest:
            quantiles = self.estimator.predict(features, quantiles=[alpha / 2, 1 - alpha / 2])
            lower, upper = quantiles[:, 0], quantiles[:, 1]
        else:
            lower, upper = [estimator.predict(features) for estimator in self.quantile_estimators]
        lower, upper = self.target_map.inverse(lower), self.target_map.inverse(upper)
        return lower, upper

    def predict_interval(self, X):
        interval = self._raw_interval(X)
        if interval is None:
            return None
        lower, upper = interval
        # Fixed, explicitly recorded two-quantile rearrangement, independent of y.
        return np.minimum(lower, upper), np.maximum(lower, upper)

    def predict_interval_at_coverage(self, X, coverage):
        interval = self._raw_interval(X, coverage)
        if interval is None:
            raise JobError("NO_INTERVAL_METHOD", "Seçilen iş akışı tahmin aralığı üretmiyor.")
        lower, upper = interval
        return np.minimum(lower, upper), np.maximum(lower, upper)

    def quantile_crossings_at_coverage(self, X, coverage):
        interval = self._raw_interval(X, coverage)
        return np.zeros(len(X), dtype=bool) if interval is None else interval[0] > interval[1]

    def quantile_crossings(self, X):
        interval = self._raw_interval(X)
        return np.zeros(len(X), dtype=bool) if interval is None else interval[0] > interval[1]

    def diagnostics(self, X):
        result = self.pipeline.diagnostics(X)
        predictions = self.predict(X)
        limits = self.settings.get("mapping", {})
        violation = np.zeros(len(predictions), dtype=bool)
        if limits.get("physical_min") is not None:
            violation |= predictions < limits["physical_min"]
        if limits.get("physical_max") is not None:
            violation |= predictions > limits["physical_max"]
        result["physical_violation"] = violation
        result["quantile_crossing"] = self.quantile_crossings(X)
        return result


def fit_workflow(X, y, ids, coordinates, groups, covariates, settings, *, crs, role, learning_audit, check_cancel=lambda: None):
    if settings['model']['id'] in ('ordinary_kriging', 'regression_kriging'):
        from .spatial_models import fit_spatial_workflow
        fitted = fit_spatial_workflow(X, y, ids, coordinates, groups, covariates, settings,
            crs=crs, role=role, learning_audit=learning_audit, check_cancel=check_cancel)
        return _record_spatial_parameters(fitted)
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    coordinates, groups = np.asarray(coordinates), np.asarray(groups)
    if len(ids) != len(y) or X.shape != (len(y), len(covariates)) or len(set(ids)) != len(ids):
        raise JobError("FIT_SHAPE", "Eğitim kimlikleri, hedef ve değişken matrisi uyuşmuyor.")
    check_cancel()
    seed = int(settings.get("optimization", {}).get("seed", settings.get("validation", {}).get("seed", 42)))
    initial = None
    if settings.get("feature_selection", {}).get("method") == "sfs":
        initial = sequential_selection(X, y, ids, coordinates, groups, covariates, settings, crs=crs, seed=seed, role=role,
            learning_audit=learning_audit, check_cancel=check_cancel)
    target = TargetMap(settings.get("target_transform", {}))
    transformed = target.fit_transform(y)
    audit(learning_audit, role, "target_transform", ids, method=target.method)
    pipeline = FeaturePipeline(covariates, settings).fit(X, transformed, ids, seed=seed, role=role, learning_audit=learning_audit, initial=initial, check_cancel=check_cancel)
    features = pipeline.transform(X)
    uncertainty = settings.get("uncertainty", {})
    method = uncertainty.get("method", "none")
    quantile_method = uncertainty.get("quantile_model", "quantile_forest") if method == "cqr" else method
    quantile_forest = quantile_method == "quantile_forest"
    quantile_estimators = None
    estimator = make_point_estimator(settings["model"], seed)
    if quantile_forest:
        if settings["model"]["id"] != "random_forest":
            raise JobError("UNCERTAINTY_MODEL", "Kantil ormanı, rastgele orman model ailesi gerektirir.")
        from quantile_forest import RandomForestQuantileRegressor
        params = estimator.get_params()
        accepted = RandomForestQuantileRegressor().get_params()
        params = {k: v for k, v in params.items() if k in accepted}
        estimator = RandomForestQuantileRegressor(**params, default_quantiles="mean", max_samples_leaf=None)
    elif quantile_method == "quantile_boosting":
        if settings["model"]["id"] != "hist_gradient_boosting":
            raise JobError("UNCERTAINTY_MODEL", "Kantil güçlendirme, histogramla gradyan güçlendirme ailesi gerektirir.")
        alpha = 1 - float(uncertainty.get("coverage", .9))
        quantile_estimators = []
        for quantile in (alpha / 2, 1 - alpha / 2):
            parameters = estimator.get_params()
            parameters.update(loss="quantile", quantile=quantile, early_stopping=False)
            quantile_estimators.append(HistGradientBoostingRegressor(**parameters))
    check_cancel()
    if settings['model']['id'] in ('tabm', 'realmlp', 'tabpfn'):
        estimator.fit(features, transformed, check_cancel=check_cancel)
    else:
        estimator.fit(features, transformed)
    audit(learning_audit, role, "point_model", ids, model=settings["model"]["id"])
    for index, quantile_estimator in enumerate(quantile_estimators or []):
        check_cancel()
        quantile_estimator.fit(features, transformed)
        audit(learning_audit, role, "quantile_model", ids, quantile=index)
    if target.method == "log" and target.config.get("log_estimand", "median") == "mean_smearing":
        fitted = estimator.predict(features, quantiles="mean") if quantile_forest else estimator.predict(features)
        target.smearing = float(np.mean(np.exp(transformed - fitted)))
        if not np.isfinite(target.smearing):
            raise JobError("SMEARING", "Eğitim artıklarından sonlu düzeltme katsayısı hesaplanamadı.")
        audit(learning_audit, role, "log_smearing", ids)
    record = {**pipeline.record, "model": deepcopy(settings["model"]), "target_transform": target.record(), "training_ids": list(ids),
        "uncertainty_method": method, "early_stopping": False, "settings": deepcopy(settings),
        **_effective_estimator_record(estimator), "fitted_model_attributes": _fitted_model_attributes(estimator, pipeline, target)}
    if quantile_estimators:
        record["quantile_effective_parameters"] = [{"bound": bound, **_effective_estimator_record(item)}
            for bound, item in zip(("lower", "upper"), quantile_estimators)]
    if hasattr(estimator, 'fit_diagnostics_'):
        record['experimental_model_diagnostics'] = deepcopy(estimator.fit_diagnostics_)
    if quantile_forest:
        record["point_estimand"] = "mean_of_training_response_distribution_in_forest_leaves_on_transformed_scale"
        record["quantile_estimator"] = "quantile_forest.RandomForestQuantileRegressor; all leaf observations"
    if quantile_forest or quantile_estimators:
        record["crossing_policy"] = "sort"
        record["interval_nominal_coverage"] = float(uncertainty.get("coverage", .9))
        record["coverage_claim"] = "empirical_only; spatial dependence does not establish exchangeability"
    fitted = FittedWorkflow(pipeline, target, estimator, settings, record, quantile_estimators, quantile_forest)
    record["training_quantile_crossing_count"] = int(np.sum(fitted.quantile_crossings(X)))
    return fitted
