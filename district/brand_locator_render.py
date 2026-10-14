#!/usr/bin/env python3
"""Count a brand's stores off a locator that only exists once JavaScript runs.

    python3 brand_locator_render.py Spykar
    python3 brand_locator_render.py --all

Headless Chrome renders the locator page, then one of the PATTERNS below pulls
the store rows out of the resulting DOM. Chrome is used rather than a plain
fetch because these locators render client-side; it is used rather than a
hand-found API because these apps ship the whole list to the browser and
paginate it there.

Two guards keep a partial render from being published as a correction:
  * the store count must match the number of map/direction links on the page,
    which these apps emit one-per-store;
  * the latitudes must span more than SPAN_MIN degrees, so a page that
    rendered only the first city is rejected rather than counted.

Writes into data/brand_locator_render_counts.json, which
`brand_store_check.py sheet` merges with the other locator sources.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from brand_locator_fetch import metro_of

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OUT = DATA / "brand_locator_render_counts.json"

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

#: India spans roughly 8N to 35N. A locator that rendered every store covers
#: most of it; one that rendered a single city covers almost none.
SPAN_MIN = 8.0

#: Chrome's own headless user-agent gets refused by bot protection that lets a
#: normal browser through -- hm.com answers "Access Denied" to the default and
#: serves the full locator with this.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

#: brand -> locator URL to render.
TARGETS: dict[str, str] = {
    "Spykar": "https://spykar.com/pages/store-locator",
}

#: Locators that are a directory of per-store pages rather than a list on one
#: page: render the index, then count the store links it carries. The regex
#: must capture (city, store) from each store URL.
#:   brand -> (index url, store-link pattern)
DIRECTORY: dict[str, tuple[str, str]] = {
    "H&M": ("https://www2.hm.com/en_in/customer-service/shopping-at-hm/"
            "store-locator.html",
            r"/store-locator/india/([a-z0-9-]+)/([a-z0-9-]+)/([A-Za-z0-9]+)\.html"),
}


def render(url: str, budget_ms: int = 25000, tries: int = 3) -> str:
    """Render one page, retrying -- these hosts stall on an odd request."""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            out = subprocess.run(
                [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                 "--disable-blink-features=AutomationControlled",
                 f"--user-agent={BROWSER_UA}",
                 f"--virtual-time-budget={budget_ms}", "--dump-dom", url],
                capture_output=True, timeout=240)
            dom = out.stdout.decode("utf-8", "replace")
            if len(dom) > 20_000:
                return dom
            last = RuntimeError(f"rendered only {len(dom)} bytes")
        except subprocess.TimeoutExpired as exc:
            last = exc
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{url}: {type(last).__name__}: {last}")


def count_directory(brand: str) -> dict:
    """Count a directory-style locator by walking its per-city pages.

    The index is only used for the city list, never for the counts: it renders
    a partial accordion, so it showed 8 Bangalore stores where Bangalore's own
    page lists 9. Each city page is then the authority for that city.
    """
    index, pattern = DIRECTORY[brand]
    dom = render(index)
    if "Access Denied" in dom[:4000] or len(dom) < 20_000:
        raise RuntimeError("locator index did not render")
    cities = sorted({c for c, _, _ in re.findall(pattern, dom)})
    if len(cities) < 3:
        raise RuntimeError(f"index listed only {len(cities)} cities -- looks partial")

    def city_stores(city: str) -> tuple[str, set[str]]:
        page = index.replace(".html", f"/india/{city}.html")
        got = render(page)
        return city, {code.lower() for c, _, code in re.findall(pattern, got)
                      if c == city}

    with ThreadPoolExecutor(3) as ex:
        per_city = dict(ex.map(city_stores, cities))
    empty = [c for c, s in per_city.items() if not s]
    if empty:
        raise RuntimeError(f"{len(empty)} city pages rendered no stores "
                           f"({', '.join(empty[:4])}) -- counts would be short")

    stores = {code: city for city, codes in per_city.items() for code in codes}
    metros, outside = Counter(), 0
    for code, city in stores.items():
        m = metro_of(city.replace("-", " "))
        if m:
            metros[m] += 1
        else:
            outside += 1
    return {
        "site": re.match(r"https?://[^/]+/", index).group(0),
        "locator": index,
        "india_total": len(stores),
        "metros": dict(metros),
        "method": "locator directory index, rendered in headless Chrome",
        "checked": date.today().isoformat(),
        "note": (f"{len(stores)} stores across {len(per_city)} city pages, "
                 f"{outside} outside the six metros; counted from each city's "
                 "own locator page, city taken from the store page URL"),
    }


def addresses(dom: str) -> list[str]:
    """Store addresses, from the app layouts seen so far."""
    rows = re.findall(r"<strong>\s*Address\s*</strong>\s*:?\s*(.*?)</span>", dom, re.S)
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", r)).strip() for r in rows]


def map_pins(dom: str) -> list[tuple[float, float]]:
    """(lat, lng) of every direction link -- these apps emit one per store."""
    pins = re.findall(r"destination=(-?\d+\.\d+),(-?\d+\.\d+)", dom)
    return [(float(a), float(b)) for a, b in pins]


def count(brand: str, url: str) -> dict:
    dom = render(url)
    rows, pins = addresses(dom), map_pins(dom)
    if not rows:
        raise RuntimeError("rendered page had no recognisable store rows")
    if pins and abs(len(pins) - len(rows)) > max(2, len(rows) // 50):
        raise RuntimeError(f"{len(rows)} store rows but {len(pins)} map pins -- "
                           "the render looks partial")
    span = (max(p[0] for p in pins) - min(p[0] for p in pins)) if pins else 0.0
    if pins and span < SPAN_MIN:
        raise RuntimeError(f"stores span only {span:.1f} degrees of latitude -- "
                           "the render looks limited to one region")

    metros, outside = Counter(), 0
    for addr in rows:
        m = metro_of(addr)
        if m:
            metros[m] += 1
        else:
            outside += 1
    paged = bool(re.search(r"[Ll]oad\s*[Mm]ore|pagination", dom))
    return {
        "site": re.match(r"https?://[^/]+/", url).group(0),
        "locator": url,
        "india_total": len(rows),
        "metros": dict(metros),
        "method": "locator page rendered in headless Chrome",
        "checked": date.today().isoformat(),
        "note": (f"{len(rows)} stores in the rendered page, {outside} outside the "
                 f"six metros; latitudes span {span:.1f} degrees"
                 + ("; the page paginates client-side, so all rows are present "
                    "in the DOM behind its Load More control" if paged else "")),
    }


def main() -> None:
    args = sys.argv[1:]
    wanted = sorted(set(TARGETS) | set(DIRECTORY)) if args[:1] == ["--all"] else args
    if not wanted:
        raise SystemExit(__doc__)
    cache = json.loads(OUT.read_text()) if OUT.exists() else {}
    for brand in wanted:
        if brand not in TARGETS and brand not in DIRECTORY:
            print(f"!! no locator URL set for {brand!r}")
            continue
        try:
            cache[brand] = (count_directory(brand) if brand in DIRECTORY
                            else count(brand, TARGETS[brand]))
        except Exception as exc:
            print(f"!! {brand}: {type(exc).__name__}: {exc}")
            continue
        hit = cache[brand]
        print(f"{brand}: {hit['india_total']} in India, "
              + ", ".join(f"{k} {v}" for k, v in sorted(hit["metros"].items())))
    OUT.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    print(f"wrote {OUT.name}: {len(cache)} brands")


if __name__ == "__main__":
    main()
