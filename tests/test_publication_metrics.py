"""Recomputable publication metrics, held-out provenance and fitted parameters."""
from copy import deepcopy
from dataclasses import dataclass
import io
import json
from pathlib import Path
import random
import sys

import joblib
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from sps import evaluation, modeling
from sps.contract import JobError
from test_science_b import scientific, fit, nested


def assert_strict_json(value):
    return json.dumps(value, allow_nan=False, sort_keys=True)


def test_metrics_match_independent_formulas_and_distinguish_correlation():
    y = np.array([1., 2., 4., 8., 11.])
    p = np.array([2., 1., 7., 9., 8.])
    e = p - y
    result = evaluation.regression_metrics(y, p)
    assert result["mse"] == pytest.approx(np.dot(e, e) / 5)
    assert result["rmse"] == pytest.approx(np.sqrt(np.dot(e, e) / 5))
    assert result["median_absolute_error"] == pytest.approx(np.median(np.abs(e)))
    assert result["residual_std"] == pytest.approx(np.sqrt(np.sum((e - e.mean()) ** 2) / 4))
    assert result["observed_std"] == pytest.approx(np.sqrt(np.sum((y - y.mean()) ** 2) / 4))
    assert result["observed_iqr"] == 6
    assert result["rpd"] == pytest.approx(result["observed_std"] / result["rmse"])
    assert result["rpiq"] == pytest.approx(6 / result["rmse"])
    cov = np.mean((y - y.mean()) * (p - p.mean()))
    assert result["lin_ccc"] == pytest.approx(2 * cov / (np.var(y) + np.var(p) + (y.mean() - p.mean()) ** 2))
    assert result["pearson_r"] == pytest.approx(np.corrcoef(y, p)[0, 1])
    assert result["pearson_r2"] == pytest.approx(result["pearson_r"] ** 2)
    assert result["reasons"] == {}
    reversed_result = evaluation.regression_metrics([1, 2, 3], [3, 2, 1])
    assert reversed_result["r2"] == -3
    assert reversed_result["pearson_r"] == pytest.approx(-1)
    assert reversed_result["pearson_r2"] == pytest.approx(1)
    assert reversed_result["lin_ccc"] == pytest.approx(-1)
    assert_strict_json(result)


@pytest.mark.parametrize("y,p,reasons", [
    ([2], [3], {"r2": "fewer_than_two_observations", "residual_std": "fewer_than_two_observations",
                 "pearson_r": "fewer_than_two_observations", "lin_ccc": "fewer_than_two_observations", "rpiq": "fewer_than_two_observations"}),
    ([2, 2], [2, 2], {"r2": "constant_observed_target", "pearson_r": "constant_observed_target",
                       "lin_ccc": "zero_ccc_denominator", "rpd": "zero_rmse", "rpiq": "zero_rmse"}),
    ([1, 2, 3], [2, 2, 2], {"pearson_r": "constant_predictions", "pearson_r2": "constant_predictions"}),
    ([1, 2, 3], [1, 2, 3], {"rpd": "zero_rmse", "rpiq": "zero_rmse"}),
])
def test_undefined_values_have_reasons_not_zero_or_infinity(y, p, reasons):
    result = evaluation.regression_metrics(y, p)
    for key, reason in reasons.items():
        assert result[key] is None
        assert result["reasons"][key] == reason
    assert result["r2_reason"] == result["reasons"].get("r2")
    assert_strict_json(result)


def test_constant_observation_ccc_can_be_zero_without_undefined_correlation_becoming_success():
    result = evaluation.regression_metrics([2, 2, 2], [1, 2, 4])
    assert result["pearson_r"] is None
    assert result["lin_ccc"] == 0
    assert result["rpd"] == 0
    assert result["rpiq"] == 0


def test_overflow_is_explicit_and_json_safe():
    result = evaluation.regression_metrics([1e308, -1e308], [-1e308, 1e308])
    assert result["mse"] is None
    assert result["r2"] is None
    assert result["reasons"]["mse"] == "numerical_overflow"
    assert_strict_json(result)


@pytest.mark.parametrize("y,p", [([], []), ([1, np.nan], [2, 3]), ([1, 2], [2]), ([[1]], [[1]])])
def test_invalid_metric_pairs_are_rejected(y, p):
    with pytest.raises(JobError):
        evaluation.regression_metrics(y, p)


def test_inner_predictions_recompute_selection_and_keep_repeat_roles(scientific):
    scientific[-1]["validation"].update(method="repeated", repeats=2)
    scientific[-1]["optimization"].update(method="grid", trials=2, spaces={"alpha": {"type": "categorical", "choices": [.1, 5.]}})
    result = nested(scientific)
    outer_lookup = {"outer/%d/%d/search" % (f["repeat"], f["fold"]): f for f in result["folds"]}
    for trial in result["trials"]:
        rows = trial["validation_predictions"]
        expected = {(f["repeat"], f["fold"], identity) for f in trial["inner_folds"] for identity in f["test_ids"]}
        assert {(r["repeat"], r["fold"], r["sample_id"]) for r in rows} == expected
        assert len(rows) == len(expected) == trial["validation_expected_rows"]
        assert trial["validation_status"] == "complete"
        assert trial["validation_metrics"] == evaluation._point_metrics(rows)
        assert trial["value"] == pytest.approx(trial["validation_metrics"][trial["metric"]])
        assert {r["repeat"] for r in trial["validation_per_repeat"]} == {0, 1}
        if trial["role"] in outer_lookup:
            assert {r["sample_id"] for r in rows}.isdisjoint(outer_lookup[trial["role"]]["test_ids"])
        for item, fold in zip(trial["inner_fold_metrics"], trial["inner_folds"]):
            subset = [r for r in rows if (r["repeat"], r["fold"]) == (fold["repeat"], fold["fold"])]
            assert item["metrics"] == evaluation._point_metrics(subset)
            assert item["test_sample_count"] == len(fold["test_ids"])
            assert item["training_sample_count"] == len(fold["train_ids"])
            assert item["training_group_count"] * 2 == item["training_sample_count"]
            assert item["effective_parameters"]["fit_intercept"] is True
    for role in {r["role"] for r in result["trials"]}:
        candidates = [r for r in result["trials"] if r["role"] == role]
        best = min(candidates, key=lambda r: (r["value"], r["trial"]))
        assert [r["trial"] for r in candidates if r["selected"]] == [best["trial"]]
    assert result["metrics"]["pooled_oof"]["mse"] == pytest.approx(result["metrics"]["pooled_oof"]["rmse"] ** 2)
    assert result["metrics"]["fold_mean"]["lin_ccc_defined_folds"] == 6


def test_failed_trial_keeps_completed_rows_without_aggregate_success(scientific, monkeypatch):
    scientific[-1]["model_candidates"] = [{"id": "ridge", "parameters": {}}, {"id": "mean", "parameters": {}}]
    real_fit = evaluation.fit_workflow
    def fail_second_fold(*args, **kwargs):
        if kwargs["role"].endswith("trial0/inner0/1"):
            raise JobError("EXPECTED_TEST_FAILURE", "Controlled second-fold failure.")
        return real_fit(*args, **kwargs)
    monkeypatch.setattr(evaluation, "fit_workflow", fail_second_fold)
    X, y, ids, xy, groups, covariates, settings = scientific
    trials = []
    chosen = evaluation._search(X, y, ids, xy, groups, covariates, settings, crs="EPSG:32637", role="test/search",
                                audit=[], trials=trials, check_cancel=lambda: None)
    failed, complete = trials
    assert failed["status"] == "failed" and failed["validation_status"] == "incomplete"
    assert failed["validation_predictions"] and failed["validation_completed_folds"] == 1
    assert failed["validation_metrics"] is None and failed["validation_per_repeat"] == []
    assert failed["validation_reason"] == "EXPECTED_TEST_FAILURE"
    assert failed["value"] is None and failed["selected"] is False
    assert complete["selected"] and chosen["model"]["id"] == "mean"


@pytest.mark.parametrize("method", ["none", "split_conformal", "spatial_conformal"])
def test_training_calibration_rows_are_points_only_with_separate_provenance(scientific, method):
    settings = scientific[-1]
    settings["uncertainty"].update(method=method, spatial_min_groups=2, spatial_min_effective_n=2, spatial_bandwidth_m=10000)
    settings["validation"]["independent_ids"] = scientific[2][-4:]
    result = nested(scientific)
    for record in [result["final_record"]] + [f["model_record"] for f in result["fold_metrics"]]:
        train, calibration = record["training_predictions"], record["calibration_predictions"]
        assert {r["sample_id"] for r in train} == set(record["development_ids"])
        assert {r["sample_id"] for r in calibration} == set(record["calibration_ids"])
        assert set(record["development_ids"]).isdisjoint(record["calibration_ids"])
        assert set(record["development_ids"] + record["calibration_ids"]).isdisjoint(settings["validation"]["independent_ids"])
        assert record["train_metrics"] == evaluation._point_metrics(train)
        assert record["calibration_metrics"] == evaluation._point_metrics(calibration)
        assert all(r["scope"] == "resubstitution" for r in train)
        assert all(r["scope"] == "calibration_diagnostic" for r in calibration)
        assert all("lower" not in r and "upper" not in r for r in train + calibration)
        assert "not independent validation" in record["calibration_metrics_scope"]
    if method == "none":
        assert result["final_record"]["calibration_metrics"] is None
    assert result["metrics"]["independent"]["n"] == 4
    assert "lin_ccc" in result["metrics"]["independent"]
    buffer = io.BytesIO()
    joblib.dump(result["final_model"], buffer)
    buffer.seek(0)
    restored = joblib.load(buffer)
    assert restored.record == result["final_record"]
    np.testing.assert_array_equal(modeling.prediction_call(restored, "predict", scientific[0], scientific[3]),
                                  modeling.prediction_call(result["final_model"], "predict", scientific[0], scientific[3]))
    assert_strict_json(result["final_record"])


@pytest.mark.parametrize("model,params", [("ridge", {}), ("random_forest", {"n_estimators": 3}),
                                         ("catboost", {"iterations": 2, "depth": 2})])
def test_effective_parameters_and_learned_attributes_are_actual(scientific, model, params):
    scientific[-1]["model"] = {"id": model, "parameters": params}
    fitted = fit(scientific)
    effective = fitted.record["effective_parameters"]
    assert effective["random_seed" if model == "catboost" else "random_state"] == (42 if model != "ridge" else None)
    if model == "ridge":
        assert effective["solver"] == "auto" and effective["tol"] == .0001 and effective["fit_intercept"] is True
        assert "coef_" in fitted.record["fitted_model_attributes"]["attributes"]
    if model == "catboost":
        assert effective["l2_leaf_reg"] == 3 and "get_all_params" in fitted.record["effective_parameters_source"]
    attrs = fitted.record["fitted_model_attributes"]
    assert len(attrs["input_features"]) == fitted.estimator.n_features_in_ == 6
    assert [r["category"] for r in attrs["input_features"] if r["kind"] == "one_hot"] == [0, 1, 2]
    assert "not raw soil-unit coefficients" in attrs["scope"]
    assert_strict_json(fitted.record)


def test_pca_coefficients_are_labeled_as_components(scientific):
    scientific[-1]["feature_selection"].update(method="pca", k=2)
    fitted = fit(scientific)
    assert fitted.record["fitted_model_attributes"]["input_features"] == [
        {"kind": "principal_component", "component": 1}, {"kind": "principal_component", "component": 2}]


@pytest.mark.parametrize("uncertainty,model", [("quantile_forest", "random_forest"), ("quantile_boosting", "hist_gradient_boosting")])
def test_quantile_estimators_report_the_actual_replacement_and_bounds(scientific, uncertainty, model):
    scientific[-1]["uncertainty"]["method"] = uncertainty
    scientific[-1]["model"] = {"id": model, "parameters": {"n_estimators": 3} if model == "random_forest" else {"max_iter": 3}}
    fitted = fit(scientific)
    if uncertainty == "quantile_forest":
        assert "RandomForestQuantileRegressor" in fitted.record["effective_estimator_class"]
        assert fitted.record["effective_parameters"]["max_samples_leaf"] is None
    else:
        bounds = fitted.record["quantile_effective_parameters"]
        assert [r["bound"] for r in bounds] == ["lower", "upper"]
        assert [r["effective_parameters"]["quantile"] for r in bounds] == pytest.approx([.1, .9])
        assert all(r["effective_parameters"]["early_stopping"] is False for r in bounds)
    assert_strict_json(fitted.record)


@pytest.mark.parametrize("name,constant", [("ordinary_kriging", False), ("regression_kriging", False), ("ordinary_kriging", True)])
def test_spatial_effective_parameters_do_not_claim_a_point_estimator_is_kriging(scientific, name, constant):
    scientific[-1]["model"] = {"id": name, "parameters": {"variogram_model": "linear"}}
    scientific[-1]["vif"] = {"mode": "off"}
    if constant:
        scientific[1][:] = 5
    fitted = fit(scientific)
    parameters = fitted.record["effective_parameters"]
    assert set(parameters) == {"base_estimator", "kriging"}
    assert "SpatialWorkflow" in fitted.record["effective_estimator_class"]
    if constant:
        assert parameters["kriging"]["variogram_fitted"] is False
    else:
        assert parameters["kriging"]["constructor"]["exact_values"] is True
        assert parameters["kriging"]["constructor"]["nlags"] == 6
        assert parameters["kriging"]["prediction"]["n_closest_points"] == 12
    if name == "regression_kriging":
        assert fitted.record["fitted_model_attributes"]["base_model_attributes"]["status"] == "available"
    assert_strict_json(fitted.record)


def test_parameter_serialization_is_deterministic_without_addresses_or_random_draws():
    @dataclass
    class Config:
        count: object
        method: object
    rng = np.random.default_rng(52)
    py_rng = random.Random(5)
    state = deepcopy(rng.bit_generator.state)
    original = {"rng": rng, "python_rng": py_rng, "scalar": np.int64(3), "nan": np.nan,
                "config": Config(np.float32(.5), np.mean), "array": np.array(3), "unknown": object()}
    first = modeling._parameter_value(original)
    assert first == modeling._parameter_value(original)
    assert state == rng.bit_generator.state
    assert first["nan"] == {"type": "nonfinite_float", "value": "NaN"}
    assert first["unknown"]["serialization"] == "type_only_not_exposed"
    assert "0x" not in assert_strict_json(first)


def test_producer_config_reads_fitted_interface_without_fitting():
    class Producer:
        interface_config_ = {"resolved": np.int32(3)}
        inference_config = None
        def get_params(self, deep=False):
            assert deep is False
            return {"fit": False, "seed": 7}
        def get_config(self):
            return {"use_early_stopping": False, "n_epochs": 2}
    record = modeling._producer_parameters(Producer(), config=True, inference=True)
    assert record["get_config"]["use_early_stopping"] is False
    assert record["inference_config"] == {"resolved": 3}
    assert record["inference_config_source"] == "interface_config_"
    assert_strict_json(record)


@pytest.mark.parametrize("name", ["tabm", "realmlp", "tabpfn"])
def test_neural_wrapper_parameters_are_available_without_loading_weights(name):
    estimator = modeling.make_point_estimator({"id": name, "parameters": {}}, seed=21)
    record = modeling._effective_estimator_record(estimator)
    assert record["effective_parameters"]["seed"] == 21
    assert record["effective_parameters"]["parameters"]
    assert_strict_json(record)


