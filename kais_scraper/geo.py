"""Reproject shapefiles to WGS84 GeoJSON (replaces the mapshaper step)."""
from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path

from .naming import geojson_name
from .paths import Layout
from .util import run_parallel

log = logging.getLogger(__name__)


def geo_available() -> bool:
    return importlib.util.find_spec("geopandas") is not None and importlib.util.find_spec("pyogrio") is not None


def convert_one(shp: Path, out: Path, encoding: str | None, source_crs: str | None) -> None:
    import geopandas as gpd

    kwargs = {"encoding": encoding} if encoding else {}
    gdf = gpd.read_file(shp, engine="pyogrio", **kwargs)
    if gdf.crs is None:
        if not source_crs:
            raise ValueError("no .prj file next to the shapefile and no --source-crs given")
        gdf = gdf.set_crs(source_crs)
    gdf = gdf.to_crs(4326)
    tmp = out.with_name(out.stem + ".tmp.geojson")
    tmp.unlink(missing_ok=True)
    gdf.to_file(tmp, driver="GeoJSON", engine="pyogrio", COORDINATE_PRECISION=6)
    os.replace(tmp, out)


def run_geojson(layout: Layout, workers: int, encoding: str | None, source_crs: str | None) -> int:
    if not geo_available():
        raise RuntimeError("geopandas/pyogrio are not installed; run pip install -r requirements.txt")
    layout.ensure()
    jobs = []
    for shp in sorted(layout.extracted.rglob("*.shp")):
        out = layout.geojson / geojson_name(shp.relative_to(layout.extracted))
        if out.exists() and out.stat().st_mtime >= shp.stat().st_mtime:
            continue
        jobs.append((shp, out, encoding, source_crs))
    log.info("Converting %d changed shapefiles", len(jobs))
    failures = run_parallel(convert_one, jobs, workers=workers, desc="geojson", processes=True)
    return len(failures)
