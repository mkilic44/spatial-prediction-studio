"""Read-only samples, aligned GeoTIFF inspection, and explicit nearest-cell extraction."""
from __future__ import annotations

from contextlib import ExitStack
import csv
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from pyproj import CRS, Transformer
import rasterio

from .contract import JobError


def read_json(path):
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        return json.load(stream)


def fingerprint(path, check_cancel=lambda: None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            check_cancel()
            digest.update(chunk)
    return {"path": str(Path(path).resolve()), "size": Path(path).stat().st_size, "sha256": digest.hexdigest()}


def read_samples(spec, check_cancel):
    path = Path(spec["path"])
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            records = list(csv.DictReader(stream))
        crs_input = spec["crs"]
        rows = [(r, [r.get(spec["x_field"]), r.get(spec["y_field"])]) for r in records]
    else:
        obj = read_json(path)
        if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection" or not isinstance(obj.get("features"), list):
            raise JobError("SAMPLE_FORMAT", "GeoJSON bir FeatureCollection olmalıdır.")
        declared_crs = obj.get("crs", {}).get("properties", {}).get("name")
        crs_input = spec.get("crs")
        if crs_input and declared_crs:
            try:
                agree = CRS.from_user_input(crs_input).equals(CRS.from_user_input(declared_crs), ignore_axis_order=True)
            except Exception as exc:
                raise JobError("SAMPLE_CRS", "GeoJSON koordinat sistemi geçersiz: " + str(exc)) from exc
            if not agree:
                raise JobError("SAMPLE_CRS_CONFLICT", "GeoJSON ve istek farklı koordinat sistemleri bildiriyor.")
        if not crs_input:
            crs_input = declared_crs or "EPSG:4326"
        rows = []
        for feature in obj["features"]:
            if not isinstance(feature, dict):
                raise JobError("SAMPLE_GEOMETRY", "Geçersiz GeoJSON kaydı.")
            geometry = feature.get("geometry") or {}
            if geometry.get("type") != "Point":
                raise JobError("SAMPLE_GEOMETRY", "Aşama A yalnız Point geometrisini destekler.")
            rows.append((feature.get("properties") or {}, geometry.get("coordinates")))
    try:
        crs = CRS.from_user_input(crs_input)
    except Exception as exc:
        raise JobError("SAMPLE_CRS", "Örneklerin koordinat sistemi geçersiz: " + str(exc)) from exc
    if not 2 <= len(rows) <= 1_000_000:
        raise JobError("SAMPLE_COUNT", "Aşama A için 2-1000000 gözlem gereklidir.")
    ids, targets, coords = [], [], []
    seen_ids, seen_coords = set(), set()
    for properties, coordinates in rows:
        check_cancel()
        identity = properties.get(spec["id_field"])
        if identity is None or isinstance(identity, (dict, list, bool)) or not str(identity).strip():
            raise JobError("MISSING_ID", "Her gözlem için dolu, tekil kimlik gereklidir.")
        identity = str(identity).strip()
        if identity in seen_ids:
            raise JobError("DUPLICATE_ID", "Yinelenen gözlem kimliği: " + identity)
        seen_ids.add(identity)
        if spec.get("unit_field") and properties.get(spec["unit_field"]) != spec["unit"]:
            raise JobError("TARGET_UNIT_MISMATCH", "Gözlem birimi hedef birimiyle uyuşmuyor. Gözlem: " + identity)
        value = properties.get(spec["target_field"])
        try:
            if value is None or isinstance(value, bool):
                raise ValueError()
            value = float(value)
            if not math.isfinite(value):
                raise ValueError()
        except (ValueError, TypeError) as exc:
            raise JobError("MISSING_TARGET", "Eksik veya sonlu olmayan hedef doldurulmaz. Gözlem: " + identity) from exc
        try:
            if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
                raise ValueError()
            point = tuple(float(v) for v in coordinates[:2])
            if not all(math.isfinite(v) for v in point):
                raise ValueError()
        except (ValueError, TypeError) as exc:
            raise JobError("SAMPLE_GEOMETRY", "Geçersiz koordinat. Gözlem: " + identity) from exc
        if point in seen_coords:
            raise JobError("DUPLICATE_COORDINATE", "Yinelenen konum Aşama A'da desteklenmiyor. Gözlem: " + identity)
        seen_coords.add(point)
        ids.append(identity)
        targets.append(value)
        coords.append(point)
    return {"ids": ids, "y": np.asarray(targets, dtype=np.float64), "coordinates": np.asarray(coords, dtype=np.float64), "crs": crs}


class RasterInputs:
    def __init__(self, covariates):
        self.covariates = covariates
        self.stack = ExitStack()
        self.datasets = []

    def __enter__(self):
        try:
            by_path = {}
            for cov in self.covariates:
                if cov["path"] not in by_path:
                    by_path[cov["path"]] = self.stack.enter_context(rasterio.open(cov["path"], "r"))
                src = by_path[cov["path"]]
                if src.driver != "GTiff" or src.crs is None:
                    raise JobError("RASTER_CRS", "Koordinat sistemi tanımlı GeoTIFF gereklidir.")
                if cov["band"] > src.count:
                    raise JobError("BAND_RANGE", "Rasterde bulunmayan bant: " + str(cov["band"]))
                if src.scales[cov["band"] - 1] != 1 or src.offsets[cov["band"] - 1] != 0:
                    raise JobError("RASTER_SCALE", "Ölçek/öteleme üstverisi olan rasterler Aşama A'da desteklenmiyor. Fiziksel değerlere dönüştürülmüş çalışma kopyası gereklidir.")
                if np.issubdtype(np.dtype(src.dtypes[cov["band"] - 1]), np.complexfloating):
                    raise JobError("RASTER_TYPE", "Karmaşık sayılı rasterler desteklenmiyor.")
                if self.datasets:
                    reference = self.datasets[0]
                    if (src.width, src.height, src.crs) != (reference.width, reference.height, reference.crs) or not np.allclose(tuple(src.transform), tuple(reference.transform), rtol=0, atol=1e-9):
                        raise JobError("GRID_MISMATCH", "Raster gridleri aynı değil. Aşama A otomatik yeniden örnekleme yapmaz.")
                if cov.get("band_description") is not None and src.descriptions[cov["band"] - 1] != cov["band_description"]:
                    raise JobError("BAND_SCHEMA", "Bant açıklaması kayıtlı şemadan farklı.")
                actual_unit = src.units[cov["band"] - 1]
                if actual_unit and actual_unit.strip() != cov["unit"]:
                    raise JobError("COVARIATE_UNIT", "Raster bandının birimi değişken şemasıyla uyuşmuyor.")
                self.datasets.append(src)
            return self
        except Exception:
            self.stack.close()
            raise

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    @property
    def reference(self):
        return self.datasets[0]

    def source_paths(self):
        return sorted({str(Path(p).resolve()) for src in self.datasets for p in src.files})

    def extract(self, samples, check_cancel):
        src = self.reference
        try:
            transform = Transformer.from_crs(samples["crs"], src.crs, always_xy=True)
            x, y = transform.transform(samples["coordinates"][:, 0], samples["coordinates"][:, 1], errcheck=True)
        except Exception as exc:
            raise JobError("SAMPLE_TRANSFORM", "Örnek koordinatları raster koordinat sistemine dönüştürülemedi: " + str(exc)) from exc
        coords = np.column_stack((x, y))
        for identity, (px, py) in zip(samples["ids"], coords):
            check_cancel()
            row, col = src.index(px, py)
            if row < 0 or col < 0 or row >= src.height or col >= src.width:
                raise JobError("SAMPLE_OUTSIDE", "Raster kapsamı dışında gözlem: " + identity)
        columns = []
        for cov, dataset in zip(self.covariates, self.datasets):
            check_cancel()
            values = []
            for identity, value in zip(samples["ids"], dataset.sample(coords, indexes=cov["band"], masked=True)):
                check_cancel()
                if np.ma.getmaskarray(value).any() or not np.isfinite(value).all():
                    raise JobError("SAMPLE_NODATA", "Eksik çevresel değer bulunan gözlem: " + identity + "; değişken: " + cov["id"])
                values.append(float(value[0]))
            columns.append(values)
        return np.asarray(columns, dtype=np.float64).T, coords

    def read_window(self, window):
        arrays = [src.read(cov["band"], window=window, masked=True, out_dtype="float64")
                  for cov, src in zip(self.covariates, self.datasets)]
        valid = np.ones(arrays[0].shape, dtype=bool)
        for arr in arrays:
            valid &= ~np.ma.getmaskarray(arr) & np.isfinite(arr.data)
        values = np.stack([a.data for a in arrays], axis=-1)
        return values, valid
