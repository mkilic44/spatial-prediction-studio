"""Read-only C cancellation and project round-trip acceptance evidence.

Only the requested evidence JSON is written. This does not start ArcMap or a job.
The successful result manifest is the baseline for its 54 declared output hashes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "build" / "arcmap-evidence"
TRANSIENT_FIELDS = {
    "preview_fingerprint", "preview_manifest", "preview_signature", "preview_sources"
}


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def snapshot(path: Path) -> dict:
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": digest(path),
        "modified_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
    }


def differences(a, b, path="") -> list[dict]:
    if isinstance(a, dict) and isinstance(b, dict):
        output = []
        for key in sorted(a.keys() | b.keys()):
            location = f"{path}.{key}" if path else key
            if key not in a or key not in b:
                output.append({"field": location, "original_present": key in a,
                               "roundtrip_present": key in b,
                               "original": a.get(key), "roundtrip": b.get(key)})
            else:
                output.extend(differences(a[key], b[key], location))
        return output
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [{"field": path, "original": a, "roundtrip": b}]
        return [item for i, (left, right) in enumerate(zip(a, b))
                for item in differences(left, right, f"{path}[{i}]")]
    if a != b:
        return [{"field": path, "original": a, "roundtrip": b}]
    return []


def verify(args) -> dict:
    checks = {}
    failure = []

    def check(name, condition):
        checks[name] = bool(condition)
        if not condition:
            failure.append(name)

    cancelled = args.cancel_run.resolve()
    cancel_result_path = cancelled / "output" / "result.json"
    cancelled_result = read(cancel_result_path)
    files = sorted(p for p in cancelled.rglob("*") if p.is_file())
    relative_files = [p.relative_to(cancelled).as_posix() for p in files]
    allowed_files = {
        "request.json", "output/request.snapshot.json", "output/result.json",
        "output/preflight.json", "output/progress.jsonl", "output/cancel.request",
    }
    unexpected = sorted(set(relative_files) - allowed_files)
    forbidden = [p for p in relative_files if any(token in p.lower() for token in
                 (".tif", ".joblib", ".pickle", ".pkl", ".onnx", ".ckpt", ".pth",
                  ".partial", ".part", ".tmp"))]
    progress_path = cancelled / "output" / "progress.jsonl"
    progress = [json.loads(line) for line in progress_path.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    check("cancelled_manifest_status", cancelled_result.get("status") == "cancelled")
    check("cancelled_error_code", cancelled_result.get("error", {}).get("code") == "CANCELLED")
    check("cancelled_outputs_empty", cancelled_result.get("outputs") == [])
    check("cancellation_request_present", (cancelled / "output" / "cancel.request").is_file())
    check("progress_ends_cancelled", bool(progress) and progress[-1].get("stage") == "cancelled")
    check("no_unexpected_cancel_run_files", not unexpected)
    check("no_partial_raster_or_model_artifacts", not forbidden)

    successful = args.success_run.resolve()
    success_result_path = successful / "output" / "result.json"
    success_result = read(success_result_path)
    outputs = success_result.get("outputs", [])
    output_root = successful / "output"
    hash_rows = []
    for item in outputs:
        path = (output_root / item["path"]).resolve()
        safe = path.is_relative_to(output_root)
        exists = safe and path.is_file()
        actual = digest(path) if exists else None
        hash_rows.append({"path": item["path"], "expected_sha256": item["sha256"],
                          "actual_sha256": actual, "matches": actual == item["sha256"],
                          "exists": exists, "within_output_directory": safe})
    check("previous_run_completed", success_result.get("status") == "completed")
    check("previous_manifest_has_54_outputs", len(outputs) == 54)
    check("previous_output_paths_unique", len({item["path"] for item in outputs}) == len(outputs))
    check("all_previous_output_hashes_unchanged", bool(hash_rows) and all(row["matches"] for row in hash_rows))

    deadline = time.monotonic() + max(0, min(args.wait_seconds, 60))
    while not args.roundtrip_project.is_file() and time.monotonic() < deadline:
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    check("roundtrip_project_exists", args.roundtrip_project.is_file())
    project = {"original": snapshot(args.saved_project)}
    if args.roundtrip_project.is_file():
        original = read(args.saved_project)
        roundtrip = read(args.roundtrip_project)
        project["roundtrip"] = snapshot(args.roundtrip_project)
        original_stable = {k: v for k, v in original.items() if k not in TRANSIENT_FIELDS}
        roundtrip_stable = {k: v for k, v in roundtrip.items() if k not in TRANSIENT_FIELDS}
        stable_changes = differences(original_stable, roundtrip_stable)
        fields = ("science", "samples", "covariates", "output_root", "window_size", "last_manifest",
                  "input_type", "csv_path", "point_source", "point_name", "point_scope", "selected_only")
        for field in fields:
            check(f"project_{field}_matches", field in original and field in roundtrip
                  and original[field] == roundtrip[field])
        check("project_unit_matches", original.get("samples", {}).get("unit")
              == roundtrip.get("samples", {}).get("unit")
              and bool(original.get("samples", {}).get("unit")))
        check("all_persistent_project_fields_match", not stable_changes)
        check("last_manifest_points_to_previous_success", Path(roundtrip.get("last_manifest", "")).resolve()
              == success_result_path.resolve())
        preview_source_records = []
        for label, saved in (("original", original), ("roundtrip", roundtrip)):
            preview_directory = Path(saved["preview_manifest"]).parent
            source_audit = read(preview_directory / "audit.json")
            entries = []
            for source in source_audit["sources"]:
                source_path = Path(source["path"])
                actual = digest(source_path) if source_path.is_file() else None
                entries.append({"path": str(source_path), "expected_sha256": source["sha256"],
                                "actual_sha256": actual, "matches": actual == source["sha256"]})
            preview_source_records.append({"project": label, "audit": snapshot(preview_directory / "audit.json"),
                                           "sources": entries})
        source_hashes = [{row["path"].casefold(): row["expected_sha256"] for row in item["sources"]}
                         for item in preview_source_records]
        check("preview_input_content_hashes_unchanged", source_hashes[0] == source_hashes[1]
              and all(row["matches"] for item in preview_source_records for row in item["sources"]))
        project.update({
            "persistent_differences": stable_changes,
            "excluded_transient_fields": sorted(TRANSIENT_FIELDS),
            "transient_values": {field: {"original": original.get(field), "roundtrip": roundtrip.get(field),
                                         "equal": original.get(field) == roundtrip.get(field)}
                                 for field in sorted(TRANSIENT_FIELDS)},
            "transient_differences": differences(
                {k: original[k] for k in TRANSIENT_FIELDS if k in original},
                {k: roundtrip[k] for k in TRANSIENT_FIELDS if k in roundtrip}),
            "exclusion_reason": "Reopening the project and running a new preview can renew session-bound preview records. "
                                "These four fields are recorded separately from the equality of scientific settings. "
                                "last_manifest and all persistent settings are included in the comparison.",
            "source_stamp_definition": "preview_sources tracks the file path, size, last write time and existing sidecar files. "
                                       "It is therefore not only a content digest. Earlier timestamps are not stored, so "
                                       "this tool does not determine which single file caused the change. "
                                       "Observation and raster contents are compared separately using the audit.json records of the two previews.",
            "source_stamp_implementation": {"path": str(ROOT / "native/SpatialPredictionStudio/MethodRegistry.cs"),
                                            "method": "ConfigurationIdentity.SourceStamp", "line": 76},
            "preview_source_audits": preview_source_records,
        })

    images = [snapshot(EVIDENCE / name) for name in ("c-cancelled.png", "c-reopened-result.png")
              if (EVIDENCE / name).is_file()]
    return {
        "schema_version": 1,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "passed" if not failure else "failed",
        "checks": checks,
        "failed_checks": failure,
        "cancelled_run": {"run_id": cancelled_result.get("run_id"),
                          "manifest": snapshot(cancel_result_path), "outputs": cancelled_result.get("outputs"),
                          "files": [snapshot(p) | {"relative_path": p.relative_to(cancelled).as_posix()} for p in files],
                          "unexpected_files": unexpected, "forbidden_artifacts": forbidden,
                          "progress_stages": [r.get("stage") for r in progress]},
        "previous_success": {"run_id": success_result.get("run_id"),
                             "baseline": "File digests in the result.json written when the successful run completed.",
                             "manifest": snapshot(success_result_path), "output_count": len(outputs),
                             "unchanged_count": sum(r["matches"] for r in hash_rows), "output_hashes": hash_rows},
        "project_roundtrip": project,
        "screenshots_present": images,
        "scope": "File and content check. This tool does not verify the interface screenshot, the process exit code or leftover R processes.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cancel-run", type=Path, default=EVIDENCE / "c-runs" / "20261017_201902_394ca067")
    parser.add_argument("--success-run", type=Path, default=EVIDENCE / "c-runs" / "20261017_200706_14264ce3")
    parser.add_argument("--saved-project", type=Path, default=EVIDENCE / "c-knndm-panel-saved.sps.json")
    parser.add_argument("--roundtrip-project", type=Path, default=EVIDENCE / "c-roundtrip.sps.json")
    parser.add_argument("--destination", type=Path, default=EVIDENCE / "cancel-roundtrip-readback-C.json")
    parser.add_argument("--wait-seconds", type=float, default=60)
    args = parser.parse_args()
    report = verify(args)
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    args.destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "failed_checks": report["failed_checks"],
                      "previous_hashes_unchanged": report["previous_success"]["unchanged_count"],
                      "destination": str(args.destination.resolve())}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
