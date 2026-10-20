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
        super().__init__("CANCELLED", "İş kullanıcı tarafından iptal edildi.")


def required_text(value, code, message):
    if not isinstance(value, str) or not value.strip():
        raise JobError(code, message)
    return value.strip()


def local_file(value):
    value = required_text(value, "INPUT_PATH", "Girdi için tam yerel dosya yolu gereklidir.")
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise JobError("INPUT_PATH", "Girdi dosyası bulunamadı veya yol mutlak değil: " + value)
    return str(path.resolve())


def validate_request(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise JobError("REQUEST_TYPE", "İş isteği bir JSON nesnesi olmalıdır.")
    req = deepcopy(raw)
    if type(req.get("schema_version")) is not int or req["schema_version"] != 1:
        raise JobError("SCHEMA_VERSION", "Yalnız schema_version=1 destekleniyor.")
    run_id = required_text(req.get("run_id"), "RUN_ID", "Çalışma kimliği gereklidir.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id):
        raise JobError("RUN_ID", "Çalışma kimliği yalnız harf, rakam, nokta, kısa çizgi ve alt çizgi içerebilir.")
    req["run_id"] = run_id
    if req.get("task") != "regression":
        raise JobError("UNSUPPORTED_TASK", "Aşama A yalnız tek hedefli regresyonu destekliyor.")
    for key in ("validation", "cross_validation", "uncertainty", "optimization", "feature_selection", "preprocessing", "target_transform"):
        if key in req and req[key] is not None:
            raise JobError("UNSUPPORTED_STAGE", key + " Aşama A kapsamında uygulanmadı.")
    samples = req.get("samples")
    if not isinstance(samples, dict):
        raise JobError("SAMPLES", "Örnek veri sözleşmesi gereklidir.")
    samples["path"] = local_file(samples.get("path"))
    if Path(samples["path"]).suffix.lower() not in (".geojson", ".json", ".csv"):
        raise JobError("SAMPLE_FORMAT", "Aşama A, GeoJSON ve koordinatlı CSV kabul eder.")
    for field in ("id_field", "target_field"):
        samples[field] = required_text(samples.get(field), "SAMPLE_FIELD", field + " gereklidir.")
    if samples["id_field"] == samples["target_field"]:
        raise JobError("SAMPLE_FIELD", "Kimlik ve hedef aynı alan olamaz.")
    samples["unit"] = required_text(samples.get("unit"), "MISSING_UNIT", "Hedef birimi boş olamaz.")
    if "unit_field" in samples:
        samples["unit_field"] = required_text(samples["unit_field"], "SAMPLE_FIELD", "Birim alanı boş olamaz.")
    if Path(samples["path"]).suffix.lower() == ".csv":
        for field in ("x_field", "y_field", "crs"):
            samples[field] = required_text(samples.get(field), "SAMPLE_CRS", "CSV için " + field + " gereklidir.")
    covs = req.get("covariates")
    if not isinstance(covs, list) or not 1 <= len(covs) <= 256:
        raise JobError("COVARIATES", "Aşama A için 1-256 çevresel bant gereklidir.")
    identifiers = set()
    bands = set()
    for cov in covs:
        if not isinstance(cov, dict):
            raise JobError("COVARIATES", "Her çevresel değişken nesne olmalıdır.")
        for field in ("id", "name", "unit"):
            cov[field] = required_text(cov.get(field), "COVARIATE_FIELD", "Çevresel değişken için " + field + " gereklidir.")
        if cov["id"] in identifiers:
            raise JobError("DUPLICATE_COVARIATE", "Yinelenen çevresel değişken kimliği: " + cov["id"])
        identifiers.add(cov["id"])
        cov["path"] = local_file(cov.get("path"))
        if Path(cov["path"]).suffix.lower() not in (".tif", ".tiff"):
            raise JobError("RASTER_FORMAT", "Aşama A yalnız yerel GeoTIFF rasterleri kabul eder.")
        if cov.get("kind") != "continuous":
            raise JobError("UNSUPPORTED_COVARIATE", "Aşama A yalnız sürekli raster değişkenlerini destekliyor.")
        if type(cov.get("band")) is not int or cov["band"] < 1:
            raise JobError("BAND_RANGE", "Bant numarası 1 tabanlı pozitif tamsayı olmalıdır.")
        band_key = (cov["path"].casefold(), cov["band"])
        if band_key in bands:
            raise JobError("DUPLICATE_BAND", "Aynı raster bandı iki değişken olarak seçilemez.")
        bands.add(band_key)
    model = req.get("model")
    if not isinstance(model, dict) or model.get("id") not in ("mean", "ridge"):
        raise JobError("UNSUPPORTED_MODEL", "Aşama A için yalnız mean ve ridge uygulanmıştır.")
    params = model.setdefault("parameters", {})
    if not isinstance(params, dict) or set(params) - ({"alpha"} if model["id"] == "ridge" else set()):
        raise JobError("MODEL_PARAMETER", "Model için desteklenmeyen parametre var.")
    if model["id"] == "ridge":
        alpha = params.setdefault("alpha", 1.0)
        if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) or not math.isfinite(alpha) or alpha <= 0:
            raise JobError("MODEL_PARAMETER", "Ridge alpha sonlu ve sıfırdan büyük olmalıdır.")
    window = req.setdefault("window_size", 256)
    if type(window) is not int or not 1 <= window <= 4096:
        raise JobError("WINDOW_SIZE", "Pencere boyutu 1-4096 arasında tamsayı olmalıdır.")
    # Float input, predictor matrix, validity mask and output coexist during mapping.
    if window * window * (24 * len(covs) + 16) > 512 * 1024 * 1024:
        raise JobError("MEMORY_BUDGET", "Pencere ve değişken sayısı 512 MiB işçi tampon bütçesini aşıyor. Pencereyi küçültün.")
    return req
