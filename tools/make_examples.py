"""Generate deterministic artificial data; these are not field observations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from pyproj import Transformer
import rasterio
from rasterio.transform import from_origin, xy


def make_examples(destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    paths = [destination / "covariates.tif", destination / "samples.geojson"]
    if any(p.exists() for p in paths):
        raise FileExistsError("Example inputs already exist; choose an empty destination.")
    rows, cols = np.mgrid[0:96, 0:128]
    first = (300 + rows * 0.6 + cols * 0.3).astype("float32")
    second = (10 + 2 * np.sin(cols / 14.0) + rows * 0.04).astype("float32")
    mask = (rows < 5) & (cols < 8)
    data = np.stack((first, second))
    data[:, mask] = -9999
    transform = from_origin(500000, 4200000, 30, 30)
    with rasterio.open(paths[0], "w", driver="GTiff", width=128, height=96, count=2, dtype="float32",
                       transform=transform, crs="EPSG:32637", nodata=-9999, compress="deflate",
                       tiled=True, blockxsize=32, blockysize=32) as dst:
        dst.write(data)
        dst.set_band_description(1, "synthetic_elevation")
        dst.set_band_description(2, "synthetic_covariate")
        dst.set_band_unit(1, "m")
        dst.set_band_unit(2, "1")
        dst.update_tags(data_origin="synthetic_integration_example", seed="20261017")
    to_lonlat = Transformer.from_crs("EPSG:32637", "EPSG:4326", always_xy=True)
    rng = np.random.default_rng(20261017)
    features = []
    for i, (r, c) in enumerate((r, c) for r in range(12, 90, 12) for c in range(12, 126, 14)):
        x, y = xy(transform, r, c)
        lon, lat = to_lonlat.transform(x, y)
        target = 2 + 0.025 * float(first[r, c]) + 0.4 * float(second[r, c]) + rng.normal(0, 0.1)
        features.append({"type": "Feature", "properties": {"sample_id": "S%03d" % (i + 1),
                         "soil_value": round(target, 6), "target_unit": "g/kg", "origin": "synthetic"},
                         "geometry": {"type": "Point", "coordinates": [round(lon, 10), round(lat, 10)]}})
    paths[1].write_text(json.dumps({"type": "FeatureCollection", "name": "Synthetic Stage A samples",
                                  "features": features}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"samples": str(paths[1]), "covariates": str(paths[0]), "sample_count": len(features), "crs": "EPSG:4326"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(make_examples(arguments.output), ensure_ascii=True))
