"""Water-surface elevation points along 100-yr floodplain boundaries.

Replicates the DFIRM_100_p3ft_<County> layers from the ArcGIS workflow:
points along DFIRM 100-yr floodplain boundaries whose elevation is the
2015 LiDAR 1m DEM value plus 3 ft, in meters.
"""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import numpy as np
import pyogrio
import rasterio
import shapely
from rasterio.windows import Window

from . import CRS, PLUS_3FT_M

log = logging.getLogger(__name__)


def boundary_points(
    sfha: gpd.GeoDataFrame,
    spacing: float = 1.0,
    clip_bounds: tuple[float, float, float, float] | None = None,
) -> np.ndarray:
    """Points at `spacing` (m) along the dissolved floodplain boundary.

    Returns an (N, 2) array of x, y in the working CRS.
    """
    dissolved = shapely.union_all(sfha.geometry.values)
    boundary = shapely.boundary(dissolved)
    if clip_bounds is not None:
        boundary = shapely.intersection(boundary, shapely.box(*clip_bounds))
    dense = shapely.segmentize(boundary, spacing)
    xy = shapely.get_coordinates(dense)
    # segmentize keeps original vertices; drop exact duplicates (ring closures)
    xy = np.unique(xy, axis=0)
    log.info("boundary points: %d at %.1f m spacing", len(xy), spacing)
    return xy


def sample_dem(vrt_path: Path, xy: np.ndarray, block: int = 3000) -> np.ndarray:
    """Sample the DEM at xy points via windowed reads (memory-bounded).

    Points are bucketed onto a `block`-meter grid so each bucket needs one
    window read. Returns float64 elevations; NaN where the DEM has no data.
    """
    values = np.full(len(xy), np.nan)
    with rasterio.open(vrt_path) as ds:
        inv = ~ds.transform
        nodata = ds.nodata
        cols, rows = inv * (xy[:, 0], xy[:, 1])
        cols = np.floor(cols).astype(np.int64)
        rows = np.floor(rows).astype(np.int64)
        inside = (cols >= 0) & (cols < ds.width) & (rows >= 0) & (rows < ds.height)

        key = (rows // block) * (ds.width // block + 2) + (cols // block)
        key[~inside] = -1
        order = np.argsort(key, kind="stable")
        sorted_key = key[order]
        starts = np.flatnonzero(np.r_[True, sorted_key[1:] != sorted_key[:-1]])
        starts = np.r_[starts, len(sorted_key)]

        for s, e in zip(starts[:-1], starts[1:]):
            if sorted_key[s] == -1:
                continue
            idx = order[s:e]
            r0, r1 = rows[idx].min(), rows[idx].max()
            c0, c1 = cols[idx].min(), cols[idx].max()
            win = Window(c0, r0, c1 - c0 + 1, r1 - r0 + 1)
            arr = ds.read(1, window=win)
            v = arr[rows[idx] - r0, cols[idx] - c0].astype(np.float64)
            if nodata is not None:
                v[v == nodata] = np.nan
            v[np.abs(v) > 1e30] = np.nan
            values[idx] = v
    return values


def generate_points(
    sfha: gpd.GeoDataFrame,
    vrt_path: Path,
    spacing: float = 1.0,
    clip_bounds: tuple[float, float, float, float] | None = None,
    plus_m: float = PLUS_3FT_M,
) -> tuple[np.ndarray, np.ndarray]:
    """DEM + freeboard points along floodplain boundaries -> (xy, wse_m)."""
    xy = boundary_points(sfha, spacing, clip_bounds)
    dem = sample_dem(vrt_path, xy)
    ok = np.isfinite(dem)
    if ok.sum() < len(xy):
        log.info("dropping %d points with no DEM data", len(xy) - ok.sum())
    xy = xy[ok]
    wse = dem[ok] + plus_m
    log.info("WSE points: %d (elev %.2f..%.2f m)", len(xy), wse.min(), wse.max())
    return xy, wse


def read_points_gdb(
    gdb: Path, layer: str, field: str = "grid_code"
) -> tuple[np.ndarray, np.ndarray]:
    """Read existing DFIRM_100_p3ft points from a file geodatabase."""
    df = pyogrio.read_dataframe(gdb, layer=layer, columns=[field])
    if df.crs is not None and df.crs.to_string() != CRS:
        df = df.to_crs(CRS)
    xy = shapely.get_coordinates(df.geometry.values)
    wse = df[field].to_numpy(dtype=np.float64)
    ok = np.isfinite(wse)
    log.info("read %d points from %s:%s", ok.sum(), gdb, layer)
    return xy[ok], wse[ok]


def save_points(xy: np.ndarray, wse: np.ndarray, path: Path) -> None:
    """Persist WSE points as a GeoPackage layer (grid_code in meters)."""
    gdf = gpd.GeoDataFrame(
        {"grid_code": wse}, geometry=gpd.points_from_xy(xy[:, 0], xy[:, 1]), crs=CRS
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(path, layer="wse_points", driver="GPKG")
    log.info("saved points: %s", path)
