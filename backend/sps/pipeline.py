"""Training-only covariate transformations with raw-variable lineage.

Selectors operate on whole input variables, never on a single category dummy.
Sequential selection refits this complete workflow in each selection-training
fold; its held-out rows cannot affect imputation, scaling, VIF or target maps.
"""
from __future__ import annotations

from copy import deepcopy
import numpy as np
from scipy.stats import spearmanr
from sklearn.decomposition import PCA
from sklearn.feature_selection import mutual_info_regression
from sklearn.impute import KNNImputer, SimpleImputer
from sklearn.linear_model import ElasticNet
from sklearn.preprocessing import OneHotEncoder, RobustScaler, StandardScaler

from .contract import JobError


def audit(log, role, stage, ids, **details):
    log.append({"role": role, "stage": stage, "ids": list(ids), **details})


class CovariateEncoder:
    """Dense numeric plus full one-hot design; missing categories are rejected."""

    def __init__(self, covariates, config):
        self.covariates = deepcopy(covariates)
        self.config = deepcopy(config)
        self.numeric = [i for i, c in enumerate(covariates) if c.get("kind", "continuous") == "continuous"]
        self.categorical = [i for i, c in enumerate(covariates) if c.get("kind") == "categorical"]

    def fit(self, X):
        X = np.asarray(X, dtype=float)
        if X.ndim != 2 or X.shape[1] != len(self.covariates) or np.isinf(X).any():
            raise JobError("FEATURE_MATRIX", "Açıklayıcı değişken matrisi geçersiz.")
        self._check_budget(len(X), len(self.covariates))
        self.parents = []
        self.discrete = []
        self.ranges = {}
        self.imputer = None
        self.scaler = None
        self.encoder = None
        if self.numeric:
            values = X[:, self.numeric]
            if np.isnan(values).all(axis=0).any():
                raise JobError("EMPTY_COVARIATE", "Eğitim katında tümü eksik açıklayıcı değişken var.")
            self.ranges = {i: (float(np.nanmin(X[:, i])), float(np.nanmax(X[:, i]))) for i in self.numeric}
            method = self.config.get("imputation", "median")
            if method == "median":
                self.imputer = SimpleImputer(strategy="median", keep_empty_features=True).fit(values)
            elif method == "knn":
                self.imputer = KNNImputer(n_neighbors=self.config.get("knn_neighbors", 5), keep_empty_features=True).fit(values)
            elif method != "none":
                raise JobError("IMPUTATION", "Desteklenmeyen eksik değer yöntemi.")
            elif np.isnan(values).any():
                raise JobError("MISSING_COVARIATE", "Eksik değer giderme kapalıyken eksik açıklayıcı değişken var.")
            filled = self.imputer.transform(values) if self.imputer else values
            scaling = self.config.get("scaling", "standard")
            if scaling == "standard":
                self.scaler = StandardScaler().fit(filled)
            elif scaling == "robust":
                self.scaler = RobustScaler().fit(filled)
            elif scaling != "none":
                raise JobError("SCALING", "Desteklenmeyen ölçekleme yöntemi.")
            self.parents.extend(self.numeric)
            self.discrete.extend([False] * len(self.numeric))
        if self.categorical:
            values = X[:, self.categorical]
            if not np.isfinite(values).all():
                raise JobError("MISSING_CATEGORY", "Eksik kategori kodları sayısal yöntemle doldurulamaz.")
            self.encoder = OneHotEncoder(sparse_output=False, handle_unknown="ignore").fit(values)
            for index, categories in zip(self.categorical, self.encoder.categories_):
                self.parents.extend([index] * len(categories))
                self.discrete.extend([True] * len(categories))
        self._check_budget(len(X), len(self.parents))
        return self

    @staticmethod
    def _check_budget(rows, columns):
        if int(rows) * int(columns) * 8 > 256 * 1024 * 1024:
            raise JobError("MEMORY_BUDGET", "Gösterge matrisi 256 MiB bellek sınırını aşıyor; kategori veya pencere sayısını azaltın.")

    def record(self):
        return {"numeric_ids": [self.covariates[i]["id"] for i in self.numeric],
            "categorical_ids": [self.covariates[i]["id"] for i in self.categorical],
            "categories": [c.tolist() for c in self.encoder.categories_] if self.encoder else [],
            "training_ranges": {self.covariates[i]["id"]: list(bounds) for i, bounds in self.ranges.items()},
            "imputation": self.config.get("imputation", "median"),
            "imputation_statistics": self.imputer.statistics_.tolist() if hasattr(self.imputer, "statistics_") else None,
            "scaling": self.config.get("scaling", "standard"),
            "scale": self.scaler.scale_.tolist() if self.scaler else None,
            "center": (self.scaler.mean_ if hasattr(self.scaler, "mean_") else self.scaler.center_).tolist() if self.scaler else None}

    def diagnostics(self, X):
        X = np.asarray(X, dtype=float)
        if X.ndim != 2 or X.shape[1] != len(self.covariates) or np.isinf(X).any():
            raise JobError("FEATURE_MATRIX", "Tahmin değişkenlerinin sayısı veya değerleri geçersiz.")
        unknown = np.zeros(len(X), dtype=bool)
        if self.encoder:
            for index, categories in zip(self.categorical, self.encoder.categories_):
                unknown |= ~np.isin(X[:, index], categories)
        extrapolation = np.zeros(len(X), dtype=bool)
        for index, (low, high) in self.ranges.items():
            extrapolation |= (X[:, index] < low) | (X[:, index] > high)
        return {"unknown_category": unknown, "range_extrapolation": extrapolation}

    def transform(self, X):
        X = np.asarray(X, dtype=float)
        self._check_budget(len(X), len(self.parents))
        diagnostics = self.diagnostics(X)
        if diagnostics["unknown_category"].any() and self.config.get("unknown_category", "error") == "error":
            raise JobError("UNKNOWN_CATEGORY", "Eğitimde görülmeyen kategori bulundu.")
        columns = []
        if self.numeric:
            values = X[:, self.numeric]
            if self.imputer:
                values = self.imputer.transform(values)
            if not np.isfinite(values).all():
                raise JobError("MISSING_COVARIATE", "Tahminde giderilemeyen eksik değer var.")
            columns.append(self.scaler.transform(values) if self.scaler else values)
        if self.categorical:
            if not np.isfinite(X[:, self.categorical]).all():
                raise JobError("MISSING_CATEGORY", "Tahminde eksik kategori kodu var.")
            columns.append(self.encoder.transform(X[:, self.categorical]))
        return np.column_stack(columns)


def _vif(values):
    values = np.asarray(values, dtype=float)
    centered = values - values.mean(axis=0)
    scores = []
    for j in range(values.shape[1]):
        y = centered[:, j]
        tss = float(y @ y)
        others = np.delete(centered, j, axis=1)
        if tss <= np.finfo(float).eps:
            scores.append(float("inf"))
        elif others.shape[1] == 0:
            scores.append(1.)
        else:
            residual = y - others @ np.linalg.lstsq(others, y, rcond=None)[0]
            rss = float(residual @ residual)
            scores.append(float("inf") if rss <= 1e-12 * tss else max(1., tss / rss))
    return scores


def _drop_choice(candidates, scores, covariates, protected, priority):
    removable = [i for i in candidates if covariates[i]["id"] not in protected]
    if not removable:
        raise JobError("PROTECTED_COLLINEARITY", "Korunan değişkenler nedeniyle doğrusal bağımlılık giderilemiyor.")
    priorities = {name: i for i, name in enumerate(priority)}
    return min(removable, key=lambda i: (priorities.get(covariates[i]["id"], len(priorities)), -scores.get(i, 0.), covariates[i]["id"]))


class FeaturePipeline:
    def __init__(self, covariates, settings):
        self.covariates = deepcopy(covariates)
        self.settings = deepcopy(settings)

    def fit(self, X, y, ids, *, seed, role, learning_audit, initial=None, check_cancel=lambda: None):
        from .modeling import make_point_estimator
        X = np.asarray(X, dtype=float)
        self.initial = list(range(X.shape[1])) if initial is None else list(initial)
        self.encoder = CovariateEncoder([self.covariates[i] for i in self.initial], self.settings.get("preprocessing", {})).fit(X[:, self.initial])
        design = self.encoder.transform(X[:, self.initial])
        parents = np.array([self.initial[i] for i in self.encoder.parents])
        active = list(self.initial)
        record = {"vif": [], "selection_method": self.settings.get("feature_selection", {}).get("method", "none")}
        record["selection_preprocessing"] = self.encoder.record()
        audit(learning_audit, role, "preprocessing", ids, features=[self.covariates[i]["id"] for i in active])
        vif = self.settings.get("vif", {})
        mode = vif.get("mode", "off")
        if mode != "off":
            numeric = [i for i in active if self.covariates[i].get("kind", "continuous") == "continuous"]
            numeric_columns = [int(np.flatnonzero(parents == i)[0]) for i in numeric]
            record["vif_diagnostics"] = {"scope": "continuous variables only; categorical groups not assessed", "n": len(X), "p": len(numeric),
                "p_at_least_n": len(numeric) >= len(X), "centered_rank": int(np.linalg.matrix_rank(design[:, numeric_columns] - design[:, numeric_columns].mean(axis=0))) if numeric else 0}
            while numeric:
                check_cancel()
                columns = [int(np.flatnonzero(parents == i)[0]) for i in numeric]
                scores = dict(zip(numeric, _vif(design[:, columns])))
                record["vif"].append({self.covariates[i]["id"]: {"value": v if np.isfinite(v) else None, "singular": not np.isfinite(v)} for i, v in scores.items()})
                high = [i for i in numeric if scores[i] > vif.get("threshold", 10.)]
                if mode != "iterative" or not high:
                    break
                dropped = _drop_choice(high, scores, self.covariates, vif.get("protected_ids", []), vif.get("priority_ids", []))
                numeric.remove(dropped)
                active.remove(dropped)
            audit(learning_audit, role, "vif", ids)
        selector = self.settings.get("feature_selection", {})
        method = selector.get("method", "none")
        protected = set(selector.get("protected_ids", [])) | set(vif.get("protected_ids", []))
        selected = list(active)
        k = int(selector.get("k", 1))
        if not selected:
            raise JobError("NO_FEATURES", "Ön işlemeden sonra açıklayıcı değişken kalmadı.")
        if method in ("pearson", "spearman"):
            numeric = [i for i in selected if self.covariates[i].get("kind", "continuous") == "continuous"]
            while len(numeric) > 1:
                columns = [int(np.flatnonzero(parents == i)[0]) for i in numeric]
                matrix = design[:, columns]
                if method == "spearman":
                    from scipy.stats import rankdata
                    matrix = np.apply_along_axis(rankdata, 0, matrix)
                with np.errstate(invalid="ignore", divide="ignore"):
                    corr = np.corrcoef(matrix, rowvar=False)
                pairs = [(numeric[a], numeric[b], abs(float(corr[a, b]))) for a in range(len(numeric)) for b in range(a + 1, len(numeric)) if abs(corr[a, b]) > selector.get("threshold", .9)]
                if not pairs:
                    break
                a, b, _ = max(pairs, key=lambda p: (p[2], -p[0], -p[1]))
                dropped = _drop_choice([a, b], {}, self.covariates, protected, selector.get("priority_ids", []))
                numeric.remove(dropped)
                selected.remove(dropped)
        elif method in ("mutual_info", "elastic_net", "rfe", "boruta"):
            if not 1 <= k <= len(active) and method != "boruta":
                raise JobError("FEATURE_COUNT", "Seçilecek değişken sayısı eğitimdeki değişken sayısını aşıyor.")
            if len([i for i in active if self.covariates[i]["id"] in protected]) > k and method != "boruta":
                raise JobError("FEATURE_COUNT", "Korunan değişken sayısı seçilecek sayıdan büyük.")
            columns = np.flatnonzero(np.isin(parents, active))
            subset = design[:, columns]
            if method == "mutual_info":
                # Jitter and estimator randomness are training-local and seeded.
                importance = mutual_info_regression(subset, y, discrete_features=np.asarray(self.encoder.discrete)[columns], random_state=seed)
            elif method == "elastic_net":
                importance = np.abs(ElasticNet(alpha=.01, l1_ratio=.5, max_iter=10000, random_state=seed).fit(StandardScaler().fit_transform(subset), y).coef_)
                record["selector_estimator"] = {"id": "elastic_net", "alpha": .01, "l1_ratio": .5, "purpose": "fixed_training_only_ranking"}
            elif method == "boruta":
                from boruta import BorutaPy
                from sklearn.ensemble import RandomForestRegressor
                estimator = RandomForestRegressor(n_estimators=100, max_depth=5, random_state=seed, n_jobs=1)
                boruta = BorutaPy(estimator, n_estimators="auto", max_iter=30, random_state=seed).fit(subset, np.asarray(y))
                importance = boruta.support_.astype(float)
                record["tentative_features"] = sorted({self.covariates[int(parents[columns[j]])]["id"] for j in np.flatnonzero(boruta.support_weak_)})
                record["selector_purpose"] = "all_relevant_confirmed_only"
            else:
                remaining = list(active)
                while len(remaining) > k:
                    check_cancel()
                    cols = np.flatnonzero(np.isin(parents, remaining))
                    estimator = make_point_estimator(self.settings["model"], seed)
                    estimator.fit(design[:, cols], y)
                    importance = getattr(estimator, "feature_importances_", getattr(estimator, "coef_", None))
                    if importance is None:
                        raise JobError("RFE_MODEL", "Bu model özyinelemeli değişken eleme için katsayı veya önem sağlamıyor.")
                    values = np.abs(np.asarray(importance)).reshape(-1)
                    scores = {i: float(values[parents[cols] == i].sum()) for i in remaining}
                    removable = [i for i in remaining if self.covariates[i]["id"] not in protected]
                    remaining.remove(min(removable, key=lambda i: (scores[i], self.covariates[i]["id"])))
                selected = remaining
                importance = None
            if importance is not None:
                scores = {i: float(np.max(np.asarray(importance)[parents[columns] == i])) for i in active}
                if method == "boruta":
                    selected = [i for i in active if scores[i] > 0 or self.covariates[i]["id"] in protected]
                else:
                    ranked = sorted(active, key=lambda i: (self.covariates[i]["id"] not in protected, -scores[i], self.covariates[i]["id"]))
                    if method == "elastic_net":
                        ranked = [i for i in ranked if scores[i] > 1e-12 or self.covariates[i]["id"] in protected]
                        record["selector_purpose"] = "nonzero_elastic_net_coefficients_capped_by_k"
                    selected = sorted(ranked[:k])
                record["selection_scores"] = {self.covariates[i]["id"]: value for i, value in scores.items()}
        elif method not in ("none", "sfs", "pca"):
            raise JobError("FEATURE_SELECTION", "Desteklenmeyen değişken seçme yöntemi.")
        if not selected:
            raise JobError("NO_SELECTED_FEATURES", "Değişken seçimi hiçbir değişkeni doğrulamadı.")
        # Refit on retained raw variables. In particular, KNN distances may not
        # keep depending on discarded variables through the imputation stage.
        record["final_vif"] = []
        while True:
            self.encoder = CovariateEncoder([self.covariates[i] for i in selected], self.settings.get("preprocessing", {})).fit(X[:, selected])
            design = self.encoder.transform(X[:, selected])
            parents = np.array([selected[i] for i in self.encoder.parents])
            numeric = [i for i in selected if self.covariates[i].get("kind", "continuous") == "continuous"]
            if mode == "off" or not numeric:
                break
            columns = [int(np.flatnonzero(parents == i)[0]) for i in numeric]
            scores = dict(zip(numeric, _vif(design[:, columns])))
            record["final_vif"].append({self.covariates[i]["id"]: {"value": value if np.isfinite(value) else None, "singular": not np.isfinite(value)} for i, value in scores.items()})
            high = [i for i in numeric if scores[i] > vif.get("threshold", 10)]
            if mode != "iterative" or not high:
                break
            dropped = _drop_choice(high, scores, self.covariates, protected, vif.get("priority_ids", []))
            selected.remove(dropped)
            if not selected:
                raise JobError("NO_FEATURES", "Son değişkenin doğrusal bağımlılığı giderilemedi.")
            check_cancel()
        self.initial = list(selected)
        self.columns = np.arange(design.shape[1])
        record["preprocessing"] = self.encoder.record()
        audit(learning_audit, role, "final_preprocessing", ids, features=[self.covariates[i]["id"] for i in selected])
        self.selected = selected
        self.pca = None
        if method == "pca":
            if protected:
                raise JobError("PCA_PROTECTED", "Temel bileşenlerde ham değişken koruma kuralı uygulanamaz.")
            if not 1 <= k <= min(len(X), len(self.columns)):
                raise JobError("PCA_COMPONENTS", "Temel bileşen sayısı matris boyutunu aşıyor.")
            self.pca = PCA(n_components=k, svd_solver="full").fit(design[:, self.columns])
            record["pca"] = {"purpose": "transformation_not_variable_selection", "components": self.pca.components_.tolist(), "explained_variance_ratio": self.pca.explained_variance_ratio_.tolist()}
        record["selected_features"] = [self.covariates[i]["id"] for i in selected]
        record["encoded_features"] = [{"source_id": self.covariates[int(parents[c])]["id"], "column": int(c)} for c in self.columns]
        self.record = record
        audit(learning_audit, role, "feature_selection", ids, method=method, selected_features=record["selected_features"])
        return self

    def transform(self, X):
        design = self.encoder.transform(np.asarray(X)[:, self.initial])[:, self.columns]
        return self.pca.transform(design) if self.pca else design

    def diagnostics(self, X):
        return self.encoder.diagnostics(np.asarray(X)[:, self.initial])


def sequential_selection(X, y, ids, coordinates, groups, covariates, settings, *, crs, seed, role, learning_audit, check_cancel):
    from .modeling import fit_workflow
    from .validation import make_folds, indices_for
    selector = settings["feature_selection"]
    count = int(selector.get("k", 1))
    protected = set(selector.get("protected_ids", [])) | set(settings.get("vif", {}).get("protected_ids", []))
    selected = [i for i, c in enumerate(covariates) if c["id"] in protected]
    if not max(1, len(selected)) <= count <= X.shape[1]:
        raise JobError("FEATURE_COUNT", "Sıralı seçimde değişken sayısı geçersiz.")
    selector_validation = deepcopy(settings["validation"])
    if selector_validation.get('method') == 'temporal':
        selector_validation['inner_initial_train_periods'] = selector_validation.get('selector_initial_train_periods', 1)
    inner = make_folds(ids, coordinates, groups, selector_validation, crs=crs, inner=True,check_cancel=check_cancel)
    child = deepcopy(settings)
    child["feature_selection"] = {"method": "none"}
    child["uncertainty"] = {"method": "none"}
    while len(selected) < count:
        losses = []
        for candidate in range(X.shape[1]):
            if candidate in selected:
                continue
            columns = sorted(selected + [candidate])
            allowed = {covariates[i]["id"] for i in columns}
            current = deepcopy(child)
            for key in ("protected_ids", "priority_ids"):
                current.setdefault("vif", {})[key] = [v for v in settings.get("vif", {}).get(key, []) if v in allowed]
            errors = []
            for fold in inner:
                check_cancel()
                train = indices_for(ids, fold["train_ids"])
                test = indices_for(ids, fold["test_ids"])
                fit = fit_workflow(X[train][:, columns], y[train], [ids[i] for i in train], coordinates[train], groups[train], [covariates[i] for i in columns], current,
                    crs=crs, role=role + "/sfs/%s/%s" % (candidate, fold["fold"]), learning_audit=learning_audit, check_cancel=check_cancel)
                errors.extend((fit.predict(X[test][:, columns]) - y[test]).tolist())
            loss = float(np.mean(np.abs(errors))) if settings.get("optimization", {}).get("metric") == "mae" else float(np.sqrt(np.mean(np.square(errors))))
            losses.append((loss, covariates[candidate]["id"], candidate))
        selected.append(min(losses)[2])
        audit(learning_audit, role, "sfs_step", ids, candidates=[{"feature": name, "loss": loss} for loss, name, _ in losses],
            selected_features=[covariates[i]["id"] for i in selected])
    audit(learning_audit, role, "sfs_selection", ids, selected_features=[covariates[i]["id"] for i in selected])
    return sorted(selected)
