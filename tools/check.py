#!/usr/bin/env python3
"""Checks the assembled layer catalog before it ships, and probes its tile services.

    python3 tools/check.py rules                       # schema + catalog rules on catalog/catalog-v1.sqlite
    python3 tools/check.py revision [--against P|URL]  # compare with the published catalog
    python3 tools/check.py probe [--only a,b] [--timeout s] [--json out.json] [--changed-since P|URL]

`rules` needs `jsonschema` (pip install jsonschema); everything else is the standard library.
Every command takes `--file` to check a catalog other than catalog/catalog-v1.sqlite, which
`tools/catalog.py assemble` builds.

The app's decoder is the authority on the format. This file describes the same format in Python so
the public repository can check a catalog without the app's source. The tile URL templates are in
the catalog itself, built by `tools/catalog.py assemble`; `probe` fills them the way the renderer does.
"""
import argparse
import concurrent.futures
import datetime
import json
import math
import re
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog" / "catalog-v1.sqlite"
SCHEMA = ROOT / "catalog" / "catalog-v1.schema.json"
LATEST_URL = "https://github.com/ouroborostechnologies/platmark/releases/latest/download/catalog-v1.sqlite"
USER_AGENT = "PlatmarkCatalogProbe/1"
REQUIRED_BASE_MAPS = ("basemap.standard", "basemap.satellite", "basemap.hybrid")

# A point in the conterminous US, for probing a source that covers the whole world.
CONUS_POINT = (-98.58, 39.83)
# Coverages whose rectangle's middle holds nothing to draw, and a point that does.
PROBE_POINTS = {
    "hawaii": (-155.5, 19.6),  # the middle is open ocean between the islands
    "louisiana": (-90.7, 29.6),  # the newest Louisiana flights cover the coast only
}
# Where GitHub Pages publishes this repository. A file under it is probed from the checkout, so a pull
# request adding one passes before Pages has deployed it.
PAGES_ROOT = "https://ouroborostechnologies.github.io/platmark/"

# Each set of closed values in x-platmark-kinds, and the $defs entry in the schema that lists it.
KIND_ENUMS = {
    "source": "sourceKind", "render": "renderKind", "discovery": "discoveryKind", "style": "styleKind",
    "category": "category", "cadence": "cadence", "placement": "placement", "cachePolicy": "cachePolicy",
    "demEncoding": "demEncoding",
}
# Polymorphic values: the $defs entry that dispatches on "type" to one definition per kind.
KIND_DISPATCH = {"source": "source", "render": "render", "discovery": "discovery", "style": "style"}
# Discoveries whose source is a template the app fills in for each date, and the placeholder it fills.
FILLED_BY_DISCOVERY = {"monthlyClock": "{date}", "wayback": "{release}"}


# MARK: - Loading

def load(path_or_url, missing_ok=False):
    """The catalog (a `.sqlite` database) or JSON at a path or URL. `None` for a URL that answers 404
    when `missing_ok`."""
    text = str(path_or_url)
    is_database = text.endswith(".sqlite")
    if re.match(r"^https?://", text):
        request = urllib.request.Request(text, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404 and missing_ok:
                return None
            raise SystemExit(f"{text}: HTTP {error.code}")
        except urllib.error.URLError as error:
            raise SystemExit(f"{text}: {error.reason}")
        if not is_database:
            return json.loads(body.decode("utf-8"))
        with tempfile.TemporaryDirectory() as scratch:
            downloaded = Path(scratch) / "catalog.sqlite"
            downloaded.write_bytes(body)
            return read_database(downloaded)
    if is_database:
        if not Path(text).exists():
            raise SystemExit(f"{text} is missing; run tools/catalog.py assemble")
        return read_database(text)
    with open(text, encoding="utf-8") as handle:
        return json.load(handle)


def read_database(path):
    """A published catalog as the tables and lists `tools/catalog.py` assembled it from, so the rules
    and the probe read one shape whatever the file."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row

    def entry(row, fields, packed=()):
        """The row's values under their catalog names, leaving out what the row leaves out."""
        result = {name: row[column] for name, column in fields.items() if row[column] is not None}
        result.update({name: json.loads(row[column]) for name, column in packed if row[column] is not None})
        return result

    def layer(row):
        result = entry(row, {"id": "id", "name": "name", "summary": "summary", "category": "category",
                             "group": "layer_group", "minimumZoom": "minimum_zoom", "maximumZoom": "maximum_zoom",
                             "defaultOpacity": "default_opacity"}, (("render", "render"), ("time", "time")))
        result["members"] = [entry(member, {"dataset": "dataset"}, (("source", "source"),)) for member in db.execute(
            "SELECT dataset, source FROM layer_members WHERE layer = ? ORDER BY position", (row["id"],))]
        return result

    try:
        schema_version, revision = db.execute("SELECT schema_version, catalog_revision FROM meta").fetchone()
        catalog = {"schemaVersion": schema_version, "catalogRevision": revision}
        catalog["providers"] = {row["id"]: entry(row, {"name": "name", "abbreviation": "abbreviation",
                                                       "officialURL": "official_url", "summary": "summary"})
                                for row in db.execute("SELECT * FROM providers ORDER BY id")}
        catalog["licenses"] = {row["id"]: entry(row, {"name": "name", "url": "url"})
                               for row in db.execute("SELECT * FROM licenses ORDER BY id")}
        catalog["coverages"] = {row["id"]: {"label": row["label"], "rects": []}
                                for row in db.execute("SELECT * FROM coverages ORDER BY id")}
        for row in db.execute("SELECT coverage, rect FROM coverage_rects ORDER BY id"):
            catalog["coverages"][row["coverage"]]["rects"].append(json.loads(row["rect"]))
        catalog["datasets"] = {}
        for row in db.execute("SELECT * FROM datasets ORDER BY id"):
            dataset = entry(row, {"name": "name", "provider": "provider", "summary": "summary",
                                  "officialURL": "official_url", "serviceURL": "service_url",
                                  "attribution": "attribution", "license": "license", "coverage": "coverage",
                                  "cachePolicy": "cache_policy", "useStatus": "use_status", "access": "access",
                                  "cost": "cost"}, (("themes", "themes"), ("formats", "formats")))
            dataset.setdefault("coverage", "global")
            if row["use_status"] == "restricted":
                dataset["useStatus"] = {"restricted": row["restricted_reason"]}
            catalog["datasets"][row["id"]] = dataset
        catalog["groups"] = [entry(row, {"id": "id", "title": "title", "systemImage": "system_image",
                                         "explanation": "explanation"})
                             for row in db.execute("SELECT * FROM groups ORDER BY position")]
        catalog["layers"] = [layer(row) for row in db.execute(
            "SELECT * FROM layers WHERE position IS NOT NULL ORDER BY position")]
        catalog["baseMaps"] = []
        for row in db.execute("SELECT * FROM base_maps ORDER BY position"):
            base = entry(row, {"id": "id", "name": "name", "summary": "summary", "systemImage": "system_image",
                               "placement": "placement"}, (("style", "style"),))
            own = [layer(inner) for inner in db.execute(
                "SELECT layers.* FROM base_map_layers JOIN layers ON layers.id = base_map_layers.layer "
                "WHERE base_map = ? ORDER BY base_map_layers.position", (row["id"],))]
            if own:
                base["layers"] = own
            base["datasets"] = [credit["dataset"] for credit in db.execute(
                "SELECT dataset FROM base_map_datasets WHERE base_map = ? ORDER BY position", (row["id"],))]
            catalog["baseMaps"].append(base)
        return catalog
    except (sqlite3.Error, TypeError) as error:
        raise SystemExit(f"{path}: not a catalog database ({error})")
    finally:
        db.close()


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


def members_of(layer):
    """A layer's publishers: each names a dataset and, unless the layer's dates supply one, a source."""
    return [obj(member) for member in seq(layer.get("members"))]


def dataset_keys(layer):
    """The datasets a layer draws from. A catalog published before layers listed members names one."""
    if "members" in layer:
        return [member.get("dataset") for member in members_of(layer)]
    return [layer.get("dataset")]


def date_key(raw):
    """A catalog date as a sortable tuple; a missing month or day counts as the first."""
    parts = [int(part) for part in str(raw).split("-")]
    return (parts + [1, 1])[:3]


def coverage_rects(catalog, key):
    """The rectangles of the coverage a dataset names, `"global"`, or `None` when it names nothing."""
    if key == "global":
        return "global"
    entry = obj(catalog.get("coverages")).get(key) if isinstance(key, str) else None
    if entry is None:
        return None
    return "global" if entry == "global" else seq(obj(entry).get("rects"))


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


def check_coverage(report, catalog, where, key, used):
    if key == "global":
        return
    used["coverages"].add(key)
    if key not in obj(catalog.get("coverages")):
        report.error(f"{where}: no coverage \"{key}\"")


def check_zooms(report, where, minimum, maximum):
    for name, value in (("minimumZoom", minimum), ("maximumZoom", maximum)):
        if isinstance(value, (int, float)) and not 0 <= value <= 22:
            report.error(f"{where}: {name} {value} is outside 0…22")
    if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)) and not minimum < maximum:
        report.error(f"{where}: minimumZoom {minimum} is not below maximumZoom {maximum}")


def check_layer(report, catalog, where, layer, used):
    datasets = obj(catalog.get("datasets"))
    groups = {obj(g).get("id") for g in seq(catalog.get("groups"))}
    members = members_of(layer)
    if not members:
        report.error(f"{where}: a layer needs at least one member")
    for index, member in enumerate(members):
        dataset = member.get("dataset")
        used["datasets"].add(dataset)
        if dataset not in datasets:
            report.error(f"{where}.members[{index}]: no dataset \"{dataset}\"")
    if "group" in layer:
        used["groups"].add(layer["group"])
        if layer["group"] not in groups:
            report.error(f"{where}: no group \"{layer['group']}\"")
    check_zooms(report, where, layer.get("minimumZoom", 0), layer.get("maximumZoom", 22))
    opacity = layer.get("defaultOpacity", 1)
    if isinstance(opacity, (int, float)) and not 0 <= opacity <= 1:
        report.error(f"{where}: defaultOpacity {opacity} is outside 0…1")

    unsourced = [index for index, member in enumerate(members) if "source" not in member]
    time_ = layer.get("time")
    if time_ is None:
        for index in unsourced:
            report.error(f"{where}.members[{index}]: a member of an undated layer needs a source")
        return
    time_ = obj(time_)
    if len(members) > 1:
        report.error(f"{where}: a dated layer has exactly one member; this one has {len(members)}")
    discovery = discovery_of(layer)
    kind = discovery.get("type")
    listed = editions_of(discovery)
    derivable = kind == "monthlyClock" or (kind == "fixed" and listed)
    if unsourced and not derivable:
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
    for token_kind, token in FILLED_BY_DISCOVERY.items():
        if kind == token_kind and token not in str(obj(discovery.get("source")).get("urlTemplate")):
            report.error(f"{where}: a {kind} source must contain {token}, or every date draws the same picture")


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
        used["licenses"].add(license_)
        if license_ not in licenses:
            report.error(f"{where}: no licence \"{license_}\"")
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

    # A dataset no layer draws is a source that is listed and not yet drawn, so it isn't reported.
    for table in used:
        if table == "datasets":
            continue
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
    """For each layer ID: everything that decides what it draws, so a change to a dataset or a
    coverage it names counts as a change to the layer. The first definition of an ID wins."""
    datasets = obj(catalog.get("datasets"))
    coverages = obj(catalog.get("coverages"))
    result = {}
    for _, layer in every_layer(catalog):
        layer_id = layer.get("id")
        if layer_id in result:
            continue
        named = [datasets.get(key) for key in dataset_keys(layer)]
        text = json.dumps(layer, sort_keys=True) + json.dumps(named, sort_keys=True)
        named = {key for key in re.findall(r'"coverage": "([^"]+)"', text) if key in coverages}
        result[layer_id] = text + json.dumps({k: coverages[k] for k in sorted(named)}, sort_keys=True)
    return result


def first_definitions(catalog):
    """Each layer by ID. A base map's layer may be listed in several base maps; the first one wins."""
    result = {}
    for _, layer in every_layer(catalog):
        result.setdefault(layer.get("id"), layer)
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
    new_layers = first_definitions(new)
    for layer_id, old_layer in first_definitions(old).items():
        if layer_id not in new_layers:
            continue
        # Before layers listed members, a layer combining several services named one dataset that
        # stood for all of its publishers, so there is nothing to compare its members with. This
        # can go once a catalog with members has been released.
        if obj(old_layer.get("source")).get("type") == "composite":
            continue
        before, after = dataset_keys(old_layer), dataset_keys(new_layers[layer_id])
        # Keeping any of its datasets makes it the same layer, and a dataset key renamed with its
        # entry unchanged is the same data. Anything else is reuse.
        entries = [old_datasets.get(key) for key in before if old_datasets.get(key) is not None]
        if set(before) & set(after) or any(new_datasets.get(key) in entries for key in after):
            continue
        problems.append(f"layer id \"{layer_id}\" now draws from {after} instead of {before}; "
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


# MARK: - Tile URLs (as the renderer fills them)

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


def drawn_source(layer, member):
    """(source, note): what the app draws for one of a layer's members before a date is picked."""
    discovery = discovery_of(layer)
    kind = discovery.get("type")
    if "source" in member:
        return member["source"], None
    if kind == "monthlyClock":
        stamp = month_ago_stamp(discovery)
        template = discovery["source"]["urlTemplate"].replace(FILLED_BY_DISCOVERY[kind], stamp)
        return {**discovery["source"], "urlTemplate": template}, f"monthlyClock date {stamp}"
    if kind == "fixed" and discovery.get("editions"):
        newest = None
        for edition in discovery["editions"]:
            if newest is None or date_key(edition["date"]) >= date_key(newest["date"]):
                newest = edition
        return newest["source"], f"newest date {newest['id']} ({newest['date']})"
    return None, None


def probe_targets(catalog, only=None):
    """One probe per drawable source: each member of each layer."""
    datasets = obj(catalog.get("datasets"))
    targets, seen = [], set()
    for _, layer in every_layer(catalog):
        layer_id = layer.get("id")
        if layer_id in seen or (only is not None and layer_id not in only):
            continue
        seen.add(layer_id)
        members = members_of(layer)
        discovery = discovery_of(layer)
        for index, member in enumerate(members):
            target = {"layer": layer_id, "member": None if len(members) == 1 else str(index),
                      "minimumZoom": layer.get("minimumZoom", 0), "maximumZoom": layer.get("maximumZoom", 22)}
            if discovery.get("type") == "sheetIndex":
                targets.append({**target, "skip": "drawn from the app's sheet index, not from the source"})
                continue
            source, note = drawn_source(layer, member)
            if source is None:
                targets.append({**target, "skip": "no source to probe"})
                continue
            coverage_key = obj(datasets.get(member.get("dataset"))).get("coverage")
            coverage = coverage_rects(catalog, coverage_key)
            point = PROBE_POINTS.get(coverage_key)
            if discovery.get("type") == "regional":
                point = None
                # A storm flight covers a strip, not the dataset's whole area; probe inside the strip.
                for edition in discovery.get("editions", []):
                    if edition.get("source") == source:
                        coverage = [edition["extent"]]
            targets.append({**target, "source": source, "coverage": coverage, "point": point, "note": note})
    return targets


def plan(target):
    """The URL to request for a target, and the zoom it's at."""
    template, maximum_level = target["source"]["urlTemplate"], target["source"].get("maximumLevel")
    low, high = math.ceil(target["minimumZoom"]), math.floor(target["maximumZoom"])
    z = max(low, min(10, high))
    if maximum_level is not None:
        z = min(z, maximum_level)
    deepest = high if maximum_level is None else min(high, maximum_level)
    coverage = target["coverage"]
    if coverage != "global" and coverage:
        # A cache made for a small area may hold no tile wider than the area itself.
        while z < deepest and 360 / 2 ** z > coverage[0][2] - coverage[0][0]:
            z += 1
    lon, lat = probe_point(target)
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
    if kind == "vector":
        return {**result, "skipped": True, "note": "vector tiles are not probed"}
    if kind == "features":
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
    elif media.startswith("image/") or (kind == "dem" and media in TILE_TYPES):
        result["ok"] = True
    elif is_image_bytes(body):
        result["ok"] = True
        result["note"] = ((result["note"] + "; ") if result["note"] else "") + f"served as {media or 'no type'}, body is an image"
    else:
        snippet = body[:160].decode("utf-8", "replace").replace("\n", " ").strip()
        result["error"] = f"content type {media or 'missing'} is not an image: {snippet}"
    return result


def probe_point(target):
    """The place a source is asked about."""
    if target["point"]:
        return target["point"]
    coverage = target["coverage"]
    contains = lambda r, p: r[0] <= p[0] <= r[2] and r[1] <= p[1] <= r[3]
    if coverage == "global" or not coverage or any(contains(rect, CONUS_POINT) for rect in coverage):
        # Worldwide or nationwide: the middle of a rectangle running from Alaska to Maine is in
        # Oregon, where a national service may hold nothing, so ask about the middle of the country.
        return CONUS_POINT
    min_lon, min_lat, max_lon, max_lat = coverage[0]
    return (min_lon + max_lon) / 2, (min_lat + max_lat) / 2


def probe_points(target, result, timeout, retries, gate):
    """Asks a features source about a degree-wide box at the probe point, and checks the answer is
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
        sub.add_argument("--file", default=str(CATALOG), help="the catalog to check (default catalog/catalog-v1.sqlite)")
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
