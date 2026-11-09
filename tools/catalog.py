#!/usr/bin/env python3
"""Builds the published layer catalog from its source fragments.

    python3 tools/catalog.py format      # rewrite catalog/src/ in the canonical layout
    python3 tools/catalog.py assemble    # write catalog/catalog-v1.json from catalog/src/
    python3 tools/catalog.py verify      # fail if src/ isn't formatted or the assembled file is stale

`catalog/src/catalog.json` holds the header and lists the fragments in order. Each fragment is a
part of the catalog: any of the tables (`providers`, `licenses`, `coverages`, `datasets`, keyed by
ID) and lists (`groups`, `layers`, `baseMaps`). Assembling merges the tables, refusing a key defined
twice, and concatenates the lists in fragment order, which is the order the app shows layers in.

There is no templating. Every URL is written out, so every URL can be reviewed and probed.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "catalog" / "src"
INDEX = SRC / "catalog.json"
SCHEMA_VERSION = 1
OUTPUT = ROOT / "catalog" / f"catalog-v{SCHEMA_VERSION}.json"

TABLES = ("providers", "licenses", "coverages", "datasets")
LISTS = ("groups", "layers", "baseMaps")

# Keys that read best first, in this order. Anything else follows alphabetically.
KEY_ORDER = [
    "schemaVersion", "catalogRevision", "include",
    "id", "type", "name", "title", "abbreviation", "label", "date", "detail", "summary", "explanation",
    "category", "group", "dataset", "provider", "systemImage",
    "officialURL", "serviceURL", "endpoint", "urlTemplate", "url",
    "attribution", "license", "coverage", "rects", "extent",
    "source", "render", "minimumZoom", "maximumZoom", "defaultOpacity", "time",
    "range", "cadence", "discovery", "preloadAllowance", "editions", "members",
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
        for listed in LISTS:
            result[listed].extend(part.get(listed, []))
    for table in TABLES:
        result[table] = dict(sorted(result[table].items()))
    return result


def command_format():
    for path in sorted(SRC.rglob("*.json")):
        text = dumps(load(path))
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"formatted {path.relative_to(ROOT)}")


def command_assemble():
    OUTPUT.write_text(dumps(assemble()), encoding="utf-8")
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
    if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != dumps(assemble()):
        problems.append(f"{OUTPUT.relative_to(ROOT)} is out of date; run tools/catalog.py assemble")
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        raise SystemExit(1)
    print("catalog sources are formatted and assembled")


if __name__ == "__main__":
    commands = {"format": command_format, "assemble": command_assemble, "verify": command_verify}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        raise SystemExit(__doc__)
    commands[sys.argv[1]]()
