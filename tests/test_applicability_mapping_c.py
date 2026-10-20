"""Applicability counts use valid input cells before optional prediction masking."""
from pathlib import Path
from types import SimpleNamespace
import json
import sys

import numpy as np
import pytest
import rasterio
from affine import Affine
from pyproj import CRS

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from sps.engine_b import map_products
from sps.reporting import build_reports
from test_reporting_b import report_case


class SmallRasterInputs:
    def __init__(self, *, geographic=False, empty=False):
        self.values = np.arange(8, dtype=float).reshape(2, 4, 1)
        self.valid = np.ones((2, 4), dtype=bool)
        self.valid[1, 2] = False
        if empty:
            self.valid[:] = False
        transform = (Affine(.1, 0, 36, 0, -.1, 38) if geographic
                     else Affine(30, 6, 500000, 3, -30, 4200000))
        self.reference = SimpleNamespace(
            width=4, height=2, transform=transform,
            crs=rasterio.crs.CRS.from_epsg(4326 if geographic else 32637))

    def read_window(self, window):
        row, column = int(window.row_off), int(window.col_off)
        height, width = int(window.height), int(window.width)
        return (self.values[row:row + height, column:column + width],
                self.valid[row:row + height, column:column + width])


class SmallModel:
    def predict(self, values):
        return 100 + values[:, 0]

    def predict_interval(self, values):
        prediction = self.predict(values)
        return prediction - 1, prediction + 1

    def diagnostics(self, values):
        x = values[:, 0]
        return {
            "range_extrapolation": x == 2,
            "unknown_category": x == 4,
            # Cell 1 lies within the univariate range but outside applicability.
            "applicability": ~np.isin(x, [1, 2, 4]),
            "dissimilarity": x / 10,
        }


def mapping_case(tmp_path, *, mask=True, enabled=True, size=2, geographic=False, empty=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    rasters = SmallRasterInputs(geographic=geographic, empty=empty)
    crs = CRS.from_user_input(rasters.reference.crs)
    coordinate = rasters.reference.transform * (.5, .5)
    samples = {"ids": ["S1"], "coordinates": np.asarray([coordinate]), "crs": crs,
               "depth": [0, 20], "support": "point"}
    request = {
        "run_id": "applicability_mapping_fixture", "window_size": size,
        "samples": {"unit": "g/kg", "target_field": "soil_value"},
        "covariates": [{"id": "r1"}], "model": {"id": "ridge"},
        "validation": {}, "uncertainty": {"method": "split_conformal", "coverage": .9},
        "diagnostics": {"applicability": enabled},
        "mapping": {"mask_extrapolation": mask, "physical_min": 0, "physical_max": 105},
    }
    record = {"development_ids": ["S1"], "model": {"id": "ridge"}}
    paths, audit = map_products(tmp_path, rasters, SmallModel(), record, request, samples,
                                lambda: None, lambda *args: None)
    arrays = {}
    for kind, path in paths:
        with rasterio.open(path) as dataset:
            arrays[kind] = dataset.read(1)
    return arrays, audit, rasters


@pytest.mark.parametrize("mask", [False, True])
def test_applicability_mask_affects_only_prediction_and_interval_products(tmp_path, mask):
    arrays, audit, rasters = mapping_case(tmp_path, mask=mask)
    # Range and category checks both pass at row 0, column 1.
    assert arrays["range_extrapolation"][0, 1] == 0
    assert arrays["unknown_category"][0, 1] == 0
    assert arrays["applicability"][0, 1] == 0
    for kind in ("prediction", "lower", "upper", "width"):
        assert np.isnan(arrays[kind][0, 1]) == mask
        assert np.isfinite(arrays[kind][0, 0])
    for kind in ("applicability", "dissimilarity", "range_extrapolation",
                 "unknown_category", "physical_violation", "nearest_distance"):
        assert np.isfinite(arrays[kind][rasters.valid]).all()
        assert np.isnan(arrays[kind][~rasters.valid]).all()
    assert arrays["physical_violation"][1, 3] == 1
    assert audit["valid_input_pixels"] == 7
    assert audit["diagnostic_pixels"]["applicability"] == 4
    assert audit["diagnostic_pixels"]["outside_applicability"] == 3
    coverage = audit["applicability_coverage"]
    assert coverage["inside_fraction"] == pytest.approx(4 / 7)
    assert coverage["outside_fraction"] == pytest.approx(3 / 7)
    assert coverage["prediction_mask_includes_outside_applicability"] is mask


def test_disabled_applicability_does_not_change_existing_range_category_mask(tmp_path):
    arrays, audit, _ = mapping_case(tmp_path, mask=True, enabled=False)
    assert arrays["prediction"][0, 1] == 101
    assert np.isnan(arrays["prediction"][0, 2])  # Existing range mask.
    assert np.isnan(arrays["prediction"][1, 0])  # Existing unknown category mask.
    assert "applicability" not in arrays
    assert "applicability" not in audit["diagnostic_pixels"]
    assert "applicability_coverage" not in audit


def test_window_sizes_preserve_counts_and_raster_values(tmp_path):
    cases = [mapping_case(tmp_path / str(size), size=size) for size in (1, 3, 256)]
    reference_arrays, reference_audit, _ = cases[0]
    for arrays, audit, _ in cases[1:]:
        assert audit == reference_audit
        for kind, expected in reference_arrays.items():
            np.testing.assert_array_equal(arrays[kind], expected)
    assert reference_audit["diagnostic_pixels"]["applicability"] == 4
    assert reference_audit["diagnostic_pixels"]["outside_applicability"] == 3


@pytest.mark.parametrize("geographic", [False, True])
def test_coverage_measure_and_report_preserve_area_limit(tmp_path, report_case, geographic):
    _, audit, rasters = mapping_case(tmp_path / "map", geographic=geographic)
    coverage = audit["applicability_coverage"]
    assert coverage["denominator"] == "valid_input_pixels_before_prediction_mask"
    assert coverage["valid_input_pixels"] == coverage["inside_pixels"] + coverage["outside_pixels"]
    if geographic:
        assert coverage["measure"] == "grid_cell_fraction"
        assert coverage["constant_cell_area_grid_units_squared"] is None
        assert "not a geographic area fraction" in coverage["measure_label"]
        assert "only the cell fraction" in coverage["area_limit"]
    else:
        assert coverage["measure"] == "projected_grid_plane_area_fraction"
        assert coverage["constant_cell_area_grid_units_squared"] == 918
        assert "Area fraction of valid cells in the grid plane" in coverage["measure_label"]
        assert "not to be interpreted as an area fraction on the surface of the Earth or on the ellipsoid" in coverage["area_limit"]
    evaluation, samples, settings = report_case
    evaluation["mapping_diagnostics"] = audit
    evaluation["final_record"]["applicability"] = {"enabled": True}
    output = tmp_path / "report"
    build_reports(output, evaluation, samples, settings, check_cancel=lambda: None)
    report = (output / "report.html").read_text(encoding="utf-8")
    assert "Cells outside the area of applicability</td><td>3</td>" in report
    assert coverage["measure_label"] in report
    assert coverage["scope_limit"] in report
    assert coverage["area_limit"] in report
    assert "Diagnostic rasters were not masked" in report
    saved = json.loads((output / "evaluation.json").read_text(encoding="utf-8"))
    assert saved["evaluation"]["mapping_diagnostics"]["applicability_coverage"] == coverage


def test_no_valid_inputs_leave_coverage_undefined(tmp_path):
    arrays, audit, _ = mapping_case(tmp_path, empty=True)
    coverage = audit["applicability_coverage"]
    assert coverage["status"] == "no_valid_input_pixels"
    assert coverage["inside_fraction"] is None and coverage["outside_fraction"] is None
    assert coverage["inside_pixels"] == coverage["outside_pixels"] == 0
    assert all(np.isnan(values).all() for values in arrays.values())
