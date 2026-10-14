#!/usr/bin/env python3
"""Find each brand's own store locator and, where it hands back a machine
readable store list, count that list into the six metros the workbook covers.

    python3 brand_locator_scan.py scan                  # every brand with a site
    python3 brand_locator_scan.py scan "Levi's" BIBA    # named brands only
    python3 brand_locator_scan.py report                # what the scan found

Nothing here guesses a store count. A brand's row is only written when its own
locator returned a list of outlets; everything else is recorded as a miss, so
`brand_store_check.py sheet` leaves District's number standing.

Reads data/brand_sites.json ({brand: {site}}) and writes:
    data/brand_locator_counts.json   brands whose locator gave a store list
    data/brand_locator_scan.json     every brand, with how the scan went
"""

from __future__ import annotations

import json
import re
import ssl
import sys
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
SITES = DATA / "brand_sites.json"
#: The scan writes its own file rather than the one brand_locator_fetch.py
#: owns -- both used to write brand_locator_counts.json and would clobber each
#: other's brands when run at the same time. brand_store_check.py merges them.
COUNTS = DATA / "brand_locator_scan_counts.json"
SCAN = DATA / "brand_locator_scan.json"
URLS = DATA / "brand_locator_urls.json"

from brand_locator_fetch import UA, metro_of  # noqa: E402  (shared with the fetchers)

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

#: Paths a store locator actually lives at, commonest first.
LOCATOR_PATHS = [
    "pages/store-locator", "pages/stores", "pages/store-locator-1",
    "pages/our-stores", "store-locator", "stores", "storelocator",
    "pages/find-a-store",
]

#: Field names that only a store record carries.
ADDRESSY = re.compile(r'"(address|address_line_1|addr|street|city|postal_code|'
                      r'pincode|locality)"', re.I)

STOCKIST_TAG = re.compile(r'data-stockist-widget-tag=["\']([A-Za-z0-9]{4,12})["\']')


def get(url: str, timeout: int = 10, referer: str | None = None,
        limit: int = 8_000_000) -> tuple[int, str]:
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.getcode(), r.read(limit).decode("utf-8", "replace")
    except Exception:
        return 0, ""


def brand_tokens(brand: str) -> set[str]:
    drop = {"the", "for", "and", "by", "of", "co", "india", "shop", "store"}
    return {w for w in re.split(r"[^a-z0-9]+", brand.lower())
            if len(w) > 2 and w not in drop}


def stockist_locations(tag: str, site: str) -> list[dict[str, Any]] | None:
    code, body = get(f"https://stockist.co/api/v1/{tag}/locations/all", referer=site)
    if code != 200:
        return None
    try:
        rows = json.loads(body)
    except ValueError:
        return None
    return rows if isinstance(rows, list) and rows else None


def json_locations(text: str) -> list[dict[str, Any]] | None:
    """Pull a list of store dicts out of an arbitrary JSON response."""
    try:
        blob = json.loads(text)
    except ValueError:
        return None
    stack, best = [blob], None
    while stack:
        node = stack.pop()
        if isinstance(node, list) and node and isinstance(node[0], dict):
            if ADDRESSY.search(json.dumps(node[0])) and (best is None or len(node) > len(best)):
                best = node
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node[:50])
    return best


#: A national brand's store feed has many rows across many cities. Anything
#: smaller is some other array that happened to carry an "address" key.
MIN_ROWS, MIN_CITIES = 10, 3


def plausible(rows: list[dict[str, Any]]) -> bool:
    if len(rows) < MIN_ROWS:
        return False
    cities = {str(city_of(r)).strip().lower() for r in rows}
    return len({c for c in cities if c}) >= MIN_CITIES


def city_of(row: dict[str, Any]) -> str:
    for key in ("city", "City", "store_city", "town", "locality", "state"):
        if row.get(key):
            return str(row[key])
    # No city field, so fall back to the address text; metro_of scans it.
    return " ".join(str(row.get(k) or "") for k in
                    ("address", "address_line_2", "address_line_1", "full_address"))


def name_of(row: dict[str, Any]) -> str:
    for key in ("name", "store_name", "title", "storeName"):
        if row.get(key):
            return str(row[key])
    return ""


def scan_brand(item: tuple[str, str, list[str]]) -> dict[str, Any]:
    brand, site, known = item
    out: dict[str, Any] = {"brand": brand, "site": site, "locator": None,
                           "source": None, "n": None, "why": None}
    base = f"{urlparse(site).scheme}://{urlparse(site).netloc}/"

    # URLs the brand's own sitemap advertises first; guessed paths only after.
    pages: list[tuple[str, str]] = []
    for path in list(known) + LOCATOR_PATHS:
        url = urljoin(base, path)
        code, html = get(url)
        if code == 200 and len(html) > 1500 and "store" in html.lower():
            pages.append((url, html))
            if len(pages) == 2:
                break
    if not pages:
        out["why"] = "no locator page found"
        return out
    out["locator"] = pages[0][0]

    # 1. Stockist -- one request returns every outlet, with a clean city field.
    for url, html in pages:
        m = STOCKIST_TAG.search(html)
        if not m:
            continue
        rows = stockist_locations(m.group(1), site)
        if rows:
            out.update(locator=url, source=f"stockist:{m.group(1)}",
                       rows=rows, n=len(rows))
            return out

    # 2. Any JSON the locator page points at that carries store records.
    for url, html in pages:
        for ref in dict.fromkeys(re.findall(
                r'["\'](https?://[^"\'\s]{12,200}|/[^"\'\s]{4,160})["\']', html)):
            if not re.search(r"(store|outlet|locat)", ref, re.I):
                continue
            if re.search(r"\.(css|png|jpe?g|gif|svg|woff2?|ico)(\?|$)", ref, re.I):
                continue
            code, body = get(urljoin(base, ref), referer=url)
            if code != 200 or len(ADDRESSY.findall(body[:200_000])) < 3:
                continue
            rows = json_locations(body)
            if rows and plausible(rows):
                out.update(locator=url, source=urljoin(base, ref),
                           rows=rows, n=len(rows))
                return out

    out["why"] = "locator page found but no store list behind it"
    return out


def siblings_of(brand: str, all_brands: list[str]) -> list[set[str]]:
    """Workbook brands that are this one plus more words.

    "U.S. Polo Assn. Kids" is served off the same feed as "U.S Polo Assn.", so
    without this the kids stores would be counted under both.
    """
    want = brand_tokens(brand)
    return [t for b in all_brands
            if (t := brand_tokens(b)) > want and want]


def tally(brand: str, rows: list[dict[str, Any]],
          all_brands: list[str] | None = None) -> tuple[Counter, int, str]:
    """Count a store list into metros, keeping only this brand's own outlets
    when the feed is shared (one Arvind widget serves several brands)."""
    want = brand_tokens(brand)
    sibs = siblings_of(brand, all_brands or [])

    def mine(row: dict[str, Any]) -> bool:
        got = brand_tokens(name_of(row))
        # Every word of the brand must appear, and no more specific sibling's.
        return bool(want <= got) and not any(s <= got for s in sibs)

    named = [r for r in rows if mine(r)]
    took_from_sibling = len(named) < len([r for r in rows if want <= brand_tokens(name_of(r))])
    note = ""
    # Filter by brand name only when the feed really is shared -- some rows
    # this brand's, many not. If almost nothing matches, the store names simply
    # do not repeat the brand (Landmark's feed is already filtered by a
    # `concept` parameter, and names read "Lifestyle1494"), and filtering would
    # cut a whole chain down to one.
    if want and rows and len(named) >= MIN_ROWS and (
            took_from_sibling or len(named) < 0.7 * len(rows)):
        rows, note = named, (f"shared feed: {len(named)} of the locator's "
                             f"{len(rows)} outlets are this brand's, counted those")
    elif want and rows and len(named) < MIN_ROWS:
        note = ("store names do not carry the brand, so every outlet this "
                "locator returned is counted")
    metros, outside = Counter(), 0
    for r in rows:
        m = metro_of(city_of(r))
        if m:
            metros[m] += 1
        else:
            outside += 1
    return metros, outside, note


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] == "report":
        log = json.loads(SCAN.read_text()) if SCAN.exists() else {}
        hit = [v for v in log.values() if v.get("n")]
        print(f"{len(log)} brands scanned, {len(hit)} returned a store list")
        for why, n in Counter(v.get("why") or "store list found"
                              for v in log.values()).most_common():
            print(f"  {n:4d}  {why}")
        return
    if args[0] != "scan":
        raise SystemExit(__doc__)

    sites = json.loads(SITES.read_text())
    district = json.loads((DATA / "brand_district_counts.json").read_text())
    wanted = args[1:] or sorted(sites, key=lambda b: -sum(district.get(b, {}).values()))
    known = json.loads(URLS.read_text()) if URLS.exists() else {}
    todo = [(b, sites[b]["site"], known.get(b, []))
            for b in wanted if sites.get(b, {}).get("site")]
    print(f"scanning {len(todo)} brands with a known site")

    log = json.loads(SCAN.read_text()) if SCAN.exists() else {}
    counts = json.loads(COUNTS.read_text()) if COUNTS.exists() else {}
    done = 0
    with ThreadPoolExecutor(12) as ex:
        futures = {ex.submit(scan_brand, item): item[0] for item in todo}
        from concurrent.futures import as_completed
        for fut in as_completed(futures):
            res = fut.result()
            brand, rows = res["brand"], res.pop("rows", None)
            log[brand] = res
            if rows:
                metros, outside, note = tally(brand, rows, list(district))
                counts[brand] = {
                    "site": res["site"], "locator": res["locator"],
                    "india_total": sum(metros.values()) + outside,
                    "metros": dict(metros),
                    "method": f"locator store list ({res['source'][:60]})",
                    "checked": date.today().isoformat(),
                    "note": "; ".join(x for x in [
                        f"{sum(metros.values()) + outside} outlets listed, "
                        f"{outside} outside the six metros", note] if x),
                }
            done += 1
            if done % 10 == 0:
                print(f"  {done}/{len(todo)} list={sum(1 for v in log.values() if v.get('n'))}",
                      flush=True)
                SCAN.write_text(json.dumps(log, indent=1, ensure_ascii=False))
                COUNTS.write_text(json.dumps(counts, indent=1, ensure_ascii=False))
    SCAN.write_text(json.dumps(log, indent=1, ensure_ascii=False))
    COUNTS.write_text(json.dumps(counts, indent=1, ensure_ascii=False))
    print(f"store list for {sum(1 for v in log.values() if v.get('n'))} brands; "
          f"{COUNTS.name} now holds {len(counts)}")


if __name__ == "__main__":
    main()
