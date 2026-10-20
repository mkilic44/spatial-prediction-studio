"""Read a new installed MCP run and compare it with the accepted Ridge reference.

Starts no process, imports no model, and writes only the requested evidence JSON.
Missing actual-run evidence cannot produce a passed result.
"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import zipfile

from verify_d_acceptance import verify_pagination

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "build/arcmap-evidence/mcp-runs/20261018_035902_48c39f66/output"
CANDIDATE_SHA = "b8831e222582b334cfdd29e61b903ef907ee391f3a11bed09d76db4728bbef0c"
TOLERANCE = 1e-12


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def record(path):
    path = Path(path).resolve(strict=True)
    return {"path": str(path), "sha256": sha(path), "bytes": path.stat().st_size}


def member(root, name):
    value = PurePosixPath(name)
    require(bool(name) and not value.is_absolute() and ".." not in value.parts and "\\" not in name and ":" not in name,
            "Unsafe relative artifact name: " + name)
    path = (root / name).resolve(strict=True)
    require(path.is_relative_to(root.resolve()) and path.is_file(), "Artifact escapes its directory")
    return path


def equivalent(expected, actual, path="scientific", differences=None):
    """Strict structures and labels; finite numeric values allow only 1e-12 absolute error."""
    if differences is None:
        differences = {"numeric_values": 0, "maximum_absolute_difference": 0.0}
    if isinstance(expected, dict):
        require(isinstance(actual, dict) and set(expected) == set(actual), path + ": dictionary keys differ")
        for key in expected:
            equivalent(expected[key], actual[key], path + "/" + str(key), differences)
    elif isinstance(expected, list):
        require(isinstance(actual, list) and len(expected) == len(actual), path + ": list length differs")
        for i, (left, right) in enumerate(zip(expected, actual)):
            equivalent(left, right, path + "/" + str(i), differences)
    elif isinstance(expected, bool) or expected is None or isinstance(expected, str):
        require(type(expected) is type(actual) and expected == actual, path + ": label/null/boolean differs")
    elif isinstance(expected, (int, float)):
        require(isinstance(actual, (int, float)) and not isinstance(actual, bool), path + ": number type differs")
        require(math.isfinite(expected) and math.isfinite(actual), path + ": nonfinite number")
        delta = abs(expected - actual)
        require(delta <= TOLERANCE, path + ": numerical value differs")
        differences["numeric_values"] += 1
        differences["maximum_absolute_difference"] = max(differences["maximum_absolute_difference"], delta)
    else:
        require(expected == actual, path + ": value differs")
    return differences


def normalize_settings(settings):
    settings = deepcopy(settings)
    for name in ("run_id", "output_dir", "preview_fingerprint"):
        settings.pop(name, None)
    if "samples" in settings:
        settings["samples"].pop("path", None)
    for covariate in settings.get("covariates", []):
        covariate.pop("path", None)
    return settings


def model_record(value):
    value = deepcopy(value)
    if "settings" in value:
        value["settings"] = normalize_settings(value["settings"])
    return value


def scientific_snapshot(evaluation):
    fields = ("oof", "independent_predictions", "metrics", "learning_audit", "folds",
              "coverage_diagnostics", "plain_model_comparison", "subgroup_diagnostics",
              "mapping_diagnostics", "permutation_importance")
    result = {key: deepcopy(evaluation[key]) for key in fields}
    result["final_record"] = model_record(evaluation["final_record"])
    result["fold_metrics"] = [
        {**deepcopy(row), "model_record": model_record(row["model_record"])}
        for row in evaluation["fold_metrics"]]
    result["trials"] = deepcopy(evaluation["trials"])
    for trial in result["trials"]:
        for field in ("elapsed_seconds", "search_elapsed_seconds"):
            trial.pop(field, None)
        trial.get("search_summary", {}).pop("elapsed_seconds", None)
    return result


def verify_outputs(output):
    result = read(output / "result.json")
    require(result["status"] == "completed" and result["stage"] in ("B", "C", "D"), "The supplied run is not a completed modeling result")
    require(not (output / "INCOMPLETE.json").exists(), "Incomplete marker remains")
    require(len(result["outputs"]) == 140, "Expected 140 published artifacts")
    entries = {}
    for item in result["outputs"]:
        require(item["path"].casefold() not in entries, "Duplicate artifact")
        path = member(output, item["path"])
        require(sha(path) == item["sha256"], "Published artifact SHA differs: " + item["path"])
        entries[item["path"].casefold()] = item
    return result


def verify_archive(output, result):
    item = next(x for x in result["outputs"] if x["kind"] == "study_package")
    path = member(output, item["path"])
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len({n.casefold() for n in names}), "Duplicate archive member")
        for name in names:
            p = PurePosixPath(name)
            require(not p.is_absolute() and ".." not in p.parts and "\\" not in name and ":" not in name,
                    "Unsafe ZIP member")
        manifest = json.loads(archive.read("study_manifest.json"))
        require(manifest["schema_version"] == 1 and manifest["run_id"] == result["run_id"], "Study manifest provenance differs")
        require(set(names) == set(manifest["members"]) | {"study_manifest.json"}, "Study ZIP member set differs")
        for name, expected in manifest["members"].items():
            require(hashlib.sha256(archive.read(name)).hexdigest() == expected, "Study ZIP member SHA differs: " + name)
        expected = [x for x in result["outputs"] if x["kind"] != "study_package"]
        snapshot = json.loads(archive.read("result.snapshot.json"))
        require(snapshot["status"] == "completed" and snapshot["run_id"] == result["run_id"], "Result snapshot provenance differs")
        require({k: v for k, v in snapshot.items() if k not in ("outputs", "archive_snapshot_scope")} ==
                {k: v for k, v in result.items() if k != "outputs"},
                "Archived result metadata differs from the final result")
        require(snapshot["outputs"] == expected, "Result snapshot output list differs")
        require(set(names) == {x["path"] for x in expected} | {"study_manifest.json", "request.snapshot.json", "result.snapshot.json"},
                "Study ZIP omits or adds an output")
        require(all(manifest["members"][x["path"]] == x["sha256"] for x in expected), "Study and result manifest hashes differ")
        require(archive.read("request.snapshot.json") == (output / "request.snapshot.json").read_bytes(), "Request snapshot differs")
        require(path.name not in names and "result.json" not in names, "Study ZIP hashes itself")
        return {"file": record(path), "verified_members": len(names), "manifest": manifest,
                "result_snapshot_stage": snapshot.get("stage"),
                "result_snapshot_sha256": manifest["members"]["result.snapshot.json"],
                "final_result_metadata_matches_snapshot": True}


def rows(path, exclude=()):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return [{key: value for key, value in row.items() if key not in exclude} for row in csv.DictReader(stream)]


def verify(actual_run, install_root, reference=REFERENCE):
    actual_run = Path(actual_run).resolve(strict=True)
    install_root = Path(install_root).resolve(strict=True)
    actual = read(actual_run)
    require(actual["status"] == "passed" and actual["analysis_job"]["action"] == "run", "Actual MCP run evidence is not passed")
    require(any(c["tool"] == "run_analysis" and not c["is_error"] for c in actual["calls"]), "No real run_analysis call")
    output = Path(actual["analysis_job"]["directory"]).resolve(strict=True) / "output"
    require(output.resolve() != reference.resolve(), "The new installed run cannot be the old reference")
    result = verify_outputs(output)
    reference_result = verify_outputs(reference)
    require(result["run_id"] == actual["analysis_job"]["id"] and result["run_id"] != reference_result["run_id"],
            "New run identity differs or repeats reference")
    require({(x["path"], x["kind"]) for x in result["outputs"]} ==
            {(x["path"], x["kind"]) for x in reference_result["outputs"]}, "Published product types/names differ")
    study_archive = verify_archive(output, result)
    reference_archive = verify_archive(reference, reference_result)
    historical_evidence_path = ROOT / "build/mcp-scientific-readback.json"
    historical = read(historical_evidence_path)
    require(historical["status"] == "passed_file_readback" and historical["run_id"] == reference_result["run_id"],
            "Historical acceptance does not identify the reference run")
    require(reference_archive["file"]["sha256"] == historical["study_archive"]["file"]["sha256"],
            "Reference study archive differs from its historical accepted checksum")
    with zipfile.ZipFile(output / "model_package.spsmodel.zip") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        require(manifest["run_id"] == result["run_id"] and manifest["model_id"] == "ridge", "Model package provenance differs")
        require(set(archive.namelist()) == set(manifest["members"]) | {"manifest.json"}, "Model package members differ")
        for name, expected in manifest["members"].items():
            require(hashlib.sha256(archive.read(name)).hexdigest() == expected, "Model package SHA differs")
    deployment = read(install_root / "deployment-record.json")
    require(deployment["status"] == "installed" and deployment["staging"] is False, "Not a real activated installation")
    require(Path(deployment["installation_root"]).resolve() == install_root, "Installed root differs")
    require(deployment["source_addin_sha256"] == CANDIDATE_SHA, "Unexpected source add-in candidate")
    require(sha(deployment["addin_path"]) == deployment["addin_sha256"], "Registered add-in SHA differs")
    audit = read(output / "audit.json")
    expected_python = install_root / "runtime-d/Scripts/python.exe"
    require(Path(audit["executable"]).resolve() == expected_python.resolve(), "Run did not use installed private Python")
    code_records = []
    for item in audit["source_code"]:
        path = Path(item["path"]).resolve(strict=True)
        require(path.is_relative_to(install_root / "backend"), "Audited backend is outside installed environment: " + str(path))
        require(sha(path) == item["sha256"], "Installed backend differs from audit")
        code_records.append(record(path))
    require(len(code_records) == 28, "Expected all 28 audited backend files")
    baseline_audit = read(reference / "audit.json")
    require(Counter(x["sha256"] for x in audit["sources"]) == Counter(x["sha256"] for x in baseline_audit["sources"]),
            "Raw source contents differ")
    for item in audit["sources"]:
        require(sha(item["path"]) == item["sha256"], "Source changed after the new run")
    evaluation = read(output / "evaluation.json")["evaluation"]
    baseline = read(reference / "evaluation.json")["evaluation"]
    final = evaluation["final_record"]
    request = read(output / "request.snapshot.json")
    require(request["validation"]["method"] == "group" and request["uncertainty"]["method"] == "split_conformal"
            and request["diagnostics"]["subgroup_fields"] == ["landuse"] and result["stage"] == "C",
            "Current stage classification does not match the requested Ridge subgroup workflow")
    require(final["model"] == {"id": "ridge", "parameters": {"alpha": .1}}, "Unexpected final model")
    require(len(evaluation["oof"]) == 45 and len(evaluation["independent_predictions"]) == 2, "Expected 45 OOF and two independent rows")
    require(len(read(output / "sampled_data.json")["rows"]) == 47, "Expected 47 retained samples")
    require({r["sample_id"] for r in evaluation["independent_predictions"]} == {"D046", "D047"}, "Independent identities differ")
    require(len(evaluation["trials"]) == 9 and sum(len(t["inner_fold_metrics"]) for t in evaluation["trials"]) == 18,
            "Expected nine actual trials and 18 inner folds")
    comparison = equivalent(scientific_snapshot(baseline), scientific_snapshot(evaluation))
    equivalent(read(reference / "sampled_data.json"), read(output / "sampled_data.json"), "sampled_data")
    a, b = read(reference / "excluded_samples.json"), read(output / "excluded_samples.json")
    for data in (a, b):
        for row in data["records"]:
            row.pop("source_path", None)
    equivalent(a, b, "excluded_samples")
    csv_counts = {}
    for name in ("sampled_data.csv", "data_roles.csv", "role_data.csv", "learning_membership.csv",
                 "publication_metrics.csv", "hyperparameters.csv", "training_predictions.csv",
                 "calibration_predictions.csv", "inner_validation_predictions.csv",
                 "oof_predictions.csv", "independent_predictions.csv", "trial_results.csv", "inner_fold_results.csv"):
        excluded = ("elapsed_seconds",) if name in ("trial_results.csv", "inner_fold_results.csv") else ()
        old, new = rows(reference / name, excluded), rows(output / name, excluded)
        require(old == new, "Scientific CSV differs: " + name)
        csv_counts[name] = len(new)
    pagination = verify_pagination(output, result, read(output / "study_summary.json"))
    require(evaluation["study_context"]["model_artifact"]["roundtrip_verified"] is True, "Model roundtrip was not recorded")
    return {
        "schema_version": 1, "status": "passed", "created_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Independent read-only comparison of an actual installed MCP Ridge run with the historical accepted reference. No fitting or serialized model loading.",
        "actual_run_evidence": record(actual_run), "run_id": result["run_id"], "output_directory": str(output),
        "stage_comparison": {"current": result["stage"], "reference": reference_result["stage"],
            "current_stage_matches_requested_workflow": True,
            "scope": "Current Ridge/group/split-conformal workflow includes the C subgroup diagnostic. Historical reference label is preserved; numerical method equality is checked separately."},
        "reference_result": record(reference / "result.json"), "reference_run_id": reference_result["run_id"],
        "reference_study_archive": reference_archive, "historical_acceptance": record(historical_evidence_path),
        "installation_root": str(install_root), "deployment_record": record(install_root / "deployment-record.json"),
        "source_addin_sha256": deployment["source_addin_sha256"], "registered_addin": record(deployment["addin_path"]),
        "python_executable": str(expected_python), "installed_backend_files_verified": code_records,
        "sources": audit["sources"], "artifact_count": len(result["outputs"]), "artifacts": result["outputs"],
        "study_archive": study_archive, "science_comparison": comparison, "csv_counts": csv_counts,
        "counts": {"retained": 47, "oof": 45, "independent": 2, "final_training": len(final["development_ids"]),
                   "final_calibration": len(final["calibration_ids"]), "actual_trials": 9, "inner_folds": 18},
        "final_model": final["model"], "metrics": evaluation["metrics"], "final_training_metrics": final["train_metrics"],
        "final_calibration_metrics": final["calibration_metrics"], "pagination": pagination,
        "independent_test_history": evaluation["independent_test_history"],
        "recorded_model_roundtrip": evaluation["study_context"]["model_artifact"],
        "explicitly_excluded_from_equality": ["run_id", "output_dir", "preview_fingerprint", "input paths (contents verified separately)",
            "trial elapsed_seconds", "trial search_elapsed_seconds", "search_summary.elapsed_seconds",
            "study_context runtime/path/time metadata", "independent_test_history (preserved, not an assertion of new independent data)",
            "artifact bytes containing run identity, paths, timing or serialized model metadata"],
        "reference_preserved": True,
        "reference_preserved_scope": "Read-only verifier. Reference archive checksum matches the historical accepted checksum; final result metadata matches its immutable archived snapshot.",
        "new_fits_started": 0, "failed_checks": [],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actual-run", type=Path, required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    target = args.output.resolve()
    require(target.is_relative_to(ROOT / "build"), "Evidence must stay under build")
    require(not target.exists(), "Refusing to replace previous evidence")
    result = verify(args.actual_run, args.install_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".partial")
    try:
        temporary.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
        require(read(temporary)["status"] == "passed", "Evidence readback failed")
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()
    print(json.dumps({"status": result["status"], "output": str(target), "counts": result["counts"],
                      "comparison": result["science_comparison"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
