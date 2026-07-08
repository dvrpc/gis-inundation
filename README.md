# gis-inundation

Open-source replacement for the ArcGIS Pro workflow that generates
**FEMA 100-year floodplain + 3 ft** inundation layers over the DVRPC 2015
LiDAR 1 m DEM (Economic Impact of Flooding project).

Everything runs from public data: no ArcGIS, no network drives. Full
provenance (exact download URLs, lidar vintages per area, caveats) is in
**[DATA_SOURCES.md](DATA_SOURCES.md)**; each run also writes
`data/dem_<name>.sources.csv` listing every raster composited into that
run's DEM mosaic.

| Input | Source (see [DATA_SOURCES.md](DATA_SOURCES.md)) |
|---|---|
| 1 m bare-earth DEM (2015 DVRPC QL2 LiDAR) | USGS 3DEP staged products on S3 (pixel-identical mirror of the PASDA tiles; PASDA used as fallback) |
| 1 m DEM where the 2015 flight has no coverage | USGS seamless 1 m DEM via The National Map API (the 2015 project skipped ~96% of Chester County; there the fill is mostly MD-PA Sandy Supplemental 2014 lidar) |
| 100-yr floodplain (SFHA) polygons | FEMA NFHL REST service (`S_FLD_HAZ_AR`, `SFHA_TF = 'T'`) |
| County boundaries (`--county`) | Census TIGERweb REST |
| Tile index | PASDA `DVRPC_2015_LiDAR_tile_index.zip` |

Working CRS is **EPSG:6347** (NAD83(2011) / UTM 18N, meters), the DEM's
native grid. Elevations and depths are in meters (3 ft = 0.9144 m).

## Install / run

```sh
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -m inundation run --county philadelphia --name philadelphia
```

Run from the repo root (`python -m inundation ...`). Ways to define the area:

- `--county <name>` (repeatable) or `--county all`: the five DVRPC PA
  counties (bucks, chester, delaware, montgomery, philadelphia). Full-res
  boundaries are fetched from Census TIGERweb and the outputs are masked to
  the county polygons, like the ArcGIS `Mask` environment.
- `--aoi-file file.gpkg`: any vector file (any CRS); outputs are masked to
  its polygons.
- `--bbox MINX MINY MAXX MAXY`: plain rectangle in EPSG:6347 meters.
- nothing: the full tile-index footprint.

Downloads are cached under `--data-dir` (default `data/`), DEM tiles by
tile name and NFHL polygons by AOI extent, so re-runs and interrupted
fetches never re-download.

**On the DVRPC network, no DEM download is needed at all**: the pipeline
probes for `V:\Imagery_Elevation\DVRPC_2015_LiDAR\DEM` (or pass the mount
point with `--dem-dir`) and uses those tiles in place. That folder holds
4,647 tiles, including ~900 Chester County tiles that were never published
to PASDA, so on-network runs get native 2015 coverage for Chester too. The
directory is indexed once (bounds of every raster) and the index is cached.

The freeboard is adjustable: `--plus-ft 2` builds a 100-yr + 2 ft surface
(default 3.0; ignored with `--points-gdb`, whose elevations already include
the offset).

Outputs in `--out-dir` (default `out/`):

- `<name>_depth.tif`: inundation depth (m), float32, 1 m, sparse tiled GeoTIFF
- `<name>_footprint.gpkg`: dissolved inundation footprint polygons (`area_sqm` attribute)
- `<name>_wse_points.gpkg`: the generated DEM+3ft boundary points
  (equivalent of the `DFIRM_100_p3ft_<County>` layers)

### Using the original ArcGIS point layers instead

If you have the project geodatabase, skip NFHL/point generation and feed the
existing points straight in:

```sh
python -m inundation run --county bucks --points-gdb /path/to/Default.gdb \
    --points-layer DFIRM_100_p3ft_Bucks --points-field grid_code --name bucks
```

## How it maps to the ArcGIS workflow

(`Inundation_Layer_Generation_Process_Notes.pdf`)

| ArcGIS step | Here |
|---|---|
| `DFIRM_100_p3ft_*` points (DEM+3ft along DFIRM boundaries) | densify dissolved NFHL SFHA boundaries at `--spacing` (1 m), sample DEM, add 0.9144 m |
| Step 1: Topo to Raster (spot, enforce) | TIN-style linear interpolation (`scipy.interpolate.LinearNDInterpolator`), evaluated per 1500 m tile from points within the tile + `--point-buffer` so memory stays bounded |
| Step 2: `Con(DEM <= WSE, WSE - DEM)` | identical conditional, windowed with rasterio |
| Steps 3-4: `Int(r*10)`, Reclassify, Raster to Polygon | polygonize the `depth > 0` mask directly (`rasterio.features.shapes`) and dissolve |

Differences to be aware of:

- **Interpolator**: Topo to Raster (ANUDEM) applies drainage enforcement and
  smoothing; a linear TIN does not. Along floodplain corridors where points
  are 1 m apart the surfaces are nearly identical; differences show up far
  from the boundary, where both methods are extrapolating anyway.
- **Extent**: instead of interpolating a whole county, only DEM tiles within
  `--halo` (default 1000 m) of the floodplain are downloaded and processed.
  Inundation more than a kilometer beyond the mapped floodplain would be
  clipped; raise `--halo` if that matters.
- Points that fall on DEM nodata (open water in the hydro-flattened DEM,
  areas outside project coverage) are dropped before interpolation.

## Commands

- `python -m inundation fetch`: download tile index + NFHL polygons + DEM
  tiles and build the VRT mosaic, without processing (same AOI options as
  `run`).
- `python -m inundation run`: full pipeline; `--workers N` parallelizes
  tile processing (default: half the cores).

## Layout

```
inundation/
  fetch.py      downloads (tile index, NFHL SFHA, counties, DEM tiles) with retries
  dem.py        VRT mosaic builder (pure rasterio, no GDAL CLI needed)
  points.py     boundary densify + DEM sample + 3 ft, or .gdb reader (pyogrio)
  surface.py    tiled TIN interpolation, depth raster, county mask, polygonize
  footprint.py  dissolve + GeoPackage writer
  cli.py        click CLI (fetch / run)
```
