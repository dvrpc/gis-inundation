"""Tiled water-surface interpolation and depth raster generation.

Replaces ArcGIS "Topo to Raster" + Raster Calculator
`Con(DEM <= WSE, WSE - DEM)`. The water surface is a TIN-style linear
interpolation (scipy LinearNDInterpolator over a Delaunay triangulation)
evaluated tile-by-tile so memory stays bounded at 1 m resolution.

Each work tile is interpolated from the WSE points falling within the tile
plus a surrounding buffer; adjacent tiles overlap heavily in their point
sets, so seams are negligible near the floodplain where points are dense.
"""

from __future__ import annotations

import logging
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from pathlib import Path

import numpy as np
import rasterio
import shapely
from rasterio.features import geometry_mask
from rasterio.features import shapes as rio_shapes
from rasterio.transform import from_origin
from rasterio.windows import Window, from_bounds
from scipy.interpolate import LinearNDInterpolator
from scipy.spatial import QhullError
from shapely.geometry import shape as shapely_shape

from . import CRS, DEPTH_NODATA

log = logging.getLogger(__name__)

Bounds = tuple[float, float, float, float]

# per-worker mask geometry, set once by the pool initializer (equivalent of
# the ArcGIS "Mask" environment setting)
_MASK: dict = {"geom": None}


def _init_worker(mask_wkb: bytes | None) -> None:
    if mask_wkb is not None:
        _MASK["geom"] = shapely.from_wkb(mask_wkb)


def _process_tile(
    vrt_path: str, bounds: Bounds, xy: np.ndarray, wse: np.ndarray
) -> tuple[Bounds, np.ndarray | None, list[bytes]]:
    """Interpolate WSE over one tile, compute depth, polygonize depth > 0.

    Returns (bounds, float32 depth array with DEPTH_NODATA fill, WKB polygons).
    """
    with rasterio.open(vrt_path) as ds:
        win = from_bounds(*bounds, transform=ds.transform)
        win = Window(
            round(win.col_off), round(win.row_off), round(win.width), round(win.height)
        )
        if win.width <= 0 or win.height <= 0:
            return bounds, None, []
        dem = ds.read(1, window=win, boundless=True, fill_value=ds.nodata or np.nan)
        nodata = ds.nodata
        t = ds.window_transform(win)

    dem = dem.astype(np.float64)
    if nodata is not None:
        dem[dem == nodata] = np.nan
    dem[np.abs(dem) > 1e30] = np.nan

    try:
        interp = LinearNDInterpolator(xy, wse)
    except (QhullError, ValueError):
        return bounds, None, []

    xs = t.c + t.a * (np.arange(win.width) + 0.5)
    ys = t.f + t.e * (np.arange(win.height) + 0.5)
    gx, gy = np.meshgrid(xs, ys)
    surf = interp(gx, gy)

    depth = surf - dem
    valid = np.isfinite(surf) & np.isfinite(dem) & (dem <= surf)
    if _MASK["geom"] is not None:
        valid &= geometry_mask(
            [_MASK["geom"]], out_shape=dem.shape, transform=t, invert=True
        )
    if not valid.any():
        return bounds, None, []

    out = np.full(dem.shape, DEPTH_NODATA, dtype=np.float32)
    out[valid] = depth[valid].astype(np.float32)

    wet = (valid & (depth > 0)).astype(np.uint8)
    polys = [
        shapely.to_wkb(shapely_shape(geom))
        for geom, val in rio_shapes(wet, mask=wet.astype(bool), transform=t)
        if val == 1
    ]
    return bounds, out, polys


def _bucket_points(
    xy: np.ndarray, tiles: list[Bounds], buffer: float
) -> list[np.ndarray]:
    """Index arrays of points within each tile's buffered bounds."""
    out = []
    x, y = xy[:, 0], xy[:, 1]
    for minx, miny, maxx, maxy in tiles:
        m = (
            (x >= minx - buffer)
            & (x <= maxx + buffer)
            & (y >= miny - buffer)
            & (y <= maxy + buffer)
        )
        out.append(np.flatnonzero(m).astype(np.int32))
    return out


def run_surface(
    xy: np.ndarray,
    wse: np.ndarray,
    vrt_path: Path,
    tiles: list[Bounds],
    depth_tif: Path,
    point_buffer: float = 1500.0,
    workers: int = 4,
    mask_wkb: bytes | None = None,
) -> list[bytes]:
    """Write the depth GeoTIFF over all work tiles; return footprint WKB polys."""
    minx = min(b[0] for b in tiles)
    miny = min(b[1] for b in tiles)
    maxx = max(b[2] for b in tiles)
    maxy = max(b[3] for b in tiles)
    transform = from_origin(minx, maxy, 1.0, 1.0)
    width, height = round(maxx - minx), round(maxy - miny)

    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "count": 1,
        "width": width,
        "height": height,
        "crs": CRS,
        "transform": transform,
        "nodata": DEPTH_NODATA,
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "compress": "deflate",
        "predictor": 3,
        "bigtiff": "if_safer",
        "sparse_ok": True,
    }

    idx_per_tile = _bucket_points(xy, tiles, point_buffer)
    order = [i for i in range(len(tiles)) if len(idx_per_tile[i]) >= 4]
    log.info(
        "processing %d/%d tiles (%d skipped: too few nearby points)",
        len(order),
        len(tiles),
        len(tiles) - len(order),
    )

    all_polys: list[bytes] = []
    depth_tif.parent.mkdir(parents=True, exist_ok=True)
    total = len(order)
    pending = iter(order)
    with rasterio.open(depth_tif, "w", **profile) as dst:
        with ProcessPoolExecutor(
            max_workers=workers, initializer=_init_worker, initargs=(mask_wkb,)
        ) as pool:
            futs: dict = {}

            def submit(i: int) -> None:
                # copy out the subset, then drop the index array: only the
                # in-flight tasks (bounded below) hold point data in memory
                idx = idx_per_tile[i]
                idx_per_tile[i] = None
                futs[
                    pool.submit(_process_tile, str(vrt_path), tiles[i], xy[idx], wse[idx])
                ] = i

            for i in [next(pending) for _ in range(min(workers * 2, total))]:
                submit(i)

            done = 0
            while futs:
                finished, _ = wait(futs, return_when=FIRST_COMPLETED)
                for fut in finished:
                    futs.pop(fut)
                    bounds, arr, polys = fut.result()
                    done += 1
                    if arr is not None:
                        win = from_bounds(*bounds, transform=transform)
                        win = Window(
                            round(win.col_off),
                            round(win.row_off),
                            round(win.width),
                            round(win.height),
                        )
                        dst.write(arr, 1, window=win)
                    all_polys.extend(polys)
                    if done % 25 == 0 or done == total:
                        log.info("tiles: %d/%d (%d polygons)", done, total, len(all_polys))
                    nxt = next(pending, None)
                    if nxt is not None:
                        submit(nxt)
    return all_polys
