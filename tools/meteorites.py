#!/usr/bin/env python3
"""Builds catalog/data/meteorite-landings.geojson from NASA's copy of the Meteoritical Society's list.

    python3 tools/meteorites.py [<Meteorite_Landings.json>]

NASA retired the Socrata API the list used to be served from and now keeps only the old export: one
12 MB file of column metadata and rows, behind a link that redirects to a signed, expiring address.
Neither is something the app should read, so this converts it once to plain GeoJSON, published from
this repository with GitHub Pages, where the app's `geoJSON` source loads it whole.

Records without a location, or recorded at exactly 0°, 0° (how the list writes an unknown one), are
left out. Each feature keeps the name, the classification, the mass in grams, whether the fall was
seen or the stone found later, and the year.
"""
import json
import sys
import urllib.request
from pathlib import Path

SOURCE_URL = "https://data.nasa.gov/docs/legacy/meteorite_landings/Meteorite_Landings.json"
OUTPUT = Path(__file__).resolve().parent.parent / "catalog" / "data" / "meteorite-landings.geojson"


def load(argument):
    if argument:
        return json.loads(Path(argument).read_text())
    request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "platmark-catalog-tools"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def features(export):
    columns = {column["fieldName"]: index for index, column in enumerate(export["meta"]["view"]["columns"])}
    for row in export["data"]:
        def field(name):
            return row[columns[name]]

        latitude, longitude = number(field("reclat")), number(field("reclong"))
        if latitude is None or longitude is None or (latitude == 0 and longitude == 0):
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        properties = {"name": field("name"), "class": field("recclass"), "fall": field("fall")}
        mass = number(field("mass"))
        if mass is not None:
            properties["massGrams"] = round(mass, 1) if mass % 1 else int(mass)
        year = (field("year") or "")[:4]
        if year.isdigit():
            properties["year"] = int(year)
        yield {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(longitude, 5), round(latitude, 5)]},
            "properties": properties,
        }


def main():
    export = load(sys.argv[1] if len(sys.argv) > 1 else None)
    collection = {"type": "FeatureCollection", "features": sorted(features(export), key=lambda f: f["properties"]["name"])}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(collection, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"wrote {len(collection['features'])} meteorites to {OUTPUT.relative_to(Path.cwd()) if OUTPUT.is_relative_to(Path.cwd()) else OUTPUT}")


if __name__ == "__main__":
    main()
