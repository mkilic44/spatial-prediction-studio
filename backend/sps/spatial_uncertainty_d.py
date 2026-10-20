"""Experimental distance-weighted split calibration, not a formal LSCP claim.

Scores are absolute errors in the original target unit. The empirical measure
uses unnormalised kernel weights in [0, 1] and a mass of one at positive infinity
for the prediction location. Its weighted quantile is a finite-mass convention;
spatial exchangeability and a coverage theorem are not asserted. Repeated rows
within a dependency group are not independent calibration replicates. Both row
and group effective sample sizes are therefore exposed and screened.
"""
from __future__ import annotations

from copy import deepcopy
import math
import numpy as np
from scipy.spatial import cKDTree

from .contract import JobError
from .modeling import prediction_call
from .validation import _metric_coordinates


REASONS = {
    "SPATIAL_NO_SUPPORT": "Bu konumda pozitif ağırlıklı kalibrasyon gözlemi yok.",
    "SPATIAL_EFFECTIVE_N": "Yerel kalibrasyonun etkin örnek sayısı belirlenen eşiğin altında.",
    "SPATIAL_GROUP_SUPPORT": "Yerel kalibrasyonun grup sayısı veya etkin grup sayısı yetersiz.",
    "SPATIAL_FINITE_MASS": "Test konumunun birim kütlesi eklendiğinde istenen kapsama için sonlu sınır yok.",
}


def _number(value, name, *, minimum=0, strict=True):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.number)):
        raise JobError("SPATIAL_CALIBRATION_SETTING", name + " sonlu sayı olmalıdır.")
    value = float(value)
    if not math.isfinite(value) or (value <= minimum if strict else value < minimum):
        raise JobError("SPATIAL_CALIBRATION_SETTING", name + " geçerli sınırda olmalıdır.")
    return value


def spatial_settings(settings):
    result = {
        "spatial_bandwidth_m": _number(settings.get("spatial_bandwidth_m", 1000.), "Bant genişliği"),
        "spatial_min_effective_n": _number(settings.get("spatial_min_effective_n", 5.), "Etkin örnek sayısı"),
        "spatial_min_groups": settings.get("spatial_min_groups", 3),
        "spatial_kernel": settings.get("spatial_kernel", "gaussian"),
    }
    groups = result["spatial_min_groups"]
    if isinstance(groups, (bool, np.bool_)) or not isinstance(groups, (int, np.integer)) or groups < 2:
        raise JobError("SPATIAL_CALIBRATION_SETTING", "En az iki kalibrasyon grubu istenmelidir.")
    result["spatial_min_groups"] = int(groups)
    if result["spatial_kernel"] not in ("gaussian", "compact"):
        raise JobError("SPATIAL_CALIBRATION_SETTING", "Mekânsal çekirdek gaussian veya compact olmalıdır.")
    return result


def _coverage(coverage):
    coverage = _number(coverage, "Kapsama düzeyi")
    if coverage >= 1:
        raise JobError("COVERAGE_LEVEL", "Kapsama düzeyi 0 ile 1 arasında olmalıdır.")
    return coverage


def weighted_conformal_quantile(scores, weights, coverage):
    """Small independently testable weighted order statistic including test mass 1."""
    coverage = _coverage(coverage)
    scores, weights = np.asarray(scores, dtype=float), np.asarray(weights, dtype=float)
    if scores.ndim != 1 or weights.shape != scores.shape or not np.isfinite(scores).all() or not np.isfinite(weights).all() or np.any(scores < 0) or np.any(weights < 0) or np.any(weights > 1):
        raise JobError("SPATIAL_CALIBRATION_SCORE", "Puanlar ve çekirdek ağırlıkları sonlu ve geçerli olmalıdır.")
    order = np.argsort(scores, kind="stable")
    weights, scores = weights[order], scores[order]
    positive = weights > 0
    weights, scores = weights[positive], scores[positive]
    total = float(weights.sum())
    threshold = coverage * (total + 1.)
    result = {"weight_sum": total, "test_mass": 1., "threshold_mass": threshold}
    if not len(scores) or threshold > total + 8 * np.finfo(float).eps * max(1., total):
        return {**result, "status": "unavailable", "adjustment": None,
                "reason_code": "SPATIAL_FINITE_MASS", "reason": REASONS["SPATIAL_FINITE_MASS"]}
    index = min(int(np.searchsorted(np.cumsum(weights), threshold, side="left")), len(scores) - 1)
    return {**result, "status": "available", "adjustment": float(scores[index]), "reason_code": None}


def _identities(values, name):
    values = list(values) if values is not None else []
    if not values or any(not isinstance(value, str) or not value.strip() for value in values):
        raise JobError("SPATIAL_CALIBRATION_PROVENANCE", name + " kimlikleri boş olmayan metin olmalıdır.")
    return values


def _xy(values, n):
    try:
        result = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as exc:
        raise JobError("COORDINATES", "Mekânsal kalibrasyon için sayısal koordinatlar gereklidir.") from exc
    if result.shape != (n, 2) or not np.isfinite(result).all():
        raise JobError("COORDINATES", "Her gözlem için iki sonlu koordinat gereklidir.")
    return result


class SpatialCalibration:
    def __init__(self, scores, coordinates, ids, groups, settings, *, crs, validation,
                 training_ids, training_groups, training_coordinates):
        self.config = spatial_settings(settings)
        self.coverage = _coverage(settings.get("coverage", .9))
        ids, groups = _identities(ids, "Kalibrasyon"), _identities(groups, "Kalibrasyon grubu")
        training_ids = _identities(training_ids, "Eğitim")
        training_groups = _identities(training_groups, "Eğitim grubu")
        if len(groups) != len(ids) or len(training_groups) != len(training_ids) or len(set(ids)) != len(ids) or len(set(training_ids)) != len(training_ids):
            raise JobError("SPATIAL_CALIBRATION_PROVENANCE", "Eğitim ve kalibrasyon kimlik/grup boyutları veya benzersizliği geçersiz.")
        if set(ids) & set(training_ids) or set(groups) & set(training_groups):
            raise JobError("SPATIAL_CALIBRATION_LEAKAGE", "Eğitim ve kalibrasyon örnekleri veya bağımlılık grupları çakışıyor.")
        scores = np.asarray(scores, dtype=float)
        if scores.shape != (len(ids),) or not np.isfinite(scores).all() or np.any(scores < 0):
            raise JobError("SPATIAL_CALIBRATION_SCORE", "Kalibrasyon mutlak hataları sonlu ve negatif olmayan değerler olmalıdır.")
        xy, train_xy = _xy(coordinates, len(ids)), _xy(training_coordinates, len(training_ids))
        self.crs, self.validation = crs, deepcopy(validation)
        metric, self.metric_crs = _metric_coordinates(xy, crs, self.validation, True)
        train_metric, _ = _metric_coordinates(train_xy, crs, self.validation, True)
        buffer = _number(validation.get("buffer_distance", 0), "Tampon uzaklığı", strict=False)
        nearest = cKDTree(train_metric).query(metric, k=1, workers=1)[0]
        if np.any(nearest <= 1e-5) or (buffer > 0 and np.any(nearest < buffer)):
            raise JobError("SPATIAL_CALIBRATION_LEAKAGE", "Kalibrasyon konumu eğitim konumuyla çakışıyor veya tampon sınırını ihlal ediyor.")
        if len(ids) < self.config["spatial_min_effective_n"] or len(set(groups)) < self.config["spatial_min_groups"] or self.coverage * (len(ids) + 1) > len(ids) + 1e-12:
            raise JobError("SPATIAL_CALIBRATION_SIZE", "Ayrılmış kalibrasyonda örnek veya grup sayısı istenen yerel aralık için yetersiz.")
        order = np.argsort(ids, kind="stable")
        self.ids = tuple(ids[i] for i in order)
        self.groups = tuple(groups[i] for i in order)
        self.training_ids, self.training_groups = tuple(sorted(training_ids)), tuple(sorted(set(training_groups)))
        self.scores, self.metric_coordinates = scores[order].copy(), metric[order].copy()
        self.scores.setflags(write=False)
        self.metric_coordinates.setflags(write=False)
        _, self.group_indices = np.unique(self.groups, return_inverse=True)

    def adjustments(self, coordinates, coverage=None, *, check_cancel=lambda: None):
        check_cancel()
        coverage = self.coverage if coverage is None else _coverage(coverage)
        xy = np.asarray(coordinates)
        xy = _xy(xy, len(xy) if xy.ndim else 0)
        metric, _ = _metric_coordinates(xy, self.crs, self.validation, True)
        n = len(xy)
        output = {"adjustment": np.full(n, np.nan), "interval_available": np.zeros(n, dtype=bool),
                  "interval_reason_code": np.full(n, "", dtype=object), "interval_effective_n": np.zeros(n),
                  "interval_group_count": np.zeros(n, dtype=int), "interval_effective_group_n": np.zeros(n),
                  "interval_weight_sum": np.zeros(n)}
        for i, location in enumerate(metric):
            if i % 128 == 0:check_cancel()
            distance = np.hypot(*(self.metric_coordinates - location).T)
            with np.errstate(over="ignore", under="ignore"):
                ratio = distance / self.config["spatial_bandwidth_m"]
                weights = np.exp(-.5 * ratio ** 2) if self.config["spatial_kernel"] == "gaussian" else np.maximum(1. - ratio ** 2, 0.) ** 2
            positive = weights > 0
            reason = None
            if not np.any(positive):
                reason = "SPATIAL_NO_SUPPORT"
            else:
                normal = weights / weights.max()
                group_weights = np.bincount(self.group_indices, weights=normal)
                effective = float(normal.sum() ** 2 / np.sum(normal ** 2))
                effective_groups = float(group_weights.sum() ** 2 / np.sum(group_weights ** 2))
                group_count = int(np.count_nonzero(group_weights))
                output["interval_effective_n"][i] = effective
                output["interval_group_count"][i] = group_count
                output["interval_effective_group_n"][i] = effective_groups
                if effective + 1e-12 < self.config["spatial_min_effective_n"]:
                    reason = "SPATIAL_EFFECTIVE_N"
                elif group_count < self.config["spatial_min_groups"] or effective_groups + 1e-12 < self.config["spatial_min_groups"]:
                    reason = "SPATIAL_GROUP_SUPPORT"
            output["interval_weight_sum"][i] = float(weights.sum())
            if reason is None:
                quantile = weighted_conformal_quantile(self.scores, weights, coverage)
                reason = quantile["reason_code"]
                if reason is None:
                    output["adjustment"][i] = quantile["adjustment"]
                    output["interval_available"][i] = True
            output["interval_reason_code"][i] = reason or ""
        check_cancel()
        return output

    def record(self):
        return {"size": len(self.ids), "group_count": len(set(self.groups)), "ids": list(self.ids),
                "groups": list(self.groups), "training_ids": list(self.training_ids),
                "training_groups": list(self.training_groups), "scores": self.scores.tolist(),
                "metric_coordinates_m": self.metric_coordinates.tolist(), "source_crs": str(self.crs),
                "metric_crs": self.metric_crs, "distance_unit": "metre", "coverage": self.coverage,
                "score": "absolute_residual", "unit": "original_target_unit", "method_status": "experimental",
                "calibration_unit": "observation; groups preserved and effective group count screened",
                "test_mass": 1., "test_mass_location": "positive_infinity", "kernel_settings": deepcopy(self.config),
                "bandwidth_selection": "fixed_before_evaluation; not selected using calibration or held-out targets",
                "coverage_claim": "empirically evaluated spatial adaptation; no exact finite-sample or spatial coverage guarantee; not LSCP",
                "unavailable_policy": "point prediction retained; interval unavailable with reason; raster interval NoData"}


class SpatialCalibratedModel:
    requires_coordinates = True

    def __init__(self, base, calibration):
        self.base, self.calibration = base, calibration
        self.record = {**base.record, "uncertainty_method": "spatial_conformal", "calibration_ids": list(calibration.ids),
                       "calibration": calibration.record()}

    def __getstate__(self):
        state=self.__dict__.copy()
        state.pop('_check_cancel_',None)
        return state

    def predict(self, X, coordinates=None):
        _xy(coordinates, len(X))
        return prediction_call(self.base, "predict", X, coordinates)

    def plain_predict(self, X, coordinates=None):
        return prediction_call(self.base, "plain_predict", X, coordinates)

    def interval_details(self, X, coverage=None, coordinates=None):
        details = self.calibration.adjustments(_xy(coordinates, len(X)), coverage, check_cancel=getattr(self,'_check_cancel_',lambda:None))
        predicted = np.asarray(self.predict(X, coordinates), dtype=float)
        if predicted.shape != (len(X),) or not np.isfinite(predicted).all():
            raise JobError("SPATIAL_CALIBRATION_PREDICTION", "Nokta tahmini sonlu ve gözlem sayısıyla eşleşen değerler vermelidir.")
        lower, upper = predicted - details["adjustment"], predicted + details["adjustment"]
        available = details["interval_available"]
        if not np.isfinite(lower[available]).all() or not np.isfinite(upper[available]).all():
            raise JobError("INTERVAL", "Yerel aralık özgün birimde sonlu hesaplanamadı.")
        return {**details, "lower": lower, "upper": upper}

    def predict_interval(self, X, coordinates=None):
        return self.predict_interval_at_coverage(X, self.calibration.coverage, coordinates)

    def predict_interval_at_coverage(self, X, coverage, coordinates=None):
        details = self.interval_details(X, coverage, coordinates)
        return details["lower"], details["upper"]

    def quantile_crossings(self, X, coordinates=None):
        return np.zeros(len(X), dtype=bool)

    def quantile_crossings_at_coverage(self, X, coverage, coordinates=None):
        _coverage(coverage)
        return np.zeros(len(X), dtype=bool)

    def diagnostics(self, X, coordinates=None):
        diagnostics = prediction_call(self.base, "diagnostics", X, coordinates)
        details = self.calibration.adjustments(_xy(coordinates, len(X)), check_cancel=getattr(self,'_check_cancel_',lambda:None))
        return {**diagnostics, **{key: value for key, value in details.items() if key != "adjustment"}}


def spatial_interval_details(model, X, coordinates, coverage):
    """Reach the calibration through existing applicability wrappers without refit."""
    current = model
    while not isinstance(current, SpatialCalibratedModel):
        if not hasattr(current, "base"):
            raise JobError("SPATIAL_CALIBRATION_MODEL", "Mekânsal kalibrasyon sarmalayıcısı bulunamadı.")
        current = current.base
    return current.interval_details(X, coverage, coordinates)
