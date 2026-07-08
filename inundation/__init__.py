"""FEMA 100-yr + 3ft inundation pipeline for the DVRPC region."""

__version__ = "0.1.0"

# Working CRS: matches the DVRPC 2015 LiDAR DEM tiles
# (NAD_1983_2011_UTM_Zone_18N, meters) == EPSG:6347.
CRS = "EPSG:6347"

# 3 feet in meters, added to sampled DEM elevations at floodplain boundaries.
PLUS_3FT_M = 0.9144

# PASDA DVRPC 2015 LiDAR endpoints.
TILE_INDEX_URL = (
    "https://www.pasda.psu.edu/download/dvrpc/lidar2015/DVRPC_2015_LiDAR_tile_index.zip"
)
DEM_TILE_URL = (
    "https://www.pasda.psu.edu/download/dvrpc/lidar2015/ERDAS_IMG_Raster_DEM/{name}.zip"
)

# USGS 3DEP staged products mirror of the same project (plain GeoTIFFs on S3;
# pixel-identical to the PASDA tiles). Tried in order, before falling back to
# PASDA, whose server is flaky.
USGS_TILE_URLS = [
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/OPR/Projects/"
    "Delaware_Valley_High_Density_QL2_LiDAR_BAA/DE_DelawareValley_HD_2015/TIFF/"
    "USGS_OPR_Delaware_Valley_High_Density_QL2_LiDAR_BAA_{name}.tif",
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/OPR/Projects/"
    "Delaware_Valley_High_Density_QL2_LiDAR_BAA/DE_DelawareValleyHD_5A_2015/TIFF/"
    "USGS_OPR_Delaware_Valley_High_Density_QL2_LiDAR_BAA_{name}.tif",
]

# FEMA National Flood Hazard Layer, "Flood Hazard Zones" (S_FLD_HAZ_AR).
NFHL_LAYER_URL = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28"
)

DEPTH_NODATA = -9999.0
