"""Reports preserve held-out evidence, nonfinite status and plot provenance."""
import copy
import csv
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from sps.contract import JobError, Cancelled
from sps.reporting import build_reports


@pytest.fixture
def report_case():
    ids = ["Örnek-%02d" % i for i in range(8)]
    rows = [{"sample_id": identity, "repeat": 0, "fold": i % 2,
             "observed": float(i), "predicted": float(i) + 0.5 * (-1) ** i,
             "residual": 0.5 * (-1) ** i, "group_id": identity,
             "lower": float(i) - 1, "upper": float(i) + 1, "interval_width": 2.0}
            for i, identity in enumerate(ids[:6])]
    folds = [{"repeat": 0, "fold": fold,
              "train_ids": [r["sample_id"] for r in rows if r["fold"] != fold],
              "test_ids": [r["sample_id"] for r in rows if r["fold"] == fold],
              "excluded_ids": [], "diagnostics": {"train_groups": 3, "test_groups": 3}}
             for fold in range(2)]
    evaluation = {"oof": rows, "independent_predictions": [
        {"sample_id": ids[6], "repeat": 0, "fold": -1, "observed": 6, "predicted": 7,
         "residual": 1, "group_id": ids[6]}], "folds": folds,
        "metrics": {"pooled_oof": {"rmse": 0.5, "mae": 0.5, "me": 0, "r2": -0.2, "r2_reason": None},
                    "per_repeat": [{"repeat": 0, "rmse": 0.5, "r2": -0.2}],
                    "fold_mean": {"rmse": 0.5, "r2": float("nan")},
                    "independent": {"rmse": 1.0, "r2": float("nan")}},
        "fold_metrics": [{"repeat": 0, "fold": 0, "r2": -0.1}],
        "trials": [{"trial": 0, "parameters": {"alpha": 1.0}, "score": 0.5}],
        "learning_audit": [{"role": "inner_train", "sample_ids": ids[:3]}],
        "final_record": {"development_ids": ids[:4], "calibration_ids": ids[4:6],
                         "selected_ids": ["elevation"], "parameters": {"alpha": 1.0}},
        "final_model": object()}
    samples = {"ids": ids, "y": np.arange(8, dtype=float),
               "coordinates": np.column_stack([500000 + np.arange(8) * 100, 4200000 + np.arange(8) * 150]),
               "crs": "EPSG:32637", "props": [{} for _ in ids], "support": "point",
               "excluded": {}, "depth": [0, 20], "laboratory_method": "örnek yöntem"}
    settings = {"run_id": "test-report", "samples": {"unit": "g/kg", "target_field": "Toprak organik karbonu"},
                "validation": {"method": "group", "n_splits": 2, "repeats": 1, "seed": 42,
                               "independent_ids": [ids[6]]},
                "uncertainty": {"method": "split_conformal", "coverage": 0.9},
                "mapping": {"figure_dpi": 150}, "model": {"id": "ridge", "parameters": {"alpha": 1}}}
    return evaluation, samples, settings


def test_report_exports_actual_data_json_nonfinite_status_and_valid_figures(tmp_path, report_case):
    evaluation, samples, settings = report_case
    paths = build_reports(tmp_path, evaluation, samples, settings, check_cancel=lambda: None)
    assert len(paths) == len(set(paths)) and all(path.is_file() for path in paths)
    assert all(path.is_relative_to(tmp_path) for path in paths)
    result = json.loads((tmp_path / "evaluation.json").read_text(encoding="utf-8"))
    assert "final_model" not in result["evaluation"]
    assert result["evaluation"]["metrics"]["pooled_oof"]["r2"] == -0.2
    assert result["evaluation"]["metrics"]["independent"]["r2"] is None
    assert any("independent.r2" in row["path"] and row["reason"] for row in result["normalization"])
    assert "NaN" not in (tmp_path / "evaluation.json").read_text(encoding="utf-8")
    with (tmp_path / "oof_predictions.csv").open(encoding="utf-8-sig", newline="") as stream:
        exported = list(csv.DictReader(stream))
    assert [row["sample_id"] for row in exported] == [row["sample_id"] for row in evaluation["oof"]]
    assert [float(row["residual"]) for row in exported] == [row["residual"] for row in evaluation["oof"]]
    for path in paths:
        if path.suffix == ".svg":
            ET.parse(path)
        if path.suffix == ".png":
            with Image.open(path) as picture:
                assert picture.width == 960 and picture.height == 720
                assert picture.info["dpi"][0] == pytest.approx(150, abs=0.1)
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "Tanımsız" in html and "-0.2" in html
    assert "<td>belirleme katsayısının durumu</td><td>Tanımlı</td>" in html
    assert "ortalama kare hatanın karekökü" in html
    assert "bağımsız örnek sayısı" in html
    assert not any(mark in html for mark in ["\u2013", "\u2014", "\u2212"])


def test_repeats_not_counted_as_new_observations_and_maps_preserve_all_repeats(tmp_path, report_case):
    evaluation, samples, settings = report_case
    extra = copy.deepcopy(evaluation["oof"])
    for row in extra:
        row["repeat"] = 1
        row["predicted"] += 1
        row["residual"] += 1
    evaluation["oof"] += extra
    second_folds = copy.deepcopy(evaluation["folds"])
    for fold in second_folds:
        fold["repeat"] = 1
    evaluation["folds"] += second_folds
    settings["validation"]["repeats"] = 2
    build_reports(tmp_path, evaluation, samples, settings, check_cancel=lambda: None)
    manifest = json.loads((tmp_path / "figure_manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"]["oof_rows"] == 12
    assert manifest["counts"]["oof_unique_samples"] == 6
    maps = [f for f in manifest["figures"] if f["kind"] == "residual_map"]
    assert {f["repeat"] for f in maps} == {0, 1}
    assert all(f["source_rows"] == 6 for f in maps)
    assert all(f["connected_observations"] is False for f in manifest["figures"])


def test_undefined_metrics_missing_predictions_are_explicit_not_zero(tmp_path, report_case):
    evaluation, samples, settings = report_case
    evaluation["oof"][0]["predicted"] = float("inf")
    evaluation["oof"][0]["residual"] = float("inf")
    evaluation["metrics"]["pooled_oof"]["r2"] = float("-inf")
    build_reports(tmp_path, evaluation, samples, settings, check_cancel=lambda: None)
    manifest = json.loads((tmp_path / "figure_manifest.json").read_text(encoding="utf-8"))
    plot = next(item for item in manifest["figures"] if item["kind"] == "observed_predicted" and item["source"] == "oof")
    assert plot["source_rows"] == 6 and plot["plotted_rows"] == 5 and plot["nonfinite_rows"] == 1
    result = json.loads((tmp_path / "evaluation.json").read_text(encoding="utf-8"))
    assert result["evaluation"]["oof"][0]["predicted"] is None
    assert result["evaluation"]["metrics"]["pooled_oof"]["r2"] is None


def test_html_escapes_user_content_and_csv_is_safe_to_open(tmp_path, report_case):
    evaluation, samples, settings = report_case
    settings["samples"]["target_field"] = '<script>alert("x")</script>'
    evaluation["trials"][0]["label"] = '=HYPERLINK("file")'
    build_reports(tmp_path, evaluation, samples, settings, check_cancel=lambda: None)
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert '<script>alert("x")</script>' not in html and "&lt;script&gt;" in html
    with (tmp_path / "optimization_trials.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert next(csv.DictReader(stream))["label"].startswith("'=")


@pytest.mark.parametrize("corruption", ["residual", "interval_width", "group_leakage", "unknown_sample"])
def test_inconsistent_scientific_inputs_fail_before_report(tmp_path, report_case, corruption):
    evaluation, samples, settings = report_case
    if corruption == "residual":
        evaluation["oof"][0]["residual"] = 90
    elif corruption == "interval_width":
        evaluation["oof"][0]["interval_width"] = -1
    elif corruption == "group_leakage":
        evaluation["independent_predictions"][0]["group_id"] = evaluation["oof"][0]["group_id"]
    else:
        evaluation["oof"][0]["sample_id"] = "does-not-exist"
    with pytest.raises(JobError):
        build_reports(tmp_path, evaluation, samples, settings, check_cancel=lambda: None)
    assert not list(tmp_path.rglob("*.html"))


def test_cancel_during_plot_export_leaves_no_published_report(tmp_path, report_case):
    evaluation, samples, settings = report_case
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        if calls >= 7:
            raise Cancelled()
    with pytest.raises(Cancelled):
        build_reports(tmp_path, evaluation, samples, settings, check_cancel=cancel)
    assert not list(tmp_path.rglob("*.png"))
    assert not (tmp_path / "report.html").exists()
    assert not list(tmp_path.glob(".report-*"))


def test_existing_report_is_never_overwritten(tmp_path, report_case):
    evaluation, samples, settings = report_case
    (tmp_path / "report.html").write_text("previous", encoding="utf-8")
    with pytest.raises(JobError):
        build_reports(tmp_path, evaluation, samples, settings, check_cancel=lambda: None)
    assert (tmp_path / "report.html").read_text(encoding="utf-8") == "previous"


def test_reused_test_warning_replaces_independent_success_heading(tmp_path, report_case):
    evaluation, samples, settings = report_case
    evaluation["independent_test_history"] = {"status": "reused_after_settings_change",
        "previous_evaluations": 2, "settings_changed_since_previous": True,
        "warning": "Bu test kümesi daha önce kullanıldı <kontrol>."}
    build_reports(tmp_path, evaluation, samples, settings, check_cancel=lambda: None)
    report = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "<h3>Yeniden kullanılmış test</h3>" in report
    assert "<h3>Kilitli bağımsız test</h3>" not in report
    assert "Bu test kümesi daha önce kullanıldı &lt;kontrol&gt;." in report
    assert "yeni ve bağımsız bir nihai başarı ölçümü" in report


def test_single_test_observation_cannot_be_reported_with_perfect_r2(tmp_path, report_case):
    evaluation, samples, settings = report_case
    evaluation["metrics"]["independent"]["r2"] = 1.0
    with pytest.raises(JobError) as error:
        build_reports(tmp_path, evaluation, samples, settings, check_cancel=lambda: None)
    assert error.value.code == "REPORT_R2"


def test_independent_prediction_must_have_a_locked_test_id(tmp_path, report_case):
    evaluation, samples, settings = report_case
    settings["validation"]["independent_ids"] = []
    with pytest.raises(JobError) as error:
        build_reports(tmp_path, evaluation, samples, settings, check_cancel=lambda: None)
    assert error.value.code == "REPORT_INDEPENDENCE"


def coverage_case(evaluation):
    available = {"nominal_coverage": .5, "status": "available", "n": 6, "group_count": 6,
                 "coverage": .33, "mean_width": 1.0, "median_width": .8,
                 "below_lower_fraction": .33, "above_upper_fraction": .34,
                 "pinball_lower": .2, "pinball_upper": .25, "interval_score": 2.1, "crossing_count": 0}
    unavailable = {"nominal_coverage": .9, "status": "unavailable", "n": 6, "group_count": 6,
                   "reason_code": "CONFORMAL_RANK", "reason": "Kalibrasyon örneği sonlu sınır için yetersiz."}
    evaluation["coverage_diagnostics"] = {"levels": [.5, .9], "pooled_oof": [available, unavailable],
        "outer_folds": [{**available, "repeat": 0, "fold": 0}],
        "per_repeat": [{**available, "repeat": 0}],
        "independent": [{**available, "n": 1, "group_count": 1, "coverage": 1.0}],
        "scope": "fixed_fitted_models; separate calibration; no refit"}


def test_multilevel_coverage_preserves_unavailable_levels_and_flags_undercoverage(tmp_path, report_case):
    evaluation, samples, settings = report_case
    coverage_case(evaluation)
    build_reports(tmp_path, evaluation, samples, settings, check_cancel=lambda: None)
    with (tmp_path / "coverage_diagnostics.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 5
    unavailable = next(row for row in rows if row["scope"] == "pooled_oof" and row["status"] == "unavailable")
    assert unavailable["coverage"] == "" and unavailable["nominal_coverage"] == "0.9"
    assert unavailable["reason_code"] == "CONFORMAL_RANK"
    manifest = json.loads((tmp_path / "figure_manifest.json").read_text(encoding="utf-8"))
    curve = next(figure for figure in manifest["figures"] if figure["kind"] == "coverage_calibration")
    assert curve["plotted_rows"] == 2 and curve["unavailable_rows"] == 1
    assert curve["connected_observations"] is False
    report = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "Yetersiz ampirik kapsama" in report
    assert "Kalibrasyon örneği sonlu sınır için yetersiz." in report
    assert "Koşullu kapsama" in report
    assert "hücrede doğruluğu kanıtlanmış" in report


def test_invalid_coverage_is_rejected_without_clipping(tmp_path, report_case):
    evaluation, samples, settings = report_case
    coverage_case(evaluation)
    evaluation["coverage_diagnostics"]["pooled_oof"][0]["coverage"] = 1.01
    with pytest.raises(JobError) as error:
        build_reports(tmp_path, evaluation, samples, settings, check_cancel=lambda: None)
    assert error.value.code == "REPORT_COVERAGE"


def test_training_inner_outer_tables_and_preflight_keep_evaluation_roles_separate(tmp_path, report_case):
    evaluation, samples, settings = report_case
    evaluation["final_record"]["train_metrics"] = {"n": 4, "rmse": .1, "mae": .09, "r2": .9}
    evaluation["trials"] = [{"role": "outer/0/0/search", "trial": 0, "metric": "rmse", "value": .7,
                              "status": "complete", "elapsed_seconds": 2.5, "search_elapsed_seconds": 3.0, "selected": True,
                              "vif_mode": "diagnostic", "comparison_scope": "selector_and_vif_elimination",
                              "search_summary": {"completed_trials": 1, "failed_trials": 0, "elapsed_seconds": 3.1, "stop_reason": "timeout_between_trials"}}]
    evaluation["audit_path"] = "audit.json"
    preflight = {"folds": [{"repeat": 0, "fold": 0, "train_ids": samples["ids"][:3], "test_ids": samples["ids"][3:6],
        "diagnostics": {"train_groups": 3, "test_groups": 3, "minimum_train_test_distance_m": 151.2},
        "environmental_conditions": [{"id": "elevation", "train_min": 1, "train_max": 20, "test_min": 3, "test_max": 25},
                                     {"id": "land_use", "train_categories": [1, 2], "test_categories": [2, 3], "unknown_test_categories": [3]}]}],
        "missing_fraction": [.1], "workload": {"estimated_pipeline_fits_upper_bound": 80}, "warnings": ["Uzaklık uyarısı örneği."]}
    (tmp_path / "preflight.json").write_text(json.dumps(preflight), encoding="utf-8")
    build_reports(tmp_path, evaluation, samples, settings, check_cancel=lambda: None)
    report = (tmp_path / "report.html").read_text(encoding="utf-8")
    for title in ["Eğitim sonuçları", "İç doğrulama denemeleri", "Dış kat sonuçları", "Katların veri ve uzaklık özeti", "Çevresel koşullar"]:
        assert title in report
    assert "151.2" in report and "elevation" in report and 'href="audit.json"' in report
    assert "Eğitim verisine yeniden uygulanan model" in report
    assert "Denemeler arasında süre sınırı" in report and "3.1" in report
    assert "Kategorik çevresel koşullar" in report and "Uzaklık uyarısı örneği." in report
    assert "Yalnız tanı" in report and "varyans şişirme faktörüne dayalı eleme kapalıdır" in report


def test_900_dpi_is_real_export_resolution_not_grid_resolution(tmp_path, report_case):
    evaluation, samples, settings = report_case
    settings["mapping"]["figure_dpi"] = 900
    # One independent point is enough to exercise export without many large plots.
    evaluation["oof"] = []
    evaluation["folds"] = []
    evaluation["metrics"]["pooled_oof"] = {"n": 0, "r2": None}
    build_reports(tmp_path, evaluation, samples, settings, check_cancel=lambda: None)
    image_path = tmp_path / "figures" / "independent_observed_predicted.png"
    with Image.open(image_path) as picture:
        assert picture.size == (5760, 4320)
        assert picture.info["dpi"][0] == pytest.approx(900, abs=0.1)
    html = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "hücre boyutunu değiştirmez" in html
