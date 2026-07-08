"""Mosaic downloaded DEM tiles into a VRT, without requiring the GDAL CLI."""

from __future__ import annotations

import csv
import logging
import math
import re
from pathlib import Path
from xml.sax.saxutils import escape

import rasterio
from rasterio.crs import CRS as RioCRS

from . import CRS

log = logging.getLogger(__name__)

VRT_NODATA = -999999.0


def _source_project(filename: str) -> str:
    """Human-readable provenance label derived from a tile filename."""
    if re.match(r"^18[ST][A-Z]{2}\d{6}\.(img|tif)$", filename):
        return "DVRPC_2015_LiDAR (PASDA / USGS OPR mirror)"
    m = re.match(r"^USGS_(?:1M_18|one_meter)_x\d+y\d+_(.+)\.tif$", filename)
    if m:
        return f"USGS seamless 1m DEM: {m.group(1)}"
    return "unknown"


def _write_manifest(srcs: list[dict], vrt_path: Path) -> Path:
    """CSV listing every raster in the mosaic (compositing order = row order;
    later rows win where sources overlap)."""
    manifest = vrt_path.with_suffix(".sources.csv")
    with open(manifest, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["order", "filename", "source_project", "minx", "miny", "maxx", "maxy"]
        )
        for i, s in enumerate(srcs):
            b = s["bounds"]
            w.writerow(
                [
                    i,
                    s["path"].name,
                    _source_project(s["path"].name),
                    b.left,
                    b.bottom,
                    b.right,
                    b.top,
                ]
            )
    return manifest


def build_vrt(imgs: list[Path], vrt_path: Path) -> Path:
    """Write a VRT mosaic of same-CRS, same-resolution, grid-aligned tiles.

    Hand-rolled XML so the pipeline only needs the rasterio wheel (no osgeo
    python bindings or gdalbuildvrt binary).
    """
    if not imgs:
        raise ValueError("no DEM tiles to mosaic")

    srcs = []
    with rasterio.Env():
        for p in imgs:
            with rasterio.open(p) as ds:
                srcs.append(
                    {
                        "path": p.resolve(),
                        "width": ds.width,
                        "height": ds.height,
                        "bounds": ds.bounds,
                        "nodata": ds.nodata,
                        "dtype": ds.dtypes[0],
                    }
                )

    res = 1.0  # DVRPC 2015 DEM is 1 m
    minx = min(s["bounds"].left for s in srcs)
    maxx = max(s["bounds"].right for s in srcs)
    miny = min(s["bounds"].bottom for s in srcs)
    maxy = max(s["bounds"].top for s in srcs)
    width = round((maxx - minx) / res)
    height = round((maxy - miny) / res)

    # tiles come from PASDA (NAD83(2011) tag) or the USGS mirror (NAD83 tag);
    # the grids are identical, so declare the authoritative CRS once here
    wkt = RioCRS.from_string(CRS).wkt

    parts = [
        f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">',
        f"  <SRS>{escape(wkt)}</SRS>",
        f"  <GeoTransform>{minx}, {res}, 0, {maxy}, 0, {-res}</GeoTransform>",
        '  <VRTRasterBand dataType="Float32" band="1">',
        f"    <NoDataValue>{VRT_NODATA!r}</NoDataValue>",
    ]
    for s in srcs:
        xoff = round((s["bounds"].left - minx) / res)
        yoff = round((maxy - s["bounds"].top) / res)
        src_nd = s["nodata"]
        parts.append("    <ComplexSource>")
        parts.append(
            f'      <SourceFilename relativeToVRT="0">{escape(str(s["path"]))}</SourceFilename>'
        )
        parts.append("      <SourceBand>1</SourceBand>")
        parts.append(
            f'      <SrcRect xOff="0" yOff="0" xSize="{s["width"]}" ySize="{s["height"]}"/>'
        )
        parts.append(
            f'      <DstRect xOff="{xoff}" yOff="{yoff}" xSize="{s["width"]}" ySize="{s["height"]}"/>'
        )
        if src_nd is not None and math.isfinite(src_nd):
            parts.append(f"      <NODATA>{src_nd!r}</NODATA>")
        parts.append("    </ComplexSource>")
    parts += ["  </VRTRasterBand>", "</VRTDataset>"]

    vrt_path.parent.mkdir(parents=True, exist_ok=True)
    vrt_path.write_text("\n".join(parts))
    manifest = _write_manifest(srcs, vrt_path)
    projects = sorted({_source_project(s["path"].name) for s in srcs})
    log.info("VRT mosaic: %s (%d tiles, %dx%d px)", vrt_path, len(srcs), width, height)
    log.info("DEM sources (%s): %s", manifest.name, "; ".join(projects))
    return vrt_path
