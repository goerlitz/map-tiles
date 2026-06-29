#!/usr/bin/env python3
"""
process_all.py
==============

Complete folder pipeline in ONE run:

  for each *.points file:
      1. Find the original scan (filename = .points name WITHOUT the .points suffix)
      2. Georeference from the 4 active points -> DHDN / EPSG:4314
      3. Clip in DHDN to the map frame (axis-aligned rectangle)
  then across ALL clipped sheets:
      4. Harmonize color/brightness (shared mean/stddev per channel)
      5. Build mosaik.vrt

Intentionally NOT included: conversion to WGS84/3857. gdal2tiles handles that
at the end, and clipping is easier in DHDN (degrees) because the frame is
axis-aligned there.

Projective transformation maps the trapezoid map sheet EXACTLY to the target
degree rectangle, which is the correct transform type for topographic maps.

Usage:
    python3 process_all.py [FOLDER]

Then generate tiles:
    gdal2tiles.py --zoom=8-16 --resampling=lanczos --tiledriver=WEBP --xyz --processes=4 --webviewer=openlayers mosaik.vrt tiles/
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from osgeo import gdal

gdal.UseExceptions()

# --- Configuration ----------------------------------------------------------
# DHDN in degrees WITH explicit datum shift for Silesia. Otherwise PROJ may pick
# a Germany-centric shift, causing position offsets over Poland.
SOURCE_SRS = ("+proj=longlat +ellps=bessel "
              "+towgs84=582,105,414,1.04,0.35,-3.08,8.3 +no_defs")
RESAMPLING = "lanczos"            # "near" pixel-faithful, "cubic"/"lanczos" smooth
CLIP_SUFFIX = "_clip.tif"
HARM_SUFFIX = "_clip_harm.tif"


def opts_for(suffix: str):
    """Compression by source extension: JPG source -> JPEG in TIFF, otherwise DEFLATE.
    JPEG supports only 3 bands, so transparency is stored as an internal mask."""
    s = suffix.lower()
    if s in (".jpg", ".jpeg"):
        return ["TILED=YES", "COMPRESS=JPEG", "PHOTOMETRIC=YCBCR",
                "JPEG_QUALITY=90", "BIGTIFF=YES"]
    return ["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=2", "BIGTIFF=YES"]
# ---------------------------------------------------------------------------


def read_gcps(points_path: Path):
    """Read active GCPs and corner bounding box from a QGIS .points file.

    IMPORTANT: QGIS stores sourceY as NEGATIVE. GDAL expects positive row values
    (0 = top), therefore line = -sourceY.
    Returns: (list[gdal.GCP], (xmin, ymin, xmax, ymax))
    """
    gcps = []
    map_pts = []
    for raw in points_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.lower().startswith("mapx"):
            continue
        cols = line.split(",") if "," in line else line.split()
        if len(cols) < 4:
            continue
        enabled = True
        if len(cols) >= 5:
            try:
                enabled = int(float(cols[4])) == 1
            except ValueError:
                enabled = True
        if not enabled:
            continue
        try:
            map_x, map_y = float(cols[0]), float(cols[1])
            src_x, src_y = float(cols[2]), float(cols[3])
        except ValueError:
            continue
        pixel = src_x
        row = -src_y                      # <-- sign convention pitfall
        gcps.append(gdal.GCP(map_x, map_y, 0.0, pixel, row))
        map_pts.append((map_x, map_y))

    if not map_pts:
        return [], None
    xs = [p[0] for p in map_pts]
    ys = [p[1] for p in map_pts]
    return gcps, (min(xs), min(ys), max(xs), max(ys))


def _add_overviews(out_path: Path, opts) -> None:
    """Build internal pyramids (like QGIS Georeferencer via gdaladdo) for fast
    zooming. Overviews use the same compression as the source image."""
    comp = next((o.split("=")[1] for o in opts if o.startswith("COMPRESS=")), "DEFLATE")
    gdal.SetConfigOption("COMPRESS_OVERVIEW", comp)
    if comp == "JPEG":
        gdal.SetConfigOption("PHOTOMETRIC_OVERVIEW", "YCBCR")
    else:
        gdal.SetConfigOption("PREDICTOR_OVERVIEW", "2")
    ds = gdal.Open(str(out_path), gdal.GA_Update)
    ds.BuildOverviews("AVERAGE", [2, 4, 8, 16, 32])
    ds = None
    for k in ("COMPRESS_OVERVIEW", "PHOTOMETRIC_OVERVIEW", "PREDICTOR_OVERVIEW"):
        gdal.SetConfigOption(k, None)


def _write_with_mask(mem_ds, out_path: Path, opts, transform=None) -> None:
    """Write RGB (bands 1-3) plus internal mask (from alpha/last band) as TIFF.
    transform: optional function band_index->array for color harmonization."""
    xsize, ysize = mem_ds.RasterXSize, mem_ds.RasterYSize
    nbands = mem_ds.RasterCount
    alpha = mem_ds.GetRasterBand(nbands).ReadAsArray()   # 4th band = alpha from warp

    gdal.SetConfigOption("GDAL_TIFF_INTERNAL_MASK", "YES")
    drv = gdal.GetDriverByName("GTiff")
    dst = drv.Create(str(out_path), xsize, ysize, 3, gdal.GDT_Byte, options=opts)
    dst.SetGeoTransform(mem_ds.GetGeoTransform())
    dst.SetProjection(mem_ds.GetProjection())
    for b in range(1, 4):
        arr = mem_ds.GetRasterBand(b).ReadAsArray()
        if transform is not None:
            arr = transform(b, arr)
        dst.GetRasterBand(b).WriteArray(arr)
    dst.CreateMaskBand(gdal.GMF_PER_DATASET)
    dst.GetRasterBand(1).GetMaskBand().WriteArray(alpha)   # transparent where alpha=0
    dst.FlushCache()
    dst = None
    gdal.SetConfigOption("GDAL_TIFF_INTERNAL_MASK", None)
    _add_overviews(out_path, opts)                         # pyramids for fast display


def georeference_and_clip(src_raster: Path, gcps, bbox, out_path: Path, opts) -> None:
    """Projectively georeference to DHDN, clip to frame, and write a 3-band TIFF
    with internal mask (no full alpha band) in source-matching compression."""
    gcp_ds = gdal.Translate("", str(src_raster), format="VRT",
                            GCPs=gcps, outputSRS=SOURCE_SRS)
    xmin, ymin, xmax, ymax = bbox
    mem = gdal.Warp(
        "", gcp_ds, format="MEM",
        dstSRS=SOURCE_SRS,
        outputBounds=(xmin, ymin, xmax, ymax),
        outputBoundsSRS=SOURCE_SRS,
        resampleAlg=RESAMPLING,
        dstAlpha=True,
        multithread=True,
        transformerOptions=["SRC_METHOD=GCP_HOMOGRAPHY"],   # projective transform
    )
    _write_with_mask(mem, out_path, opts)
    gcp_ds = mem = None


    # --- Color harmonization (across all sheets) --------------------------------

def tile_stats(path: Path):
    """Mean/stddev per RGB band over valid (masked) pixels only."""
    ds = gdal.Open(str(path))
    mask = ds.GetRasterBand(1).GetMaskBand().ReadAsArray() > 0
    stats = {}
    for b in range(1, 4):
        arr = ds.GetRasterBand(b).ReadAsArray().astype("float64")
        vals = arr[mask]
        std = vals.std() if vals.size else 1.0
        stats[b] = (vals.mean() if vals.size else 0.0, std if std > 1e-6 else 1.0)
    ds = None
    return stats


def harmonize(clip_path: Path, target, opts, out_path: Path):
    """Read clip, harmonize RGB, and write a fresh compressed output with mask."""
    ds = gdal.Open(str(clip_path))
    means_stds = {}
    valid = ds.GetRasterBand(1).GetMaskBand().ReadAsArray() > 0
    for b in range(1, 4):
        a = ds.GetRasterBand(b).ReadAsArray().astype("float64")[valid]
        std = a.std() if a.std() > 1e-6 else 1.0
        means_stds[b] = (a.mean(), std)

    def transform(b, arr):
        arr = arr.astype("float64")
        m, s = means_stds[b]
        t_mean, t_std = target[b]
        return np.clip((arr - m) * (t_std / s) + t_mean, 0, 255).astype("uint8")

    _write_with_mask(ds, out_path, opts, transform=transform)
    ds = None
    print(f"  [harmonized] {out_path.name}")


def main() -> int:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    points_files = sorted(folder.glob("*.points"))
    if not points_files:
        print(f"No .points files found in {folder}")
        return 1

    print(f"{len(points_files)} sheet(s) -- georeferencing and clipping ...")
    clips = []   # (clip_path, opts) per sheet; opts preserves source compression
    for pf in points_files:
        src = pf.with_name(pf.name[: -len(".points")])   # strip .points suffix -> source
        if not src.exists():
            print(f"  [skip-invalid] {pf.name}: source {src.name} is missing")
            continue
        gcps, bbox = read_gcps(pf)
        if len(gcps) < 3 or bbox is None:
            print(f"  [skip-invalid] {pf.name}: only {len(gcps)} active points")
            continue
        opts = opts_for(src.suffix)                       # JPG source -> JPEG, TIF -> DEFLATE
        out = src.with_name(src.stem + CLIP_SUFFIX)
        if out.exists():
            clips.append((out, opts))
            print(f"  [skip-existing] {src.name}: {out.name} already exists")
            continue
        try:
            georeference_and_clip(src, gcps, bbox, out, opts)
            clips.append((out, opts))
            print(f"  [ok] {src.name} -> {out.name}  ({opts[1].split('=')[1]})")
        except Exception as exc:
            print(f"  [ERROR] {pf.name}: {exc}")

    if not clips:
        print("No outputs produced.")
        return 1

    print("Computing shared color target ...")
    all_stats = {c: tile_stats(c) for c, _ in clips}
    target = {b: (float(np.mean([all_stats[c][b][0] for c, _ in clips])),
                  float(np.mean([all_stats[c][b][1] for c, _ in clips])))
              for b in (1, 2, 3)}

    produced = []
    for clip, opts in clips:
        out = clip.with_name(clip.name.replace(CLIP_SUFFIX, HARM_SUFFIX))
        harmonize(clip, target, opts, out)
        produced.append(out)

    vrt = folder / "mosaik.vrt"
    gdal.BuildVRT(str(vrt), [str(p) for p in produced])
    print(f"\nDone. Mosaic: {vrt}")
    print("Tiles:  gdal2tiles.py --zoom=8-16 --resampling=lanczos "
          "--tiledriver=WEBP --xyz --processes=4 --webviewer=openlayers mosaik.vrt tiles/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
