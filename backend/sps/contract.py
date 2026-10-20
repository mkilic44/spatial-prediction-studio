"""Versioned, fail-closed Stage A job contract."""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import re


class JobError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class Cancelled(JobError):
    def __init__(self):
        super().__init__("CANCELLED", "The job was canceled by the user.")


def required_text(value, code, message):
    if not isinstance(value, str) or not value.strip():
        raise JobError(code, message)
    return value.strip()


def local_file(value):
    value = required_text(value, "INPUT_PATH", "A full local file path is required for the input.")
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise JobError("INPUT_PATH", "Input file not found, or the path is not absolute: " + value)
    return str(path.resolve())


def validate_request(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise JobError("REQUEST_TYPE", "The job request must be a JSON object.")
    req = deepcopy(raw)
    if type(req.get("schema_version")) is not int or req["schema_version"] != 1:
        raise JobError("SCHEMA_VERSION", "Only schema_version=1 is supported.")
    run_id = required_text(req.get("run_id"), "RUN_ID", "A run ID is required.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id):
        raise JobError("RUN_ID", "The run ID may contain only letters, digits, periods, hyphens and underscores.")
    req["run_id"] = run_id
    if req.get("task") != "regression":
        raise JobError("UNSUPPORTED_TASK", "Stage A supports only single-target regression.")
    for key in ("validation", "cross_validation", "uncertainty", "optimization", "feature_selection", "preprocessing", "target_transform"):
        if key in req and req[key] is not None:
            raise JobError("UNSUPPORTED_STAGE", key + " is not implemented in Stage A.")
    samples = req.get("samples")
    if not isinstance(samples, dict):
        raise JobError("SAMPLES", "A sample data contract is required.")
    samples["path"] = local_file(samples.get("path"))
    if Path(samples["path"]).suffix.lower() not in (".geojson", ".json", ".csv"):
        raise JobError("SAMPLE_FORMAT", "Stage A accepts GeoJSON and CSV files with coordinates.")
    for field in ("id_field", "target_field"):
        samples[field] = required_text(samples.get(field), "SAMPLE_FIELD", field + " is required.")
    if samples["id_field"] == samples["target_field"]:
        raise JobError("SAMPLE_FIELD", "The ID and target cannot be the same field.")
    samples["unit"] = required_text(samples.get("unit"), "MISSING_UNIT", "The target unit cannot be empty.")
    if "unit_field" in samples:
        samples["unit_field"] = required_text(samples["unit_field"], "SAMPLE_FIELD", "The unit field cannot be empty.")
    if Path(samples["path"]).suffix.lower() == ".csv":
        for field in ("x_field", "y_field", "crs"):
            samples[field] = required_text(samples.get(field), "SAMPLE_CRS", field + " is required for CSV.")
    covs = req.get("covariates")
    if not isinstance(covs, list) or not 1 <= len(covs) <= 256:
        raise JobError("COVARIATES", "Stage A requires 1 to 256 environmental bands.")
    identifiers = set()
    bands = set()
    for cov in covs:
        if not isinstance(cov, dict):
            raise JobError("COVARIATES", "Each environmental covariate must be an object.")
        for field in ("id", "name", "unit"):
            cov[field] = required_text(cov.get(field), "COVARIATE_FIELD", field + " is required for each environmental covariate.")
        if cov["id"] in identifiers:
            raise JobError("DUPLICATE_COVARIATE", "Duplicate environmental covariate ID: " + cov["id"])
        identifiers.add(cov["id"])
        cov["path"] = local_file(cov.get("path"))
        if Path(cov["path"]).suffix.lower() not in (".tif", ".tiff"):
            raise JobError("RASTER_FORMAT", "Stage A accepts only local GeoTIFF rasters.")
        if cov.get("kind") != "continuous":
            raise JobError("UNSUPPORTED_COVARIATE", "Stage A supports only continuous raster variables.")
        if type(cov.get("band")) is not int or cov["band"] < 1:
            raise JobError("BAND_RANGE", "The band number must be a positive, 1-based integer.")
        band_key = (cov["path"].casefold(), cov["band"])
        if band_key in bands:
            raise JobError("DUPLICATE_BAND", "The same raster band cannot be selected as two variables.")
        bands.add(band_key)
    model = req.get("model")
    if not isinstance(model, dict) or model.get("id") not in ("mean", "ridge"):
        raise JobError("UNSUPPORTED_MODEL", "Only mean and ridge are implemented for Stage A.")
    params = model.setdefault("parameters", {})
    if not isinstance(params, dict) or set(params) - ({"alpha"} if model["id"] == "ridge" else set()):
        raise JobError("MODEL_PARAMETER", "The model has an unsupported parameter.")
    if model["id"] == "ridge":
        alpha = params.setdefault("alpha", 1.0)
        if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not math.isfinite(alpha) or alpha <= 0:
            raise JobError("MODEL_PARAMETER", "Ridge alpha must be finite and greater than zero.")
    window = req.setdefault("window_size", 256)
    if type(window) is not int or not 1 <= window <= 4096:
        raise JobError("WINDOW_SIZE", "The window size must be an integer from 1 to 4096.")
    # Float input, predictor matrix, validity mask and output coexist during mapping.
    if window * window * (24 * len(covs) + 16) > 512 * 1024 * 1024:
        raise JobError("MEMORY_BUDGET", "The window size and number of variables exceed the 512 MiB worker buffer budget. Reduce the window size.")
    return req
