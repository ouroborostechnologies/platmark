# Platmark

The public website and layer catalog for Platmark. The app's source is private.

## Website

Published with GitHub Pages from the root of `main`:

- https://ouroborostechnologies.github.io/platmark/support/
- https://ouroborostechnologies.github.io/platmark/privacy/

These URLs are compiled into the app (`Constants.swift`) and listed on the App Store, so keep the paths stable.

### Running locally

The site is plain HTML, CSS and JavaScript with no build step. Serve the repository root:

```sh
python3 -m http.server 8000
```

Then open http://localhost:8000/. Reload the page to see edits.

## Layer catalog

The app reads its map layers from `catalog-v1.sqlite`, an SQLite database built from `catalog/src/` and published as a release asset here. The app draws four kinds of source (raster, vector and DEM tiles from a URL template, and point features from a URL); this repository says which layers exist and where their tiles come from.

### Layout

```
catalog/
  src/catalog.json        schemaVersion, catalogRevision, and the fragments to include, in order
  src/providers.json …      shared tables: providers, licenses, coverages, groups
  src/layers/<area>.json    datasets and layers, grouped however reads best
  src/sources/<country>.json  satellite and aerial imagery sources that are listed but not drawn, by country
  src/basemaps.json         the base maps
  catalog-v1.sqlite       built from src/ by `tools/catalog.py assemble`; not committed
  catalog-v1.schema.json  JSON Schema for the catalog as assembled, before it is written to the database
  thumbnails/<stem>.png     one picture per layer; stem is the layer ID with "." replaced by "_"
tools/
  catalog.py              format, assemble and verify src/
  check.py                  rules, revision and tile probe
```

A fragment can hold the tables (`providers`, `licenses`, `coverages`, `datasets`, keyed by ID) and the lists (`groups`, `layers`, `baseMaps`). Assembling merges the tables, refusing a key defined twice, and joins the lists in fragment order, which is the order the app shows them in.

A fragment describes each source as the service it is: `xyzRaster`, `arcGISTiled`, `arcGISMapExport`, `arcGISImageExport`, `wms`, `wmts`, `vectorTiles`, `rasterDEM` or `geoJSON`. Assembling turns each into what the app draws (`raster`, `vector`, `dem` or `features`) and builds its tile URL template, so the published file holds the exact URLs the app requests and `tools/catalog.py` is the only place one is built.

A dataset needs no layer. The files in `src/sources/` list satellite and aerial imagery sources worldwide with what they cover, so the app can show what exists for a place; each may also say what it is about (`themes`), the forms it comes in (`formats`), how its link gives the data (`access`) and what it costs (`cost`). Give one a layer when it has a tile or map service the app can draw.

A layer names one publisher with `dataset` and `source`, or several with `members`, each a `dataset` and a `source` drawn bottom to top. The published file lists `members` either way.

### Making a change

1. Edit a fragment in `catalog/src/`. A new fragment must be added to `include` in `src/catalog.json`.
2. `python3 tools/catalog.py format`
3. `python3 tools/catalog.py assemble`
4. Increase `catalogRevision` in `src/catalog.json` (by convention `YYYYMMDDNN`), then assemble again. Apps ignore a catalog whose revision isn't newer than the one they have.
5. Run the checks below, and open a pull request.

Rules worth knowing:

- **Layer IDs are permanent.** Saved documents refer to layers by ID. To retire a layer, delete it; never give its ID to a different dataset.
- **A new kind of service needs only the assembler.** If its tiles can be requested through a URL template, teach `tools/catalog.py` to build that template and every released app can draw it.
- **New render and discovery kinds need an app release first.** A layer can only use a render or discovery `type` the app already knows. An older app skips a layer it can't read, so add the layer once an app that understands it has shipped.
- `basemap.standard`, `basemap.satellite` and `basemap.hybrid` must always exist.

### Checks

```sh
pip install jsonschema                 # only `rules` needs it
python3 tools/catalog.py verify      # src/ is formatted and assembles
python3 tools/catalog.py assemble    # build catalog/catalog-v1.sqlite, which the checks below read
python3 tools/check.py rules           # schema, unique IDs, every reference resolves, zoom and opacity ranges
python3 tools/check.py revision        # against the latest release: revision increased, no layer ID reused
python3 tools/check.py probe --only usgs.contours,gebco.depth   # fetch one real tile per source
```

`.github/workflows/check-catalog.yml` runs all of these on every pull request and push to `main` that touches `catalog/` or `tools/`. Its probe covers only layers added or changed since the latest release, so a PR fails for a source it touched rather than for an unrelated server being down.

`.github/workflows/probe-catalog.yml` probes the published catalog every night. A failing layer gets one issue labelled `catalog-probe`, titled `Probe failing: <layer id>`, which closes itself when the layer passes again.

### Publishing

Merge to `main`, then tag the commit and push the tag:

```sh
git tag catalog-2026.11.08
git push origin catalog-2026.11.08
```

`.github/workflows/release-catalog.yml` runs `verify`, `rules` and `revision`, and publishes nothing if any fail. It then creates a release of that name with `catalog-v1.sqlite` and every `catalog/thumbnails/<stem>.png` attached as `thumb-<stem>.png` (release assets can't be in folders). Apps fetch `https://github.com/ouroborostechnologies/platmark/releases/latest/download/catalog-v1.sqlite`, and a new catalog takes effect the next time the app launches.

A tag on a commit that isn't on `main` becomes a pre-release. `latest` skips pre-releases, so a branch's catalog never reaches users; debug builds can fetch it by tag from `releases/download/<tag>/…`.

Releases in this repo are only for the catalog. Any other release would become `latest` and break the address above.
