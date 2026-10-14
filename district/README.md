# District — apparel brand density by locality

Pulls District's (Zomato Edition) shopping catalogue and answers: **which apparel
brands are dense in which localities.**

Current run: **14,821 store pages** resolved across six metros — a **complete
census** of what District publishes in each.

| Metro | District cities | stores | fashion | apparel | localities | brands |
|---|---|---|---|---|---|---|
| Delhi NCR | New Delhi, Gurgaon, Noida, Greater Noida, Ghaziabad, Faridabad | 5,441 | 3,723 | 2,192 | 248 | 595 |
| Bengaluru | Bangalore | 3,140 | 2,109 | 1,220 | 89 | 320 |
| Mumbai | Mumbai, Navi Mumbai, Thane | 2,963 | 2,072 | 1,120 | 111 | 316 |
| Hyderabad | Hyderabad, Secunderabad | 1,661 | 1,209 | 687 | 93 | 152 |
| Pune | Pune | 1,065 | 826 | 422 | 74 | 139 |

Store counts are after merging District's duplicate listings; `localities` and
`brands` are the dimensions of the `fashion` matrix.

```bash
python3 crawl.py sitemap --cities hyderabad,bangalore,pune,mumbai
python3 crawl.py enrich --categories fashion

python3 city_matrix.py --city Hyderabad --categories fashion   # locality x brand
python3 to_excel.py --locality "Kondapur" --city Hyderabad     # one locality
```

Delhi NCR additionally runs the `hubs`, `stores` and `gender` stages — see
*Outside Delhi NCR* for why the other metros cannot.

Each stage is separately runnable (`hubs`, `sitemap`, `stores`, `enrich`), and the store
outputs are named after the active filter — `stores_apparel_enriched.csv`,
`stores_fashion_enriched.csv` — so widening the filter never overwrites a
narrower run.

Everything is cached under `data/`, so re-running only fetches what changed.
No dependencies beyond the standard library.

---

## The API

District's app and website talk to the same gateway. No login is needed for the
read endpoints — the web client ships a constant guest token, so these headers
are enough:

```
POST https://www.district.in/gw/<path>
x-guest-token: 1212          x-client-id: district-web
x-app-type: ed_web           x-app-version: 11.11.1
x-device-id: <any>           x-city-id / x-user-lat / x-user-lng
```

| Endpoint | What it gives |
|---|---|
| `/web/pre_home` | lat/lng → city, sub-city, locality, Zomato subzone id |
| `/web/get_discovery_results` | `layout_type: "shopping_home"` → `hub_rail`: the ~7–14 nearest malls/markets, each with a store count |
| `/web/hub_page` | `layout_type: "hub_page"` → hub address, coordinates, category counts.<br>`layout_type: "hub_categories_shops"` → **the hub's full store directory**, 10 per page via `pagination.postbackParams` |
| `/web/consumer/shopping/store/details` | `{"slug": "..."}` → brand, categories, `seo_locality`, street address, exact coordinates |

Plus the piece that makes a census possible rather than a sample:

| Resource | What it gives |
|---|---|
| `cdn.district.in/sitemap/sitemap-stores.xml` → `sitemap-store-detail-pages.xml.gz` | **every store page District publishes** — 25,432 nationwide, 5,637 in Delhi NCR. Not linked from `robots.txt`, which only advertises the movies/events/dining/activities/play sitemaps. |

Two details worth knowing, since nothing documents them:

- **Hub store pagination.** The first call returns the section plus
  `pagination.postbackParams`; each next page is the same call with
  `request_type: "load_more"` and that string echoed back. `hasMore` goes false
  at the end. Ten stores per page, so a 148-store mall is 15 calls.
- **Locality.** `seo_locality` on a store inside a mall reads
  `"Ambience Mall, DLF Phase 3"` — hub name *plus* locality. The hub page's own
  `addressText` gives the bare locality (`"DLF Phase 3"`, `"Sector 68"`), so the
  crawler prefers that for hub stores and falls back to `seo_locality` for
  standalone ones. Both are kept (`locality`, `locality_raw`).

## Pipeline

**1. `crawl.py hubs`** — the hub rail only returns the nearest handful of hubs
per call, so coverage comes from sweeping a lat/lng grid: 341 points over Delhi
NCR (5 km spacing) plus a denser Gurgaon pass (2.8 km). It converges at
**25 hubs** — the count stops moving around point 125 of 341, which is the
signal that District's NCR hub list is exhausted, not that the grid is too
coarse. Each hub is then fetched for its address, coordinates and counts.

**2. `crawl.py sitemap`** — pulls District's gzipped store sitemap and scopes it
by the city token in each slug: 5,637 slugs for NCR, 14,820 with the four other
metros added (`--cities`, see *Outside Delhi NCR*). This is what covers
standalone high-street stores, which no hub lists.

**3. `crawl.py stores`** — pages through each hub's directory. 1,371 stores, and
the only source of per-store floor and offer text.

**4a. `crawl.py gender`** — harvests District's audience facet. See
*Gender / audience* below; writes `data/brand_gender.csv`.

**4. `crawl.py enrich`** — resolves every known slug through its detail page for
store-level locality, street address and exact coordinates. Slugs come from the
sitemap, the hub crawl, the public `/stores/` page, and `seeds.txt` if present.
5,667 stores resolved.

## Outside Delhi NCR

The sitemap and the store detail page work everywhere; the hub endpoints mostly
do not. `hub_rail` returns **14 hubs for Bengaluru, 5 for Mumbai and zero for
Hyderabad and Pune** — not a grid that is too coarse, but District serving no
hub rail there at all (`pre_home` resolves those points fine, and returns the
right city id). So outside NCR the pipeline is two stages:

```bash
python3 crawl.py sitemap --cities hyderabad,bangalore,pune,mumbai
python3 crawl.py enrich --categories fashion
```

`--cities` scopes the national sitemap by the city token in each slug
(`go-colors-khopat-thane-west` → `thane`); `CITY_SLUG_TOKENS` holds the sets.
Scoping only decides which pages are worth fetching — a store's real city comes
from `seo_city` on its detail page — so the token lists are deliberately
generous. New slugs merge into `data/sitemap_slugs.json`, so scoping to one
metro never discards another's.

Three consequences of having no hub data:

- **Locality** comes from `seo_locality` alone, with no hub `addressText` to
  cross-check it against. It is good — `"Viviana Mall, Majiwada Thane West"`,
  `"Bhartiya Mall Of Bengaluru, Jakkur"` — and `norm_locality` now also strips a
  trailing zone (`Majiwada`, not `Majiwada Thane West`) and treats
  `"Central Bangalore"`-style compounds as city tokens rather than localities.
- **No floor or offer text**, which only the hub store directory carries.
- **Gender** still resolves for most stores, because the tag is a brand
  attribute and the gender sweep already covered 44 hubs including Bengaluru and
  Mumbai. Coverage of fashion stores is 82% in Hyderabad, 83% in Mumbai, 81% in
  Pune, 79% in Bengaluru. Brands that trade only in Hyderabad or Pune come back
  `Unknown`, since District lists them in no hub anywhere.

**Metros split across several `seo_city` values.** District files Navi Mumbai
and Thane apart from Mumbai, and Secunderabad apart from Hyderabad.
`--city Mumbai` pulls all three and keeps District's own label in a `sub_city`
column; `--city Thane` still works on its own. A locality name used by more than
one sub-city is qualified with it, so two different places never merge into one
row. `CITY_GROUPS` in `city_matrix.py` holds the mapping.

**Malls named without a locality.** District usually writes
`"<mall>, <locality>"`, but for some malls it gives only the mall — 295 of
Mumbai's 2,963 stores, nearly all in five malls (Phoenix Marketcity 83, Sky City
76, Dynamix 49, R Mall 19, Infinity 18). Left alone, each mall becomes a
"locality" and the real one reads thin. They are placed instead by District's own
coordinates: the locality of a store within 400 m if there is one, otherwise the
majority locality among stores within 1.5 km. The vote is taken **once per
mall**, not per store — coordinates vary enough inside a big complex to
otherwise split one mall across two localities (DLF Summit Plaza, 68 stores,
was landing in both Golf Course Road and DLF Phase 5). Malls are identified by
name and then split by distance, since a chain shares a name across a metro.

All 295 Mumbai stores placed (Phoenix Marketcity → Kurla, Sky City → Borivali
East, Dynamix → Juhu, R Mall → Manpada); across NCR 261 of 263 placed and 2 had
no neighbour within range, so they keep the mall name. `locality_source` on the
Stores sheet says which rows were inferred and from what, and `locality_raw`
keeps District's original label, so the inference is auditable rather than
silent.

## Category filter

`--categories apparel` (default) keeps stores tagged **Apparel**. The observed
taxonomy is `"<Vertical> | <Tag> • <Tag>"`, verticals being Fashion, Beauty,
Wellness, Electronics, Home & Living, Gifting, Gourmet Food.

- `--categories apparel` → Apparel only (2,214 stores)
- `--categories fashion` → adds footwear, bags, luggage, accessories, jewellery, watches, eyewear
- `--categories all` → no filter (5,667 stores)

## Outputs (`data/`)

| File | Contents |
|---|---|
| `locality_density.csv` | one row per locality: apparel stores, distinct brands, all-category total, apparel share, hubs, sample brands |
| `brand_footprint.csv` | one row per brand: store count, locality count, which localities |
| `brand_x_locality.csv` | brand × locality matrix — the pivot for density |
| `hub_density.csv` | per mall/market, for comparing hubs within a locality |
| `stores_apparel_enriched.csv` | the store-level table everything is built from (named after the active `--categories` filter) |
| `hubs.csv` | the 25 hubs with locality, subzone id, coordinates, counts |

Raw API responses are kept alongside (`hubs_raw.json`, `hub_stores_raw.json`,
`store_details.json`) so a re-analysis never needs a re-crawl.

## Excel output

Two generators, depending on whether you want a whole city or one locality.

**`city_matrix.py` — locality × brand matrix for a city.** Rows are localities,
columns are brands, each cell is the store count. Both axes sorted by total, so
the dense corner is top-left; the matrix body is colour-scaled.

```bash
python3 city_matrix.py --city Gurgaon --categories fashion     # 51 x 335, 939 stores
python3 city_matrix.py --city Mumbai --categories fashion      # 111 x 316, 2072 stores
python3 city_matrix.py --city Bengaluru --categories apparel
python3 city_matrix.py --city "Delhi NCR" --categories fashion # the whole region
python3 city_matrix.py --city Gurgaon --zeros                  # 0 instead of blank
```

Sheets: **Locality x Brand** (the matrix, with a `caters to` band under the
brand headers and a TOTAL row), **Localities** (totals split Unisex / Women /
Men / Kids / No tag / Unknown), **Brands**, **Stores** (flat list, with
`sub_city`, `locality_source` and District's raw label), **Check duplicates**,
**Method**.

**A named set of brands.** `--brands` / `--gap-brands` replaces the category
filter entirely, which is what these brands need — District leaves most of them
untagged, so no `--categories` value reaches them.

```bash
python3 city_matrix.py --city Gurgaon --gap-brands --categories apparel
python3 city_matrix.py --city Gurgaon --brands "Raymond,Fabindia,Decathlon"
```

`--gap-brands` still honours `--categories`, so the set narrows like any other
run. Each gap brand carries a sector in `GAP_BRANDS` — District's own tag where
it has one, stated explicitly for the brands District leaves untagged, since
there is no data to infer those from. Under `apparel` the set is Raymond,
Fabindia, Louis Philippe, Cobb, Manyavar, Duke, Ibadat, Shree and Peter England;
Decathlon and Columbia Sportswear need `fashion+sports`, and MR.DIY, FNP, Great
Eastern Retail and Sony are not fashion at all so only `all` reaches them.

District spells one brand several ways (`The Raymond Shop`,
`Raymond - Ready to wear`, `Ethnix By Raymond`, …), so stores are rolled up to
the term you asked for; the District spellings stay visible in
`district_spellings` on the Brands sheet and `district_spelling` on Stores.
Delisted entries are **kept and flagged** in a `status` column here rather than
dropped, since a delisting is the point of a gap report. Brands with no store in
the city are named on stdout.

**`to_excel.py` — one locality in depth.**

```bash
python3 to_excel.py --locality "Sector 29" --city Gurgaon
python3 to_excel.py --locality "DLF Phase 3" --city Gurgaon --categories fashion
```

Sheets: **Summary** (headline counts and the locality's rank in its city),
**Stores**, **&lt;City&gt; Localities** (all localities ranked, the queried one
highlighted), **Brand x Locality**, **Method**.

Name a locality that has no stores and it lists the ones that do, so you can
retry with a label District actually uses.

## Gender / audience

District does expose who a store caters to, but only as a **filter facet on hub
store directories** — `post_filters: ['{"FilterType":"gender_filter",...}']` on
`/web/hub_page`. It is absent from store detail pages, and from every non-hub
listing layout tried. So the tag is harvested by listing each hub unfiltered and
then once per gender, and set-differencing.

**It is a brand attribute, not a store one.** Of 99 brands observed in more than
one hub, 99 carried an identical tag — so a brand seen in any hub anywhere gives
its real tag, which is why the sweep covers 44 hubs (25 NCR + 19 elsewhere, which
is all District has) rather than only the city under analysis.

`Unisex` means tagged both Men and Women. Two gaps are kept distinct and neither
is guessed:

| Value | Meaning |
|---|---|
| `No tag` | District lists the brand in a hub but applies no gender facet (Caratlane, Giva, Joyalukkas, Mango, Miniso, Mufti) |
| `Unknown` | the brand appears in no hub at all, so District never exposes a tag |

Coverage for Gurgaon fashion: **761 of 939 stores (81%) carry a real tag** —
475 Unisex, 202 Women, 76 Men, 8 Kids — with 60 `No tag` and 118 `Unknown`. This
is not complete, and localities differ: MG Road has 18 `Unknown` of 114, DLF
Phase 3 only 6 of 135. The Localities sheet counts every state per locality so
the gap is visible rather than buried.

## Data-quality handling

Three things District's own data does that would otherwise distort counts:

- **Mall-prefixed localities.** `seo_locality` is `"<mall>, <locality>"` for
  mall stores and `"<locality>, <city>"` for high-street ones. `norm_locality`
  drops city tokens and keeps the last segment, so `"Ambience Mall, DLF Phase 3"`
  and `"Sector 29, Gurugram"` both reduce correctly. It also strips a city
  buried inside a segment (`"MG Road Gurugram"` → `"MG Road"`), which was
  splitting MG Road into two localities.
- **Duplicate listings.** Some stores have two detail pages. `dedupe_stores`
  collapses them on brand + coordinates + address, after flattening whitespace
  (including NBSP), case and punctuation — 57 in Gurgaon. Same brand at the same
  coordinates with a materially different address is *kept* as a separate store,
  because two outlets in one mall legitimately share coordinates and differ only
  by shop number; those groups go to a **Check duplicates** sheet for a human
  call rather than being merged on a guess (5 groups in Gurgaon).
- **Region instead of sub-city.** ~681 NCR stores carry `seo_city: "Delhi NCR"`
  rather than Noida/Gurgaon/etc. `resolve_subcity` reattributes those whose
  locality appears under exactly one real sub-city elsewhere, and leaves
  genuinely ambiguous ones alone.

## Coverage audit

`coverage_gaps.py` checks the dataset two ways and writes `coverage_gaps.csv`
plus `untagged_stores.csv`.

```bash
python3 coverage_gaps.py                 # audit from cached data
python3 coverage_gaps.py --refresh-osm   # re-query Overpass first
```

**Against District itself** — stores its catalogue carries that a category
filter drops: **453 of 5,472 NCR stores (8.3%) have no category tag at all**,
33 have a brand District flagged `- DELETED`, and 29 are tagged only
`Sports & Outdoors`, which `--categories fashion` does not list. The matrix
script now prints each of these counts instead of dropping them silently, skips
delisted brands, and offers `--categories fashion+sports`.

**Against an outside source** — OpenStreetMap retail POIs in the NCR bounding
box, via Overpass (query and responses cached in `data/osm/`). OSM is sparse: it
knows 25 Lenskart outlets where District lists 159. Its count is therefore a
**floor, never a measurement** — an OSM floor of 10+ proves a brand has 10+
stores, but a low floor proves nothing. Brand names are matched after stripping
noise words, with an explicit alias table for the cases neither prefix nor fuzzy
matching bridges (`W` → `W For Woman`, `Max Fashion` → `Max`).

Result: of 61 brands with an OSM floor of 5+, essentially none with 10+ outlets
is genuinely missing from District. What looked like absences were not:

| Brand | OSM floor | Reality |
|---|---|---|
| Decathlon | 26 | **on District, 22 NCR stores** — tagged only `Sports & Outdoors`, so the fashion filter hid it |
| Louis Philippe | 15–26 | on District as `Louis Philippe- DELETED`, 4 stores, untagged — District delisted it |
| Peter England | 9–17 | on District, 3 stores, untagged — real but very thin coverage |

The more useful finding is brands District lists but **thinly** relative to the
floor: Fabindia 14 vs 28, Raymond 19 vs 27, Manyavar 7 vs 13, Redtape 3 vs 8,
Cobb 1 vs 14, Relaxo 1 vs 6, Duke 1 vs 6, Kalyan Jewellers 4 vs 7.

Brands absent from both District and OSM — Wrangler, Lee, Lee Cooper, Clarks —
cannot be judged from these two sources. They trade mainly through multi-brand
outlets rather than own stores, so a low NCR count is plausible but unverified.

## Cross-check against the brands' own store locators

The coverage audit above measures District against OpenStreetMap, which is only
ever a floor. A sharper check is the brand itself: almost every chain publishes
a store locator, and that locator is the brand's own statement of where it
trades. `Brand Stores - Pan India.xlsx` carries a
`Corrected Store Count-Pan India` sheet built from it.

```bash
python3 brand_store_check.py counts     # read the workbook's own TOTAL rows
python3 brand_locator_find.py           # where each brand's locator lives
python3 brand_locator_fetch.py fetch --all
python3 brand_locator_scan.py scan
python3 brand_locator_render.py --all
python3 brand_store_check.py sheet      # write the corrected sheet
```

**A count only changes when a locator contradicts it.** Where no locator can be
read, District's figure is carried through untouched and the row says so.
Nothing in the sheet is estimated, and every number traces to a locator
response — which is why the fetchers raise on a failed request instead of
swallowing it: a rate-limited sitemap that returned half its children would
undercount a brand and look like a correction.

Three guards exist because each caught a real fault, not a hypothetical one:

- **Two reads must agree.** Some hosts serve a short sitemap under load and
  raise nothing at all: `stores.styleunion.in` returned 198, 266, 278 and 222
  stores on four consecutive reads. `stable_stores` reads until two reads give
  the same count and refuses the brand if four disagree, so no figure is
  published that will not reproduce.
- **A failed re-fetch drops the cached number.** A figure the current run cannot
  reproduce has no business staying in the sheet as a correction.
- **Walk the per-city pages, never the index.** A locator's index renders a
  partial accordion — H&M's showed 8 Bangalore stores where Bangalore's own
  page lists 9 — so `count_directory` fetches each city page and refuses to
  publish if any renders empty.

Two further traps are about geography, not plumbing. `METRO_OF` must mirror
`city_matrix.CITY_GROUPS` exactly and no wider: folding Kalyan or Mira Road into
Mumbai credits the locator with stores the District side structurally cannot
have, faking an undercount (`test_metro.py` asserts the two agree). And a
locator's own map may aggregate differently than you assume — H&M's India map is
**state**-level, so its "15 in Mumbai" is Maharashtra: Mumbai 8 + Pune 5 +
Nagpur 1 + Aurangabad 1.

Four scripts because there is no single way in:

| Script | Handles | Yield |
|---|---|---|
| `brand_locator_fetch.py` | sitemap-backed locators (`stores.<brand>`), one page per store, plus hand-written fetchers for bespoke APIs | most of the coverage |
| `brand_locator_scan.py` | Stockist widgets (`data-stockist-widget-tag` → one JSON call) and other JSON endpoints | a handful |
| `brand_locator_render.py` | locators that only exist after JavaScript, via headless Chrome | a handful |
| `brand_locator_find.py` | finding the locator URL from the brand's own sitemap, rather than guessing paths | feeds the other three |

Each writes its own cache file. They used to share one and silently clobbered
each other's brands when run together.

**The two sources disagree in both directions, and neither is simply right.**
District lists what it has onboarded, including brand counters inside
multi-brand and department stores, so it can far exceed a brand's own list —
Levi's is 302 stores across the six metros on District against 144 on
`stores.levi.in`, U.S. Polo Assn. 208 against 101. Equally, District misses
chains that are expanding faster than it onboards: Pantaloons is 41 against
394 nationally, Manyavar 22 against 297, Go Colors 277 against 302. Both
numbers are kept side by side with a delta, because resolving them needs a
judgement about what counts as a store, not a scraper.

Brands that defeat automation entirely are named in the sheet's Method tab:
Zudio and Westside refuse automated requests at every door tried, and the ABFRL
labels load their stores through a mechanism with no replayable endpoint.

## Coverage, honestly

The store sitemap is a census of what District publishes, so standalone
high-street stores are now included and no locality is structurally
undercounted. Two things still worth knowing:

1. **It is District's coverage, not the market's.** A brand trading in a
   locality without a District listing does not appear. Localities where
   District's retail onboarding is thin will read as sparse — Sector 29 Gurgaon
   has 8 stores listed, which reflects District, not the high street.
2. **The `shopping_home_all_stores` rail** remains unavailable to the public web
   client (it appears in the site's server-rendered HTML with `has_more: true`
   and gender filters, but is not served for any location, header or experiment
   bucket tried). The sitemap makes it unnecessary for enumeration, but the app
   does get it, and it carries gender and category facets the sitemap does not.

## Capturing the app (optional)

No longer needed for enumeration, but the app exposes facets the web client does
not. It hits the same `/gw` gateway with `store_id` instead of `slug`, and does
not pin certificates, so mitmproxy is enough.

```bash
brew install mitmproxy
mitmdump -s capture_district.py        # listens on :8080
```

Point the phone's Wi-Fi proxy at your Mac's IP on port 8080, install the CA from
`http://mitm.it`, and — on Android 7+ — run a rooted device or an emulator image
so the system store is writable, since apps ignore user-added CAs by default. A
Google-APIs emulator image is the least painful route:

```bash
emulator -avd <avd> -writable-system -http-proxy http://<mac-ip>:8080
```

Then scroll the Stores tab and open a few malls. `capture_district.py` writes one
JSON file per District API call into `data/captured/`. The payloads you want:

- `get_discovery_results` with `layout_type: "shopping_home"` — look for the
  `shopping_home_all_stores` rail and its `postback_params`; that unlocks a full
  store listing per location.
- `shopping_store` / `store/details` calls — they carry `store_id`, which maps
  ids to slugs and lets you walk the id space directly.

Drop any slugs you harvest into `seeds.txt` (one per line, or paste URLs — the
crawler regexes them out) and re-run `python3 crawl.py enrich`.

Note the app's responses are protobuf-shaped JSON with numeric field keys
(`{"1": ..., "3": [...]}`) rather than the named fields the web gateway returns —
`districtData.json` in this directory is an example. The field numbers are stable
per message type, so decoding is a matter of mapping them once against the
equivalent web response.
