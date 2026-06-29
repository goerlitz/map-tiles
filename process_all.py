#!/usr/bin/env python3
"""
process_all.py
==============

Komplette Pipeline pro Ordner, in EINEM Lauf:

  fuer jede  *.points :
      1. Original-Scan finden  (Dateiname = .points OHNE die Endung .points)
      2. aus den 4 aktiven Punkten georeferenzieren -> DHDN / EPSG:4314
      3. in DHDN auf den Kartenrahmen zuschneiden (achsenparalleles Rechteck)
  danach ueber ALLE zugeschnittenen Blaetter:
      4. Farbstich/Helligkeit angleichen (gemeinsamer Mittelwert/Streuung je Kanal)
      5. mosaik.vrt bauen

Bewusst NICHT enthalten: Umrechnung nach WGS84/3857. Die macht gdal2tiles am
Ende selbst -- und Schneiden ist in DHDN (Grad) einfacher, weil der Rahmen dort
achsenparallel ist.

Projektive Transformation: bildet das trapezfoermige Blatt EXAKT auf das
Gradrechteck ab -- der richtige Typ fuer topografische Karten.

Aufruf:
    python3 process_all.py [ORDNER]

Danach kacheln:
    gdal2tiles.py --zoom=8-16 --resampling=lanczos --tiledriver=WEBP --xyz --processes=4 --webviewer=openlayers mosaik.vrt tiles/
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from osgeo import gdal

gdal.UseExceptions()

# --- Konfiguration ----------------------------------------------------------
# DHDN in Grad MIT explizitem Datumsuebergang fuer Schlesien (sonst waehlt PROJ den
# deutschland-bezogenen Uebergang -> Versatz ueber Polen). Bessel + dein towgs84.
SOURCE_SRS = ("+proj=longlat +ellps=bessel "
              "+towgs84=582,105,414,1.04,0.35,-3.08,8.3 +no_defs")
RESAMPLING = "lanczos"            # "near" pixeltreu, "cubic"/"lanczos" glaetten
CLIP_SUFFIX = "_clip.tif"
HARM_SUFFIX = "_clip_harm.tif"


def opts_for(suffix: str):
    """Kompression nach Quell-Endung: JPG-Quelle -> JPEG im TIFF, sonst DEFLATE.
    JPEG erlaubt nur 3 Baender -> Transparenz laeuft ueber eine interne Maske."""
    s = suffix.lower()
    if s in (".jpg", ".jpeg"):
        return ["TILED=YES", "COMPRESS=JPEG", "PHOTOMETRIC=YCBCR",
                "JPEG_QUALITY=90", "BIGTIFF=YES"]
    return ["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=2", "BIGTIFF=YES"]
# ---------------------------------------------------------------------------


def read_gcps(points_path: Path):
    """Liest aktive GCPs + Eckpunkt-Bounding-Box aus einer QGIS-.points-Datei.

    WICHTIG: QGIS speichert sourceY NEGATIV. GDAL braucht die Zeile positiv
    (0 = oben), darum line = -sourceY.
    Rueckgabe: (liste[gdal.GCP], (xmin, ymin, xmax, ymax))
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
        row = -src_y                      # <-- die Vorzeichen-Falle
        gcps.append(gdal.GCP(map_x, map_y, 0.0, pixel, row))
        map_pts.append((map_x, map_y))

    if not map_pts:
        return [], None
    xs = [p[0] for p in map_pts]
    ys = [p[1] for p in map_pts]
    return gcps, (min(xs), min(ys), max(xs), max(ys))


def _add_overviews(out_path: Path, opts) -> None:
    """Interne Pyramiden (wie der QGIS-Georeferencer per gdaladdo) -> schnelle
    Anzeige beim Zoomen. Overviews in derselben Kompression wie das Bild."""
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
    """Schreibt RGB (Band 1-3) + interne Maske (aus dem letzten/Alpha-Band) als TIFF.
    transform: optionale Funktion band_index->array fuer die Farbangleichung."""
    xsize, ysize = mem_ds.RasterXSize, mem_ds.RasterYSize
    nbands = mem_ds.RasterCount
    alpha = mem_ds.GetRasterBand(nbands).ReadAsArray()   # 4. Band = Alpha aus dem Warp

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
    dst.GetRasterBand(1).GetMaskBand().WriteArray(alpha)   # transparent wo Alpha=0
    dst.FlushCache()
    dst = None
    gdal.SetConfigOption("GDAL_TIFF_INTERNAL_MASK", None)
    _add_overviews(out_path, opts)                         # Pyramiden -> schnelle Anzeige


def georeference_and_clip(src_raster: Path, gcps, bbox, out_path: Path, opts) -> None:
    """Georeferenziert projektiv auf DHDN, schneidet auf den Rahmen, schreibt
    3-Band-TIFF mit interner Maske (kein Alpha-Vollband) in Quell-Kompression."""
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
        transformerOptions=["SRC_METHOD=GCP_HOMOGRAPHY"],   # projektiv
    )
    _write_with_mask(mem, out_path, opts)
    gcp_ds = mem = None


# --- Farbangleichung (ueber alle Blaetter) ----------------------------------

def tile_stats(path: Path):
    """Mittelwert/Streuung je RGB-Band, nur gueltige (maskierte) Pixel."""
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
    """Liest den Clip, gleicht RGB an, schreibt frisch (komprimiert) + Maske."""
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
    print(f"  [harmonisiert] {out_path.name}")


def main() -> int:
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    points_files = sorted(folder.glob("*.points"))
    if not points_files:
        print(f"Keine .points in {folder}")
        return 1

    print(f"{len(points_files)} Blatt/Blaetter -- georeferenziere + schneide ...")
    clips = []   # (clip_path, opts) je Blatt -- opts traegt die Quell-Kompression
    for pf in points_files:
        src = pf.with_name(pf.name[: -len(".points")])   # .points abschneiden -> Original
        if not src.exists():
            print(f"  [skip-invalid] {pf.name}: Original {src.name} fehlt")
            continue
        gcps, bbox = read_gcps(pf)
        if len(gcps) < 3 or bbox is None:
            print(f"  [skip-invalid] {pf.name}: nur {len(gcps)} aktive Punkte")
            continue
        opts = opts_for(src.suffix)                       # JPG-Quelle -> JPEG, TIF -> DEFLATE
        out = src.with_name(src.stem + CLIP_SUFFIX)
        if out.exists():
            clips.append((out, opts))
            print(f"  [skip-existing] {src.name}: {out.name} existiert bereits")
            continue
        try:
            georeference_and_clip(src, gcps, bbox, out, opts)
            clips.append((out, opts))
            print(f"  [ok] {src.name} -> {out.name}  ({opts[1].split('=')[1]})")
        except Exception as exc:
            print(f"  [FEHLER] {pf.name}: {exc}")

    if not clips:
        print("Nichts erzeugt.")
        return 1

    print("Berechne gemeinsames Farbniveau ...")
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
    print(f"\nFertig. Mosaik: {vrt}")
    print("Kacheln:  gdal2tiles.py --zoom=8-16 --resampling=lanczos "
          "--tiledriver=WEBP --xyz --processes=4 --webviewer=openlayers mosaik.vrt tiles/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
