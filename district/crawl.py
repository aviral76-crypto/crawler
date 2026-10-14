#!/usr/bin/env python3
"""
Crawl District's shopping catalogue for Delhi NCR and emit apparel-brand
footprint data, locality by locality.

Pipeline
--------
1. hubs   -- sweep a lat/lng grid over Delhi NCR, collecting every mall /
             market / high street from the shopping feed's hub_rail.
2. detail -- for each hub, fetch its page for a clean address, coordinates and
             reported store count, then resolve that point to a canonical
             locality (District's own display_title + Zomato subzone id).
3. stores -- page through each hub's full store directory and keep the ones
             tagged as apparel.
4. enrich -- resolve every store slug to its own detail page, which states the
             store's locality, street address and exact coordinates.

Everything is cached on disk, so re-running only fetches what is missing.

    python3 crawl.py hubs      # grid-sweep -> data/hubs.csv
    python3 crawl.py stores    # per-hub directories -> data/stores_<filter>.csv
    python3 crawl.py enrich    # per-store locality -> data/stores_<filter>_enriched.csv
    python3 crawl.py all       # all three, in order

Widen the category filter with --categories fashion (adds footwear, bags,
jewellery, watches, eyewear, accessories) or --categories all.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

from district_api import (
    APPAREL_TAGS,
    FASHION_TAGS,
    DistrictClient,
    DistrictError,
    Location,
    parse_count,
    parse_floor,
    parse_latlng,
    gender_label,
    parse_store_details,
    parse_subtitle,
    store_matches,
)

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

# Delhi NCR bounding box, plus a denser pass over Gurgaon. The hub rail returns
# only the ~7-14 nearest hubs per call, so coverage comes from grid density.
NCR_BOX = (28.30, 28.92, 76.80, 77.60)
NCR_STEP = 0.045  # ~5 km
GURGAON_BOX = (28.36, 28.56, 76.92, 77.16)
GURGAON_STEP = 0.025  # ~2.8 km


def grid(box: tuple[float, float, float, float], step: float) -> list[tuple[float, float]]:
    lat0, lat1, lng0, lng1 = box
    pts = []
    lat = lat0
    while lat <= lat1 + 1e-9:
        lng = lng0
        while lng <= lng1 + 1e-9:
            pts.append((round(lat, 4), round(lng, 4)))
            lng += step
        lat += step
    return pts


def sweep_points() -> list[tuple[float, float]]:
    seen: dict[tuple[float, float], None] = {}
    for pt in grid(NCR_BOX, NCR_STEP) + grid(GURGAON_BOX, GURGAON_STEP):
        seen.setdefault(pt, None)
    return list(seen)


#: Trailing tokens in a hub address that name a city/region, not a locality.
_CITY_SUFFIXES = {
    "gurgaon", "gurugram", "new delhi", "delhi", "noida", "greater noida",
    "ghaziabad", "faridabad", "south delhi", "west delhi", "east delhi",
    "north delhi", "central delhi", "delhi ncr", "haryana", "uttar pradesh",
}


def locality_from_address(address: str | None) -> str | None:
    """"Sector 68, Gurgaon" -> "Sector 68";  "Saket" -> "Saket".

    Drops trailing city/region tokens and keeps the rest, so a hub in
    "Vasant Kunj, South Delhi" keys on "Vasant Kunj".
    """
    if not address:
        return None
    parts = [p.strip() for p in address.split(",") if p.strip()]
    kept = [p for p in parts if p.lower() not in _CITY_SUFFIXES]
    # Nothing but city tokens (e.g. "New Delhi" for Connaught Place): let the
    # caller fall back to the resolved locality or the hub's own name.
    return ", ".join(kept) if kept else None


def load_json(path: Path, default: Any) -> Any:
    if path.exists():
        return json.loads(path.read_text())
    return default


def dump_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, ensure_ascii=False))


def write_csv(path: Path, rows: list[dict[str, Any]], columns: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"  wrote {path.relative_to(HERE)}  ({len(rows)} rows)")


# --------------------------------------------------------------------- stage 1

def crawl_hubs(client: DistrictClient, workers: int) -> dict[str, dict[str, Any]]:
    """Grid-sweep the shopping feed, then enrich each hub with its own page."""
    hubs: dict[str, dict[str, Any]] = load_json(DATA / "hubs_raw.json", {})
    points = sweep_points()
    print(f"[hubs] sweeping {len(points)} points over Delhi NCR "
          f"({len(hubs)} hubs already cached)")

    def probe(pt: tuple[float, float]) -> list[dict[str, Any]]:
        lat, lng = pt
        # A bare Location is enough for the hub rail; no need to spend a
        # pre_home call on every grid point.
        return client.nearby_hubs(Location(lat=lat, lng=lng))

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(probe, pt): pt for pt in points}
        for fut in as_completed(futures):
            pt = futures[fut]
            done += 1
            try:
                found = fut.result()
            except DistrictError as exc:
                print(f"  ! {pt}: {exc}", file=sys.stderr)
                continue
            for hub in found:
                hubs.setdefault(hub["hub_id"], {}).update(hub)
            if done % 25 == 0 or done == len(points):
                print(f"  {done}/{len(points)} points -> {len(hubs)} unique hubs")
    dump_json(DATA / "hubs_raw.json", hubs)

    # Enrich: hub page gives a clean address, coordinates and category counts.
    details: dict[str, dict[str, Any]] = load_json(DATA / "hubs_detail.json", {})
    pending = [h for h in hubs if h not in details]
    print(f"[hubs] fetching detail for {len(pending)} hubs")

    def detail(hub_id: str) -> tuple[str, dict[str, Any]]:
        anchor = Location(lat=28.4595, lng=77.0266)  # any NCR point works
        data = client.hub_details(hub_id, anchor)
        basic = ((data.get("headerData") or {}).get("basicData") or {})
        loc = basic.get("location") or {}
        lat, lng = parse_latlng(loc.get("link"))
        stats = {}
        for item in ((basic.get("hubStats") or {}).get("items") or []):
            title = (item.get("title") or "").lower()
            for unit, key in (("store", "stores"), ("restaurant", "restaurants"), ("movie", "movies")):
                if unit in title:
                    stats[key] = parse_count(item.get("title"), unit)
        resolved = None
        if lat and lng:
            try:
                resolved = client.resolve_location(lat, lng)
            except DistrictError:
                resolved = None
        address = loc.get("addressText")
        return hub_id, {
            "hub_id": hub_id,
            "hub_name": basic.get("name"),
            "address_text": address,
            "lat": lat,
            "lng": lng,
            "stores_reported": stats.get("stores"),
            "restaurants_reported": stats.get("restaurants"),
            # The hub page's own addressText is the cleanest locality signal
            # ("DLF Phase 3, Gurgaon", "Sector 68, Gurgaon", "Saket"). pre_home's
            # display_title snaps to the nearest POI, which is often a different
            # mall, so keep it only as a fallback / cross-check.
            "locality": locality_from_address(address)
            or locality_from_address(resolved.locality if resolved else None)
            or basic.get("name"),
            "locality_resolved": resolved.locality if resolved else None,
            "locality_detail": resolved.locality_detail if resolved else None,
            "subzone_id": resolved.subzone_id if resolved else None,
            "p_city_name": resolved.p_city_name if resolved else None,
            "zone_id": resolved.zone_id if resolved else None,
        }

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(detail, h) for h in pending]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                hub_id, info = fut.result()
            except DistrictError as exc:
                print(f"  ! {exc}", file=sys.stderr)
                continue
            details[hub_id] = info
            if i % 10 == 0 or i == len(pending):
                print(f"  {i}/{len(pending)} hub pages")
    dump_json(DATA / "hubs_detail.json", details)

    merged: dict[str, dict[str, Any]] = {}
    for hub_id, rail in hubs.items():
        row = dict(rail)
        row.update(details.get(hub_id, {}))
        # The rail subtitle ("12.3 km | 148 stores") is a fallback count.
        row.setdefault("stores_reported", None)
        if row.get("stores_reported") is None:
            row["stores_reported"] = parse_count(rail.get("rail_subtitle"), "store")
        row["hub_name"] = row.get("hub_name") or rail.get("hub_name")
        merged[hub_id] = row

    write_csv(
        DATA / "hubs.csv",
        sorted(merged.values(), key=lambda r: (r.get("p_city_name") or "", r.get("locality") or "", r.get("hub_name") or "")),
        ["hub_id", "hub_name", "hub_type", "locality", "p_city_name", "subzone_id",
         "zone_id", "address_text", "locality_resolved", "locality_detail",
         "lat", "lng", "stores_reported", "restaurants_reported"],
    )
    dump_json(DATA / "hubs.json", merged)
    return merged


# --------------------------------------------------------------------- stage 2

def crawl_stores(client: DistrictClient, allowed: set[str] | None, workers: int,
                 label: str = "apparel") -> list[dict[str, Any]]:
    hubs: dict[str, dict[str, Any]] = load_json(DATA / "hubs.json", {})
    if not hubs:
        sys.exit("No hubs cached -- run `python3 crawl.py hubs` first.")

    cache: dict[str, list[dict[str, Any]]] = load_json(DATA / "hub_stores_raw.json", {})
    pending = [h for h in hubs if h not in cache]
    print(f"[stores] {len(hubs)} hubs, {len(pending)} still to crawl")

    def fetch(hub_id: str) -> tuple[str, list[dict[str, Any]]]:
        anchor = Location(lat=28.4595, lng=77.0266)
        return hub_id, list(client.hub_stores(hub_id, anchor))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, h) for h in pending]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                hub_id, items = fut.result()
            except DistrictError as exc:
                print(f"  ! {exc}", file=sys.stderr)
                continue
            cache[hub_id] = items
            name = (hubs.get(hub_id) or {}).get("hub_name") or hub_id
            print(f"  [{i}/{len(pending)}] {name}: {len(items)} stores")
            dump_json(DATA / "hub_stores_raw.json", cache)  # checkpoint
    dump_json(DATA / "hub_stores_raw.json", cache)

    rows: list[dict[str, Any]] = []
    for hub_id, items in cache.items():
        hub = hubs.get(hub_id, {})
        for item in items:
            vertical, tags = parse_subtitle(item.get("subTitle"))
            if not store_matches(item, allowed):
                continue
            rows.append(
                {
                    "store_id": item.get("id"),
                    "brand": item.get("title"),
                    "vertical": vertical,
                    "categories": " | ".join(tags),
                    "floor": parse_floor(item.get("metaText")),
                    "offer": item.get("offerText"),
                    "hub_id": hub_id,
                    "hub_name": hub.get("hub_name"),
                    "hub_type": hub.get("hub_type"),
                    "locality": hub.get("locality"),
                    "locality_detail": hub.get("locality_detail"),
                    "subzone_id": hub.get("subzone_id"),
                    "city": hub.get("p_city_name"),
                    "address_text": hub.get("address_text"),
                    "lat": hub.get("lat"),
                    "lng": hub.get("lng"),
                    "url": item.get("link"),
                }
            )

    total = sum(len(v) for v in cache.values())
    print(f"[stores] {total} stores crawled, {len(rows)} match the category filter")
    rows.sort(key=lambda r: ((r.get("city") or ""), (r.get("locality") or ""), (r.get("brand") or "")))
    write_csv(
        DATA / f"stores_{label}.csv",
        rows,
        ["store_id", "brand", "vertical", "categories", "floor", "offer",
         "hub_id", "hub_name", "hub_type", "locality", "locality_detail",
         "subzone_id", "city", "address_text", "lat", "lng", "url"],
    )
    dump_json(DATA / f"stores_{label}.json", rows)
    return rows


# --------------------------------------------------------------------- stage 3

SSR_PAGE = "https://www.district.in/stores/"
SLUG_RE = re.compile(r"[A-Za-z0-9\-]+-SDP-\d+")

#: District publishes every store detail page in a gzipped sitemap -- 25k stores
#: nationwide. This is the only complete enumeration of standalone high-street
#: stores, which no hub lists and no discovery endpoint will page through.
SITEMAP_INDEX = "https://cdn.district.in/sitemap/sitemap-stores.xml"

#: City tokens that appear at the end of a store slug, e.g.
#: "snitch-sector-29-gurgaon". Used to scope the national sitemap to a metro.
#: Scoping only decides which slugs are worth fetching -- a store's real city
#: comes from `seo_city` on its detail page -- so being generous is cheap.
CITY_SLUG_TOKENS: dict[str, tuple[str, ...]] = {
    "ncr": ("gurgaon", "gurugram", "new-delhi", "delhi", "delhi-ncr", "ncr",
            "noida", "greater-noida", "ghaziabad", "faridabad"),
    "mumbai": ("mumbai", "navi-mumbai", "thane"),
    "pune": ("pune", "pimpri", "chinchwad"),
    "hyderabad": ("hyderabad", "secunderabad"),
    "bangalore": ("bangalore", "bengaluru"),
}

#: District appends the zone to some slugs ("...-majiwada-thane-west"), so one
#: trailing direction segment is stripped before the city token is matched.
_DIRECTION_SUFFIXES = ("-west", "-east", "-north", "-south", "-central")


def fetch_url(url: str, timeout: float = 90.0) -> bytes:
    req = urllib.request.Request(url, headers={"user-agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if url.endswith(".gz") or raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def slug_city_token(slug: str, tokens: Iterable[str]) -> str | None:
    """City token of a slug: "snitch-sector-29-gurgaon" -> "gurgaon"."""
    body = slug.rsplit("-SDP-", 1)[0]
    for suffix in _DIRECTION_SUFFIXES:
        if body.endswith(suffix):
            body = body[: -len(suffix)]
            break
    ordered = sorted(tokens, key=len, reverse=True)
    for token in ordered:
        if body.endswith("-" + token):
            return token
    # A few slugs put the city before the locality ("...-hyderabad-kukatpally").
    for token in ordered:
        if f"-{token}-" in body:
            return token
    return None


def crawl_sitemap(cities: list[str] | None = None) -> dict[str, str]:
    """Pull every store slug District publishes, scoped to the named metros.

    Merges into whatever is already cached, so scoping to a new city never
    discards the slugs an earlier run collected. `cities=None` keeps the lot.
    """
    index = fetch_url(SITEMAP_INDEX).decode(errors="replace")
    children = re.findall(r"<loc>(.*?)</loc>", index)
    print(f"[sitemap] {len(children)} child sitemap(s)")

    wanted: dict[str, str] = {}  # token -> metro
    for metro in cities or []:
        for token in CITY_SLUG_TOKENS[metro]:
            wanted[token] = metro

    slugs: dict[str, str] = load_json(DATA / "sitemap_slugs.json", {})
    before = len(slugs)
    for child in children:
        body = fetch_url(child).decode(errors="replace")
        for url in re.findall(r"<loc>(.*?)</loc>", body):
            if "/stores/" not in url:
                continue
            slug = url.rstrip("/").rsplit("/stores/", 1)[-1]
            if not SLUG_RE.fullmatch(slug):
                continue
            if cities is None:
                slugs[slug] = slug_city_token(slug, wanted) or "other"
                continue
            token = slug_city_token(slug, wanted)
            if token:
                slugs[slug] = token

    by_city = Counter(slugs.values())
    print(f"[sitemap] {len(slugs)} slugs (+{len(slugs) - before} new) "
          f"({dict(by_city.most_common())})")
    dump_json(DATA / "sitemap_slugs.json", slugs)
    return slugs


def collect_slugs() -> dict[str, str]:
    """Every store slug we know about -> where it came from.

    Three sources, in order of volume: the hub directories we crawled, the
    public /stores/ page (which surfaces standalone high-street stores that no
    hub lists), and an optional seeds.txt for slugs captured from the app.
    """
    slugs: dict[str, str] = {}

    for items in load_json(DATA / "hub_stores_raw.json", {}).values():
        for item in items:
            found = SLUG_RE.search(item.get("link") or "")
            if found:
                slugs.setdefault(found.group(0), "hub")

    try:
        with urllib.request.urlopen(
            urllib.request.Request(SSR_PAGE, headers={"user-agent": "Mozilla/5.0"}), timeout=45
        ) as resp:
            for slug in SLUG_RE.findall(resp.read().decode(errors="replace")):
                slugs.setdefault(slug, "web_page")
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"  ! could not read {SSR_PAGE}: {exc}", file=sys.stderr)

    for slug in load_json(DATA / "sitemap_slugs.json", {}):
        slugs.setdefault(slug, "sitemap")

    seeds = HERE / "seeds.txt"
    if seeds.exists():
        for line in seeds.read_text().splitlines():
            for slug in SLUG_RE.findall(line):
                slugs.setdefault(slug, "seeds")

    return slugs


def enrich_stores(client: DistrictClient, allowed: set[str] | None, workers: int,
                  label: str = "apparel") -> list[dict[str, Any]]:
    """Resolve every known slug to a store-level locality via its detail page."""
    slugs = collect_slugs()
    by_source = Counter(slugs.values())
    print(f"[enrich] {len(slugs)} slugs ({dict(by_source)})")

    cache: dict[str, dict[str, Any]] = load_json(DATA / "store_details.json", {})
    pending = [s for s in slugs if s not in cache]
    print(f"[enrich] {len(pending)} to fetch")

    def fetch(slug: str) -> tuple[str, dict[str, Any] | None]:
        try:
            return slug, parse_store_details(slug, client.store_details(slug))
        except DistrictError as exc:
            print(f"  ! {slug}: {exc}", file=sys.stderr)
            return slug, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, s) for s in pending]
        for i, fut in enumerate(as_completed(futures), 1):
            slug, row = fut.result()
            if row:
                cache[slug] = row
            if i % 50 == 0 or i == len(pending):
                print(f"  {i}/{len(pending)} store pages")
                dump_json(DATA / "store_details.json", cache)
    dump_json(DATA / "store_details.json", cache)

    # Hub context, keyed by slug, so each store keeps its mall when it has one.
    hubs = load_json(DATA / "hubs.json", {})
    hub_by_slug: dict[str, dict[str, Any]] = {}
    for hub_id, items in load_json(DATA / "hub_stores_raw.json", {}).items():
        hub = hubs.get(hub_id, {})
        for item in items:
            found = SLUG_RE.search(item.get("link") or "")
            if found:
                hub_by_slug[found.group(0)] = {
                    "store_id": item.get("id"),
                    "hub_id": hub_id,
                    "hub_name": hub.get("hub_name"),
                    "hub_type": hub.get("hub_type"),
                    "floor": parse_floor(item.get("metaText")),
                    "offer": item.get("offerText"),
                    "subTitle": item.get("subTitle"),
                    "metaText": item.get("metaText"),
                    "hub_locality": hub.get("locality"),
                    "hub_city": hub.get("p_city_name"),
                    "subzone_id": hub.get("subzone_id"),
                }

    rows = []
    for slug, store in cache.items():
        hub = hub_by_slug.get(slug, {})
        # Category tags come from the detail page; fall back to the hub listing.
        probe = {"subTitle": f"{store.get('vertical') or ''} | {store.get('categories') or ''}",
                 "metaText": hub.get("metaText") or ""}
        if not store_matches(probe, allowed) and not store_matches(hub, allowed):
            continue
        # For a store inside a mall, seo_locality reads "Ambience Mall, DLF Phase 3"
        # -- the hub name plus the locality. The hub's own addressText already
        # gives the bare locality, so prefer that and keep seo_locality raw.
        rows.append(
            {
                "store_id": hub.get("store_id"),
                "brand": store.get("brand"),
                "vertical": store.get("vertical"),
                "categories": store.get("categories"),
                "locality": hub.get("hub_locality") or store.get("locality"),
                "locality_raw": store.get("locality"),
                "city": hub.get("hub_city") or store.get("city"),
                "subzone_id": hub.get("subzone_id"),
                "hub_id": hub.get("hub_id"),
                "hub_name": hub.get("hub_name"),
                "hub_type": hub.get("hub_type"),
                "standalone": "" if hub.get("hub_id") else "yes",
                "floor": hub.get("floor"),
                "offer": hub.get("offer"),
                "street_address": store.get("street_address"),
                "postal_code": store.get("postal_code"),
                "lat": store.get("lat"),
                "lng": store.get("lng"),
                "slug": slug,
                "url": store.get("url"),
            }
        )
    rows.sort(key=lambda r: ((r.get("city") or ""), (r.get("locality") or ""), (r.get("brand") or "")))
    print(f"[enrich] {len(cache)} stores resolved, {len(rows)} match the category filter")
    write_csv(
        DATA / f"stores_{label}_enriched.csv", rows,
        ["store_id", "brand", "vertical", "categories", "locality", "city",
         "subzone_id", "hub_id", "hub_name", "hub_type", "standalone", "floor",
         "offer", "locality_raw", "street_address", "postal_code",
         "lat", "lng", "slug", "url"],
    )
    dump_json(DATA / f"stores_{label}_enriched.json", rows)
    return rows


# --------------------------------------------------------------------- stage 5

#: Approximate centres for a coarse hub sweep outside NCR. District's gender
#: facet is a brand attribute (verified: 99/99 brands seen in more than one hub
#: carry an identical tag), so a brand observed in any mall anywhere yields its
#: real tag -- which is why this reaches beyond the city under analysis.
OTHER_CITY_BOXES = {
    "Bangalore": (12.83, 13.12, 77.46, 77.78),
    "Mumbai": (18.90, 19.33, 72.78, 73.02),
    "Hyderabad": (17.26, 17.54, 78.30, 78.60),
    "Chennai": (12.88, 13.17, 80.14, 80.30),
    "Pune": (18.42, 18.64, 73.74, 73.98),
    "Kolkata": (22.46, 22.68, 88.26, 88.46),
    "Ahmedabad": (22.96, 23.12, 72.46, 72.68),
    "Jaipur": (26.78, 26.98, 75.70, 75.90),
    "Lucknow": (26.76, 26.92, 80.86, 81.04),
    "Chandigarh": (30.68, 30.80, 76.72, 76.86),
    "Kochi": (9.88, 10.06, 76.26, 76.38),
    "Indore": (22.66, 22.80, 75.80, 75.94),
    "Coimbatore": (10.94, 11.08, 76.90, 77.04),
    "Nagpur": (21.06, 21.20, 78.98, 79.12),
    "Surat": (21.12, 21.26, 72.76, 72.90),
}
OTHER_CITY_STEP = 0.045


def crawl_gender(client: DistrictClient, workers: int, max_hubs: int) -> dict[str, dict[str, Any]]:
    """Resolve each brand's audience (Men / Women / Unisex / Kids).

    District exposes gender only as a facet on the hub store directory, so the
    tag is harvested by listing each hub unfiltered and then once per gender,
    and set-differencing. NCR hubs alone cover ~75% of NCR fashion brands, so
    hubs in other cities are swept too until the brand list stops growing.
    """
    hubs: dict[str, dict[str, Any]] = load_json(DATA / "hubs_raw.json", {})
    discovered: dict[str, dict[str, Any]] = load_json(DATA / "hubs_other_cities.json", {})

    if not discovered:
        points: list[tuple[str, float, float]] = []
        for city, box in OTHER_CITY_BOXES.items():
            for lat, lng in grid(box, OTHER_CITY_STEP):
                points.append((city, lat, lng))
        print(f"[gender] discovering hubs outside NCR: {len(points)} points")

        def probe(entry: tuple[str, float, float]) -> list[dict[str, Any]]:
            city, lat, lng = entry
            try:
                loc = client.resolve_location(lat, lng)
            except DistrictError:
                return []
            found = client.nearby_hubs(loc)
            for hub in found:
                hub["city"] = loc.p_city_name or city
                hub["city_id"] = loc.city_id
                hub["lat"], hub["lng"] = lat, lng
            return found

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(probe, pt) for pt in points]
            for fut in as_completed(futures):
                done += 1
                for hub in fut.result():
                    discovered.setdefault(hub["hub_id"], {}).update(hub)
                if done % 50 == 0 or done == len(points):
                    print(f"  {done}/{len(points)} points -> {len(discovered)} hubs")
        dump_json(DATA / "hubs_other_cities.json", discovered)

    # NCR hubs first (city under analysis), then the rest, biggest malls first.
    def store_count(hub: dict[str, Any]) -> int:
        return parse_count(hub.get("rail_subtitle"), "store") or 0

    order = [(h, {**v, "city_id": 1}) for h, v in hubs.items()]
    order += [(h, v) for h, v in discovered.items() if h not in hubs]
    order.sort(key=lambda kv: (kv[0] not in hubs, -store_count(kv[1])))
    if max_hubs:
        order = order[:max_hubs]
    print(f"[gender] harvesting gender from {len(order)} hubs")

    cache: dict[str, dict[str, Any]] = load_json(DATA / "hub_gender_raw.json", {})
    pending = [(h, v) for h, v in order if h not in cache]
    print(f"[gender] {len(pending)} hubs still to query ({len(cache)} cached)")

    def harvest(entry: tuple[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        hub_id, meta = entry
        loc = Location(lat=meta.get("lat") or 28.4595, lng=meta.get("lng") or 77.0266,
                       city_id=int(meta.get("city_id") or 1))
        everything = client.hub_store_ids(hub_id, loc)
        per_gender = {
            gender: set(client.hub_store_ids(hub_id, loc, gender))
            for gender in DistrictClient.GENDERS
        }
        return hub_id, {
            "brands": everything,
            "men": sorted(per_gender["Men"]),
            "women": sorted(per_gender["Women"]),
            "kids": sorted(per_gender["Kids"]),
        }

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(harvest, e) for e in pending]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                hub_id, payload = fut.result()
            except DistrictError as exc:
                print(f"  ! {exc}", file=sys.stderr)
                continue
            cache[hub_id] = payload
            if i % 10 == 0 or i == len(pending):
                print(f"  {i}/{len(pending)} hubs  ({sum(len(v['brands']) for v in cache.values())} store rows)")
                dump_json(DATA / "hub_gender_raw.json", cache)
    dump_json(DATA / "hub_gender_raw.json", cache)

    # Roll store-level observations up to the brand.
    flags: dict[str, dict[str, Any]] = {}
    for payload in cache.values():
        brands = payload["brands"]
        sets = {g: set(payload[g]) for g in ("men", "women", "kids")}
        for store_id, brand in brands.items():
            key = (brand or "").strip().lower()
            if not key:
                continue
            slot = flags.setdefault(
                key, {"brand": brand.strip(), "men": False, "women": False,
                      "kids": False, "stores_seen": 0}
            )
            slot["stores_seen"] += 1
            for gender in ("men", "women", "kids"):
                if store_id in sets[gender]:
                    slot[gender] = True
    for slot in flags.values():
        slot["gender"] = gender_label(slot["men"], slot["women"], slot["kids"])

    dump_json(DATA / "brand_gender.json", flags)
    counts = Counter(v["gender"] for v in flags.values())
    print(f"[gender] {len(flags)} brands tagged: {dict(counts.most_common())}")
    write_csv(
        DATA / "brand_gender.csv",
        sorted(flags.values(), key=lambda r: (r["gender"], r["brand"])),
        ["brand", "gender", "men", "women", "kids", "stores_seen"],
    )
    return flags


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["hubs", "sitemap", "stores", "enrich", "gender", "all"])
    ap.add_argument("--categories", choices=["apparel", "fashion", "all"], default="apparel",
                    help="which store categories to keep (default: apparel)")
    ap.add_argument("--qps", type=float, default=5.0, help="requests per second ceiling")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-hubs", type=int, default=0,
                    help="cap how many hubs the gender stage queries (0 = all)")
    ap.add_argument("--cities", default="ncr",
                    help="comma-separated metros to scope the sitemap to "
                         f"({', '.join(CITY_SLUG_TOKENS)})")
    ap.add_argument("--nationwide", action="store_true",
                    help="keep every city in the sitemap, not just --cities")
    args = ap.parse_args()

    cities = None if args.nationwide else [c.strip() for c in args.cities.split(",") if c.strip()]
    unknown = [c for c in (cities or []) if c not in CITY_SLUG_TOKENS]
    if unknown:
        ap.error(f"unknown city {unknown!r}; known: {', '.join(CITY_SLUG_TOKENS)}")

    # None == keep every category.
    allowed = {"apparel": APPAREL_TAGS, "fashion": FASHION_TAGS, "all": None}[args.categories]
    client = DistrictClient(qps=args.qps)

    if args.stage in ("hubs", "all"):
        crawl_hubs(client, args.workers)
    if args.stage in ("sitemap", "all"):
        crawl_sitemap(cities)
    if args.stage in ("stores", "all"):
        crawl_stores(client, allowed, args.workers, args.categories)
    if args.stage in ("enrich", "all"):
        enrich_stores(client, allowed, args.workers, args.categories)
    if args.stage in ("gender", "all"):
        crawl_gender(client, args.workers, args.max_hubs)


if __name__ == "__main__":
    main()
