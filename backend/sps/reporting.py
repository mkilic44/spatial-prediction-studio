"""Local, target-unit reports from already evaluated Stage B predictions.

Reports never refit, select models or alter observations. All finite plot rows
are retained; repeated predictions are distinct records, not new observations.
Nonfinite values become JSON null with an explicit path and reason.
"""
from __future__ import annotations

from collections import defaultdict
import csv
import html
import json
import math
from pathlib import Path
import shutil
import tempfile

import matplotlib
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.colors import TwoSlopeNorm
import numpy as np
from pyproj import CRS

from .contract import JobError


METRIC_LABELS = {
    "rmse": "root mean squared error (RMSE)",
    "mae": "mean absolute error (MAE)", "me": "mean error (ME)", "bias": "mean error (ME)", "mean_error": "mean error (ME)",
    "r2": "coefficient of determination (R²)", "coverage": "empirical coverage",
    "mean_width": "mean interval width", "median_width": "median interval width",
    "lower_miss_rate": "fraction below the lower bound",
    "upper_miss_rate": "fraction above the upper bound", "interval_score": "interval score",
    "below_lower_fraction": "fraction below the lower bound",
    "above_upper_fraction": "fraction above the upper bound", "nominal_coverage": "nominal coverage",
    "r2_reason": "status of the coefficient of determination",
    "crossing_count": "number of crossing quantiles",
    "rmse_defined_folds": "number of folds with a computed squared error metric",
    "mae_defined_folds": "number of folds with a computed absolute error metric",
    "mean_error_defined_folds": "number of folds with a computed mean error",
    "r2_defined_folds": "number of folds with a defined coefficient of determination",
    "repeat": "repeat", "fold": "fold",
    "pinball_lower": "lower pinball loss", "pinball_upper": "upper pinball loss",
    "n": "number of evaluation records", "n_unique": "number of unique observations",
    "available_subset": "Only observations with a computed interval",
    "available_n": "records with a computed interval", "unavailable_n": "records whose interval could not be computed",
    "available_fraction": "fraction of records with a computable interval",
    "coverage_denominator": "coverage denominator",
    "availability_denominator": "denominator of the fraction with a computable interval",
}
SCOPE_LABELS = {"pooled_oof": "Pooled out-of-fold evaluation", "per_repeat": "Per-repeat evaluation",
                "fold_mean": "Mean of fold metrics", "independent": "Locked independent test set"}
METHOD_LABELS = {"random": "random k-fold", "repeated": "repeated random k-fold", "group": "grouped k-fold",
                 "spatial_block": "spatial blocks", "buffered": "buffered spatial blocks", "custom": "user-defined folds",'knndm':'k-fold nearest neighbor distance matching (kNNDM)',
                 "none": "not applied", "split_conformal": "split conformal prediction", "quantile_forest": "quantile regression forest",
                 "quantile_boosting": "gradient boosting with quantile loss", "cqr": "conformalized quantile regression (CQR)",
                 "spatial_conformal": "spatially weighted conformal prediction (experimental)",
                 "temporal": "time-ordered validation that keeps groups separate"}
COLORS = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#000000"]
MARKERS = ["o", "s", "^", "D", "v", "P", "X"]
COVERAGE_SCOPES = {"pooled_oof": "Pooled out-of-fold", "per_repeat": "Repeat", "outer_folds": "Outer fold", "independent": "Locked test set"}


def _normal(value, path="", issues=None):
    if issues is None:
        issues = []
    if isinstance(value, np.ndarray):
        value = value.tolist()
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        reason = "The number is undefined." if math.isnan(value) else "The number is not finite."
        issues.append({"path": path, "reason": reason, "status": "undefined"})
        return None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        if any(not isinstance(key, (str, int)) for key in value):
            raise JobError("REPORT_SERIALIZATION", "Report field names must be text or integers.")
        return {str(key): _normal(item, path + "." + str(key) if path else str(key), issues)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normal(item, path + "[%d]" % i, issues) for i, item in enumerate(value)]
    raise JobError("REPORT_SERIALIZATION", "Unsupported report object: " + path)


def _number(value):
    try:
        return float(value) if value is not None and not isinstance(value, bool) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def _coverage_rows(evaluation):
    """Keep unavailable levels explicit; never substitute a smaller fold subset."""
    diagnostics = evaluation.get("coverage_diagnostics", {})
    if not isinstance(diagnostics, dict):
        raise JobError("REPORT_COVERAGE", "Coverage diagnostics must be an object.")
    result = []
    for scope in COVERAGE_SCOPES:
        for row in diagnostics.get(scope, []):
            if not isinstance(row, dict) or row.get("status") not in ("available", "partial", "unavailable"):
                raise JobError("REPORT_COVERAGE", "The computation status of each coverage level must be explicit.")
            nominal = _number(row.get("nominal_coverage"))
            if not math.isfinite(nominal) or not 0 < nominal < 1:
                raise JobError("REPORT_COVERAGE", "The nominal coverage level must be between 0 and 1.")
            if row["status"] == "available":
                actual = _number(row.get("coverage"))
                if not math.isfinite(actual) or not 0 <= actual <= 1:
                    raise JobError("REPORT_COVERAGE", "Empirical coverage must be between 0 and 1.")
                if not _number(row.get("n")) > 0 or not 0 < _number(row.get("group_count")) <= _number(row.get("n")):
                    raise JobError("REPORT_COVERAGE", "The coverage table has invalid observation or group counts.")
            elif not row.get("reason"):
                raise JobError("REPORT_COVERAGE", "A coverage level that could not be computed requires an explanation.")
            if row["status"] == "partial":
                n, available, unavailable, groups, available_groups = [
                    _number(row.get(key)) for key in ("n", "available_n", "unavailable_n", "group_count", "available_group_count")]
                subset = row.get("available_subset")
                actual = _number(subset.get("coverage")) if isinstance(subset, dict) else float("nan")
                counts = (n, available, unavailable, groups, available_groups)
                valid = (all(math.isfinite(v) and v.is_integer() for v in counts)
                         and 0 < available < n and available + unavailable == n
                         and 0 < available_groups <= min(available, groups) and groups <= n
                         and math.isclose(_number(row.get("available_fraction")), available / n, abs_tol=1e-12)
                         and math.isfinite(actual) and 0 <= actual <= 1
                         and "coverage" not in row and bool(row.get("metrics_scope")))
                for key, expected in (("coverage_denominator", available), ("availability_denominator", n)):
                    if key in row and _number(row[key]) != expected:
                        valid = False
                if isinstance(subset, dict):
                    for key in ("mean_width", "median_width", "pinball_lower", "pinball_upper", "interval_score"):
                        if key in subset and not (math.isfinite(_number(subset[key])) and _number(subset[key]) >= 0):
                            valid = False
                if not valid:
                    raise JobError("REPORT_COVERAGE", "The denominator, counts or metrics of partial interval coverage are inconsistent.")
            result.append({**row, "scope": scope})
    return result


def _validate(evaluation, samples, settings):
    _coverage_rows(evaluation)
    ids = samples.get("ids", [])
    coordinates = np.asarray(samples.get("coordinates"), dtype=float)
    if len(ids) != len(set(ids)) or coordinates.shape != (len(ids), 2) or not np.isfinite(coordinates).all():
        raise JobError("REPORT_SAMPLES", "Report observations and coordinates are inconsistent.")
    known = set(ids)
    independent = set(settings.get("validation", {}).get("independent_ids", []))
    groups = {}
    role_groups = {}
    for source in ("oof", "independent_predictions"):
        rows = evaluation.get(source, [])
        seen = set()
        for row in rows:
            if row.get("sample_id") not in known:
                raise JobError("REPORT_SAMPLE_ID", "The report contains an observation ID that is not in the data.")
            if (source == "oof" and row["sample_id"] in independent) or (source == "independent_predictions" and row["sample_id"] not in independent):
                raise JobError("REPORT_INDEPENDENCE", "The prediction role does not match the locked independent test set IDs.")
            key = (row.get("repeat", 0), row["sample_id"])
            if key in seen:
                raise JobError("REPORT_DUPLICATE", "An observation has more than one test prediction in the same repeat.")
            seen.add(key)
            observed, predicted, residual = [_number(row.get(name)) for name in ("observed", "predicted", "residual")]
            if all(math.isfinite(value) for value in (observed, predicted, residual)) and not math.isclose(
                    residual, predicted - observed, rel_tol=1e-9, abs_tol=1e-10):
                raise JobError("REPORT_RESIDUAL", "The residual sign or value is inconsistent; the residual must be prediction minus observation.")
            if "lower" in row and "upper" in row:
                lower, upper = _number(row["lower"]), _number(row["upper"])
                if settings.get('uncertainty',{}).get('method')=='spatial_conformal':
                    status=row.get('interval_status')
                    valid=(status=='available' and math.isfinite(lower) and math.isfinite(upper)) or (
                        status=='unavailable' and row['lower'] is None and row['upper'] is None
                        and row.get('interval_width') is None and bool(row.get('reason_code')))
                    if not valid:raise JobError('REPORT_INTERVAL','The bounds, status or reason code of the local interval are inconsistent.')
                if math.isfinite(lower) and math.isfinite(upper):
                    if lower > upper or ("interval_width" in row and not math.isclose(
                            _number(row["interval_width"]), upper - lower, rel_tol=1e-9, abs_tol=1e-10)):
                        raise JobError("REPORT_INTERVAL", "Interval bounds or width are inconsistent.")
            group = row.get("group_id")
            if group is not None:
                if row["sample_id"] in groups and groups[row["sample_id"]] != str(group):
                    raise JobError("REPORT_GROUP", "The group ID of an observation differs between results.")
                groups[row["sample_id"]] = str(group)
        role_groups[source] = {str(row["group_id"]) for row in rows if row.get("group_id") is not None}
    if role_groups["oof"] & role_groups["independent_predictions"]:
        raise JobError("REPORT_INDEPENDENCE", "An independent test group was used in the out-of-fold evaluation.")
    for fold in evaluation.get("folds", []):
        train, test = set(fold["train_ids"]), set(fold["test_ids"])
        if train & test or (train | test) - known or (train | test) & independent:
            raise JobError("REPORT_FOLD", "The report folds violate an ID or independent test boundary.")
        if {groups[i] for i in train if i in groups} & {groups[i] for i in test if i in groups}:
            raise JobError("REPORT_GROUP", "Training and test groups overlap in the report folds.")
    for scope, source in [("pooled_oof", "oof"), ("independent", "independent_predictions")]:
        metric = evaluation.get("metrics", {}).get(scope, {})
        if not isinstance(metric, dict):
            continue
        values = [_number(row.get("observed")) for row in evaluation.get(source, [])]
        values = [value for value in values if math.isfinite(value)]
        if (len(values) < 2 or (values and max(values) == min(values))) and math.isfinite(_number(metric.get("r2"))):
            raise JobError("REPORT_R2", "A finite coefficient of determination cannot be reported for a single observation or a constant target.")
    return groups


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r", "\n")):
        return "'" + value
    return value


def _write_csv(path, rows, default_columns=()):
    if any(not isinstance(row, dict) for row in rows):
        raise JobError("REPORT_TABLE", "Each row of a report table must be an object.")
    columns = list(default_columns)
    for row in rows:
        columns.extend(key for key in row if key not in columns)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows({key: _csv_cell(value) for key, value in row.items()} for row in rows)


def _flatten(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _flatten(item, prefix + "." + str(key) if prefix else str(key))
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _flatten(item, prefix + "[%d]" % i)
    else:
        yield {"metric": prefix, "value": value,
               "label": METRIC_LABELS.get(prefix.rsplit(".", 1)[-1], prefix.rsplit(".", 1)[-1])}


def _counts(evaluation, samples, groups):
    rows = evaluation.get("oof", [])
    independent = evaluation.get("independent_predictions", [])
    return {"input_samples": len(samples["ids"]), "oof_rows": len(rows),
            "oof_unique_samples": len({row["sample_id"] for row in rows}),
            "oof_repeats": len({row.get("repeat", 0) for row in rows}),
            "independent_rows": len(independent),
            "independent_unique_samples": len({row["sample_id"] for row in independent}),
            "reported_unique_groups": len(set(groups.values())),
            "samples_with_reported_group": len(groups)}


def _plot_base(title, xlabel, ylabel):
    figure = Figure(figsize=(6.4, 4.8), layout="constrained")
    FigureCanvasAgg(figure)
    axis = figure.add_subplot(111)
    axis.set(title=title, xlabel=xlabel, ylabel=ylabel)
    axis.grid(alpha=0.18, linewidth=0.6)
    return figure, axis


def _save_figure(figure, folder, name, dpi, record, check_cancel):
    try:
        for extension in ("svg", "png"):
            check_cancel()
            path = folder / (name + "." + extension)
            metadata = {"Date": None} if extension == "svg" else {"Software": "Spatial Prediction Studio"}
            figure.savefig(path, format=extension, dpi=dpi, facecolor="white", transparent=False, metadata=metadata)
            if not path.is_file() or path.stat().st_size == 0:
                raise JobError("REPORT_FIGURE", "The figure file could not be created.")
    finally:
        figure.clear()
        # Release the large Agg raster buffer between 900 dpi exports.
        figure.set_canvas(None)
    record.update({"files": ["figures/" + name + ".svg", "figures/" + name + ".png"],
                   "dpi": dpi, "width_inches": 6.4, "height_inches": 4.8,
                   "connected_observations": False})
    return record


def _plots(folder, evaluation, samples, unit, dpi, check_cancel):
    folder.mkdir()
    manifest = []
    reused = evaluation.get("independent_test_history", {}).get("settings_changed_since_previous", False)
    for source, key, label in [("oof", "oof", "Out-of-fold"), ("independent", "independent_predictions", "Reused test set" if reused else "Independent test")]:
        rows = evaluation.get(key, [])
        if not rows:
            continue
        observed = np.asarray([_number(row.get("observed")) for row in rows])
        predicted = np.asarray([_number(row.get("predicted")) for row in rows])
        residual = np.asarray([_number(row.get("residual")) for row in rows])
        finite_pair = np.isfinite(observed) & np.isfinite(predicted)
        for kind in ("observed_predicted", "residual_predicted", "residual_histogram"):
            if kind == "observed_predicted":
                finite = finite_pair
                figure, axis = _plot_base(label + ": observed and predicted", "Observed (" + unit + ")", "Predicted (" + unit + ")")
                axis.scatter(observed[finite], predicted[finite], s=26, alpha=0.8, color=COLORS[0], edgecolors="white", linewidths=0.4)
                if finite.any():
                    lower = min(observed[finite].min(), predicted[finite].min())
                    upper = max(observed[finite].max(), predicted[finite].max())
                    pad = (upper - lower) * .05 if upper > lower else max(abs(lower) * .05, 1.0)
                    limits = [lower - pad, upper + pad]
                    axis.plot(limits, limits, color="#333333", linestyle="--", linewidth=1, label="1:1 line")
                    axis.set_xlim(limits); axis.set_ylim(limits); axis.set_aspect("equal", adjustable="box")
                    axis.legend(loc="best")
            elif kind == "residual_predicted":
                finite = np.isfinite(predicted) & np.isfinite(residual)
                figure, axis = _plot_base(label + ": residuals", "Predicted (" + unit + ")", "Predicted - observed (" + unit + ")")
                axis.scatter(predicted[finite], residual[finite], s=26, alpha=.8, color=COLORS[1], edgecolors="white", linewidths=.4)
                axis.axhline(0, color="#333333", linestyle="--", linewidth=1)
            else:
                finite = np.isfinite(residual)
                figure, axis = _plot_base(label + ": residual distribution", "Predicted - observed (" + unit + ")", "Number of prediction records")
                bins = np.histogram_bin_edges(residual[finite], bins="fd") if finite.any() else np.asarray([0, 1])
                axis.hist(residual[finite], bins=bins, color=COLORS[0], edgecolor="white")
            omitted = int((~finite).sum())
            axis.text(.02, .98, "Shown: %d/%d records" % (finite.sum(), len(rows)), transform=axis.transAxes, va="top", fontsize=8,
                      bbox={"facecolor": "white", "edgecolor": "none", "alpha": .85})
            record = {"kind": kind, "source": source, "source_rows": len(rows), "plotted_rows": int(finite.sum()),
                      "nonfinite_rows": omitted, "unit": unit,
                      "caption": label + (". Histogram bars show the number of prediction records in each bin. " if kind == "residual_histogram" else ". Each point is one evaluation record. ") + "The residual is prediction minus observation. "
                                 + ("Nonfinite records not plotted: %d; they are kept in the source table." % omitted if omitted else "All finite records are shown.")}
            if kind == "residual_histogram":
                record["bin_edges"] = bins.tolist()
                record["bin_rule"] = "Freedman-Diaconis; left closed, last bin includes right edge"
            manifest.append(_save_figure(figure, folder, source + "_" + kind, dpi, record, check_cancel))
    coordinates = np.asarray(samples["coordinates"], dtype=float)
    locations = dict(zip(samples["ids"], coordinates))
    crs = CRS.from_user_input(samples["crs"])
    coord_unit = crs.axis_info[0].unit_name if crs.axis_info else "coordinate unit"
    coord_unit = {"metre": "m", "meter": "m", "foot": "ft", "US survey foot": "US survey ft"}.get(coord_unit, coord_unit)
    xlabel, ylabel = ("Longitude (degrees)", "Latitude (degrees)") if crs.is_geographic else ("X (" + coord_unit + ")", "Y (" + coord_unit + ")")
    repeats = sorted({row.get("repeat", 0) for row in evaluation.get("oof", [])} | {fold["repeat"] for fold in evaluation.get("folds", [])})
    finite_residuals = [_number(row.get("residual")) for row in evaluation.get("oof", [])]
    limit = max([abs(value) for value in finite_residuals if math.isfinite(value)] + [1e-12])
    for repeat in repeats:
        folds = sorted((fold for fold in evaluation.get("folds", []) if fold["repeat"] == repeat), key=lambda fold: fold["fold"])
        figure, axis = _plot_base("Test folds: repeat %d" % (repeat + 1), xlabel, ylabel)
        total = 0
        for index, fold in enumerate(folds):
            points = np.asarray([locations[identity] for identity in fold["test_ids"]])
            if not len(points):
                continue
            total += len(points)
            axis.scatter(points[:, 0], points[:, 1], color=COLORS[index % len(COLORS)],
                         marker=MARKERS[index % len(MARKERS)], s=28,
                         label="Fold %d" % (fold["fold"] + 1), edgecolors="#333333", linewidths=.35)
        if total:
            axis.legend(loc="best", ncols=2 if len(folds) > 5 else 1, fontsize=8)
        axis.ticklabel_format(useOffset=False, style="plain")
        axis.set_aspect("equal", adjustable="datalim")
        manifest.append(_save_figure(figure, folder, "folds_repeat_%d" % repeat, dpi,
                        {"kind": "fold_map", "source": "folds", "repeat": repeat, "source_rows": total,
                         "plotted_rows": total, "nonfinite_rows": 0, "crs": crs.to_string(),
                         "caption": "Test folds in repeat %d. Symbol and color identify the fold. Training membership is in the fold table." % (repeat + 1)}, check_cancel))
        rows = [row for row in evaluation.get("oof", []) if row.get("repeat", 0) == repeat]
        residual = np.asarray([_number(row.get("residual")) for row in rows])
        finite = np.isfinite(residual)
        points = np.asarray([locations[row["sample_id"]] for row in rows])
        figure, axis = _plot_base("Spatial residuals: repeat %d" % (repeat + 1), xlabel, ylabel)
        if finite.any():
            for mask, marker in [(finite & (residual < 0), "v"), (finite & (residual > 0), "^"), (finite & (residual == 0), "o")]:
                if not mask.any():
                    continue
                scatter = axis.scatter(points[mask, 0], points[mask, 1], c=residual[mask], s=40, marker=marker,
                                       cmap="RdBu_r", norm=TwoSlopeNorm(vmin=-limit, vcenter=0, vmax=limit),
                                       edgecolors="#333333", linewidths=.4)
            figure.colorbar(scatter, ax=axis, label="Predicted - observed (" + unit + ")")
        axis.ticklabel_format(useOffset=False, style="plain")
        axis.set_aspect("equal", adjustable="datalim")
        manifest.append(_save_figure(figure, folder, "residual_map_repeat_%d" % repeat, dpi,
                        {"kind": "residual_map", "source": "oof", "repeat": repeat, "source_rows": len(rows),
                         "plotted_rows": int(finite.sum()), "nonfinite_rows": int((~finite).sum()),
                         "crs": crs.to_string(), "color_limits": [-limit, limit], "unit": unit,
                         "marker_encoding": "negative=triangle_down; positive=triangle_up; zero=circle",
                         "caption": "Out-of-fold residuals for repeat %d. The color scale is centered on zero and is the same in all repeats. A downward triangle shows a negative residual, an upward triangle a positive residual and a circle a zero residual. No interpolation was done between points." % (repeat + 1)}, check_cancel))
    return manifest


def _coverage_plot(folder, evaluation, dpi, check_cancel):
    rows = [row for row in _coverage_rows(evaluation) if row["scope"] in ("pooled_oof", "independent")]
    available = [row for row in rows if row["status"] == "available"]
    if not available:
        return []
    figure, axis = _plot_base("Nominal and empirical coverage", "Nominal coverage", "Empirical coverage")
    reused = evaluation.get("independent_test_history", {}).get("settings_changed_since_previous", False)
    for index, scope in enumerate(("pooled_oof", "independent")):
        values = [row for row in available if row["scope"] == scope]
        if values:
            label = "Reused test set" if scope == "independent" and reused else COVERAGE_SCOPES[scope]
            axis.scatter([row["nominal_coverage"] for row in values], [row["coverage"] for row in values],
                         s=50, color=COLORS[index], marker=MARKERS[index], edgecolors="#333333", linewidths=.4, label=label)
    axis.plot([0, 1], [0, 1], linestyle="--", color="#333333", linewidth=1, label="Nominal = empirical")
    axis.set_xlim(-.02, 1.02); axis.set_ylim(-.02, 1.02)
    axis.set_aspect("equal", adjustable="box"); axis.legend(loc="lower right", fontsize=8)
    caption = "Each symbol is the outer evaluation of the fixed fitted models at one coverage level. Levels that could not be computed are not plotted; their reasons are kept in the table. Repeated records are not counted as independent replicates."
    record = {"kind": "coverage_calibration", "source": "coverage_diagnostics", "source_rows": len(rows),
              "plotted_rows": len(available), "unavailable_rows": len(rows) - len(available), "nonfinite_rows": 0,
              "values": available, "caption": caption}
    return [_save_figure(figure, folder, "coverage_calibration", dpi, record, check_cancel)]


def _display(value):
    if value is None:
        return "Undefined or not computed"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return format(value, ".8g")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _table(headers, rows):
    return "<table><thead><tr>" + "".join("<th>" + html.escape(str(value)) + "</th>" for value in headers) + "</tr></thead><tbody>" + "".join(
        "<tr>" + "".join("<td>" + html.escape(_display(value)) + "</td>" for value in row) + "</tr>" for row in rows) + "</tbody></table>"


def _metric_rows(value, prefix=""):
    """Readable metric scope labels; exact field paths remain in metrics.csv."""
    if isinstance(value, list):
        for index, record in enumerate(value):
            label = "Repeat %d" % (record.get("repeat", index) + 1) if isinstance(record, dict) else "Record %d" % (index + 1)
            yield from _metric_rows(record, label + ": ")
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "repeat" and prefix:
                continue
            if isinstance(item, (dict, list)):
                label = "Prediction interval" if key == "interval" else ("" if key == "metrics" else METRIC_LABELS.get(key, key))
                yield from _metric_rows(item, prefix + (label + ": " if label else ""))
            else:
                if key == "r2_reason":
                    item = {"constant_observed_target": "The observed target is constant.", "fewer_than_two_observations": "There are fewer than two observations."}.get(item, item)
                    if item is None and math.isfinite(_number(value.get("r2"))):
                        item = "Defined"
                yield prefix + METRIC_LABELS.get(key, key), item
    else:
        yield prefix or "Evaluation", value


def _training_rows(evaluation):
    rows = []
    for fold in evaluation.get("fold_metrics", []):
        metrics = fold.get("model_record", {}).get("train_metrics")
        if metrics:
            rows.append({"scope": "outer_model_training", "repeat": fold["repeat"], "fold": fold["fold"], **metrics})
    final = evaluation.get("final_record", {}).get("train_metrics")
    if final:
        rows.append({"scope": "final_model_training", "repeat": None, "fold": None, **final})
    return rows


def _role_tables(evaluation):
    training = _training_rows(evaluation)
    pieces = ['<h3>Training results</h3><p>The model reapplied to its training data is shown separately here. These results are not validation performance.</p>',
              _table(["Model", "Repeat", "Fold", "Observations", METRIC_LABELS["rmse"], METRIC_LABELS["mae"], "Mean error (ME)", "Coefficient of determination (R²)"],
                     [("Final model" if row["scope"] == "final_model_training" else "Outer-fold model",
                       "" if row["repeat"] is None else row["repeat"] + 1, "" if row["fold"] is None else row["fold"] + 1,
                       row.get("n"), row.get("rmse"), row.get("mae"), row.get("mean_error"), row.get("r2")) for row in training]),
              '<h3>Inner validation trials</h3><p>Model and setting selection is based on the inner evaluation. The duration below is the actual duration of each trial.</p>',
              _table(["Evaluation", "Trial", "Model", "Variable selection", "Multicollinearity check", "Metric", "Inner loss", "Status", "Duration (s)", "Selected"],
                     [(row.get("role"), int(row.get("trial", 0)) + 1, row.get("model", {}).get("id", "not specified"),
                       "No selection" if row.get("feature_selection_method") == "none" else row.get("feature_selection_method", "not specified"),
                       {"off": "Off", "diagnostic": "Diagnostic only", "iterative": "Iterative elimination"}.get(row.get("vif_mode"), "not specified"),
                       METRIC_LABELS.get(row.get("metric"), row.get("metric")), row.get("value"),
                       {"complete": "Completed", "failed": "Failed"}.get(row.get("status"), row.get("status", "not specified")),
                       row.get("elapsed_seconds"), "Yes" if row.get("selected") else "No") for row in evaluation.get("trials", [])]),
              '<h3>Outer-fold results</h3>',
              _table(["Repeat", "Fold", "Observations", METRIC_LABELS["rmse"], METRIC_LABELS["mae"], "Mean error (ME)", "Coefficient of determination (R²)"],
                     [(row["repeat"] + 1, row["fold"] + 1, row.get("metrics", row).get("n"), row.get("metrics", row).get("rmse"),
                       row.get("metrics", row).get("mae"), row.get("metrics", row).get("mean_error"), row.get("metrics", row).get("r2"))
                      for row in evaluation.get("fold_metrics", [])])]
    if any(row.get("comparison_scope") == "selector_and_vif_elimination" for row in evaluation.get("trials", [])):
        pieces.append('<p>In the comparison without selection, variable selection and elimination based on the variance inflation factor (VIF) are turned off. The multicollinearity check produces diagnostics only.</p>')
    searches = defaultdict(list)
    for row in evaluation.get("trials", []):
        searches[row.get("role", "not specified")].append(row)
    if searches:
        summary_rows = []
        for role, rows in sorted(searches.items()):
            recorded = rows[-1].get("search_summary", {})
            elapsed = recorded.get("elapsed_seconds", max((_number(row.get("search_elapsed_seconds")) for row in rows
                if math.isfinite(_number(row.get("search_elapsed_seconds")))), default=None))
            reason = {"timeout_between_trials": "Time limit reached between trials", "candidate_or_trial_budget_exhausted": "Candidate list or trial budget exhausted"}.get(
                recorded.get("stop_reason"), "Stop reason not recorded")
            summary_rows.append((role, len(rows), sum(row.get("status") == "failed" for row in rows), elapsed, reason))
        pieces.extend(['<h3>Search budget used</h3>',
            _table(["Search", "Recorded trials", "Failed trials", "Total elapsed time (s)", "Stop reason"], summary_rows),
            '<p>Durations of parallel trials are not summed as total run time. The time limit is applied between trials; a running estimator fit is not interrupted.</p>'])
    return pieces


def _preflight_tables(preflight, settings):
    if not preflight:
        return []
    folds = preflight.get("folds", [])
    summary, conditions, categories = [], [], []
    for fold in folds:
        diagnostics = fold.get("diagnostics", {})
        distance = diagnostics.get("test_to_train_distance_m") or {}
        summary.append((fold["repeat"] + 1, fold["fold"] + 1, len(fold["train_ids"]), len(fold["test_ids"]),
                        diagnostics.get("train_groups"), diagnostics.get("test_groups"),
                        distance.get("min", diagnostics.get("minimum_train_test_distance_m")), distance.get("median"), distance.get("max")))
        for item in fold.get("environmental_conditions", []):
            if "train_categories" in item or "test_categories" in item:
                categories.append((fold["repeat"] + 1, fold["fold"] + 1, item.get("id"), item.get("train_categories"),
                                   item.get("test_categories"), item.get("unknown_test_categories")))
            else:
                conditions.append((fold["repeat"] + 1, fold["fold"] + 1, item.get("id"), item.get("train_min"), item.get("train_max"),
                                   item.get("test_min"), item.get("test_max")))
    pieces = ['<h2>Fold data and distance summary</h2>',
              _table(["Repeat", "Fold", "Training observations", "Test observations", "Training groups", "Test groups", "Minimum distance (m)", "Median distance (m)", "Maximum distance (m)"], summary),
              '<p>Distances are from each test observation to its nearest training observation. An undefined distance does not mean zero.</p>',
              '<h3>Environmental conditions</h3>',
              _table(["Repeat", "Fold", "Variable", "Training minimum", "Training maximum", "Test minimum", "Test maximum"], conditions)]
    if categories:
        pieces.extend(['<h3>Categorical environmental conditions</h3>',
                       _table(["Repeat", "Fold", "Variable", "Training categories", "Test categories", "New categories in test"], categories)])
    missing = preflight.get("missing_fraction", [])
    covariates = settings.get("covariates", [])
    if missing:
        pieces.extend(['<h3>Fraction of missing values in environmental covariates</h3>',
                       _table(["Variable", "Missing fraction"], [(covariates[i].get("id") if i < len(covariates) else "Variable %d" % (i + 1), value)
                                                            for i, value in enumerate(missing)])])
    for fold in folds:
        warning = fold.get("diagnostics", {}).get("distance_warning") or fold.get("diagnostics", {}).get("warning")
        if warning:
            pieces.append('<p class="note">' + html.escape(str(warning)) + '</p>')
    for warning in preflight.get("warnings", []):
        pieces.append('<p class="note">' + html.escape(str(warning)) + '</p>')
    workload = preflight.get("workload", {})
    if workload:
        labels = {"candidate_arms": "Candidate model families", "trial_budget": "Trial budget", "inner_folds": "Inner folds",
                  "outer_folds": "Outer folds", "estimated_pipeline_fits_upper_bound": "Estimated upper bound of pipeline fits",
                  "extra_selector_fits_not_included": "Additional selector fits not included in the count", "threads_per_fit": "Threads per fit"}
        pieces.extend(['<h3>Estimated workload</h3>', _table(["Component", "Estimate"], [(labels.get(key, key), value) for key, value in workload.items()]),
                       '<p>These numbers are a workload estimate computed before training. The actual number of trials and the durations are given in a separate table.</p>'])
    return pieces


def _coverage_tables(evaluation):
    rows = _coverage_rows(evaluation)
    if not rows:
        return []
    def status(row):
        if row["status"] == "unavailable":
            return "Could not be computed: " + str(row["reason"])
        if row["status"] == "partial":
            return "Interval computed for only some observations: " + str(row["reason"])
        return "Insufficient empirical coverage" if row["coverage"] < row["nominal_coverage"] else "Nominal level reached (empirical)"
    pieces = ['<h3>Coverage at multiple levels</h3>',
              '<p>The same fitted models were evaluated at different coverage levels. Calibration observations are not included in this evaluation table. Levels that could not be computed are not filled with zero or with results from suitable folds only.</p>',
              _table(["Scope", "Repeat", "Fold", "Nominal coverage", "Empirical coverage", "Records", "Groups", "Status"],
                     [(COVERAGE_SCOPES[row["scope"]], row["repeat"] + 1 if row.get("repeat") is not None else "",
                       row["fold"] + 1 if row.get("fold") is not None else "", row["nominal_coverage"],
                       row.get("coverage") if row["status"] == "available" else None, row.get("n"), row.get("group_count"), status(row)) for row in rows]),
              '<p>Insufficient empirical coverage means that the observed fraction is below the nominal level. This flag is not by itself a statistical significance test.</p>',
              '<h3>Interval width and exceedance diagnostics</h3>',
              _table(["Scope", "Nominal coverage", "Mean width", "Median width", "Below lower bound", "Above upper bound", "Lower pinball loss", "Upper pinball loss", "Interval score"],
                     [(COVERAGE_SCOPES[row["scope"]], row["nominal_coverage"], row.get("mean_width"), row.get("median_width"),
                       row.get("below_lower_fraction"), row.get("above_upper_fraction"), row.get("pinball_lower"), row.get("pinball_upper"), row.get("interval_score"))
                      for row in rows if row["status"] == "available"])]
    partial = [row for row in rows if row["status"] == "partial"]
    if partial:
        pieces.extend(['<h3>Only observations with a computed interval</h3>',
                       '<p class="note">The denominator of the coverage below is only the records with a computed interval. Observations without an interval are not included in this fraction. The result is not the coverage of all observations.</p>',
                       _table(["Scope", "Repeat", "Fold", "Nominal coverage", "Total records", "With interval", "Without interval", "Fraction with computable interval", "Coverage in subset", "Mean width in subset", "Interval score in subset"],
                              [(COVERAGE_SCOPES[r["scope"]], r.get("repeat"), r.get("fold"), r["nominal_coverage"], r["n"],
                                r["available_n"], r["unavailable_n"], r["available_fraction"], r["available_subset"]["coverage"],
                                r["available_subset"].get("mean_width"), r["available_subset"].get("interval_score")) for r in partial])])
    return pieces


def _html(evaluation, samples, settings, counts, figures, issues, study_section=""):
    unit = str(settings.get("samples", {}).get("unit", "unit not specified"))
    target = str(settings.get("samples", {}).get("target_field", "Target"))
    validation = settings.get("validation", {})
    uncertainty = settings.get("uncertainty", {})
    history = evaluation.get("independent_test_history", {})
    reused = history.get("settings_changed_since_previous", False)
    pieces = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
              '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' data:; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
              '<title>Spatial prediction evaluation</title><style>body{font-family:Segoe UI,Arial,sans-serif;color:#18222c;background:#fff;max-width:1100px;margin:2rem auto;padding:0 1rem;line-height:1.55}h1,h2{line-height:1.25}table{border-collapse:collapse;width:100%;margin:1rem 0}th,td{border:1px solid #cad1d7;padding:.45rem;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#eef2f5}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f7;padding:1rem}figure{margin:2rem 0}img{max-width:100%;height:auto}.note{border-left:4px solid #006a99;padding:.6rem 1rem;background:#eef7fc}a{color:#005983}code{overflow-wrap:anywhere}</style></head><body>',
              '<h1>Spatial prediction evaluation</h1>',
              '<p>Target: <strong>' + html.escape(target) + '</strong>. Unit: ' + html.escape(unit) + '.</p>',
              '<p>Run ID: ' + html.escape(str(settings.get("run_id", "not specified"))) + '.</p>',
              '<h2>Data and evaluation scope</h2>',
              _table(["Scope", "Count"], [("Input observations", counts["input_samples"]), ("Unique out-of-fold observations", counts["oof_unique_samples"]),
                      ("Out-of-fold prediction records", counts["oof_rows"]), ("Evaluated repeats", counts["oof_repeats"]),
                      ("Unique independent test observations", counts["independent_unique_samples"]),
                      ("Distinct groups reported in the results", counts["reported_unique_groups"])]),
              '<p class="note">Repeats do not count as independent samples. The group count is based only on the selected dependence fields; it does not by itself prove spatial independence.</p>',
              '<p>Sample support: ' + html.escape({"point": "point", "composite": "composite sample", "parcel_mean": "parcel mean"}.get(samples.get("support"), "not specified")) + '. Depth: ' + html.escape(_display(samples.get("depth"))) +
              '. Laboratory method: ' + html.escape(_display(samples.get("laboratory_method"))) + '.</p>',
              '<h2>Accuracy metrics</h2>',
              '<p>The mean error (ME) and the residual in the figures are computed as prediction minus observation. The coefficient of determination (R²) can be negative. It is undefined for a constant target or a single observation.</p>',
              '<p>Error metrics are in the original unit of the target. Pooled out-of-fold metrics are reported separately from the mean of fold metrics.</p>']
    for scope, metrics in evaluation.get("metrics", {}).items():
        if scope not in SCOPE_LABELS:
            continue
        title = "Reused test set" if scope == "independent" and reused else SCOPE_LABELS.get(scope, scope)
        pieces.append("<h3>" + html.escape(title) + "</h3>")
        pieces.append(_table(["Metric or record", "Value"], _metric_rows(metrics)))
    pieces.extend(_role_tables(evaluation))
    pieces.extend(_preflight_tables(evaluation.get("preflight", {}), settings))
    if history:
        pieces.append("<h3>Use history of the locked test set</h3>")
        pieces.append(_table(["Record", "Value"], [("Number of previous evaluations", history.get("previous_evaluations", 0)),
                             ("Settings changed since previous use", "Yes" if reused else "No")]))
        if history.get("warning"):
            pieces.append('<p class="note">' + html.escape(str(history["warning"])) + '</p>')
        if reused:
            pieces.append('<p class="note">This test data was reused after the settings were changed. The result cannot be interpreted as a new, independent final performance measurement.</p>')
    if not counts["independent_unique_samples"]:
        pieces.append('<p>There is no independent test result. The out-of-fold evaluation is not presented as a test on independent external data.</p>')
    pieces.extend(['<h2>Validation and uncertainty</h2>',
                   '<p>Validation method: ' + html.escape(METHOD_LABELS.get(validation.get("method"), str(validation.get("method", "not specified")))) +
                   '. Number of folds: ' + html.escape(str(validation.get("n_splits", "not specified"))) +
                   '. Random seed: ' + html.escape(str(validation.get("seed", "not specified"))) + '.</p>',
                   '<p>Uncertainty method: ' + html.escape(METHOD_LABELS.get(uncertainty.get("method", "none"), str(uncertainty.get("method")))) +
                   '. Nominal coverage: ' + html.escape(_display(uncertainty.get("coverage"))) + '.</p>'])
    if uncertainty.get("method", "none") == "none":
        pieces.append('<p>No prediction interval was produced. An accuracy metric of the point prediction is not cell-specific uncertainty.</p>')
    else:
        pieces.append('<p class="note">Nominal coverage is not to be interpreted as an exact coverage guarantee for spatial or grouped data. Coverage and interval width must be read together in the outer evaluation.</p>')
        pieces.append('<p>Development, calibration and test IDs are kept separate in the record files. Calibration targets must not be used for model selection or for training the underlying model.</p>')
        pieces.append('<p>Overall coverage pools all evaluation records. Conditional coverage is the coverage within specific environmental conditions. Selected subgroups are evaluated separately; coverage is not computed if there are not enough observations or intervals.</p>')
        pieces.append('<p>A narrow interval is not by itself a good result. The prediction interval map cannot be interpreted as a product with proven accuracy in every cell.</p>')
    pieces.extend(_coverage_tables(evaluation))
    if validation.get('method')=='temporal':
        pieces.append('<h3>Time order and evaluation limits</h3><p>The initial training history is not tested in the outer folds. Pooled accuracy is computed only from observations that were actually tested. The same profile and overlapping date periods are not split.</p>')
        pieces.append('<p class="note">The test covers new profiles or parcels at later dates. Spatial transfer and change over time are not measured separately. Static covariate rasters do not validate a map for a specific future date.</p>')
        pieces.append(_table(['Fold','Training start','Training end','Test start','Test end','Test duration (calendar days)','Gap days'],
            [(f['fold']+1,*[f.get('diagnostics',{}).get('temporal',{}).get(k) for k in ('train_start','train_end','test_start','test_end','test_calendar_days','gap_days')]) for f in evaluation.get('folds',[])]))
        temporal=evaluation.get('final_record',{}).get('temporal',{})
        if temporal:
            pieces.append(_table(['Time record of the final model','Value'],[
                ('Time zone',temporal.get('timezone')),('Last date used',temporal.get('last_used_date')),
                ('Development dates',temporal.get('development_dates')),('Calibration dates',temporal.get('calibration_dates')),
                ('Requested calibration group fraction',temporal.get('requested_calibration_group_fraction')),
                ('Actual calibration group fraction',temporal.get('actual_calibration_group_fraction')),
                ('Actual calibration observation fraction',temporal.get('actual_calibration_sample_fraction'))]))
            pieces.append('<p>The denominator of the calibration fractions is the model-building subset before the calendar and spatial gaps are applied. Because periods are not split, the actual fraction can differ.</p>')
    if uncertainty.get("method") == "spatial_conformal":
        pieces.append('<p class="note">This experimental adaptation weights separate calibration residuals by distance. The bandwidth is fixed before evaluation; it is not selected with outer test targets. It gives no spatial coverage guarantee.</p>')
        pieces.append(_table(["Spatial calibration setting", "Value"], [
            ("Bandwidth (meters)", uncertainty.get("spatial_bandwidth_m")),
            ("Weight function", uncertainty.get("spatial_kernel")),
            ("Minimum effective number of observations", uncertainty.get("spatial_min_effective_n")),
            ("Minimum effective number of groups", uncertainty.get("spatial_min_groups"))]))
    interval_support = evaluation.get("mapping_diagnostics", {}).get("interval_support")
    if interval_support:
        pieces.append('<h3>Local calibration support on the map</h3>')
        pieces.append(_table(["Scope", "Value"], [
            ("Valid input cells", interval_support["valid_input_pixels"]),
            ("Cells with a computed interval", interval_support["available_input_pixels"]),
            ("Cells without local support", interval_support["unavailable_input_pixels"]),
            ("Fraction of cells with a computable interval", interval_support["available_fraction"])]))
        pieces.append('<p>The denominator is the valid input cells before the prediction mask is applied. This fraction is not the coverage performance of the prediction interval. Where there is no local support, the point prediction is kept; only the interval bounds and width are left missing.</p>')
    subgroup=evaluation.get('subgroup_diagnostics',{})
    subrows=[{'scope':scope,**r} for scope in ('per_repeat','independent') for r in subgroup.get(scope,[])]
    if subrows:
        pieces.append('<h2>Uncertainty in subgroups</h2><p>Each repeat is kept separate. No coverage guarantee is inferred for small groups. No significance test was done for coverage differences.</p>')
        pieces.append(_table(['Scope','Repeat','Field','Subgroup','Observations','Groups','Status','Coverage','Mean width'],
            [(SCOPE_LABELS.get(r['scope'],r['scope']),r['repeat'],r['field'],r['subgroup'],r['n'],r['group_count'],(r.get('interval') or {}).get('status'),
              (r.get('interval') or {}).get('coverage'),(r.get('interval') or {}).get('mean_width')) for r in subrows]))
    else:pieces.append('<p>No field was selected for subgroup uncertainty diagnostics.</p>')
    importance=evaluation.get('permutation_importance',{})
    if importance.get('rows'):
        pieces.append('<h2>Model explanation by permuting variables</h2><p>Importance is the error of the fixed outer-fold model on permuted input minus the baseline error of the same fold. Correlated variables can share importance. The result is not causal and was not used for reselection.</p>')
        pieces.append('<p>Permuting across rows does not preserve within-group or spatial dependence. The spread across repeats is not a confidence interval.</p>')
        pieces.append(_table(['Repeat','Variable','Error metric','Mean importance','Status'],
            [(r.get('repeat'),r.get('feature'),r.get('metric'),r.get('importance_mean'),r.get('status')) for r in importance.get('per_repeat',[])]))
    applicability=evaluation.get('final_record',{}).get('applicability')
    if applicability:
        pieces.append('<h2>Area of applicability (AOA)</h2><p>The environmental representativeness diagnostic uses the Euclidean distance computation validated against CAST 1.0.4. Equal weights are applied in the selected numeric design. Calibration and independent test observations are not used to learn the threshold.</p>')
        pieces.append('<p>In the applicability raster, 1 means inside the area and 0 means outside the area. Higher dissimilarity means greater distance from the training conditions. This product is not a prediction interval and does not guarantee cell accuracy.</p>')
    mapped_applicability=evaluation.get('mapping_diagnostics',{}).get('applicability_coverage')
    if mapped_applicability:
        pieces.append('<h3>Applicability coverage on the map</h3>')
        pieces.append(_table(['Scope','Value'],[
            ('Valid input cells',mapped_applicability['valid_input_pixels']),
            ('Cells inside the area of applicability',mapped_applicability['inside_pixels']),
            ('Cells outside the area of applicability',mapped_applicability['outside_pixels']),
            ('Inside the area: '+mapped_applicability['measure_label'],mapped_applicability['inside_fraction']),
            ('Outside the area: '+mapped_applicability['measure_label'],mapped_applicability['outside_fraction'])]))
        pieces.append('<p>'+html.escape(mapped_applicability['scope_limit'])+'</p>')
        pieces.append('<p class="note">'+html.escape(mapped_applicability['area_limit'])+'</p>')
        if mapped_applicability['status']=='no_valid_input_pixels':
            pieces.append('<p>The fraction was not computed because there are no valid input cells; it is not shown as zero.</p>')
        pieces.append('<p>'+('Prediction and interval cells outside the area were masked. Diagnostic rasters were not masked.'
                             if mapped_applicability['prediction_mask_includes_outside_applicability'] else
                             'This diagnostic did not mask predictions outside the area of applicability.')+'</p>')
    comparison=evaluation.get('plain_model_comparison',{})
    if comparison:
        pieces.append('<h2>Contribution of residual kriging</h2><p>The base model and the hybrid are compared on the same outer test observations. The base model is the same underlying model, built with the settings selected for the hybrid. It is not an independently reoptimized comparator.</p>')
        pieces.append(_table(['Method','Root mean squared error (RMSE)','Mean absolute error (MAE)','Coefficient of determination (R²)'],
            [(name,r.get('rmse'),r.get('mae'),r.get('r2')) for name,r in [('Base model',comparison['pooled_oof_plain']),('Hybrid',comparison['pooled_oof_hybrid'])]]))
    pieces.append('<p>Shapley value explanations, the Moran autocorrelation test and design-based map accuracy were not computed in this run. No spatial coverage guarantee or causal effect is inferred.</p>')
    pieces.extend(['<h2>Figures</h2>',
                   '<p>All figures use the original target scale. Observation points are not joined by lines in sequence. Spatial figures show coordinates; they do not produce a residual surface.</p>',
                   '<p>Figure resolution is %s dots per inch (dpi); it does not change the cell size of the raster map.</p>' % settings.get("mapping", {}).get("figure_dpi", 150)])
    for figure in figures:
        caption = figure["caption"]
        pieces.append('<figure><img src="' + html.escape(figure["files"][1], quote=True) + '" alt="' + html.escape(caption, quote=True) +
                      '"><figcaption>' + html.escape(caption) + ' <a href="' + html.escape(figure["files"][0], quote=True) + '">Vector graphic</a></figcaption></figure>')
    if issues:
        pieces.append('<h2>Undefined or nonfinite numbers</h2><p>These values were not replaced with zero. They are stored as null in the machine-readable record.</p>')
        pieces.append(_table(["Field", "Explanation"], [(issue["path"], issue["reason"]) for issue in issues]))
    final_record = evaluation.get("final_record", {})
    if final_record.get('experimental_model_diagnostics',{}).get('output_clamping_to_training_range'):
        pieces.append('<p>The RealMLP developer default keeps the point prediction within the training target range. This model behavior is kept in all folds; it is not an application of physical bounds set by the user.</p>')
    if final_record.get('model',{}).get('id')=='tabpfn':
        pieces.append('<p>Built with PriorLabs-TabPFN. TabPFN 2.2.1 and the fixed v2 regression weights were used on the local CPU. License: Prior Labs License 1.1.</p>')
    selected = final_record.get("selected_features", final_record.get("selected_ids", []))
    pieces.append('<h2>Features used in the final model</h2>')
    pieces.append(_table(["Order", "Feature"], [(i + 1, value) for i, value in enumerate(selected)]))
    pieces.append(study_section)
    pieces.extend(['<h2>Final model record</h2><pre>' + html.escape(json.dumps(final_record, ensure_ascii=False, indent=2)) + '</pre>',
                   '<h2>Settings used</h2><pre>' + html.escape(json.dumps(settings, ensure_ascii=False, indent=2)) + '</pre>',
                   '<h2>Downloadable records</h2><ul>'])
    for filename, label in [("evaluation.json", "Evaluation and numeric value status"), ("metrics.csv", "Accuracy metrics"),
                            ("oof_predictions.csv", "Out-of-fold predictions"), ("independent_predictions.csv", "Independent test predictions"),
                            ("validation_folds.csv", "Fold membership"), ("optimization_trials.csv", "Model search trials"),
                            ("learning_audit.json", "IDs used during learning"), ("final_record.json", "Final model and variables"),
                            ("selected_features.csv", "Features used in the final model"),
                            ("training_metrics.csv", "Training results"), ("coverage_diagnostics.csv", "Multilevel coverage diagnostics"),
                            ('subgroup_diagnostics.csv','Subgroup uncertainty diagnostics'),('permutation_summary.csv','Permutation variable importance'),('plain_model_comparison.csv','Base model and hybrid comparison'),
                            ("figure_manifest.json", "Figure data and export details")]:
        pieces.append('<li><a href="' + filename + '">' + label + '</a></li>')
    if evaluation.get("preflight"):
        pieces.append('<li><a href="preflight.json">Data, folds and estimated workload</a></li>')
    if evaluation.get("audit_path") == "audit.json":
        pieces.append('<li><a href="audit.json">Source file digests, package and code versions, run dates, operating system and hardware record</a></li>')
    pieces.append('</ul><p>Data records are in JavaScript Object Notation (JSON) format. In tables, a single quote is added before text that starts like a formula. Full text values are kept in the data records.</p></body></html>')
    return "\n".join(pieces)


def build_reports(output_dir, evaluation_result, samples, settings, *, check_cancel):
    """Write report artifacts transactionally and return their absolute paths.

    `settings` is the full schema-2 request; `samples` is read_samples_b output.
    Existing reports are refused. Figures are genuine 150/900 dpi renders, with
    separate SVG exports and complete tabular data. The live model is omitted.
    """
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    for name in ("report.html", "evaluation.json", "figure_manifest.json", "figures"):
        if (output / name).exists():
            raise JobError("REPORT_EXISTS", "An existing report is not overwritten: " + name)
    check_cancel()
    groups = _validate(evaluation_result, samples, settings)
    issues = []
    report_data = {key: value for key, value in evaluation_result.items() if key != "final_model"}
    if (output / "preflight.json").is_file():
        try:
            report_data["preflight"] = json.loads((output / "preflight.json").read_text(encoding="utf-8-sig"))
        except (ValueError, OSError) as exc:
            raise JobError("REPORT_PREFLIGHT", "The preflight check record could not be read.") from exc
    evaluation = _normal(report_data, "evaluation", issues)
    clean_settings = _normal(settings, "settings", issues)
    dpi = settings.get("mapping", {}).get("figure_dpi", 150)
    if type(dpi) is not int or dpi not in (150, 900):
        raise JobError("REPORT_DPI", "Figure resolution must be 150 or 900 dpi.")
    counts = _counts(evaluation_result, samples, groups)
    stage = Path(tempfile.mkdtemp(prefix=".report-", dir=output))
    published = []
    try:
        check_cancel()
        _write_json(stage / "evaluation.json", {"schema_version": 2, "evaluation": evaluation,
                    "counts": counts, "normalization": issues, "settings": clean_settings})
        prediction_fields = ["sample_id", "repeat", "fold", "group_id", "observed", "predicted", "residual", "lower", "upper", "interval_width", "interval_status", "reason_code"]
        _write_csv(stage / "oof_predictions.csv", evaluation.get("oof", []), prediction_fields)
        _write_csv(stage / "independent_predictions.csv", evaluation.get("independent_predictions", []), prediction_fields)
        _write_csv(stage / "metrics.csv", list(_flatten(evaluation.get("metrics", {}))), ["metric", "value", "label"])
        _write_csv(stage / "fold_metrics.csv", evaluation.get("fold_metrics", []))
        _write_csv(stage / "training_metrics.csv", _training_rows(evaluation), ["scope", "repeat", "fold", "n", "rmse", "mae", "mean_error", "r2"])
        coverage_rows = _coverage_rows(evaluation)
        _write_json(stage / "coverage_diagnostics.json", evaluation.get("coverage_diagnostics", {}))
        _write_csv(stage / "coverage_diagnostics.csv", coverage_rows, ["scope", "repeat", "fold", "nominal_coverage", "status", "n", "group_count", "coverage"])
        for key, name in [("predictions", "coverage_predictions.csv"), ("independent_predictions", "coverage_independent_predictions.csv")]:
            _write_csv(stage / name, evaluation.get("coverage_diagnostics", {}).get(key, []))
        for key, name in [("folds", "validation_folds"), ("trials", "optimization_trials"), ("learning_audit", "learning_audit")]:
            values = evaluation.get(key, [])
            _write_json(stage / (name + ".json"), values)
            if key == "folds":
                values = [{"repeat": fold["repeat"], "fold": fold["fold"], "role": role, "sample_id": identity}
                          for fold in values for role in ("train", "test", "excluded") for identity in fold.get(role + "_ids", [])]
            _write_csv(stage / (name + ".csv"), values)
        _write_json(stage / "final_record.json", evaluation.get("final_record", {}))
        for key in ('plain_model_comparison','permutation_importance','subgroup_diagnostics'):
            _write_json(stage/(key+'.json'),evaluation.get(key,{}))
        importance=evaluation.get('permutation_importance',{})
        _write_csv(stage/'permutation_importance.csv',importance.get('rows',[]))
        _write_csv(stage/'permutation_summary.csv',importance.get('per_repeat',[]))
        subgroup=evaluation.get('subgroup_diagnostics',{})
        _write_csv(stage/'subgroup_diagnostics.csv',[{'scope':scope,**r,**{'interval_'+k:v for k,v in (r.get('interval') or {}).items()}}
            for scope in ('per_repeat','outer_folds','independent') for r in subgroup.get(scope,[])])
        _write_csv(stage/'plain_model_comparison.csv',list(_flatten(evaluation.get('plain_model_comparison',{}))))
        final_record = evaluation.get("final_record", {})
        selected = final_record.get("selected_features", final_record.get("selected_ids", []))
        _write_csv(stage / "selected_features.csv", [{"index": i + 1, "feature": value} for i, value in enumerate(selected)], ["index", "feature"])
        check_cancel()
        with matplotlib.rc_context({"font.family": "DejaVu Sans", "font.size": 9, "axes.titlesize": 11,
                                    "axes.labelsize": 10, "svg.fonttype": "none", "svg.hashsalt": "spatial-prediction-stage-b",
                                    "axes.unicode_minus": False}):
            figures = _plots(stage / "figures", evaluation_result, samples,
                             str(settings.get("samples", {}).get("unit", "unit not specified")), dpi, check_cancel)
            figures.extend(_coverage_plot(stage / "figures", evaluation, dpi, check_cancel))
        _write_json(stage / "figure_manifest.json", {"schema_version": 1, "counts": counts, "figures": figures,
                    "matplotlib_version": matplotlib.__version__, "normalization": issues,
                    "csv_text_escape": "Apostrophe prefix for text starting with =,+,-,@ or control whitespace; JSON is unmodified."})
        from .publication_report import write_publication_reports
        publication_samples = {key: samples.get(key) for key in ('ids', 'support', 'depth', 'laboratory_method', 'excluded')}
        publication_samples['crs'] = str(samples['crs']) if samples.get('crs') is not None else None
        clean_samples = _normal(publication_samples, 'samples', issues)
        supplemental_report = _html(evaluation, samples, clean_settings, counts, figures, issues)
        study_section = write_publication_reports(stage, evaluation, clean_samples, clean_settings, counts, figures,
                                                  check_cancel=check_cancel, supplemental_report=supplemental_report)
        (stage / "report.html").write_text(_html(evaluation, samples, clean_settings, counts, figures, issues, study_section), encoding="utf-8")
        check_cancel()
        staged = sorted(path for path in stage.rglob("*") if path.is_file())
        destinations = [output / path.relative_to(stage) for path in staged]
        if any(path.exists() for path in destinations):
            raise JobError("REPORT_EXISTS", "One of the report files already exists; it was not overwritten.")
        for source, destination in zip(staged, destinations):
            check_cancel()
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.rename(destination)
            published.append(destination)
        return published
    except Exception:
        for path in published:
            path.unlink(missing_ok=True)
        figures_dir = output / "figures"
        if figures_dir.is_dir() and not any(figures_dir.iterdir()):
            figures_dir.rmdir()
        raise
    finally:
        shutil.rmtree(stage)
