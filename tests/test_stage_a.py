"""Stage A contract tests, using real files and independently checked predictions."""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin, xy
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def engine():
    assert (ROOT / "backend" / "sps" / "engine.py").is_file(), "Stage A engine has not been implemented"
    return importlib.import_module("sps.engine")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def case(tmp_path):
    root = tmp_path / "Türkçe ölçüm"
    root.mkdir()
    raster = root / "çevresel.tif"
    data = np.stack([np.arange(20).reshape(4, 5), np.arange(20, 40).reshape(4, 5)]).astype("float32")
    data[:, 0, 0] = -9999
    data[1, 1, 2] = -9999
    transform = from_origin(500000, 4200000, 10, 10)
    with rasterio.open(raster, "w", driver="GTiff", width=5, height=4, count=2,
                       dtype="float32", crs="EPSG:32637", transform=transform, nodata=-9999) as dst:
        dst.write(data)
        dst.set_band_description(1, "first")
        dst.set_band_description(2, "second")
    features = []
    for i, (r, c, value) in enumerate([(0, 1, 4), (1, 1, 8), (2, 1, 12), (3, 1, 16)]):
        x, y = xy(transform, r, c)
        features.append({"type": "Feature", "properties": {"sample_id": str(i), "soil_value": value},
                         "geometry": {"type": "Point", "coordinates": [x, y]}})
    samples = root / "örnekler.geojson"
    samples.write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    request = {"schema_version": 1, "run_id": "test-run", "task": "regression",
               "samples": {"path": str(samples), "id_field": "sample_id", "target_field": "soil_value",
                           "unit": "g/kg", "crs": "EPSG:32637"},
               "covariates": [{"id": "first", "name": "Birinci", "path": str(raster), "band": 1,
                               "unit": "m", "kind": "continuous"},
                              {"id": "second", "name": "İkinci", "path": str(raster), "band": 2,
                               "unit": "m", "kind": "continuous"}],
               "model": {"id": "mean", "parameters": {}}, "window_size": 2,
               "output_dir": str(root / "output")}
    return request, data, features


def read_result(request):
    return json.loads((Path(request["output_dir"]) / "result.json").read_text(encoding="utf-8"))


def test_mean_real_raster_masks_metadata_and_sources(case):
    """Catches source writes, dropped masks, wrong mean, and premature success."""
    req, data, _ = case
    original = {p: sha(p) for p in {req["samples"]["path"], req["covariates"][0]["path"]}}
    events = []
    result = engine().run_job(req, events.append)
    assert result["status"] == "completed", result
    assert result == read_result(req)
    assert result["validation"]["status"] == "not_performed_stage_a"
    prediction = Path(req["output_dir"]) / next(p["path"] for p in result["outputs"] if p["kind"] == "prediction")
    with rasterio.open(prediction) as src:
        values = src.read(1, masked=True)
        assert np.array_equal(np.ma.getmaskarray(values), np.any(data == -9999, axis=0))
        np.testing.assert_allclose(values.compressed(), 10.0)
        assert src.tags()["target_unit"] == "g/kg"
        assert src.tags()["run_id"] == "test-run"
        assert src.units == ("g/kg",)
        assert src.descriptions == ("soil_value",)
        assert src.crs.to_epsg() == 32637
    assert all(sha(p) == digest for p, digest in original.items())
    assert events and all(e["event"] == "progress" and e["run_id"] == req["run_id"] for e in events)
    assert all(0 <= e["completed"] <= e["total"] for e in events)
    assert not list(Path(req["output_dir"]).glob("*.partial*"))
    assert all((Path(req["output_dir"]) / p["path"]).is_file() for p in result["outputs"])


def test_fixed_ridge_save_reload_and_window_independence(case):
    """Catches fit-per-window, wrong band order, or model persistence drift."""
    req, data, _ = case
    req["model"] = {"id": "ridge", "parameters": {"alpha": 2.0}}
    # Four samples are [1,21],[6,26],[11,31],[16,36], targets 4,8,12,16.
    train = np.array([[1, 21], [6, 26], [11, 31], [16, 36]], dtype=float)
    target = np.array([4, 8, 12, 16], dtype=float)
    centered = train - train.mean(axis=0)
    coef = np.linalg.solve(centered.T @ centered + 2 * np.eye(2), centered.T @ (target - 10))
    expected = (np.moveaxis(data, 0, -1) - train.mean(axis=0)) @ coef + 10
    results = []
    for size in (1, 7, 256):
        job = copy.deepcopy(req)
        job["output_dir"] = str(Path(req["output_dir"]).with_name("run-" + str(size)))
        job["window_size"] = size
        result = engine().run_job(job, lambda _: None)
        assert result["status"] == "completed", result
        with rasterio.open(Path(job["output_dir"]) / "prediction.tif") as src:
            values = src.read(1, masked=True)
            np.testing.assert_allclose(values.compressed(), expected[~values.mask], rtol=1e-6, atol=1e-5)
            results.append(values)
        restored = engine().load_model(Path(job["output_dir"]) / "model.json", expected_run_id="test-run")
        np.testing.assert_allclose(restored.predict(train), (train - train.mean(axis=0)) @ coef + 10)
    assert all(np.array_equal(results[0].mask, a.mask) for a in results)
    assert all(np.array_equal(results[0].data, a.data, equal_nan=True) for a in results)


@pytest.mark.parametrize("mutation,code", [
    (lambda r: r.update(task="classification"), "UNSUPPORTED_TASK"),
    (lambda r: r.update(schema_version=2), "SCHEMA_VERSION"),
    (lambda r: r["samples"].update(unit=" "), "MISSING_UNIT"),
    (lambda r: r["covariates"][0].update(band=3), "BAND_RANGE"),
    (lambda r: r["covariates"][0].update(kind="categorical"), "UNSUPPORTED_COVARIATE"),
    (lambda r: r["covariates"][1].update(id="first"), "DUPLICATE_COVARIATE"),
    (lambda r: r["model"].update(id="random_forest"), "UNSUPPORTED_MODEL"),
    (lambda r: r.update(window_size=0), "WINDOW_SIZE"),
    (lambda r: r["model"].update(parameters={"unrecognized": 1}), "MODEL_PARAMETER"),
])
def test_contract_rejects_unsupported_inputs_without_prediction(case, mutation, code):
    req, _, _ = case
    mutation(req)
    result = engine().run_job(req, lambda _: None)
    assert result["status"] == "failed", result
    assert result["error"]["code"] == code
    assert not (Path(req["output_dir"]) / "prediction.tif").exists()


@pytest.mark.parametrize("mutation,code", [
    (lambda f: f[1]["properties"].update(sample_id="0"), "DUPLICATE_ID"),
    (lambda f: f[1]["properties"].update(soil_value=None), "MISSING_TARGET"),
    (lambda f: f[1]["geometry"].update(coordinates=[0, 0]), "SAMPLE_OUTSIDE"),
    (lambda f: f[1]["geometry"].update(coordinates=f[0]["geometry"]["coordinates"]), "DUPLICATE_COORDINATE"),
    (lambda f: f[1]["geometry"].update(coordinates=[500005, 4199995]), "SAMPLE_NODATA"),
])
def test_sample_failures_are_not_silently_dropped(case, mutation, code):
    req, _, features = case
    mutation(features)
    Path(req["samples"]["path"]).write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    result = engine().run_job(req, lambda _: None)
    assert result["status"] == "failed", result
    assert result["error"]["code"] == code
    assert read_result(req)["status"] == "failed"


def test_shifted_grid_is_rejected(case):
    req, data, _ = case
    other = Path(req["samples"]["path"]).with_name("shifted.tif")
    with rasterio.open(req["covariates"][0]["path"]) as src:
        profile = src.profile.copy()
    profile.update(transform=from_origin(500001, 4200000, 10, 10))
    with rasterio.open(other, "w", **profile) as dst:
        dst.write(data)
    req["covariates"][1]["path"] = str(other)
    result = engine().run_job(req, lambda _: None)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "GRID_MISMATCH"


def test_csv_samples_and_explicit_crs(case):
    req, _, features = case
    csv = Path(req["samples"]["path"]).with_suffix(".csv")
    rows = ["sample_id,soil_value,x,y"] + [f"{f['properties']['sample_id']},{f['properties']['soil_value']},{f['geometry']['coordinates'][0]},{f['geometry']['coordinates'][1]}" for f in features]
    csv.write_text("\n".join(rows), encoding="utf-8-sig")
    req["samples"].update(path=str(csv), x_field="x", y_field="y")
    assert engine().run_job(req, lambda _: None)["status"] == "completed"


def test_cancellation_removes_incomplete_raster(case):
    req, _, _ = case
    def cancel(event):
        if event["stage"] == "mapping" and event["completed"] == 1:
            (Path(req["output_dir"]) / "cancel.request").touch()
    result = engine().run_job(req, cancel)
    assert result["status"] == "cancelled", result
    assert read_result(req)["status"] == "cancelled"
    assert not (Path(req["output_dir"]) / "prediction.tif").exists()
    assert not list(Path(req["output_dir"]).glob("*.partial*"))
    assert result["outputs"] == []


def test_existing_output_cannot_be_overwritten(case):
    req, _, _ = case
    output = Path(req["output_dir"])
    output.mkdir()
    sentinel = output / "result.json"
    sentinel.write_text('{"previous": true}', encoding="utf-8")
    digest = sha(sentinel)
    result = engine().run_job(req, lambda _: None)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "OUTPUT_NOT_EMPTY"
    assert sha(sentinel) == digest


def test_cli_emits_json_lines_and_exit_code(case):
    req, _, _ = case
    request_file = Path(req["samples"]["path"]).with_name("request.json")
    request_file.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8-sig")
    worker = ROOT / "backend" / "worker.py"
    assert worker.is_file(), "CLI worker has not been implemented"
    environment = os.environ.copy()
    environment["PROJ_NETWORK"] = "ON"
    process = subprocess.run([sys.executable, str(worker), str(request_file)], capture_output=True, text=True,
                             encoding="utf-8", timeout=45, env=environment)
    assert process.returncode == 0, process.stderr + process.stdout
    events = [json.loads(line) for line in process.stdout.splitlines() if line]
    assert events and all(e["event"] == "progress" for e in events)
    assert read_result(req)["status"] == "completed"
    audit = json.loads((Path(req["output_dir"]) / "audit.json").read_text(encoding="utf-8"))
    assert audit["proj_network_enabled"] is False


def test_reproducible_examples_script(tmp_path):
    script = ROOT / "tools" / "make_examples.py"
    assert script.is_file(), "Reproducible example generator has not been implemented"
    for folder in (tmp_path / "a", tmp_path / "b"):
        proc = subprocess.run([sys.executable, str(script), "--output", str(folder)], capture_output=True, text=True,
                              encoding="utf-8", timeout=45)
        assert proc.returncode == 0, proc.stderr
    for name in ("covariates.tif", "samples.geojson"):
        assert sha(tmp_path / "a" / name) == sha(tmp_path / "b" / name)


def test_scaled_raster_is_not_silently_used_as_raw_values(case):
    """Catches treating stored digital numbers as already scaled physical values."""
    req, _, _ = case
    with rasterio.open(req["covariates"][0]["path"], "r+") as dst:
        dst.scales = (0.1, 1)
    result = engine().run_job(req, lambda _: None)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "RASTER_SCALE"


def test_conflicting_geojson_and_request_crs_is_rejected(case):
    """Catches silently assigning a CRS different from the source declaration."""
    req, _, features = case
    Path(req["samples"]["path"]).write_text(json.dumps({"type": "FeatureCollection", "features": features,
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}}}), encoding="utf-8")
    result = engine().run_job(req, lambda _: None)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "SAMPLE_CRS_CONFLICT"


def test_declared_per_observation_units_must_match(case):
    """Catches silent mixing of targets recorded in different measurement units."""
    req, _, features = case
    req["samples"]["unit_field"] = "measurement_unit"
    for f in features:
        f["properties"]["measurement_unit"] = "g/kg"
    features[1]["properties"]["measurement_unit"] = "%"
    Path(req["samples"]["path"]).write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    result = engine().run_job(req, lambda _: None)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "TARGET_UNIT_MISMATCH"


def test_model_file_is_bound_to_run_id(case):
    req, _, _ = case
    assert engine().run_job(req, lambda _: None)["status"] == "completed"
    with pytest.raises(Exception, match="model ID"):
        engine().load_model(Path(req["output_dir"]) / "model.json", expected_run_id="another-run")


def test_input_changed_during_job_is_not_published(case):
    req, _, features = case
    def change_input(event):
        if event["stage"] == "training" and event["completed"] == 1:
            features[0]["properties"]["soil_value"] = 999
            Path(req["samples"]["path"]).write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    result = engine().run_job(req, change_input)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "SOURCE_CHANGED"
    assert not (Path(req["output_dir"]) / "prediction.tif").exists()


@pytest.mark.parametrize("field,value,code", [
    ("window_size", 4096, "MEMORY_BUDGET"),
    ("uncertainty", {"method": "conformal"}, "UNSUPPORTED_STAGE"),
])
def test_stage_a_resource_and_method_limits(case, field, value, code):
    req, _, _ = case
    req[field] = value
    result = engine().run_job(req, lambda _: None)
    assert result["status"] == "failed"
    assert result["error"]["code"] == code


def test_expected_raster_fingerprint_must_match(case):
    req, _, _ = case
    req["covariates"][0]["sha256"] = "0" * 64
    result = engine().run_job(req, lambda _: None)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "SOURCE_FINGERPRINT"


def test_lonlat_geojson_extracts_from_aligned_single_band_files(case):
    """Catches skipped CRS transformation and assuming all inputs share one file."""
    req, data, features = case
    with rasterio.open(req["covariates"][0]["path"]) as src:
        profile = src.profile.copy()
    profile["count"] = 1
    for i, cov in enumerate(req["covariates"]):
        path = Path(req["samples"]["path"]).with_name("single%d.tif" % i)
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(data[i], 1)
        cov.update(path=str(path), band=1)
    transformer = Transformer.from_crs("EPSG:32637", "EPSG:4326", always_xy=True)
    for f in features:
        f["geometry"]["coordinates"] = list(transformer.transform(*f["geometry"]["coordinates"]))
    Path(req["samples"]["path"]).write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")
    req["samples"].pop("crs")
    result = engine().run_job(req, lambda _: None)
    assert result["status"] == "completed", result
    extracted = np.genfromtxt(Path(req["output_dir"]) / "training_samples.csv", delimiter=",", skip_header=1,
                             usecols=(5, 6), encoding="utf-8-sig")
    np.testing.assert_array_equal(extracted, [[1, 21], [6, 26], [11, 31], [16, 36]])


def test_result_paths_resolve_relative_to_manifest_after_directory_relocation(case):
    """Catches absolute worker paths that disagree with the ArcMap filesystem view."""
    req, _, _ = case
    result = engine().run_job(req, lambda _: None)
    assert result["status"] == "completed", result
    original = Path(req["output_dir"])
    relocated = original.with_name("native-view")
    shutil.copytree(original, relocated)
    manifest = json.loads((relocated / "result.json").read_text(encoding="utf-8"))
    assert {item["kind"] for item in manifest["outputs"]} == {"prediction", "model", "training_samples", "audit"}
    for item in manifest["outputs"]:
        relative = Path(item["path"])
        assert not relative.is_absolute(), "Manifest must not expose worker-specific absolute output paths"
        assert ".." not in relative.parts
        resolved = (relocated / relative).resolve()
        assert resolved.is_relative_to(relocated.resolve())
        assert sha(resolved) == item["sha256"]
        if item["kind"] == "prediction":
            with rasterio.open(resolved) as raster:
                np.testing.assert_allclose(raster.read(1, masked=True).compressed(), 10)
        elif item["kind"] == "model":
            restored = engine().load_model(resolved, expected_run_id="test-run")
            np.testing.assert_array_equal(restored.predict([[1, 21], [6, 26]]), [10, 10])
        elif item["kind"] == "audit":
            assert json.loads(resolved.read_text(encoding="utf-8"))["output_raster_readback_verified"] is True
