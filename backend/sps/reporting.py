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
    "rmse": "ortalama kare hatanın karekökü",
    "mae": "ortalama mutlak hata", "me": "ortalama hata", "bias": "ortalama hata", "mean_error": "ortalama hata",
    "r2": "belirleme katsayısı (R²)", "coverage": "ampirik kapsama oranı",
    "mean_width": "ortalama aralık genişliği", "median_width": "medyan aralık genişliği",
    "lower_miss_rate": "alt sınırın altında kalma oranı",
    "upper_miss_rate": "üst sınırın üstünde kalma oranı", "interval_score": "aralık skoru",
    "below_lower_fraction": "alt sınırın altında kalma oranı",
    "above_upper_fraction": "üst sınırın üstünde kalma oranı", "nominal_coverage": "istenen kapsama oranı",
    "r2_reason": "belirleme katsayısının durumu",
    "crossing_count": "kesişen kantil sayısı",
    "rmse_defined_folds": "kare hata ölçütü hesaplanan kat sayısı",
    "mae_defined_folds": "mutlak hata ölçütü hesaplanan kat sayısı",
    "mean_error_defined_folds": "ortalama hata hesaplanan kat sayısı",
    "r2_defined_folds": "belirleme katsayısı tanımlı kat sayısı",
    "repeat": "tekrar", "fold": "kat",
    "pinball_lower": "alt kantil kaybı", "pinball_upper": "üst kantil kaybı",
    "n": "değerlendirme kaydı sayısı", "n_unique": "tekil gözlem sayısı",
    "available_subset": "Yalnız aralığı hesaplanan gözlemler",
    "available_n": "aralığı hesaplanan kayıt", "unavailable_n": "aralığı hesaplanamayan kayıt",
    "available_fraction": "aralık hesaplanabilen kayıtların payı",
    "coverage_denominator": "kapsama oranının paydası",
    "availability_denominator": "aralık hesaplanabilme payının paydası",
}
SCOPE_LABELS = {"pooled_oof": "Birleştirilmiş kat dışı değerlendirme", "per_repeat": "Tekrar bazında değerlendirme",
                "fold_mean": "Kat ölçütlerinin ortalaması", "independent": "Kilitli bağımsız test"}
METHOD_LABELS = {"random": "rastgele katlar", "repeated": "tekrarlı rastgele katlar", "group": "gruplu katlar",
                 "spatial_block": "mekânsal bloklar", "buffered": "tamponlu mekânsal bloklar", "custom": "kullanıcı tanımlı katlar",'knndm':'en yakın komşu uzaklığı eşleştirmeli katlar',
                 "none": "uygulanmadı", "split_conformal": "bölünmüş konformal tahmin", "quantile_forest": "kantil regresyon ormanı",
                 "quantile_boosting": "kantil kayıplı gradyan artırma", "cqr": "konformalize kantil regresyonu",
                 "spatial_conformal": "mekânsal ağırlıklı konformal tahmin (deneysel)",
                 "temporal": "zaman sıralı ve grupları ayıran doğrulama"}
COLORS = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#000000"]
MARKERS = ["o", "s", "^", "D", "v", "P", "X"]
COVERAGE_SCOPES = {"pooled_oof": "Birleştirilmiş kat dışı", "per_repeat": "Tekrar", "outer_folds": "Dış kat", "independent": "Kilitli test"}


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
        reason = "Sayı tanımsız." if math.isnan(value) else "Sayı sonlu değil."
        issues.append({"path": path, "reason": reason, "status": "undefined"})
        return None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        if any(not isinstance(key, (str, int)) for key in value):
            raise JobError("REPORT_SERIALIZATION", "Rapor alan adları metin veya tamsayı olmalıdır.")
        return {str(key): _normal(item, path + "." + str(key) if path else str(key), issues)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normal(item, path + "[%d]" % i, issues) for i, item in enumerate(value)]
    raise JobError("REPORT_SERIALIZATION", "Rapor nesnesi desteklenmiyor: " + path)


def _number(value):
    try:
        return float(value) if value is not None and not isinstance(value, bool) else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def _coverage_rows(evaluation):
    """Keep unavailable levels explicit; never substitute a smaller fold subset."""
    diagnostics = evaluation.get("coverage_diagnostics", {})
    if not isinstance(diagnostics, dict):
        raise JobError("REPORT_COVERAGE", "Kapsama tanıları nesne olmalıdır.")
    result = []
    for scope in COVERAGE_SCOPES:
        for row in diagnostics.get(scope, []):
            if not isinstance(row, dict) or row.get("status") not in ("available", "partial", "unavailable"):
                raise JobError("REPORT_COVERAGE", "Kapsama düzeyinin hesaplanma durumu açık olmalıdır.")
            nominal = _number(row.get("nominal_coverage"))
            if not math.isfinite(nominal) or not 0 < nominal < 1:
                raise JobError("REPORT_COVERAGE", "İstenen kapsama düzeyi 0 ile 1 arasında olmalıdır.")
            if row["status"] == "available":
                actual = _number(row.get("coverage"))
                if not math.isfinite(actual) or not 0 <= actual <= 1:
                    raise JobError("REPORT_COVERAGE", "Ampirik kapsama oranı 0 ile 1 arasında olmalıdır.")
                if not _number(row.get("n")) > 0 or not 0 < _number(row.get("group_count")) <= _number(row.get("n")):
                    raise JobError("REPORT_COVERAGE", "Kapsama tablosunda gözlem ve grup sayısı geçersiz.")
            elif not row.get("reason"):
                raise JobError("REPORT_COVERAGE", "Hesaplanamayan kapsama düzeyi için açıklama gereklidir.")
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
                    raise JobError("REPORT_COVERAGE", "Kısmi aralık kapsamının paydası, sayıları veya ölçütleri tutarsız.")
            result.append({**row, "scope": scope})
    return result


def _validate(evaluation, samples, settings):
    _coverage_rows(evaluation)
    ids = samples.get("ids", [])
    coordinates = np.asarray(samples.get("coordinates"), dtype=float)
    if len(ids) != len(set(ids)) or coordinates.shape != (len(ids), 2) or not np.isfinite(coordinates).all():
        raise JobError("REPORT_SAMPLES", "Rapor gözlemleri ve koordinatları tutarsız.")
    known = set(ids)
    independent = set(settings.get("validation", {}).get("independent_ids", []))
    groups = {}
    role_groups = {}
    for source in ("oof", "independent_predictions"):
        rows = evaluation.get(source, [])
        seen = set()
        for row in rows:
            if row.get("sample_id") not in known:
                raise JobError("REPORT_SAMPLE_ID", "Rapor, veride olmayan gözlem kimliği içeriyor.")
            if (source == "oof" and row["sample_id"] in independent) or (source == "independent_predictions" and row["sample_id"] not in independent):
                raise JobError("REPORT_INDEPENDENCE", "Tahminin rolü kilitli bağımsız test kimlikleriyle uyuşmuyor.")
            key = (row.get("repeat", 0), row["sample_id"])
            if key in seen:
                raise JobError("REPORT_DUPLICATE", "Bir gözlem aynı tekrarda birden fazla test tahmini taşıyor.")
            seen.add(key)
            observed, predicted, residual = [_number(row.get(name)) for name in ("observed", "predicted", "residual")]
            if all(math.isfinite(value) for value in (observed, predicted, residual)) and not math.isclose(
                    residual, predicted - observed, rel_tol=1e-9, abs_tol=1e-10):
                raise JobError("REPORT_RESIDUAL", "Artık işareti veya değeri tutarsız; artık tahmin eksi gözlem olmalıdır.")
            if "lower" in row and "upper" in row:
                lower, upper = _number(row["lower"]), _number(row["upper"])
                if settings.get('uncertainty',{}).get('method')=='spatial_conformal':
                    status=row.get('interval_status')
                    valid=(status=='available' and math.isfinite(lower) and math.isfinite(upper)) or (
                        status=='unavailable' and row['lower'] is None and row['upper'] is None
                        and row.get('interval_width') is None and bool(row.get('reason_code')))
                    if not valid:raise JobError('REPORT_INTERVAL','Yerel aralığın sınırları, durumu veya neden kodu tutarsız.')
                if math.isfinite(lower) and math.isfinite(upper):
                    if lower > upper or ("interval_width" in row and not math.isclose(
                            _number(row["interval_width"]), upper - lower, rel_tol=1e-9, abs_tol=1e-10)):
                        raise JobError("REPORT_INTERVAL", "Aralık sınırları veya genişliği tutarsız.")
            group = row.get("group_id")
            if group is not None:
                if row["sample_id"] in groups and groups[row["sample_id"]] != str(group):
                    raise JobError("REPORT_GROUP", "Gözlemin grup kimliği sonuçlar arasında değişiyor.")
                groups[row["sample_id"]] = str(group)
        role_groups[source] = {str(row["group_id"]) for row in rows if row.get("group_id") is not None}
    if role_groups["oof"] & role_groups["independent_predictions"]:
        raise JobError("REPORT_INDEPENDENCE", "Bağımsız test grubu kat dışı değerlendirmede kullanılmış.")
    for fold in evaluation.get("folds", []):
        train, test = set(fold["train_ids"]), set(fold["test_ids"])
        if train & test or (train | test) - known or (train | test) & independent:
            raise JobError("REPORT_FOLD", "Rapor katlarında kimlik veya bağımsız test sınırı ihlali var.")
        if {groups[i] for i in train if i in groups} & {groups[i] for i in test if i in groups}:
            raise JobError("REPORT_GROUP", "Rapor katlarında eğitim ve test grupları örtüşüyor.")
    for scope, source in [("pooled_oof", "oof"), ("independent", "independent_predictions")]:
        metric = evaluation.get("metrics", {}).get(scope, {})
        if not isinstance(metric, dict):
            continue
        values = [_number(row.get("observed")) for row in evaluation.get(source, [])]
        values = [value for value in values if math.isfinite(value)]
        if (len(values) < 2 or (values and max(values) == min(values))) and math.isfinite(_number(metric.get("r2"))):
            raise JobError("REPORT_R2", "Tek gözlem veya sabit hedef için sonlu belirleme katsayısı raporlanamaz.")
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
        raise JobError("REPORT_TABLE", "Rapor tablosunun her satırı nesne olmalıdır.")
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
                raise JobError("REPORT_FIGURE", "Grafik dosyası oluşturulamadı.")
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
    for source, key, label in [("oof", "oof", "Kat dışı"), ("independent", "independent_predictions", "Yeniden kullanılmış test" if reused else "Bağımsız test")]:
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
                figure, axis = _plot_base(label + ": gözlenen ve tahmin edilen", "Gözlenen (" + unit + ")", "Tahmin edilen (" + unit + ")")
                axis.scatter(observed[finite], predicted[finite], s=26, alpha=0.8, color=COLORS[0], edgecolors="white", linewidths=0.4)
                if finite.any():
                    lower = min(observed[finite].min(), predicted[finite].min())
                    upper = max(observed[finite].max(), predicted[finite].max())
                    pad = (upper - lower) * .05 if upper > lower else max(abs(lower) * .05, 1.0)
                    limits = [lower - pad, upper + pad]
                    axis.plot(limits, limits, color="#333333", linestyle="--", linewidth=1, label="1:1 doğrusu")
                    axis.set_xlim(limits); axis.set_ylim(limits); axis.set_aspect("equal", adjustable="box")
                    axis.legend(loc="best")
            elif kind == "residual_predicted":
                finite = np.isfinite(predicted) & np.isfinite(residual)
                figure, axis = _plot_base(label + ": artıklar", "Tahmin edilen (" + unit + ")", "Tahmin - gözlem (" + unit + ")")
                axis.scatter(predicted[finite], residual[finite], s=26, alpha=.8, color=COLORS[1], edgecolors="white", linewidths=.4)
                axis.axhline(0, color="#333333", linestyle="--", linewidth=1)
            else:
                finite = np.isfinite(residual)
                figure, axis = _plot_base(label + ": artık dağılımı", "Tahmin - gözlem (" + unit + ")", "Tahmin kaydı sayısı")
                bins = np.histogram_bin_edges(residual[finite], bins="fd") if finite.any() else np.asarray([0, 1])
                axis.hist(residual[finite], bins=bins, color=COLORS[0], edgecolor="white")
            omitted = int((~finite).sum())
            axis.text(.02, .98, "Gösterilen: %d/%d kayıt" % (finite.sum(), len(rows)), transform=axis.transAxes, va="top", fontsize=8,
                      bbox={"facecolor": "white", "edgecolor": "none", "alpha": .85})
            record = {"kind": kind, "source": source, "source_rows": len(rows), "plotted_rows": int(finite.sum()),
                      "nonfinite_rows": omitted, "unit": unit,
                      "caption": label + (". Histogram sütunları sınıf aralığındaki tahmin kaydı sayısını gösterir. " if kind == "residual_histogram" else ". Her nokta bir değerlendirme kaydıdır. ") + "Artık, tahmin eksi gözlemdir. "
                                 + ("Sonlu olmayan %d kayıt çizilmedi; kaynak tabloda korunuyor." % omitted if omitted else "Bütün sonlu kayıtlar gösterildi.")}
            if kind == "residual_histogram":
                record["bin_edges"] = bins.tolist()
                record["bin_rule"] = "Freedman-Diaconis; left closed, last bin includes right edge"
            manifest.append(_save_figure(figure, folder, source + "_" + kind, dpi, record, check_cancel))
    coordinates = np.asarray(samples["coordinates"], dtype=float)
    locations = dict(zip(samples["ids"], coordinates))
    crs = CRS.from_user_input(samples["crs"])
    coord_unit = crs.axis_info[0].unit_name if crs.axis_info else "koordinat birimi"
    coord_unit = {"metre": "m", "meter": "m", "foot": "ft", "US survey foot": "ABD ölçüm ayağı"}.get(coord_unit, coord_unit)
    xlabel, ylabel = ("Boylam (derece)", "Enlem (derece)") if crs.is_geographic else ("X (" + coord_unit + ")", "Y (" + coord_unit + ")")
    repeats = sorted({row.get("repeat", 0) for row in evaluation.get("oof", [])} | {fold["repeat"] for fold in evaluation.get("folds", [])})
    finite_residuals = [_number(row.get("residual")) for row in evaluation.get("oof", [])]
    limit = max([abs(value) for value in finite_residuals if math.isfinite(value)] + [1e-12])
    for repeat in repeats:
        folds = sorted((fold for fold in evaluation.get("folds", []) if fold["repeat"] == repeat), key=lambda fold: fold["fold"])
        figure, axis = _plot_base("Test katları: tekrar %d" % (repeat + 1), xlabel, ylabel)
        total = 0
        for index, fold in enumerate(folds):
            points = np.asarray([locations[identity] for identity in fold["test_ids"]])
            if not len(points):
                continue
            total += len(points)
            axis.scatter(points[:, 0], points[:, 1], color=COLORS[index % len(COLORS)],
                         marker=MARKERS[index % len(MARKERS)], s=28,
                         label="Kat %d" % (fold["fold"] + 1), edgecolors="#333333", linewidths=.35)
        if total:
            axis.legend(loc="best", ncols=2 if len(folds) > 5 else 1, fontsize=8)
        axis.ticklabel_format(useOffset=False, style="plain")
        axis.set_aspect("equal", adjustable="datalim")
        manifest.append(_save_figure(figure, folder, "folds_repeat_%d" % repeat, dpi,
                        {"kind": "fold_map", "source": "folds", "repeat": repeat, "source_rows": total,
                         "plotted_rows": total, "nonfinite_rows": 0, "crs": crs.to_string(),
                         "caption": "Tekrar %d içindeki test katları. Sembol ve renk katı belirtir. Eğitim üyelikleri kat tablosundadır." % (repeat + 1)}, check_cancel))
        rows = [row for row in evaluation.get("oof", []) if row.get("repeat", 0) == repeat]
        residual = np.asarray([_number(row.get("residual")) for row in rows])
        finite = np.isfinite(residual)
        points = np.asarray([locations[row["sample_id"]] for row in rows])
        figure, axis = _plot_base("Mekânsal artıklar: tekrar %d" % (repeat + 1), xlabel, ylabel)
        if finite.any():
            for mask, marker in [(finite & (residual < 0), "v"), (finite & (residual > 0), "^"), (finite & (residual == 0), "o")]:
                if not mask.any():
                    continue
                scatter = axis.scatter(points[mask, 0], points[mask, 1], c=residual[mask], s=40, marker=marker,
                                       cmap="RdBu_r", norm=TwoSlopeNorm(vmin=-limit, vcenter=0, vmax=limit),
                                       edgecolors="#333333", linewidths=.4)
            figure.colorbar(scatter, ax=axis, label="Tahmin - gözlem (" + unit + ")")
        axis.ticklabel_format(useOffset=False, style="plain")
        axis.set_aspect("equal", adjustable="datalim")
        manifest.append(_save_figure(figure, folder, "residual_map_repeat_%d" % repeat, dpi,
                        {"kind": "residual_map", "source": "oof", "repeat": repeat, "source_rows": len(rows),
                         "plotted_rows": int(finite.sum()), "nonfinite_rows": int((~finite).sum()),
                         "crs": crs.to_string(), "color_limits": [-limit, limit], "unit": unit,
                         "marker_encoding": "negative=triangle_down; positive=triangle_up; zero=circle",
                         "caption": "Tekrar %d için kat dışı artıklar. Renk ölçeği sıfır merkezlidir ve bütün tekrarlarda aynıdır. Aşağı üçgen negatif, yukarı üçgen pozitif, daire sıfır artığı gösterir. Noktalar arasında enterpolasyon yapılmadı." % (repeat + 1)}, check_cancel))
    return manifest


def _coverage_plot(folder, evaluation, dpi, check_cancel):
    rows = [row for row in _coverage_rows(evaluation) if row["scope"] in ("pooled_oof", "independent")]
    available = [row for row in rows if row["status"] == "available"]
    if not available:
        return []
    figure, axis = _plot_base("İstenen ve ampirik kapsama", "İstenen kapsama oranı", "Ampirik kapsama oranı")
    reused = evaluation.get("independent_test_history", {}).get("settings_changed_since_previous", False)
    for index, scope in enumerate(("pooled_oof", "independent")):
        values = [row for row in available if row["scope"] == scope]
        if values:
            label = "Yeniden kullanılmış test" if scope == "independent" and reused else COVERAGE_SCOPES[scope]
            axis.scatter([row["nominal_coverage"] for row in values], [row["coverage"] for row in values],
                         s=50, color=COLORS[index], marker=MARKERS[index], edgecolors="#333333", linewidths=.4, label=label)
    axis.plot([0, 1], [0, 1], linestyle="--", color="#333333", linewidth=1, label="İstenen = ampirik")
    axis.set_xlim(-.02, 1.02); axis.set_ylim(-.02, 1.02)
    axis.set_aspect("equal", adjustable="box"); axis.legend(loc="lower right", fontsize=8)
    caption = "Her sembol, sabit eğitilmiş modellerin bir kapsama düzeyindeki dış değerlendirmesidir. Hesaplanamayan düzeyler çizilmedi; nedenleri tabloda korunur. Tekrarlı kayıtlar bağımsız tekrar sayılmaz."
    record = {"kind": "coverage_calibration", "source": "coverage_diagnostics", "source_rows": len(rows),
              "plotted_rows": len(available), "unavailable_rows": len(rows) - len(available), "nonfinite_rows": 0,
              "values": available, "caption": caption}
    return [_save_figure(figure, folder, "coverage_calibration", dpi, record, check_cancel)]


def _display(value):
    if value is None:
        return "Tanımsız veya hesaplanmadı"
    if isinstance(value, bool):
        return "Evet" if value else "Hayır"
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
            label = "Tekrar %d" % (record.get("repeat", index) + 1) if isinstance(record, dict) else "Kayıt %d" % (index + 1)
            yield from _metric_rows(record, label + ": ")
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "repeat" and prefix:
                continue
            if isinstance(item, (dict, list)):
                label = "Tahmin aralığı" if key == "interval" else ("" if key == "metrics" else METRIC_LABELS.get(key, key))
                yield from _metric_rows(item, prefix + (label + ": " if label else ""))
            else:
                if key == "r2_reason":
                    item = {"constant_observed_target": "Gözlenen hedef sabit.", "fewer_than_two_observations": "İkiden az gözlem var."}.get(item, item)
                    if item is None and math.isfinite(_number(value.get("r2"))):
                        item = "Tanımlı"
                yield prefix + METRIC_LABELS.get(key, key), item
    else:
        yield prefix or "Değerlendirme", value


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
    pieces = ['<h3>Eğitim sonuçları</h3><p>Eğitim verisine yeniden uygulanan model burada ayrı gösterilir. Bu sonuçlar doğrulama başarısı değildir.</p>',
              _table(["Model", "Tekrar", "Kat", "Gözlem", METRIC_LABELS["rmse"], METRIC_LABELS["mae"], "Ortalama hata", "Belirleme katsayısı"],
                     [("Nihai model" if row["scope"] == "final_model_training" else "Dış kat modeli",
                       "" if row["repeat"] is None else row["repeat"] + 1, "" if row["fold"] is None else row["fold"] + 1,
                       row.get("n"), row.get("rmse"), row.get("mae"), row.get("mean_error"), row.get("r2")) for row in training]),
              '<h3>İç doğrulama denemeleri</h3><p>Model ve ayar seçimi iç değerlendirmeye dayanır. Aşağıdaki süre, ilgili denemenin gerçekleşen süresidir.</p>',
              _table(["Değerlendirme", "Deneme", "Model", "Değişken seçimi", "Bağlantı denetimi", "Ölçüt", "İç kayıp", "Durum", "Süre (s)", "Seçildi"],
                     [(row.get("role"), int(row.get("trial", 0)) + 1, row.get("model", {}).get("id", "belirtilmedi"),
                       "Seçim yok" if row.get("feature_selection_method") == "none" else row.get("feature_selection_method", "belirtilmedi"),
                       {"off": "Kapalı", "diagnostic": "Yalnız tanı", "iterative": "İteratif eleme"}.get(row.get("vif_mode"), "belirtilmedi"),
                       METRIC_LABELS.get(row.get("metric"), row.get("metric")), row.get("value"),
                       {"complete": "Tamamlandı", "failed": "Başarısız"}.get(row.get("status"), row.get("status", "belirtilmedi")),
                       row.get("elapsed_seconds"), "Evet" if row.get("selected") else "Hayır") for row in evaluation.get("trials", [])]),
              '<h3>Dış kat sonuçları</h3>',
              _table(["Tekrar", "Kat", "Gözlem", METRIC_LABELS["rmse"], METRIC_LABELS["mae"], "Ortalama hata", "Belirleme katsayısı"],
                     [(row["repeat"] + 1, row["fold"] + 1, row.get("metrics", row).get("n"), row.get("metrics", row).get("rmse"),
                       row.get("metrics", row).get("mae"), row.get("metrics", row).get("mean_error"), row.get("metrics", row).get("r2"))
                      for row in evaluation.get("fold_metrics", [])])]
    if any(row.get("comparison_scope") == "selector_and_vif_elimination" for row in evaluation.get("trials", [])):
        pieces.append('<p>Seçimsiz karşılaştırmada değişken seçimi ve varyans şişirme faktörüne dayalı eleme kapalıdır. Bağlantı denetimi yalnız tanı üretir.</p>')
    searches = defaultdict(list)
    for row in evaluation.get("trials", []):
        searches[row.get("role", "belirtilmedi")].append(row)
    if searches:
        summary_rows = []
        for role, rows in sorted(searches.items()):
            recorded = rows[-1].get("search_summary", {})
            elapsed = recorded.get("elapsed_seconds", max((_number(row.get("search_elapsed_seconds")) for row in rows
                if math.isfinite(_number(row.get("search_elapsed_seconds")))), default=None))
            reason = {"timeout_between_trials": "Denemeler arasında süre sınırı", "candidate_or_trial_budget_exhausted": "Aday listesi veya deneme bütçesi tamamlandı"}.get(
                recorded.get("stop_reason"), "Sonlandırma nedeni kaydedilmedi")
            summary_rows.append((role, len(rows), sum(row.get("status") == "failed" for row in rows), elapsed, reason))
        pieces.extend(['<h3>Gerçekleşen arama bütçesi</h3>',
            _table(["Arama", "Kaydedilen deneme", "Başarısız deneme", "Geçen toplam süre (s)", "Sonlandırma nedeni"], summary_rows),
            '<p>Paralel denemelerin süreleri toplam çalışma süresi olarak toplanmaz. Süre sınırı denemeler arasında uygulanır; sürmekte olan tahminci eğitimi yarıda kesilmez.</p>'])
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
    pieces = ['<h2>Katların veri ve uzaklık özeti</h2>',
              _table(["Tekrar", "Kat", "Eğitim gözlemi", "Test gözlemi", "Eğitim grubu", "Test grubu", "En kısa uzaklık (m)", "Medyan uzaklık (m)", "En uzun uzaklık (m)"], summary),
              '<p>Uzaklıklar her test gözleminin en yakın eğitim gözlemine uzaklığıdır. Tanımlanmayan uzaklık sıfır anlamına gelmez.</p>',
              '<h3>Çevresel koşullar</h3>',
              _table(["Tekrar", "Kat", "Değişken", "Eğitim en küçük", "Eğitim en büyük", "Test en küçük", "Test en büyük"], conditions)]
    if categories:
        pieces.extend(['<h3>Kategorik çevresel koşullar</h3>',
                       _table(["Tekrar", "Kat", "Değişken", "Eğitim kategorileri", "Test kategorileri", "Testte yeni kategoriler"], categories)])
    missing = preflight.get("missing_fraction", [])
    covariates = settings.get("covariates", [])
    if missing:
        pieces.extend(['<h3>Çevresel değişkenlerde eksik gözlem oranı</h3>',
                       _table(["Değişken", "Eksik oranı"], [(covariates[i].get("id") if i < len(covariates) else "Değişken %d" % (i + 1), value)
                                                            for i, value in enumerate(missing)])])
    for fold in folds:
        warning = fold.get("diagnostics", {}).get("distance_warning") or fold.get("diagnostics", {}).get("warning")
        if warning:
            pieces.append('<p class="note">' + html.escape(str(warning)) + '</p>')
    for warning in preflight.get("warnings", []):
        pieces.append('<p class="note">' + html.escape(str(warning)) + '</p>')
    workload = preflight.get("workload", {})
    if workload:
        labels = {"candidate_arms": "Aday model ailesi", "trial_budget": "Deneme bütçesi", "inner_folds": "İç kat",
                  "outer_folds": "Dış kat", "estimated_pipeline_fits_upper_bound": "Tahmin edilen işlem hattı eğitimi üst sınırı",
                  "extra_selector_fits_not_included": "Seçicinin ek eğitimleri sayıya dahil değil", "threads_per_fit": "Eğitim başına iş parçacığı"}
        pieces.extend(['<h3>Öngörülen iş yükü</h3>', _table(["Bileşen", "Öngörü"], [(labels.get(key, key), value) for key, value in workload.items()]),
                       '<p>Bu sayılar eğitimden önce hesaplanan iş yükü öngörüsüdür. Gerçekleşen deneme sayısı ve süreler ayrı tabloda verilmiştir.</p>'])
    return pieces


def _coverage_tables(evaluation):
    rows = _coverage_rows(evaluation)
    if not rows:
        return []
    def status(row):
        if row["status"] == "unavailable":
            return "Hesaplanamadı: " + str(row["reason"])
        if row["status"] == "partial":
            return "Yalnız bazı gözlemlerde aralık hesaplandı: " + str(row["reason"])
        return "Yetersiz ampirik kapsama" if row["coverage"] < row["nominal_coverage"] else "İstenen düzeye ulaştı (ampirik)"
    pieces = ['<h3>Birden fazla düzeyde kapsama</h3>',
              '<p>Aynı eğitilmiş modeller farklı kapsama düzeylerinde değerlendirilmiştir. Kalibrasyon gözlemleri bu değerlendirme tablosuna katılmaz. Hesaplanamayan düzeyler sıfırla veya yalnız uygun katların sonucuyla doldurulmaz.</p>',
              _table(["Kapsam", "Tekrar", "Kat", "İstenen oran", "Ampirik oran", "Kayıt", "Grup", "Durum"],
                     [(COVERAGE_SCOPES[row["scope"]], row["repeat"] + 1 if row.get("repeat") is not None else "",
                       row["fold"] + 1 if row.get("fold") is not None else "", row["nominal_coverage"],
                       row.get("coverage") if row["status"] == "available" else None, row.get("n"), row.get("group_count"), status(row)) for row in rows]),
              '<p>Yetersiz ampirik kapsama, gözlenen oranın istenen düzeyin altında kaldığını belirtir. Bu işaret tek başına istatistiksel anlamlılık testi değildir.</p>',
              '<h3>Aralık genişliği ve aşım tanıları</h3>',
              _table(["Kapsam", "İstenen oran", "Ortalama genişlik", "Medyan genişlik", "Alt aşım", "Üst aşım", "Alt kantil kaybı", "Üst kantil kaybı", "Aralık skoru"],
                     [(COVERAGE_SCOPES[row["scope"]], row["nominal_coverage"], row.get("mean_width"), row.get("median_width"),
                       row.get("below_lower_fraction"), row.get("above_upper_fraction"), row.get("pinball_lower"), row.get("pinball_upper"), row.get("interval_score"))
                      for row in rows if row["status"] == "available"])]
    partial = [row for row in rows if row["status"] == "partial"]
    if partial:
        pieces.extend(['<h3>Yalnız aralığı hesaplanan gözlemler</h3>',
                       '<p class="note">Aşağıdaki kapsama oranının paydası yalnız aralığı hesaplanan kayıtlardır. Aralığı bulunmayan gözlemler bu orana dahil edilmez. Sonuç bütün gözlemlerin kapsaması değildir.</p>',
                       _table(["Kapsam", "Tekrar", "Kat", "İstenen oran", "Toplam kayıt", "Aralık var", "Aralık yok", "Aralık hesaplanabilen pay", "Alt kümede kapsama", "Alt kümede ortalama genişlik", "Alt kümede aralık skoru"],
                              [(COVERAGE_SCOPES[r["scope"]], r.get("repeat"), r.get("fold"), r["nominal_coverage"], r["n"],
                                r["available_n"], r["unavailable_n"], r["available_fraction"], r["available_subset"]["coverage"],
                                r["available_subset"].get("mean_width"), r["available_subset"].get("interval_score")) for r in partial])])
    return pieces


def _html(evaluation, samples, settings, counts, figures, issues, study_section=""):
    unit = str(settings.get("samples", {}).get("unit", "birim belirtilmedi"))
    target = str(settings.get("samples", {}).get("target_field", "Hedef"))
    validation = settings.get("validation", {})
    uncertainty = settings.get("uncertainty", {})
    history = evaluation.get("independent_test_history", {})
    reused = history.get("settings_changed_since_previous", False)
    pieces = ['<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
              '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' data:; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
              '<title>Mekânsal tahmin değerlendirmesi</title><style>body{font-family:Segoe UI,Arial,sans-serif;color:#18222c;background:#fff;max-width:1100px;margin:2rem auto;padding:0 1rem;line-height:1.55}h1,h2{line-height:1.25}table{border-collapse:collapse;width:100%;margin:1rem 0}th,td{border:1px solid #cad1d7;padding:.45rem;text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#eef2f5}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f7;padding:1rem}figure{margin:2rem 0}img{max-width:100%;height:auto}.note{border-left:4px solid #006a99;padding:.6rem 1rem;background:#eef7fc}a{color:#005983}code{overflow-wrap:anywhere}</style></head><body>',
              '<h1>Mekânsal tahmin değerlendirmesi</h1>',
              '<p>Hedef: <strong>' + html.escape(target) + '</strong>. Birim: ' + html.escape(unit) + '.</p>',
              '<p>Çalışma kimliği: ' + html.escape(str(settings.get("run_id", "belirtilmedi"))) + '.</p>',
              '<h2>Veri ve değerlendirme kapsamı</h2>',
              _table(["Kapsam", "Sayı"], [("Girdi gözlemi", counts["input_samples"]), ("Tekil kat dışı gözlem", counts["oof_unique_samples"]),
                      ("Kat dışı tahmin kaydı", counts["oof_rows"]), ("Değerlendirilen tekrar", counts["oof_repeats"]),
                      ("Tekil bağımsız test gözlemi", counts["independent_unique_samples"]),
                      ("Sonuçlarda bildirilen ayrı grup", counts["reported_unique_groups"])]),
              '<p class="note">Tekrarlar bağımsız örnek sayısı değildir. Grup sayısı yalnız seçilen bağımlılık alanlarına dayanır; mekânsal bağımsızlığı tek başına kanıtlamaz.</p>',
              '<p>Örnek desteği: ' + html.escape({"point": "nokta", "composite": "kompozit örnek", "parcel_mean": "parsel ortalaması"}.get(samples.get("support"), "belirtilmedi")) + '. Derinlik: ' + html.escape(_display(samples.get("depth"))) +
              '. Laboratuvar yöntemi: ' + html.escape(_display(samples.get("laboratory_method"))) + '.</p>',
              '<h2>Doğruluk ölçütleri</h2>',
              '<p>Ortalama hata ve grafiklerdeki artık, tahmin eksi gözlem olarak hesaplanır. Belirleme katsayısı negatif olabilir. Sabit hedefte veya tek gözlemde tanımsızdır.</p>',
              '<p>Hata ölçütleri hedefin özgün birimindedir. Birleştirilmiş kat dışı ölçütler, kat ölçütlerinin ortalamasından ayrı verilir.</p>']
    for scope, metrics in evaluation.get("metrics", {}).items():
        if scope not in SCOPE_LABELS:
            continue
        title = "Yeniden kullanılmış test" if scope == "independent" and reused else SCOPE_LABELS.get(scope, scope)
        pieces.append("<h3>" + html.escape(title) + "</h3>")
        pieces.append(_table(["Ölçüt veya kayıt", "Değer"], _metric_rows(metrics)))
    pieces.extend(_role_tables(evaluation))
    pieces.extend(_preflight_tables(evaluation.get("preflight", {}), settings))
    if history:
        pieces.append("<h3>Kilitli testin kullanım geçmişi</h3>")
        pieces.append(_table(["Kayıt", "Değer"], [("Önceki değerlendirme sayısı", history.get("previous_evaluations", 0)),
                             ("Önceki kullanımdan sonra ayarlar değişti", "Evet" if reused else "Hayır")]))
        if history.get("warning"):
            pieces.append('<p class="note">' + html.escape(str(history["warning"])) + '</p>')
        if reused:
            pieces.append('<p class="note">Bu test verisi ayarlar değiştirildikten sonra yeniden kullanıldı. Sonuç, yeni ve bağımsız bir nihai başarı ölçümü olarak yorumlanamaz.</p>')
    if not counts["independent_unique_samples"]:
        pieces.append('<p>Bağımsız test sonucu yoktur. Kat dışı değerlendirme bağımsız dış veriyle sınama olarak sunulmaz.</p>')
    pieces.extend(['<h2>Doğrulama ve belirsizlik</h2>',
                   '<p>Doğrulama yöntemi: ' + html.escape(METHOD_LABELS.get(validation.get("method"), str(validation.get("method", "belirtilmedi")))) +
                   '. Kat sayısı: ' + html.escape(str(validation.get("n_splits", "belirtilmedi"))) +
                   '. Tohum: ' + html.escape(str(validation.get("seed", "belirtilmedi"))) + '.</p>',
                   '<p>Belirsizlik yöntemi: ' + html.escape(METHOD_LABELS.get(uncertainty.get("method", "none"), str(uncertainty.get("method")))) +
                   '. İstenen kapsama oranı: ' + html.escape(_display(uncertainty.get("coverage"))) + '.</p>'])
    if uncertainty.get("method", "none") == "none":
        pieces.append('<p>Tahmin aralığı üretilmedi. Nokta tahmininin doğruluk ölçütü, hücreye özgü belirsizlik değildir.</p>')
    else:
        pieces.append('<p class="note">Nominal kapsama, mekânsal veya gruplu veride kesin kapsama güvencesi olarak yorumlanmaz. Kapsama ve aralık genişliği dış değerlendirmede birlikte okunmalıdır.</p>')
        pieces.append('<p>Geliştirme, kalibrasyon ve test kimlikleri kayıt dosyalarında ayrı tutulur. Kalibrasyon hedefleri, model seçimi veya temel model eğitimi için kullanılmamalıdır.</p>')
        pieces.append('<p>Toplam kapsama bütün değerlendirme kayıtlarını birleştirir. Koşullu kapsama, belirli çevresel koşullar içindeki kapsamadır. Seçilen altgruplar ayrı değerlendirilir; yeterli gözlem veya aralık yoksa kapsama hesaplanmaz.</p>')
        pieces.append('<p>Dar aralık tek başına iyi sonuç değildir. Tahmin aralığı haritası, her hücrede doğruluğu kanıtlanmış bir ürün olarak yorumlanamaz.</p>')
    pieces.extend(_coverage_tables(evaluation))
    if validation.get('method')=='temporal':
        pieces.append('<h3>Zaman sırası ve değerlendirme sınırı</h3><p>İlk eğitim geçmişi dış katlarda sınanmaz. Birleştirilmiş doğruluk yalnız gerçekten sınanan gözlemlerden hesaplanır. Aynı profil ve çakışan tarih dönemleri bölünmez.</p>')
        pieces.append('<p class="note">Sınama, sonraki tarihlerdeki yeni profil veya parselleri kapsar. Mekânsal aktarım ve zaman içindeki değişim ayrı ayrı ölçülmüş değildir. Sabit yardımcı rasterler, belirli bir gelecek tarihe ait haritayı doğrulamaz.</p>')
        pieces.append(_table(['Kat','Eğitim başlangıcı','Eğitim sonu','Sınama başlangıcı','Sınama sonu','Sınama süresi (takvim günü)','Boş gün'],
            [(f['fold']+1,*[f.get('diagnostics',{}).get('temporal',{}).get(k) for k in ('train_start','train_end','test_start','test_end','test_calendar_days','gap_days')]) for f in evaluation.get('folds',[])]))
        temporal=evaluation.get('final_record',{}).get('temporal',{})
        if temporal:
            pieces.append(_table(['Nihai modelin zaman kaydı','Değer'],[
                ('Saat dilimi',temporal.get('timezone')),('Son kullanılan tarih',temporal.get('last_used_date')),
                ('Geliştirme tarihleri',temporal.get('development_dates')),('Kalibrasyon tarihleri',temporal.get('calibration_dates')),
                ('İstenen kalibrasyon grup oranı',temporal.get('requested_calibration_group_fraction')),
                ('Gerçekleşen kalibrasyon grup oranı',temporal.get('actual_calibration_group_fraction')),
                ('Gerçekleşen kalibrasyon gözlem oranı',temporal.get('actual_calibration_sample_fraction'))]))
            pieces.append('<p>Kalibrasyon oranlarının paydası, takvim ve mekânsal boşluk uygulanmadan önceki model kurma altkümesidir. Dönemler bölünmediği için gerçekleşen oran değişebilir.</p>')
    if uncertainty.get("method") == "spatial_conformal":
        pieces.append('<p class="note">Bu deneysel uyarlama, ayrı kalibrasyon artıklarını uzaklığa göre ağırlıklandırır. Bant genişliği değerlendirmeden önce sabittir; dış test hedefleriyle seçilmez. Mekânsal kapsama güvencesi vermez.</p>')
        pieces.append(_table(["Mekânsal kalibrasyon ayarı", "Değer"], [
            ("Bant genişliği (metre)", uncertainty.get("spatial_bandwidth_m")),
            ("Ağırlık işlevi", uncertainty.get("spatial_kernel")),
            ("Asgari etkin gözlem sayısı", uncertainty.get("spatial_min_effective_n")),
            ("Asgari etkin grup sayısı", uncertainty.get("spatial_min_groups"))]))
    interval_support = evaluation.get("mapping_diagnostics", {}).get("interval_support")
    if interval_support:
        pieces.append('<h3>Haritada yerel kalibrasyon desteği</h3>')
        pieces.append(_table(["Kapsam", "Değer"], [
            ("Geçerli girdi hücresi", interval_support["valid_input_pixels"]),
            ("Aralık hesaplanan hücre", interval_support["available_input_pixels"]),
            ("Yerel destek bulunmayan hücre", interval_support["unavailable_input_pixels"]),
            ("Aralık hesaplanabilen hücrelerin payı", interval_support["available_fraction"])]))
        pieces.append('<p>Payda, tahmin maskesi uygulanmadan önceki geçerli girdi hücreleridir. Bu pay, tahmin aralığının kapsama başarısı değildir. Yerel destek yoksa nokta tahmini korunur; yalnız aralık sınırları ve genişliği eksik bırakılır.</p>')
    subgroup=evaluation.get('subgroup_diagnostics',{})
    subrows=[{'scope':scope,**r} for scope in ('per_repeat','independent') for r in subgroup.get(scope,[])]
    if subrows:
        pieces.append('<h2>Altgruplarda belirsizlik</h2><p>Her tekrar ayrı tutulur. Küçük gruplarda kapsama güvencesi çıkarılmaz. Kapsama farkları için anlamlılık testi yapılmadı.</p>')
        pieces.append(_table(['Kapsam','Tekrar','Alan','Altgrup','Gözlem','Grup','Durum','Kapsama','Ortalama genişlik'],
            [(SCOPE_LABELS.get(r['scope'],r['scope']),r['repeat'],r['field'],r['subgroup'],r['n'],r['group_count'],(r.get('interval') or {}).get('status'),
              (r.get('interval') or {}).get('coverage'),(r.get('interval') or {}).get('mean_width')) for r in subrows]))
    else:pieces.append('<p>Altgrup belirsizlik tanısı için alan seçilmedi.</p>')
    importance=evaluation.get('permutation_importance',{})
    if importance.get('rows'):
        pieces.append('<h2>Değişkenleri karıştırarak model açıklama</h2><p>Önem, sabit dış kat modelinin karıştırılmış girdideki hatası eksi aynı katın temel hatasıdır. İlişkili değişkenler önemi paylaşabilir. Sonuç nedensel değildir ve yeniden seçim için kullanılmadı.</p>')
        pieces.append('<p>Satırlar arasında karıştırma grup içi veya mekânsal bağımlılığı korumaz. Tekrarların yayılımı güven aralığı değildir.</p>')
        pieces.append(_table(['Tekrar','Değişken','Hata ölçütü','Ortalama önem','Durum'],
            [(r.get('repeat'),r.get('feature'),r.get('metric'),r.get('importance_mean'),r.get('status')) for r in importance.get('per_repeat',[])]))
    applicability=evaluation.get('final_record',{}).get('applicability')
    if applicability:
        pieces.append('<h2>Uygulanabilirlik alanı</h2><p>Çevresel temsil tanısı, CAST 1.0.4 ile doğrulanan Öklid uzaklığı hesabını kullanır. Seçilen sayısal tasarımda eşit ağırlık uygulanır. Kalibrasyon ve bağımsız test gözlemleri eşiği öğrenmez.</p>')
        pieces.append('<p>Uygulanabilirlik rasterinde 1 alan içini, 0 alan dışını gösterir. Benzemezlik arttıkça eğitim koşullarından uzaklaşılır. Bu ürün tahmin aralığı veya hücre doğruluğu güvencesi değildir.</p>')
    mapped_applicability=evaluation.get('mapping_diagnostics',{}).get('applicability_coverage')
    if mapped_applicability:
        pieces.append('<h3>Haritada uygulanabilirlik kapsamı</h3>')
        pieces.append(_table(['Kapsam','Değer'],[
            ('Geçerli girdi hücresi',mapped_applicability['valid_input_pixels']),
            ('Uygulanabilirlik alanı içindeki hücre',mapped_applicability['inside_pixels']),
            ('Uygulanabilirlik alanı dışındaki hücre',mapped_applicability['outside_pixels']),
            ('Alan içi: '+mapped_applicability['measure_label'],mapped_applicability['inside_fraction']),
            ('Alan dışı: '+mapped_applicability['measure_label'],mapped_applicability['outside_fraction'])]))
        pieces.append('<p>'+html.escape(mapped_applicability['scope_limit'])+'</p>')
        pieces.append('<p class="note">'+html.escape(mapped_applicability['area_limit'])+'</p>')
        if mapped_applicability['status']=='no_valid_input_pixels':
            pieces.append('<p>Geçerli girdi hücresi bulunmadığı için pay hesaplanmadı; sıfır olarak gösterilmedi.</p>')
        pieces.append('<p>'+('Alan dışındaki tahmin ve aralık hücreleri maskelendi. Tanı rasterleri maskelenmedi.'
                             if mapped_applicability['prediction_mask_includes_outside_applicability'] else
                             'Uygulanabilirlik alanı dışında kalan tahminler bu tanı nedeniyle maskelenmedi.')+'</p>')
    comparison=evaluation.get('plain_model_comparison',{})
    if comparison:
        pieces.append('<h2>Artık kriging katkısı</h2><p>Yalın model ve hibrit aynı dış gözlemlerde karşılaştırılır. Yalın model, hibrit için seçilen ayarlarla kurulan aynı temel modeldir. Bağımsız biçimde yeniden optimize edilmiş bir karşılaştırıcı değildir.</p>')
        pieces.append(_table(['Yöntem','Ortalama kare hatanın karekökü','Ortalama mutlak hata','Belirleme katsayısı'],
            [(name,r.get('rmse'),r.get('mae'),r.get('r2')) for name,r in [('Yalın',comparison['pooled_oof_plain']),('Hibrit',comparison['pooled_oof_hybrid'])]]))
    pieces.append('<p>Bu çalıştırmada Shapley katkı açıklamaları, Moran otokorelasyon sınaması ve tasarıma dayalı alan doğruluğu hesaplanmadı. Mekânsal kapsama güvencesi veya nedensel etki çıkarılmaz.</p>')
    pieces.extend(['<h2>Grafikler</h2>',
                   '<p>Bütün grafikler özgün hedef ölçeğini kullanır. Gözlem noktaları sıraya göre çizgiyle bağlanmaz. Mekânsal grafikler koordinatları gösterir; bir artık yüzeyi üretmez.</p>',
                   '<p>Grafik baskı yoğunluğu inç başına %s noktadır (dpi); raster haritanın hücre boyutunu değiştirmez.</p>' % settings.get("mapping", {}).get("figure_dpi", 150)])
    for figure in figures:
        caption = figure["caption"]
        pieces.append('<figure><img src="' + html.escape(figure["files"][1], quote=True) + '" alt="' + html.escape(caption, quote=True) +
                      '"><figcaption>' + html.escape(caption) + ' <a href="' + html.escape(figure["files"][0], quote=True) + '">Vektörel grafik</a></figcaption></figure>')
    if issues:
        pieces.append('<h2>Tanımsız veya sonlu olmayan sayılar</h2><p>Bu değerler sıfırla değiştirilmedi. Makine tarafından okunabilen kayıtta null olarak saklandı.</p>')
        pieces.append(_table(["Alan", "Açıklama"], [(issue["path"], issue["reason"]) for issue in issues]))
    final_record = evaluation.get("final_record", {})
    if final_record.get('experimental_model_diagnostics',{}).get('output_clamping_to_training_range'):
        pieces.append('<p>RealMLP üretici ayarı, nokta tahminini eğitim hedef aralığında tutar. Bu model davranışı bütün katlarda korunmuştur; kullanıcıya ait fiziksel sınır uygulaması değildir.</p>')
    if final_record.get('model',{}).get('id')=='tabpfn':
        pieces.append('<p>Built with PriorLabs-TabPFN. TabPFN 2.2.1 ve sabit v2 regresyon ağırlığı yerel işlemcide kullanıldı. Lisans: Prior Labs License 1.1.</p>')
    selected = final_record.get("selected_features", final_record.get("selected_ids", []))
    pieces.append('<h2>Nihai modelde kullanılan özellikler</h2>')
    pieces.append(_table(["Sıra", "Özellik"], [(i + 1, value) for i, value in enumerate(selected)]))
    pieces.append(study_section)
    pieces.extend(['<h2>Nihai model kaydı</h2><pre>' + html.escape(json.dumps(final_record, ensure_ascii=False, indent=2)) + '</pre>',
                   '<h2>Kullanılan ayarlar</h2><pre>' + html.escape(json.dumps(settings, ensure_ascii=False, indent=2)) + '</pre>',
                   '<h2>İndirilebilir kayıtlar</h2><ul>'])
    for filename, label in [("evaluation.json", "Değerlendirme ve sayısal durumlar"), ("metrics.csv", "Doğruluk ölçütleri"),
                            ("oof_predictions.csv", "Kat dışı tahminler"), ("independent_predictions.csv", "Bağımsız test tahminleri"),
                            ("validation_folds.csv", "Kat üyelikleri"), ("optimization_trials.csv", "Model arama denemeleri"),
                            ("learning_audit.json", "Öğrenme sırasında kullanılan kimlikler"), ("final_record.json", "Nihai model ve değişkenler"),
                            ("selected_features.csv", "Nihai modelde kullanılan özellikler"),
                            ("training_metrics.csv", "Eğitim sonuçları"), ("coverage_diagnostics.csv", "Çok düzeyli kapsama tanıları"),
                            ('subgroup_diagnostics.csv','Altgrup belirsizlik tanıları'),('permutation_summary.csv','Değişkenlerin permütasyon önemi'),('plain_model_comparison.csv','Yalın ve hibrit karşılaştırması'),
                            ("figure_manifest.json", "Grafik verileri ve dışa aktarma bilgileri")]:
        pieces.append('<li><a href="' + filename + '">' + label + '</a></li>')
    if evaluation.get("preflight"):
        pieces.append('<li><a href="preflight.json">Veri, katlar ve öngörülen iş yükü</a></li>')
    if evaluation.get("audit_path") == "audit.json":
        pieces.append('<li><a href="audit.json">Kaynak özetleri, paket ve kod sürümleri, çalışma tarihleri, işletim sistemi ve donanım kaydı</a></li>')
    pieces.append('</ul><p>Veri kayıtları, JavaScript Object Notation (JSON) biçimindedir. Tablolarda formül gibi başlayan metinlerin başına tek tırnak eklenir. Tam metin değerleri veri kayıtlarında korunur.</p></body></html>')
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
            raise JobError("REPORT_EXISTS", "Mevcut raporun üzerine yazılmaz: " + name)
    check_cancel()
    groups = _validate(evaluation_result, samples, settings)
    issues = []
    report_data = {key: value for key, value in evaluation_result.items() if key != "final_model"}
    if (output / "preflight.json").is_file():
        try:
            report_data["preflight"] = json.loads((output / "preflight.json").read_text(encoding="utf-8-sig"))
        except (ValueError, OSError) as exc:
            raise JobError("REPORT_PREFLIGHT", "Ön kontrol kaydı okunamadı.") from exc
    evaluation = _normal(report_data, "evaluation", issues)
    clean_settings = _normal(settings, "settings", issues)
    dpi = settings.get("mapping", {}).get("figure_dpi", 150)
    if type(dpi) is not int or dpi not in (150, 900):
        raise JobError("REPORT_DPI", "Grafik baskı yoğunluğu 150 veya 900 olmalıdır.")
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
                             str(settings.get("samples", {}).get("unit", "birim belirtilmedi")), dpi, check_cancel)
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
            raise JobError("REPORT_EXISTS", "Rapor dosyalarından biri zaten var; üzerine yazılmadı.")
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
