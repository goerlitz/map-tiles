# Messtischblätter → Web-Kacheln: Ablauf

Pipeline, um historische Kartenblätter (Scans + QGIS-Passpunkte) georeferenziert,
zugeschnitten, farblich angeglichen und als Web-Kacheln auszugeben.

---

## DAVOR (manuell, in QGIS)

Jedes Blatt im **QGIS-Georeferencer** mit **4 Eckpunkten** versehen
(Transformationstyp egal — das Skript rechnet die Entzerrung selbst) und die
Passpunkte speichern. Ergebnis pro Blatt im selben Ordner:

- `<name>.jpg` oder `<name>.tif` — der Original-Scan
- `<name>.jpg.points` bzw. `<name>.tif.points` — die 4 Passpunkte (QGIS-Format)

Die `_modified.tif` aus QGIS werden **nicht** gebraucht — das Skript
georeferenziert aus dem Original selbst.

**Annahmen:** Zielkoordinaten in der `.points` sind **Grad** (DHDN/Greenwich),
die 4 Punkte sind die **Rahmenecken**, Gebiet ist **Schlesien** (Datumsübergang!).

---

## DAS SKRIPT (`process_all.py`)

Aufruf:

```bash
python3 process_all.py /pfad/zum/ordner
```

Es durchläuft jede `.points` und macht pro Blatt:

| Schritt | Was | Warum |
|--------|-----|-------|
| 1. Original finden | `.points`-Name ohne die Endung `.points` | QGIS benennt die Passpunktdatei nach dem Original |
| 2. Punkte lesen | 4 aktive Punkte, `sourceY` negiert | QGIS speichert die Pixelzeile **negativ** — ohne Negieren steht das Bild kopf |
| 3. Georeferenzieren | **projektiv** (`SRC_METHOD=GCP_HOMOGRAPHY`) nach DHDN | Das Blatt ist ein **Trapez** (Meridiane laufen zusammen); nur projektiv bildet Viereck → Gradrechteck exakt ab. Affin/TPS lägen an den Ecken daneben |
| 4. Datumsübergang | `+towgs84=582,105,414,…` fest eingebettet | Über Polen wählt PROJ sonst den deutschland-bezogenen Übergang → **Lageversatz**. Der feste Parametersatz rechnet überall richtig |
| 5. Zuschneiden | auf die Bounding-Box der 4 Ecken | In Grad ist der Rahmen achsenparallel → einfacher Rechteckschnitt entfernt Rand/Legende exakt |
| 6. Schreiben | JPG-Quelle → JPEG im TIFF, TIF-Quelle → DEFLATE; Transparenz als **interne Maske** | Kompression des Originals beibehalten (sonst bläht verlustfreies TIFF ein Ex-JPG auf das ~6-fache); Maske statt Alpha-Vollband spart ein ganzes Band |
| 7. Pyramiden | interne Overviews 2×–32× | Wie der QGIS-Georeferencer (`gdaladdo`) → schnelle Anzeige beim Zoomen |

Danach **über alle Blätter**:

| Schritt | Was | Warum |
|--------|-----|-------|
| 8. Farbangleichung | jedes Blatt auf gemeinsamen Mittelwert/Streuung je Kanal | Scans aus verschiedenen Quellen haben verschiedenen Farbstich; ohne Überlappung ist Angleichung an ein gemeinsames Niveau die richtige Methode |
| 9. Mosaik | `mosaik.vrt` über alle `_clip_harm.tif` | Leichtgewichtige Referenz auf alle Blätter, ohne sie zu kopieren |

**Ausgabe je Blatt:** `<name>_clip.tif` (Zwischenstand) und `<name>_clip_harm.tif`
(farblich angeglichen, final). Plus `mosaik.vrt`.

---

## DANACH (Web-Kacheln)

```bash
gdal2tiles.py --zoom=8-16 --resampling=lanczos --tiledriver=WEBP --xyz --processes=4 --webviewer=openlayers mosaik.vrt tiles/
```

- `--xyz` — Kachelschema für OpenLayers/Leaflet (nicht das alte TMS)
- `gdal2tiles` projiziert selbst nach Web-Mercator (EPSG:3857) — der eingebettete
  `+towgs84` sorgt für die korrekte Lage
- `--resampling=lanczos` sorgt fuer weiche Skalierung, `--tiledriver=WEBP` reduziert
  die Kachelgroesse deutlich.
- Zoom **16** ist hier das gesetzte Detaillimit. `--webviewer=openlayers` legt eine
  `openlayers.html` zum Testen an.

---

## Stellschrauben im Skript (oben)

- `SOURCE_SRS` — der `+towgs84`-Parametersatz (gilt für Schlesien)
- `RESAMPLING` — `lanczos` (glättet) / `near` (pixeltreu)
- `opts_for()` — Kompression je Quell-Endung (JPEG bzw. DEFLATE)

## Voraussetzungen

GDAL mit Python-Bindings (`osgeo`) und `numpy`. `GCP_HOMOGRAPHY` braucht ein
neueres GDAL (≥ 3.5).

## Verhalten bei erneutem Lauf

- Wenn `<name>_clip.tif` bereits existiert, wird die Georeferenzierung fuer dieses Blatt uebersprungen.
- Die Farbangleichung (`<name>_clip_harm.tif`) laeuft weiterhin fuer alle verfuegbaren Clips.
- `mosaik.vrt` wird bei jedem Lauf neu aufgebaut.
