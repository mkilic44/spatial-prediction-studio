"""Real CAST readback with relocated Unicode R and current production adapter."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
# Relocated Unicode installation recorded for the 0.5.0 acceptance; keep the path as recorded.
INSTALLED = ROOT.parent / "sps-ç/app/versions/0.5.0-6d5fb15e486c"
FIX = ROOT / "tests/fixtures/cast-1.0.4"
OUT = ROOT / "build/relocated-unicode-knndm"


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    assert sys.dont_write_bytecode
    assert Path(sys.executable).resolve() == (INSTALLED / "runtime-d/Scripts/python.exe").resolve()
    OUT.mkdir(exist_ok=True)
    work = OUT / "çalışma_ölçüm"  # Non-ASCII work directory on purpose: this check exercises Unicode paths.
    work.mkdir(exist_ok=True)
    tempfile.tempdir = str(work)
    os.environ["TEMP"] = str(work)
    os.environ["TMP"] = str(work)
    os.environ["LC_ALL"] = "C"
    os.environ["LANG"] = "C"
    sys.path.insert(0, str(ROOT / "backend"))
    import numpy as np
    from sps import knndm
    assert Path(np.__file__).resolve().is_relative_to(INSTALLED)
    assert Path(knndm.__file__).resolve() == (ROOT / "backend/sps/knndm.py").resolve()
    assert not knndm._CACHE
    candidate = ROOT / "build/stage-050-utf8-release/SpatialPredictionStudio.esriAddIn"
    candidate_sha = sha(candidate)
    assert candidate_sha == "b8831e222582b334cfdd29e61b903ef907ee391f3a11bed09d76db4728bbef0c"
    with zipfile.ZipFile(candidate) as archive:
        for name in ("knndm.py", "knndm_bridge.R"):
            assert hashlib.sha256(archive.read("Install/backend/sps/" + name)).hexdigest() == sha(ROOT / "backend/sps" / name)

    source_paths = [
        ROOT / "backend/sps/knndm.py", ROOT / "backend/sps/knndm_bridge.R",
        INSTALLED / "payload-manifest.json",
        INSTALLED / "reference-r/lib/R/bin/Rscript.exe",
        INSTALLED / "reference-r/Scripts/conda-unpack-script.py",
        *FIX.glob("knndm_*.csv"),
    ]
    for pkg in ("methods", "datasets", "utils", "stats", "compiler"):
        source_paths.extend(p for p in (INSTALLED / "reference-r/lib/R/library" / pkg).rglob("*") if p.is_file())
    for path in (ROOT / "installer/payload-050/payload-manifest.json", ROOT / "installer/dist/installer-build-record.json"):
        if path.is_file():
            source_paths.append(path)
    before = {str(p): sha(p) for p in source_paths}

    def matrix(name):
        a = np.genfromtxt(FIX / name, delimiter=",", names=True)
        return np.column_stack([a[n] for n in a.dtype.names])

    training = matrix("knndm_train.csv")
    prediction = matrix("knndm_prediction.csv")
    assert training.shape == (30, 2) and prediction.shape == (225, 2)
    observed_processes = []
    processes = []
    original_popen = knndm.subprocess.Popen

    def tracked_popen(args, **kwargs):
        env = kwargs["env"]
        assert Path(args[0]).resolve() == (INSTALLED / "reference-r/lib/R/bin/Rscript.exe").resolve()
        assert Path(env["R_HOME"]).resolve() == (INSTALLED / "reference-r/lib/R").resolve()
        assert env["LC_ALL"] == env["LANG"] == "en_US.UTF-8"
        process = original_popen(args, **kwargs)
        processes.append(process)
        observed_processes.append({
            "pid": process.pid, "executable": args[0], "R_HOME": env["R_HOME"],
            "LC_ALL": env["LC_ALL"], "LANG": env["LANG"],
            "bridge": args[2], "work_directory": args[5],
        })
        return process

    started = time.monotonic()
    knndm.subprocess.Popen = tracked_popen
    try:
        actual = knndm.cast_knndm(training, prediction, 5, .5, 42)
    finally:
        knndm.subprocess.Popen = original_popen
    assert len(processes) == 1 and processes[0].returncode == 0
    assert not any(work.iterdir()), "Temporary adapter files were not cleaned"

    tr = np.genfromtxt(FIX / "knndm_train_result.csv", delimiter=",", names=True)
    pr = np.genfromtxt(FIX / "knndm_prediction_result.csv", delimiter=",", names=True)
    with (FIX / "knndm_summary.csv").open(encoding="utf-8", newline="") as stream:
        summary = next(csv.DictReader(stream))
    np.testing.assert_array_equal(actual["fold"], tr["fold"].astype(int))
    maximum_errors = {}
    for key, expected in (("Gj", tr["Gj"]), ("Gjstar", tr["Gjstar"]), ("Gij", pr["Gij"])):
        maximum_errors[key] = float(np.max(np.abs(np.asarray(actual[key]) - expected)))
        np.testing.assert_allclose(actual[key], expected, rtol=0, atol=1e-8)
    maximum_errors["W"] = abs(actual["W"] - float(summary["W"]))
    assert maximum_errors["W"] <= 1e-8
    assert actual["q"] == summary["q"] and actual["reference"] == "CAST 1.0.4"
    after = {str(p): sha(p) for p in source_paths}
    assert before == after, "Source or installed reference file changed"
    evidence = {
        "schema_version": 1, "status": "passed", "created_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Real CAST process with relocated Unicode R_HOME and current backend; not installer or real ArcMap UI acceptance.",
        "python_executable": sys.executable, "numpy_module": np.__file__,
        "adapter_path": knndm.__file__, "installation_root": str(INSTALLED),
        "addin_candidate_sha256": candidate_sha, "addin_candidate_path": str(candidate),
        "candidate_adapter_and_bridge_hashes_matched": True,
        "parent_locale": {"LC_ALL": "C", "LANG": "C"},
        "processes": [dict(p, exit_code=proc.returncode) for p, proc in zip(observed_processes, processes)],
        "training_locations": len(training), "prediction_locations": len(prediction),
        "k": 5, "maxp": .5, "seed": 42, "elapsed_seconds": time.monotonic() - started,
        "fold_reference_exact": True, "fold_counts": dict(Counter(actual["fold"])),
        "reference_q": summary["q"], "reference_W": float(summary["W"]),
        "maximum_absolute_errors": maximum_errors, "distance_absolute_tolerance": 1e-8,
        "actual": actual, "unchanged_files": before, "unchanged_file_count": len(before),
        "installed_or_production_files_written": False, "temporary_work_cleaned": True,
    }
    path = OUT / "readback.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "passed"
    print(json.dumps({"status": evidence["status"], "evidence": str(path), "fold_counts": evidence["fold_counts"],
        "W": actual["W"], "q": actual["q"], "maximum_absolute_errors": maximum_errors,
        "unchanged_files": len(before), "elapsed_seconds": evidence["elapsed_seconds"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
