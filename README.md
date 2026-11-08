# Platmark

The public website and layer catalog for Platmark. The app's source is private.

## Website

Published with GitHub Pages from the root of `main`:

- https://ouroborostechnologies.github.io/platmark/support/
- https://ouroborostechnologies.github.io/platmark/privacy/

These URLs are compiled into the app (`Constants.swift`) and listed on the App Store, so keep the paths stable.

## Layer catalog

The app reads its map layers from `catalog/catalog-v1.json`, published as a release asset here. The app has the code that draws each kind of source; this repository says which layers exist and where their tiles come from.

### Layout

```
catalog/
  src/catalog.json        schemaVersion, catalogRevision, and the fragments to include, in order
  src/providers.json …      shared tables: providers, licenses, coverages, groups
  src/layers/<area>.json    datasets and layers, grouped however reads best
  src/basemaps.json         the base maps
  catalog-v1.json         assembled from src/; committed so a PR shows the effect of each edit
  catalog-v1.schema.json  JSON Schema for the assembled file
  thumbnails/<stem>.png     one picture per layer; stem is the layer ID with "." replaced by "_"
tools/
  catalog.py              format, assemble and verify src/
  check.py                  rules, revision and tile probe
```

A fragment can hold the tables (`providers`, `licenses`, `coverages`, `datasets`, keyed by ID) and the lists (`groups`, `layers`, `baseMaps`). Assembling merges the tables, refusing a key defined twice, and joins the lists in fragment order, which is the order the app shows them in.

### Making a change

1. Edit a fragment in `catalog/src/`. A new fragment must be added to `include` in `src/catalog.json`.
2. `python3 tools/catalog.py format`
3. `python3 tools/catalog.py assemble`
4. Increase `catalogRevision` in `src/catalog.json` (by convention `YYYYMMDDNN`), then assemble again. Apps ignore a catalog whose revision isn't newer than the one they have.
5. Run the checks below, and open a pull request.

Rules worth knowing:

- **Layer IDs are permanent.** Saved documents refer to layers by ID. To retire a layer, delete it; never give its ID to a different dataset.
- **New kinds need an app release first.** A layer can only use a source, render or discovery `type` the app already knows. An older app skips a layer it can't read, so add the layer once an app that understands it has shipped.
- `basemap.standard`, `basemap.satellite` and `basemap.hybrid` must always exist.

### Checks

```sh
pip install jsonschema                 # only `rules` needs it
python3 tools/catalog.py verify      # src/ is formatted and catalog-v1.json is up to date
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

`.github/workflows/release-catalog.yml` runs `verify`, `rules` and `revision`, and publishes nothing if any fail. It then creates a release of that name with `catalog-v1.json`, its schema, and every `catalog/thumbnails/<stem>.png` attached as `thumb-<stem>.png` (release assets can't be in folders). Apps fetch `https://github.com/ouroborostechnologies/platmark/releases/latest/download/catalog-v1.json`, and a new catalog takes effect the next time the app launches.

A tag on a commit that isn't on `main` becomes a pre-release. `latest` skips pre-releases, so a branch's catalog never reaches users; debug builds can fetch it by tag from `releases/download/<tag>/…`.

Releases in this repo are only for the catalog. Any other release would become `latest` and break the address above.
