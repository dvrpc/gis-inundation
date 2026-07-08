"""Command-line interface for the inundation pipeline."""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path

import click
import geopandas as gpd
import numpy as np
import shapely

from . import CRS
from .dem import build_vrt
from .fetch import (
    PA_COUNTIES,
    fetch_counties,
    fetch_dem_tiles,
    fetch_nfhl_sfha,
    fetch_tnm_tiles,
    find_network_dem,
    get_tile_index,
    index_local_dem,
    tnm_1m_products,
)
from .footprint import write_footprint
from .points import generate_points, read_points_gdb, save_points
from .surface import run_surface

log = logging.getLogger("inundation")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )


def _resolve_dem_tiles(
    dem_dir: str | None, data_dir: Path
) -> tuple[gpd.GeoDataFrame, bool]:
    """DEM tile index and whether tiles are local files (no downloads).

    Prefers a local/network copy of the DVRPC DEM (e.g. the V: drive on the
    DVRPC network, which also holds the Chester tiles PASDA never published);
    falls back to the PASDA tile index + downloads.
    """
    net = find_network_dem(dem_dir)
    if net is not None:
        log.info("using local DEM tiles from %s (downloads skipped)", net)
        return index_local_dem(net, data_dir), True
    return get_tile_index(data_dir), False


def _sfha_cache(data_dir: Path, aoi: tuple[float, float, float, float]) -> Path:
    """NFHL cache path keyed by AOI extent, so the same area is fetched once."""
    key = "_".join(str(int(round(v))) for v in aoi)
    return data_dir / f"sfha_{key}.gpkg"


def _resolve_aoi(
    bbox: tuple[float, ...] | None,
    aoi_file: str | None,
    county: tuple[str, ...],
    data_dir: Path,
) -> tuple[tuple[float, float, float, float] | None, shapely.Geometry | None]:
    """Resolve AOI options to (bounding box, mask polygon or None).

    --bbox masks nothing (pure rectangle); --aoi-file and --county mask the
    outputs to the actual polygons (the ArcGIS "Mask" environment).
    """
    if sum(map(bool, (bbox, aoi_file, county))) > 1:
        raise click.UsageError("use only one of --bbox, --aoi-file, --county")
    if bbox:
        return tuple(bbox), None  # type: ignore[return-value]
    if aoi_file:
        gdf = gpd.read_file(aoi_file).to_crs(CRS)
        mask = shapely.union_all(gdf.geometry.values)
        return mask.bounds, mask
    if county:
        names = sorted(PA_COUNTIES) if "all" in county else sorted(set(county))
        bad = [c for c in names if c not in PA_COUNTIES]
        if bad:
            raise click.UsageError(
                f"unknown county {bad}; choose from {', '.join(PA_COUNTIES)} or 'all'"
            )
        gdf = fetch_counties(names, data_dir)
        mask = shapely.union_all(gdf.geometry.values)
        return mask.bounds, mask
    return None, None


def _select_tiles(
    tiles: gpd.GeoDataFrame,
    target_geoms: np.ndarray,
    halo: float,
    aoi: tuple[float, float, float, float] | None,
    mask: shapely.Geometry | None = None,
) -> gpd.GeoDataFrame:
    """Tiles within `halo` meters of the floodplain (or points), inside the AOI."""
    if mask is not None:
        tiles = tiles[tiles.intersects(mask)]
    elif aoi is not None:
        tiles = tiles[tiles.intersects(shapely.box(*aoi))]
    tree = shapely.STRtree(tiles.geometry.values)
    hit = tree.query(target_geoms, predicate="dwithin", distance=halo)
    keep = np.unique(hit[1])
    return tiles.iloc[keep].reset_index(drop=True)


def _fill_dem_gaps(
    tiles: gpd.GeoDataFrame,
    target_geoms: np.ndarray,
    halo: float,
    aoi: tuple[float, float, float, float],
    mask: shapely.Geometry | None,
    dem_dir: Path,
) -> list[Path]:
    """Cover AOI areas outside the DVRPC 2015 LiDAR with USGS seamless 1m DEM.

    The 2015 flight skipped most of Chester County; this fills any such gap
    near the floodplain from The National Map.
    """
    covered = shapely.union_all(tiles.geometry.values)
    needed = mask if mask is not None else shapely.box(*aoi)
    gap = shapely.difference(needed, covered)
    if gap.is_empty or gap.area < 1e6:
        return []
    log.info(
        "%.1f km^2 of the AOI is outside DVRPC 2015 coverage; querying USGS TNM",
        gap.area / 1e6,
    )
    prods = tnm_1m_products(gap.bounds)
    if len(prods) == 0:
        log.warning("no USGS 1m DEM products found for the gap; it will be skipped")
        return []
    prods = prods[prods.intersects(gap)]
    tree = shapely.STRtree(target_geoms)
    hits = tree.query(prods.geometry.values, predicate="dwithin", distance=halo)
    prods = prods.iloc[np.unique(hits[0])]
    log.info("%d USGS 1m tiles selected for the gap", len(prods))
    return fetch_tnm_tiles(prods, dem_dir)


def _work_grid(
    aoi: tuple[float, float, float, float],
    mask: shapely.Geometry | None,
    target_geoms: np.ndarray,
    halo: float,
    tile: float = 1500.0,
) -> list[tuple[float, float, float, float]]:
    """Processing tile bounds: a 1500 m grid over the AOI, kept where within
    `halo` of the floodplain (or points) and intersecting the mask."""
    minx, miny, maxx, maxy = aoi
    xs = np.arange(math.floor(minx / tile) * tile, maxx, tile)
    ys = np.arange(math.floor(miny / tile) * tile, maxy, tile)
    cells = np.array(
        [shapely.box(x, y, x + tile, y + tile) for x in xs for y in ys], dtype=object
    )
    tree = shapely.STRtree(target_geoms)
    hits = tree.query(cells, predicate="dwithin", distance=halo)
    cells = cells[np.unique(hits[0])]
    if mask is not None:
        shapely.prepare(mask)
        cells = cells[shapely.intersects(cells, mask)]

    out = []
    for cell in cells:
        cminx, cminy, cmaxx, cmaxy = shapely.bounds(cell)
        cminx, cminy = np.floor(max(cminx, aoi[0])), np.floor(max(cminy, aoi[1]))
        cmaxx, cmaxy = np.ceil(min(cmaxx, aoi[2])), np.ceil(min(cmaxy, aoi[3]))
        if cmaxx - cminx >= 1 and cmaxy - cminy >= 1:
            out.append((float(cminx), float(cminy), float(cmaxx), float(cmaxy)))
    return out


aoi_options = [
    click.option(
        "--bbox",
        nargs=4,
        type=float,
        default=None,
        help="AOI bounding box in EPSG:6347 meters: MINX MINY MAXX MAXY",
    ),
    click.option(
        "--aoi-file",
        type=click.Path(exists=True),
        default=None,
        help="Vector file whose polygons define and mask the AOI (any CRS)",
    ),
    click.option(
        "--county",
        multiple=True,
        default=(),
        help="PA county to process (repeatable), or 'all' for the five "
        f"DVRPC PA counties: {', '.join(PA_COUNTIES)}. Outputs are masked "
        "to the county boundaries.",
    ),
    click.option(
        "--dem-dir",
        default=None,
        help="Directory of DEM tiles to use in place of downloads (e.g. "
        r"V:\Imagery_Elevation\DVRPC_2015_LiDAR\DEM on the DVRPC network). "
        "Known network locations are probed automatically when omitted.",
    ),
]


def add_options(opts):
    def wrap(f):
        for opt in reversed(opts):
            f = opt(f)
        return f

    return wrap


@click.group()
def cli() -> None:
    """FEMA 100-yr + 3ft inundation pipeline (DVRPC 2015 LiDAR DEM)."""
    _setup_logging()


@cli.command()
@add_options(aoi_options)
@click.option("--data-dir", type=click.Path(), default="data", show_default=True)
@click.option(
    "--name", default="aoi", show_default=True, help="Label for the VRT mosaic"
)
@click.option(
    "--halo",
    default=1000.0,
    show_default=True,
    help="Meters beyond the floodplain to include DEM tiles / processing",
)
def fetch(bbox, aoi_file, county, dem_dir, data_dir, name, halo) -> None:
    """Download tile index, NFHL floodplain polygons, and DEM tiles; build VRT."""
    data_dir = Path(data_dir)
    aoi, mask = _resolve_aoi(bbox, aoi_file, county, data_dir)
    tiles, local = _resolve_dem_tiles(dem_dir, data_dir)
    if aoi is None:
        aoi = tuple(tiles.total_bounds)
        log.info("no AOI given; using full tile-index extent %s", aoi)

    sfha = fetch_nfhl_sfha(aoi, _sfha_cache(data_dir, aoi))
    sel = _select_tiles(tiles, sfha.geometry.values, halo, aoi, mask)
    log.info("%d DEM tiles selected", len(sel))
    if local:
        imgs = [Path(p) for p in sel["path"]]
    else:
        imgs = fetch_dem_tiles(list(sel["Name"]), data_dir / "dem")
    # gap tiles first: at seams the DVRPC tiles (listed later) win in the VRT
    imgs = _fill_dem_gaps(tiles, sfha.geometry.values, halo, aoi, mask, data_dir / "dem") + imgs
    build_vrt(imgs, data_dir / f"dem_{name}.vrt")


@cli.command()
@add_options(aoi_options)
@click.option("--data-dir", type=click.Path(), default="data", show_default=True)
@click.option("--out-dir", type=click.Path(), default="out", show_default=True)
@click.option("--name", default="aoi", show_default=True, help="Output/cache label")
@click.option(
    "--halo",
    default=1000.0,
    show_default=True,
    help="Meters beyond the floodplain to include DEM tiles / processing",
)
@click.option(
    "--plus-ft",
    default=3.0,
    show_default=True,
    help="Feet added to the DEM at floodplain boundaries when generating WSE "
    "points (ignored with --points-gdb, whose elevations are used as-is)",
)
@click.option(
    "--spacing",
    default=1.0,
    show_default=True,
    help="Spacing (m) of generated WSE points along floodplain boundaries",
)
@click.option(
    "--point-buffer",
    default=1500.0,
    show_default=True,
    help="Meters around each work tile to gather points for interpolation",
)
@click.option(
    "--workers",
    default=max(1, (os.cpu_count() or 2) // 2),
    show_default=True,
    help="Parallel tile workers",
)
@click.option(
    "--points-gdb",
    type=click.Path(exists=True),
    default=None,
    help="Use existing DFIRM_100_p3ft points from this file geodatabase "
    "instead of generating them from NFHL + DEM",
)
@click.option("--points-layer", default=None, help="Layer name inside --points-gdb")
@click.option(
    "--points-field", default="grid_code", show_default=True, help="Elevation field"
)
def run(
    bbox,
    aoi_file,
    county,
    dem_dir,
    data_dir,
    out_dir,
    name,
    halo,
    plus_ft,
    spacing,
    point_buffer,
    workers,
    points_gdb,
    points_layer,
    points_field,
) -> None:
    """Full pipeline: points -> water surface -> depth raster -> footprint."""
    data_dir, out_dir = Path(data_dir), Path(out_dir)
    aoi, mask = _resolve_aoi(bbox, aoi_file, county, data_dir)
    tiles, local = _resolve_dem_tiles(dem_dir, data_dir)
    if aoi is None:
        aoi = tuple(tiles.total_bounds)
        log.info("no AOI given; using full tile-index extent %s", aoi)

    if points_gdb:
        if not points_layer:
            raise click.UsageError("--points-layer is required with --points-gdb")
        xy, wse = read_points_gdb(Path(points_gdb), points_layer, points_field)
        target = shapely.points(xy[:: max(1, len(xy) // 200_000)])
    else:
        sfha = fetch_nfhl_sfha(aoi, _sfha_cache(data_dir, aoi))
        target = sfha.geometry.values

    sel = _select_tiles(tiles, target, halo, aoi, mask)
    log.info("%d DEM tiles selected", len(sel))
    if local:
        imgs = [Path(p) for p in sel["path"]]
    else:
        imgs = fetch_dem_tiles(list(sel["Name"]), data_dir / "dem")
    imgs = _fill_dem_gaps(tiles, target, halo, aoi, mask, data_dir / "dem") + imgs
    vrt = build_vrt(imgs, data_dir / f"dem_{name}.vrt")

    if not points_gdb:
        clip = (
            aoi[0] - point_buffer,
            aoi[1] - point_buffer,
            aoi[2] + point_buffer,
            aoi[3] + point_buffer,
        )
        plus_m = plus_ft * 0.3048
        log.info("freeboard: %.2f ft = %.4f m", plus_ft, plus_m)
        xy, wse = generate_points(sfha, vrt, spacing, clip, plus_m=plus_m)
        save_points(xy, wse, out_dir / f"{name}_wse_points.gpkg")

    work = _work_grid(aoi, mask, target, halo)
    log.info("%d work tiles", len(work))
    depth_tif = out_dir / f"{name}_depth.tif"
    polys = run_surface(
        xy,
        wse,
        vrt,
        work,
        depth_tif,
        point_buffer=point_buffer,
        workers=workers,
        mask_wkb=shapely.to_wkb(mask) if mask is not None else None,
    )
    write_footprint(polys, out_dir / f"{name}_footprint.gpkg")
    log.info("done: %s", out_dir)


if __name__ == "__main__":
    cli()
