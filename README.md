# Messtischblaetter to Web Tiles: Workflow

Pipeline for historical map sheets (scans + QGIS control points): georeference,
clip, color-harmonize, and export as web tiles.

---

## BEFORE (manual, in QGIS)

For each sheet, set **4 corner points** in the **QGIS Georeferencer**
(transform type does not matter, the script performs the transformation itself)
and save the points. Expected files per sheet in the same folder:

- `<name>.jpg` or `<name>.tif` - original scan
- `<name>.jpg.points` or `<name>.tif.points` - 4 control points (QGIS format)

The `_modified.tif` created by QGIS is **not** required.
The script georeferences directly from the original scan.

**Assumptions:** target coordinates in `.points` are in **degrees**
(DHDN/Greenwich), the 4 points are **frame corners**, area is **Silesia**
(datum shift matters).

---

## THE SCRIPT (`process_all.py`)

Run:

```bash
python3 process_all.py /path/to/folder
```

For each `.points` file it does:

| Step | What | Why |
|------|------|-----|
| 1. Find source | remove `.points` suffix from filename | QGIS names the point file after the source image |
| 2. Read points | 4 active points, negate `sourceY` | QGIS stores pixel row as **negative**; without negation the image is flipped |
| 3. Georeference | **projective** (`SRC_METHOD=GCP_HOMOGRAPHY`) to DHDN | The sheet is a **trapezoid** (converging meridians); projective is the correct map to degree rectangle |
| 4. Datum shift | explicit `+towgs84=582,105,414,...` | Over Poland, PROJ may otherwise choose a Germany-centric shift and cause offsets |
| 5. Clip | to bounding box of the 4 corners | In degree space the frame is axis-aligned, so rectangular clipping is exact |
| 6. Write output | JPG source -> JPEG-in-TIFF, TIF source -> DEFLATE, transparency as **internal mask** | Preserves practical compression and avoids a full alpha output band |
| 7. Overviews | internal pyramids 2x-32x | Same concept as QGIS/georeferencer (`gdaladdo`) for faster zoom/display |

Then **across all sheets**:

| Step | What | Why |
|------|------|-----|
| 8. Color harmonization | each sheet to shared per-channel mean/stddev | Inputs from different sources have different color casts; this normalizes visual appearance |
| 9. Mosaic | `mosaik.vrt` over all `_clip_harm.tif` | Lightweight virtual mosaic without duplicating raster data |

**Output per sheet:** `<name>_clip.tif` (intermediate) and
`<name>_clip_harm.tif` (harmonized final), plus `mosaik.vrt`.

---

## AFTER (Web Tiles)

```bash
gdal2tiles.py --zoom=8-16 --resampling=lanczos --tiledriver=WEBP --xyz --processes=4 --webviewer=openlayers mosaik.vrt tiles/
```

- `--xyz` uses XYZ tile schema for OpenLayers/Leaflet (not legacy TMS)
- `gdal2tiles` projects to Web Mercator (EPSG:3857); embedded `+towgs84`
  keeps geographic placement correct
- `--resampling=lanczos` gives smoother resampling, `--tiledriver=WEBP`
  significantly reduces tile size
- Zoom **16** is the configured detail limit, `--webviewer=openlayers`
  creates `openlayers.html` for quick verification

---

## Tunables in the Script

- `SOURCE_SRS` - `+towgs84` parameter set (for Silesia)
- `RESAMPLING` - `lanczos` (smoother) / `near` (pixel-faithful)
- `opts_for()` - output compression by source extension (JPEG vs DEFLATE)

## Requirements

GDAL with Python bindings (`osgeo`) and `numpy`.
`GCP_HOMOGRAPHY` requires a newer GDAL (>= 3.5).

## Behavior on Re-run

- If `<name>_clip.tif` already exists, georeferencing for that sheet is skipped.
- Harmonization (`<name>_clip_harm.tif`) still runs for all available clips.
- `mosaik.vrt` is rebuilt on every run.
