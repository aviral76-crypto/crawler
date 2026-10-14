"""
Snitch (www.snitch.com) product scraper — sitemap first, then per-product enrichment.

The site is a headless Shopify store with a Next.js front end, so the usual
/products.json endpoint returns the app shell instead of JSON. robots.txt allows
product pages, disallows /api/*, and asks for Crawl-delay: 1.

    pip install requests beautifulsoup4 lxml

    # Stage 1 — catalogue from the sitemaps (2 requests, a few seconds)
    python snitch_scrape.py catalog --out products.csv

    # Stage 2 — prices/sizes by visiting each product page (1 req/sec, resumable)
    python snitch_scrape.py enrich --in products.csv --out products_detailed.csv --limit 100

    # Inspect one product page to see what's actually embedded
    python snitch_scrape.py inspect --url "<product url>"
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

SITEMAPS = [
    "https://www.snitch.com/sitemap-products-1.xml",
    "https://www.snitch.com/sitemap-products-2.xml",
]
USER_AGENT = "Mozilla/5.0 (compatible; price-research/0.1; +mailto:you@example.com)"
DELAY = 1.0  # robots.txt Crawl-delay
TIMEOUT = 30
MAX_RETRIES = 3


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-IN,en;q=0.9"})
    return s


def get(s: requests.Session, url: str) -> requests.Response | None:
    for attempt in range(MAX_RETRIES):
        try:
            res = s.get(url, timeout=TIMEOUT)
        except requests.RequestException:
            time.sleep(DELAY * (attempt + 2))
            continue
        if res.status_code == 429 or res.status_code >= 500:
            time.sleep(float(res.headers.get("Retry-After", DELAY * (attempt + 2))))
            continue
        return res
    return None


# ---------------------------------------------------------------- stage 1

def parse_sitemap(xml: bytes) -> list[dict[str, str]]:
    soup = BeautifulSoup(xml, "xml")
    rows = []
    for url in soup.find_all("url"):
        loc = url.find("loc", recursive=False)
        if not loc:
            continue
        link = loc.get_text(strip=True)
        # /men-shirts/<handle>/<shopify_product_id>/buy
        parts = [p for p in urlparse(link).path.split("/") if p]
        img = url.find("image")  # <image:image> block
        img_loc = img.find("loc") if img else None
        img_title = img.find("title") if img else None
        lastmod = url.find("lastmod")
        rows.append(
            {
                "url": link,
                "product_id": parts[2] if len(parts) > 2 and parts[2].isdigit() else "",
                "category": unquote(parts[0]) if parts else "",
                "handle": parts[1] if len(parts) > 1 else "",
                "title": img_title.get_text(strip=True) if img_title else "",
                "image": img_loc.get_text(strip=True) if img_loc else "",
                "lastmod": lastmod.get_text(strip=True) if lastmod else "",
            }
        )
    return rows


def cmd_catalog(args: argparse.Namespace) -> None:
    s = session()
    rows: list[dict[str, str]] = []
    for sm in SITEMAPS:
        res = get(s, sm)
        time.sleep(DELAY)
        if not res or res.status_code != 200:
            print(f"skip {sm} ({res.status_code if res else 'no response'})")
            continue
        found = parse_sitemap(res.content)
        print(f"{sm}: {len(found)} products")
        rows += found

    seen, unique = set(), []
    for r in rows:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        unique.append(r)

    write_csv(args.out, unique)
    print(f"wrote {len(unique)} products to {args.out}")


# ---------------------------------------------------------------- stage 2

def json_blobs(html: str) -> Iterable[Any]:
    """Yield every JSON-LD object and the __NEXT_DATA__ payload, if present."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            yield json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
    nd = soup.find("script", attrs={"id": "__NEXT_DATA__"})
    if nd and nd.string:
        try:
            yield json.loads(nd.string)
        except json.JSONDecodeError:
            pass


def find_product_ld(blob: Any) -> dict[str, Any] | None:
    """JSON-LD can be a list, a @graph, or a bare object."""
    if isinstance(blob, list):
        for item in blob:
            found = find_product_ld(item)
            if found:
                return found
    elif isinstance(blob, dict):
        if blob.get("@type") in ("Product", ["Product"]):
            return blob
        if "@graph" in blob:
            return find_product_ld(blob["@graph"])
    return None


def offers_of(product: dict[str, Any]) -> list[dict[str, Any]]:
    offers = product.get("offers")
    if isinstance(offers, dict):
        return offers.get("offers", []) if offers.get("@type") == "AggregateOffer" else [offers]
    if isinstance(offers, list):
        return offers
    return []


def parse_product(url: str, html: str) -> dict[str, str]:
    row: dict[str, str] = {"url": url}
    for blob in json_blobs(html):
        product = find_product_ld(blob)
        if not product:
            continue
        row["name"] = str(product.get("name", ""))
        row["sku"] = str(product.get("sku", ""))
        brand = product.get("brand")
        row["brand"] = brand.get("name", "") if isinstance(brand, dict) else str(brand or "")
        offers = offers_of(product)
        prices = [o.get("price") for o in offers if o.get("price") is not None]
        row["price"] = str(prices[0]) if prices else ""
        row["currency"] = str(offers[0].get("priceCurrency", "")) if offers else ""
        row["availability"] = str(offers[0].get("availability", "")).split("/")[-1] if offers else ""
        sizes = [str(o.get("name") or o.get("sku") or "") for o in offers]
        row["variants"] = " | ".join(x for x in sizes if x)
        rating = product.get("aggregateRating") or {}
        row["rating"] = str(rating.get("ratingValue", ""))
        row["review_count"] = str(rating.get("reviewCount", ""))
        break

    if not row.get("price"):
        # Fallback: MRP / selling price often sit in the Next.js payload
        m = re.search(r'"(?:price|sellingPrice|finalPrice)"\s*:\s*"?(\d+(?:\.\d+)?)"?', html)
        if m:
            row["price"] = m.group(1)
            row["currency"] = row.get("currency") or "INR"
    return row


def cmd_enrich(args: argparse.Namespace) -> None:
    with open(args.inp, newline="", encoding="utf-8") as f:
        catalog = list(csv.DictReader(f))

    done: set[str] = set()
    if os.path.exists(args.out):  # resume
        with open(args.out, newline="", encoding="utf-8") as f:
            done = {r["url"] for r in csv.DictReader(f)}
        print(f"resuming, {len(done)} already fetched")

    todo = [r for r in catalog if r["url"] not in done][: args.limit]
    s = session()
    rows: list[dict[str, str]] = []

    for i, item in enumerate(todo, 1):
        res = get(s, item["url"])
        time.sleep(DELAY)
        if not res or res.status_code != 200:
            print(f"[{i}/{len(todo)}] FAIL {item['url']}")
            continue
        detail = parse_product(item["url"], res.text)
        rows.append({**item, **detail})
        print(f"[{i}/{len(todo)}] {detail.get('name') or item['title']} — {detail.get('price', '?')}")
        if len(rows) % 25 == 0:
            append_csv(args.out, rows)
            rows = []

    if rows:
        append_csv(args.out, rows)
    print(f"done → {args.out}")


def cmd_inspect(args: argparse.Namespace) -> None:
    """Dump what a product page actually embeds, so you can adjust parse_product()."""
    s = session()
    res = get(s, args.url)
    if not res:
        print("fetch failed")
        return
    open("page.html", "w", encoding="utf-8").write(res.text)
    print(f"status {res.status_code}, {len(res.text)} chars → page.html")
    for blob in json_blobs(res.text):
        product = find_product_ld(blob)
        if product:
            print("\n--- JSON-LD Product ---")
            print(json.dumps(product, indent=2)[:3000])
            return
        print("\n--- JSON blob keys ---", list(blob)[:20] if isinstance(blob, dict) else type(blob))
    print("\nNo JSON-LD Product found — search page.html for a price you can see on screen")


# ---------------------------------------------------------------- io

def write_csv(path: str, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def append_csv(path: str, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    exists = os.path.exists(path)
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("catalog", help="build product list from sitemaps")
    c.add_argument("--out", default="products.csv")
    c.set_defaults(func=cmd_catalog)

    e = sub.add_parser("enrich", help="fetch each product page for price/variants")
    e.add_argument("--in", dest="inp", default="products.csv")
    e.add_argument("--out", default="products_detailed.csv")
    e.add_argument("--limit", type=int, default=100)
    e.set_defaults(func=cmd_enrich)

    i = sub.add_parser("inspect", help="dump one product page's embedded JSON")
    i.add_argument("--url", required=True)
    i.set_defaults(func=cmd_inspect)

    args = ap.parse_args()
    args.func(args)
