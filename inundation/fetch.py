"""Download inputs: PASDA tile index, DEM tiles, and FEMA NFHL floodplain polygons."""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
import shapely
from shapely.geometry import box

from . import CRS, DEM_TILE_URL, NFHL_LAYER_URL, TILE_INDEX_URL, USGS_TILE_URLS

log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "inundation-pipeline/0.1 (slawrence@dvrpc.org)"

RETRIES = 3

# The five DVRPC Pennsylvania counties, by Census FIPS GEOID.
PA_COUNTIES = {
    "bucks": "42017",
    "chester": "42029",
    "delaware": "42045",
    "montgomery": "42091",
    "philadelphia": "42101",
}

TIGERWEB_COUNTIES_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "State_County/MapServer/1/query"
)

# USGS The National Map API, used to fill areas the DVRPC 2015 LiDAR project
# does not cover (most of Chester County) with the seamless 1 m DEM product.
TNM_API_URL = "https://tnmaccess.nationalmap.gov/api/v1/products"

# On the DVRPC network the full DEM tile set (4,647 tiles, including ~900
# Chester tiles never published to PASDA) is available directly. Probed in
# order; the first directory that exists wins. Override with --dem-dir.
NETWORK_DEM_DIRS = [
    r"V:\Imagery_Elevation\DVRPC_2015_LiDAR\DEM",
    "V:/Imagery_Elevation/DVRPC_2015_LiDAR/DEM",
    "/Volumes/*/Imagery_Elevation/DVRPC_2015_LiDAR/DEM",
]


def _get(url: str, timeout: int = 300, **kw) -> requests.Response:
    """GET with retries for transient network/server errors."""
    import time

    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            r = SESSION.get(url, timeout=timeout, **kw)
            if r.status_code >= 500:
                r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as e:
            last = e
            wait = 5 * (attempt + 1)
            log.warning("retrying %s in %ds (%s)", url, wait, e)
            time.sleep(wait)
    raise last  # type: ignore[misc]


def _download(url: str, dest: Path, timeout: int = 300) -> Path:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with _get(url, timeout=timeout, stream=True) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    tmp.rename(dest)
    return dest


def get_tile_index(data_dir: Path) -> gpd.GeoDataFrame:
    """Return the DVRPC 2015 LiDAR 1500m tile index, in the working CRS."""
    data_dir.mkdir(parents=True, exist_ok=True)
    shp_dir = data_dir / "tile_index"
    shp = shp_dir / "DVRPC_2015_LiDAR_tile_index.shp"
    if not shp.exists():
        log.info("downloading tile index from PASDA")
        zpath = _download(TILE_INDEX_URL, data_dir / "tile_index.zip")
        with zipfile.ZipFile(zpath) as z:
            z.extractall(shp_dir)
    tiles = gpd.read_file(shp)  # EPSG:4269 polygons; Name = tile id
    return tiles[["Name", "geometry"]].to_crs(CRS)


def _fetch_one_tile(name: str, dem_dir: Path) -> Path | None:
    """Try the USGS S3 mirror (plain GeoTIFF) first, then PASDA (zipped img)."""
    for url in USGS_TILE_URLS:
        try:
            r = _get(url.format(name=name), stream=True)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            dest = dem_dir / f"{name}.tif"
            tmp = dest.with_suffix(".tif.part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
            tmp.rename(dest)
            return dest
        except requests.RequestException as e:
            log.warning("USGS mirror failed for %s: %s", name, e)
    try:
        r = _get(DEM_TILE_URL.format(name=name))
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            z.extract(f"{name}.img", dem_dir)
        return dem_dir / f"{name}.img"
    except (requests.RequestException, KeyError, zipfile.BadZipFile) as e:
        # a handful of index entries have no published raster
        log.warning("skipping tile %s: %s", name, e)
        return None


def fetch_dem_tiles(names: list[str], dem_dir: Path) -> list[Path]:
    """Download DEM tiles (skipping any already present); return raster paths."""
    dem_dir.mkdir(parents=True, exist_ok=True)
    rasters: list[Path] = []
    n = len(names)
    for i, name in enumerate(names, 1):
        existing = next(
            (p for ext in (".tif", ".img") if (p := dem_dir / f"{name}{ext}").exists()),
            None,
        )
        if existing is None:
            log.info("DEM tile %s (%d/%d)", name, i, n)
            existing = _fetch_one_tile(name, dem_dir)
            if existing is None:
                continue
        rasters.append(existing)
    return rasters


def find_network_dem(dem_dir: str | None) -> Path | None:
    """Resolve a local/network DEM tile directory, if one is available."""
    import glob as _glob

    candidates = [dem_dir] if dem_dir else NETWORK_DEM_DIRS
    for pat in candidates:
        if pat is None:
            continue
        for hit in _glob.glob(pat):
            p = Path(hit)
            if p.is_dir():
                return p
    if dem_dir:
        raise FileNotFoundError(f"--dem-dir {dem_dir} does not exist")
    return None


def index_local_dem(dem_dir: Path, data_dir: Path) -> gpd.GeoDataFrame:
    """Tile index (Name/path/geometry) for a directory of DEM rasters.

    Bounds are read from each raster once and cached in data_dir; the cache
    is rebuilt if the directory's raster count changes.
    """
    import hashlib

    import rasterio
    from shapely.geometry import box as _box

    rasters = sorted(
        p for ext in ("*.img", "*.tif") for p in dem_dir.glob(ext)
    )
    if not rasters:
        raise FileNotFoundError(f"no .img/.tif rasters found in {dem_dir}")

    key = hashlib.md5(str(dem_dir.resolve()).encode()).hexdigest()[:10]
    cache = data_dir / f"local_dem_index_{key}_{len(rasters)}.gpkg"
    if cache.exists():
        log.info("using cached local DEM index: %s", cache)
        return gpd.read_file(cache)

    log.info("indexing %d local DEM tiles in %s (one-time)", len(rasters), dem_dir)
    rows = []
    for i, p in enumerate(rasters, 1):
        try:
            with rasterio.open(p) as ds:
                rows.append(
                    {"Name": p.stem, "path": str(p), "geometry": _box(*ds.bounds)}
                )
        except rasterio.errors.RasterioIOError as e:
            log.warning("skipping unreadable raster %s: %s", p.name, e)
        if i % 500 == 0:
            log.info("indexed %d/%d", i, len(rasters))
    gdf = gpd.GeoDataFrame(rows, crs=CRS)
    data_dir.mkdir(parents=True, exist_ok=True)
    gdf.to_file(cache, layer="tiles", driver="GPKG")
    return gdf


def fetch_counties(counties: list[str], data_dir: Path) -> gpd.GeoDataFrame:
    """County boundary polygons (full-res TIGERweb), in the working CRS.

    `counties` are lower-case names from PA_COUNTIES. Cached per selection.
    """
    fips = sorted(PA_COUNTIES[c] for c in counties)
    cache = data_dir / f"counties_{'_'.join(fips)}.gpkg"
    if cache.exists():
        return gpd.read_file(cache)

    log.info("fetching county boundaries from TIGERweb: %s", ", ".join(counties))
    quoted = ",".join(f"'{f}'" for f in fips)
    r = _get(
        TIGERWEB_COUNTIES_URL,
        params={
            "where": f"GEOID IN ({quoted})",
            "outFields": "GEOID,NAME",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "geojson",
        },
    )
    r.raise_for_status()
    gdf = gpd.GeoDataFrame.from_features(r.json()["features"], crs="EPSG:4326")
    if len(gdf) != len(fips):
        raise RuntimeError(f"expected {len(fips)} counties, got {len(gdf)}")
    gdf = gdf.to_crs(CRS)
    cache.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(cache, layer="counties", driver="GPKG")
    return gdf


def tnm_1m_products(bounds: tuple[float, float, float, float]) -> gpd.GeoDataFrame:
    """USGS seamless 1 m DEM tiles intersecting bounds (working CRS).

    All overlapping products are kept (oldest first): tiles from border
    projects (e.g. DE statewide) are nodata outside their own state even
    though their footprint spans the full 10 km cell, so overlaps are needed
    to assemble complete coverage. The VRT lets later (newer) tiles win
    where both have data.
    """
    import re

    bbox4326 = gpd.GeoSeries([box(*bounds)], crs=CRS).to_crs("EPSG:4326").total_bounds
    items: list[dict] = []
    offset = 0
    while True:
        r = _get(
            TNM_API_URL,
            params={
                "datasets": "Digital Elevation Model (DEM) 1 meter",
                "bbox": ",".join(str(v) for v in bbox4326),
                "max": 100,
                "offset": offset,
            },
        )
        r.raise_for_status()
        data = r.json()
        items.extend(data.get("items", []))
        if offset + 100 >= data.get("total", 0):
            break
        offset += 100

    rows = []
    for it in items:
        m = re.search(r"x(\d+)y(\d+)", it.get("title", ""))
        bb = it.get("boundingBox")
        if not (m and bb and it.get("downloadURL")):
            continue
        geom4326 = box(bb["minX"], bb["minY"], bb["maxX"], bb["maxY"])
        rows.append(
            {
                "key": m.group(0),
                "title": it["title"],
                "url": it["downloadURL"],
                "published": it.get("publicationDate", ""),
                "geometry": geom4326,
            }
        )
    if not rows:
        return gpd.GeoDataFrame(
            columns=["key", "title", "url", "published", "geometry"],
            geometry="geometry",
            crs=CRS,
        )
    gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326").to_crs(CRS)
    gdf = (
        gdf.sort_values("published")
        .drop_duplicates("url")
        .reset_index(drop=True)
    )
    return gdf


def fetch_tnm_tiles(products: gpd.GeoDataFrame, dem_dir: Path) -> list[Path]:
    """Download USGS seamless 1 m DEM tiles (cached by filename)."""
    dem_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    n = len(products)
    for i, row in enumerate(products.itertuples(), 1):
        dest = dem_dir / row.url.rsplit("/", 1)[-1]
        if not dest.exists():
            log.info("TNM 1m tile %s (%d/%d)", row.key, i, n)
            try:
                _download(row.url, dest)
            except requests.RequestException as e:
                log.warning("skipping TNM tile %s: %s", row.key, e)
                continue
        out.append(dest)
    return out


def fetch_nfhl_sfha(
    bounds: tuple[float, float, float, float], out_gpkg: Path
) -> gpd.GeoDataFrame:
    """Query FEMA NFHL for 100-yr (SFHA) flood zone polygons intersecting bounds.

    bounds are in the working CRS; results are reprojected to it and cached in
    out_gpkg.
    """
    if out_gpkg.exists():
        log.info("using cached NFHL polygons: %s", out_gpkg)
        return gpd.read_file(out_gpkg)

    bbox4326 = gpd.GeoSeries([box(*bounds)], crs=CRS).to_crs("EPSG:4326").total_bounds
    # large pages make the NFHL server return 500s on big envelopes
    page = 200
    params = {
        "where": "SFHA_TF = 'T'",
        "geometry": ",".join(str(v) for v in bbox4326),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF,STATIC_BFE",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": page,
    }
    frames: list[gpd.GeoDataFrame] = []
    offset = 0
    while True:
        params["resultOffset"] = offset
        r = _get(f"{NFHL_LAYER_URL}/query", params=params)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"NFHL query failed: {data['error']}")
        feats = data.get("features", [])
        if feats:
            frames.append(gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326"))
        total = sum(len(f) for f in frames)
        log.info("NFHL query: %d features so far", total)
        if len(feats) < page:
            break
        offset += len(feats)

    if not frames:
        raise RuntimeError("NFHL query returned no SFHA polygons for this AOI")

    sfha = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), crs="EPSG:4326")
    sfha = sfha.to_crs(CRS)
    sfha["geometry"] = shapely.make_valid(sfha.geometry.values)
    sfha = sfha[sfha.geometry.geom_type.isin(["Polygon", "MultiPolygon"])]
    out_gpkg.parent.mkdir(parents=True, exist_ok=True)
    sfha.to_file(out_gpkg, layer="sfha", driver="GPKG")
    return sfha
