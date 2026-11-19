#!/usr/bin/env python3
"""Checks the assembled layer catalog before it ships, and probes its tile services.

    python3 tools/check.py rules                       # schema + catalog rules on catalog/catalog-v1.json
    python3 tools/check.py revision [--against P|URL]  # compare with the published catalog
    python3 tools/check.py probe [--only a,b] [--timeout s] [--json out.json] [--changed-since P|URL]

`rules` needs `jsonschema` (pip install jsonschema); everything else is the standard library.
Every command takes `--file` to check a catalog other than catalog/catalog-v1.json.

The app's decoder is the authority on the format. This file describes the same format in Python so
the public repository can check a catalog without the app's source; the tile URLs `probe`
requests are built the way the app builds them.
"""
import argparse
import concurrent.futures
import datetime
import json
import math
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog" / "catalog-v1.json"
SCHEMA = ROOT / "catalog" / "catalog-v1.schema.json"
LATEST_URL = "https://github.com/ouroborostechnologies/platmark/releases/latest/download/catalog-v1.json"
USER_AGENT = "PlatmarkCatalogProbe/1"
REQUIRED_BASE_MAPS = ("basemap.standard", "basemap.satellite", "basemap.hybrid")

# A point in the conterminous US, for probing a source that covers the whole world.
CONUS_POINT = (-98.58, 39.83)
# Where GitHub Pages publishes this repository. A file under it is probed from the checkout, so a pull
# request adding one passes before Pages has deployed it.
PAGES_ROOT = "https://ouroborostechnologies.github.io/platmark/"

# Each set of closed values in x-platmark-kinds, and the $defs entry in the schema that lists it.
KIND_ENUMS = {
    "source": "sourceKind", "render": "renderKind", "discovery": "discoveryKind", "style": "styleKind",
    "category": "category", "cadence": "cadence", "placement": "placement", "cachePolicy": "cachePolicy",
    "wmsVersion": "wmsVersion", "demEncoding": "demEncoding",
}
# Polymorphic values: the $defs entry that dispatches on "type" to one definition per kind.
KIND_DISPATCH = {"source": "source", "render": "render", "discovery": "discovery", "style": "style"}


# MARK: - Loading

def load(path_or_url, missing_ok=False):
    """The JSON at a path or URL. `None` for a URL that answers 404 when `missing_ok`."""
    text = str(path_or_url)
    if re.match(r"^https?://", text):
        request = urllib.request.Request(text, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing_ok:
                return None
            raise SystemExit(f"{text}: HTTP {error.code}")
        except urllib.error.URLError as error:
            raise SystemExit(f"{text}: {error.reason}")
    with open(text, encoding="utf-8") as handle:
        return json.load(handle)


def obj(value):
    return value if isinstance(value, dict) else {}


def seq(value):
    return value if isinstance(value, list) else []


# MARK: - Walking the catalog

def every_layer(catalog):
    """(where, layer) for each layer, including the layers inside base maps."""
    for index, layer in enumerate(seq(catalog.get("layers"))):
        yield f"layers[{index}] {obj(layer).get('id', '?')}", obj(layer)
    for index, base in enumerate(seq(catalog.get("baseMaps"))):
        base = obj(base)
        for sub, layer in enumerate(seq(base.get("layers"))):
            yield f"baseMaps[{index}] {base.get('id', '?')} layers[{sub}] {obj(layer).get('id', '?')}", obj(layer)


def discovery_of(layer):
    return obj(obj(layer.get("time")).get("discovery"))


def editions_of(discovery):
    """The dated entries a discovery lists: editions, or a service set's members."""
    kind = discovery.get("type")
    if kind in ("fixed", "regional"):
        return seq(discovery.get("editions"))
    if kind == "serviceSet":
        return seq(discovery.get("members"))
    return []


def sources_in(source, where):
    """(where, source) for a source and, recursively, every composite member's source."""
    source = obj(source)
    yield where, source
    if source.get("type") == "composite":
        for index, member in enumerate(seq(source.get("members"))):
            yield from sources_in(obj(member).get("source"), f"{where}.members[{index}]")


def layer_sources(layer, where):
    """Every source a layer can draw: its own, each listed date's, and a monthly series' template."""
    if "source" in layer:
        yield from sources_in(layer["source"], f"{where}.source")
    discovery = discovery_of(layer)
    field = "members" if discovery.get("type") == "serviceSet" else "editions"
    for index, edition in enumerate(editions_of(discovery)):
        if "source" in obj(edition):
            yield from sources_in(edition["source"], f"{where}.time.discovery.{field}[{index}].source")
    if discovery.get("type") == "monthlyClock" and "source" in discovery:
        yield from sources_in(discovery["source"], f"{where}.time.discovery.source")


def date_key(raw):
    """A catalog date as a sortable tuple; a missing month or day counts as the first."""
    parts = [int(part) for part in str(raw).split("-")]
    return (parts + [1, 1])[:3]


def coverage_rects(catalog, value):
    """A coverage value's rectangles, `"global"`, or `None` when it names nothing."""
    if value == "global":
        return "global"
    if isinstance(value, str):
        entry = obj(catalog.get("coverages")).get(value)
        if entry is None:
            return None
        return "global" if entry == "global" else seq(obj(entry).get("rects"))
    if isinstance(value, dict):
        return seq(value.get("rects"))
    return None


# MARK: - Schema

def kind_problems(schema):
    """Where the schema's own enums disagree with x-platmark-kinds."""
    problems = []
    kinds = obj(schema.get("x-platmark-kinds"))
    defs = obj(schema.get("$defs"))
    expected = set(KIND_ENUMS) | {"useStatus"}
    if set(kinds) != expected:
        problems.append(f"x-platmark-kinds has {sorted(kinds)}, expected {sorted(expected)}")
    for kind, name in KIND_ENUMS.items():
        listed = obj(defs.get(name)).get("enum")
        if listed != kinds.get(kind):
            problems.append(f"$defs/{name} lists {listed}, x-platmark-kinds.{kind} lists {kinds.get(kind)}")
    for kind, name in KIND_DISPATCH.items():
        branches = seq(obj(defs.get(name)).get("allOf"))
        consts = [obj(obj(obj(b.get("if")).get("properties")).get("type")).get("const") for b in branches]
        if consts != kinds.get(kind):
            problems.append(f"$defs/{name} dispatches on {consts}, x-platmark-kinds.{kind} lists {kinds.get(kind)}")
        for value, branch in zip(consts, branches):
            if required_type(schema, obj(branch.get("then"))) != value:
                problems.append(f"$defs/{name}: the definition for \"{value}\" doesn't require type \"{value}\"")
    status = seq(obj(defs.get("useStatus")).get("oneOf"))
    listed = None
    if len(status) == 2:
        listed = seq(status[0].get("enum")) + seq(status[1].get("required"))
    if listed != kinds.get("useStatus"):
        problems.append(f"$defs/useStatus allows {listed}, x-platmark-kinds.useStatus lists {kinds.get('useStatus')}")
    return problems


def required_type(schema, node):
    """The "type" a definition pins with `const`, following $ref until one does."""
    while True:
        const = obj(obj(node.get("properties")).get("type")).get("const")
        if const is not None or "$ref" not in node:
            return const
        target = schema
        for part in node["$ref"].lstrip("#/").split("/"):
            target = target[part]
        node = target


def schema_problems(catalog, schema):
    try:
        import jsonschema
    except ImportError:
        raise SystemExit("check.py rules needs the jsonschema package: pip install jsonschema")
    validator = jsonschema.Draft202012Validator(schema)
    problems = []
    for error in sorted(validator.iter_errors(catalog), key=lambda e: list(map(str, e.absolute_path))):
        # A failed if/then or oneOf says little; its deepest cause says what's wrong.
        leaf = error
        while leaf.context:
            leaf = min(leaf.context, key=lambda e: -len(e.absolute_path))
        path = "/".join(map(str, leaf.absolute_path)) or "(top level)"
        problems.append(f"schema: {path}: {leaf.message}")
    return problems


# MARK: - Rules

class Report:
    def __init__(self):
        self.errors = []
        self.warnings = []

    def error(self, message):
        self.errors.append(message)

    def warn(self, message):
        self.warnings.append(message)


def check_rect(report, where, rect):
    rect = seq(rect)
    if len(rect) != 4 or not all(isinstance(v, (int, float)) for v in rect):
        return
    min_lon, min_lat, max_lon, max_lat = rect
    if not (-180 <= min_lon < max_lon <= 180 and -90 <= min_lat < max_lat <= 90):
        report.error(f"{where}: {rect} is not [minLon, minLat, maxLon, maxLat] within -180…180, -90…90")


def check_coverage(report, catalog, where, value, used):
    if isinstance(value, str) and value != "global":
        used["coverages"].add(value)
        if value not in obj(catalog.get("coverages")):
            report.error(f"{where}: no coverage \"{value}\"")
    elif isinstance(value, dict):
        for index, rect in enumerate(seq(value.get("rects"))):
            check_rect(report, f"{where}.rects[{index}]", rect)


def check_zooms(report, where, minimum, maximum):
    for name, value in (("minimumZoom", minimum), ("maximumZoom", maximum)):
        if isinstance(value, (int, float)) and not 0 <= value <= 22:
            report.error(f"{where}: {name} {value} is outside 0…22")
    if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)) and not minimum < maximum:
        report.error(f"{where}: minimumZoom {minimum} is not below maximumZoom {maximum}")


def check_layer(report, catalog, where, layer, used):
    datasets = obj(catalog.get("datasets"))
    groups = {obj(g).get("id") for g in seq(catalog.get("groups"))}
    dataset = layer.get("dataset")
    used["datasets"].add(dataset)
    if dataset not in datasets:
        report.error(f"{where}: no dataset \"{dataset}\"")
    if "group" in layer:
        used["groups"].add(layer["group"])
        if layer["group"] not in groups:
            report.error(f"{where}: no group \"{layer['group']}\"")
    check_zooms(report, where, layer.get("minimumZoom", 0), layer.get("maximumZoom", 22))
    opacity = layer.get("defaultOpacity", 1)
    if isinstance(opacity, (int, float)) and not 0 <= opacity <= 1:
        report.error(f"{where}: defaultOpacity {opacity} is outside 0…1")

    for at, source in layer_sources(layer, where):
        if source.get("type") == "composite":
            for index, member in enumerate(seq(source.get("members"))):
                member = obj(member)
                check_coverage(report, catalog, f"{at}.members[{index}].coverage", member.get("coverage"), used)
                if not str(member.get("attribution", "")).strip():
                    report.error(f"{at}.members[{index}]: no attribution")
                check_zooms(report, f"{at}.members[{index}]", member.get("minimumZoom"), member.get("maximumZoom"))

    time_ = layer.get("time")
    if time_ is None:
        if "source" not in layer:
            report.error(f"{where}: an undated layer needs a source")
        return
    time_ = obj(time_)
    discovery = discovery_of(layer)
    kind = discovery.get("type")
    listed = editions_of(discovery)
    derivable = kind == "monthlyClock" or (kind == "fixed" and listed)
    if "source" not in layer and not derivable:
        report.error(f"{where}: a dated layer may leave out source only when its discovery is fixed (with dates) "
                     f"or monthlyClock; this one is {kind}")
    if "range" not in time_ and not derivable:
        report.error(f"{where}: a dated layer may leave out range only when its discovery is fixed (with dates) "
                     f"or monthlyClock; this one is {kind}")
    dates = seq(time_.get("range"))
    if len(dates) == 2 and all(isinstance(d, str) for d in dates):
        try:
            if date_key(dates[0]) > date_key(dates[1]):
                report.error(f"{where}: range {dates} runs backwards")
        except ValueError:
            pass
    seen = {}
    for index, edition in enumerate(listed):
        edition_id = obj(edition).get("id")
        if edition_id in seen:
            report.error(f"{where}: edition id \"{edition_id}\" is used by entries {seen[edition_id]} and {index}")
        seen.setdefault(edition_id, index)
        if kind == "regional":
            check_rect(report, f"{where}.time.discovery.editions[{index}].extent", obj(edition).get("extent"))
    if kind == "monthlyClock" and "{date}" not in json.dumps(discovery.get("source")):
        report.error(f"{where}: a monthlyClock source must contain {{date}}, or every month draws the same picture")


def rules(catalog, schema):
    report = Report()
    for problem in kind_problems(schema):
        report.error(f"schema: {problem}")
    for problem in schema_problems(catalog, schema):
        report.error(problem)

    used = {name: set() for name in ("providers", "licenses", "coverages", "datasets", "groups")}
    providers = obj(catalog.get("providers"))
    licenses = obj(catalog.get("licenses"))

    for key, entry in obj(catalog.get("coverages")).items():
        for index, rect in enumerate(seq(obj(entry).get("rects"))):
            check_rect(report, f"coverages.{key}.rects[{index}]", rect)

    for key, dataset in obj(catalog.get("datasets")).items():
        dataset = obj(dataset)
        where = f"datasets.{key}"
        provider = dataset.get("provider")
        used["providers"].add(provider)
        if provider not in providers:
            report.error(f"{where}: no provider \"{provider}\"")
        license_ = dataset.get("license")
        if isinstance(license_, str):
            used["licenses"].add(license_)
            if license_ not in licenses:
                report.error(f"{where}: no licence \"{license_}\"")
        elif not str(obj(license_).get("name", "")).strip():
            report.error(f"{where}: no licence")
        if not str(dataset.get("attribution", "")).strip():
            report.error(f"{where}: no attribution")
        check_coverage(report, catalog, f"{where}.coverage", dataset.get("coverage"), used)

    group_ids = {}
    for index, group in enumerate(seq(catalog.get("groups"))):
        group_id = obj(group).get("id")
        if group_id in group_ids:
            report.error(f"groups[{index}]: group id \"{group_id}\" is already used by groups[{group_ids[group_id]}]")
        group_ids.setdefault(group_id, index)

    # IDs: layers and base maps share one namespace. A base map's own layer may appear in several
    # base maps (satellite and hybrid draw the same imagery), but only with the same definition.
    owners = {}
    for index, layer in enumerate(seq(catalog.get("layers"))):
        layer_id = obj(layer).get("id")
        if layer_id in owners:
            report.error(f"layers[{index}]: id \"{layer_id}\" is already used by {owners[layer_id][0]}")
        else:
            owners[layer_id] = (f"layers[{index}]", None)
    base_ids = set()
    for index, base in enumerate(seq(catalog.get("baseMaps"))):
        base = obj(base)
        base_id = base.get("id")
        base_ids.add(base_id)
        if base_id in owners:
            report.error(f"baseMaps[{index}]: id \"{base_id}\" is already used by {owners[base_id][0]}")
        else:
            owners[base_id] = (f"baseMaps[{index}]", None)
        for key in seq(base.get("datasets")):
            used["datasets"].add(key)
            if key not in obj(catalog.get("datasets")):
                report.error(f"baseMaps[{index}] {base_id}: no dataset \"{key}\"")
        for sub, layer in enumerate(seq(base.get("layers"))):
            layer_id = obj(layer).get("id")
            where = f"baseMaps[{index}] {base_id} layers[{sub}]"
            if layer_id in owners:
                first, definition = owners[layer_id]
                if definition is None or definition != layer:
                    report.error(f"{where}: id \"{layer_id}\" is already used by {first}"
                                 + ("" if definition is None else " with a different definition"))
            else:
                owners[layer_id] = (where, layer)
    for required in REQUIRED_BASE_MAPS:
        if required not in base_ids:
            report.error(f"baseMaps: \"{required}\" is required")

    for where, layer in every_layer(catalog):
        check_layer(report, catalog, where, layer, used)

    for table in used:
        defined = obj(catalog.get(table)) if table != "groups" else group_ids
        for key in sorted(set(defined) - used[table]):
            report.warn(f"{table}.{key} is not used by anything")
    return report


def command_rules(args):
    catalog = load(args.file)
    schema = load(SCHEMA)
    report = rules(catalog, schema)
    for warning in report.warnings:
        print(f"warning: {warning}")
    for error in report.errors:
        print(f"error: {error}", file=sys.stderr)
    if report.errors:
        raise SystemExit(f"{len(report.errors)} problem(s) in {args.file}")
    layers = len({layer.get("id") for _, layer in every_layer(catalog)})
    print(f"{args.file}: {layers} layers, {len(seq(catalog.get('baseMaps')))} base maps pass "
          f"({len(report.warnings)} warning(s))")


# MARK: - Revision

def layer_fingerprints(catalog):
    """For each layer ID: everything that decides what it draws, so a change to its dataset or a
    coverage it names counts as a change to the layer. The first definition of an ID wins."""
    datasets = obj(catalog.get("datasets"))
    coverages = obj(catalog.get("coverages"))
    result = {}
    for _, layer in every_layer(catalog):
        layer_id = layer.get("id")
        if layer_id in result:
            continue
        dataset = datasets.get(layer.get("dataset"))
        text = json.dumps(layer, sort_keys=True) + json.dumps(dataset, sort_keys=True)
        named = {key for key in re.findall(r'"coverage": "([^"]+)"', text) if key in coverages}
        result[layer_id] = text + json.dumps({k: coverages[k] for k in sorted(named)}, sort_keys=True)
    return result


def layer_datasets(catalog):
    result = {}
    for _, layer in every_layer(catalog):
        result.setdefault(layer.get("id"), layer.get("dataset"))
    return result


def compare(old, new):
    """Added, removed and changed layer IDs from `old` to `new`."""
    before, after = layer_fingerprints(old), layer_fingerprints(new)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(key for key in set(before) & set(after) if before[key] != after[key])
    return added, removed, changed


def command_revision(args):
    new = load(args.file)
    old = load(args.against, missing_ok=True)
    if old is None:
        print(f"no published catalog at {args.against}; this will be the first release")
        return
    problems = []
    strip = lambda c: {k: v for k, v in c.items() if k != "catalogRevision"}
    old_revision, new_revision = old.get("catalogRevision"), new.get("catalogRevision")
    unchanged = strip(old) == strip(new)
    if unchanged:
        print(f"unchanged from {args.against} apart from catalogRevision ({old_revision} → {new_revision})"
              if old_revision != new_revision else f"unchanged from {args.against} (revision {new_revision})")
    elif not (isinstance(new_revision, int) and isinstance(old_revision, int) and new_revision > old_revision):
        problems.append(f"the catalog changed but catalogRevision did not increase ({old_revision} → {new_revision}); "
                        f"bump it in catalog/src/catalog.json and reassemble")

    old_datasets, new_datasets = obj(old.get("datasets")), obj(new.get("datasets"))
    for layer_id, before in layer_datasets(old).items():
        after = layer_datasets(new).get(layer_id)
        if after is None or after == before:
            continue
        # A dataset key renamed with its entry unchanged is the same data; anything else is reuse.
        if old_datasets.get(before) is not None and old_datasets.get(before) == new_datasets.get(after):
            continue
        problems.append(f"layer id \"{layer_id}\" now points at dataset \"{after}\" instead of \"{before}\"; "
                        f"layer IDs are permanent, so give the new layer a new ID")

    added, removed, changed = compare(old, new)
    if not unchanged:
        print(f"revision {old_revision} → {new_revision}")
    for label, ids in (("added", added), ("removed", removed), ("changed", changed)):
        if ids:
            print(f"{label} ({len(ids)}): {', '.join(ids)}")
    if not unchanged and not (added or removed or changed):
        print("no layer changed; the difference is in shared tables or base map settings")
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if problems:
        raise SystemExit(1)


# MARK: - Tile URLs (as the app builds them)

def fill_token(source, token, value):
    """The source with a placeholder filled in, wherever a template or query value can hold one."""
    source = json.loads(json.dumps(source))
    fill = lambda text: text.replace(token, value)
    kind = source.get("type")
    if kind in ("xyzRaster", "wmts", "vectorTiles", "rasterDEM"):
        source["urlTemplate"] = fill(source["urlTemplate"])
    elif kind == "arcGISTiled":
        source["serviceURL"] = fill(source["serviceURL"])
    elif kind in ("arcGISMapExport", "arcGISImageExport"):
        source["serviceURL"] = fill(source["serviceURL"])
        source["additionalQuery"] = {k: fill(v) for k, v in obj(source.get("additionalQuery")).items()}
    elif kind == "wms":
        source["endpoint"] = fill(source["endpoint"])
        source["additionalQuery"] = {k: fill(v) for k, v in obj(source.get("additionalQuery")).items()}
    elif kind == "composite":
        for member in source["members"]:
            member["source"] = fill_token(member["source"], token, value)
    return source


def escape(value):
    """A query value as the app escapes it. A whole value in braces is left alone, so a placeholder
    such as {bbox-epsg-3857} survives for the renderer to substitute."""
    if value.startswith("{") and value.endswith("}"):
        return value
    return urllib.parse.quote(value, safe="!$'()*,-./:;@_~")


def encoded_query(items):
    return "&".join(f"{key}={escape(value)}" for key, value in items)


def tile_template(source):
    """(url template, maximum level) for a single, non-composite source."""
    kind = source["type"]
    if kind in ("xyzRaster", "wmts"):
        return source["urlTemplate"], source.get("maximumLevel")
    if kind in ("vectorTiles", "rasterDEM"):
        return source["urlTemplate"], None
    if kind == "arcGISTiled":
        base = source["serviceURL"][:-1] if source["serviceURL"].endswith("/") else source["serviceURL"]
        ext = source.get("fileExtension")
        return f"{base}/tile/{{z}}/{{y}}/{{x}}" + (f".{ext}" if ext is not None else ""), source.get("maximumLevel")
    if kind in ("arcGISMapExport", "arcGISImageExport"):
        ratio = max(source.get("pixelRatio", 2), 1)
        pixels = source.get("tileSize", 256) * ratio
        items = [
            ("bbox", "{bbox-epsg-3857}"), ("bboxSR", "3857"), ("imageSR", "3857"),
            ("size", f"{pixels},{pixels}"), ("dpi", str(96 * ratio)),
            ("format", source.get("imageFormat", "png32")),
            ("transparent", "true" if source.get("transparent", True) else "false"), ("f", "image"),
        ]
        for key, name in (("visibleLayers", "layers"), ("mosaicRule", "mosaicRule"), ("renderingRule", "renderingRule")):
            if key in source:
                items.append((name, source[key]))
        query = obj(source.get("additionalQuery"))
        items += [(key, query[key]) for key in sorted(query)]
        base = source["serviceURL"][:-1] if source["serviceURL"].endswith("/") else source["serviceURL"]
        path = "export" if kind == "arcGISMapExport" else "exportImage"
        return f"{base}/{path}?" + encoded_query(items), None
    if kind == "wms":
        pixels = source.get("tileSize", 256) * max(source.get("pixelRatio", 2), 1)
        version = source["version"]
        items = [
            ("service", "WMS"), ("version", version), ("request", "GetMap"), ("layers", source["layers"]),
            ("styles", source.get("styles", "")), ("srs" if version == "1.1.1" else "crs", "EPSG:3857"),
            ("bbox", "{bbox-epsg-3857}"), ("width", str(pixels)), ("height", str(pixels)),
            ("format", source.get("imageFormat", "image/png")),
            ("transparent", "TRUE" if source.get("transparent", True) else "FALSE"),
        ]
        query = obj(source.get("additionalQuery"))
        items += [(key, query[key]) for key in sorted(query)]
        endpoint = source["endpoint"]
        return endpoint + ("&" if "?" in endpoint else "?") + encoded_query(items), None
    raise ValueError(f"no tile template for source type {kind}")


def tile_at(lon, lat, z):
    n = 2 ** z
    lat = max(min(lat, 85.0511), -85.0511)
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def mercator_bbox(z, x, y):
    half = math.pi * 6378137
    size = 2 * half / 2 ** z
    return ",".join(repr(round(v, 6)) for v in (-half + x * size, half - (y + 1) * size, -half + (x + 1) * size, half - y * size))


def tile_url(template, z, x, y):
    """The renderer's substitution of a template's placeholders, then the escaping the system's URL
    loading applies to characters a URL can't hold (a renderingRule's JSON goes out unescaped)."""
    quadkey = "".join(str(((x >> i) & 1) + 2 * ((y >> i) & 1)) for i in range(z - 1, -1, -1))
    url = (template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
           .replace("{bbox-epsg-3857}", mercator_bbox(z, x, y)).replace("{quadkey}", quadkey)
           .replace("{prefix}", f"{x % 16:x}{y % 16:x}").replace("{ratio}", "@2x"))
    leftover = re.findall(r"\{[A-Za-z0-9_-]+\}", url)
    if leftover:
        raise ValueError(f"placeholder {leftover[0]} is not one the renderer fills in")
    return urllib.parse.quote(url, safe="!#$%&'()*+,/:;=?@[]~")


# MARK: - Probe

def month_ago_stamp(series):
    """The day a monthly series pictures in the month about thirty days back, as yyyy-mm-dd."""
    when = datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=30)
    key = f"{when.year:04d}-{when.month:02d}"
    day = obj(series.get("substituteDays")).get(key, series.get("dayOfMonth", 15))
    next_month = datetime.date(when.year + when.month // 12, when.month % 12 + 1, 1)
    day = min(day, (next_month - datetime.timedelta(days=1)).day)
    return f"{key}-{day:02d}"


def drawn_source(layer):
    """(source, note): what the app draws for a layer before a date is picked."""
    discovery = discovery_of(layer)
    kind = discovery.get("type")
    if "source" in layer:
        return layer["source"], None
    if kind == "monthlyClock":
        stamp = month_ago_stamp(discovery)
        return fill_token(discovery["source"], "{date}", stamp), f"monthlyClock date {stamp}"
    if kind == "fixed" and discovery.get("editions"):
        newest = None
        for edition in discovery["editions"]:
            if newest is None or date_key(edition["date"]) >= date_key(newest["date"]):
                newest = edition
        return newest["source"], f"newest date {newest['id']} ({newest['date']})"
    return None, None


def probe_targets(catalog, only=None):
    """One probe per drawable source: each layer's drawn source, or each member of a composite."""
    datasets = obj(catalog.get("datasets"))
    targets, seen = [], set()
    for _, layer in every_layer(catalog):
        layer_id = layer.get("id")
        if layer_id in seen or (only is not None and layer_id not in only):
            continue
        seen.add(layer_id)
        dataset = obj(datasets.get(layer.get("dataset")))
        base = {"layer": layer_id, "member": None, "minimumZoom": layer.get("minimumZoom", 0),
                "maximumZoom": layer.get("maximumZoom", 22)}
        if dataset.get("requiresBackendProxy"):
            targets.append({**base, "skip": "dataset requires the backend proxy"})
            continue
        source, note = drawn_source(layer)
        if source is None:
            targets.append({**base, "skip": "no source to probe"})
            continue
        coverage = coverage_rects(catalog, dataset.get("coverage"))
        discovery = discovery_of(layer)
        if discovery.get("type") == "regional":
            # A storm flight covers a strip, not the dataset's whole area; probe inside the strip.
            for edition in discovery.get("editions", []):
                if edition.get("source") == source:
                    coverage = [edition["extent"]]
        flatten(targets, base, source, coverage, note, catalog, None)
    return targets


def flatten(targets, base, source, coverage, note, catalog, prefix):
    if source.get("type") != "composite":
        targets.append({**base, "source": source, "coverage": coverage, "note": note,
                        "member": prefix})
        return
    for index, member in enumerate(source.get("members", [])):
        label = str(index) if prefix is None else f"{prefix}.{index}"
        nested = {**base,
                  "minimumZoom": member.get("minimumZoom", base["minimumZoom"]),
                  "maximumZoom": member.get("maximumZoom", base["maximumZoom"])}
        flatten(targets, nested, member["source"], coverage_rects(catalog, member.get("coverage")), note,
                catalog, label)


def plan(target):
    """The URL to request for a target, and the zoom it's at."""
    template, maximum_level = tile_template(target["source"])
    low, high = math.ceil(target["minimumZoom"]), math.floor(target["maximumZoom"])
    z = max(low, min(10, high))
    if maximum_level is not None:
        z = min(z, maximum_level)
    coverage = target["coverage"]
    contains = lambda r, p: r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3]
    if coverage == "global" or not coverage or any(contains(rect, CONUS_POINT) for rect in coverage):
        # Worldwide or nationwide: the middle of a rectangle running from Alaska to Maine is in
        # Oregon, where a national service may hold nothing, so ask about the middle of the country.
        lon, lat = CONUS_POINT
    else:
        min_lon, min_lat, max_lon, max_lat = coverage[0]
        lon, lat = (min_lon + max_lon) / 2, (min_lat + max_lat) / 2
    x, y = tile_at(lon, lat, z)
    return tile_url(template, z, x, y), z


class HostGate:
    """At most two requests at a time to any one host, and a short gap between them."""

    def __init__(self, per_host=2, gap=0.25):
        self.per_host, self.gap = per_host, gap
        self.lock = threading.Lock()
        self.hosts = {}

    def __call__(self, host):
        with self.lock:
            if host not in self.hosts:
                self.hosts[host] = [threading.Semaphore(self.per_host), threading.Lock(), 0.0]
            return self.hosts[host]


IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"II*\x00", b"MM\x00*")
TILE_TYPES = ("application/x-protobuf", "application/vnd.mapbox-vector-tile", "application/octet-stream")


def is_image_bytes(body):
    return body.startswith(IMAGE_MAGIC) or (body[:4] == b"RIFF" and body[8:12] == b"WEBP")


def fetch(url, timeout, gate):
    semaphore, spacing, _ = slot = gate(urllib.parse.urlsplit(url).hostname or "")
    with semaphore:
        with spacing:
            wait = slot[2] + gate.gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            slot[2] = time.monotonic()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*,*/*;q=0.8"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.headers.get("Content-Type", ""), response.read(8 << 20), None
        except urllib.error.HTTPError as error:
            body = error.read(2048) if error.fp else b""
            return error.code, error.headers.get("Content-Type", ""), body, f"HTTP {error.code}"
        except Exception as error:  # timeouts, refused connections, TLS failures
            reason = getattr(error, "reason", error)
            return None, "", b"", f"{type(reason).__name__}: {reason}"


def probe_one(target, timeout, retries, gate):
    result = {"layer": target["layer"], "member": target["member"], "url": None, "zoom": None, "status": None,
              "contentType": None, "error": None, "ok": False, "skipped": False, "note": target.get("note")}
    if "skip" in target:
        return {**result, "skipped": True, "note": target["skip"]}
    kind = target["source"]["type"]
    if kind == "vectorTiles":
        return {**result, "skipped": True, "note": "vector tiles are not probed"}
    if kind == "geoJSON":
        return probe_points(target, result, timeout, retries, gate)
    try:
        result["url"], result["zoom"] = plan(target)
    except (ValueError, KeyError, TypeError) as error:
        return {**result, "error": f"can't build a tile URL: {error}"}
    for attempt in range(retries + 1):
        status, content_type, body, error = fetch(result["url"], timeout, gate)
        if not (status is None or status >= 500 or status == 429) or attempt == retries:
            break
        time.sleep(2)
    result.update(status=status, contentType=content_type or None)
    media = content_type.split(";")[0].strip().lower()
    if error:
        result["error"] = error
    elif status != 200:
        result["error"] = f"HTTP {status}"
    elif not body:
        result["error"] = "empty response"
    elif media.startswith("image/") or (kind == "rasterDEM" and media in TILE_TYPES):
        result["ok"] = True
    elif is_image_bytes(body):
        result["ok"] = True
        result["note"] = ((result["note"] + "; ") if result["note"] else "") + f"served as {media or 'no type'}, body is an image"
    else:
        snippet = body[:160].decode("utf-8", "replace").replace("\n", " ").strip()
        result["error"] = f"content type {media or 'missing'} is not an image: {snippet}"
    return result


def probe_point(target):
    """The place a point source is asked about: as `plan` chooses for tiles."""
    coverage = target["coverage"]
    contains = lambda r, p: r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3]
    if coverage == "global" or not coverage or any(contains(rect, CONUS_POINT) for rect in coverage):
        return CONUS_POINT
    min_lon, min_lat, max_lon, max_lat = coverage[0]
    return (min_lon + max_lon) / 2, (min_lat + max_lat) / 2


def probe_points(target, result, timeout, retries, gate):
    """Asks a geoJSON source about a degree-wide box at the probe point, and checks the answer is
    the shape the app reads: a FeatureCollection, or a list of records at the mapped path."""
    source = target["source"]
    lon, lat = probe_point(target)
    box = {"{west}": lon - 0.5, "{south}": lat - 0.5, "{east}": lon + 0.5, "{north}": lat + 0.5}
    url = source["url"]
    for token, value in box.items():
        url = url.replace(token, f"{value:.4f}")
    result["url"] = url
    if url.startswith(PAGES_ROOT):
        path = ROOT / url[len(PAGES_ROOT):]
        if not path.is_file():
            return {**result, "error": f"published from this repository, but {path.relative_to(ROOT)} is missing"}
        status, content_type, body, error = 200, "application/geo+json", path.read_bytes(), None
        result["note"] = f"read from {path.relative_to(ROOT)}"
    else:
        for attempt in range(retries + 1):
            status, content_type, body, error = fetch(url, timeout, gate)
            if not (status is None or status >= 500 or status == 429) or attempt == retries:
                break
            time.sleep(2)
    result.update(status=status, contentType=(content_type or None))
    if error or status != 200:
        return {**result, "error": error or f"HTTP {status}"}
    try:
        answer = json.loads(body)
    except ValueError:
        snippet = body[:160].decode("utf-8", "replace").replace("\n", " ").strip()
        return {**result, "error": f"not JSON: {snippet}"}
    mapping = source.get("records")
    if mapping is None:
        features = answer.get("features") if isinstance(answer, dict) else None
        if answer.get("type") != "FeatureCollection" or not isinstance(features, list):
            return {**result, "error": "not a GeoJSON FeatureCollection"}
        count = len(features)
    else:
        node = answer
        for key in [k for k in mapping.get("path", "").split(".") if k]:
            node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, list):
            return {**result, "error": f"no list of records at \"{mapping.get('path', '')}\": {str(answer)[:160]}"}
        count = sum(1 for r in node if isinstance(r, dict) and mapping["longitude"] in r and mapping["latitude"] in r)
    result["note"] = ((result["note"] + "; ") if result["note"] else "") + f"{count} points"
    return {**result, "ok": True}


def command_probe(args):
    catalog = load(args.file)
    only = None
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        known = {layer.get("id") for _, layer in every_layer(catalog)}
        bases = {obj(b).get("id"): obj(b) for b in seq(catalog.get("baseMaps"))}
        only = set()
        for item in wanted:
            if item in bases:
                only |= {obj(layer).get("id") for layer in seq(bases[item].get("layers"))}
            elif item in known:
                only.add(item)
            else:
                raise SystemExit(f"--only: no layer or base map \"{item}\"")
    if args.changed_since:
        old = load(args.changed_since, missing_ok=True)
        if old is None:
            print(f"no published catalog at {args.changed_since}; probing every layer")
        else:
            added, _, changed = compare(old, catalog)
            changed_ids = set(added) | set(changed)
            only = changed_ids if only is None else only & changed_ids
            print(f"probing {len(only)} layer(s) added or changed since {args.changed_since}")

    targets = probe_targets(catalog, only)
    gate = HostGate()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(lambda t: probe_one(t, args.timeout, args.retries, gate), targets))

    for result in results:
        label = result["layer"] + ("" if result["member"] is None else f" [member {result['member']}]")
        if result["skipped"]:
            print(f"skip  {label}: {result['note']}")
        elif result["ok"]:
            where = "" if result["zoom"] is None else f" z{result['zoom']}"
            print(f"ok    {label}: {result['status']} {result['contentType']}{where}"
                  + (f" ({result['note']})" if result["note"] else ""))
        else:
            print(f"FAIL  {label}: {result['error']}\n      {result['url']}")
    failed = [r for r in results if not r["ok"] and not r["skipped"]]
    passed = sum(1 for r in results if r["ok"])
    skipped = sum(1 for r in results if r["skipped"])
    print(f"{passed} passed, {len(failed)} failed, {skipped} skipped")
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    if failed:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    rules_parser = commands.add_parser("rules", help="schema validation and catalog rules")
    rules_parser.set_defaults(run=command_rules)

    revision_parser = commands.add_parser("revision", help="compare with the published catalog")
    revision_parser.add_argument("--against", default=LATEST_URL, help="the previous catalog, a path or URL")
    revision_parser.set_defaults(run=command_revision)

    probe_parser = commands.add_parser("probe", help="request one tile from every source")
    probe_parser.add_argument("--only", help="comma-separated layer or base map IDs")
    probe_parser.add_argument("--changed-since", help="probe only layers added or changed since this catalog (path or URL)")
    probe_parser.add_argument("--timeout", type=float, default=20, help="seconds per request (default 20)")
    probe_parser.add_argument("--retries", type=int, default=1, help="retries after a timeout or server error (default 1)")
    probe_parser.add_argument("--concurrency", type=int, default=8)
    probe_parser.add_argument("--json", help="write per-source results to this file")
    probe_parser.set_defaults(run=command_probe)

    for sub in (rules_parser, revision_parser, probe_parser):
        sub.add_argument("--file", default=str(CATALOG), help="the catalog to check (default catalog/catalog-v1.json)")
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
