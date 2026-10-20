"""Scientific contracts exercised on actual estimators and explicit sample identities."""
from __future__ import annotations
import copy
import importlib
import json
from pathlib import Path
import sys
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def module(name):
    assert (ROOT / "backend" / "sps" / (name + ".py")).is_file(), "Scientific module is not implemented: " + name
    return importlib.import_module("sps." + name)


@pytest.fixture
def scientific():
    rng = np.random.default_rng(31)
    X = rng.normal(size=(72, 4))
    X[:, 3] = np.tile([0, 1, 2], 24)
    y = 8 + 3 * X[:, 0] - 2 * X[:, 1] + X[:, 3] + rng.normal(0, .3, len(X))
    ids = ["S%03d" % i for i in range(len(X))]
    coords = np.column_stack([np.arange(len(X)) * 100, np.zeros(len(X))])
    groups = np.repeat(np.arange(36), 2).astype(str)
    covs = [{"id": "x%d" % j, "name": "variable%d" % j, "kind": "categorical" if j == 3 else "continuous", "unit": "1", "band": j+1, "path": "fixture.tif"} for j in range(4)]
    settings = {"validation": {"method": "group", "n_splits": 3, "inner_splits": 2, "repeats": 1, "seed": 42},
        "preprocessing": {"imputation": "median", "scaling": "standard", "unknown_category": "flag", "knn_neighbors": 3},
        "target_transform": {"method": "none"}, "feature_selection": {"method": "none", "k": 2},
        "vif": {"mode": "diagnostic", "threshold": 10}, "model": {"id": "ridge", "parameters": {"alpha": 1.0}},
        "optimization": {"method": "manual", "trials": 2, "seed": 42, "metric": "rmse", "n_jobs": 1, "timeout_seconds": 60},
        "uncertainty": {"method": "none", "coverage": .8, "calibration_fraction": .25}, "mapping": {}}
    return X, y, ids, coords, groups, covs, settings


def nested(case):
    X, y, ids, coords, groups, covs, settings = case
    return module("evaluation").evaluate_nested(X, y, ids, coords, groups, covs, settings,
        crs="EPSG:32637", check_cancel=lambda: None, progress=lambda *args: None)


def fit(case):
    X, y, ids, coords, groups, covs, settings = case
    return module("modeling").fit_workflow(X, y, ids, coords, groups, covs, settings, crs="EPSG:32637", role="test", learning_audit=[])


def test_outer_target_intervention_does_not_change_its_predictions(scientific):
    result = nested(scientific)
    first = result["folds"][0]
    altered = list(copy.deepcopy(scientific))
    lookup = {sid: i for i, sid in enumerate(altered[2])}
    altered[1][[lookup[sid] for sid in first["test_ids"]]] += 10000
    changed = nested(altered)
    key = (first["repeat"], first["fold"])
    original_rows = {r["sample_id"]: r["predicted"] for r in result["oof"] if (r["repeat"], r["fold"]) == key}
    changed_rows = {r["sample_id"]: r["predicted"] for r in changed["oof"] if (r["repeat"], r["fold"]) == key}
    assert original_rows == changed_rows
    for item in result["learning_audit"]:
        if item["role"].startswith("outer/0/0/"):
            assert not set(item["ids"]) & set(first["test_ids"])


def test_calibration_target_intervention_changes_only_interval(scientific):
    scientific[-1]["uncertainty"]["method"] = "split_conformal"
    result = nested(scientific)
    ids_cal = result["final_record"]["calibration_ids"]
    assert ids_cal
    altered = list(copy.deepcopy(scientific))
    altered[1][[altered[2].index(sid) for sid in ids_cal]] += 500
    changed = nested(altered)
    np.testing.assert_array_equal(result["final_model"].predict(scientific[0]), changed["final_model"].predict(scientific[0]))
    lower_a, upper_a = result["final_model"].predict_interval(scientific[0])
    lower_b, upper_b = changed["final_model"].predict_interval(scientific[0])
    assert np.mean(upper_b-lower_b) > np.mean(upper_a-lower_a) + 500
    for row in result["learning_audit"]:
        if row["role"].startswith("final/") and row["stage"] != "calibration":
            assert not set(row["ids"]) & set(ids_cal)


@pytest.mark.parametrize("model,params", [("mean", {}), ("median", {}), ("ridge", {}), ("elastic_net", {"alpha": .01}),
    ("random_forest", {"n_estimators": 12}), ("extra_trees", {"n_estimators": 12}), ("svr", {"C": 2.0}),
    ("hist_gradient_boosting", {"max_iter": 12, "min_samples_leaf": 4}), ("xgboost", {"n_estimators": 12, "max_depth": 2})])
def test_all_base_models_fit_real_data(scientific, model, params):
    scientific[-1]["model"] = {"id": model, "parameters": params}
    fitted = fit(scientific)
    predictions = fitted.predict(scientific[0][:7])
    assert predictions.shape == (7,) and np.isfinite(predictions).all()
    assert fitted.predict_interval(scientific[0][:7]) is None


@pytest.mark.parametrize("selector", ["none", "pearson", "spearman", "mutual_info", "elastic_net", "sfs", "rfe", "boruta", "pca"])
def test_all_selectors_use_training_data_and_record_features(scientific, selector):
    X, y, ids, coords, groups, covs, settings = scientific
    settings["feature_selection"].update(method=selector, k=2, threshold=.9)
    settings["model"] = {"id": "random_forest", "parameters": {"n_estimators": 10, "max_depth": 4}}
    fitted = fit(scientific)
    assert fitted.record["selected_features"]
    assert np.isfinite(fitted.predict(X[:5])).all()
    if selector not in ("none", "pearson", "spearman", "pca", "boruta"):
        assert len(fitted.record["selected_features"]) <= 2


def test_unseen_category_has_flag_and_error_policies(scientific):
    test = scientific[0][:2].copy(); test[0, 3] = 99
    fitted = fit(scientific)
    assert fitted.diagnostics(test)["unknown_category"].tolist() == [True, False]
    assert np.isfinite(fitted.predict(test)).all()
    scientific[-1]["preprocessing"]["unknown_category"] = "error"
    fitted = fit(scientific)
    with pytest.raises(Exception, match="kategori"):
        fitted.predict(test)


def test_negative_and_undefined_r2_and_signed_error():
    metrics = module("evaluation").regression_metrics([1, 2, 3], [3, 2, 1])
    assert metrics["r2"] == -3
    assert module("evaluation").regression_metrics([2, 2], [2, 2])["r2"] is None
    assert module("evaluation").regression_metrics([1], [4])["mean_error"] == 3


def test_small_calibration_cannot_invent_finite_bound():
    with pytest.raises(Exception, match="kalibrasyon"):
        module("uncertainty").finite_sample_quantile(np.array([1., 2.]), .95)
    assert module("uncertainty").finite_sample_quantile(np.array([1., 4., 2., 3.]), .6) == 3


@pytest.mark.parametrize("method,model", [("quantile_forest", "random_forest"), ("quantile_boosting", "hist_gradient_boosting"), ("cqr", "random_forest")])
def test_real_quantile_intervals(scientific, method, model):
    settings = scientific[-1]
    settings["model"] = {"id": model, "parameters": {"n_estimators": 15, "min_samples_leaf": 3} if model == "random_forest" else {"max_iter": 20, "min_samples_leaf": 4}}
    settings["uncertainty"].update(method=method, quantile_model="quantile_forest")
    result = nested(scientific)
    lower, upper = result["final_model"].predict_interval(scientific[0][:10])
    assert (upper >= lower).all() and (upper > lower).any()
    assert result["metrics"]["pooled_oof"]["interval"]["coverage"] >= 0
    assert result["final_record"]["uncertainty_method"] == method


@pytest.mark.parametrize("method", ["grid", "random", "optuna"])
def test_search_keeps_families_inside_inner_loop(scientific, method):
    settings = scientific[-1]
    settings["model_candidates"] = [{"id": "mean", "parameters": {}}, {"id": "ridge", "parameters": {}}]
    settings["optimization"].update(method=method, trials=3, spaces={"ridge.alpha": {"type": "categorical", "choices": [.1, 10]}})
    result = nested(scientific)
    assert result["trials"]
    assert all(t["role"] != "outer_test" for t in result["trials"])
    assert all(r["predicted"] is not None for r in result["oof"])


def test_log_smearing_mean_and_median_are_separate(scientific):
    X, y, ids, coords, groups, covs, settings = scientific
    y[:] = np.tile([1., 4., 16.], 24)
    settings["model"] = {"id": "mean", "parameters": {}}
    settings["target_transform"] = {"method": "log", "log_estimand": "median"}
    median = fit(scientific).predict(X[:1])[0]
    settings["target_transform"]["log_estimand"] = "mean_smearing"
    mean = fit(scientific).predict(X[:1])[0]
    assert median == pytest.approx(4)
    assert mean == pytest.approx(7)


def test_vif_iterative_preserves_named_variable(scientific):
    scientific[0][:, 1] = scientific[0][:, 0]
    scientific[-1]["vif"] = {"mode": "iterative", "threshold": 5, "protected_ids": ["x0"], "priority_ids": ["x1"]}
    fitted = fit(scientific)
    assert "x0" in fitted.record["selected_features"] and "x1" not in fitted.record["selected_features"]


def test_trusted_reload_checks_schema_and_digest_before_loading(scientific, tmp_path):
    evaluator = module("evaluation")
    fitted = fit(scientific)
    path = tmp_path / "model.joblib"
    record = evaluator.save_trusted_model(fitted, path, "run-1", scientific[5])
    restored = evaluator.load_trusted_model(path, "run-1", scientific[5], record["sha256"])
    np.testing.assert_array_equal(fitted.predict(scientific[0]), restored.predict(scientific[0]))
    with pytest.raises(Exception, match="şema"):
        evaluator.load_trusted_model(path, "run-1", scientific[5][::-1], record["sha256"])
    path.write_bytes(path.read_bytes() + b"corruption")
    with pytest.raises(Exception, match="özeti"):
        evaluator.load_trusted_model(path, "run-1", scientific[5], record["sha256"])


def test_independent_targets_never_change_development_model(scientific):
    scientific[-1]["validation"]["independent_ids"] = scientific[2][-8:]
    result = nested(scientific)
    changed = copy.deepcopy(scientific)
    changed[1][-8:] += 10000
    other = nested(changed)
    np.testing.assert_array_equal(result["final_model"].predict(scientific[0]), other["final_model"].predict(scientific[0]))
    assert len(result["independent_predictions"]) == 8
    for entry in result["learning_audit"]:
        assert not set(entry["ids"]) & set(scientific[2][-8:])


def test_inner_preprocessing_statistics_are_training_only(scientific):
    result = nested(scientific)
    fold = result["folds"][0]
    training_rows = [scientific[2].index(sid) for sid in fold["train_ids"]]
    expected_median = np.median(scientific[0][training_rows, :3], axis=0)
    record = result["fold_metrics"][0]["model_record"]
    np.testing.assert_allclose(record["preprocessing"]["imputation_statistics"], expected_median)
    for trial in result["trials"]:
        if trial["role"] == "outer/0/0/search":
            for inner in trial["inner_folds"]:
                assert set(inner["train_ids"]).isdisjoint(inner["test_ids"])
                assert set(inner["train_ids"] + inner["test_ids"]) <= set(fold["train_ids"])


def test_sfs_refits_preprocessing_inside_its_scoring_folds(scientific):
    scientific[-1]["feature_selection"].update(method="sfs", k=1)
    audit = []
    X, y, ids, coordinates, groups, covs, settings = scientific
    module("modeling").fit_workflow(X, y, ids, coordinates, groups, covs, settings, crs="EPSG:32637", role="sfs_test", learning_audit=audit)
    child = [entry for entry in audit if "/sfs/" in entry["role"] and entry["stage"] == "preprocessing"]
    assert len(child) == X.shape[1] * 2
    assert all(0 < len(entry["ids"]) < len(ids) for entry in child)
    first_candidate = [entry for entry in child if "/sfs/0/" in entry["role"]]
    assert set(first_candidate[0]["ids"]).isdisjoint(first_candidate[1]["ids"])


def test_cqr_keeps_negative_adjustment_and_rejects_empty_intervals():
    uncertainty = module("uncertainty")
    class FixedQuantiles:
        record = {}
        def predict(self, X): return np.zeros(len(X))
        def predict_interval(self, X): return -X[:, 0], X[:, 0]
        def quantile_crossings(self, X): return np.zeros(len(X), dtype=bool)
    model = uncertainty.calibrate(FixedQuantiles(), np.full((4, 1), 3.), np.zeros(4), list("abcd"),
        {"method": "cqr", "coverage": .6}, role="cal", learning_audit=[])
    assert model.adjustment == -3
    assert model.record["calibration"]["score"] == "max(lower-y,y-upper)"
    with pytest.raises(Exception, match="boş tahmin aralığı"):
        model.predict_interval(np.ones((1, 1)))


@pytest.mark.parametrize("imputation,scaling", [("median", "robust"), ("knn", "standard"), ("none", "none")])
def test_preprocessing_and_yeo_johnson_are_real_transformations(scientific, imputation, scaling):
    scientific[-1]["preprocessing"].update(imputation=imputation, scaling=scaling)
    scientific[-1]["target_transform"] = {"method": "yeo_johnson"}
    if imputation != "none":
        scientific[0][::5, 0] = np.nan
    fitted = fit(scientific)
    assert np.isfinite(fitted.predict(scientific[0])).all()
    assert np.isfinite(fitted.record["target_transform"]["lambda"])


def test_parallel_search_and_cancellation(scientific):
    scientific[-1]["optimization"].update(method="grid", n_jobs=2, trials=2, spaces={"alpha": {"type": "categorical", "choices": [.1, 10.]}})
    result = nested(scientific)
    assert len(result["trials"]) == 8
    from sps.contract import Cancelled
    def cancel(): raise Cancelled()
    X, y, ids, coordinates, groups, covs, settings = scientific
    with pytest.raises(Cancelled):
        module("evaluation").evaluate_nested(X, y, ids, coordinates, groups, covs, settings, crs="EPSG:32637", check_cancel=cancel, progress=lambda *args: None)


def test_nonlinear_svr_rfe_is_not_falsely_enabled(scientific):
    scientific[-1]["model"] = {"id": "svr", "parameters": {"kernel": "rbf"}}
    scientific[-1]["feature_selection"].update(method="rfe", k=1)
    with pytest.raises(Exception, match="katsayı veya önem"):
        fit(scientific)


def test_baseline_quantile_family_mismatch_is_rejected(scientific):
    scientific[-1]["uncertainty"]["method"] = "quantile_forest"
    with pytest.raises(Exception, match="rastgele orman"):
        fit(scientific)


def test_onehot_expansion_enforces_budget_before_dense_allocation():
    pipeline = module("pipeline")
    covariates = [{"id": "category", "kind": "categorical"}]
    encoder = pipeline.CovariateEncoder(covariates, {"imputation": "none"})
    with pytest.raises(Exception, match="bellek"):
        encoder.fit(np.arange(10000.)[:, None])
    encoder.fit(np.arange(4000.)[:, None])
    with pytest.raises(Exception, match="bellek"):
        encoder.transform(np.tile(np.arange(4000.), 3)[:, None])


def test_discarded_features_do_not_affect_knn_or_unknown_category_prediction(scientific):
    scientific[0][::4, 0] = np.nan
    scientific[-1]["preprocessing"]["imputation"] = "knn"
    scientific[-1]["preprocessing"]["unknown_category"] = "error"
    scientific[-1]["feature_selection"].update(method="mutual_info", k=1, protected_ids=["x0"])
    fitted = fit(scientific)
    altered = scientific[0].copy()
    altered[:, 1:] += 10000
    np.testing.assert_array_equal(fitted.predict(scientific[0]), fitted.predict(altered))
    assert not fitted.diagnostics(altered)["unknown_category"].any()


@pytest.mark.parametrize("search", ["manual", "grid", "random", "optuna"])
def test_no_selection_comparison_is_selected_only_in_shared_inner_folds(scientific, search):
    scientific[-1]["feature_selection"].update(method="mutual_info", k=1, protected_ids=["x0"], compare_without_selection=True)
    scientific[-1]["optimization"].update(method=search, trials=2)
    result = nested(scientific)
    for role in {trial["role"] for trial in result["trials"]}:
        trials = [trial for trial in result["trials"] if trial["role"] == role]
        assert {trial["feature_selection_method"] for trial in trials} == {"none", "mutual_info"}
        assert {trial["comparison_arm"] for trial in trials} == {"without_selection", "with_selection"}
        assert all(trial["inner_folds"] == trials[0]["inner_folds"] for trial in trials)
    # The deliberately protected single feature omits the known second signal.
    assert result["final_record"]["selected_comparison_arm"] == "without_selection"
    assert result["final_record"]["selection_method"] == "none"


@pytest.mark.parametrize("method,model", [("split_conformal", "ridge"), ("quantile_forest", "random_forest"), ("cqr", "random_forest")])
def test_multilevel_coverage_uses_fixed_models_and_separate_calibration(scientific, method, model):
    scientific[-1]["model"] = {"id": model, "parameters": {"n_estimators": 10, "min_samples_leaf": 3} if model == "random_forest" else {"alpha": 1}}
    scientific[-1]["uncertainty"].update(method=method, quantile_model="quantile_forest")
    result = nested(scientific)
    diagnostics = result["coverage_diagnostics"]
    assert diagnostics["levels"] == [.5, .8, .9]
    assert len(diagnostics["outer_folds"]) == 9
    available = [row for row in diagnostics["pooled_oof"] if row["status"] == "available"]
    assert any(row["nominal_coverage"] == .9 for row in available)
    assert all(0 <= row["coverage"] <= 1 and row["n"] == 72 and row["group_count"] == 36 for row in available)
    assert sum(item["stage"] == "point_model" for item in result["learning_audit"]) == 12
    if method in ("split_conformal", "cqr"):
        assert result["final_record"]["calibration"]["levels"]["0.9"]["status"] == "available"


def test_quantile_boosting_does_not_invent_untrained_quantiles(scientific):
    scientific[-1]["model"] = {"id": "hist_gradient_boosting", "parameters": {"max_iter": 8, "min_samples_leaf": 4}}
    scientific[-1]["uncertainty"]["method"] = "quantile_boosting"
    result = nested(scientific)
    rows = {row["nominal_coverage"]: row for row in result["coverage_diagnostics"]["pooled_oof"]}
    assert rows[.8]["status"] == "available"
    assert rows[.5]["status"] == rows[.9]["status"] == "unavailable"
    assert "QUANTILE_LEVEL_NOT_FITTED" in rows[.5]["reason_codes"]
    assert rows[.5]["n"] == rows[.5]["distinct_sample_count"] == 72
    assert rows[.5]["group_count"] == 36


def test_multilevel_interval_widths_do_not_use_outer_targets(scientific):
    scientific[-1]["uncertainty"]["method"] = "split_conformal"
    result = nested(scientific)
    first = result["folds"][0]
    changed = copy.deepcopy(scientific)
    changed[1][[changed[2].index(sid) for sid in first["test_ids"]]] += 10000
    other = nested(changed)
    key = (first["repeat"], first["fold"])
    a = [row["mean_width"] for row in result["coverage_diagnostics"]["outer_folds"] if (row["repeat"], row["fold"]) == key]
    b = [row["mean_width"] for row in other["coverage_diagnostics"]["outer_folds"] if (row["repeat"], row["fold"]) == key]
    assert a == b


def test_selection_and_family_comparison_budget_and_conditional_spaces(scientific):
    scientific[-1]["feature_selection"].update(method="mutual_info", k=1, compare_without_selection=True)
    scientific[-1]["model_candidates"] = [{"id": "mean", "parameters": {}}, {"id": "ridge", "parameters": {}}]
    scientific[-1]["optimization"].update(method="grid", trials=3, spaces={"feature_selection.k": {"type": "categorical", "choices": [1, 2]}})
    with pytest.raises(Exception, match="birleşimlerinin"):
        nested(scientific)
    scientific[-1]["optimization"]["trials"] = 4
    result = nested(scientific)
    for role in {row["role"] for row in result["trials"]}:
        rows = [row for row in result["trials"] if row["role"] == role]
        assert len({(row["model"]["id"], row["feature_selection_method"]) for row in rows}) == 4
        assert all("feature_selection.k" not in row["parameters"] for row in rows if row["comparison_arm"] == "without_selection")


def test_unavailable_high_coverage_does_not_invent_a_bound():
    uncertainty = module("uncertainty")
    class FixedPoint:
        record = {}
        def predict(self, X): return np.zeros(len(X))
        def quantile_crossings(self, X): return np.zeros(len(X), dtype=bool)
    model = uncertainty.calibrate(FixedPoint(), np.zeros((4, 1)), np.arange(4.), list("abcd"),
        {"method": "split_conformal", "coverage": .8}, role="cal", learning_audit=[])
    assert model.record["calibration"]["levels"]["0.9"]["reason_code"] == "CALIBRATION_SIZE"
    with pytest.raises(Exception, match="kalibrasyon örneği yetersiz"):
        model.predict_interval_at_coverage(np.zeros((1, 1)), .9)


def test_calibrated_multilevel_model_survives_trusted_reload(scientific, tmp_path):
    scientific[-1]["uncertainty"]["method"] = "split_conformal"
    result = nested(scientific)
    path = tmp_path / "calibrated.joblib"
    evaluator = module("evaluation")
    saved = evaluator.save_trusted_model(result["final_model"], path, "calibrated-run", scientific[5])
    restored = evaluator.load_trusted_model(path, "calibrated-run", scientific[5], saved["sha256"])
    for level in (.5, .8, .9):
        old = result["final_model"].predict_interval_at_coverage(scientific[0], level)
        new = restored.predict_interval_at_coverage(scientific[0], level)
        np.testing.assert_array_equal(old, new)


def test_cqr_boosting_retains_nominal_only_without_refitting(scientific):
    scientific[-1]["model"] = {"id": "hist_gradient_boosting", "parameters": {"max_iter": 8, "min_samples_leaf": 4}}
    scientific[-1]["uncertainty"].update(method="cqr", quantile_model="quantile_boosting")
    result = nested(scientific)
    rows = {row["nominal_coverage"]: row for row in result["coverage_diagnostics"]["pooled_oof"]}
    assert rows[.8]["status"] == "available"
    assert rows[.5]["status"] == rows[.9]["status"] == "unavailable"
    assert "QUANTILE_LEVEL_NOT_FITTED" in rows[.9]["reason_codes"]


def test_without_selection_arm_retains_variables_removed_by_iterative_vif(scientific):
    scientific[0][:, 1] = scientific[0][:, 0]
    scientific[-1]["feature_selection"].update(method="mutual_info", k=1, compare_without_selection=True)
    scientific[-1]["vif"] = {"mode": "iterative", "threshold": 5, "protected_ids": ["x0"], "priority_ids": ["x1"]}
    result = nested(scientific)
    baseline = [row for row in result["learning_audit"] if row["role"].startswith("final/search/trial1/") and row["stage"] == "feature_selection"]
    assert baseline and all("x1" in row["selected_features"] for row in baseline)
    assert result["final_record"]["comparison_scope"] == "selector_and_vif_elimination"
    assert all(row["vif_mode"] == "diagnostic" for row in result["trials"] if row["comparison_arm"] == "without_selection")
