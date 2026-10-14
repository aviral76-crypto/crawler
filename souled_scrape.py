"""
The Souled Store (www.thesouledstore.com) scraper.

Its sitemap lists only category pages, so there's a discovery step:
    sitemap.xml  ->  category pages  ->  /product/<slug> links  ->  product pages

    pip install requests beautifulsoup4 lxml

    python souled_scrape.py categories --out categories.csv
    python souled_scrape.py discover  --out products.csv --limit 5      # try a few categories
    python souled_scrape.py enrich    --out sample.csv  --limit 6
    python souled_scrape.py inspect   --url "https://www.thesouledstore.com/product/<slug>"

robots.txt: Allow /, disallows /search and ?utm_/?gte= URLs, no Crawl-delay declared.
We still pace at 1 req/sec.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = "https://www.thesouledstore.com"
SITEMAP = f"{ROOT}/sitemap.xml"
USER_AGENT = "Mozilla/5.0 (compatible; price-research/0.1; +mailto:you@example.com)"
DELAY = 1.0
TIMEOUT = 30
MAX_RETRIES = 3
PRODUCT_RE = re.compile(r"^/product/[^/?#]+/?$")


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


# ---------------------------------------------------------------- categories

def cmd_categories(args: argparse.Namespace) -> None:
    s = session()
    res = get(s, SITEMAP)
    if not res or res.status_code != 200:
        print("could not fetch sitemap")
        return
    soup = BeautifulSoup(res.content, "xml")
    rows = []
    for loc in soup.find_all("loc"):
        url = loc.get_text(strip=True)  # some entries have stray whitespace
        if not url:
            continue
        parts = [p for p in urlparse(url).path.split("/") if p]
        rows.append(
            {
                "url": url,
                "section": parts[0] if parts else "",       # men / women / explore ...
                "category": parts[-1] if parts else "",
            }
        )
    write_csv(args.out, rows)
    print(f"wrote {len(rows)} category pages to {args.out}")


# ---------------------------------------------------------------- discover

def product_links(html: str, base: str) -> list[str]:
    """Product URLs from <a> tags, plus any /product/<slug> inside embedded JSON."""
    found: list[str] = []
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        link = urljoin(base, a["href"]).split("?")[0].rstrip("/")
        if PRODUCT_RE.match(urlparse(link).path + ("/" if link.endswith("/") else "")) or urlparse(link).path.startswith("/product/"):
            found.append(link)
    for m in re.finditer(r'"(?:url|link|slug)"\s*:\s*"((?:https?://[^"]+)?/product/[^"]+)"', html):
        found.append(urljoin(base, m.group(1)).split("?")[0].rstrip("/"))

    seen, out = set(), []
    for link in found:
        if link not in seen and "/product/" in link:
            seen.add(link)
            out.append(link)
    return out


def cmd_discover(args: argparse.Namespace) -> None:
    with open(args.cats, newline="", encoding="utf-8") as f:
        categories = list(csv.DictReader(f))[: args.limit]

    s = session()
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    for i, cat in enumerate(categories, 1):
        page, added_total = 1, 0
        while page <= args.pages:
            url = cat["url"] if page == 1 else f"{cat['url']}?page={page}"
            res = get(s, url)
            time.sleep(DELAY)
            if not res or res.status_code != 200:
                break
            links = product_links(res.text, url)
            new = [link for link in links if link not in seen]
            if not new:
                break  # no more products (or pagination isn't ?page=)
            for link in new:
                seen.add(link)
                rows.append(
                    {
                        "url": link,
                        "slug": urlparse(link).path.split("/product/")[-1].strip("/"),
                        "section": cat.get("section", ""),
                        "category": cat.get("category", ""),
                    }
                )
            added_total += len(new)
            page += 1
        print(f"[{i}/{len(categories)}] {cat['category']}: {added_total} products")
        if added_total == 0:
            print("    ^ no links in the HTML — the listing is probably client-rendered;"
                  " run `inspect` on this category URL")

    write_csv(args.out, rows)
    print(f"wrote {len(rows)} products to {args.out}")


# ---------------------------------------------------------------- enrich

def json_blobs(html: str) -> Iterable[Any]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            yield json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
    for attrs in ({"id": "__NEXT_DATA__"}, {"id": "__NUXT_DATA__"}):
        tag = soup.find("script", attrs=attrs)
        if tag and tag.string:
            try:
                yield json.loads(tag.string)
            except json.JSONDecodeError:
                pass


def find_product_ld(blob: Any) -> dict[str, Any] | None:
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
    return offers if isinstance(offers, list) else []


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
        row["description"] = str(product.get("description", ""))[:500]
        offers = offers_of(product)
        if offers:
            row["price"] = str(offers[0].get("price", ""))
            row["currency"] = str(offers[0].get("priceCurrency", ""))
            row["availability"] = str(offers[0].get("availability", "")).split("/")[-1]
            row["variants"] = " | ".join(
                str(o.get("name") or o.get("sku") or "") for o in offers if o.get("name") or o.get("sku")
            )
        rating = product.get("aggregateRating") or {}
        row["rating"] = str(rating.get("ratingValue", ""))
        row["review_count"] = str(rating.get("reviewCount", ""))
        break

    if not row.get("price"):
        m = re.search(r'"(?:price|sellingPrice|finalPrice|mrp)"\s*:\s*"?(\d+(?:\.\d+)?)"?', html)
        if m:
            row["price"] = m.group(1)
            row["currency"] = row.get("currency") or "INR"
    return row


def cmd_enrich(args: argparse.Namespace) -> None:
    with open(args.inp, newline="", encoding="utf-8") as f:
        catalog = list(csv.DictReader(f))

    done: set[str] = set()
    if os.path.exists(args.out):
        with open(args.out, newline="", encoding="utf-8") as f:
            done = {r["url"] for r in csv.DictReader(f)}
        print(f"resuming, {len(done)} already fetched")

    todo = [r for r in catalog if r["url"] not in done][: args.limit]
    s = session()
    buffer: list[dict[str, str]] = []

    for i, item in enumerate(todo, 1):
        res = get(s, item["url"])
        time.sleep(DELAY)
        if not res or res.status_code != 200:
            print(f"[{i}/{len(todo)}] FAIL {item['url']}")
            continue
        detail = parse_product(item["url"], res.text)
        buffer.append({**item, **detail})
        print(f"[{i}/{len(todo)}] {detail.get('name') or item['slug']} — {detail.get('price', '?')}")
        if len(buffer) >= 25:
            append_csv(args.out, buffer)
            buffer = []

    if buffer:
        append_csv(args.out, buffer)
    print(f"done → {args.out}")


def cmd_inspect(args: argparse.Namespace) -> None:
    s = session()
    res = get(s, args.url)
    if not res:
        print("fetch failed")
        return
    open("page.html", "w", encoding="utf-8").write(res.text)
    print(f"status {res.status_code}, {len(res.text)} chars → page.html")
    links = product_links(res.text, args.url)
    print(f"product links found in HTML: {len(links)}")
    for link in links[:5]:
        print("  ", link)
    for blob in json_blobs(res.text):
        product = find_product_ld(blob)
        if product:
            print("\n--- JSON-LD Product ---")
            print(json.dumps(product, indent=2)[:3000])
            return
        print("--- JSON blob keys:", list(blob)[:15] if isinstance(blob, dict) else type(blob))
    print("\nNo JSON-LD Product. Search page.html for a price you can see on screen.")


# ---------------------------------------------------------------- io

def write_csv(path: str, rows: list[dict[str, str]]) -> None:
    if not rows:
        print("nothing to write")
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

    c = sub.add_parser("categories", help="category pages from sitemap.xml")
    c.add_argument("--out", default="categories.csv")
    c.set_defaults(func=cmd_categories)

    d = sub.add_parser("discover", help="find product URLs on category pages")
    d.add_argument("--cats", default="categories.csv")
    d.add_argument("--out", default="products.csv")
    d.add_argument("--limit", type=int, default=5, help="how many categories to walk")
    d.add_argument("--pages", type=int, default=3, help="max ?page=N per category")
    d.set_defaults(func=cmd_discover)

    e = sub.add_parser("enrich", help="fetch product pages")
    e.add_argument("--in", dest="inp", default="products.csv")
    e.add_argument("--out", default="sample.csv")
    e.add_argument("--limit", type=int, default=6)
    e.set_defaults(func=cmd_enrich)

    i = sub.add_parser("inspect", help="dump one page's links and JSON")
    i.add_argument("--url", required=True)
    i.set_defaults(func=cmd_inspect)

    args = ap.parse_args()
    args.func(args)
