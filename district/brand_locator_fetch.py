#!/usr/bin/env python3
"""Pull a brand's own store list off its own store locator, and count it.

    python3 brand_locator_fetch.py list                 # what is wired up
    python3 brand_locator_fetch.py fetch "Go Colors"     # one brand
    python3 brand_locator_fetch.py fetch --all           # every wired brand

Each brand's locator is its own small reverse-engineering job -- some serve a
JSON API, some a paginated HTML fragment, some only a rendered page -- so there
is one FETCHERS entry per brand rather than one clever general scraper. What
every entry returns is the same: a list of (store label, city) pairs, which is
then counted into the six metros the workbook covers.

Results are merged into data/brand_locator_counts.json, which
`brand_store_check.py sheet` turns into the corrected sheet.
"""

from __future__ import annotations

import html
import json
import re
import ssl
import sys
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
LOCATOR_COUNTS = DATA / "brand_locator_counts.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

#: City spellings a brand's locator uses -> the workbook's metro.
#:
#: This has to cover exactly the same ground as city_matrix.CITY_GROUPS, no
#: more: the District columns were built from those groups, so folding a city
#: in here that District excludes would credit the locator with stores
#: District's side structurally cannot have. Mumbai is therefore Mumbai, Navi
#: Mumbai and Thane only -- Kalyan, Mira Road and Vasai are outside it, even
#: though they are part of the wider metropolitan region. Spelling variants
#: District normalises away (Gurugram, Bengaluru, Pimpri, Secunderabad) do
#: belong here, since a brand's locator may use either form.
METRO_OF: dict[str, str] = {
    "gurgaon": "Gurgaon", "gurugram": "Gurgaon",
    "delhi": "Delhi NCR (ex Gurgaon)", "new delhi": "Delhi NCR (ex Gurgaon)",
    "noida": "Delhi NCR (ex Gurgaon)", "greater noida": "Delhi NCR (ex Gurgaon)",
    "ghaziabad": "Delhi NCR (ex Gurgaon)", "faridabad": "Delhi NCR (ex Gurgaon)",
    "bangalore": "Bangalore", "bengaluru": "Bangalore",
    "mumbai": "Mumbai", "navi mumbai": "Mumbai", "thane": "Mumbai",
    "pune": "Pune", "pimpri": "Pune", "chinchwad": "Pune",
    "pimpri chinchwad": "Pune", "pimpri-chinchwad": "Pune",
    "hyderabad": "Hyderabad", "secunderabad": "Hyderabad",
}


def metro_of(city: str | None) -> str | None:
    """Which of the workbook's six metros a locator's city name falls in."""
    c = re.sub(r"[^a-z ]+", " ", (city or "").lower())
    c = re.sub(r"\s+", " ", c).strip()
    if c in METRO_OF:
        return METRO_OF[c]
    # "Thane West", "Navi Mumbai (Vashi)", "South Delhi" -- longest key wins so
    # "greater noida" is not read as "noida" and "navi mumbai" not as "mumbai".
    for key in sorted(METRO_OF, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", c):
            return METRO_OF[key]
    return None


def get(url: str, data: bytes | None = None, timeout: int = 30) -> str:
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA, "Accept": "*/*",
        **({"Content-Type": "application/x-www-form-urlencoded"} if data else {})})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return r.read().decode("utf-8", "replace")


def get_json(url: str, data: bytes | None = None, timeout: int = 30):
    return json.loads(get(url, data, timeout))


def paged(fn: Callable[[int], list], pages: range, workers: int = 10) -> list:
    """Fetch numbered pages in parallel and concatenate them."""
    with ThreadPoolExecutor(workers) as ex:
        return [item for page in ex.map(fn, pages) for item in page]


def yext_stores(host: str) -> list[tuple[str, str]]:
    """Every store a Yext-style locator lists, from its sitemap index.

    These locators (stores.levi.in and friends) publish one child sitemap per
    city, each listing several URLs per store -- Home, Map, Photos. store_pages
    below collapses those onto one entry per outlet, and the child sitemap's
    filename gives the city.
    """
    import gzip
    from urllib.request import Request, urlopen

    def raw(url: str, tries: int = 4) -> str:
        last: Exception | None = None
        for attempt in range(tries):
            try:
                with urlopen(Request(url, headers={"User-Agent": UA}), timeout=30,
                             context=CTX) as r:
                    body = r.read()
                # Sniff rather than trust the extension: these URLs carry
                # query strings, so ".xml.gz?bid=..." does not end in ".gz".
                if body[:2] == b"\x1f\x8b":
                    body = gzip.decompress(body)
                return body.decode("utf-8", "replace")
            except Exception as exc:                   # these hosts rate-limit
                last = exc
                time.sleep(2 ** attempt)                     # back off before retrying
        raise RuntimeError(f"{url} failed after {tries} tries: {last}")

    #: Path prefixes that are directories of stores, never a store.
    not_a_store = ("location", "search", "near-me")

    def store_pages(urls: list[str], city: str) -> list[tuple[str, str]]:
        """One entry per outlet among a sitemap's URLs.

        Every store is published once per tab, and the tab names differ by
        locator (Home, Map, Photos-Videos, Products, home...), so rather than
        keep a list of them the store is taken to be the parent of any path
        that has children. That self-calibrates across the shapes seen:

            levi-s-saket-110017/Home             -> levi-s-saket-110017
            pantaloons/<city>/<locality>/<id>/home -> .../<id>
            clothing-store-in-civil-lines-agra   -> itself (BIBA has no tabs)

        Keying on the first segment instead collapsed all 390 Pantaloons
        stores into one; keying on a fixed tab list double-counted Blackberrys,
        whose tabs include "Products".
        """
        paths = []
        for u in urls:
            if f"{host}/" not in u:
                continue
            parts = [p for p in u.split(f"{host}/", 1)[1].split("?")[0].split("/") if p]
            # store.ethnicity.in's sitemap lists only itself, over and over; a
            # sitemap is never a store page.
            if parts and parts[0] not in not_a_store and not parts[-1].endswith(".xml"):
                paths.append(parts)

        parents = {"/".join(p[:-1]) for p in paths if len(p) > 1}
        stores = {("/".join(p[:-1]) if "/".join(p[:-1]) in parents else "/".join(p))
                  for p in paths}
        # A parent that is itself some other store's prefix is a directory page.
        return [(s, city) for s in stores
                if s and not any(o != s and o.startswith(s + "/") for o in stores)]

    top = [html.unescape(u) for u in
           re.findall(r"<loc>(.*?)</loc>", raw(f"https://{host}/sitemap.xml"))]
    if not top:
        raise RuntimeError(f"{host} sitemap is empty")

    # Two shapes in the wild: an index of per-city child sitemaps (Levi's), or
    # one flat urlset listing every store page (BIBA).
    # A child sitemap can carry a query string ("sitemap.xml?bid=..."), so match
    # the extension before the query rather than at the end of the URL.
    children = [u for u in top if re.search(r"\.xml(\.gz)?($|\?)", u)]
    stores = store_pages([u for u in top if u not in children], "")

    def one(child: str) -> list[tuple[str, str]]:
        m = re.search(r"/([^/]+)/([^/]+)\.xml", child)
        city = (m.group(2) if m else "").replace("_", " ").replace("-", " ")
        # A swallowed failure here would silently undercount the brand, so let
        # it propagate and skip the brand instead.
        return store_pages([html.unescape(u) for u in
                            re.findall(r"<loc>(.*?)</loc>", raw(child))], city)

    if children:
        stores += paged(one, children, workers=4)      # child sitemaps, not pages
    # One store can be listed under two cities; keep the first sighting.
    seen: dict[str, str] = {}
    for slug, city in stores:
        seen.setdefault(slug, city)
    if not seen:
        raise RuntimeError(f"{host} sitemap listed no store pages")
    return list(seen.items())


# --------------------------------------------------------------- per brand

def go_colors() -> tuple[list[tuple[str, str]], str, str]:
    """Laravel locator behind the Shopify app proxy; 15 stores an HTML page."""
    base = "https://admin3157.gocolors.com/new_go_colors_india/public/getstore-all"

    def one(n: int) -> list[str]:
        frag = html.unescape(get_json(f"{base}?page={n}")["data"])
        return re.findall(r"<h3>(.*?)</h3>", frag, re.S)

    # The first page states the India-wide total, which also bounds the paging.
    first = html.unescape(get_json(base)["data"])
    total = int(re.search(r"ALL\s+STORES\s*\((\d+)\)", first).group(1))
    names = paged(one, range(1, total // 15 + 3))
    # "Go Colors - KONDAPUR - SARATH CITY MALL, Hyderabad" -> city is last
    stores = [(n.strip(), n.rsplit(",", 1)[-1].strip()) for n in dict.fromkeys(names)]
    return stores, "https://gocolors.com/apps/s/storelocators", f"stated total {total}"


#: U.S. Polo Assn. is deliberately absent: it runs both a sitemap subdomain
#: (139 outlets) and the Stockist widget its own /pages/stores links to (356),
#: and the linked one is the brand's live locator. brand_locator_scan.py takes
#: it from there.
#: Brands whose locator is a sitemap-backed subdomain -- brand: (site, host).
#: All of them answer to yext_stores, so they need no code of their own.
YEXT: dict[str, tuple[str, str]] = {
    "Levi's": ("https://levi.in/", "stores.levi.in"),
    "Blackberrys": ("https://blackberrys.com/", "stores.blackberrys.com"),
    "Pepe Jeans": ("https://www.pepejeans.in/", "stores.pepejeans.in"),
    "Jack & Jones": ("https://www.jackjones.in/", "stores.jackjones.in"),
    "Monte Carlo": ("https://www.montecarlo.in/", "stores.montecarlo.in"),
    "Rareism": ("https://rareism.com/", "stores.rareism.com"),
    "Tasva": ("https://www.tasva.com/", "stores.tasva.com"),
    "Armani Exchange": ("https://armaniexchange.in/", "stores.armaniexchange.in"),
    "Kraus Jeans": ("https://www.krausjeans.com/", "storelocator.krausjeans.com"),
    "Ritu Kumar": ("https://www.ritukumar.com/", "stores.ritukumar.com"),
    "ONLY": ("https://www.only.in/", "stores.only.in"),
    "Only": ("https://www.only.in/", "stores.only.in"),
    "GANT": ("https://www.gant.in/", "stores.gant.in"),
    "Miniklub": ("https://www.miniklub.in/", "stores.miniklub.in"),
    "Ethnicity": ("https://www.ethnicity.in/", "store.ethnicity.in"),
    "Kazo": ("https://www.kazo.com/", "stores.kazo.com"),
    "Nykd by Nykaa": ("https://www.nykdbynykaa.com/", "stores.nykdbynykaa.com"),
    "Elleven": ("https://www.elleven.in/", "stores.elleven.in"),
    "La Martina": ("https://www.lamartina.in/", "stores.lamartina.in"),
    "SELECTED HOMME": ("https://www.selectedhomme.in/", "stores.selectedhomme.in"),
    "Flying Machine": ("https://flyingmachine.in/", "stores.flyingmachine.in"),
    "Style Union": ("https://www.styleunion.in/", "stores.styleunion.in"),
    "W For Woman": ("https://www.wforwoman.com/", "stores.wforwoman.com"),
    "BIBA": ("https://www.biba.in/", "stores.biba.in"),
    "Vero Moda": ("https://www.veromoda.in/", "stores.veromoda.in"),
    "Wishful": ("https://wishfulbyw.com/", "stores.wishfulbyw.com"),
    "Mufti": ("https://www.muftijeans.in/", "stores.muftijeans.in"),
    "Killer": ("https://www.killerjeans.com/", "stores.killerjeans.com"),
    "Sabhyata": ("https://www.sabhyataclothing.com/", "stores.sabhyataclothing.com"),
    "Jaipur Kurti": ("https://jaipurkurti.com/", "store.jaipurkurti.com"),
    "Superdry": ("https://www.superdry.in/", "stores.superdry.in"),
    "The Pant Project": ("https://www.pantproject.com/", "stores.pantproject.com"),
    "The Bear House": ("https://www.thebearhouse.com/", "stores.thebearhouse.com"),
    "VIRGIO": ("https://www.virgio.com/", "store.virgio.com"),
    "Aurelia": ("https://aurelia.shop/", "stores.shopforaurelia.com"),
    "Shoppers Stop": ("https://www.shoppersstop.com/", "store.shoppersstop.com"),
    "Pantaloons": ("https://www.pantaloons.com/", "stores.pantaloons.com"),
    "Manyavar": ("https://www.manyavar.com/", "stores.manyavar.com"),
    "Mothercare": ("https://www.mothercare.in/", "stores.mothercare.in"),
    "GUESS": ("https://guess.in/", "stores.guess.com"),
    "Reebok": ("https://www.reebok.in/", "stores.reebok.com"),
}

#: Locators that cover the brand worldwide, not just India. The metro counts
#: still hold (they are matched on Indian city names) but the sitemap's size is
#: a world total, so no India figure is reported for these.
GLOBAL_HOSTS = {"stores.guess.com", "stores.reebok.com"}

#: Brands that share one locator with a parent label, which the locator does
#: not separate -- counting it would report the parent's stores twice. Left
#: unchanged rather than guessed: "Aarke Ritu Kumar" on stores.ritukumar.com,
#: "Trends Woman" inside Reliance Trends, "U.S. Polo Assn. Kids" except where
#: the store names say "Kids" (which the Stockist feed does, so the scan
#: handles that one).
SHARED_WITH_PARENT = ("Aarke Ritu Kumar", "Trends Woman")

FETCHERS: dict[str, dict] = {
    "Go Colors": {
        "site": "https://gocolors.com/",
        "fn": go_colors,
        "method": "locator API (paged)",
    },
    **{brand: {
        "site": site,
        "fn": (lambda h=host: (
            stable_stores(h), f"https://{h}/",
            "this locator covers the brand worldwide, so no India total is "
            "reported; the metro counts are matched on Indian city names"
            if h in GLOBAL_HOSTS else "")),
        "method": "locator sitemap (one page per store)",
        "worldwide": host in GLOBAL_HOSTS,
    } for brand, (site, host) in YEXT.items()},
}


def stable_stores(host: str, tries: int = 6) -> list[tuple[str, str]]:
    """yext_stores, but only once the *fullest* read has reproduced.

    Some of these hosts serve a short sitemap under load and raise nothing:
    stores.styleunion.in returned 198, 266, 278 and 222 stores on four reads,
    and stores.miniklub.in returned 49, 4, 58, 4. A single read is therefore
    not evidence.

    Agreement alone is not enough either -- Miniklub's two reads of 4 agreed
    with each other while a read of 58 proved both were partial. So the rule is
    that the largest count seen must itself have come back twice: a bigger read
    is proof that a smaller one was short, and can never be proof of the
    reverse.
    """
    seen: dict[int, list[tuple[str, str]]] = {}
    counts: list[int] = []
    for _ in range(tries):
        stores = yext_stores(host)
        counts.append(len(stores))
        if len(stores) in seen and len(stores) == max(counts):
            return seen[len(stores)]
        seen.setdefault(len(stores), stores)
    raise RuntimeError(
        f"{host} never served its fullest store list twice "
        f"({', '.join(map(str, counts))}) -- refusing to publish a number "
        "that a larger read shows to be partial")


# --------------------------------------------------------------- driver

def run(brand: str) -> dict:
    spec = FETCHERS[brand]
    stores, locator, note = spec["fn"]()
    metros = Counter()
    unmapped = 0
    for label, city in stores:
        # Some locators name the city only inside the store label
        # ("...-nexus-mall-koramangala-bangalore-560034"), so fall back to it.
        m = metro_of(city) or metro_of(label.replace("-", " ").replace("/", " "))
        if m:
            metros[m] += 1
        else:
            unmapped += 1
    return {
        "site": spec["site"],
        "locator": locator,
        "india_total": None if spec.get("worldwide") else len(stores),
        "metros": dict(metros),
        "method": spec["method"],
        "checked": date.today().isoformat(),
        "note": f"{note}; {len(stores)} stores listed, {unmapped} outside the six metros",
    }


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] == "list":
        print(f"{len(FETCHERS)} brands wired up:")
        for b in sorted(FETCHERS):
            print(f"  {b}")
        return
    if args[0] != "fetch":
        raise SystemExit(__doc__)

    wanted = sorted(FETCHERS) if args[1:] == ["--all"] else args[1:]
    cache = json.loads(LOCATOR_COUNTS.read_text()) if LOCATOR_COUNTS.exists() else {}
    for brand in wanted:
        if brand in SHARED_WITH_PARENT:
            print(f"-- {brand}: shares its parent brand's locator, which does "
                  "not separate the two; left unchanged")
            continue
        if brand not in FETCHERS:
            print(f"!! no fetcher for {brand!r}")
            continue
        try:
            cache[brand] = run(brand)
        except Exception as exc:                       # one brand must not stop the rest
            # Drop any earlier number too: a figure this run cannot reproduce
            # has no business staying in the sheet as a correction.
            stale = " (dropped the cached number)" if cache.pop(brand, None) else ""
            print(f"!! {brand}: {type(exc).__name__}: {exc}{stale}")
            continue
        hit = cache[brand]
        print(f"{brand}: {hit['india_total']} in India, "
              + ", ".join(f"{k} {v}" for k, v in sorted(hit["metros"].items())))
    LOCATOR_COUNTS.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    print(f"wrote {LOCATOR_COUNTS.name}: {len(cache)} brands")


if __name__ == "__main__":
    main()
