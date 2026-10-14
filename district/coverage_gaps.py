#!/usr/bin/env python3
"""
Audit what the District dataset is missing, two ways.

1. Against District itself -- stores its own catalogue carries but every
   category filter silently drops: ones with no category tag at all, brands it
   has flagged "- DELETED", and tags the fashion set does not list.

2. Against an outside source -- OpenStreetMap retail POIs in the Delhi NCR
   bounding box, fetched via Overpass. OSM is sparse (it knows 25 Lenskart
   outlets where District lists 159), so its count is a *floor*, never a
   measurement. A brand with an OSM floor of 10+ certainly has 10+ stores; a
   low OSM count proves nothing either way.

    python3 coverage_gaps.py                 # audit + report
    python3 coverage_gaps.py --refresh-osm   # re-query Overpass first

Writes data/coverage_gaps.csv and data/untagged_stores.csv.
"""

from __future__ import annotations

import argparse
import collections
import csv
import difflib
import glob
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from district_api import FASHION_TAGS, dedupe_stores, norm_city, norm_locality

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OSM_DIR = DATA / "osm"

#: Delhi NCR bounding box, as Overpass wants it: south,west,north,east.
NCR_BBOX = "28.30,76.80,28.92,77.60"

#: Overpass mirrors, tried in order. The query is split by shop type because
#: the whole thing in one request times out on the public endpoint.
OVERPASS_ENDPOINTS = (
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
)
SHOP_GROUPS = {
    "clothes": "clothes",
    "shoes": "shoes",
    "jewelry": "jewelry",
    "other": "bag|optician|watches|boutique|department_store|variety_store",
    "sports": "sports|outdoor|fashion|fabric|tailor|leather",
}

#: Words that carry no brand identity, stripped before matching names across
#: the two datasets.
_NOISE = re.compile(r"\b(and|&|the|india|store|stores|showroom|outlet|exclusive|factory|sportswear)\b")


def brand_key(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", _NOISE.sub(" ", (name or "").lower()))


def fetch_osm(refresh: bool = False) -> None:
    OSM_DIR.mkdir(parents=True, exist_ok=True)
    for label, pattern in SHOP_GROUPS.items():
        out = OSM_DIR / f"osm_{label}.json"
        if out.exists() and not refresh:
            continue
        query = (
            f'[out:json][timeout:120];('
            f'node["shop"~"^({pattern})$"]({NCR_BBOX});'
            f'way["shop"~"^({pattern})$"]({NCR_BBOX});'
            f');out tags center;'
        )
        for endpoint in OVERPASS_ENDPOINTS:
            try:
                request = urllib.request.Request(
                    endpoint,
                    data=urllib.parse.urlencode({"data": query}).encode(),
                    headers={"User-Agent": "district-coverage-audit/1.0"},
                )
                with urllib.request.urlopen(request, timeout=200) as resp:
                    body = resp.read()
                json.loads(body)
                out.write_bytes(body)
                print(f"  [osm] {label}: {len(body)} bytes from {endpoint.split('/')[2]}")
                break
            except Exception as exc:  # noqa: BLE001 - any failure means try the mirror
                print(f"  [osm] {label}: {endpoint.split('/')[2]} failed ({exc})", file=sys.stderr)
        else:
            print(f"  [osm] {label}: all endpoints failed", file=sys.stderr)


def load_osm() -> collections.Counter:
    """{brand_key: outlet count} from the cached Overpass responses.

    `brand` is preferred over `operator` over `name`, since `name` is often a
    branch label rather than the brand.
    """
    counts: collections.Counter = collections.Counter()
    labels: dict[str, str] = {}
    files = sorted(glob.glob(str(OSM_DIR / "osm_*.json")))
    if not files:
        raise SystemExit("No OSM data -- run with --refresh-osm first.")
    for path in files:
        if path.endswith("osm_brands.json"):
            continue
        for element in json.loads(Path(path).read_text()).get("elements", []):
            tags = element.get("tags", {})
            name = tags.get("brand") or tags.get("operator") or tags.get("name")
            if not name:
                continue
            key = brand_key(name)
            if key:
                counts[key] += 1
                labels.setdefault(key, name.strip())
    counts.labels = labels  # type: ignore[attr-defined]
    return counts


def load_district() -> tuple[list[dict[str, Any]], collections.Counter, dict[str, str]]:
    path = DATA / "store_details.json"
    if not path.exists():
        raise SystemExit("data/store_details.json not found -- run the crawl first.")
    rows = [dict(v, slug=k) for k, v in json.loads(path.read_text()).items()]
    kept, _, _ = dedupe_stores(rows)
    counts: collections.Counter = collections.Counter()
    labels: dict[str, str] = {}
    for store in kept:
        key = brand_key(store.get("brand"))
        if key:
            counts[key] += 1
            labels.setdefault(key, (store.get("brand") or "").strip())
    return kept, counts, labels


#: OSM name -> District name, where the two datasets disagree by more than
#: punctuation and neither prefix nor fuzzy matching bridges it.
BRAND_ALIASES = {
    "w": "wforwoman",            # OSM tags the brand simply "W"
    "maxfashion": "max",         # District lists the parent brand
    "cobbitaly": "cobb",
    "duke": "duke",
    "usapolo": "uspoloassn",
    "benetton": "unitedcolorsofbenetton",
    "titaneye": "titaneyeplus",
    "gocolors": "gocolors",
}


def match(key: str, district: collections.Counter) -> int:
    """District's count for an OSM brand key, tolerating naming drift."""
    if key in district:
        return district[key]
    alias = BRAND_ALIASES.get(key)
    if alias and alias in district:
        return district[alias]
    for other, count in district.items():
        if len(key) > 4 and len(other) > 4 and (other.startswith(key) or key.startswith(other)):
            return count
    close = difflib.get_close_matches(key, [k for k in district if len(k) > 4], n=1, cutoff=0.9)
    return district[close[0]] if close else 0


def tags_of(store: dict[str, Any]) -> set[str]:
    raw = (store.get("categories") or "").replace("•", "|")
    return {t.strip().lower() for t in raw.split("|") if t.strip()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--refresh-osm", action="store_true")
    ap.add_argument("--min-osm", type=int, default=5,
                    help="ignore OSM brands below this floor (default 5)")
    args = ap.parse_args()

    if args.refresh_osm:
        print("Fetching OSM:")
    fetch_osm(args.refresh_osm)

    stores, district, dlabels = load_district()
    osm = load_osm()
    olabels = osm.labels  # type: ignore[attr-defined]

    # ---------------------------------------------- 1. gaps inside District
    untagged = [s for s in stores if not (s.get("categories") or "").strip()]
    deleted = [s for s in stores if "DELETED" in (s.get("brand") or "").upper()]
    fashion = [s for s in stores if tags_of(s) & FASHION_TAGS]
    sports_only = [s for s in stores
                   if "sports & outdoors" in tags_of(s) and not (tags_of(s) & FASHION_TAGS)]

    print(f"\nDistrict NCR catalogue: {len(stores)} stores")
    print(f"  no category tag at all, so dropped by every filter : {len(untagged)}"
          f" ({len(untagged)/len(stores):.1%})")
    print(f"  brand flagged '- DELETED' by District               : {len(deleted)}")
    print(f"  tagged only 'Sports & Outdoors', outside FASHION_TAGS: {len(sports_only)}")
    print(f"  matching FASHION_TAGS                               : {len(fashion)}")

    rows = sorted(collections.Counter(
        (s.get("brand") or "").strip() for s in untagged).items(),
        key=lambda kv: -kv[1])
    with (DATA / "untagged_stores.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["brand", "ncr_stores", "note"])
        for brand, count in rows:
            w.writerow([brand, count, "DELETED by District" if "DELETED" in brand.upper() else ""])
    print(f"  wrote data/untagged_stores.csv ({len(rows)} brands)")

    # --------------------------------------------- 2. gaps against OSM
    audit = []
    for key, floor in osm.items():
        if floor < args.min_osm or not key:
            continue
        have = match(key, district)
        audit.append({
            "brand": olabels[key],
            "district_matched_as": dlabels.get(key, ""),
            "osm_floor": floor,
            "district_ncr_stores": have,
            "verdict": (
                "absent from District" if have == 0
                else "District thinner than OSM floor" if have < floor * 0.75
                else "covered"
            ),
        })
    audit.sort(key=lambda r: (r["verdict"] != "absent from District", -r["osm_floor"]))
    with (DATA / "coverage_gaps.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(audit[0]), extrasaction="ignore")
        w.writeheader()
        w.writerows(audit)
    print(f"  wrote data/coverage_gaps.csv ({len(audit)} brands with OSM floor >= {args.min_osm})")

    print("\nAbsent from District (OSM floor >= %d):" % args.min_osm)
    hits = [r for r in audit if r["verdict"] == "absent from District"]
    for r in hits:
        print(f"  {r['osm_floor']:4d}  {r['brand']}")
    if not hits:
        print("  none")

    print("\nDistrict lists the brand but far below the OSM floor:")
    for r in audit:
        if r["verdict"] == "District thinner than OSM floor":
            print(f"  OSM {r['osm_floor']:4d} vs District {r['district_ncr_stores']:4d}   {r['brand']}")


if __name__ == "__main__":
    main()
