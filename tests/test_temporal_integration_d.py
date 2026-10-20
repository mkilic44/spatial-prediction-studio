"""Actual Ridge fits test temporal separation through nested model development."""
from __future__ import annotations

import copy
from datetime import date, timedelta
import json
from pathlib import Path
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from sps.contract import JobError
from sps.evaluation import evaluate_nested
from sps.modeling import fit_workflow
from sps.validation import make_folds, split_calibration


def case(periods=32, initial=12, *, uncertainty="split_conformal", gap_days=1):
    rng = np.random.default_rng(419)
    n = periods * 2
    X = rng.normal(size=(n, 3))
    y = 12 + 3 * X[:, 0] - 2 * X[:, 1] + rng.normal(0, .2, n)
    ids = ["s%03d" % i for i in range(n)]
    coordinates = np.column_stack([400000. + np.arange(n) * 100., np.full(n, 4200000.)])
    # Two distinct profiles share each date. Same-day ties may never cross roles.
    groups = np.asarray(["profile%03d" % i for i in range(n)])
    offsets = np.r_[0, np.cumsum(np.resize([3, 5, 2, 7], periods - 1))]
    dates = {identity: (date(2020, 1, 1) + timedelta(days=int(offsets[i // 2]))).isoformat()
             for i, identity in enumerate(ids)}
    covariates = [{"id": "x%d" % i, "kind": "continuous", "unit": "1"} for i in range(3)]
    settings = {
        "validation": {"method": "temporal", "temporal_mode": "temporal_new_groups", "timezone": "UTC",
            "gap_days": gap_days, "initial_train_periods": initial, "inner_initial_train_periods": 2,
            "n_splits": 3, "inner_splits": 2, "repeats": 1, "seed": 42, "dates_by_id": dates},
        "preprocessing": {"imputation": "median", "scaling": "standard", "unknown_category": "flag"},
        "target_transform": {"method": "none"}, "feature_selection": {"method": "none"},
        "vif": {"mode": "diagnostic", "threshold": 10},
        "model": {"id": "ridge", "parameters": {"alpha": .1}},
        "optimization": {"method": "manual", "metric": "rmse", "n_jobs": 1, "trials": 1, "seed": 42, "timeout_seconds": 60},
        "uncertainty": {"method": uncertainty, "coverage": .8, "calibration_fraction": .25,
            "spatial_bandwidth_m": 100000., "spatial_kernel": "gaussian",
            "spatial_min_groups": 3, "spatial_min_effective_n": 5},
        "mapping": {}}
    return X, y, ids, coordinates, groups, covariates, settings


def run(data):
    return evaluate_nested(*data, crs="EPSG:32637", check_cancel=lambda: None, progress=lambda *args: None)


def assert_before(before, after, settings):
    dates = settings["validation"]["dates_by_id"]
    gap = timedelta(days=settings["validation"]["gap_days"])
    assert before and after
    assert max(date.fromisoformat(dates[i]) for i in before) + gap < min(date.fromisoformat(dates[i]) for i in after)
    assert set(before).isdisjoint(after)


def canonical_predictions(rows):
    keys = ("sample_id", "repeat", "fold", "predicted", "lower", "upper", "interval_status")
    return [{key: row[key] for key in keys if key in row} for row in rows]


def stable_trials(rows, role=None):
    omit = {"elapsed_seconds", "search_elapsed_seconds", "search_summary"}
    return [{key: value for key, value in row.items() if key not in omit}
            for row in rows if role is None or row["role"] == role]


@pytest.mark.parametrize("uncertainty", ["none", "split_conformal"])
def test_24_periods_initial_history_is_not_scored_or_called_independent(uncertainty):
    data = case(periods=24, initial=8, uncertainty=uncertainty)
    result = run(data)
    tested = {identity for fold in result["folds"] for identity in fold["test_ids"]}
    assert tested == set(data[2][16:])
    assert {row["sample_id"] for row in result["oof"]} == tested
    assert len(result["oof"]) == result["metrics"]["pooled_oof"]["n"] == 32
    assert result["metrics"]["per_repeat"][0]["n"] == 32
    assert result["independent_predictions"] == [] and result["metrics"]["independent"] is None
    assert result["final_record"]["independent_ids"] == []
    for summary in result["coverage_diagnostics"]["pooled_oof"]:
        assert summary["n"] == 32 and summary["distinct_sample_count"] == 32


@pytest.mark.parametrize("uncertainty", ["split_conformal", "spatial_conformal"])
def test_every_real_fit_uses_older_history_than_calibration_and_outer_test(uncertainty):
    data = case(uncertainty=uncertainty)
    result = run(data)
    settings = data[-1]
    for fold, evaluated in zip(result["folds"], result["fold_metrics"]):
        record = evaluated["model_record"]
        development, calibration = record["development_ids"], record["calibration_ids"]
        assert_before(development, calibration, settings)
        assert_before(calibration, fold["test_ids"], settings)
        assert_before(fold["train_ids"], fold["test_ids"], settings)
        dates = settings["validation"]["dates_by_id"]
        # Calibration consists of the last whole dates in this outer training partition.
        assert max(dates[i] for i in calibration) == max(dates[i] for i in fold["train_ids"])
        boundary = min(dates[i] for i in calibration)
        assert set(calibration) == {i for i in fold["train_ids"] if dates[i] >= boundary}
        role = "outer/%d/%d" % (fold["repeat"], fold["fold"])
        trials = [row for row in result["trials"] if row["role"] == role + "/search"]
        for trial in trials:
            for inner in trial["inner_folds"]:
                assert_before(inner["train_ids"], inner["test_ids"], settings)
                assert set(inner["train_ids"] + inner["test_ids"]) <= set(development)
                assert set(inner["train_ids"] + inner["test_ids"]).isdisjoint(calibration)
        for audit in result["learning_audit"]:
            if audit["role"].startswith(role + "/"):
                assert set(audit["ids"]).isdisjoint(fold["test_ids"])
                if audit["stage"] != "calibration":
                    assert set(audit["ids"]) <= set(development)
    assert_before(result["final_record"]["development_ids"], result["final_record"]["calibration_ids"], settings)


def test_latest_calibration_and_full_evaluation_are_row_and_seed_stable():
    data = case(gap_days=4)
    ids, xy, groups, settings = data[2], data[3], data[4], data[-1]
    train, calibration = split_calibration(ids, xy, groups, settings["validation"], .25, crs="EPSG:32637", seed=1)
    assert_before(train, calibration, settings)
    assert calibration == ids[-16:]
    reverse = np.arange(len(ids))[::-1]
    same = split_calibration([ids[i] for i in reverse], xy[reverse], groups[reverse], settings["validation"], .25, crs="EPSG:32637", seed=999)
    assert same == (train, calibration)
    permuted = list(copy.deepcopy(data))
    for index in (0, 1, 3, 4):
        permuted[index] = permuted[index][reverse]
    permuted[2] = [ids[i] for i in reverse]
    original, reordered = run(data), run(permuted)
    assert original["folds"] == reordered["folds"]
    assert canonical_predictions(original["oof"]) == canonical_predictions(reordered["oof"])
    assert stable_trials(original["trials"]) == stable_trials(reordered["trials"])


def test_future_independent_targets_cannot_change_selection_or_final_predictions():
    data = case()
    data[-1]["validation"]["independent_ids"] = data[2][-8:]
    data[-1]["optimization"].update(method="grid", trials=2, spaces={"ridge.alpha": {"type": "categorical", "choices": [.1, 10.]}})
    original = run(data)
    changed = copy.deepcopy(data)
    changed[1][-8:] += 100000.
    other = run(changed)
    assert canonical_predictions(original["oof"]) == canonical_predictions(other["oof"])
    assert canonical_predictions(original["independent_predictions"]) == canonical_predictions(other["independent_predictions"])
    assert original["final_record"] == other["final_record"]
    assert stable_trials(original["trials"]) == stable_trials(other["trials"])
    assert original["learning_audit"] == other["learning_audit"]
    assert original["metrics"]["independent"]["rmse"] != other["metrics"]["independent"]["rmse"]
    assert_before(original["final_record"]["calibration_ids"], data[2][-8:], data[-1])
    for entry in original["learning_audit"]:
        assert set(entry["ids"]).isdisjoint(data[2][-8:])


def test_future_outer_targets_cannot_change_its_fold_model_or_prior_models():
    data = case()
    original = run(data)
    target_fold = original["folds"][1]
    changed = copy.deepcopy(data)
    for identity in target_fold["test_ids"]:
        changed[1][changed[2].index(identity)] -= 5000.
    other = run(changed)
    for fold_number in (0, 1):
        before = [r for r in original["oof"] if r["fold"] == fold_number]
        after = [r for r in other["oof"] if r["fold"] == fold_number]
        assert canonical_predictions(before) == canonical_predictions(after)
        assert original["fold_metrics"][fold_number]["model_record"] == other["fold_metrics"][fold_number]["model_record"]
        role = "outer/0/%d/search" % fold_number
        assert stable_trials(original["trials"], role) == stable_trials(other["trials"], role)
    # Later history legitimately sees those former test observations. Here they
    # become calibration rows, so interval calibration changes while fit may not.
    assert original["fold_metrics"][2]["model_record"]["calibration"] != other["fold_metrics"][2]["model_record"]["calibration"]


def test_latest_calibration_targets_change_interval_without_refitting_final_model():
    data = case()
    original = run(data)
    changed = copy.deepcopy(data)
    calibration_ids = original["final_record"]["calibration_ids"]
    for identity in calibration_ids:
        changed[1][changed[2].index(identity)] += 1000.
    other = run(changed)
    np.testing.assert_array_equal(original["final_model"].predict(data[0]), other["final_model"].predict(data[0]))
    lower, upper = original["final_model"].predict_interval(data[0])
    new_lower, new_upper = other["final_model"].predict_interval(data[0])
    assert np.mean(new_upper - new_lower) > np.mean(upper - lower) + 1000
    assert stable_trials(original["trials"], "final/search") == stable_trials(other["trials"], "final/search")


def test_direct_sequential_selector_subfits_use_forward_folds_and_train_only(monkeypatch):
    import sps.validation as validation
    data = case(uncertainty="none")
    data[-1]["feature_selection"] = {"method": "sfs", "k": 1}
    calls = []
    original_make = validation.make_folds
    def capture(ids, xy, groups, config, **kwargs):
        folds = original_make(ids, xy, groups, config, **kwargs)
        calls.append((list(ids), copy.deepcopy(config), copy.deepcopy(folds)))
        return folds
    monkeypatch.setattr(validation, "make_folds", capture)
    audit = []
    model = fit_workflow(*data, crs="EPSG:32637", role="selector", learning_audit=audit)
    assert len(model.record["selected_features"]) == 1 and calls
    for ids, config, folds in calls:
        assert config["method"] == "temporal"
        for fold in folds:
            assert_before(fold["train_ids"], fold["test_ids"], data[-1])
            assert set(fold["train_ids"] + fold["test_ids"]) <= set(ids)
            fits = [row for row in audit if row["stage"] == "preprocessing" and row["role"].endswith("/%d" % fold["fold"])]
            assert len(fits) == data[0].shape[1]
            assert all(set(row["ids"]) == set(fold["train_ids"]) for row in fits)


def test_nested_sequential_selector_uses_its_explicit_shorter_initial_history(monkeypatch):
    import sps.validation as validation
    data = case(periods=64, initial=32, uncertainty="none")
    data[-1]["validation"]["inner_initial_train_periods"] = 8
    data[-1]["validation"]["selector_initial_train_periods"] = 1
    data[-1]["feature_selection"] = {"method": "sfs", "k": 1}
    calls = []
    original_make = validation.make_folds
    def capture(ids, xy, groups, config, **kwargs):
        folds = original_make(ids, xy, groups, config, **kwargs)
        calls.append((tuple(ids), copy.deepcopy(config), copy.deepcopy(folds)))
        return folds
    monkeypatch.setattr(validation, "make_folds", capture)
    result = run(data)
    assert result["metrics"]["pooled_oof"]["n"] == 64
    assert all(row["status"] == "complete" for row in result["trials"])
    selections = [row for row in result["learning_audit"] if row["stage"] == "sfs_selection"]
    assert selections and calls
    for selection in selections:
        source = next(call for call in calls if set(call[0]) == set(selection["ids"]))
        assert source[1]["method"] == "temporal"
        assert source[1]["inner_initial_train_periods"] == 1
        for fold in source[2]:
            assert_before(fold["train_ids"], fold["test_ids"], data[-1])
            fits = [row for row in result["learning_audit"] if row["stage"] == "preprocessing"
                    and row["role"].startswith(selection["role"] + "/sfs/")
                    and row["role"].endswith("/%d" % fold["fold"])]
            assert len(fits) == data[0].shape[1]
            assert all(set(row["ids"]) == set(fold["train_ids"]) for row in fits)
    assert data[-1]["validation"]["inner_initial_train_periods"] == 8


def test_selector_calendar_gap_failure_never_reverts_to_random_folds():
    data = case(periods=64, initial=32, uncertainty="none", gap_days=20)
    data[-1]["validation"].update(inner_initial_train_periods=8, selector_initial_train_periods=1)
    data[-1]["feature_selection"] = {"method": "sfs", "k": 1}
    with pytest.raises(JobError) as error:
        run(data)
    assert error.value.code == "SEARCH_FAILED"
    assert "eğitim grubu" in str(error.value) or "yeterli dönem" in str(error.value)


def test_evaluation_inputs_are_unchanged_and_export_contains_no_nonfinite_values():
    data = case()
    before = copy.deepcopy(data)
    result = run(data)
    for index in (0, 1, 3, 4):
        np.testing.assert_array_equal(data[index], before[index])
    for index in (2, 5, 6):
        assert data[index] == before[index]
    json.dumps({key: value for key, value in result.items() if key != "final_model"}, allow_nan=False)


def test_old_independent_dates_are_rejected_before_model_fit(monkeypatch):
    import sps.evaluation as evaluation
    data = case()
    data[-1]["validation"]["independent_ids"] = data[2][:4]
    monkeypatch.setattr(evaluation, "fit_workflow", lambda *a, **k: pytest.fail("Model fitting must not start for invalid chronology"))
    with pytest.raises(JobError) as error:
        run(data)
    assert error.value.code == "TEMPORAL_ORDER"


def test_large_calendar_gap_rejects_insufficient_inner_history_without_random_fallback():
    data = case(gap_days=20)
    with pytest.raises(JobError) as error:
        run(data)
    assert error.value.code in {"INSUFFICIENT_TRAIN_GROUPS", "TEMPORAL_PERIODS", "TEMPORAL_CALIBRATION"}


def test_temporal_record_uses_actual_groups_and_pre_exclusion_denominators():
    data = case(gap_days=4)
    # Early profiles have two observations; later profiles have one each.
    # Group and sample calibration fractions must therefore differ in the final fit.
    data[4][:32] = ["paired%03d" % (i // 2) for i in range(32)]
    result = run(data)
    lookup = dict(zip(data[2], data[4]))
    dates = data[-1]["validation"]["dates_by_id"]
    partitions = [(row["model_record"], fold["train_ids"])
                  for row, fold in zip(result["fold_metrics"], result["folds"])]
    partitions.append((result["final_record"], data[2]))
    for record, partition in partitions:
        temporal = record["temporal"]
        development, calibration = record["development_ids"], record["calibration_ids"]
        assert temporal["requested_calibration_group_fraction"] == .25
        assert temporal["actual_calibration_sample_fraction"] == len(calibration) / len(partition)
        assert temporal["actual_calibration_group_fraction"] == len({lookup[i] for i in calibration}) / len({lookup[i] for i in partition})
        assert temporal["denominator"] == "partition_before_calendar_and_spatial_exclusions"
        assert temporal["development_samples"] == len(development)
        assert temporal["development_groups"] == len({lookup[i] for i in development})
        assert temporal["calibration_samples"] == len(calibration)
        assert temporal["calibration_groups"] == len({lookup[i] for i in calibration})
        assert temporal["excluded_samples"] == len(record["excluded_ids"])
        for name, identities in (("development_dates", development), ("calibration_dates", calibration)):
            assert temporal[name] == {"start": min(dates[i] for i in identities), "end": max(dates[i] for i in identities)}
        assert temporal["last_used_date"] == max(dates[i] for i in development + calibration)
        assert temporal["timezone"] == "UTC" and temporal["gap_days"] == 4
    final = result["final_record"]["temporal"]
    assert final["actual_calibration_sample_fraction"] == .1875
    assert final["actual_calibration_group_fraction"] == .25
    assert any(record["temporal"]["excluded_samples"] > 0 for record, _ in partitions)


def test_temporal_record_without_calibration_has_no_invented_calibration_dates():
    result = run(case(periods=24, initial=8, uncertainty="none"))
    record = result["final_record"]["temporal"]
    assert record["calibration_dates"] is None and record["requested_calibration_group_fraction"] is None
    assert record["calibration_samples"] == record["calibration_groups"] == 0
    assert record["actual_calibration_sample_fraction"] == record["actual_calibration_group_fraction"] == 0


def test_engine_preview_counts_initial_history_and_rejects_changed_calendar_gap(tmp_path):
    from sps.engine_b import run_job_b
    from test_engine_b import request
    req = request(tmp_path)
    samples = json.loads(Path(req["samples"]["path"]).read_text(encoding="utf-8"))
    samples["features"] = samples["features"][:48]
    assert len(samples["features"]) == 48
    for i, feature in enumerate(samples["features"]):
        feature["properties"]["date"] = (date(2020, 1, 1) + timedelta(days=(i // 2) * 3)).isoformat()
    source = tmp_path / "dated.geojson"
    source.write_text(json.dumps(samples), encoding="utf-8")
    req["samples"].update(path=str(source), date_field="date")
    independent = [str(row["properties"]["sample_id"]) for row in samples["features"][-4:]]
    req["validation"] = {"method": "temporal", "n_splits": 3, "inner_splits": 2,
        "initial_train_periods": 8, "inner_initial_train_periods": 2, "gap_days": 1,
        "independent_ids": independent}
    preview = run_job_b(req)
    assert preview["status"] == "completed", preview
    preflight = json.loads((tmp_path / "preview" / "preflight.json").read_text(encoding="utf-8"))
    assert preflight["sample_count"] == 48 and preflight["independent_test_count"] == 4
    assert preflight["initial_training_count"] == 16 and preflight["outer_tested_count"] == 28
    roles = json.loads((tmp_path / "preview" / "folds.geojson").read_text(encoding="utf-8"))["features"]
    assert {r["properties"]["sample_id"] for r in roles if r["properties"]["role"] == "independent"} == set(independent)
    assert len({r["properties"]["sample_id"] for r in roles if r["properties"]["role"] == "initial_training"}) == 16
    req.update(action="run", preview_fingerprint=preview["preview_fingerprint"], output_dir=str(tmp_path / "stale"))
    req["validation"]["gap_days"] = 2
    rejected = run_job_b(req)
    assert rejected["status"] == "failed" and rejected["error"]["code"] == "PREVIEW_STALE"
