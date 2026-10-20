"""Separated calibration in the original target unit, with finite-sample ranks."""
from __future__ import annotations

import math
import numpy as np

from .contract import JobError
from .pipeline import audit
from .modeling import prediction_call


def diagnostic_levels(nominal):
    return sorted({.5, .8, .9, float(nominal)})


def finite_sample_quantile(scores, coverage):
    scores = np.asarray(scores, dtype=float)
    if scores.ndim != 1 or len(scores) == 0 or not np.isfinite(scores).all() or not 0 < coverage < 1:
        raise JobError("CALIBRATION", "Invalid calibration score or coverage.")
    rank = int(math.ceil((len(scores) + 1) * float(coverage)))
    if rank > len(scores):
        raise JobError("CALIBRATION_SIZE", "Too few calibration samples for the nominal coverage; a finite bound cannot be computed.")
    return float(np.sort(scores)[rank - 1])


class CalibratedModel:
    def __init__(self, base, adjustment, method, record):
        self.base = base
        self.adjustment = adjustment
        self.method = method
        self.record = record
        self.requires_coordinates = getattr(base, 'requires_coordinates', False)

    def predict(self, X, coordinates=None):
        return prediction_call(self.base, 'predict', X, coordinates)

    def plain_predict(self, X, coordinates=None):
        return prediction_call(self.base, 'plain_predict', X, coordinates)

    def predict_interval(self, X, coordinates=None):
        return self.predict_interval_at_coverage(X, self.record["calibration"]["coverage"], coordinates)

    def predict_interval_at_coverage(self, X, coverage, coordinates=None):
        entry = self.record["calibration"]["levels"].get(str(float(coverage)))
        if entry is None:
            raise JobError("CALIBRATION_LEVEL_NOT_PREPARED", "This coverage level was not prepared with the reserved calibration data.")
        if entry["status"] != "available":
            raise JobError(entry["reason_code"], entry["reason"])
        adjustment = entry["adjustment"]
        if self.method == "split_conformal":
            prediction = prediction_call(self.base, 'predict', X, coordinates)
            return prediction - adjustment, prediction + adjustment
        if np.isclose(coverage, self.record["calibration"]["coverage"], rtol=0, atol=1e-12):
            lower, upper = self.base.predict_interval(X)
        else:
            lower, upper = self.base.predict_interval_at_coverage(X, coverage)
        lower, upper = lower - adjustment, upper + adjustment
        if np.any(lower > upper):
            raise JobError("CQR_EMPTY_INTERVAL", "A negative CQR adjustment produced an empty prediction interval; the bounds cannot be reordered.")
        return lower, upper

    def quantile_crossings(self, X, coordinates=None):
        return prediction_call(self.base, 'quantile_crossings', X, coordinates)

    def quantile_crossings_at_coverage(self, X, coverage, coordinates=None):
        return np.zeros(len(X), dtype=bool) if self.method == "split_conformal" else self.base.quantile_crossings_at_coverage(X, coverage)

    def diagnostics(self, X, coordinates=None):
        return prediction_call(self.base, 'diagnostics', X, coordinates)


def calibrate(base, X, y, ids, settings, *, role, learning_audit, coordinates=None,
              groups=None, training_ids=None, training_groups=None, training_coordinates=None,
              crs=None, validation=None):
    method = settings.get("method", "none")
    y = np.asarray(y, dtype=float)
    if method == "spatial_conformal":
        from .spatial_uncertainty_d import SpatialCalibration, SpatialCalibratedModel
        predictions = np.asarray(prediction_call(base, 'predict', X, coordinates), dtype=float)
        if y.shape != (len(ids),) or predictions.shape != y.shape or not np.isfinite(y).all() or not np.isfinite(predictions).all():
            raise JobError("SPATIAL_CALIBRATION_SCORE", "Calibration observations and predictions in the original unit must be finite and the same size.")
        calibration = SpatialCalibration(np.abs(y - predictions), coordinates, ids, groups, settings,
            crs=crs, validation=validation or {}, training_ids=training_ids,
            training_groups=training_groups, training_coordinates=training_coordinates)
        audit(learning_audit, role, "calibration", ids, method=method,
              group_ids=list(groups), training_group_ids=sorted(set(training_groups)),
              source_crs=str(crs), metric_crs=calibration.metric_crs)
        return SpatialCalibratedModel(base, calibration)
    if method == "split_conformal":
        scores = np.abs(y - prediction_call(base, 'predict', X, coordinates))
    elif method == "cqr":
        lower, upper = base.predict_interval(X)
        scores = np.maximum(lower - y, y - upper)
    else:
        raise JobError("CALIBRATION_METHOD", "Incompatible calibration method.")
    coverage = float(settings.get("coverage", .9))
    adjustment = finite_sample_quantile(scores, coverage)
    levels = {}
    for level in diagnostic_levels(coverage):
        try:
            level_scores = scores
            if method == "cqr" and not np.isclose(level, coverage, rtol=0, atol=1e-12):
                if not hasattr(base, "predict_interval_at_coverage"):
                    raise JobError("QUANTILE_LEVEL_NOT_FITTED", "No quantile model is available for this coverage level.")
                lower, upper = base.predict_interval_at_coverage(X, level)
                level_scores = np.maximum(lower - y, y - upper)
            levels[str(level)] = {"status": "available", "adjustment": finite_sample_quantile(level_scores, level),
                "rank": int(math.ceil((len(scores) + 1) * level)), "scores": level_scores.tolist()}
        except JobError as exc:
            levels[str(level)] = {"status": "unavailable", "reason_code": exc.code, "reason": str(exc)}
    record = {**base.record, "calibration_ids": list(ids), "uncertainty_method": method,
        "calibration": {"size": len(scores), "rank": int(math.ceil((len(scores) + 1) * coverage)), "adjustment": adjustment,
            "scores": scores.tolist(), "coverage": coverage, "score": "absolute_residual" if method == "split_conformal" else "max(lower-y,y-upper)",
            "crossing_count": int(np.sum(prediction_call(base, 'quantile_crossings', X, coordinates))),
            "levels": levels, "levels_scope": "same reserved calibration observations; no outer or independent targets",
            "unit": "original_target_unit", "coverage_claim": "exchangeability-dependent; spatial coverage is assessed empirically"}}
    audit(learning_audit, role, "calibration", ids, method=method)
    return CalibratedModel(base, adjustment, method, record)


def interval_metrics(y, lower, upper, coverage):
    y, lower, upper = [np.asarray(values, dtype=float) for values in (y, lower, upper)]
    if not (y.shape == lower.shape == upper.shape) or not np.isfinite(np.concatenate([y, lower, upper])).all() or (lower > upper).any():
        raise JobError("INTERVAL", "Invalid prediction interval size or bound order.")
    alpha = 1 - float(coverage)
    width = upper - lower
    def pinball(q, prediction):
        error = y - prediction
        return float(np.mean(np.maximum(q * error, (q - 1) * error)))
    return {"nominal_coverage": float(coverage), "coverage": float(np.mean((y >= lower) & (y <= upper))),
        "mean_width": float(np.mean(width)), "median_width": float(np.median(width)),
        "below_lower_fraction": float(np.mean(y < lower)), "above_upper_fraction": float(np.mean(y > upper)),
        "pinball_lower": pinball(alpha / 2, lower), "pinball_upper": pinball(1 - alpha / 2, upper),
        "interval_score": float(np.mean(width + 2 / alpha * np.maximum(lower - y, 0) + 2 / alpha * np.maximum(y - upper, 0)))}
