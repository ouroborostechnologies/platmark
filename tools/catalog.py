#!/usr/bin/env python3
"""Builds the published layer catalog from its source fragments.

    python3 tools/catalog.py format      # rewrite catalog/src/ in the canonical layout
    python3 tools/catalog.py assemble    # write catalog/catalog-v1.sqlite from catalog/src/
    python3 tools/catalog.py verify      # fail if src/ isn't formatted or doesn't assemble

`catalog/src/catalog.json` holds the header and lists the fragments in order. Each fragment is a
part of the catalog: any of the tables (`providers`, `licenses`, `coverages`, `datasets`, keyed by
ID) and lists (`groups`, `layers`, `baseMaps`). Assembling merges the tables, refusing a key defined
twice, and concatenates the lists in fragment order, which is the order the app shows layers in.

A fragment describes each source as the service it is: an ArcGIS cache, a WMS endpoint, a WMTS
template. The published file describes it as what the app's renderer draws: raster, vector or DEM
tiles from a URL template, or point features from a URL. Assembling does that conversion, and is the
only place a tile URL is built, so the URLs `tools/check.py probe` requests are the ones that ship.

A fragment's layer names one publisher with `dataset` (and `source`, unless its dates supply one),
or several with `members`. The published file always lists `members`.

The published file is an SQLite database, so the app can ask it which layers and datasets match some
text or cover a place. It is built here and never committed: a release builds its own.
"""
import json
import sqlite3
import sys
import tempfile
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "catalog" / "src"
INDEX = SRC / "catalog.json"
SCHEMA_VERSION = 1
OUTPUT = ROOT / "catalog" / f"catalog-v{SCHEMA_VERSION}.sqlite"

TABLES = ("providers", "licenses", "coverages", "datasets")
LISTS = ("groups", "layers", "baseMaps")

# Keys that read best first, in this order. Anything else follows alphabetically.
KEY_ORDER = [
    "schemaVersion", "catalogRevision", "include",
    "id", "type", "name", "title", "abbreviation", "label", "date", "detail", "summary", "explanation",
    "category", "group", "dataset", "provider", "systemImage",
    "officialURL", "serviceURL", "endpoint", "urlTemplate", "url",
    "attribution", "license", "coverage", "rects", "extent", "themes", "formats", "access", "cost",
    "members", "source", "render", "minimumZoom", "maximumZoom", "defaultOpacity", "time",
    "range", "cadence", "discovery", "preloadAllowance", "editions",
    "style", "placement", "datasets", "layers",
    "providers", "licenses", "coverages", "groups", "baseMaps",
]
RANK = {key: index for index, key in enumerate(KEY_ORDER)}
WIDTH = 110


def ordered(value):
    """The same value with object keys in canonical order, recursively."""
    if isinstance(value, dict):
        keys = sorted(value, key=lambda k: (RANK.get(k, len(RANK)), k))
        return {key: ordered(value[key]) for key in keys}
    if isinstance(value, list):
        return [ordered(item) for item in value]
    return value


def render(value, indent=0, table=False):
    """JSON with small containers kept on one line, so a rectangle or a short source reads at a glance.

    `table` marks an object keyed by ID; its entries are never folded onto the same line as each other.
    """
    flat = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    if not isinstance(value, (dict, list)) or (not table and indent + len(flat) <= WIDTH):
        return flat
    pad = " " * (indent + 2)
    if isinstance(value, list):
        if not value:
            return "[]"
        items = [pad + render(item, indent + 2) for item in value]
        return "[\n" + ",\n".join(items) + "\n" + " " * indent + "]"
    if not value:
        return "{}"
    items = []
    for key, item in value.items():
        prefix = json.dumps(key, ensure_ascii=False) + ": "
        items.append(pad + prefix + render(item, indent + 2, table=key in TABLES and indent == 0))
    return "{\n" + ",\n".join(items) + "\n" + " " * indent + "}"


def dumps(value):
    return render(ordered(value)) + "\n"


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


# MARK: - Publishing

EXPORT_OPTIONS = ("visibleLayers", "mosaicRule", "renderingRule", "imageFormat", "transparent", "tileSize",
                  "pixelRatio", "additionalQuery")
# Each service a fragment can name, with the fields it requires and the fields it may add.
SERVICES = {
    "xyzRaster": (("urlTemplate",), ("tileSize", "maximumLevel")),
    "arcGISTiled": (("serviceURL",), ("fileExtension", "tileSize", "maximumLevel")),
    "arcGISMapExport": (("serviceURL",), EXPORT_OPTIONS),
    "arcGISImageExport": (("serviceURL",), EXPORT_OPTIONS),
    "wms": (("endpoint", "layers", "version"),
            ("styles", "imageFormat", "transparent", "tileSize", "pixelRatio", "additionalQuery")),
    "wmts": (("urlTemplate",), ("tileSize", "maximumLevel")),
    "vectorTiles": (("urlTemplate", "sourceLayer"), ()),
    "rasterDEM": (("urlTemplate", "encoding"), ("tileSize",)),
    "geoJSON": (("url",), ("records",)),
}
# 1.3.0 names the reference system `crs` and 1.1.1 names it `srs`; confusing them misplaces the map.
WMS_REFERENCE_PARAMETER = {"1.1.1": "srs", "1.3.0": "crs"}
# The renderer replaces this with the tile's Web Mercator extent, which is what lets a service that
# draws any rectangle on request stand in for a tile set.
BBOX = "{bbox-epsg-3857}"


def escape(value):
    """A query value, percent-encoded. A whole value in braces is left alone, so a placeholder such
    as {bbox-epsg-3857} or {date} survives for whoever fills it in."""
    if value.startswith("{") and value.endswith("}"):
        return value
    return urllib.parse.quote(value, safe="!$'()*,-./:;@_~")


def encoded_query(items):
    return "&".join(f"{key}={escape(value)}" for key, value in items)


def service_root(source):
    """Service roots are written with and without a trailing slash; the template adds its own."""
    root = source["serviceURL"]
    return root[:-1] if root.endswith("/") else root


def tile_template(source):
    """The URL template a raster service's tiles are requested from."""
    kind = source["type"]
    if kind in ("xyzRaster", "wmts"):
        return source["urlTemplate"]
    if kind == "arcGISTiled":
        # ArcGIS caches address a tile as level/row/column: {y} before {x}.
        extension = source.get("fileExtension")
        return f"{service_root(source)}/tile/{{z}}/{{y}}/{{x}}" + ("" if extension is None else f".{extension}")
    # The rest draw on request. Asking for more pixels than the tile's logical size, and a DPI to
    # match, is what makes them sharp on a high-resolution screen.
    ratio = max(source.get("pixelRatio", 2), 1)
    pixels = source.get("tileSize", 256) * ratio
    extra = source.get("additionalQuery", {})
    if kind == "wms":
        items = [
            ("service", "WMS"), ("version", source["version"]), ("request", "GetMap"),
            ("layers", source["layers"]), ("styles", source.get("styles", "")),
            (WMS_REFERENCE_PARAMETER[source["version"]], "EPSG:3857"), ("bbox", BBOX),
            ("width", str(pixels)), ("height", str(pixels)), ("format", source.get("imageFormat", "image/png")),
            ("transparent", "TRUE" if source.get("transparent", True) else "FALSE"),
        ]
        items += [(key, extra[key]) for key in sorted(extra)]
        endpoint = source["endpoint"]
        return endpoint + ("&" if "?" in endpoint else "?") + encoded_query(items)
    # imageSR is always stated: several services are stored in a local projection and return
    # unprojected imagery when the output reference is left to their default.
    items = [
        ("bbox", BBOX), ("bboxSR", "3857"), ("imageSR", "3857"), ("size", f"{pixels},{pixels}"),
        ("dpi", str(96 * ratio)), ("format", source.get("imageFormat", "png32")),
        ("transparent", "true" if source.get("transparent", True) else "false"), ("f", "image"),
    ]
    for key, name in (("visibleLayers", "layers"), ("mosaicRule", "mosaicRule"), ("renderingRule", "renderingRule")):
        if key in source:
            items.append((name, source[key]))
    items += [(key, extra[key]) for key in sorted(extra)]
    path = "export" if kind == "arcGISMapExport" else "exportImage"
    return f"{service_root(source)}/{path}?" + encoded_query(items)


def published_source(source, where):
    """A fragment's source as the catalog publishes it: one of raster, vector, dem or features."""
    kind = source.get("type") if isinstance(source, dict) else None
    if kind not in SERVICES:
        raise SystemExit(f"{where}: unknown source type {json.dumps(kind)}")
    required, optional = SERVICES[kind]
    missing = [key for key in required if key not in source]
    if missing:
        raise SystemExit(f"{where}: {kind} source needs {', '.join(missing)}")
    unknown = sorted(set(source) - {"type", *required, *optional})
    if unknown:
        raise SystemExit(f"{where}: {kind} source has no {', '.join(unknown)}")
    if kind == "wms" and source["version"] not in WMS_REFERENCE_PARAMETER:
        raise SystemExit(f"{where}: WMS version {json.dumps(source['version'])} is not one of "
                         f"{', '.join(WMS_REFERENCE_PARAMETER)}")

    if kind == "geoJSON":
        return {"type": "features", **{key: source[key] for key in ("url", "records") if key in source}}
    if kind == "vectorTiles":
        return {"type": "vector", "urlTemplate": source["urlTemplate"], "sourceLayer": source["sourceLayer"]}
    if kind == "rasterDEM":
        published = {"type": "dem", "urlTemplate": source["urlTemplate"], "encoding": source["encoding"]}
    else:
        published = {"type": "raster", "urlTemplate": tile_template(source)}
        if "maximumLevel" in source:
            published["maximumLevel"] = source["maximumLevel"]
    # The tile's logical size, whatever number of pixels the template asks for.
    if source.get("tileSize", 256) != 256:
        published["tileSize"] = source["tileSize"]
    return published


def published_discovery(discovery, where):
    """A dated layer's discovery with every source in it converted."""
    if not isinstance(discovery, dict):
        return discovery
    discovery = dict(discovery)
    kind = discovery.get("type")
    listed = "members" if kind == "serviceSet" else "editions" if kind in ("fixed", "regional") else None
    if listed is not None and isinstance(discovery.get(listed), list):
        discovery[listed] = [
            {**date, "source": published_source(date.get("source"), f"{where} date {json.dumps(date.get('id'))}")}
            if isinstance(date, dict) else date
            for date in discovery[listed]
        ]
    if kind in ("monthlyClock", "wayback"):
        discovery["source"] = published_source(discovery.get("source"), f"{where} discovery")
    return discovery


def published_layer(layer, fragment):
    """A fragment's layer as published: its publishers as `members` whether it names one or several,
    and every source converted."""
    if not isinstance(layer, dict):
        raise SystemExit(f"{fragment}: a layer must be an object")
    where = f"{fragment}: layer {json.dumps(layer.get('id'))}"
    layer = dict(layer)
    if "members" in layer:
        if "dataset" in layer or "source" in layer:
            raise SystemExit(f"{where}: a layer has either members, or a dataset and source of its own")
        members = layer["members"]
        if not isinstance(members, list) or not all(isinstance(member, dict) for member in members):
            raise SystemExit(f"{where}: members must be a list of objects")
    elif "dataset" in layer:
        members = [{key: layer.pop(key) for key in ("dataset", "source") if key in layer}]
    else:
        raise SystemExit(f"{where}: needs a dataset, or members")
    layer["members"] = [
        {**member, "source": published_source(member["source"], f"{where} member {index}")}
        if "source" in member else member
        for index, member in enumerate(members)
    ]
    if isinstance(layer.get("time"), dict):
        layer["time"] = {**layer["time"], "discovery": published_discovery(layer["time"].get("discovery"), where)}
    return layer


def published_base_map(base, fragment):
    if not isinstance(base, dict) or "layers" not in base:
        return base
    if not isinstance(base["layers"], list):
        raise SystemExit(f"{fragment}: base map {json.dumps(base.get('id'))}: layers must be a list")
    return {**base, "layers": [published_layer(layer, fragment) for layer in base["layers"]]}


def fragments():
    index = load(INDEX)
    return index, [(name, load(SRC / name)) for name in index.get("include", [])]


def assemble():
    index, parts = fragments()
    if index.get("schemaVersion") != SCHEMA_VERSION:
        raise SystemExit(f"catalog.json: schemaVersion must be {SCHEMA_VERSION}")
    result = {"schemaVersion": SCHEMA_VERSION, "catalogRevision": index["catalogRevision"]}
    result.update({table: {} for table in TABLES})
    result.update({name: [] for name in LISTS})
    origin = {}
    for name, part in parts:
        unknown = set(part) - set(TABLES) - set(LISTS)
        if unknown:
            raise SystemExit(f"{name}: unknown top-level keys {sorted(unknown)}")
        for table in TABLES:
            for key, entry in part.get(table, {}).items():
                if key in result[table]:
                    raise SystemExit(f"{name}: {table} \"{key}\" is already defined in {origin[(table, key)]}")
                result[table][key] = entry
                origin[(table, key)] = name
        result["groups"].extend(part.get("groups", []))
        result["layers"].extend(published_layer(layer, name) for layer in part.get("layers", []))
        result["baseMaps"].extend(published_base_map(base, name) for base in part.get("baseMaps", []))
    for table in TABLES:
        result[table] = dict(sorted(result[table].items()))
    return result


# MARK: - Database

# What only the renderer reads (a source, a render style, a layer's dates, a base map's style) is
# kept as the JSON the app already decodes. Everything the app looks up or searches by is a column.
DATABASE = """
CREATE TABLE meta (schema_version INTEGER NOT NULL, catalog_revision INTEGER NOT NULL);
CREATE TABLE providers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, abbreviation TEXT NOT NULL, official_url TEXT NOT NULL,
    summary TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE licenses (id TEXT PRIMARY KEY, name TEXT NOT NULL, url TEXT) WITHOUT ROWID;
CREATE TABLE coverages (id TEXT PRIMARY KEY, label TEXT NOT NULL) WITHOUT ROWID;
-- An R-tree keeps 32-bit coordinates, rounded outward, which is right for finding what overlaps a
-- place. `rect` is the rectangle as written, [west, south, east, north].
CREATE VIRTUAL TABLE coverage_rects USING rtree(id, west, east, south, north, +coverage TEXT, +rect TEXT);
CREATE TABLE groups (
    id TEXT PRIMARY KEY, position INTEGER NOT NULL, title TEXT NOT NULL, system_image TEXT NOT NULL,
    explanation TEXT NOT NULL
) WITHOUT ROWID;
-- A dataset with no coverage has data worldwide.
CREATE TABLE datasets (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, provider TEXT NOT NULL REFERENCES providers,
    summary TEXT NOT NULL, official_url TEXT, service_url TEXT NOT NULL, attribution TEXT NOT NULL,
    license TEXT NOT NULL REFERENCES licenses, coverage TEXT REFERENCES coverages, cache_policy TEXT,
    use_status TEXT, restricted_reason TEXT, themes TEXT, formats TEXT, access TEXT, cost TEXT
) WITHOUT ROWID;
-- A layer with no position is drawn only as part of a base map.
CREATE TABLE layers (
    id TEXT PRIMARY KEY, position INTEGER, name TEXT NOT NULL, summary TEXT NOT NULL,
    category TEXT NOT NULL, layer_group TEXT REFERENCES groups, render TEXT, minimum_zoom REAL,
    maximum_zoom REAL, default_opacity REAL, time TEXT
) WITHOUT ROWID;
CREATE TABLE layer_members (
    layer TEXT NOT NULL REFERENCES layers, position INTEGER NOT NULL,
    dataset TEXT NOT NULL REFERENCES datasets, source TEXT, PRIMARY KEY (layer, position)
) WITHOUT ROWID;
CREATE TABLE base_maps (
    id TEXT PRIMARY KEY, position INTEGER NOT NULL, name TEXT NOT NULL, summary TEXT NOT NULL,
    system_image TEXT NOT NULL, style TEXT NOT NULL, placement TEXT
) WITHOUT ROWID;
CREATE TABLE base_map_layers (
    base_map TEXT NOT NULL REFERENCES base_maps, position INTEGER NOT NULL,
    layer TEXT NOT NULL REFERENCES layers, PRIMARY KEY (base_map, position)
) WITHOUT ROWID;
CREATE TABLE base_map_datasets (
    base_map TEXT NOT NULL REFERENCES base_maps, position INTEGER NOT NULL,
    dataset TEXT NOT NULL REFERENCES datasets, PRIMARY KEY (base_map, position)
) WITHOUT ROWID;
CREATE INDEX datasets_by_coverage ON datasets (coverage);
CREATE INDEX members_by_dataset ON layer_members (dataset);
-- One row per listed layer (kind 'layer') and per dataset (kind 'dataset').
CREATE VIRTUAL TABLE search USING fts5(
    kind UNINDEXED, id UNINDEXED, name, summary, publishers, themes,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def packed(value):
    """A nested value as the JSON text one column holds, or NULL for a value left out."""
    return None if value is None else json.dumps(ordered(value), ensure_ascii=False, separators=(",", ":"))


def insert(db, table, where, **row):
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    try:
        db.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", list(row.values()))
    except sqlite3.IntegrityError as error:
        # A key naming an entry that isn't there, or a required field left out.
        raise SystemExit(f"{where}: {error}; `tools/check.py rules` says which")


def insert_layer(db, layer, position, where):
    insert(db, "layers", where, id=layer["id"], position=position, name=layer.get("name"),
           summary=layer.get("summary"), category=layer.get("category"), layer_group=layer.get("group"),
           render=packed(layer.get("render")), minimum_zoom=layer.get("minimumZoom"),
           maximum_zoom=layer.get("maximumZoom"), default_opacity=layer.get("defaultOpacity"),
           time=packed(layer.get("time")))
    for index, member in enumerate(layer["members"]):
        insert(db, "layer_members", f"{where} member {index}", layer=layer["id"], position=index,
               dataset=member.get("dataset"), source=packed(member.get("source")))


def write_database(catalog, path):
    """The assembled catalog as the database the app reads."""
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.executescript(DATABASE)
    db.execute("PRAGMA foreign_keys = ON")
    with db:
        db.execute("INSERT INTO meta VALUES (?, ?)", (catalog["schemaVersion"], catalog["catalogRevision"]))
        for key, entry in catalog["providers"].items():
            insert(db, "providers", f"provider {key}", id=key, name=entry.get("name"),
                   abbreviation=entry.get("abbreviation"), official_url=entry.get("officialURL"),
                   summary=entry.get("summary"))
        for key, entry in catalog["licenses"].items():
            insert(db, "licenses", f"license {key}", id=key, name=entry.get("name"), url=entry.get("url"))
        for key, entry in catalog["coverages"].items():
            insert(db, "coverages", f"coverage {key}", id=key, label=entry.get("label"))
            for rect in entry["rects"]:
                west, south, east, north = rect
                db.execute("INSERT INTO coverage_rects (west, east, south, north, coverage, rect) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (west, east, south, north, key, packed(rect)))
        for index, group in enumerate(catalog["groups"]):
            insert(db, "groups", f"group {group.get('id')}", id=group.get("id"), position=index,
                   title=group.get("title"), system_image=group.get("systemImage"),
                   explanation=group.get("explanation"))
        for key, entry in catalog["datasets"].items():
            status = entry.get("useStatus")
            restricted = status.get("restricted") if isinstance(status, dict) else None
            coverage = entry.get("coverage")
            insert(db, "datasets", f"dataset {key}", id=key, name=entry.get("name"), provider=entry.get("provider"),
                   summary=entry.get("summary"), official_url=entry.get("officialURL"),
                   service_url=entry.get("serviceURL"), attribution=entry.get("attribution"),
                   license=entry.get("license"), coverage=None if coverage == "global" else coverage,
                   cache_policy=entry.get("cachePolicy"),
                   use_status="restricted" if isinstance(status, dict) else status, restricted_reason=restricted,
                   themes=packed(entry.get("themes")), formats=packed(entry.get("formats")),
                   access=entry.get("access"), cost=entry.get("cost"))
        for index, layer in enumerate(catalog["layers"]):
            insert_layer(db, layer, index, f"layer {layer.get('id')}")
        listed = {layer["id"] for layer in catalog["layers"]}
        shared = {}
        for index, base in enumerate(catalog["baseMaps"]):
            where = f"base map {base.get('id')}"
            insert(db, "base_maps", where, id=base.get("id"), position=index, name=base.get("name"),
                   summary=base.get("summary"), system_image=base.get("systemImage"),
                   style=packed(base.get("style")), placement=base.get("placement"))
            for position, key in enumerate(base.get("datasets", [])):
                insert(db, "base_map_datasets", where, base_map=base["id"], position=position, dataset=key)
            for position, layer in enumerate(base.get("layers", [])):
                # Satellite and hybrid draw the same imagery, so a base map's layer is stored once.
                if layer["id"] in listed or shared.setdefault(layer["id"], layer) != layer:
                    raise SystemExit(f"{where}: layer id \"{layer['id']}\" is already used with a different definition")
                if shared[layer["id"]] is layer:
                    insert_layer(db, layer, None, f"{where} layer {layer['id']}")
                insert(db, "base_map_layers", where, base_map=base["id"], position=position, layer=layer["id"])
        index_search(db, catalog)
    db.execute("VACUUM")
    db.close()


def index_search(db, catalog):
    datasets, providers = catalog["datasets"], catalog["providers"]

    def publishers(keys):
        words = []
        for key in keys:
            dataset = datasets[key]
            provider = providers[dataset["provider"]]
            words += [dataset["name"], provider["name"], provider["abbreviation"]]
        return " ".join(dict.fromkeys(words))

    def themes(keys):
        return " ".join(dict.fromkeys(theme for key in keys for theme in datasets[key].get("themes", [])))

    rows = [("layer", layer["id"], layer["name"], layer["summary"], [m["dataset"] for m in layer["members"]])
            for layer in catalog["layers"]]
    rows += [("dataset", key, entry["name"], entry["summary"], [key]) for key, entry in datasets.items()]
    db.executemany("INSERT INTO search (kind, id, name, summary, publishers, themes) VALUES (?, ?, ?, ?, ?, ?)",
                   [(kind, key, name, summary, publishers(keys), themes(keys))
                    for kind, key, name, summary, keys in rows])


def command_format():
    for path in sorted(SRC.rglob("*.json")):
        text = dumps(load(path))
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"formatted {path.relative_to(ROOT)}")


def command_assemble():
    write_database(assemble(), OUTPUT)
    print(f"wrote {OUTPUT.relative_to(ROOT)}")


def command_verify():
    problems = []
    for path in sorted(SRC.rglob("*.json")):
        if path.read_text(encoding="utf-8") != dumps(load(path)):
            problems.append(f"{path.relative_to(ROOT)} is not formatted; run tools/catalog.py format")
    listed = set(load(INDEX).get("include", []))
    for path in sorted(SRC.rglob("*.json")):
        name = str(path.relative_to(SRC))
        if path != INDEX and name not in listed:
            problems.append(f"{name} is not listed in catalog.json, so it would be left out")
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        raise SystemExit(1)
    with tempfile.TemporaryDirectory() as scratch:
        write_database(assemble(), Path(scratch) / OUTPUT.name)
    print("catalog sources are formatted and assemble")


if __name__ == "__main__":
    commands = {"format": command_format, "assemble": command_assemble, "verify": command_verify}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit(__doc__)
    commands[sys.argv[1]]()
