"""Explicit nested evaluation, conditional searches and trusted local persistence.

Outer and independent targets are read only after model selection and fitting.
For conformal methods, calibration IDs are removed before inner folds are built.
Repeated out-of-fold rows are retained with repeat IDs, not treated as independent
replicates. All metrics and conformity scores use the original target unit.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
from importlib.metadata import version, PackageNotFoundError
import itertools
import json
import math
from pathlib import Path
import platform
import time

import joblib
import numpy as np
from threadpoolctl import threadpool_limits

from .contract import Cancelled, JobError
from .modeling import fit_workflow, prediction_call, bind_runtime_callbacks
from .uncertainty import calibrate, diagnostic_levels, interval_metrics
from .validation import indices_for, make_folds, split_calibration


POINT_METRIC_KEYS = ("rmse", "mae", "mean_error", "r2", "mse", "median_absolute_error",
                     "residual_std", "pearson_r", "pearson_r2", "lin_ccc", "rpd", "rpiq",
                     "observed_std", "observed_iqr")


def regression_metrics(y, prediction):
    """Point metrics on the original scale, with explicit undefined values.

    Residuals are prediction minus observation. Standard deviations use n-1;
    Lin's concordance uses population moments (n). Quartiles use linear
    interpolation. Pearson r squared is distinct from 1-SSE/SST.
    """
    y, prediction = np.asarray(y, dtype=float), np.asarray(prediction, dtype=float)
    if y.ndim != 1 or prediction.shape != y.shape or not len(y) or not np.isfinite(np.concatenate([y, prediction])).all():
        raise JobError("METRICS", "Performance metrics require finite, matched observation-prediction pairs.")
    result = {"n": len(y), "reasons": {}}

    def value(name, number=None, reason=None):
        if reason is None and (number is None or not np.isfinite(number)):
            reason = "numerical_overflow"
        result[name] = None if reason else float(number)
        if reason:
            result["reasons"][name] = reason

    with np.errstate(over="ignore", invalid="ignore", divide="ignore", under="ignore"):
        error = prediction - y
        centered_y, centered_p = y - np.mean(y), prediction - np.mean(prediction)
        denominator = float(np.sum(centered_y ** 2))
        mse = np.mean(error ** 2)
        rmse = np.sqrt(mse)
        value("mse", mse)
        value("rmse", rmse)
        value("mae", np.mean(np.abs(error)))
        value("mean_error", np.mean(error))
        value("median_absolute_error", np.median(np.abs(error)))
        small = "fewer_than_two_observations" if len(y) < 2 else None
        r2_reason = small or ("constant_observed_target" if denominator == 0 else
                             "numerical_overflow" if not np.isfinite(denominator) else None)
        value("r2", None if r2_reason else 1 - np.sum(error ** 2) / denominator, r2_reason)
        value("residual_std", np.std(error, ddof=1) if not small else None, small)
        value("observed_std", np.std(y, ddof=1) if not small else None, small)
        quartiles = np.percentile(y, [25, 75], method="linear")
        value("observed_iqr", quartiles[1] - quartiles[0])
        var_y, var_p = np.mean(centered_y ** 2), np.mean(centered_p ** 2)
        covariance = np.mean(centered_y * centered_p)
        correlation_reason = small or ("constant_observed_target" if var_y == 0 else
                                      "constant_predictions" if var_p == 0 else
                                      "numerical_overflow" if not np.isfinite([var_y, var_p, covariance]).all() else None)
        correlation = None if correlation_reason else covariance / (np.sqrt(var_y) * np.sqrt(var_p))
        if correlation is not None and np.isfinite(correlation):
            correlation = np.clip(correlation, -1., 1.)
        value("pearson_r", correlation, correlation_reason)
        value("pearson_r2", None if result["pearson_r"] is None else result["pearson_r"] ** 2,
              result["reasons"].get("pearson_r"))
        ccc_denominator = var_y + var_p + (np.mean(y) - np.mean(prediction)) ** 2
        ccc_reason = small or ("zero_ccc_denominator" if ccc_denominator == 0 else
                              "numerical_overflow" if not np.isfinite(ccc_denominator) else None)
        value("lin_ccc", None if ccc_reason else 2 * covariance / ccc_denominator, ccc_reason)
        for name, numerator in (("rpd", result["observed_std"]), ("rpiq", result["observed_iqr"])):
            reason = small or ("zero_rmse" if rmse == 0 else None)
            value(name, None if reason or numerator is None or not np.isfinite(rmse) else numerator / rmse, reason)
    result["r2_reason"] = result["reasons"].get("r2")
    return result


def _point_rows(model, X, y, ids, groups, repeat, fold, coordinates, *, scope=None):
    """Point predictions only: calibration observations never request intervals."""
    predictions = np.asarray(prediction_call(model, 'predict', X, coordinates), dtype=float)
    if predictions.shape != (len(ids),) or not np.isfinite(predictions).all():
        raise JobError("METRICS", "Point predictions must be a finite vector matching the number of observations.")
    rows = [{"sample_id": identity, "group_id": str(groups[i]), "repeat": repeat, "fold": fold,
             "observed": float(y[i]), "predicted": float(predictions[i]), "residual": float(predictions[i] - y[i])}
            for i, identity in enumerate(ids)]
    if scope is not None:
        for row in rows:
            row["scope"] = scope
    return rows


def _point_metrics(rows):
    return regression_metrics([r["observed"] for r in rows], [r["predicted"] for r in rows]) if rows else None


def _row_metrics(rows, coverage):
    if not rows:
        return None
    y, prediction = [r["observed"] for r in rows], [r["predicted"] for r in rows]
    result = regression_metrics(y, prediction)
    if any("interval_status" in row for row in rows):
        result["interval"] = _interval_summary(rows, coverage)
    elif "lower" in rows[0]:
        result["interval"] = interval_metrics(y, [r["lower"] for r in rows], [r["upper"] for r in rows], coverage)
        result["interval"]["crossing_count"] = sum(r.get("quantile_crossing", False) for r in rows)
    return result


def _interval_summary(rows, coverage):
    """Keep unavailable observations in the denominator of availability, not coverage."""
    available, reasons = [], {}
    for row in rows:
        lower, upper = row.get("lower"), row.get("upper")
        if lower is None or upper is None:
            if lower is not None or upper is not None or row.get("interval_status") != "unavailable" or not row.get("reason_code"):
                raise JobError("INTERVAL", "A missing interval requires two empty bounds and an unavailable status with a reason.")
            reason = row["reason_code"]
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            if not np.isfinite([lower, upper]).all() or lower > upper or row.get("interval_status", "available") != "available":
                raise JobError("INTERVAL", "Available interval bounds must be finite and ordered.")
            available.append(row)
    n = len(rows)
    summary = {"nominal_coverage": float(coverage), "n": n, "available_n": len(available),
               "unavailable_n": n - len(available), "available_fraction": len(available) / n if n else None,
               "group_count": len({r["group_id"] for r in rows}),
               "available_group_count": len({r["group_id"] for r in available}),
               "distinct_sample_count": len({r["sample_id"] for r in rows}),
               "coverage_denominator": len(available), "availability_denominator": n, "reason_counts": reasons,
               "metrics_scope": "available_intervals_only; excluded targets not in coverage denominator"}
    values = None
    if available:
        values = {**interval_metrics([r["observed"] for r in available], [r["lower"] for r in available],
                                     [r["upper"] for r in available], coverage),
                  "crossing_count": sum(r.get("quantile_crossing", False) for r in available)}
    if n and len(available) == n:
        summary.update(status="available", **values)
    else:
        summary.update(status="partial" if available else "unavailable",
                       reason_code="PARTIAL_SPATIAL_SUPPORT" if available else "NO_SPATIAL_INTERVALS",
                       reason="Local support is not sufficient at all observations; interval metrics refer only to the available subset.")
        if values is not None:
            summary["available_subset"] = values
    return summary


def _spatial_interval_rows(model, X, y, ids, groups, repeat, fold, coverage, coordinates):
    from .spatial_uncertainty_d import spatial_interval_details, REASONS
    calibration = model.record["calibration"]
    if set(ids) & (set(calibration["ids"]) | set(calibration["training_ids"])) or set(map(str, groups)) & (set(calibration["groups"]) | set(calibration["training_groups"])):
        raise JobError("SPATIAL_EVALUATION_LEAKAGE", "Evaluation samples or groups overlap with training or calibration data.")
    details = spatial_interval_details(model, X, coordinates, coverage)
    available = np.asarray(details["interval_available"], dtype=bool)
    lower, upper = np.asarray(details["lower"]), np.asarray(details["upper"])
    if available.shape != (len(ids),) or lower.shape != available.shape or upper.shape != available.shape:
        raise JobError("INTERVAL", "The spatial interval mask and bound sizes do not match.")
    if not np.isfinite(lower[available]).all() or not np.isfinite(upper[available]).all() or np.any(lower[available] > upper[available]) or not np.isnan(lower[~available]).all() or not np.isnan(upper[~available]).all():
        raise JobError("INTERVAL", "A spatial interval may have two empty bounds only where the unavailable mask is set.")
    rows = []
    for i, identity in enumerate(ids):
        reason_code = str(details["interval_reason_code"][i])
        if (not available[i] and reason_code not in REASONS) or (available[i] and reason_code):
            raise JobError("INTERVAL", "The spatial interval status and the reason code do not match.")
        row = {"nominal_coverage": float(coverage), "repeat": repeat, "fold": fold,
               "sample_id": identity, "observed": float(y[i]), "group_id": str(groups[i]),
               "lower": float(lower[i]) if available[i] else None,
               "upper": float(upper[i]) if available[i] else None,
               "interval_width": float(upper[i] - lower[i]) if available[i] else None,
               "interval_status": "available" if available[i] else "unavailable",
               "reason_code": reason_code or None, "reason": REASONS.get(reason_code),
               "quantile_crossing": False}
        for key in ("interval_effective_n", "interval_effective_group_n", "interval_group_count", "interval_weight_sum"):
            row[key] = int(details[key][i]) if key == "interval_group_count" else float(details[key][i])
        rows.append(row)
    return rows


def _rows(model, X, y, ids, groups, repeat, fold, coordinates=None):
    predictions = prediction_call(model, 'predict', X, coordinates)
    spatial = model.record.get("uncertainty_method") == "spatial_conformal"
    spatial_rows = _spatial_interval_rows(model, X, y, ids, groups, repeat, fold, model.record["calibration"]["coverage"], coordinates) if spatial else None
    intervals = prediction_call(model, 'predict_interval', X, coordinates) if not spatial else None
    crossings = prediction_call(model, 'quantile_crossings', X, coordinates) if intervals is not None else None
    plain = prediction_call(model, 'plain_predict', X, coordinates) if model.record.get('model',{}).get('id')=='regression_kriging' else None
    diag=prediction_call(model,'diagnostics',X,coordinates) if model.record.get('applicability') else {}
    result = []
    for i, sample_id in enumerate(ids):
        row = {"sample_id": sample_id, "repeat": repeat, "fold": fold, "observed": float(y[i]), "predicted": float(predictions[i]),
            "residual": float(predictions[i] - y[i]), "group_id": str(groups[i])}
        if plain is not None:row['plain_predicted']=float(plain[i])
        if 'applicability' in diag:row.update(applicability=bool(diag['applicability'][i]),dissimilarity=float(diag['dissimilarity'][i]))
        if spatial_rows is not None:
            row.update(spatial_rows[i])
        if intervals is not None:
            lower, upper = float(intervals[0][i]), float(intervals[1][i])
            row.update(lower=lower, upper=upper, interval_width=upper - lower, quantile_crossing=bool(crossings[i]))
        result.append(row)
    return result


def _coverage_fold(model, X, y, ids, groups, repeat, fold, levels, coordinates=None):
    """Evaluate pre-existing intervals; never pass held-out y into an estimator."""
    summaries, predictions = [], []
    for level in levels:
        if model.record.get("uncertainty_method") == "spatial_conformal":
            rows = _spatial_interval_rows(model, X, y, ids, groups, repeat, fold, level, coordinates)
            summaries.append({"repeat": repeat, "fold": fold, **_interval_summary(rows, level)})
            predictions.extend(rows)
            continue
        row = {"nominal_coverage": level, "repeat": repeat, "fold": fold, "n": len(y),
            "group_count": len(set(map(str, groups))), "distinct_sample_count": len(set(ids))}
        try:
            lower, upper = prediction_call(model, 'predict_interval_at_coverage', X, coordinates, level)
            crossings = prediction_call(model, 'quantile_crossings_at_coverage', X, coordinates, level)
            row.update(status="available", **interval_metrics(y, lower, upper, level), crossing_count=int(np.sum(crossings)))
            predictions.extend({"nominal_coverage": level, "repeat": repeat, "fold": fold, "sample_id": sample_id,
                "observed": float(y[i]), "lower": float(lower[i]), "upper": float(upper[i]), "group_id": str(groups[i]),
                "quantile_crossing": bool(crossings[i])} for i, sample_id in enumerate(ids))
        except JobError as exc:
            row.update(status="unavailable", reason_code=exc.code, reason=str(exc))
        summaries.append(row)
    return summaries, predictions


def _aggregate_coverage(summaries, predictions, levels, observations):
    result = []
    for level in levels:
        folds = [row for row in summaries if row["nominal_coverage"] == level]
        rows = [row for row in predictions if row["nominal_coverage"] == level]
        if rows and any("interval_status" in row for row in rows):
            if len(rows) != sum(row["n"] for row in folds):
                raise JobError("INTERVAL", "The number of spatial interval observations does not match the fold total.")
            result.append({**_interval_summary(rows, level), "fold_count": len(folds),
                "available_fold_count": sum(row["status"] == "available" for row in folds),
                "partial_fold_count": sum(row["status"] == "partial" for row in folds),
                "unavailable_folds": [{"repeat": row["repeat"], "fold": row["fold"], "reason_code": row["reason_code"]}
                                      for row in folds if row["status"] != "available"]})
            continue
        unavailable = [row for row in folds if row["status"] != "available"]
        summary = {"nominal_coverage": level, "n": sum(row["n"] for row in folds), "fold_count": len(folds),
            "available_fold_count": len(folds) - len(unavailable), "group_count": len({row["group_id"] for row in observations}),
            "distinct_sample_count": len({row["sample_id"] for row in observations})}
        if not folds or unavailable:
            summary.update(status="unavailable",
                reason_code="INCOMPLETE_COVERAGE_LEVEL" if folds else "NO_EVALUATION_ROWS",
                reason="This level could not be produced in all evaluation folds; partial results were not combined." if folds else "There are no evaluation observations in this scope.",
                reason_codes=sorted({row["reason_code"] for row in unavailable}) if folds else ["NO_EVALUATION_ROWS"],
                unavailable_folds=[{"repeat": row["repeat"], "fold": row["fold"], "reason_code": row["reason_code"]} for row in unavailable])
        else:
            summary.update(status="available", group_count=len({row["group_id"] for row in rows}), distinct_sample_count=len({row["sample_id"] for row in rows}),
                **interval_metrics([row["observed"] for row in rows], [row["lower"] for row in rows], [row["upper"] for row in rows], level),
                crossing_count=sum(row["quantile_crossing"] for row in rows))
        result.append(summary)
    return result


_SHARED_FIELDS = {"preprocessing.imputation", "preprocessing.scaling", "preprocessing.knn_neighbors", "feature_selection.method", "feature_selection.k",
    "feature_selection.threshold", "vif.mode", "vif.threshold", "target_transform.method", "target_transform.log_estimand"}


def _selection_arms(settings):
    selection = settings.get("feature_selection", {})
    method = selection.get("method", "none")
    compare = selection.get("compare_without_selection", False)
    if type(compare) is not bool:
        raise JobError("SELECTION_COMPARISON", "The comparison without selection must be a Boolean value.")
    if compare and (method == "none" or "feature_selection.method" in settings.get("optimization", {}).get("spaces", {})):
        raise JobError("SELECTION_COMPARISON", "The comparison without selection requires a single selected method; a method search space cannot be used.")
    return [method, "none"] if compare else [method]


def _spaces(settings, model_id, selection_method=None):
    candidates = settings.get("model_candidates") or [settings["model"]]
    family_ids = {model["id"] for model in candidates}
    result = {}
    for name, spec in settings.get("optimization", {}).get("spaces", {}).items():
        if not isinstance(spec, dict) or set(spec) - {"type", "choices", "low", "high", "log"}:
            raise JobError("SEARCH_SPACE", "Search space contains an unrecognized key: " + name)
        if name in _SHARED_FIELDS:
            if selection_method == "none" and settings.get("feature_selection", {}).get("compare_without_selection") and name.startswith(("feature_selection.", "vif.")):
                continue
            result[name] = spec
        elif "." in name:
            family, parameter = name.split(".", 1)
            if family not in family_ids:
                raise JobError("SEARCH_SPACE", "Unknown conditional model name: " + family)
            if family == model_id:
                result["model.parameters." + parameter] = spec
        elif len(candidates) == 1:
            result["model.parameters." + name] = spec
        else:
            raise JobError("SEARCH_SPACE", "With multiple families, a model parameter must start with the family name: " + name)
    return result


def _apply(settings, model, params, selection_method=None):
    result = deepcopy(settings)
    result["model"] = deepcopy(model)
    if selection_method is not None:
        result.setdefault("feature_selection", {})["method"] = selection_method
    for name, value in params.items():
        if isinstance(value, np.generic):
            value = value.item()
        if name.startswith("model.parameters."):
            result["model"].setdefault("parameters", {})[name[len("model.parameters."):]] = value
        else:
            section, key = name.split(".", 1)
            result.setdefault(section, {})[key] = value
    if selection_method == "none" and settings.get("feature_selection", {}).get("compare_without_selection"):
        # The comparator retains all raw variables, including those that the
        # selected workflow's iterative VIF stage would otherwise remove.
        result["feature_selection"] = {"method": "none", "compare_without_selection": True}
        result["vif"] = {"mode": "diagnostic", "threshold": settings.get("vif", {}).get("threshold", 10.), "protected_ids": [], "priority_ids": []}
    return result


def _grid_values(spec, budget):
    kind = spec.get("type")
    if kind == "categorical":
        values = spec.get("choices", [])
        if not values:
            raise JobError("SEARCH_SPACE", "Categorical search choices cannot be empty.")
        return values
    if kind not in ("int", "float") or "low" not in spec or "high" not in spec or spec["low"] > spec["high"]:
        raise JobError("SEARCH_SPACE", "Numeric search bounds are invalid.")
    low, high = spec["low"], spec["high"]
    if spec.get("log") and low <= 0:
        raise JobError("SEARCH_SPACE", "The lower bound of a logarithmic search must be positive.")
    count = min(max(2, budget), int(high - low + 1)) if kind == "int" else max(2, min(5, budget))
    values = np.geomspace(low, high, count) if spec.get("log") else np.linspace(low, high, count)
    return sorted(set(int(round(x)) if kind == "int" else float(x) for x in values))


def _random_value(spec, rng):
    _grid_values(spec, 2)  # validate the domain, independently of the sampler
    if spec["type"] == "categorical":
        return spec["choices"][int(rng.integers(len(spec["choices"])))]
    low, high = spec["low"], spec["high"]
    if spec.get("log"):
        value = float(np.exp(rng.uniform(np.log(low), np.log(high))))
        return int(np.clip(round(value), low, high)) if spec["type"] == "int" else value
    return int(rng.integers(low, high + 1)) if spec["type"] == "int" else float(rng.uniform(low, high))


def _candidate_list(settings):
    opt = settings.get("optimization", {})
    models = settings.get("model_candidates") or [settings["model"]]
    if len({m["id"] for m in models}) != len(models):
        raise JobError("MODEL_CANDIDATES", "The same family cannot be repeated among model candidates; use the parameter search space.")
    method = opt.get("method", "manual")
    budget = int(opt.get("trials", 10))
    combinations = [(model, arm) for model in models for arm in _selection_arms(settings)]
    if method != "manual" and budget < len(combinations):
        raise JobError("SEARCH_BUDGET", "The number of trials cannot be less than the number of model family and selection arm combinations.")
    if method == "manual":
        if opt.get("spaces"):
            raise JobError("SEARCH_SPACE", "Manual search cannot use a parameter search space.")
        return [(model, {}, arm) for model, arm in combinations]
    if method == "grid":
        per_family = []
        for model, arm in combinations:
            spaces = _spaces(settings, model["id"], arm)
            keys = list(spaces)
            products = itertools.product(*[_grid_values(spaces[key], budget) for key in keys])
            per_family.append([(model, dict(zip(keys, values)), arm) for values in itertools.islice(products, budget)])
        # Round-robin ensures every declared family receives a first trial.
        return [candidate for row in itertools.zip_longest(*per_family) for candidate in row if candidate is not None][:budget]
    if method == "random":
        rng = np.random.default_rng(opt.get("seed", 42))
        return [(combinations[i % len(combinations)][0],
                 {name: _random_value(spec, rng) for name, spec in _spaces(settings, combinations[i % len(combinations)][0]["id"], combinations[i % len(combinations)][1]).items()},
                 combinations[i % len(combinations)][1]) for i in range(budget)]
    raise JobError("OPTIMIZATION", "Unsupported search method.")


def _search(X, y, ids, coordinates, groups, covariates, settings, *, crs, role, audit, trials, check_cancel):
    folds = make_folds(ids, coordinates, groups, settings["validation"], crs=crs, inner=True,check_cancel=check_cancel)
    opt = settings.get("optimization", {})
    metric = opt.get("metric", "rmse")
    if metric not in ("rmse", "mae"):
        raise JobError("SEARCH_METRIC", "The inner search metric must be rmse or mae.")
    start = time.monotonic()
    timeout = float(opt.get("timeout_seconds", 300))
    n_jobs = int(opt.get("n_jobs", 1))
    if n_jobs < 1:
        raise JobError("SEARCH_JOBS", "The number of search workers must be positive.")
    def evaluate(number, model, params, selection_method):
        check_cancel()
        trial_start = time.monotonic()
        config = _apply(settings, model, params, selection_method)
        actual_selection = config.get("feature_selection", {}).get("method", "none")
        record = {"role": role, "trial": int(number), "model": deepcopy(config["model"]), "parameters": deepcopy(params), "metric": metric,
            "feature_selection_method": actual_selection,
            "vif_mode": config.get("vif", {}).get("mode", "off"),
            "comparison_scope": "selector_and_vif_elimination" if settings.get("feature_selection", {}).get("compare_without_selection") else "not_requested",
            "comparison_arm": ("without_selection" if actual_selection == "none" else "with_selection") if settings.get("feature_selection", {}).get("compare_without_selection") else "not_requested",
            "inner_folds": [{"repeat": f["repeat"], "fold": f["fold"], "train_ids": f["train_ids"], "test_ids": f["test_ids"]} for f in folds],
            "validation_predictions": [], "validation_metrics": None, "validation_per_repeat": [], "inner_fold_metrics": [],
            "validation_status": "incomplete", "validation_reason": "inner_folds_not_completed",
            "validation_expected_rows": sum(len(f["test_ids"]) for f in folds),
            "validation_scope": "inner_heldout_predictions; hyperparameter_selection; not independent model assessment"}
        errors = []
        try:
            for fold in folds:
                check_cancel()
                train, test = indices_for(ids, fold["train_ids"]), indices_for(ids, fold["test_ids"])
                fit = fit_workflow(X[train], y[train], [ids[i] for i in train], coordinates[train], groups[train], covariates, config, crs=crs,
                    role=role + "/trial%d/inner%d/%d" % (number, fold["repeat"], fold["fold"]), learning_audit=audit, check_cancel=check_cancel)
                rows = _point_rows(fit, X[test], y[test], [ids[i] for i in test], groups[test],
                                   fold["repeat"], fold["fold"], coordinates[test])
                errors.extend(row["residual"] for row in rows)
                record["validation_predictions"].extend(rows)
                inner_record = {"repeat": fold["repeat"], "fold": fold["fold"], "metrics": _point_metrics(rows),
                    "training_sample_count": len(train), "training_group_count": len(set(map(str, groups[train]))),
                    "test_sample_count": len(test), "test_group_count": len(set(map(str, groups[test])))}
                for key in ("model", "selected_features", "target_transform", "effective_estimator_class", "effective_parameters",
                            "effective_parameters_scope", "effective_parameters_source", "quantile_effective_parameters", "fitted_model_attributes",
                            "experimental_model_diagnostics"):
                    if key in fit.record:
                        inner_record[key] = deepcopy(fit.record[key])
                record["inner_fold_metrics"].append(inner_record)
            loss = float(np.mean(np.abs(errors))) if metric == "mae" else float(np.sqrt(np.mean(np.square(errors))))
            if not np.isfinite(loss):
                raise JobError("SEARCH_LOSS", "The inner search loss is not finite.")
            record.update(status="complete", value=loss, validation_status="complete", validation_reason=None,
                validation_metrics=_point_metrics(record["validation_predictions"]),
                validation_per_repeat=[{"repeat": repeat, **_point_metrics([r for r in record["validation_predictions"] if r["repeat"] == repeat])}
                    for repeat in sorted({r["repeat"] for r in record["validation_predictions"]})])
        except Cancelled:
            raise
        except (JobError, ValueError, FloatingPointError) as exc:
            loss = float("inf")
            record.update(status="failed", value=None, error=str(exc), validation_reason=getattr(exc, "code", type(exc).__name__))
        record["validation_completed_folds"] = len(record["inner_fold_metrics"])
        record["elapsed_seconds"] = float(time.monotonic() - trial_start)
        record["search_elapsed_seconds"] = float(time.monotonic() - start)
        trials.append(record)
        return loss, int(number), config
    method = opt.get("method", "manual")
    stop_reason = "candidate_or_trial_budget_exhausted"
    if method == "optuna":
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
        models = settings.get("model_candidates") or [settings["model"]]
        arms = _selection_arms(settings)
        if int(opt.get("trials", 10)) < len(models) * len(arms):
            raise JobError("SEARCH_BUDGET", "The number of trials cannot be less than the number of model family and selection arm combinations.")
        sampler = optuna.samplers.TPESampler(seed=int(opt.get("seed", 42)))
        study = optuna.create_study(direction="minimize", sampler=sampler)
        lookup = {model["id"]: model for model in models}
        results = []
        # Initial trials ensure every requested family is evaluated internally.
        for model in models:
            for arm in arms:
                study.enqueue_trial({"model_family": model["id"], "selection_method": arm})
        def objective(trial):
            model_id = trial.suggest_categorical("model_family", list(lookup))
            arm = trial.suggest_categorical("selection_method", arms)
            parameters = {}
            for name, spec in _spaces(settings, model_id, arm).items():
                _grid_values(spec, 2)
                key = model_id + ":" + name
                if spec["type"] == "categorical":
                    value = trial.suggest_categorical(key, spec["choices"])
                elif spec["type"] == "int":
                    value = trial.suggest_int(key, spec["low"], spec["high"], log=spec.get("log", False))
                else:
                    value = trial.suggest_float(key, spec["low"], spec["high"], log=spec.get("log", False))
                parameters[name] = value
            result = evaluate(trial.number, lookup[model_id], parameters, arm)
            results.append(result)
            return result[0]
        study.optimize(objective, n_trials=int(opt.get("trials", 10)), timeout=timeout, n_jobs=n_jobs)
        if len(results) < int(opt.get("trials", 10)) and time.monotonic() - start >= timeout:
            stop_reason = "timeout_between_trials"
        for record in trials:
            if record["role"] == role:
                trial = study.trials[record["trial"]]
                record["study_trial"] = {"schema_version": 1, "study_name": study.study_name, "state": trial.state.name,
                    "params": trial.params, "distributions": {name: optuna.distributions.distribution_to_json(distribution) for name, distribution in trial.distributions.items()},
                    "datetime_start": trial.datetime_start.isoformat() if trial.datetime_start else None,
                    "datetime_complete": trial.datetime_complete.isoformat() if trial.datetime_complete else None,
                    "purpose": "versioned_history_export; not a resumable optimizer checkpoint"}
                record["search"] = {"method": "optuna", "sampler": "TPESampler", "seed": opt.get("seed", 42), "n_jobs": n_jobs,
                    "optuna_version": version("optuna"), "reproducible_schedule": n_jobs == 1}
    else:
        candidates = _candidate_list(settings)
        results = []
        if n_jobs == 1:
            for number, (model, params, arm) in enumerate(candidates):
                if results and time.monotonic() - start >= timeout:
                    stop_reason = "timeout_between_trials"
                    break
                results.append(evaluate(number, model, params, arm))
        else:
            # Bounded batches, no unbounded queued futures or nested estimator jobs.
            with ThreadPoolExecutor(max_workers=n_jobs) as executor:
                for begin in range(0, len(candidates), n_jobs):
                    if results and time.monotonic() - start >= timeout:
                        stop_reason = "timeout_between_trials"
                        break
                    futures = [executor.submit(evaluate, number, *candidates[number]) for number in range(begin, min(begin + n_jobs, len(candidates)))]
                    results.extend(future.result() for future in futures)
        for record in trials:
            if record["role"] == role:
                record["search"] = {"method": method, "seed": opt.get("seed", 42), "n_jobs": n_jobs,
                    "grid_rule": "categorical choices; numeric equally spaced or logarithmic axes; at most trial budget per axis" if method == "grid" else None}
    successful = [result for result in results if np.isfinite(result[0])]
    records = [trial for trial in trials if trial["role"] == role]
    if settings.get("feature_selection", {}).get("compare_without_selection"):
        expected = {(model["id"], arm) for model in (settings.get("model_candidates") or [settings["model"]]) for arm in _selection_arms(settings)}
        attempted = {(record["model"]["id"], record["feature_selection_method"]) for record in records}
        if not expected <= attempted:
            raise JobError("SEARCH_COMPARISON_INCOMPLETE", "Because of the time limit, not all comparison arms with and without selection could be tried.")
    if not successful:
        failures = [t.get("error", "") for t in trials if t["role"] == role]
        raise JobError("SEARCH_FAILED", "No successful candidate in the inner search. " + (failures[0] if failures else "The time or trial limit was reached."))
    best = min(successful, key=lambda row: (row[0], row[1]))
    for record in trials:
        if record["role"] == role:
            record["selected"] = record["trial"] == best[1]
            record["timeout_policy"] = "between_trials; running estimator fit is not interrupted"
            record["search_summary"] = {"completed_trials": sum(r["status"] == "complete" for r in records),
                "failed_trials": sum(r["status"] == "failed" for r in records), "elapsed_seconds": float(time.monotonic() - start),
                "stop_reason": stop_reason, "tie_breaker": "lowest_trial_number"}
    return best[2]


def _fit_partition(X, y, ids, coordinates, groups, covariates, settings, *, crs, role, audit, trials, check_cancel):
    method = settings.get("uncertainty", {}).get("method", "none")
    if method in ("split_conformal", "cqr", "spatial_conformal"):
        development_ids, calibration_ids = split_calibration(ids, coordinates, groups, settings["validation"],
            settings["uncertainty"].get("calibration_fraction", .25), crs=crs, seed=settings["validation"].get("seed", 42))
    else:
        development_ids, calibration_ids = sorted(ids), []
    development = indices_for(ids, development_ids)
    calibration = indices_for(ids, calibration_ids)
    selected_settings = _search(X[development], y[development], development_ids, coordinates[development], groups[development], covariates, settings,
        crs=crs, role=role + "/search", audit=audit, trials=trials, check_cancel=check_cancel)
    check_cancel()
    model = fit_workflow(X[development], y[development], development_ids, coordinates[development], groups[development], covariates, selected_settings,
        crs=crs, role=role + "/fit", learning_audit=audit, check_cancel=check_cancel)
    if calibration_ids:
        check_cancel()
        model = calibrate(model, X[calibration], y[calibration], calibration_ids, selected_settings["uncertainty"], role=role + "/calibration", learning_audit=audit, coordinates=coordinates[calibration],
            groups=groups[calibration], training_ids=development_ids, training_groups=groups[development],
            training_coordinates=coordinates[development], crs=crs, validation=selected_settings["validation"])
    if settings.get('diagnostics',{}).get('applicability'):
        from .applicability import attach_applicability
        threshold_folds=make_folds(development_ids,coordinates[development],groups[development],selected_settings['validation'],crs=crs,inner=True,check_cancel=check_cancel)
        model=attach_applicability(model,X[development],development_ids,threshold_folds,check_cancel)
        from .pipeline import audit as append_audit
        append_audit(audit,role,'applicability',development_ids)
    bind_runtime_callbacks(model,check_cancel)
    check_cancel()
    training_rows = _point_rows(model, X[development], y[development], development_ids, groups[development], None, None,
                               coordinates[development], scope="resubstitution")
    calibration_rows = _point_rows(model, X[calibration], y[calibration], calibration_ids, groups[calibration], None, None,
                                  coordinates[calibration], scope="calibration_diagnostic") if calibration_ids else []
    check_cancel()
    record = {**model.record, "development_ids": development_ids, "calibration_ids": calibration_ids,
        "excluded_ids": sorted(set(ids) - set(development_ids) - set(calibration_ids)),
        "train_metrics": _point_metrics(training_rows), "train_metrics_scope": "resubstitution; not validation",
        "training_predictions": training_rows, "training_predictions_scope": "resubstitution; not validation",
        "calibration_predictions": calibration_rows, "calibration_predictions_scope": "calibration_diagnostic; not independent validation",
        "calibration_metrics": _point_metrics(calibration_rows), "calibration_metrics_scope": "calibration_diagnostic; not independent validation"}
    record["selected_comparison_arm"] = ("without_selection" if selected_settings.get("feature_selection", {}).get("method") == "none" else "with_selection") if settings.get("feature_selection", {}).get("compare_without_selection") else "not_requested"
    record["comparison_scope"] = "selector_and_vif_elimination" if settings.get("feature_selection", {}).get("compare_without_selection") else "not_requested"
    if settings['validation']['method']=='temporal':
        dates=settings['validation']['dates_by_id']
        def bounds(values):
            return {'start':min(dates[i] for i in values),'end':max(dates[i] for i in values)} if values else None
        record['temporal']={'timezone':settings['validation'].get('timezone','UTC'),
            'gap_days':settings['validation'].get('gap_days',0),
            'development_dates':bounds(development_ids),'calibration_dates':bounds(calibration_ids),
            'last_used_date':max(dates[i] for i in development_ids+calibration_ids),
            'requested_calibration_group_fraction':settings['uncertainty'].get('calibration_fraction') if calibration_ids else None,
            'actual_calibration_sample_fraction':len(calibration_ids)/len(ids),
            'actual_calibration_group_fraction':len(set(groups[calibration]))/len(set(groups)),
            'denominator':'partition_before_calendar_and_spatial_exclusions',
            'development_samples':len(development_ids),'development_groups':len(set(groups[development])),
            'calibration_samples':len(calibration_ids),'calibration_groups':len(set(groups[calibration])),
            'excluded_samples':len(record['excluded_ids']),
            'scope':'new groups at later dates; fixed covariate rasters; not a forecast map for a specified future date'}
    model.record = record
    return model, record


def evaluate_nested(X, y, ids, coordinates, groups, covariates, settings, *, crs, check_cancel, progress):
    X, y = np.asarray(X, dtype=float), np.asarray(y, dtype=float)
    coordinates, groups = np.asarray(coordinates), np.asarray(groups)
    if settings.get("uncertainty", {}).get("method") == "spatial_conformal" and settings.get("optimization", {}).get("n_jobs", 1) != 1:
        raise JobError("SPATIAL_PARALLELISM", "With experimental spatial calibration, the number of search workers must be 1.")
    if y.ndim != 1 or not np.isfinite(y).all() or X.shape != (len(ids), len(covariates)) or len(y) != len(ids):
        raise JobError("EVALUATION_INPUT", "The evaluation matrix, target and IDs do not match.")
    folds = make_folds(ids, coordinates, groups, settings["validation"], crs=crs,check_cancel=check_cancel)
    oof, fold_metrics, trials, learning_audit = [], [], [], []
    permutation_folds=[]
    coverage = float(settings.get("uncertainty", {}).get("coverage", .9))
    levels = diagnostic_levels(coverage)
    coverage_folds, coverage_predictions = [], []
    with threadpool_limits(limits=1):
        for number, fold in enumerate(folds):
            check_cancel()
            role = "outer/%d/%d" % (fold["repeat"], fold["fold"])
            train, test = indices_for(ids, fold["train_ids"]), indices_for(ids, fold["test_ids"])
            model, record = _fit_partition(X[train], y[train], [ids[i] for i in train], coordinates[train], groups[train], covariates, settings,
                crs=crs, role=role, audit=learning_audit, trials=trials, check_cancel=check_cancel)
            check_cancel()
            rows = _rows(model, X[test], y[test], [ids[i] for i in test], groups[test], fold["repeat"], fold["fold"], coordinates[test])
            if settings.get('diagnostics',{}).get('permutation'):
                from .diagnostics_c import heldout_permutation_importance
                permutation_folds.append(heldout_permutation_importance(model,X[test],y[test],[ids[i] for i in test],groups[test],covariates,
                    repeat=fold['repeat'],fold=fold['fold'],coordinates=coordinates[test],training_ids=record['development_ids'],
                    calibration_ids=record['calibration_ids'],training_groups=groups[train],
                    n_permutations=settings['diagnostics'].get('permutation_repeats',5),seed=settings['validation'].get('seed',42),
                    metric=settings.get('optimization',{}).get('metric','rmse'),check_cancel=check_cancel))
            oof.extend(rows)
            level_summaries, level_predictions = _coverage_fold(model, X[test], y[test], [ids[i] for i in test], groups[test], fold["repeat"], fold["fold"], levels, coordinates[test])
            coverage_folds.extend(level_summaries)
            coverage_predictions.extend(level_predictions)
            fold_metrics.append({"repeat": fold["repeat"], "fold": fold["fold"], "metrics": _row_metrics(rows, coverage), "model_record": record})
            progress("evaluation", number + 1, len(folds) + 1, "Outer fold %d/%d completed." % (number + 1, len(folds)))
        independent_ids = settings["validation"].get("independent_ids", [])
        development_ids = sorted(set(ids) - set(independent_ids))
        development, independent = indices_for(ids, development_ids), indices_for(ids, independent_ids)
        final_model, final_record = _fit_partition(X[development], y[development], development_ids, coordinates[development], groups[development], covariates, settings,
            crs=crs, role="final", audit=learning_audit, trials=trials, check_cancel=check_cancel)
        independent_rows = _rows(final_model, X[independent], y[independent], [ids[i] for i in independent], groups[independent], None, None, coordinates[independent]) if len(independent) else []
        independent_coverage, independent_coverage_predictions = _coverage_fold(final_model, X[independent], y[independent], [ids[i] for i in independent], groups[independent], None, None, levels, coordinates[independent]) if len(independent) else ([], [])
    per_repeat = [{"repeat": repeat, **_row_metrics([row for row in oof if row["repeat"] == repeat], coverage)} for repeat in sorted({row["repeat"] for row in oof})]
    fold_mean = {}
    for key in POINT_METRIC_KEYS:
        values = [row["metrics"][key] for row in fold_metrics if row["metrics"][key] is not None]
        fold_mean[key] = float(np.mean(values)) if values else None
        fold_mean[key + "_defined_folds"] = len(values)
    metrics = {"pooled_oof": _row_metrics(oof, coverage), "per_repeat": per_repeat, "fold_mean": fold_mean,
        "independent": _row_metrics(independent_rows, coverage), "residual_sign": "prediction_minus_observation",
        "pooled_scope": "all repeat-specific predictions; repeated sample IDs are not independent replicates"}
    final_record["independent_ids"] = list(independent_ids)
    final_record["selection_scope"] = "inner_validation_only; outer and independent metrics do not select final family"
    coverage_diagnostics = {"levels": levels, "outer_folds": coverage_folds,
        "pooled_oof": _aggregate_coverage(coverage_folds, coverage_predictions, levels, oof),
        "per_repeat": [{"repeat": repeat, **row} for repeat in sorted({fold["repeat"] for fold in folds})
            for row in _aggregate_coverage([r for r in coverage_folds if r["repeat"] == repeat], [r for r in coverage_predictions if r["repeat"] == repeat], levels,
                [r for r in oof if r["repeat"] == repeat])],
        "independent": _aggregate_coverage(independent_coverage, independent_coverage_predictions, levels, independent_rows),
        "scope": "fixed fitted models; same separate calibration observations; no refit; empirical coverage, not a spatial guarantee",
        "predictions": coverage_predictions, "independent_predictions": independent_coverage_predictions}
    check_cancel()
    progress("evaluation", len(folds) + 1, len(folds) + 1, "Nested evaluation and final model completed.")
    comparison = {}
    if oof and all('plain_predicted' in r for r in oof):
        comparison = {'scope':'same fitted base; hybrid settings; no independent baseline tuning; no significance test',
            'pooled_oof_plain':regression_metrics([r['observed'] for r in oof],[r['plain_predicted'] for r in oof]),
            'pooled_oof_hybrid':metrics['pooled_oof'],
            'per_repeat':[{'repeat':rep,'plain':regression_metrics([r['observed'] for r in oof if r['repeat']==rep],
                [r['plain_predicted'] for r in oof if r['repeat']==rep])} for rep in sorted({r['repeat'] for r in oof})]}
    from .diagnostics_c import aggregate_permutation_importance
    permutation=aggregate_permutation_importance(permutation_folds) if permutation_folds else {'status':'not_requested','folds':[],'per_repeat':[]}
    return {"oof": oof, "fold_metrics": fold_metrics, "metrics": metrics, "trials": sorted(trials, key=lambda r: (r["role"], r["trial"])),
        "learning_audit": learning_audit, "folds": folds, "independent_predictions": independent_rows, "final_model": final_model, "final_record": final_record,
        "coverage_diagnostics": coverage_diagnostics, 'plain_model_comparison':comparison,'permutation_importance':permutation}


def _versions():
    result={"python": platform.python_version(), **{name: version(name) for name in ("numpy", "scipy", "scikit-learn", "joblib", "xgboost", "quantile-forest", "Boruta", "optuna", "lightgbm", "catboost", "cubist", "PyKrige")}}
    for name in ('torch','tabm','pytabkit','tabpfn','tabpfn-common-utils'):
        try:result[name]=version(name)
        except PackageNotFoundError:pass
    return result


def _schema(covariates):
    # Ordered full declared covariate metadata, including bands, units and source.
    return json.loads(json.dumps(covariates, ensure_ascii=False, sort_keys=True, allow_nan=False))


def save_trusted_model(model, path, run_id, covariates):
    path = Path(path)
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    if path.exists() or meta_path.exists():
        raise JobError("MODEL_EXISTS", "The model file cannot be overwritten.")
    joblib.dump(model, path, compress=3)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    metadata = {"schema_version": 1, "run_id": run_id, "sha256": digest, "covariates": _schema(covariates), "versions": _versions(),
        "trust_scope": "only the current run's newly generated local artifact; never an imported pickle"}
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return {"path": str(path), "metadata_path": str(meta_path), "sha256": digest}


def load_trusted_model(path, expected_run_id, covariates, expected_sha256):
    path = Path(path)
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if not isinstance(expected_sha256, str) or digest != expected_sha256:
        raise JobError("MODEL_HASH", "The model file digest does not match the expected value.")
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    if metadata.get("sha256") != digest or metadata.get("run_id") != expected_run_id or metadata.get("schema_version") != 1:
        raise JobError("MODEL_PROVENANCE", "The model run ID or source digest does not match.")
    if metadata.get("covariates") != _schema(covariates):
        raise JobError("MODEL_SCHEMA", "The covariate schema or order of the model does not match.")
    if metadata.get("versions") != _versions():
        raise JobError("MODEL_VERSIONS", "The library versions of the model do not match the runtime environment.")
    return joblib.load(path)
