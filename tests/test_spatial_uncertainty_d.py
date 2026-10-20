"""Spatial intervals preserve evaluation, calibration and original-unit contracts."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import sys

import numpy as np
import pytest
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sps.contract import JobError
from sps.evaluation import (_aggregate_coverage, _coverage_fold, _interval_summary,
    _row_metrics, _rows, evaluate_nested, load_trusted_model, save_trusted_model)
from sps.spatial_uncertainty_d import (REASONS, SpatialCalibration,
    SpatialCalibratedModel, spatial_settings, weighted_conformal_quantile)
from sps.uncertainty import calibrate, finite_sample_quantile


def configuration(**overrides):
    return {"method": "spatial_conformal", "coverage": .8,
        "spatial_bandwidth_m": 1000., "spatial_min_effective_n": 5.,
        "spatial_min_groups": 3, "spatial_kernel": "gaussian", **overrides}


def calibration(n=16, *, coordinates=None, groups=None, scores=None, settings=None,
                crs="EPSG:32637", validation=None, training_coordinates=None,
                training_ids=None, training_groups=None, ids=None):
    return SpatialCalibration(np.arange(1., n + 1) if scores is None else scores,
        np.column_stack([np.arange(n) * 10., np.zeros(n)]) if coordinates is None else coordinates,
        ["c%03d" % i for i in range(n)] if ids is None else ids,
        ["cg%03d" % i for i in range(n)] if groups is None else groups,
        configuration() if settings is None else settings, crs=crs, validation=validation or {},
        training_ids=["t0", "t1"] if training_ids is None else training_ids,
        training_groups=["tg0", "tg1"] if training_groups is None else training_groups,
        training_coordinates=[[-1000., 0.], [-2000., 0.]] if training_coordinates is None else training_coordinates)


class PointModel:
    record = {"model": {"id": "fixture"}}

    def predict(self, X):
        return np.asarray(X)[:, 0] * 2 + 10

    def diagnostics(self, X):
        return {"physical_violation": np.zeros(len(X), bool)}


def test_weighted_quantile_matches_independent_mass_accumulation():
    rng = np.random.default_rng(294)
    for count in (7, 25, 100):
        scores, weights = rng.uniform(0, 8, count), rng.uniform(.1, 1, count)
        for coverage in (.5, .8, .9, .99):
            target = coverage * (math.fsum(weights) + 1.)
            accumulated, expected = 0., None
            for score, weight in sorted(zip(scores, weights)):
                accumulated += weight
                if accumulated >= target:
                    expected = score
                    break
            result = weighted_conformal_quantile(scores, weights, coverage)
            assert result["adjustment"] == expected
            assert result["status"] == ("unavailable" if expected is None else "available")
            assert result["test_mass"] == 1


def test_test_location_mass_is_not_dropped_by_weight_normalization():
    # A normalized empirical quantile would produce 2 here. Mass 1 at infinity forbids it.
    result = weighted_conformal_quantile([1., 2.], [.2, .2], .8)
    assert result["status"] == "unavailable" and result["reason_code"] == "SPATIAL_FINITE_MASS"
    assert result["threshold_mass"] == pytest.approx(1.12)
    for coverage in (.5, .8, .9):
        scores = np.array([8, 2, 4, 1, 9, 6, 3, 7, 5, 10.])
        assert weighted_conformal_quantile(scores, np.ones(10), coverage)["adjustment"] == finite_sample_quantile(scores, coverage)


@pytest.mark.parametrize("scores,weights", [([1], [-.1]), ([1], [1.1]), ([1], [np.nan]),
    ([np.inf], [1]), ([-1], [1]), ([1, 2], [1]), ([[1]], [[1]])])
def test_invalid_scores_and_kernel_weights_are_errors(scores, weights):
    with pytest.raises(JobError) as error:
        weighted_conformal_quantile(scores, weights, .8)
    assert error.value.code == "SPATIAL_CALIBRATION_SCORE"


@pytest.mark.parametrize("override", [{"spatial_bandwidth_m": 0}, {"spatial_bandwidth_m": True},
    {"spatial_min_effective_n": np.nan}, {"spatial_min_groups": 1},
    {"spatial_min_groups": 2.5}, {"spatial_kernel": "adaptive"}])
def test_invalid_local_settings_fail_before_prediction(override):
    with pytest.raises(JobError) as error:
        spatial_settings(configuration(**override))
    assert error.value.code == "SPATIAL_CALIBRATION_SETTING"


@pytest.mark.parametrize("kernel", ["gaussian", "compact"])
def test_metric_kernel_has_independent_distance_reference(kernel):
    coords = np.column_stack([np.arange(16) * 50., np.zeros(16)])
    cal = calibration(coordinates=coords, settings=configuration(spatial_kernel=kernel))
    query = np.array([[100., 100.], [500., -50.], [1000000., 0.]])
    result = cal.adjustments(query)
    for i in range(2):
        distances = np.sqrt(((coords - query[i]) ** 2).sum(axis=1))
        weights = np.exp(-.5 * (distances / 1000) ** 2) if kernel == "gaussian" else np.maximum(1 - (distances / 1000) ** 2, 0) ** 2
        expected = weighted_conformal_quantile(np.arange(1., 17), weights, .8)
        assert result["adjustment"][i] == expected["adjustment"]
        assert result["interval_weight_sum"][i] == pytest.approx(weights.sum())
        assert result["interval_effective_n"][i] == pytest.approx(weights.sum() ** 2 / np.dot(weights, weights))
    assert not result["interval_available"][2]
    assert result["interval_reason_code"][2] == "SPATIAL_NO_SUPPORT"
    assert np.isnan(result["adjustment"][2])


def test_order_window_and_serialization_preserve_results_without_mutation(tmp_path):
    n = 16
    coords = np.column_stack([np.arange(n) * 10., np.zeros(n)])
    scores = np.arange(n, dtype=float)
    ids, groups = ["c%03d" % i for i in range(n)], ["cg%03d" % i for i in range(n)]
    before = coords.copy(), scores.copy(), list(ids), list(groups)
    cal = calibration(coordinates=coords, scores=scores, ids=ids, groups=groups)
    reverse = np.arange(n)[::-1]
    other = calibration(coordinates=coords[reverse], scores=scores[reverse],
        ids=[ids[i] for i in reverse], groups=[groups[i] for i in reverse])
    query = np.column_stack([np.r_[np.arange(23) * 20, 1e7], np.zeros(24)])
    whole, reordered = cal.adjustments(query), other.adjustments(query)
    for key, values in whole.items():
        np.testing.assert_array_equal(values, reordered[key])
        np.testing.assert_array_equal(values, np.concatenate([cal.adjustments(query[i:i + 5])[key] for i in range(0, len(query), 5)]))
    np.testing.assert_array_equal(coords, before[0])
    np.testing.assert_array_equal(scores, before[1])
    assert ids == before[2] and groups == before[3]
    model = SpatialCalibratedModel(PointModel(), cal)
    X = np.arange(len(query), dtype=float).reshape(-1, 1)
    covariates = [{"id": "x0", "kind": "continuous"}]
    saved = save_trusted_model(model, tmp_path / "local.joblib", "spatial-test", covariates)
    loaded = load_trusted_model(tmp_path / "local.joblib", "spatial-test", covariates, saved["sha256"])
    for old, new in zip(model.predict_interval(X, query), loaded.predict_interval(X, query)):
        np.testing.assert_array_equal(old, new)
    assert np.isfinite(model.predict(X, query)).all()
    assert model.record["calibration"]["method_status"] == "experimental"
    assert "no exact" in model.record["calibration"]["coverage_claim"]


def test_geographic_requires_explicit_metric_crs_and_preserves_projected_distances():
    coords = np.column_stack([500000 + np.arange(16) * 10., np.full(16, 4200000.)])
    train = np.array([[490000., 4200000.], [480000., 4200000.]])
    query = np.array([[500075., 4200100.], [501000., 4200000.]])
    transform = Transformer.from_crs("EPSG:32637", "EPSG:4326", always_xy=True)
    geo = lambda values: np.column_stack(transform.transform(*values.T))
    with pytest.raises(JobError) as error:
        calibration(coordinates=geo(coords), training_coordinates=geo(train), crs="EPSG:4326")
    assert error.value.code == "METRIC_CRS_REQUIRED"
    a = calibration(coordinates=coords, training_coordinates=train)
    b = calibration(coordinates=geo(coords), training_coordinates=geo(train),
        crs="EPSG:4326", validation={"metric_crs": "EPSG:32637"})
    actual, expected = b.adjustments(geo(query)), a.adjustments(query)
    for key in ("adjustment", "interval_effective_n", "interval_weight_sum"):
        np.testing.assert_allclose(actual[key], expected[key], rtol=1e-9)
    assert b.record()["source_crs"] == "EPSG:4326"
    assert b.record()["metric_crs"] == "EPSG:32637"


def test_projected_feet_are_converted_to_meters_before_weighting():
    factor = 1200 / 3937  # US survey foot in EPSG:2263
    meters = np.column_stack([np.arange(16) * 10., np.zeros(16)])
    train = np.array([[-1000., 0.], [-2000., 0.]])
    query = np.array([[70., 20.], [600., 0.]])
    met = calibration(coordinates=meters, training_coordinates=train)
    feet = calibration(coordinates=meters / factor, training_coordinates=train / factor, crs="EPSG:2263")
    for key in ("adjustment", "interval_effective_n", "interval_weight_sum"):
        np.testing.assert_allclose(met.adjustments(query)[key], feet.adjustments(query / factor)[key], rtol=1e-12)


def test_local_effective_rows_groups_and_finite_mass_have_distinct_reasons():
    far = np.array([[0., 0.]] + [[1e4 + i, 0.] for i in range(15)])
    assert calibration(coordinates=far).adjustments([[0., 0.]])["interval_reason_code"][0] == "SPATIAL_EFFECTIVE_N"
    coords = np.array([[i, 0.] for i in range(12)] + [[1000 + i, 0.] for i in range(4)])
    groups = ["dominant"] * 12 + ["g%d" % i for i in range(4)]
    result = calibration(coordinates=coords, groups=groups).adjustments([[0., 0.]])
    assert result["interval_group_count"][0] == 5
    assert result["interval_effective_n"][0] > 5
    assert result["interval_effective_group_n"][0] < 3
    assert result["interval_reason_code"][0] == "SPATIAL_GROUP_SUPPORT"
    # Equal small weights preserve effective sample counts but cannot absorb test mass.
    coords = np.column_stack([np.zeros(16), np.arange(16) * .001])
    result = calibration(coordinates=coords).adjustments([[2500., 0.]])
    assert result["interval_effective_n"][0] > 15.9
    assert result["interval_reason_code"][0] == "SPATIAL_FINITE_MASS"


@pytest.mark.parametrize("kwargs,code", [
    ({"n": 4}, "SPATIAL_CALIBRATION_SIZE"),
    ({"groups": ["a"] * 8 + ["b"] * 8}, "SPATIAL_CALIBRATION_SIZE"),
    ({"training_ids": ["c000", "t1"]}, "SPATIAL_CALIBRATION_LEAKAGE"),
    ({"training_groups": ["cg000", "tg1"]}, "SPATIAL_CALIBRATION_LEAKAGE"),
    ({"training_coordinates": [[0, 0], [-2000, 0]]}, "SPATIAL_CALIBRATION_LEAKAGE"),
    ({"validation": {"buffer_distance": 1001}}, "SPATIAL_CALIBRATION_LEAKAGE"),
    ({"training_groups": []}, "SPATIAL_CALIBRATION_PROVENANCE"),
])
def test_bad_global_size_and_provenance_fail_fast(kwargs, code):
    with pytest.raises(JobError) as error:
        calibration(**kwargs)
    assert error.value.code == code


@pytest.mark.parametrize("bad", [None, 1, [1, 2], [[np.inf, 0]]])
def test_query_coordinates_require_two_finite_values(bad):
    with pytest.raises(JobError) as error:
        calibration().adjustments(bad)
    assert error.value.code == "COORDINATES"


def test_spatial_rows_expose_partial_scope_with_json_null_and_full_point_metrics():
    model = SpatialCalibratedModel(PointModel(), calibration())
    X, coords = np.array([[0.], [1.]]), np.array([[70., 0.], [1e7, 0.]])
    rows = _rows(model, X, [10, 5000], ["test-a", "test-b"], ["test-ga", "test-gb"], 0, 2, coords)
    assert rows[1]["predicted"] == 12 and rows[1]["lower"] is None and rows[1]["upper"] is None
    assert rows[1]["interval_status"] == "unavailable" and rows[1]["reason_code"] == "SPATIAL_NO_SUPPORT"
    metrics = _row_metrics(rows, .8)
    assert metrics["n"] == 2 and metrics["rmse"] > 3000
    scope = metrics["interval"]
    assert scope["status"] == "partial" and scope["n"] == 2
    assert scope["available_n"] == scope["coverage_denominator"] == 1
    assert scope["availability_denominator"] == 2 and scope["available_fraction"] == .5
    assert "coverage" not in scope and scope["available_subset"]["coverage"] == 1
    assert scope["reason_counts"] == {"SPATIAL_NO_SUPPORT": 1}
    summaries, predictions = _coverage_fold(model, X, [10, 5000], ["test-a", "test-b"], ["test-ga", "test-gb"], 0, 2, [.8], coords)
    pooled = _aggregate_coverage(summaries, predictions, [.8], rows)[0]
    assert pooled["status"] == "partial" and pooled["n"] == 2 and pooled["partial_fold_count"] == 1
    json.dumps({"rows": rows, "metrics": metrics, "pooled": pooled}, allow_nan=False)
    empty = _interval_summary([rows[1]], .8)
    assert empty["status"] == "unavailable" and "available_subset" not in empty


@pytest.mark.parametrize("identity,group", [("c000", "test-ga"), ("t0", "test-ga"),
    ("test-a", "cg000"), ("test-a", "tg0")])
def test_evaluated_identity_or_group_must_be_disjoint_from_fit_and_calibration(identity, group):
    with pytest.raises(JobError) as error:
        _rows(SpatialCalibratedModel(PointModel(), calibration()), np.array([[0.]]), [0.],
            [identity], [group], 0, 0, np.array([[70., 0.]]))
    assert error.value.code == "SPATIAL_EVALUATION_LEAKAGE"


@pytest.mark.parametrize("lower,upper,available,reason", [
    (np.nan, 1., False, "SPATIAL_NO_SUPPORT"), (np.inf, np.inf, False, "SPATIAL_NO_SUPPORT"),
    (2., 1., True, ""), (np.nan, np.nan, True, ""),
    (np.nan, np.nan, False, ""), (0., 1., True, "SPATIAL_NO_SUPPORT")])
def test_malformed_interval_arrays_are_errors_not_unavailable(monkeypatch, lower, upper, available, reason):
    model = SpatialCalibratedModel(PointModel(), calibration())
    def malformed(X, coverage=None, coordinates=None):
        result = model.calibration.adjustments(coordinates)
        return {**result, "lower": np.array([lower]), "upper": np.array([upper]),
            "interval_available": np.array([available]), "interval_reason_code": np.array([reason])}
    monkeypatch.setattr(model, "interval_details", malformed)
    with pytest.raises(JobError) as error:
        _rows(model, np.array([[0.]]), [0.], ["test"], ["test-group"], 0, 0, np.array([[70., 0.]]))
    assert error.value.code == "INTERVAL"


@pytest.fixture
def nested_case():
    rng = np.random.default_rng(1008)
    n = 96
    X = rng.normal(size=(n, 2))
    coords = np.column_stack([500000. + np.arange(n) * 100., np.full(n, 4200000.)])
    y = 10 + 2 * X[:, 0] - X[:, 1] + rng.normal(size=n) * np.linspace(.2, 2., n)
    ids, groups = ["S%03d" % i for i in range(n)], np.repeat(np.arange(n // 2), 2).astype(str)
    covs = [{"id": "x%d" % j, "kind": "continuous", "unit": "1"} for j in range(2)]
    settings = {"validation": {"method": "group", "n_splits": 3, "inner_splits": 2, "repeats": 1, "seed": 42},
        "preprocessing": {"imputation": "median", "scaling": "standard", "unknown_category": "flag"},
        "target_transform": {"method": "none"}, "feature_selection": {"method": "none"},
        "vif": {"mode": "diagnostic", "threshold": 10}, "model": {"id": "ridge", "parameters": {"alpha": 1.}},
        "optimization": {"method": "manual", "trials": 1, "seed": 42, "metric": "rmse", "n_jobs": 1, "timeout_seconds": 60},
        "uncertainty": configuration(spatial_bandwidth_m=100000., calibration_fraction=.25), "mapping": {}}
    return X, y, ids, coords, groups, covs, settings


def nested(case):
    return evaluate_nested(*case, crs="EPSG:32637", check_cancel=lambda: None, progress=lambda *args: None)


def test_same_outer_and_calibration_ids_benchmark_spatial_against_global(nested_case):
    local = nested(nested_case)
    global_case = copy.deepcopy(nested_case)
    global_case[-1]["uncertainty"]["method"] = "split_conformal"
    global_result = nested(global_case)
    assert local["folds"] == global_result["folds"]
    for local_fold, global_fold in zip(local["fold_metrics"], global_result["fold_metrics"]):
        for key in ("development_ids", "calibration_ids", "excluded_ids"):
            assert local_fold["model_record"][key] == global_fold["model_record"][key]
    assert [r["predicted"] for r in local["oof"]] == [r["predicted"] for r in global_result["oof"]]
    for result in (local, global_result):
        pairs = result["oof"]
        empirical = np.mean([r["lower"] <= r["observed"] <= r["upper"] for r in pairs])
        summary = result["metrics"]["pooled_oof"]["interval"]
        assert summary["coverage"] == empirical and 0 <= empirical <= 1
        assert result["metrics"]["pooled_oof"]["n"] == len(nested_case[0])
        assert summary["nominal_coverage"] == .8
    assert local["metrics"]["pooled_oof"]["interval"]["available_fraction"] == 1
    # This benchmark checks paired scope and honest empirical values, not superiority.
    json.dumps({k: v for k, v in local.items() if k != "final_model"}, allow_nan=False)


def test_heldout_targets_never_change_its_interval_or_point_fit(nested_case):
    result = nested(nested_case)
    first = result["folds"][0]
    changed = copy.deepcopy(nested_case)
    for identity in first["test_ids"]:
        changed[1][changed[2].index(identity)] += 10000.
    other = nested(changed)
    keys = ("sample_id", "predicted", "lower", "upper", "interval_effective_n", "interval_weight_sum")
    select = lambda rows: [{key: r[key] for key in keys} for r in rows if r["repeat"] == first["repeat"] and r["fold"] == first["fold"]]
    assert select(result["oof"]) == select(other["oof"])
    for row in result["learning_audit"]:
        if row["role"].startswith("outer/0/0"):
            assert set(row["ids"]).isdisjoint(first["test_ids"])


def test_calibration_target_intervention_and_audit_prove_separate_fit(nested_case):
    result = nested(nested_case)
    changed = copy.deepcopy(nested_case)
    calibration_ids = result["final_record"]["calibration_ids"]
    for identity in calibration_ids:
        changed[1][changed[2].index(identity)] += 500.
    other = nested(changed)
    X, coords = nested_case[0], nested_case[3]
    np.testing.assert_array_equal(result["final_model"].predict(X, coords), other["final_model"].predict(X, coords))
    lower, upper = result["final_model"].predict_interval(X, coords)
    changed_lower, changed_upper = other["final_model"].predict_interval(X, coords)
    assert np.mean(changed_upper - changed_lower) > np.mean(upper - lower) + 500
    calibration_groups = set(result["final_record"]["calibration"]["groups"])
    assert calibration_groups.isdisjoint(result["final_record"]["calibration"]["training_groups"])
    for row in result["learning_audit"]:
        if row["role"].startswith("final/") and row["stage"] != "calibration":
            assert set(row["ids"]).isdisjoint(calibration_ids)


def test_repeats_independent_far_points_and_inputs_remain_separate(nested_case):
    settings = nested_case[-1]
    settings["validation"].update(method="repeated", repeats=2, independent_ids=nested_case[2][-8:])
    settings["uncertainty"].update(spatial_kernel="compact", spatial_bandwidth_m=100000.)
    nested_case[3][-8:, 0] += 10000000.
    before = copy.deepcopy(nested_case)
    result = nested(nested_case)
    assert len(result["oof"]) == 88 * 2
    assert [row["n"] for row in result["metrics"]["per_repeat"]] == [88, 88]
    assert all(row["lower"] is None and row["predicted"] is not None for row in result["independent_predictions"])
    assert result["metrics"]["independent"]["interval"]["status"] == "unavailable"
    assert result["metrics"]["independent"]["interval"]["availability_denominator"] == 8
    for row in result["learning_audit"]:
        assert set(row["ids"]).isdisjoint(settings["validation"]["independent_ids"])
    for index in (0, 1, 3, 4):
        np.testing.assert_array_equal(nested_case[index], before[index])
    for index in (2, 5, 6):
        assert nested_case[index] == before[index]


def test_log_target_calibration_scores_stay_in_original_units(nested_case):
    nested_case[1][:] = np.exp(nested_case[1] / 4)
    nested_case[-1]["target_transform"] = {"method": "log", "log_estimand": "median"}
    result = nested(nested_case)
    model = result["final_model"]
    record = model.record["calibration"]
    indices = [nested_case[2].index(identity) for identity in record["ids"]]
    expected = np.abs(nested_case[1][indices] - model.predict(nested_case[0][indices], nested_case[3][indices]))
    np.testing.assert_allclose(record["scores"], expected)
    assert record["unit"] == "original_target_unit"


def test_parallel_search_is_explicitly_rejected(nested_case):
    nested_case[-1]["optimization"]["n_jobs"] = 2
    with pytest.raises(JobError) as error:
        nested(nested_case)
    assert error.value.code == "SPATIAL_PARALLELISM"


@pytest.mark.parametrize("model_id,parameters", [("mean", {}),
    ("random_forest", {"n_estimators": 8, "max_depth": 3}),
    ("ordinary_kriging", {"nlags": 4, "n_closest_points": 8}),
    ("regression_kriging", {"nlags": 4, "n_closest_points": 8, "base_model": "ridge"})])
def test_actual_point_and_kriging_models_use_separate_spatial_calibration(nested_case, model_id, parameters):
    from sps.modeling import fit_workflow
    X, y, ids, coords, groups, covariates, settings = nested_case
    settings["model"] = {"id": model_id, "parameters": parameters}
    if model_id == "ordinary_kriging":
        settings["vif"]["mode"] = "off"
    audit = []
    base = fit_workflow(X[:48], y[:48], ids[:48], coords[:48], groups[:48], covariates, settings,
        crs="EPSG:32637", role="fit", learning_audit=audit)
    model = calibrate(base, X[48:72], y[48:72], ids[48:72], settings["uncertainty"],
        coordinates=coords[48:72], groups=groups[48:72], training_ids=ids[:48], training_groups=groups[:48],
        training_coordinates=coords[:48], crs="EPSG:32637", validation=settings["validation"],
        role="calibration", learning_audit=audit)
    rows = _rows(model, X[72:], y[72:], ids[72:], groups[72:], 0, 0, coords[72:])
    assert all(row["interval_status"] == "available" and np.isfinite(row["predicted"]) for row in rows)
    if model_id == "regression_kriging":
        assert all(np.isfinite(row["plain_predicted"]) for row in rows)
    for entry in audit:
        if entry["stage"] != "calibration":
            assert set(entry["ids"]).isdisjoint(ids[48:])
    assert audit[-1]["group_ids"] == list(groups[48:72])


def test_applicability_wrapper_keeps_spatial_interval_and_diagnostics(nested_case):
    nested_case[-1]["diagnostics"] = {"applicability": True}
    result = nested(nested_case)
    assert all("applicability" in row and row["interval_status"] == "available" for row in result["oof"])
    diagnostics = result["final_model"].diagnostics(nested_case[0][:2], nested_case[3][:2])
    assert "applicability" in diagnostics and "interval_available" in diagnostics
