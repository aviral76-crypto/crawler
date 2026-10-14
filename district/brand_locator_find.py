#!/usr/bin/env python3
"""Find each brand's store-locator URL from the brand's own sitemap.

    python3 brand_locator_find.py

Guessing paths misses most locators -- brands put them at /pages/store-locator,
/apps/s/<brand>/storelocators, /find-a-store, /our-stores-1 and a hundred other
spellings. Every one of those is listed in the brand's own sitemap, so the
sitemap is read instead of guessed at.

Writes data/brand_locator_urls.json: {brand: [urls]}, most store-locator-like
first, for brand_locator_scan.py and the per-brand fetchers to work from.
"""

from __future__ import annotations

import gzip
import json
import re
import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
SITES = DATA / "brand_sites.json"
OUT = DATA / "brand_locator_urls.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

#: A URL worth trying, and how good a bet it is (lower sorts first).
WANTED = (
    (re.compile(r"store[-_]?locator", re.I), 0),
    (re.compile(r"storelocators?", re.I), 0),
    (re.compile(r"find[-_]?(a[-_]?)?store", re.I), 1),
    (re.compile(r"store[-_]?finder", re.I), 1),
    (re.compile(r"our[-_]?stores", re.I), 2),
    (re.compile(r"/stores?(/|$|\?)", re.I), 3),
    (re.compile(r"outlets?", re.I), 4),
)

#: Sitemaps that cannot hold a locator page, skipped so a 40k-product sitemap
#: is never downloaded.
SKIP = re.compile(r"(product|collection|article|blog|image|variant)", re.I)


def raw(url: str, timeout: int = 20, limit: int = 2_000_000) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        body = r.read(limit)
    if url.endswith(".gz"):
        body = gzip.decompress(body)
    return body


def locs(url: str) -> list[str]:
    try:
        return re.findall(r"<loc>\s*(.*?)\s*</loc>",
                          raw(url).decode("utf-8", "replace"))
    except Exception:
        return []


def rank(url: str) -> int | None:
    for pattern, score in WANTED:
        if pattern.search(url):
            return score
    return None


def find(item: tuple[str, dict]) -> tuple[str, list[str]]:
    brand, entry = item
    site = entry["site"]
    base = f"{urlparse(site).scheme}://{urlparse(site).netloc}/"

    urls = locs(urljoin(base, "sitemap.xml"))
    if not urls:
        urls = locs(urljoin(base, "sitemap_index.xml"))
    children = [u for u in urls if re.search(r"\.xml(\.gz)?$", u) and not SKIP.search(u)]
    pages = [u for u in urls if u not in children]

    # Shopify splits pages into their own child sitemap, so one more hop.
    if children:
        with ThreadPoolExecutor(4) as ex:
            for got in ex.map(locs, children[:6]):
                pages += got

    hits = sorted({u for u in pages if rank(u) is not None},
                  key=lambda u: (rank(u), len(u)))
    return brand, hits[:6]


def main() -> None:
    sites = json.loads(SITES.read_text())
    found: dict[str, list[str]] = {}
    with ThreadPoolExecutor(10) as ex:
        for i, (brand, hits) in enumerate(ex.map(find, sites.items()), 1):
            if hits:
                found[brand] = hits
            if i % 25 == 0:
                print(f"  {i}/{len(sites)} with a locator URL: {len(found)}", flush=True)
                OUT.write_text(json.dumps(found, indent=1, ensure_ascii=False))
    OUT.write_text(json.dumps(found, indent=1, ensure_ascii=False))
    print(f"wrote {OUT.name}: locator URLs for {len(found)} of {len(sites)} brands")


if __name__ == "__main__":
    main()
