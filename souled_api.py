"""
The Souled Store catalogue collector — uses the site's own search API.

    python3 souled_api.py probe                       # how pagination behaves
    python3 souled_api.py run --out souled.csv        # walk the catalogue
    python3 souled_api.py run --out souled.csv --json souled.json

Keywords: an empty keyword may return everything. If not, the script falls back to
using the category slugs from souled_categories.csv as keywords and de-duplicates by id.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from typing import Any
from urllib.parse import urlparse

import requests

API = "https://api.thesouledstore.com/api/v2/pre-order/products/search"
PRODUCT_URL = "https://www.thesouledstore.com/product/{slug}"
IMAGE_BASE = "https://prod-img.thesouledstore.com/public/theSoul/uploads/catalog/product/"
PAGE_SIZE = 24
DELAY = 1.0
TIMEOUT = 30

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.thesouledstore.com",
    "Referer": "https://www.thesouledstore.com/",
    "U-Device-Type": '{"type":"Desktop","os":"Mac","source":"Browser"}',
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
}


def body(keyword: str, page: int, size: int = PAGE_SIZE, gender: int = 1) -> dict[str, Any]:
    return {
        "filters": {},
        "gender_filter_removed": False,
        "gender_type": gender,
        "isKids": False,
        "is_ab_visible": True,
        "is_exclusive": 0,
        "keyword": keyword,
        "offset": size,
        "page": page,
        "showSelectedFiltersData": {"agegroup": True},
        "show_upgrade": 0,
    }


def search(s: requests.Session, keyword: str, page: int, size: int = PAGE_SIZE,
           gender: int = 1) -> dict[str, Any] | None:
    for attempt in range(3):
        try:
            res = s.post(API, json=body(keyword, page, size, gender), timeout=TIMEOUT)
        except requests.RequestException:
            time.sleep(DELAY * (attempt + 2))
            continue
        if res.status_code == 429 or res.status_code >= 500:
            time.sleep(float(res.headers.get("Retry-After", DELAY * (attempt + 2))))
            continue
        if res.status_code != 200:
            print(f"  HTTP {res.status_code}: {res.text[:200]}")
            return None
        return res.json()
    return None


def flatten(p: dict[str, Any]) -> dict[str, Any]:
    images = p.get("imageUrl") or []
    return {
        "id": p.get("id"),
        "title": p.get("title"),
        "slug": p.get("product_slug"),
        "url": PRODUCT_URL.format(slug=p.get("product_slug")),
        "category": " / ".join(p.get("category") or []),
        "price": p.get("price"),
        "spl_price": p.get("spl_price"),
        "exclusive_price": p.get("exclusive_price"),
        "stock": p.get("stock"),
        "gender_type": p.get("gender_type"),
        "artist": p.get("artist"),
        "prod_type": p.get("prodType"),
        "image": IMAGE_BASE + images[0] if images else "",
        "image_count": len(images),
    }


def cmd_probe(args: argparse.Namespace) -> None:
    s = requests.Session()
    s.headers.update(HEADERS)

    for keyword in ("", "tshirt"):
        first = search(s, keyword, 1)
        time.sleep(DELAY)
        label = repr(keyword) or "''"
        if not first:
            print(f"keyword {label}: no response")
            continue
        total = first.get("numberOfProducts")
        items = first.get("products") or []
        print(f"keyword {label}: total={total}, page1={len(items)}")
        if not items:
            continue
        second = search(s, keyword, 2)
        time.sleep(DELAY)
        slugs1 = {p.get("product_slug") for p in items}
        slugs2 = {p.get("product_slug") for p in (second or {}).get("products", [])}
        if not slugs2:
            print("  page 2 empty — single page only")
        elif slugs1 & slugs2:
            print("  page 2 OVERLAPS page 1 — 'page' is ignored, use offset as a skip count")
        else:
            print("  page 2 is distinct — pagination works via 'page'")

    big = search(s, "tshirt", 1, size=100)
    if big:
        print(f"page size 100 → {len(big.get('products') or [])} items returned")


def keywords_from_categories(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    seen, out = set(), []
    for row in csv.DictReader(open(path, encoding="utf-8")):
        url = (row.get("url") or "").strip()
        if "/product/" in url:
            continue
        slug = urlparse(url).path.rstrip("/").split("/")[-1]
        term = slug.replace("-", " ").strip()
        if term and term not in seen:
            seen.add(term)
            out.append(term)
    return out


def cmd_run(args: argparse.Namespace) -> None:
    s = requests.Session()
    s.headers.update(HEADERS)

    probe = search(s, "", 1, size=args.size)
    time.sleep(DELAY)
    if probe and (probe.get("products") or []):
        terms = [""]
        print(f"empty keyword works — {probe.get('numberOfProducts')} products reported")
    else:
        terms = keywords_from_categories(args.cats)
        print(f"empty keyword returned nothing — using {len(terms)} category keywords")
        if not terms:
            print(f"no {args.cats} found; run: python3 souled_scrape.py categories --out {args.cats}")
            return

    products: dict[int, dict[str, Any]] = {}
    for i, term in enumerate(terms, 1):
        page, prev_slugs = 1, set()
        while page <= args.max_pages:
            data = search(s, term, page, size=args.size, gender=args.gender)
            time.sleep(DELAY)
            items = (data or {}).get("products") or []
            if not items:
                break
            slugs = {p.get("product_slug") for p in items}
            if slugs and slugs == prev_slugs:
                break  # same page repeating — pagination not advancing
            for p in items:
                if p.get("id") is not None:
                    products[p["id"]] = flatten(p)
            prev_slugs = slugs
            if len(items) < args.size:
                break
            page += 1
        print(f"[{i}/{len(terms)}] {term or '(all)'}: total unique so far {len(products)}")

    rows = list(products.values())
    if not rows:
        print("nothing collected")
        return

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} products to {args.out}")

    if args.json:
        json.dump(rows, open(args.json, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        print(f"wrote {args.json}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="check keyword and pagination behaviour")
    p.set_defaults(func=cmd_probe)

    r = sub.add_parser("run", help="collect the catalogue")
    r.add_argument("--out", default="souled.csv")
    r.add_argument("--json", default="")
    r.add_argument("--cats", default="souled_categories.csv")
    r.add_argument("--size", type=int, default=PAGE_SIZE)
    r.add_argument("--gender", type=int, default=1, help="1=men, try 2 for women")
    r.add_argument("--max-pages", type=int, default=100)
    r.set_defaults(func=cmd_run)

    args = ap.parse_args()
    args.func(args)
