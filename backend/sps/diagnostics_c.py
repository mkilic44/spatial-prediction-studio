"""Post-evaluation diagnostics. No fitting, selection, calibration or file writes.

Permutation definitions follow the fitted-model, held-out procedure documented at
https://scikit-learn.org/1.5/modules/permutation_importance.html .
Moran's I uses the explicit row-standardized spatial-weight definition described at
https://pysal.org/esda/stable/user-guide/global_morans_i.html .
The empirical half-squared-difference bin average follows Esri's explanation at
https://resources.arcgis.com/en/help/main/10.2/0031/0031000000mz000000.htm .
No randomization p-value or confidence interval assumes independent spatial rows.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json

import numpy as np
from scipy.spatial.distance import pdist

from .contract import JobError
from .modeling import prediction_call
from .uncertainty import interval_metrics
from .validation import _metric_coordinates


def _integer(value, minimum, name):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)) or value < minimum:
        raise JobError("DIAGNOSTIC_CONFIG", name + " geçerli bir tam sayı olmalıdır.")
    return int(value)


def _identities(values, name, unique=False):
    values = list(values)
    if any(not isinstance(v, (str, np.str_)) or not str(v).strip() for v in values):
        raise JobError("DIAGNOSTIC_IDS", name + " boş olmayan metin kimlikleri içermelidir.")
    result = [str(v) for v in values]
    if unique and len(set(result)) != len(result):
        raise JobError("DIAGNOSTIC_IDS", name + " yinelenen kimlik içeriyor.")
    return result


def _loss(y, prediction, metric):
    prediction = np.asarray(prediction, dtype=float)
    if prediction.shape != y.shape or not np.isfinite(prediction).all():
        raise JobError("DIAGNOSTIC_PREDICTION", "Tanı için tahminler sonlu ve hedefle aynı boyutta olmalıdır.")
    with np.errstate(over="ignore", invalid="ignore"):
        error = prediction - y
        value = np.sqrt(np.mean(error ** 2)) if metric == "rmse" else np.mean(np.abs(error))
    if not np.isfinite(value):
        raise JobError("DIAGNOSTIC_METRIC", "Tanı hata ölçütü sonlu hesaplanamadı.")
    return float(value)


def _feature_sets(covariates, feature_groups):
    names = [item["id"] if isinstance(item, dict) else item for item in covariates]
    names = _identities(names, "Değişkenler", unique=True)
    if feature_groups is None:
        return [(name, [name], [i]) for i, name in enumerate(names)]
    if not isinstance(feature_groups, dict) or not feature_groups:
        raise JobError("DIAGNOSTIC_FEATURES", "Değişken grupları, ad ve değişken kimlikleri içeren boş olmayan bir sözlük olmalıdır.")
    result = []
    for name, members in feature_groups.items():
        _identities([name], "Değişken grubu")
        if not isinstance(members, (list, tuple)):
            raise JobError("DIAGNOSTIC_FEATURES", "Değişken grubundaki kimlikler liste olmalıdır.")
        members = _identities(members, "Değişken grubu üyeleri", unique=True)
        if not members or set(members) - set(names):
            raise JobError("DIAGNOSTIC_FEATURES", "Değişken grubu boş veya bilinmeyen değişken içeriyor.")
        result.append((name, members, [names.index(member) for member in members]))
    return result


def _permutation_index(groups, mode, rng):
    index = np.arange(len(groups))
    grouped = [np.flatnonzero(groups == group) for group in sorted(set(groups))]
    if mode == "rows":
        return rng.permutation(index)
    if mode == "within_group":
        for members in grouped:
            index[members] = rng.permutation(members)
    else:
        for destination, source in enumerate(rng.permutation(len(grouped))):
            index[grouped[destination]] = grouped[source]
    return index


def _spread(values):
    values = np.asarray(values, dtype=float)
    scale = float(np.max(np.abs(values))) or 1.
    return {"importance_mean": float(np.mean(values / scale) * scale), "importance_sd": float(np.std(values / scale, ddof=0) * scale),
            "importance_min": float(np.min(values)), "importance_max": float(np.max(values))}


def heldout_permutation_importance(model, X, y, ids, groups, covariates, *, repeat, fold,
        coordinates=None, training_ids=(), calibration_ids=(), training_groups=(), calibration_groups=(),
        n_permutations=10, seed=42, metric="rmse", permutation="rows", feature_groups=None,
        check_cancel=lambda: None):
    """Evaluate a fixed outer-fold model; importance is permuted loss - baseline loss.

    X is the raw feature matrix, before the fitted workflow's preprocessing. All
    feature sets share a permutation within each permutation_index. Locations stay
    fixed for spatial prediction. within_group measures only variation within a
    supplied dependence group. whole_group exchanges equal-size blocks, aligned by
    sorted sample-ID rank; it requires comparable member positions across groups.
    Model training/calibration IDs are mandatory provenance, from arguments or
    model.record. Group checks are additional when their IDs are supplied.
    """
    check_cancel()
    n_permutations = _integer(n_permutations, 1, "Permütasyon sayısı")
    seed = _integer(seed, 0, "Rastgelelik tohumu")
    if metric not in ("rmse", "mae") or permutation not in ("rows", "within_group", "whole_group"):
        raise JobError("DIAGNOSTIC_CONFIG", "Permütasyon için desteklenen hata ölçütü ve grup kuralı seçilmelidir.")
    if (repeat is None) != (fold is None):
        raise JobError("DIAGNOSTIC_CONFIG", "Tekrar ve kat birlikte tanımlanmalıdır.")
    if repeat is not None:
        repeat, fold = _integer(repeat, 0, "Tekrar"), _integer(fold, 0, "Kat")
    ids, groups = _identities(ids, "Değerlendirme", unique=True), _identities(groups, "Gruplar")
    X, y = np.asarray(X, dtype=float), np.asarray(y, dtype=float)
    if X.shape != (len(ids), len(covariates)) or y.shape != (len(ids),) or len(groups) != len(ids):
        raise JobError("DIAGNOSTIC_SHAPE", "Tanı matrisi, hedef, gruplar ve kimlikler uyuşmuyor.")
    if not len(ids) or not np.isfinite(y).all() or np.isinf(X).any():
        raise JobError("DIAGNOSTIC_VALUES", "Tanı verisi boş olamaz; hedefler sonlu olmalı, değişkenlerde sonsuz değer bulunmamalıdır.")
    record = getattr(model, "record", {}) or {}
    trained = set(_identities(training_ids, "Eğitim"))
    calibrated = set(_identities(calibration_ids, "Kalibrasyon"))
    for key in ("training_ids", "development_ids"):
        trained.update(_identities(record.get(key, []), "Eğitim"))
    calibrated.update(_identities(record.get("calibration_ids", []), "Kalibrasyon"))
    if not trained:
        raise JobError("DIAGNOSTIC_PROVENANCE", "Eğitim kimlikleri olmadan kat dışı permütasyon tanısı hesaplanamaz.")
    if set(ids) & (trained | calibrated):
        raise JobError("DIAGNOSTIC_LEAKAGE", "Permütasyon verisi eğitim veya kalibrasyon kimliği içeriyor.")
    known_train_groups = set(_identities(training_groups, "Eğitim grupları"))
    known_cal_groups = set(_identities(calibration_groups, "Kalibrasyon grupları"))
    if set(groups) & (known_train_groups | known_cal_groups):
        raise JobError("DIAGNOSTIC_GROUP_LEAKAGE", "Permütasyon verisinin grubu eğitim veya kalibrasyon verisiyle ortak.")
    features = _feature_sets(covariates, feature_groups)
    order = np.argsort(np.asarray(ids), kind="stable")
    ids = [ids[i] for i in order]
    X, y, groups = X[order].copy(), y[order].copy(), np.asarray(groups, dtype=object)[order]
    locations = None
    if coordinates is not None:
        locations = np.asarray(coordinates, dtype=float)
        if locations.shape != (len(ids), 2) or not np.isfinite(locations).all():
            raise JobError("DIAGNOSTIC_COORDINATES", "Tanı koordinatları sonlu ve iki sütunlu olmalıdır.")
        locations = locations[order].copy()
    sizes = [int(np.sum(groups == group)) for group in sorted(set(groups))]
    if permutation == "whole_group" and len(set(sizes)) != 1:
        raise JobError("DIAGNOSTIC_GROUP_SIZE", "Bütün grup permütasyonu eşit büyüklükte gruplar gerektirir.")
    result = {"scope": "independent" if repeat is None else "outer_fold", "repeat": repeat, "fold": fold,
        "status": "available", "metric": metric, "permutation": permutation, "seed": seed,
        "n_permutations": n_permutations, "n": len(ids), "distinct_sample_count": len(ids), "group_count": len(sizes),
        "heldout_ids": ids, "training_ids": sorted(trained), "calibration_ids": sorted(calibrated),
        "training_group_check": bool(known_train_groups), "calibration_group_check": bool(known_cal_groups),
        "model": deepcopy(record.get("model", {})), "rows": [], "summary": [],
        "importance_definition": "permuted_loss_minus_same_fold_baseline_loss",
        "coordinates_permuted": False, "feature_groups": {name: members for name, members, _ in features},
        "warnings": [
            "Permütasyon önemi, değişken değerleri karıştırıldığında sabit modelin dış test hatasındaki değişimdir.",
            "İlişkili değişkenler önemi paylaşabilir. Karıştırma, eğitimde görülmeyen değişken birleşimleri oluşturabilir.",
            "Sonuçlar nedensel etkiyi göstermez. Bu tanılara göre yeniden seçim yeni bir geliştirme döngüsü gerektirir.",
            "Permütasyon tekrarları ve çapraz doğrulama katları bağımsız örnek değildir. Standart sapma tanımlayıcıdır; güven aralığı değildir.",
            "Koordinatlar sabit tutulur; önem bütün tahmin yönteminin çevresel değişkenlere duyarlılığını gösterir."],
        "permutation_assumption": {
            "rows": "Satırlar arasında karıştırma yapılır. Grup içi ve mekânsal bağımlılık korunmaz.",
            "within_group": "Yalnız aynı grup içindeki satırlar karıştırılır. Grup içinde sabit değişkenlerin gruplar arasındaki etkisi değerlendirilmez.",
            "whole_group": "Eşit büyüklükte gruplar bütünüyle değiştirilir. Grup içi üyeler sıralanmış kimlik konumuyla eşlenir; bu konumlar karşılaştırılabilir olmalıdır."
        }[permutation]}
    if not features:
        result.update(status="unavailable", reason_code="NO_FEATURES", reason="Permütasyon için çevresel değişken yok.")
        return result
    if len(ids) < 2 or (permutation == "within_group" and max(sizes) < 2) or (permutation == "whole_group" and len(sizes) < 2):
        result.update(status="unavailable", reason_code="NO_PERMUTABLE_UNITS", reason="Seçilen kuralla karıştırılabilecek yeterli birim yok.")
        return result
    baseline = _loss(y, prediction_call(model, "predict", X.copy(), None if locations is None else locations.copy()), metric)
    result["baseline_loss"] = baseline
    for number in range(n_permutations):
        digest = hashlib.sha256(json.dumps([seed, repeat, fold, number, permutation]).encode("utf-8")).digest()
        rng = np.random.default_rng(int.from_bytes(digest[:8], "little"))
        index = _permutation_index(groups, permutation, rng)
        for name, members, columns in features:
            check_cancel()
            changed = X.copy()
            changed[:, columns] = X[index][:, columns]
            loss = _loss(y, prediction_call(model, "predict", changed, None if locations is None else locations.copy()), metric)
            result["rows"].append({"repeat": repeat, "fold": fold, "feature": name, "members": list(members),
                "permutation_index": number, "metric": metric, "permutation": permutation,
                "baseline_loss": baseline, "permuted_loss": loss, "importance": loss - baseline,
                "n": len(ids), "group_count": len(sizes), "moved_rows": int(np.sum(index != np.arange(len(ids)))),
                "permutation_sha256": hashlib.sha256(index.astype("<i8").tobytes()).hexdigest()})
    for name, members, _ in features:
        values = [row["importance"] for row in result["rows"] if row["feature"] == name]
        result["summary"].append({"repeat": repeat, "fold": fold, "feature": name, "members": list(members),
            "metric": metric, "permutation": permutation, "n_permutations": n_permutations, **_spread(values)})
    check_cancel()
    return result


def aggregate_permutation_importance(fold_results):
    """Equal-fold descriptive means, separately for each CV repeat; no inference."""
    results = deepcopy(list(fold_results))
    seen = set()
    for result in results:
        key = result["scope"], result["repeat"], result["fold"]
        if key in seen:
            raise JobError("DIAGNOSTIC_DUPLICATE_FOLD", "Permütasyon özeti aynı değerlendirme katını birden fazla kez içeriyor.")
        seen.add(key)
    rows = [row for result in results for row in result["rows"]]
    grouped = defaultdict(list)
    definitions = {}
    for result in results:
        for feature, members in result["feature_groups"].items():
            key = (result["scope"], result["repeat"], feature, result["metric"], result["permutation"])
            if key in definitions and definitions[key] != members:
                raise JobError("DIAGNOSTIC_INCOMPARABLE", "Katlar arasında aynı değişken grubunun üyeleri farklı; önemler birleştirilemez.")
            definitions[key] = members
            grouped[key]  # Keep explicit unavailable rows when every fold failed.
        for summary in result["summary"]:
            key = (result["scope"], result["repeat"], summary["feature"], summary["metric"], summary["permutation"])
            grouped[key].append(summary)
    aggregated = []
    for (scope, repeat, feature, metric, permutation), summaries in grouped.items():
        expected = [result for result in results if result["scope"] == scope and result["repeat"] == repeat]
        complete = len(summaries) == len(expected)
        aggregated.append({"scope": scope, "repeat": repeat, "feature": feature, "metric": metric, "permutation": permutation,
            "status": "available" if complete else "unavailable", "fold_count": len(expected), "available_fold_count": len(summaries),
            "fold_mean_values": [{"fold": row["fold"], "importance_mean": row["importance_mean"]} for row in summaries],
            **(_spread([row["importance_mean"] for row in summaries]) if complete else
               {"reason_code": "INCOMPLETE_FOLDS", "reason": "Bütün ilgili dış katlarda önem hesaplanamadı."})})
    return {"folds": results, "rows": rows, "per_repeat": [r for r in aggregated if r["scope"] == "outer_fold"],
        "independent": [r for r in aggregated if r["scope"] == "independent"],
        "aggregation": "equal_fold_means_within_repeat; no pooling across repeats; incomplete folds unavailable",
        "warning": "Katlar, tekrarlar ve permütasyonlar bağımsız deney tekrarı değildir. Güven aralığı veya anlamlılık testi hesaplanmadı."}


def _samples(samples):
    ids = _identities(samples["ids"], "Örnekler", unique=True)
    props = samples.get("props", [{} for _ in ids])
    if len(props) != len(ids) or any(not isinstance(row, dict) for row in props):
        raise JobError("DIAGNOSTIC_PROPERTIES", "Örnek özellikleri kimliklerle uyuşmuyor.")
    return ids, {identity: index for index, identity in enumerate(ids)}, props


def _available_interval(row):
    if "lower" not in row and "upper" not in row:
        return False
    if "lower" not in row or "upper" not in row:
        raise JobError("DIAGNOSTIC_INTERVAL", "Aralık alt ve üst sınırı birlikte verilmelidir.")
    lower, upper = row["lower"], row["upper"]
    if lower is None and upper is None and row.get("interval_status") == "unavailable" and isinstance(row.get("reason_code"), str) and row["reason_code"].strip():
        return False
    if lower is None or upper is None or row.get("interval_status", "available") != "available":
        raise JobError("DIAGNOSTIC_INTERVAL", "Boş aralık için açık kullanılamaz durumu ve neden kodu gerekir.")
    try:
        valid = np.isfinite([lower, upper]).all() and lower <= upper
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise JobError("DIAGNOSTIC_INTERVAL", "Aralık sınırları sonlu ve sıralı olmalıdır.")
    return True


def _evaluation_partitions(evaluation, sample_index):
    outer = list(evaluation.get("oof", []))
    independent = list(evaluation.get("independent_predictions", []))
    if {row["sample_id"] for row in outer} & {row["sample_id"] for row in independent}:
        raise JobError("DIAGNOSTIC_LEAKAGE", "Bağımsız test ile dış kat tahminleri ortak örnek içeriyor.")
    for source, records in (("outer", outer), ("independent", independent)):
        seen = set()
        for row in records:
            identity = row.get("sample_id")
            if identity not in sample_index:
                raise JobError("DIAGNOSTIC_IDS", "Değerlendirme kimliği örnek özelliklerinde bulunamadı.")
            _identities([row.get("group_id")], "Değerlendirme grubu")
            if source == "outer":
                _integer(row.get("repeat"), 0, "Tekrar")
                _integer(row.get("fold"), 0, "Kat")
            key = identity, row.get("repeat") if source == "outer" else None
            if key in seen:
                raise JobError("DIAGNOSTIC_DUPLICATE_PREDICTION", "Aynı örnek bir değerlendirme tekrarında birden fazla tahmin içeriyor.")
            seen.add(key)
            try:
                observed, predicted = float(row["observed"]), float(row["predicted"])
            except (ValueError, TypeError, KeyError) as exc:
                raise JobError("DIAGNOSTIC_VALUES", "Gözlenen ve tahmin edilen değerler sayısal olmalıdır.") from exc
            if not np.isfinite([observed, predicted]).all():
                raise JobError("DIAGNOSTIC_VALUES", "Gözlenen ve tahmin edilen değerler sonlu olmalıdır.")
            if not np.isfinite(predicted - observed):
                raise JobError("DIAGNOSTIC_VALUES", "Tahmin ile gözlem farkı sonlu hesaplanamadı.")
            if "residual" in row and not np.isclose(row["residual"], predicted - observed, rtol=1e-10, atol=1e-12):
                raise JobError("DIAGNOSTIC_RESIDUAL", "Artık, tahmin eksi gözlem işaretiyle uyuşmuyor.")
            _available_interval(row)
    partitions = []
    for repeat in sorted({row["repeat"] for row in outer}):
        records = [row for row in outer if row["repeat"] == repeat]
        partitions.append(("per_repeat", repeat, None, records))
        for fold in sorted({row["fold"] for row in records}):
            partitions.append(("outer_folds", repeat, fold, [row for row in records if row["fold"] == fold]))
    if independent:
        partitions.append(("independent", None, None, independent))
    return partitions


def _field_definitions(fields):
    definitions = []
    seen = set()
    if not isinstance(fields, (list, tuple)):
        raise JobError("DIAGNOSTIC_SUBGROUP", "Altgrup alanları liste olmalıdır.")
    for item in fields:
        item = {"field": item} if isinstance(item, str) else deepcopy(item)
        if not isinstance(item, dict) or not isinstance(item.get("field"), str) or not item["field"].strip():
            raise JobError("DIAGNOSTIC_SUBGROUP", "Altgrup için geçerli alan adı gereklidir.")
        item.setdefault("source", "property")
        if item["source"] not in ("property", "row") or (item["source"] == "row" and item["field"] not in ("predicted", "training_distance_m")):
            raise JobError("DIAGNOSTIC_SUBGROUP", "Altgrup kaynağı örnek özelliği, tahmin veya eğitim verisine uzaklık olmalıdır.")
        key = item["source"], item["field"]
        if key in seen:
            raise JobError("DIAGNOSTIC_SUBGROUP", "Altgrup alanı birden fazla kez tanımlandı.")
        seen.add(key)
        if "bins" in item:
            edges = np.asarray(item["bins"], dtype=float)
            if edges.ndim != 1 or not len(edges) or not np.isfinite(edges).all() or np.any(np.diff(edges) <= 0):
                raise JobError("DIAGNOSTIC_SUBGROUP", "Sınıf sınırları sonlu ve kesin artan sırada olmalıdır.")
            item["bins"] = edges.tolist()
        elif item["source"] == "row":
            raise JobError("DIAGNOSTIC_SUBGROUP", "Tahmin ve uzaklık sınıfları için önceden belirlenmiş sınırlar gereklidir.")
        definitions.append(item)
    return definitions


def _category(value, definition):
    missing = value is None or (isinstance(value, str) and not value.strip())
    if isinstance(value, (float, np.floating)) and not np.isfinite(value):
        missing = True
    if missing:
        return "missing", {"subgroup": "Eksik", "subgroup_kind": "missing"}
    if "bins" in definition:
        try:
            value = float(value)
        except (ValueError, TypeError):
            return "invalid", {"subgroup": "Sayısal olmayan değer", "subgroup_kind": "invalid"}
        if not np.isfinite(value):
            return "missing", {"subgroup": "Eksik", "subgroup_kind": "missing"}
        index = int(np.searchsorted(definition["bins"], value, side="right"))
        lower = definition["bins"][index - 1] if index else None
        upper = definition["bins"][index] if index < len(definition["bins"]) else None
        label = ("%g altı" % upper if lower is None else "%g ve üzeri" % lower if upper is None else "%g <= değer < %g" % (lower, upper))
        return "bin:%d" % index, {"subgroup": label, "subgroup_kind": "bin", "lower_bound": lower, "upper_bound": upper}
    if isinstance(value, np.generic):
        value = value.item()
    if not isinstance(value, (str, int, float, bool)):
        raise JobError("DIAGNOSTIC_SUBGROUP", "Kategorik altgrup değeri tek bir metin veya sayı olmalıdır.")
    return "category:" + json.dumps(value, ensure_ascii=False), {"subgroup": str(value), "subgroup_kind": "category", "category_value": value}


def _subgroup_metrics(records, coverage, min_samples, min_groups):
    from .evaluation import regression_metrics  # Avoid a module-level evaluation cycle.
    n = len(records)
    group_count = len({row["group_id"] for row in records})
    result = {"n": n, "distinct_sample_count": len({row["sample_id"] for row in records}), "group_count": group_count,
        "fold_count": len({row.get("fold") for row in records}),
        "point_metrics": regression_metrics([row["observed"] for row in records], [row["predicted"] for row in records]) if n else None}
    with_interval = [row for row in records if _available_interval(row)]
    reasons = []
    if n < min_samples or group_count < min_groups:
        reasons.append(("INSUFFICIENT_SUBGROUP_SIZE", "Altgruptaki örnek veya bağımlılık grubu sayısı belirlenen eşiğin altında."))
    if not with_interval:
        reasons.append(("NO_INTERVALS", "Altgrupta değerlendirilmiş tahmin aralığı yok."))
    elif len(with_interval) != n:
        reasons.append(("INCOMPLETE_INTERVALS", "Altgruptaki bütün örneklerde tahmin aralığı bulunmuyor; kısmi kapsama hesaplanmadı."))
    interval = {"nominal_coverage": coverage, "n": n, "interval_count": len(with_interval), "group_count": group_count,
                "status": "unavailable" if reasons else "available"}
    if reasons:
        interval.update(reason_code=reasons[0][0], reason_codes=[r[0] for r in reasons], reason=" ".join(r[1] for r in reasons))
    else:
        interval.update(interval_metrics([r["observed"] for r in records], [r["lower"] for r in records], [r["upper"] for r in records], coverage))
        if not all(np.isfinite(value) for value in interval.values() if isinstance(value, (int, float))):
            raise JobError("DIAGNOSTIC_METRIC", "Altgrubun aralık ölçütleri sonlu hesaplanamadı.")
        interval["empirical_undercoverage"] = interval["coverage"] < coverage
    result["interval"] = interval
    return result


def subgroup_diagnostics(evaluation_result, samples, *, fields, coverage=.9, min_samples=10, min_groups=3,
                         check_cancel=lambda: None):
    """Summaries at explicitly supplied group boundaries, separately by repeat.

    fields: property-name strings, or dictionaries with field, source ('property'
    or 'row') and optional fixed bins. row permits predicted/training_distance_m
    and requires bins. Numeric bins are left-closed; under/overflow are retained.
    Only the evaluated, configured nominal interval is summarized. Small groups,
    missing intervals and missing properties remain visible. No conditional
    coverage guarantee, binomial confidence interval or post-hoc best-group search.
    """
    check_cancel()
    min_samples, min_groups = _integer(min_samples, 2, "En az örnek sayısı"), _integer(min_groups, 2, "En az grup sayısı")
    coverage = float(coverage)
    if not np.isfinite(coverage) or not 0 < coverage < 1:
        raise JobError("DIAGNOSTIC_COVERAGE", "Nominal kapsama sıfır ile bir arasında olmalıdır.")
    for scope in ("pooled_oof", "independent"):
        recorded = (evaluation_result.get("metrics", {}).get(scope) or {}).get("interval", {}).get("nominal_coverage")
        if recorded is not None and not np.isclose(float(recorded), coverage, rtol=0, atol=1e-12):
            raise JobError("DIAGNOSTIC_COVERAGE", "Altgrup için nominal kapsama, değerlendirilmiş aralığın nominal kapsamıyla uyuşmuyor.")
    _, sample_index, props = _samples(samples)
    definitions = _field_definitions(fields)
    partitions = _evaluation_partitions(evaluation_result, sample_index)
    result = {"definitions": definitions, "nominal_coverage": coverage, "min_samples": min_samples, "min_groups": min_groups,
        "per_repeat": [], "outer_folds": [], "independent": [], "status": "available" if definitions and partitions else "unavailable",
        "scope": "fixed evaluated predictions; separate CV repeats; configured nominal interval only; no refit",
        "warnings": [
            "Altgrup sınırları ayarlardan alınır; değerlendirme hatasına göre aranmaz. Sınırlar sonuçlar görülmeden belirlenmelidir.",
            "Her tekrar ayrı özetlenir. Aynı örneğin farklı tekrarlardaki tahminleri bağımsız örnek sayılmaz.",
            "Bağımlılık grubu sayısı, mekânsal bağımsızlığın kanıtı değildir. Eşikler yeterli gücü veya koşullu kapsamayı garanti etmez.",
            "Toplam kapsama, her altgrupta aynı kapsamayı garanti etmez. Buradaki kapsama gözlenen altgrubun tanımlayıcı sonucudur.",
            "Nominal değerin altında kalan kapsama işaretlenir; farkın anlamlılığına ilişkin test veya güven aralığı hesaplanmaz."]}
    if not definitions or not partitions:
        result.update(reason_code="NO_SUBGROUP_DATA", reason="Altgrup alanı veya değerlendirilmiş tahmin bulunmuyor.")
        return result
    for scope, repeat, fold, records in partitions:
        for definition in definitions:
            check_cancel()
            buckets, labels = defaultdict(list), {}
            if "bins" in definition:
                edges = definition["bins"]
                for value in [np.nextafter(edges[0], -np.inf), *edges]:
                    key, label = _category(value, definition)
                    labels[key] = label
            for row in records:
                value = (props[sample_index[row["sample_id"]]] if definition["source"] == "property" else row).get(definition["field"])
                key, label = _category(value, definition)
                buckets[key].append(row)
                labels[key] = label
            for key, label in labels.items():
                result[scope].append({"field": definition["field"], "source": definition["source"], "repeat": repeat, "fold": fold,
                    "subgroup_id": key, **label, **_subgroup_metrics(buckets[key], coverage, min_samples, min_groups)})
    check_cancel()
    return result


def _spatial_summary(records, locations, sample_index, distance_band_m, edges, min_samples, max_samples, min_pairs):
    n = len(records)
    summary = {"n": n, "distinct_sample_count": len({r["sample_id"] for r in records}),
        "group_count": len({r["group_id"] for r in records}), "status": "available"}
    if n < min_samples or n > max_samples:
        summary.update(status="unavailable", reason_code="INSUFFICIENT_SPATIAL_SAMPLES" if n < min_samples else "SPATIAL_SIZE_LIMIT",
            reason="Mekânsal tanı için örnek sayısı belirlenen sınırlar dışında; alt örnekleme yapılmadı.")
        return summary
    records = sorted(records, key=lambda row: row["sample_id"])
    xy = locations[[sample_index[r["sample_id"]] for r in records]]
    residual = np.asarray([r["predicted"] - r["observed"] for r in records], dtype=float)
    left, right = np.triu_indices(n, 1)
    distances = pdist(xy)
    neighbors = distances <= distance_band_m
    degree = np.bincount(np.concatenate([left[neighbors], right[neighbors]]), minlength=n)
    with np.errstate(over="ignore", invalid="ignore"):
        centered = residual - np.mean(residual)
        semivariance = .5 * (residual[left] - residual[right]) ** 2
    if not np.isfinite(centered).all() or not np.isfinite(semivariance).all():
        raise JobError("DIAGNOSTIC_METRIC", "Artıkların mekânsal ölçütleri sonlu hesaplanamadı.")
    # Scale invariance avoids overflow in the Moran numerator/denominator.
    scale = np.max(np.abs(centered))
    if scale > 0:
        centered = centered / scale
    denominator = float(centered @ centered)
    s0 = int(np.sum(degree > 0))
    moran = {"status": "available", "island_count": int(np.sum(degree == 0)), "neighbor_pair_count": int(np.sum(neighbors)),
        "s0": s0, "p_value": None, "p_value_reason": "Bağımsız ve yer değiştirebilir artık varsayımı doğrulanmadığından anlamlılık testi hesaplanmadı."}
    if not s0 or denominator == 0:
        moran.update(status="unavailable", reason_code="NO_NEIGHBORS" if not s0 else "CONSTANT_RESIDUAL",
                     reason="Komşuluk bağı yok." if not s0 else "Artık varyansı sıfır; Moran I tanımsız.")
    else:
        a, b = left[neighbors], right[neighbors]
        numerator = np.sum(centered[a] * centered[b] * (1. / degree[a] + 1. / degree[b]))
        moran["value"] = float(n / s0 * numerator / denominator)
    bins = []
    for index in range(len(edges) - 1):
        mask = (distances >= edges[index]) & ((distances <= edges[index + 1]) if index == len(edges) - 2 else (distances < edges[index + 1]))
        count = int(np.sum(mask))
        row = {"lower_m": float(edges[index]), "upper_m": float(edges[index + 1]), "upper_inclusive": index == len(edges) - 2,
            "pair_count": count, "status": "available" if count >= min_pairs else "unavailable"}
        if count >= min_pairs:
            scale = float(np.max(semivariance[mask])) or 1.
            row.update(mean_distance_m=float(np.mean(distances[mask])), semivariance=float(np.mean(semivariance[mask] / scale) * scale))
        else:
            row.update(reason_code="INSUFFICIENT_PAIRS", reason="Uzaklık sınıfındaki çift sayısı belirlenen eşiğin altında.")
        bins.append(row)
    summary.update(moran_i=moran, semivariogram=bins, total_pair_count=len(distances),
        included_pair_count=sum(row["pair_count"] for row in bins), duplicate_location_pair_count=int(np.sum(distances == 0)),
        within_group_pair_count=int(np.sum(np.asarray([r["group_id"] for r in records], dtype=object)[left] == np.asarray([r["group_id"] for r in records], dtype=object)[right])))
    return summary


def residual_spatial_diagnostics(evaluation_result, samples, *, distance_band_m, bin_edges_m, metric_crs=None,
        min_samples=5, max_samples=2000, min_pairs=5, check_cancel=lambda: None):
    """Descriptive row-standardized Moran I and isotropic Matheron semivariogram.

    W_ij=1 for different rows within the supplied closed distance band, then each
    nonempty row is divided by its neighbor count. Islands have zero weights and
    remain in n and centering. I=(n/S0) sum_ij(W_ij*z_i*z_j)/sum_i(z_i^2).
    Semivariance in a bin is mean((e_i-e_j)^2)/2 over unordered pairs.
    No variogram model is fitted and evaluated residuals are never fed back to fit.
    """
    check_cancel()
    min_samples = _integer(min_samples, 3, "Mekânsal tanı için en az örnek sayısı")
    max_samples = _integer(max_samples, min_samples, "Mekânsal tanı için en çok örnek sayısı")
    min_pairs = _integer(min_pairs, 1, "En az çift sayısı")
    edges, distance_band_m = np.asarray(bin_edges_m, dtype=float), float(distance_band_m)
    if not np.isfinite(distance_band_m) or distance_band_m <= 0 or edges.ndim != 1 or len(edges) < 2 or not np.isfinite(edges).all() or edges[0] < 0 or np.any(np.diff(edges) <= 0):
        raise JobError("DIAGNOSTIC_DISTANCE", "Komşuluk uzaklığı pozitif; uzaklık sınıfları sonlu, negatif olmayan ve kesin artan sırada olmalıdır.")
    ids, sample_index, _ = _samples(samples)
    xy = np.asarray(samples["coordinates"], dtype=float)
    if xy.shape != (len(ids), 2) or not np.isfinite(xy).all():
        raise JobError("DIAGNOSTIC_COORDINATES", "Mekânsal tanı koordinatları sonlu ve iki sütunlu olmalıdır.")
    locations, distance_crs = _metric_coordinates(xy, samples["crs"], {"metric_crs": metric_crs}, True)
    result = {"status": "available", "per_repeat": [], "outer_folds": [], "independent": [],
        "distance_crs": distance_crs, "distance_unit": "metre", "distance_band_m": distance_band_m,
        "bin_edges_m": edges.tolist(), "min_samples": min_samples, "max_samples": max_samples, "min_pairs": min_pairs,
        "neighborhood": "closed_distance_band; distinct rows; duplicate locations retained", "weight_standardization": "row_standardized; islands zero; all rows in n and centering",
        "residual_sign": "prediction_minus_observation", "semivariogram_estimator": "Matheron: mean(squared_pair_difference)/2; unordered pairs; isotropic",
        "warnings": [
            "Artıklar değerlendirilmiş tahminlerden alınır; eğitim veya kalibrasyona geri verilmez.",
            "Moran I, seçilen komşuluk kuralına bağlı tanımlayıcı bir mekânsal otokorelasyon ölçütüdür. Anlamlılık testi hesaplanmadı.",
            "Yarı-variogram yönlerden bağımsız olarak hesaplanır. Durağanlık ve yönlere göre benzerlik doğrulanmadı; bir yarı-variogram modeli uydurulmadı.",
            "Çiftler ve bağımlılık grupları bağımsız tekrar sayılmaz. Eşikler istatistiksel gücü garanti etmez.",
            "Tekrar içindeki dış kat artıkları farklı eğitilmiş modellerden gelir. Birlikte verilen tanı bu karışımı tanımlar.",
            "Komşusu olmayan noktalar sıfır ağırlıkla korunur. Çakışan koordinatlardaki farklı örnekler ayrıca sayılır."]}
    partitions = _evaluation_partitions(evaluation_result, sample_index)
    if not partitions:
        result.update(status="unavailable", reason_code="NO_EVALUATION_ROWS", reason="Mekânsal tanı için değerlendirilmiş tahmin yok.")
    for scope, repeat, fold, records in partitions:
        check_cancel()
        result[scope].append({"repeat": repeat, "fold": fold,
            **_spatial_summary(records, locations, sample_index, distance_band_m, edges, min_samples, max_samples, min_pairs)})
    check_cancel()
    return result
