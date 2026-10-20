"""Held-out explanation and descriptive uncertainty/spatial diagnostic gates."""
import copy
import json
from pathlib import Path
import sys

import numpy as np
from pyproj import Transformer
import pytest
from sklearn.linear_model import LinearRegression

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from sps.contract import Cancelled, JobError
from sps.diagnostics_c import (aggregate_permutation_importance, heldout_permutation_importance,
                               residual_spatial_diagnostics, subgroup_diagnostics)


@pytest.fixture
def importance_case():
    rng = np.random.default_rng(121)
    X = rng.normal(size=(160, 3))
    y = 8 * X[:, 0]
    model = LinearRegression().fit(X[:80], y[:80])
    model.record = {"training_ids": ["train%03d" % i for i in range(80)], "model": {"id": "linear_reference"}}
    ids = ["test%03d" % i for i in range(80)]
    return model, X[80:], y[80:], ids, np.asarray(["group%03d" % (i // 2) for i in range(80)]), ["signal", "noise1", "noise2"]


def importance(case, **kwargs):
    return heldout_permutation_importance(*case, repeat=kwargs.pop("repeat", 0), fold=kwargs.pop("fold", 0), **kwargs)


def test_known_signal_paired_losses_and_no_refit_or_input_mutation(importance_case):
    model, X, y, ids, groups, names = importance_case
    before = copy.deepcopy((X, y, ids, groups, model.record, model.coef_))
    model.fit = lambda *args, **kwargs: pytest.fail("Diagnostics must never fit")
    result = importance(importance_case, n_permutations=15)
    summary = {r["feature"]: r for r in result["summary"]}
    assert summary["signal"]["importance_mean"] > 8
    assert abs(summary["noise1"]["importance_mean"]) < 1e-12
    assert abs(summary["noise2"]["importance_mean"]) < 1e-12
    assert len(result["rows"]) == 45
    for row in result["rows"]:
        assert row["importance"] == pytest.approx(row["permuted_loss"] - result["baseline_loss"])
    for k in range(15):
        assert len({r["permutation_sha256"] for r in result["rows"] if r["permutation_index"] == k}) == 1
    np.testing.assert_array_equal(X, before[0])
    np.testing.assert_array_equal(y, before[1])
    assert ids == before[2] and model.record == before[4]
    np.testing.assert_array_equal(groups, before[3])
    np.testing.assert_array_equal(model.coef_, before[5])
    json.dumps(result, allow_nan=False)


def test_stable_identity_order_and_seed_preserve_exact_result(importance_case):
    expected = importance(importance_case, n_permutations=3)
    model, X, y, ids, groups, names = importance_case
    order = np.random.default_rng(6).permutation(len(ids))
    changed = (model, X[order], y[order], [ids[i] for i in order], groups[order], names)
    assert importance(changed, n_permutations=3) == expected
    assert importance(importance_case, n_permutations=3, seed=91)["rows"] != expected["rows"]


@pytest.mark.parametrize("kwargs,code", [
    ({"training_ids": ["test000"]}, "DIAGNOSTIC_LEAKAGE"),
    ({"calibration_ids": ["test000"]}, "DIAGNOSTIC_LEAKAGE"),
    ({"training_groups": ["group000"]}, "DIAGNOSTIC_GROUP_LEAKAGE"),
    ({"calibration_groups": ["group000"]}, "DIAGNOSTIC_GROUP_LEAKAGE"),
    ({"metric": "r2"}, "DIAGNOSTIC_CONFIG"),
    ({"n_permutations": True}, "DIAGNOSTIC_CONFIG"),
    ({"feature_groups": {"bad": ["missing"]}}, "DIAGNOSTIC_FEATURES")])
def test_leakage_and_invalid_configuration_fail(importance_case, kwargs, code):
    with pytest.raises(JobError) as error:
        importance(importance_case, **kwargs)
    assert error.value.code == code


def test_provenance_cannot_be_omitted_or_overridden(importance_case):
    model = importance_case[0]
    model.record = {}
    with pytest.raises(JobError) as error:
        importance(importance_case)
    assert error.value.code == "DIAGNOSTIC_PROVENANCE"
    model.record = {"training_ids": ["test000"]}
    with pytest.raises(JobError) as error:
        importance(importance_case, training_ids=["safe"])
    assert error.value.code == "DIAGNOSTIC_LEAKAGE"


class RecordingModel:
    record = {"training_ids": ["training"], "model": {"id": "recording"}}

    def __init__(self):
        self.received = []

    def predict(self, X):
        self.received.append(X.copy())
        return X[:, 0]


def test_within_group_permutation_and_joint_features_preserve_group_information():
    X = np.column_stack([np.repeat([1., 4., 9.], 3), np.arange(9.), np.arange(9.) + 100])
    model = RecordingModel()
    result = heldout_permutation_importance(model, X, X[:, 0], [str(i) for i in range(9)],
        np.repeat(["a", "b", "c"], 3), ["group_signal", "first", "second"], repeat=0, fold=1,
        permutation="within_group", feature_groups={"joint": ["first", "second"], "signal": ["group_signal"]}, n_permutations=5)
    assert all(row["importance"] == 0 for row in result["rows"])
    for changed in model.received:
        np.testing.assert_array_equal(changed[:, 2] - changed[:, 1], np.full(9, 100))
        for begin in (0, 3, 6):
            assert set(changed[begin:begin + 3, 1]) == set(X[begin:begin + 3, 1])
    assert any(not np.array_equal(changed, X) for changed in model.received)


def test_whole_group_permutation_moves_blocks_and_rejects_unequal_groups():
    X = np.column_stack([np.repeat([1., 4., 9.], 2), [0, 1, 10, 11, 20, 21]])
    model = RecordingModel()
    arguments = (model, X, X[:, 0], [str(i) for i in range(6)], ["a", "a", "b", "b", "c", "c"], ["x", "position"])
    result = heldout_permutation_importance(*arguments, repeat=0, fold=0, permutation="whole_group",
        feature_groups={"joint": ["x", "position"]}, n_permutations=6)
    assert result["summary"][0]["importance_mean"] > 0
    for changed in model.received:
        for start in (0, 2, 4):
            assert changed[start, 0] == changed[start + 1, 0]
            assert changed[start + 1, 1] - changed[start, 1] == 1
    with pytest.raises(JobError) as error:
        heldout_permutation_importance(*arguments[:4], ["a", "a", "a", "b", "b", "c"], arguments[-1],
            repeat=0, fold=0, permutation="whole_group")
    assert error.value.code == "DIAGNOSTIC_GROUP_SIZE"


def test_singletons_within_groups_are_unavailable(importance_case):
    model, X, y, ids, _, names = importance_case
    result = importance((model, X, y, ids, ids, names), permutation="within_group")
    assert result["status"] == "unavailable" and result["reason_code"] == "NO_PERMUTABLE_UNITS"
    assert result["rows"] == []


def test_spatial_predictions_receive_fixed_copied_locations(importance_case):
    class SpatialModel:
        requires_coordinates = True
        record = {"training_ids": ["train"]}

        def predict(self, X, coordinates):
            assert np.array_equal(coordinates[:, 0], np.arange(len(X)))
            values = X[:, 0] + coordinates[:, 0]
            coordinates[:] = -999
            X[:] = -888
            return values

    _, X, _, ids, groups, names = importance_case
    xy = np.column_stack([np.arange(len(X)), np.zeros(len(X))])
    before = X.copy()
    result = importance((SpatialModel(), X, X[:, 0] + xy[:, 0], ids, groups, names), coordinates=xy, n_permutations=2)
    assert result["coordinates_permuted"] is False
    np.testing.assert_array_equal(xy[:, 0], np.arange(len(X)))
    np.testing.assert_array_equal(X, before)


def test_aggregation_preserves_repeat_and_fold_pairing_and_missing_status(importance_case):
    results = [importance(importance_case, repeat=r, fold=f, n_permutations=2) for r in (0, 1) for f in (0, 1)]
    result = aggregate_permutation_importance(results)
    assert len(result["per_repeat"]) == 6
    for row in result["per_repeat"]:
        assert row["fold_count"] == 2
        assert row["importance_mean"] == pytest.approx(np.mean([v["importance_mean"] for v in row["fold_mean_values"]]))
    unavailable = copy.deepcopy(results)
    unavailable[0].update(status="unavailable", rows=[], summary=[])
    result = aggregate_permutation_importance(unavailable)
    assert all(r["status"] == "unavailable" and "importance_mean" not in r for r in result["per_repeat"] if r["repeat"] == 0)
    assert all(r["status"] == "available" for r in result["per_repeat"] if r["repeat"] == 1)
    with pytest.raises(JobError):
        aggregate_permutation_importance([results[0], results[0]])


@pytest.fixture
def evaluated_case():
    ids = ["s%02d" % i for i in range(15)]
    props = [{"land_use": "tarım" if i < 9 else "orman" if i < 12 else "mera", "depth": i * 10} for i in range(15)]
    coordinates = np.column_stack([500000 + np.arange(15) * 100., np.full(15, 4200000.)])
    samples = {"ids": ids, "props": props, "coordinates": coordinates, "crs": "EPSG:32637"}
    def row(index, repeat, fold):
        observed = float(index)
        predicted = observed + (1 if repeat == 0 else 3)
        return {"sample_id": ids[index], "repeat": repeat, "fold": fold, "observed": observed,
            "predicted": predicted, "residual": predicted - observed, "group_id": "g%02d" % (index // 2),
            "lower": predicted - 2, "upper": predicted + 2, "interval_width": 4., "training_distance_m": index * 50}
    evaluation = {"oof": [row(i, repeat, i % 3) for repeat in (0, 1) for i in range(12)],
                  "independent_predictions": [row(i, None, None) for i in range(12, 15)]}
    return evaluation, samples


def test_subgroups_join_by_id_separate_repeats_count_groups_and_flag_undercoverage(evaluated_case):
    evaluation, samples = evaluated_case
    before = copy.deepcopy((evaluation, samples))
    result = subgroup_diagnostics(evaluation, samples, fields=["land_use"], min_samples=3, min_groups=2)
    selected = [r for r in result["per_repeat"] if r["subgroup"] == "tarım"]
    assert len(selected) == 2 and [r["n"] for r in selected] == [9, 9]
    assert [r["group_count"] for r in selected] == [5, 5]
    assert [r["interval"]["coverage"] for r in selected] == [1., 0.]
    assert [r["interval"]["empirical_undercoverage"] for r in selected] == [False, True]
    assert len(result["independent"]) == 1 and result["independent"][0]["n"] == 3
    assert evaluation == before[0] and samples["props"] == before[1]["props"]
    np.testing.assert_array_equal(samples["coordinates"], before[1]["coordinates"])
    order = list(reversed(range(15)))
    reordered = {**samples, "ids": [samples["ids"][i] for i in order], "props": [samples["props"][i] for i in order]}
    assert subgroup_diagnostics(evaluation, reordered, fields=["land_use"], min_samples=3, min_groups=2) == result
    json.dumps(result, allow_nan=False)


def test_tiny_and_incompletely_evaluated_subgroups_never_report_zero_coverage(evaluated_case):
    evaluation, samples = evaluated_case
    result = subgroup_diagnostics(evaluation, samples, fields=["land_use"], min_samples=5, min_groups=3)
    forest = next(r for r in result["per_repeat"] if r["subgroup"] == "orman")
    assert forest["n"] == 3 and forest["group_count"] == 2
    assert forest["interval"]["reason_code"] == "INSUFFICIENT_SUBGROUP_SIZE"
    assert "coverage" not in forest["interval"]
    evaluation["oof"][0].pop("upper")
    evaluation["oof"][0].pop("lower")
    result = subgroup_diagnostics(evaluation, samples, fields=["land_use"], min_samples=3, min_groups=2)
    farm = next(r for r in result["per_repeat"] if r["subgroup"] == "tarım" and r["repeat"] == 0)
    assert farm["interval"]["reason_code"] == "INCOMPLETE_INTERVALS"
    assert farm["interval"]["interval_count"] == 8 and "coverage" not in farm["interval"]


def test_explicit_spatial_null_intervals_preserve_subgroup_denominator(evaluated_case):
    evaluation, samples = evaluated_case
    evaluation["oof"][0].update(lower=None, upper=None, interval_width=None,
                                interval_status="unavailable", reason_code="SPATIAL_FINITE_MASS")
    result = subgroup_diagnostics(evaluation, samples, fields=["land_use"], min_samples=3, min_groups=2)
    farm = next(r for r in result["per_repeat"] if r["subgroup"] == "tarım" and r["repeat"] == 0)
    assert farm["n"] == 9 and farm["interval"]["interval_count"] == 8
    assert farm["interval"]["status"] == "unavailable" and "coverage" not in farm["interval"]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("changes", [
    {"lower": None, "upper": None},
    {"lower": None, "upper": None, "interval_status": "unavailable"},
    {"lower": None, "upper": 4., "interval_status": "unavailable", "reason_code": "SPATIAL_FINITE_MASS"},
    {"lower": float("nan"), "upper": float("nan"), "interval_status": "unavailable", "reason_code": "SPATIAL_FINITE_MASS"},
    {"lower": float("inf"), "upper": float("inf")},
    {"lower": 1., "upper": 2., "interval_status": "unavailable", "reason_code": "SPATIAL_FINITE_MASS"},
])
def test_malformed_or_unexplained_null_intervals_rejected(evaluated_case, changes):
    evaluation, samples = evaluated_case
    evaluation["oof"][0].update(changes)
    with pytest.raises(JobError, match="Interval|interval"):
        subgroup_diagnostics(evaluation, samples, fields=["land_use"])


def test_predefined_bins_retain_boundary_missing_and_empty_classes(evaluated_case):
    evaluation, samples = evaluated_case
    samples["props"][0].pop("depth")
    fields = [{"field": "depth", "bins": [0, 30, 80, 120, 200]},
              {"field": "predicted", "source": "row", "bins": [0, 5, 10]},
              {"field": "training_distance_m", "source": "row", "bins": [100, 300]}]
    result = subgroup_diagnostics(evaluation, samples, fields=fields, min_samples=3, min_groups=2)
    rows = [r for r in result["per_repeat"] if r["field"] == "depth" and r["repeat"] == 0]
    assert sum(r["n"] for r in rows) == 12
    assert next(r for r in rows if r["subgroup_id"] == "bin:2")["n"] == 5  # depths 30,40,50,60,70
    assert next(r for r in rows if r["subgroup_kind"] == "missing")["n"] == 1
    assert any(r["n"] == 0 and r["interval"]["status"] == "unavailable" for r in rows)
    assert result["definitions"] == [{"source": "property", **field} if "source" not in field else field for field in fields]
    with pytest.raises(JobError):
        subgroup_diagnostics(evaluation, samples, fields=[{"field": "residual", "source": "row", "bins": [0]}])


@pytest.mark.parametrize("alter,code", [
    (lambda ev: ev["oof"].append(copy.deepcopy(ev["oof"][0])), "DIAGNOSTIC_DUPLICATE_PREDICTION"),
    (lambda ev: ev["oof"][0].update(sample_id="unknown"), "DIAGNOSTIC_IDS"),
    (lambda ev: ev["oof"][0].update(residual=-55), "DIAGNOSTIC_RESIDUAL"),
    (lambda ev: ev["oof"][0].update(lower=50, upper=0), "DIAGNOSTIC_INTERVAL"),
    (lambda ev: ev["independent_predictions"].append(copy.deepcopy(ev["oof"][0])), "DIAGNOSTIC_LEAKAGE")])
def test_invalid_evaluated_rows_fail_closed(evaluated_case, alter, code):
    evaluation, samples = evaluated_case
    alter(evaluation)
    with pytest.raises(JobError) as error:
        subgroup_diagnostics(evaluation, samples, fields=["land_use"])
    assert error.value.code == code


def spatial_case(residuals):
    n = len(residuals)
    ids = [str(i) for i in range(n)]
    rows = [{"sample_id": str(i), "observed": 0., "predicted": float(value), "residual": float(value),
             "repeat": 0, "fold": 0, "group_id": str(i)} for i, value in enumerate(residuals)]
    samples = {"ids": ids, "coordinates": np.column_stack([500000 + np.arange(n) * 100, np.full(n, 4200000)]), "crs": "EPSG:32637"}
    return {"oof": rows, "independent_predictions": []}, samples


def test_moran_and_semivariogram_match_hand_computed_reference():
    evaluation, samples = spatial_case([-1, 0, 1])
    result = residual_spatial_diagnostics(evaluation, samples, distance_band_m=150, bin_edges_m=[0, 150, 250], min_samples=3, min_pairs=1)
    row = result["per_repeat"][0]
    assert row["moran_i"]["value"] == pytest.approx(0.)
    assert row["moran_i"]["s0"] == 3 and row["moran_i"]["p_value"] is None
    assert [r["pair_count"] for r in row["semivariogram"]] == [2, 1]
    assert [r["semivariance"] for r in row["semivariogram"]] == [0.5, 2.]
    all_pairs = residual_spatial_diagnostics(evaluation, samples, distance_band_m=250, bin_edges_m=[0, 250], min_samples=3, min_pairs=1)
    assert all_pairs["per_repeat"][0]["moran_i"]["value"] == pytest.approx(-.5)
    json.dumps(result, allow_nan=False)


def test_geographic_crs_requires_explicit_metric_projection_and_matches_projected_result():
    evaluation, samples = spatial_case([-1, 0, 1, 2, 1])
    projected = residual_spatial_diagnostics(evaluation, samples, distance_band_m=150, bin_edges_m=[0, 150, 500], min_pairs=1)
    source = copy.deepcopy(samples)
    source["coordinates"] = np.column_stack(Transformer.from_crs(32637, 4326, always_xy=True).transform(*samples["coordinates"].T))
    source["crs"] = "EPSG:4326"
    with pytest.raises(JobError) as error:
        residual_spatial_diagnostics(evaluation, source, distance_band_m=150, bin_edges_m=[0, 150, 500])
    assert error.value.code == "METRIC_CRS_REQUIRED"
    geographic = residual_spatial_diagnostics(evaluation, source, metric_crs="EPSG:32637", distance_band_m=150, bin_edges_m=[0, 150, 500], min_pairs=1)
    assert geographic["per_repeat"][0]["moran_i"]["value"] == pytest.approx(projected["per_repeat"][0]["moran_i"]["value"])
    assert geographic["distance_unit"] == "metre"
    assert [r["pair_count"] for r in geographic["per_repeat"][0]["semivariogram"]] == [4, 6]


def test_constant_residuals_no_neighbors_size_limit_and_small_bins_are_explicit():
    evaluation, samples = spatial_case([1, 1, 1, 1, 1])
    result = residual_spatial_diagnostics(evaluation, samples, distance_band_m=150, bin_edges_m=[0, 150, 500], min_pairs=8)
    assert result["per_repeat"][0]["moran_i"]["reason_code"] == "CONSTANT_RESIDUAL"
    assert all(r["status"] == "unavailable" and "semivariance" not in r for r in result["per_repeat"][0]["semivariogram"])
    result = residual_spatial_diagnostics(evaluation, samples, distance_band_m=50, bin_edges_m=[0, 500])
    assert result["per_repeat"][0]["moran_i"]["reason_code"] == "NO_NEIGHBORS"
    result = residual_spatial_diagnostics(evaluation, samples, distance_band_m=150, bin_edges_m=[0, 500], min_samples=3, max_samples=4)
    assert result["per_repeat"][0]["reason_code"] == "SPATIAL_SIZE_LIMIT"


def test_residual_repeats_not_pooled_and_source_is_not_mutated():
    evaluation, samples = spatial_case([-1, 0, 1, 0, -1])
    evaluation["oof"] += [{**r, "repeat": 1, "predicted": 4 * r["predicted"], "residual": 4 * r["residual"]} for r in evaluation["oof"]]
    before = copy.deepcopy(evaluation)
    result = residual_spatial_diagnostics(evaluation, samples, distance_band_m=150, bin_edges_m=[0, 150, 500], min_pairs=1)
    assert [r["n"] for r in result["per_repeat"]] == [5, 5]
    a, b = result["per_repeat"]
    assert b["semivariogram"][0]["semivariance"] == pytest.approx(16 * a["semivariogram"][0]["semivariance"])
    assert a["moran_i"]["value"] == pytest.approx(b["moran_i"]["value"])
    assert evaluation == before


def test_cancellation_is_propagated(importance_case, evaluated_case):
    def cancel():
        raise Cancelled()
    with pytest.raises(Cancelled):
        importance(importance_case, check_cancel=cancel)
    with pytest.raises(Cancelled):
        subgroup_diagnostics(*evaluated_case, fields=["land_use"], check_cancel=cancel)
    with pytest.raises(Cancelled):
        residual_spatial_diagnostics(*evaluated_case, distance_band_m=100, bin_edges_m=[0, 100], check_cancel=cancel)


def test_all_unavailable_folds_still_export_per_feature_unavailable_rows(importance_case):
    model, X, y, ids, _, names = importance_case
    case = (model, X, y, ids, ids, names)
    folds = [importance(case, permutation="within_group", fold=i) for i in range(2)]
    result = aggregate_permutation_importance(folds)
    assert len(result["per_repeat"]) == 3
    assert all(r["status"] == "unavailable" and r["available_fold_count"] == 0 for r in result["per_repeat"])


def test_incomparable_feature_groups_fail_in_aggregation(importance_case):
    first = importance(importance_case, feature_groups={"joint": ["signal", "noise1"]}, n_permutations=1)
    second = importance(importance_case, feature_groups={"joint": ["signal", "noise2"]}, fold=1, n_permutations=1)
    with pytest.raises(JobError) as error:
        aggregate_permutation_importance([first, second])
    assert error.value.code == "DIAGNOSTIC_INCOMPARABLE"


def test_negative_importance_is_preserved(importance_case):
    model, X, y, ids, groups, names = importance_case
    result = importance((model, X, -y, ids, groups, names), n_permutations=10)
    assert next(r for r in result["summary"] if r["feature"] == "signal")["importance_mean"] < -1


def test_known_nominal_level_must_match_evaluated_intervals(evaluated_case):
    evaluation, samples = evaluated_case
    evaluation["metrics"] = {"pooled_oof": {"interval": {"nominal_coverage": .8}}}
    with pytest.raises(JobError) as error:
        subgroup_diagnostics(evaluation, samples, fields=["land_use"], coverage=.9)
    assert error.value.code == "DIAGNOSTIC_COVERAGE"


def test_no_covariates_returns_unavailable_explanation():
    result = heldout_permutation_importance(RecordingModel(), np.zeros((3, 0)), np.zeros(3),
        ["a", "b", "c"], ["a", "b", "c"], [], repeat=0, fold=0)
    assert result["status"] == "unavailable" and result["reason_code"] == "NO_FEATURES"


def test_actual_fitted_workflow_never_refits_preprocessing_or_target(importance_case):
    from sps.modeling import fit_workflow
    model, X, y, ids, groups, names = importance_case
    covariates = [{"id": name, "kind": "continuous"} for name in names]
    settings = {"model": {"id": "ridge", "parameters": {"alpha": .1}},
        "preprocessing": {"imputation": "median", "scaling": "standard"},
        "target_transform": {"method": "none"}, "feature_selection": {"method": "none"},
        "vif": {"mode": "diagnostic"}, "uncertainty": {"method": "none"}}
    coordinates = np.column_stack([np.arange(len(X)) * 100., np.zeros(len(X))])
    audit = []
    fitted = fit_workflow(X[:40], y[:40], ids[:40], coordinates[:40], groups[:40], covariates, settings,
                          crs="EPSG:32637", role="diagnostic_test", learning_audit=audit)
    audit_before = copy.deepcopy(audit)
    predictions_before = fitted.predict(X[40:]).copy()
    fitted.pipeline.fit = lambda *a, **k: pytest.fail("Preprocessing must not refit")
    fitted.target_map.fit_transform = lambda *a, **k: pytest.fail("Target transformation must not refit")
    result = heldout_permutation_importance(fitted, X[40:], y[40:], ids[40:], groups[40:], covariates,
        repeat=0, fold=1, coordinates=coordinates[40:], training_groups=groups[:40], n_permutations=3)
    assert result["training_group_check"] is True
    assert next(r for r in result["summary"] if r["feature"] == "signal")["importance_mean"] > 8
    assert audit == audit_before
    np.testing.assert_array_equal(fitted.predict(X[40:]), predictions_before)
