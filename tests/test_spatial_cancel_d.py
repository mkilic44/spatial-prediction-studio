"""Spatial intervals stop within a raster window, including after local reload."""
import io
from types import SimpleNamespace

import joblib
import numpy as np
import pytest

from test_spatial_uncertainty_d import calibration, PointModel, nested_case
from sps.applicability import ApplicableWorkflow
from sps.contract import Cancelled
from sps.evaluation import _fit_partition
from sps.modeling import bind_runtime_callbacks
from sps.spatial_uncertainty_d import SpatialCalibratedModel


def queries():
    xy = np.column_stack([np.linspace(20., 100., 513), np.zeros(513)])
    return np.ones((len(xy), 1)), xy


@pytest.mark.parametrize("stop_at", [2, 3])
def test_calibration_cancels_during_window_not_only_at_entry(monkeypatch, stop_at):
    import sps.spatial_uncertainty_d as spatial
    cal = calibration()
    _, xy = queries()
    before = xy.copy()
    calls, completed = [], []
    quantile = spatial.weighted_conformal_quantile
    def counted(*args, **kwargs):
        completed.append(1)
        return quantile(*args, **kwargs)
    def cancel():
        calls.append(1)
        if len(calls) == stop_at:
            raise Cancelled()
    monkeypatch.setattr(spatial, "weighted_conformal_quantile", counted)
    with pytest.raises(Cancelled):
        cal.adjustments(xy, check_cancel=cancel)
    assert len(calls) == stop_at and len(completed) < len(xy)
    if stop_at == 3:
        assert 0 < len(completed) <= 256
    np.testing.assert_array_equal(xy, before)


@pytest.mark.parametrize("method", ["predict_interval", "diagnostics"])
def test_loaded_spatial_wrapper_rebinds_cancel_through_applicability(method):
    spatial = SpatialCalibratedModel(PointModel(), calibration())
    outer = ApplicableWorkflow(spatial, None, [], SimpleNamespace(record={}))
    # A local lambda cannot ordinarily be pickled. It must stay transient.
    bind_runtime_callbacks(outer, lambda: None)
    stream = io.BytesIO()
    joblib.dump(outer, stream)
    stream.seek(0)
    restored = joblib.load(stream)
    assert not hasattr(restored.base, "_check_cancel_")
    calls = []
    def cancel():
        calls.append(1)
        if len(calls) == 3:
            raise Cancelled()
    assert bind_runtime_callbacks(restored, cancel) is restored
    X, xy = queries()
    with pytest.raises(Cancelled):
        getattr(restored, method)(X, coordinates=xy)
    assert len(calls) == 3


def test_actual_fit_partition_binds_spatial_callback_before_outer_prediction(nested_case):
    state = {"armed": False, "count": 0}
    def cancel():
        if state["armed"]:
            state["count"] += 1
            if state["count"] == 3:
                raise Cancelled()
    model, record = _fit_partition(*nested_case, crs="EPSG:32637", role="cancel-test",
        audit=[], trials=[], check_cancel=cancel)
    assert model._check_cancel_ is cancel
    assert record["calibration_ids"]
    state["armed"] = True
    X = np.repeat(nested_case[0][:1], 513, axis=0)
    xy = np.repeat(nested_case[3][:1], 513, axis=0)
    with pytest.raises(Cancelled):
        model.predict_interval(X, coordinates=xy)
    assert state["count"] == 3
