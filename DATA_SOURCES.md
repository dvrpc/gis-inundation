# DEM and vector data sources

All inputs are public. Everything is reprojected/served in **EPSG:6347**
(NAD83(2011) / UTM zone 18N, meters); elevations are **NAVD88 meters**.
For each run, the exact rasters composited into the DEM mosaic are listed in
`data/dem_<name>.sources.csv` (written alongside the VRT).

## Primary DEM: DVRPC 2015 LiDAR 1 m bare-earth DEM

- **Coverage**: the 2015 Delaware Valley QL2 acquisition covers Bucks,
  Delaware, Montgomery, Philadelphia (PA) and Burlington, Camden,
  Gloucester, Mercer (NJ). **Chester County was not flown** (only ~4% of
  it, the eastern edge, falls inside project tiles).
- **Tiling**: 3,752 tiles, 1500 m x 1500 m, MGRS-style names (e.g.
  `18SVJ469393`).
- **Downloaded from** (in order of preference):
  1. USGS 3DEP staged OPR GeoTIFFs (S3, pixel-identical to the PASDA rasters):
     `https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/OPR/Projects/Delaware_Valley_High_Density_QL2_LiDAR_BAA/DE_DelawareValley_HD_2015/TIFF/USGS_OPR_Delaware_Valley_High_Density_QL2_LiDAR_BAA_<tile>.tif`
     (also `.../DE_DelawareValleyHD_5A_2015/TIFF/...`)
  2. PASDA (authoritative publisher; zipped ERDAS .img, server can be flaky):
     `https://www.pasda.psu.edu/download/dvrpc/lidar2015/ERDAS_IMG_Raster_DEM/<tile>.zip`
- **Tile index**: `https://www.pasda.psu.edu/download/dvrpc/lidar2015/DVRPC_2015_LiDAR_tile_index.zip`
  (the PASDA download directory contains exactly these 3,752 tiles, no more)
- **Vintage**: flown spring 2015. Vertical datum NAVD88 (GEOID12A).
- PASDA metadata: https://www.pasda.psu.edu/uci/DataSummary.aspx?dataset=3188

## DVRPC network copy (preferred when reachable)

`V:\Imagery_Elevation\DVRPC_2015_LiDAR\DEM` on the DVRPC network holds
**4,647** DEM tiles: the 3,752 published to PASDA plus ~900 additional
tiles covering Chester County (extent reaches easting ~403 km, spanning the
whole county). The pipeline auto-detects this folder (or takes `--dem-dir`)
and uses the tiles directly, skipping downloads; gap-fill then only covers
whatever the folder still lacks.

The `imgelev.amd_dvrpc_2015_lidar_dem_*` tables in the DVRPC GIS database
(`dvrpcgis-db.postgres.database.azure.com/gis`) are an ArcGIS mosaic
dataset over this same folder. Its catalog stores ~3 KB pointer blobs
referencing the V: drive .img files, not pixels, so it is not usable as a
raster source outside ArcGIS; the file share is the actual data.

## Gap-fill DEM: USGS seamless 1 m DEM (3DEP)

Used only where the AOI falls outside the 2015 DVRPC tiles, in practice
~96% of Chester County. Discovered per run through The National Map API
(`https://tnmaccess.nationalmap.gov/api/v1/products`, dataset
*"Digital Elevation Model (DEM) 1 meter"*), downloaded from
`https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1m/Projects/...`
as 10 km x 10 km GeoTIFFs.

**All** overlapping products intersecting the gap are downloaded and
composited oldest-to-newest (newest wins where both have data): tiles from
state-specific projects (e.g. DE statewide) span full 10 km cells but are
nodata outside their own state, so single-product selection leaves holes.

Projects contributing to the five-county run (`--county all`), from the run
manifest:

| 3DEP project | Lidar vintage | Where it lands here |
|---|---|---|
| MD_PA_SandySupp_2014 | 2014 | most of Chester County |
| PA_Sandy_2014 | 2014 | western Chester |
| PA_Northcentral_2019_B19 | 2019 | far western fringe |
| PA_3_County_South_Central_2018_D18 | 2018 | northwestern fringe |
| DE_Statewide_B23 | 2023 (DE reprocess) | southern state-line cells (DE side) |
| DE_Snds_2013 | 2013 | southern state-line cells (DE side) |
| MD_VA_NorthChesapeakeBay_KGeorge_2020_D20 | 2020 | southwestern corner (MD side) |
| DE_DelawareValley_HD_2015 / ...HD_5A_2015 | 2015 | eastern Chester (same 2015 DVRPC acquisition, seamless product) |

Note: the DVRPC 2015 tiles always sit on top of gap-fill tiles in the mosaic,
so gap-fill data is only ever used where the 2015 project has nothing.

## Vector inputs

| Layer | Source |
|---|---|
| 100-yr floodplain (SFHA, `SFHA_TF='T'`) | FEMA NFHL REST: `https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28` (queried in 200-feature pages; larger pages 500 on county-scale envelopes) |
| County boundaries | Census TIGERweb REST: `https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/State_County/MapServer/1` (GEOIDs 42017, 42029, 42045, 42091, 42101) |

## Known caveats

- Chester County elevations are mostly 2014-vintage lidar (Sandy
  Supplemental), not the 2015 DVRPC flight: same 1 m resolution and NAVD88
  datum, different acquisition date and geoid model (cm-level differences).
- Hydro-flattening conventions differ slightly between projects; boundary
  points landing on DEM nodata (open water) are dropped before interpolation.
- Deep excavations (e.g. Falls Township quarries) produce locally extreme
  depths where the interpolated water surface spans a pit. This is inherent
  to the `Con(DEM <= WSE, WSE - DEM)` method, same as the ArcGIS workflow.
