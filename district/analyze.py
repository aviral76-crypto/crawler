#!/usr/bin/env python3
"""
Turn the crawl output into locality-density views.

    python3 analyze.py                      # stores_apparel_enriched.csv -> reports + summary
    python3 analyze.py --min-stores 3       # only localities with >=3 apparel stores

Outputs (in data/):
    locality_density.csv   one row per locality: store count, brand count, hubs
    brand_footprint.csv    one row per brand: how many localities it is in, and which
    brand_x_locality.csv   matrix of brand (rows) x locality (columns)
    hub_density.csv        one row per hub, for mall-level comparison
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from district_api import norm_city, norm_locality, resolve_subcity

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"


def read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise SystemExit(f"{path} not found -- run `python3 crawl.py all` first.")
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"  wrote {path.relative_to(HERE)}  ({len(rows)} rows)")


def canonical_localities(hubs: list[dict[str, Any]]) -> dict[str, str]:
    """Map subzone_id -> a single label, so "NSP" and "Netaji Subhash Place"
    (both Zomato subzone 73034) collapse into one locality."""
    by_zone: dict[str, Counter] = defaultdict(Counter)
    for h in hubs:
        zone, label = h.get("subzone_id"), h.get("locality")
        if zone and label:
            # Prefer the longer, spelled-out label over an abbreviation.
            by_zone[zone][label] += len(label)
    return {z: c.most_common(1)[0][0] for z, c in by_zone.items()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stores", default=str(DATA / "stores_apparel_enriched.csv"),
                    help="crawl output to analyse (default: the enriched, store-level file)")
    ap.add_argument("--min-stores", type=int, default=1,
                    help="hide localities below this apparel-store count in the summary")
    ap.add_argument("--top", type=int, default=20, help="rows to print in the console summary")
    args = ap.parse_args()

    stores = read_csv(Path(args.stores))
    hubs = read_csv(DATA / "hubs.csv")
    canon = canonical_localities(hubs)

    # Attribute "Delhi NCR"-labelled stores to their actual sub-city where the
    # locality is unambiguous, so one locality is not split in two.
    subcity = resolve_subcity(stores)
    for row in stores:
        if id(row) in subcity:
            row["city"] = subcity[id(row)].title()

    def locality_of(row: dict[str, Any]) -> str:
        # The shared normalizer strips mall prefixes and city suffixes, which
        # keeps one locality from splitting across several labels.
        return norm_locality(row.get("locality")) or "unknown"

    # ------------------------------------------------------------- aggregation
    per_locality: dict[tuple[str, str], dict[str, Any]] = {}
    brand_localities: dict[str, set[tuple[str, str]]] = defaultdict(set)
    brand_counts: Counter = Counter()
    matrix: dict[str, Counter] = defaultdict(Counter)

    for row in stores:
        key = ((norm_city(row.get("city")) or "unknown").title(), locality_of(row))
        slot = per_locality.setdefault(
            key, {"city": key[0], "locality": key[1], "stores": 0, "brands": set(), "hubs": set()}
        )
        slot["stores"] += 1
        brand = row.get("brand") or "unknown"
        slot["brands"].add(brand)
        if row.get("hub_name"):
            slot["hubs"].add(row["hub_name"])
        brand_localities[brand].add(key)
        brand_counts[brand] += 1
        matrix[brand][key[1]] += 1

    # All-category baseline per locality, so apparel share sits next to the
    # absolute count. Taken from the full store_details cache when available
    # (every store we resolved, not just apparel); otherwise from the hubs'
    # own reported counts.
    total_by_locality: Counter = Counter()
    hubs_by_locality: dict[str, set[str]] = defaultdict(set)
    hub_locality = {h["hub_id"]: norm_locality(h.get("locality")) for h in hubs}

    all_stores_path = DATA / "store_details.json"
    if all_stores_path.exists():
        import json

        details = json.loads(all_stores_path.read_text())
        hub_of_slug: dict[str, str] = {}
        raw = DATA / "hub_stores_raw.json"
        if raw.exists():
            import re

            slug_re = re.compile(r"[A-Za-z0-9\-]+-SDP-\d+")
            for hub_id, items in json.loads(raw.read_text()).items():
                for item in items:
                    found = slug_re.search(item.get("link") or "")
                    if found:
                        hub_of_slug[found.group(0)] = hub_id
        for slug, store in details.items():
            label = hub_locality.get(hub_of_slug.get(slug, "")) or norm_locality(store.get("locality"))
            if label:
                total_by_locality[label] += 1
    else:
        for h in hubs:
            label = hub_locality.get(h["hub_id"]) or "unknown"
            try:
                total_by_locality[label] += int(h.get("stores_reported") or 0)
            except ValueError:
                pass

    for h in hubs:
        label = hub_locality.get(h["hub_id"])
        if label and h.get("hub_name"):
            hubs_by_locality[label].add(h["hub_name"])

    locality_rows = []
    for (city, locality), slot in per_locality.items():
        total = total_by_locality.get(locality) or 0
        locality_rows.append(
            {
                "city": city,
                "locality": locality,
                "apparel_stores": slot["stores"],
                "distinct_brands": len(slot["brands"]),
                "all_category_stores": total or "",
                "apparel_share": f"{slot['stores'] / total:.0%}" if total else "",
                "hubs": len(hubs_by_locality.get(locality) or slot["hubs"]),
                "hub_names": "; ".join(sorted(hubs_by_locality.get(locality) or slot["hubs"])),
                "top_brands": "; ".join(sorted(slot["brands"])[:12]),
            }
        )
    locality_rows.sort(key=lambda r: -r["apparel_stores"])

    brand_rows = [
        {
            "brand": brand,
            "stores": brand_counts[brand],
            "localities": len(keys),
            "cities": len({c for c, _ in keys}),
            "locality_list": "; ".join(sorted(loc for _, loc in keys)),
        }
        for brand, keys in brand_localities.items()
    ]
    brand_rows.sort(key=lambda r: (-r["stores"], -r["localities"], r["brand"]))

    # ----------------------------------------------------------------- outputs
    print("\nReports:")
    write_csv(
        DATA / "locality_density.csv", locality_rows,
        ["city", "locality", "apparel_stores", "distinct_brands", "all_category_stores",
         "apparel_share", "hubs", "hub_names", "top_brands"],
    )
    write_csv(
        DATA / "brand_footprint.csv", brand_rows,
        ["brand", "stores", "localities", "cities", "locality_list"],
    )

    localities = [r["locality"] for r in locality_rows]
    with (DATA / "brand_x_locality.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["brand", "total", *localities])
        for brand, _, in [(b["brand"], None) for b in brand_rows]:
            counts = matrix[brand]
            w.writerow([brand, sum(counts.values()), *(counts.get(loc, "") for loc in localities)])
    print(f"  wrote data/brand_x_locality.csv  ({len(brand_rows)} brands x {len(localities)} localities)")

    hub_rows = []
    apparel_by_hub = Counter(r.get("hub_id") for r in stores)
    brands_by_hub: dict[str, set[str]] = defaultdict(set)
    for r in stores:
        brands_by_hub[r.get("hub_id") or ""].add(r.get("brand") or "")
    for h in hubs:
        hid = h["hub_id"]
        hub_rows.append(
            {
                "hub_id": hid,
                "hub_name": h.get("hub_name"),
                "hub_type": h.get("hub_type"),
                "locality": norm_locality(h.get("locality")),
                "city": h.get("p_city_name"),
                "apparel_stores": apparel_by_hub.get(hid, 0),
                "distinct_apparel_brands": len(brands_by_hub.get(hid, ())),
                "all_category_stores": h.get("stores_reported"),
                "lat": h.get("lat"),
                "lng": h.get("lng"),
            }
        )
    hub_rows.sort(key=lambda r: -r["apparel_stores"])
    write_csv(
        DATA / "hub_density.csv", hub_rows,
        ["hub_id", "hub_name", "hub_type", "locality", "city", "apparel_stores",
         "distinct_apparel_brands", "all_category_stores", "lat", "lng"],
    )

    # ----------------------------------------------------------------- summary
    shown = [r for r in locality_rows if r["apparel_stores"] >= args.min_stores]
    print(f"\n{len(stores)} apparel stores · {len(brand_localities)} brands · "
          f"{len(per_locality)} localities · {len(hubs)} hubs\n")
    print(f"Densest localities (top {args.top}):")
    print(f"  {'locality':26s} {'city':14s} {'apparel':>7s} {'brands':>6s} {'all':>5s}  hubs")
    for r in shown[: args.top]:
        print(f"  {r['locality'][:26]:26s} {r['city'][:14]:14s} "
              f"{r['apparel_stores']:7d} {r['distinct_brands']:6d} "
              f"{str(r['all_category_stores']):>5s}  {r['hub_names'][:46]}")

    print(f"\nMost widespread brands (top {args.top}):")
    print(f"  {'brand':28s} {'stores':>6s} {'localities':>10s}  where")
    for r in brand_rows[: args.top]:
        print(f"  {r['brand'][:28]:28s} {r['stores']:6d} {r['localities']:10d}  {r['locality_list'][:52]}")


if __name__ == "__main__":
    main()
