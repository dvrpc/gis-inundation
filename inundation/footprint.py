"""Dissolve per-tile inundation polygons and write the footprint GeoPackage.

Replaces ArcGIS Int(raster*10) -> Reclassify -> Raster to Polygon: we
polygonize the depth > 0 mask directly, then dissolve.
"""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely

from . import CRS

log = logging.getLogger(__name__)


def write_footprint(wkb_polys: list[bytes], out_gpkg: Path) -> gpd.GeoDataFrame:
    """Dissolve raw polygons (which tile edges may split) and write GPKG."""
    if not wkb_polys:
        raise RuntimeError("no inundated area found; nothing to write")

    geoms = shapely.from_wkb(np.array(wkb_polys, dtype=object))
    log.info("dissolving %d raw polygons", len(geoms))
    try:
        # polygons form a valid coverage (grid-derived, non-overlapping)
        dissolved = shapely.coverage_union_all(geoms)
    except shapely.errors.GEOSException:
        dissolved = shapely.union_all(geoms)

    parts = np.asarray(
        [g for g in shapely.get_parts(dissolved) if not shapely.is_empty(g)]
    )
    gdf = gpd.GeoDataFrame({"geometry": parts}, crs=CRS)
    gdf["area_sqm"] = gdf.geometry.area
    gdf = gdf.sort_values("area_sqm", ascending=False).reset_index(drop=True)

    out_gpkg.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(out_gpkg, layer="inundation_footprint", driver="GPKG")
    log.info(
        "footprint: %d polygons, %.2f km^2 -> %s",
        len(gdf),
        gdf["area_sqm"].sum() / 1e6,
        out_gpkg,
    )
    return gdf
