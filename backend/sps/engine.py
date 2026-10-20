"""Phase A execution: fixed regression, auditable local files, atomic publication."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import csv
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import sys
import traceback

import numpy as np
from pyproj import network as proj_network
import rasterio
from rasterio.windows import Window
from sklearn.linear_model import Ridge

from . import __version__
from .contract import Cancelled, JobError, required_text, validate_request
from .data import RasterInputs, fingerprint, read_json, read_samples


def write_json(path, obj):
    path = Path(path)
    temporary = path.with_name(path.name + ".partial")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(obj, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@dataclass
class FixedModel:
    coefficients: np.ndarray
    intercept: float

    def predict(self, values):
        values = np.asarray(values, dtype=np.float64)
        if values.ndim != 2 or values.shape[1] != len(self.coefficients) or not np.isfinite(values).all():
            raise JobError("PREDICTION_SCHEMA", "The number or values of the prediction variables do not match the model.")
        # Fixed per-row reduction is independent of raster window dimensions.
        return np.sum(values * self.coefficients, axis=1) + self.intercept


def load_model(path, *, expected_run_id):
    """Read the non-executable JSON model produced by this run; never unpickle input."""
    obj = read_json(path)
    if obj.get("schema_version") != 1 or obj.get("run_id") != expected_run_id or obj.get("model_id") not in ("mean", "ridge"):
        raise JobError("MODEL_SCHEMA", "The model ID or version does not match the run.")
    coefficients = np.asarray(obj.get("coefficients"), dtype=np.float64)
    intercept = float(obj["intercept"])
    if coefficients.ndim != 1 or len(coefficients) != len(obj["covariates"]) or not np.isfinite(coefficients).all() or not math.isfinite(intercept):
        raise JobError("MODEL_SCHEMA", "The model coefficients are invalid.")
    return FixedModel(coefficients, intercept)


def reserve_output(raw):
    value = required_text(raw.get("output_dir") if isinstance(raw, dict) else None, "OUTPUT_PATH", "A full output folder path is required.")
    path = Path(value)
    if not path.is_absolute():
        raise JobError("OUTPUT_PATH", "The output path must be absolute.")
    path = path.resolve()
    if path.exists() and (not path.is_dir() or any(p.name != "cancel.request" for p in path.iterdir())):
        raise JobError("OUTPUT_NOT_EMPTY", "The output folder must be new or empty; a previous job is not overwritten.")
    path.mkdir(parents=True, exist_ok=True)
    # Exclusive claim prevents two worker processes from owning the same empty run directory.
    try:
        with (path / ".run.lock").open("x", encoding="ascii") as stream:
            stream.write(str(os.getpid()))
    except FileExistsError as exc:
        raise JobError("OUTPUT_NOT_EMPTY", "The output folder is in use by another worker.") from exc
    return path


def run_job(request: dict, emit=lambda event: None) -> dict:
    run_id = request.get("run_id", "unknown") if isinstance(request, dict) else "unknown"
    if not isinstance(run_id, str):
        run_id = "unknown"
    result = {"schema_version": 1, "run_id": run_id, "status": "failed", "outputs": [],
              "validation": {"status": "not_performed_stage_a", "scope": "integration_only"}}
    output = None
    produced = []
    started = datetime.now(timezone.utc).isoformat()

    def check_cancel():
        if output is not None and (output / "cancel.request").exists():
            raise Cancelled()

    def progress(stage, completed, total, message):
        event = {"event": "progress", "run_id": run_id, "stage": stage,
                 "completed": int(completed), "total": int(total), "message": message}
        if output is not None:
            with (output / "progress.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
        emit(event)

    try:
        proj_network.set_network_enabled(False)
        output = reserve_output(request)
        check_cancel()
        progress("preflight", 0, 1, "Stage A data and version check started.")
        req = validate_request(request)
        req["output_dir"] = str(output)
        write_json(output / "request.snapshot.json", req)
        samples = read_samples(req["samples"], check_cancel)
        with RasterInputs(req["covariates"]) as rasters:
            paths = sorted(set(rasters.source_paths() + [req["samples"]["path"]]))
            sources = [fingerprint(p, check_cancel) for p in paths]
            source_by_path = {p["path"].casefold(): p for p in sources}
            for cov in req["covariates"]:
                if cov.get("sha256") and cov["sha256"] != source_by_path[str(Path(cov["path"]).resolve()).casefold()]["sha256"]:
                    raise JobError("SOURCE_FINGERPRINT", "The raster content differs from the recorded schema.")
            X, map_coordinates = rasters.extract(samples, check_cancel)
            progress("preflight", 1, 1, "Input IDs, targets, bands and grid verified.")
            check_cancel()
            progress("training", 0, 1, "Training a fixed model; no cross-validation is performed.")
            if req["model"]["id"] == "mean":
                trained = FixedModel(np.zeros(X.shape[1]), float(samples["y"].mean()))
            else:
                estimator = Ridge(alpha=req["model"]["parameters"]["alpha"], solver="svd", fit_intercept=True)
                estimator.fit(X, samples["y"])
                trained = FixedModel(np.asarray(estimator.coef_, dtype=np.float64), float(estimator.intercept_))
            check_cancel()
            model_record = {"schema_version": 1, "run_id": run_id, "core_version": __version__,
                            "model_id": req["model"]["id"], "parameters": req["model"]["parameters"],
                            "coefficients": trained.coefficients.tolist(), "intercept": trained.intercept,
                            "covariates": req["covariates"], "target": req["samples"]["target_field"],
                            "target_unit": req["samples"]["unit"], "training_ids": samples["ids"],
                            "preprocessing": "none", "validation": result["validation"]}
            model_path = output / "model.json"
            write_json(model_path, model_record)
            produced.append(model_path)
            restored = load_model(model_path, expected_run_id=run_id)
            if not np.array_equal(trained.predict(X), restored.predict(X)):
                raise JobError("MODEL_ROUNDTRIP", "Predictions changed after the model was saved and reloaded.")
            progress("training", 1, 1, "Model saved and checked to reproduce the same predictions.")
            training_path = output / "training_samples.csv"
            with training_path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["sample_id", "target", "target_unit", "map_x", "map_y"] + [c["id"] for c in req["covariates"]])
                for identity, target, coord, values in zip(samples["ids"], samples["y"], map_coordinates, X):
                    writer.writerow([identity, float(target), req["samples"]["unit"], float(coord[0]), float(coord[1])] + values.tolist())
            produced.append(training_path)
            reference = rasters.reference
            grid = {"width": reference.width, "height": reference.height, "crs_wkt": reference.crs.to_wkt(),
                    "transform": list(reference.transform), "sampling": "nearest_cell"}
            size = req["window_size"]
            total = math.ceil(reference.height / size) * math.ceil(reference.width / size)
            temporary = output / "prediction.partial.tif"
            final = output / "prediction.tif"
            profile = {"driver": "GTiff", "width": reference.width, "height": reference.height,
                       "count": 1, "dtype": "float32", "crs": reference.crs, "transform": reference.transform,
                       "nodata": float("nan"), "compress": "deflate", "tiled": True,
                       "blockxsize": 256, "blockysize": 256, "BIGTIFF": "IF_SAFER"}
            progress("mapping", 0, total, "Writing the prediction raster in windows.")
            check_cancel()
            valid_count = 0
            with rasterio.open(temporary, "w", **profile) as dst:
                dst.set_band_unit(1, req["samples"]["unit"])
                dst.set_band_description(1, req["samples"]["target_field"])
                dst.update_tags(run_id=run_id, target=req["samples"]["target_field"], target_unit=req["samples"]["unit"],
                                depth="unspecified", method=req["model"]["id"], nominal_coverage="not_applicable",
                                validation_status="not_performed_stage_a", core_version=__version__,
                                covariate_ids=json.dumps([c["id"] for c in req["covariates"]], ensure_ascii=False))
                completed = 0
                for row in range(0, reference.height, size):
                    for col in range(0, reference.width, size):
                        check_cancel()
                        window = Window(col, row, min(size, reference.width - col), min(size, reference.height - row))
                        values, valid = rasters.read_window(window)
                        prediction = np.full(valid.shape, np.nan, dtype=np.float32)
                        if valid.any():
                            computed = restored.predict(values[valid])
                            if not np.isfinite(computed).all() or (np.abs(computed) > np.finfo(np.float32).max).any():
                                raise JobError("NONFINITE_PREDICTION", "A prediction was nonfinite or exceeded the output data type.")
                            prediction[valid] = computed.astype(np.float32)
                        dst.write(prediction, indexes=1, window=window)
                        valid_count += int(valid.sum())
                        completed += 1
                        progress("mapping", completed, total, "Raster window completed.")
            check_cancel()
            progress("verification", 0, total, "Checking the saved raster by reading it back.")
            with rasterio.open(temporary) as saved:
                if (saved.width, saved.height, saved.crs, saved.transform) != (reference.width, reference.height, reference.crs, reference.transform):
                    raise JobError("OUTPUT_GRID", "The output grid does not match the reference grid.")
                if saved.units != (req["samples"]["unit"],) or saved.descriptions != (req["samples"]["target_field"],):
                    raise JobError("OUTPUT_METADATA", "The output band unit or target description could not be saved.")
                completed = 0
                for row in range(0, reference.height, size):
                    for col in range(0, reference.width, size):
                        check_cancel()
                        window = Window(col, row, min(size, reference.width - col), min(size, reference.height - row))
                        values, valid = rasters.read_window(window)
                        actual = saved.read(1, window=window, masked=True)
                        if not np.array_equal(np.ma.getmaskarray(actual), ~valid):
                            raise JobError("OUTPUT_MASK", "The output mask does not match the inputs.")
                        if valid.any() and not np.array_equal(actual.data[valid], restored.predict(values[valid]).astype(np.float32)):
                            raise JobError("OUTPUT_VALUES", "The saved raster does not match the predictions.")
                        completed += 1
                        progress("verification", completed, total, "Saved raster window verified.")
            # A file changed while the worker ran must not be published under its old identity.
            if sources != [fingerprint(p, check_cancel) for p in paths]:
                raise JobError("SOURCE_CHANGED", "An input file changed during the job; the result was not published.")
            check_cancel()
            temporary.replace(final)
            produced.append(final)
            versions = {name: importlib.metadata.version(name) for name in ("numpy", "scipy", "scikit-learn", "rasterio", "pyproj")}
            code_files = [Path(__file__).parent / n for n in ("__init__.py", "contract.py", "data.py", "engine.py", "cli.py")]
            audit = {"schema_version": 1, "run_id": run_id, "core_version": __version__, "started_utc": started,
                     "finished_utc": datetime.now(timezone.utc).isoformat(), "sources": sources, "grid": grid,
                     "sample_count": len(samples["ids"]), "training_ids": samples["ids"], "valid_pixel_count": valid_count,
                     "source_code": [fingerprint(p) for p in code_files if p.is_file()], "packages": versions,
                     "python": sys.version, "python_executable": sys.executable, "platform": platform.platform(),
                     "proj_network_enabled": proj_network.is_network_enabled(),
                     "validation": result["validation"], "uncertainty": "not_implemented_stage_a",
                     "sampling_support": "point_only_stage_a", "assumptions": ["The target unit was declared by the user.",
                     "Sample depth and laboratory method are not harmonized in this stage.",
                     "This run is integration evidence; independent prediction performance was not measured."],
                     "model_roundtrip_verified": True, "output_raster_readback_verified": True}
            audit_path = output / "audit.json"
            write_json(audit_path, audit)
            produced.append(audit_path)
            # ArcMap and the packaged launcher can see different absolute aliases
            # for LocalAppData. Bind products to the manifest directory instead.
            result["outputs"] = [{"kind": kind, "path": path.relative_to(output).as_posix(), "sha256": fingerprint(path, check_cancel)["sha256"]}
                                 for kind, path in (("prediction", final), ("model", model_path), ("training_samples", training_path), ("audit", audit_path))]
            check_cancel()
            result["status"] = "completed"
            result["stage"] = "A"
            write_json(output / "result.json", result)
            progress("completed", 1, 1, "Stage A completed. No scientific validation was performed.")
            return result
    except Exception as exc:
        result["status"] = "cancelled" if isinstance(exc, Cancelled) else "failed"
        result["outputs"] = []
        result["error"] = {"code": exc.code if isinstance(exc, JobError) else "WORKER_ERROR", "message": str(exc)}
        if output is not None:
            # Only files created by this job, inside its freshly claimed directory, are removed.
            for path in produced + list(output.glob("*.partial*")):
                try:
                    if path.is_file():
                        path.unlink()
                except OSError:
                    pass
            try:
                if not isinstance(exc, JobError):
                    (output / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
                write_json(output / "result.json", result)
                progress(result["status"], 0, 1, str(exc))
            except OSError as save_error:
                result["manifest_write_error"] = str(save_error)
        return result
    finally:
        if output is not None:
            try:
                (output / ".run.lock").unlink(missing_ok=True)
            except OSError:
                pass
