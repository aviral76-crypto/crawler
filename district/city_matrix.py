#!/usr/bin/env python3
"""
Build a locality x brand store-count matrix for one city.

    python3 city_matrix.py --city Gurgaon --categories fashion
    python3 city_matrix.py --city "New Delhi" --categories apparel

Rows are localities, columns are brands, each cell is how many stores of that
brand sit in that locality. Localities and brands are both ordered by total
store count, so the dense corner is top-left.

Sheets produced:
    Locality x Brand   the matrix, colour-scaled, with row and column totals
    Localities         per-locality totals: stores, distinct brands, share of city
    Brands             per-brand totals: stores, localities, where
    Stores             the flat store list the matrix is built from
    Method             sources and caveats

Reads data/store_details.json -- run `python3 crawl.py sitemap` then
`python3 crawl.py enrich` first.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from math import cos, radians
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from district_api import dedupe_stores, norm_city, norm_locality, resolve_subcity

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

CATEGORY_SETS: dict[str, set[str] | None] = {
    "apparel": {"apparel"},
    "fashion": {"apparel", "footwear", "bags", "luggage", "accessories",
                "jewellery", "watches", "eyewear"},
    # Sportswear is tagged "Sports & Outdoors". Most such stores also carry a
    # fashion tag, but a few (Decathlon, Columbia Sportswear) carry only this
    # one and are invisible under "fashion".
    "fashion+sports": {"apparel", "footwear", "bags", "luggage", "accessories",
                       "jewellery", "watches", "eyewear", "sports & outdoors"},
    "all": None,
}

#: The brands the coverage audit flagged -- untagged by District, delisted by
#: it, or listed far more thinly than an independent floor suggests. None of
#: them survive a plain --categories run, so they are named explicitly.
#:
#: The sector per brand is District's own tag where it has one; for the brands
#: District leaves untagged it is stated here, because there is no data to
#: infer it from. --categories then filters this set like any other.
GAP_BRANDS: dict[str, set[str]] = {
    # District tags these itself
    "Fabindia": {"apparel"},
    "Raymond": {"apparel"},
    "Manyavar": {"apparel", "accessories"},
    "Cobb": {"apparel", "footwear"},
    "Duke": {"apparel", "footwear"},
    "Ibadat": {"apparel"},
    "Shree": {"apparel"},
    "Redtape": {"footwear"},
    "Relaxo": {"footwear"},
    "Kalyan Jewellers": {"jewellery"},
    "Malabar Gold": {"jewellery"},
    "Ethos": {"watches", "jewellery"},
    # Tagged only "Sports & Outdoors", so they need --categories fashion+sports
    "Decathlon": {"sports & outdoors"},
    "Columbia Sportswear": {"sports & outdoors"},
    # Untagged by District. Both are menswear apparel labels; stated, not inferred.
    "Louis Philippe": {"apparel"},
    "Peter England": {"apparel"},
    # Untagged by District and not fashion at all -- reachable via --categories all
    "MR.DIY": set(),                  # household variety store
    "FNP": set(),                     # flowers and gifting
    "Great Eastern Retail": set(),    # consumer electronics
    "Sony": set(),                    # consumer electronics
}


def gap_brands_for(allowed: set[str] | None) -> list[str]:
    """The gap brands whose sector matches the active category filter."""
    if allowed is None:
        return list(GAP_BRANDS)
    return [b for b, sectors in GAP_BRANDS.items() if sectors & allowed]


#: Metros District splits across several `seo_city` values. Asking for the metro
#: pulls all of them and keeps the District name in a `sub_city` column; asking
#: for a member name on its own still works.
CITY_GROUPS: dict[str, tuple[str, ...]] = {
    "delhi ncr": ("new delhi", "gurgaon", "noida", "greater noida", "ghaziabad",
                  "faridabad", "delhi ncr"),
    # Gurgaon has its own workbooks, so the rest of the region gets one too.
    "delhi ncr ex gurgaon": ("new delhi", "noida", "greater noida", "ghaziabad",
                             "faridabad", "delhi ncr"),
    "mumbai": ("mumbai", "navi mumbai", "thane"),
    "hyderabad": ("hyderabad", "secunderabad"),
    "pune": ("pune", "pimpri chinchwad"),
}


HEADER_FILL = PatternFill("solid", fgColor="1F2937")
HEADER_FONT = Font(color="FFFFFF", bold=True)
TOTAL_FILL = PatternFill("solid", fgColor="E5E7EB")
BOLD = Font(bold=True)


def store_tags(store: dict[str, Any]) -> set[str]:
    """Category tags of a store, lower-cased.

    `categories` arrives as "Apparel • Accessories"; some rows use "|" instead,
    so both separators are split on.
    """
    raw = (store.get("categories") or "").replace("•", "|")
    return {t.strip().lower() for t in raw.split("|") if t.strip()}


#: Why a store can lack a gender: District only exposes the facet on its hub
#: store directories, and not every brand is listed in a hub or tagged there.
GENDER_NO_TAG = "No tag"
GENDER_UNKNOWN = "Unknown"


def load_brand_gender() -> dict[str, dict[str, Any]]:
    path = DATA / "brand_gender.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def gender_of(store: dict[str, Any], brand_gender: dict[str, dict[str, Any]]) -> str:
    """District's audience tag for a store's brand.

    The tag is a brand attribute, not a store one -- brands observed in more
    than one hub carry an identical tag every time -- so it is looked up by
    brand name. Two distinct kinds of gap are kept apart: a brand District
    lists in a hub without any gender facet ("No tag"), and a brand that
    appears in no hub at all, where we simply have no information ("Unknown").
    """
    if not brand_gender:
        return GENDER_UNKNOWN
    entry = brand_gender.get((store.get("brand") or "").strip().lower())
    if entry is None:
        return GENDER_UNKNOWN
    label = entry.get("gender") or "Unspecified"
    return GENDER_NO_TAG if label == "Unspecified" else label


def matched_brand(store: dict[str, Any], wanted: list[str] | None) -> str | None:
    """Which query term a store's brand matched, longest term first.

    District spells one brand several ways -- "The Raymond Shop",
    "Raymond - Ready to wear", "Ethnix By Raymond" -- so results are rolled up
    to the term that was asked for, with the District spellings preserved on
    the Brands sheet.
    """
    if wanted is None:
        return None
    name = (store.get("brand") or "").lower()
    for term in sorted(wanted, key=len, reverse=True):
        if term.lower() in name:
            return term
    return None


def brand_selected(store: dict[str, Any], wanted: list[str] | None) -> bool:
    """Match a store against an explicit brand list, substring and case loose.

    District spells one brand several ways ("The Raymond Shop",
    "Raymond - Ready to wear", "Ethnix By Raymond"), so a substring match on the
    query collects the whole family rather than needing every variant spelled
    out.
    """
    if wanted is None:
        return True
    name = (store.get("brand") or "").lower()
    return any(w.lower() in name for w in wanted)


#: District normally writes a mall store's locality as "<mall>, <locality>", but
#: for some malls it names only the mall ("Phoenix Marketcity Shopping Mall"),
#: which would stand as a locality of its own and leave the real one reading
#: thin -- 295 of 2,963 Mumbai stores, concentrated in five malls.
MALL_WORDS = {"mall", "centre", "center", "plaza", "arcade", "galleria"}

#: How far a mall may sit from the stores whose locality is used to place it.
FOLD_KM = 1.5

#: Close enough that a single store settles it, without a vote.
NEIGHBOUR_KM = 0.4

#: How far apart two stores of the same mall name can be and still be one mall.
MALL_SPAN_KM = 1.0

LOCALITY_FROM_DISTRICT = "District (seo_locality)"


def is_mall_only(raw: str | None) -> bool:
    """True if District's locality label is a bare mall name, with no locality."""
    if not raw or "," in raw:
        return False
    # Token-wise, or "Malleshwaram" and "Mallapur" read as malls.
    return bool(MALL_WORDS & {w.strip(".").lower() for w in raw.split()})


def km_between(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Flat-earth distance. Fine at city scale, and no trig beyond one cosine."""
    dlat = (a["lat"] - b["lat"]) * 111.32
    dlng = (a["lng"] - b["lng"]) * 111.32 * cos(radians(a["lat"]))
    return (dlat * dlat + dlng * dlng) ** 0.5


def mall_clusters(stores: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Mall-only stores grouped into one cluster per physical mall.

    Grouped by the mall name District gives, then split by distance, since a
    chain of malls shares a name across a metro (Infinity Mall is in both
    Andheri and Malad) while one mall's stores sit within a few hundred metres.
    """
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for store in stores:
        if store["mall_only"] and store.get("lat") is not None and store.get("lng") is not None:
            by_name[(store.get("locality") or "").strip().lower()].append(store)

    clusters: list[list[dict[str, Any]]] = []
    for members in by_name.values():
        # ponytail: greedy single pass, so grouping depends on store order. Fine
        # at this scale (one mall's stores are all within a block); use proper
        # single-linkage if a mall ever straddles the threshold.
        groups: list[list[dict[str, Any]]] = []
        for store in members:
            for group in groups:
                if km_between(store, group[0]) <= MALL_SPAN_KM:
                    group.append(store)
                    break
            else:
                groups.append([store])
        clusters.extend(groups)
    return clusters


def fold_mall_localities(stores: list[dict[str, Any]]) -> int:
    """Place mall-only stores in the locality their neighbours are in.

    The alternative -- a mall standing as its own locality -- splits one place
    into two rows. Inferred from District's own coordinates: the locality of a
    store within NEIGHBOUR_KM if there is one, else the majority locality among
    the stores within FOLD_KM. A mall with no such neighbour keeps its own name
    rather than being guessed at.

    The vote is taken once per mall, not per store, so a mall lands in exactly
    one locality -- per-store coordinates vary enough inside a big complex to
    otherwise split it (DLF Summit Plaza across Golf Course Road and DLF Phase
    5). The original label stays in locality_raw and the Stores sheet's
    locality_source says which rows were inferred.
    """
    anchors = [s for s in stores
               if not s["mall_only"] and s.get("lat") is not None and s.get("lng") is not None]
    if not anchors:
        return 0

    folded = 0
    for cluster in mall_clusters(stores):
        anchor = cluster[0]
        by_distance = sorted(((km_between(anchor, a), a) for a in anchors),
                             key=lambda pair: pair[0])
        nearest_km, nearest = by_distance[0]
        # A store a few hundred metres away settles it, whatever the wider
        # neighbourhood votes -- which matters where a neighbouring locality's
        # high street is denser than the mall's own (Sky City Mall, Borivali).
        if nearest_km <= NEIGHBOUR_KM:
            locality = nearest["locality_norm"]
            source = f"inferred from a store {nearest_km * 1000:.0f} m away"
        else:
            near = Counter(a["locality_norm"] for km, a in by_distance if km <= FOLD_KM)
            if not near:
                continue
            locality = near.most_common(1)[0][0]
            source = f"inferred from {sum(near.values())} stores within {FOLD_KM} km"
        for store in cluster:
            store["locality_norm"] = locality
            store["locality_source"] = source
        folded += len(cluster)
    return folded


def load_city_stores(city: str, allowed: set[str] | None,
                     brands: list[str] | None = None) -> list[dict[str, Any]]:
    path = DATA / "store_details.json"
    if not path.exists():
        raise SystemExit("data/store_details.json not found -- run `python3 crawl.py sitemap` "
                         "then `python3 crawl.py enrich` first.")
    rows = [dict(v, slug=k) for k, v in json.loads(path.read_text()).items()]

    # Stores District labels only "Delhi NCR" get their real sub-city back.
    subcity = resolve_subcity(rows)
    for row in rows:
        if id(row) in subcity:
            row["city"] = subcity[id(row)].title()

    target = norm_city(city)
    members = CITY_GROUPS.get(target, (target,))
    in_city = [r for r in rows if norm_city(r.get("city")) in members]
    if not in_city:
        names = sorted({norm_city(r.get("city")).title() for r in rows if r.get("city")})
        raise SystemExit(f"No stores for city {city!r}. Available: {', '.join(names)}")

    # District sometimes publishes two detail pages for one physical store.
    deduped, duplicates, ambiguous = dedupe_stores(in_city)
    for row in deduped:
        row["locality_norm"] = norm_locality(row.get("locality"))
        row["sub_city"] = (row.get("city") or "").strip()
        row["mall_only"] = is_mall_only(row.get("locality"))
        row["locality_source"] = LOCALITY_FROM_DISTRICT
    folded = fold_mall_localities(deduped)
    mall_rows = sum(1 for r in deduped if r["mall_only"])
    if mall_rows:
        print(f"  {mall_rows} stores give a mall name and no locality; {folded} placed "
              f"by their neighbours' locality, {mall_rows - folded} left as the mall")

    # Two sub-cities of one metro can use the same locality name (Navi Mumbai
    # and Thane both have a Sector 15), which would silently merge two places
    # into one row. Qualify only the names that actually collide.
    sub_cities: dict[str, set[str]] = defaultdict(set)
    for row in deduped:
        sub_cities[row["locality_norm"]].add(row["sub_city"])
    for row in deduped:
        if len(sub_cities[row["locality_norm"]]) > 1:
            row["locality_norm"] = f"{row['locality_norm']}, {row['sub_city']}"

    brand_gender = load_brand_gender()
    for row in deduped:
        row["gender"] = gender_of(row, brand_gender)

    for row in deduped:
        row["status"] = ("delisted by District"
                         if "DELETED" in (row.get("brand") or "").upper() else "")

    # District flags withdrawn listings in the brand name itself; they are not
    # trading stores, so drop them -- but say how many, never silently. When an
    # explicit brand list is given they are kept and flagged instead, since a
    # delisting is exactly what you want to see in a gap report.
    delisted = [r for r in deduped if r["status"]]
    if brands is None:
        dropped_ids = {id(r) for r in delisted}
        deduped = [r for r in deduped if id(r) not in dropped_ids]

    untagged = [r for r in deduped if not (r.get("categories") or "").strip()]
    if brands is not None:
        # An explicit brand list replaces the category filter -- most of these
        # brands are untagged, which is why they needed naming in the first place.
        kept = [r for r in deduped if brand_selected(r, brands)]
        for row in kept:
            row["brand_variant"] = row.get("brand")
            row["brand"] = matched_brand(row, brands) or row.get("brand")
    else:
        kept = [r for r in deduped if allowed is None or (store_tags(r) & allowed)]
    in_scope = {id(r) for r in kept}
    ambiguous = [g for g in ambiguous if any(id(r) in in_scope for r in g)]
    print(f"{city}: {len(deduped)} stores on District "
          f"({duplicates} duplicate listings merged), {len(kept)} match the filter")
    if delisted:
        verb = "flagged" if brands is not None else "excluded"
        print(f"  {len(delisted)} {verb}: District marks the brand '- DELETED'")
    if untagged and allowed is not None and brands is None:
        print(f"  {len(untagged)} dropped: District gives them no category tag at all "
              f"-- see coverage_gaps.py")
    if ambiguous:
        print(f"  {len(ambiguous)} brand+location groups need a human eye "
              f"-- see the 'Check duplicates' sheet")
    return kept, ambiguous


def style_header_row(ws, ncols: int, row: int = 1) -> None:
    for col in range(1, ncols + 1):
        cell = ws.cell(row=row, column=col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="bottom", wrap_text=False)


def autosize(ws, max_width: int = 46) -> None:
    for col in ws.columns:
        letter = get_column_letter(col[0].column)
        longest = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[letter].width = min(max(10, longest + 2), max_width)


def add_table(wb: Workbook, title: str, columns: list[str], rows: list[list[Any]]):
    ws = wb.create_sheet(title[:31])
    ws.append(columns)
    for row in rows:
        ws.append(row)
    style_header_row(ws, len(columns))
    ws.freeze_panes = "A2"
    autosize(ws)
    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{len(rows) + 1}"
    return ws


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", default="Gurgaon")
    ap.add_argument("--categories", choices=sorted(CATEGORY_SETS), default="fashion")
    ap.add_argument("--out", default=None)
    ap.add_argument("--zeros", action="store_true",
                    help="write 0 in empty cells instead of leaving them blank")
    ap.add_argument("--brands", default=None,
                    help="comma-separated brand list; replaces the category filter and "
                         "keeps District's delisted entries, flagged")
    ap.add_argument("--gap-brands", action="store_true",
                    help="use the coverage-audit brand set (see GAP_BRANDS)")
    ap.add_argument("--label", default=None, help="filename label for the output workbook")
    args = ap.parse_args()

    label = args.categories
    allowed = CATEGORY_SETS[label]

    brands = None
    if args.gap_brands:
        brands = gap_brands_for(allowed)
        if not brands:
            raise SystemExit(f"No gap brands are in the {args.categories!r} category.")
    elif args.brands:
        brands = [b.strip() for b in args.brands.split(",") if b.strip()]
    stores, ambiguous = load_city_stores(args.city, allowed, brands)
    if not stores:
        raise SystemExit(f"No matching stores in {args.city}.")
    if brands is not None:
        label = args.label or (f"gap_brands_{args.categories}" if args.gap_brands
                               else "selected_brands")
        missing = [b for b in brands
                   if not any(b.lower() in (s.get("brand") or "").lower() for s in stores)]
        if missing:
            print(f"  no {args.city} stores for: {', '.join(missing)}")

    brand_gender_label: dict[str, str] = {}
    gender_by_locality: dict[str, Counter] = defaultdict(Counter)

    counts: dict[str, Counter] = defaultdict(Counter)       # locality -> brand -> n
    brand_localities: dict[str, set[str]] = defaultdict(set)
    locality_total: Counter = Counter()
    brand_total: Counter = Counter()

    for store in stores:
        locality = store["locality_norm"] or "unknown"
        brand = (store.get("brand") or "unknown").strip()
        counts[locality][brand] += 1
        brand_localities[brand].add(locality)
        locality_total[locality] += 1
        brand_total[brand] += 1
        brand_gender_label.setdefault(brand, store["gender"])
        gender_by_locality[locality][store["gender"]] += 1

    # Densest first on both axes, so the populated corner is top-left.
    localities = [name for name, _ in locality_total.most_common()]
    brands = [name for name, _ in sorted(brand_total.items(), key=lambda kv: (-kv[1], kv[0]))]

    wb = Workbook()
    wb.remove(wb.active)

    # ------------------------------------------------------- Locality x Brand
    ws = wb.create_sheet("Locality x Brand")
    ws.append(["locality", "total_stores", "distinct_brands", *brands])
    # Audience band: gender is a property of the brand, so it belongs to the
    # column rather than to any one cell.
    ws.append(["caters to \u2192", "", "",
               *(brand_gender_label.get(b, GENDER_UNKNOWN) for b in brands)])
    for locality in localities:
        row = counts[locality]
        ws.append([
            locality, locality_total[locality], len(row),
            *((row.get(b) or (0 if args.zeros else None)) for b in brands),
        ])
    # Column totals as a footer row.
    ws.append(["TOTAL", sum(locality_total.values()), len(brands),
               *(brand_total[b] for b in brands)])

    style_header_row(ws, 3 + len(brands))
    for cell in ws[2]:
        cell.font = Font(italic=True, size=9)
        cell.alignment = Alignment(textRotation=90, vertical="bottom")
    ws[2][0].alignment = Alignment(horizontal="right")
    ws.row_dimensions[2].height = 56
    ws.freeze_panes = "D3"
    for col in range(1, 4):
        ws.column_dimensions[get_column_letter(col)].width = (34, 13, 16)[col - 1]
    for col in range(4, 4 + len(brands)):
        ws.column_dimensions[get_column_letter(col)].width = 4.5
        ws.cell(row=1, column=col).alignment = Alignment(textRotation=90, vertical="bottom")
    ws.row_dimensions[1].height = 128
    last_row = ws.max_row
    for cell in ws[last_row]:
        cell.font = BOLD
        cell.fill = TOTAL_FILL
    for cell in (ws.cell(row=r, column=2) for r in range(2, last_row + 1)):
        cell.font = BOLD
    # Colour scale over the matrix body only, so density is visible at a glance.
    body = (f"D3:{get_column_letter(3 + len(brands))}{last_row - 1}")
    ws.conditional_formatting.add(
        body,
        ColorScaleRule(start_type="num", start_value=1, start_color="FFFFFF",
                       mid_type="num", mid_value=2, mid_color="BFDBFE",
                       end_type="max", end_color="1D4ED8"),
    )
    ws.auto_filter.ref = f"A2:C{last_row - 1}"

    # ------------------------------------------------------------ Localities
    city_total = sum(locality_total.values())
    gender_cols = ["Unisex", "Women", "Men", "Kids", GENDER_NO_TAG, GENDER_UNKNOWN]
    add_table(wb, "Localities",
              ["rank", "locality", f"{label}_stores", "distinct_brands",
               *gender_cols, "share_of_city", "top_brands"],
              [[i + 1, name, locality_total[name], len(counts[name]),
                *(gender_by_locality[name].get(g) or "" for g in gender_cols),
                round(locality_total[name] / city_total, 4),
                "; ".join(b for b, _ in counts[name].most_common(10))]
               for i, name in enumerate(localities)])

    # ---------------------------------------------------------------- Brands
    variants: dict[str, Counter] = defaultdict(Counter)
    for st in stores:
        if st.get("brand_variant"):
            variants[st["brand"]][st["brand_variant"]] += 1
    add_table(wb, "Brands",
              ["rank", "brand", "caters_to", "stores", "localities",
               "district_spellings", "locality_list"],
              [[i + 1, b, brand_gender_label.get(b, GENDER_UNKNOWN), brand_total[b],
                len(brand_localities[b]),
                "; ".join(f"{k} ({v})" for k, v in variants[b].most_common()),
                "; ".join(sorted(brand_localities[b]))]
               for i, b in enumerate(brands)])

    # ---------------------------------------------------------------- Stores
    add_table(wb, "Stores",
              ["brand", "district_spelling", "caters_to", "locality", "sub_city",
               "locality_source", "status", "vertical", "categories", "locality_raw",
               "street_address", "pincode", "lat", "lng", "url"],
              [[s.get("brand"), s.get("brand_variant") or s.get("brand"),
                s.get("gender"), s["locality_norm"], s.get("sub_city"),
                s.get("locality_source"), s.get("status"),
                s.get("vertical"), s.get("categories"), s.get("locality"),
                s.get("street_address"), s.get("postal_code"), s.get("lat"),
                s.get("lng"), s.get("url")]
               for s in sorted(stores, key=lambda x: (x["locality_norm"], x.get("brand") or ""))])

    # ----------------------------------------------------- Check duplicates
    if ambiguous:
        rows = []
        for n, group in enumerate(ambiguous, 1):
            for store in group:
                rows.append([n, store.get("brand"), store["locality_norm"],
                             store.get("street_address"), store.get("lat"),
                             store.get("lng"), store.get("url")])
        ws = add_table(wb, "Check duplicates",
                       ["group", "brand", "locality", "street_address", "lat", "lng", "url"],
                       rows)
        ws.insert_rows(1)
        ws["A1"] = ("Same brand at identical coordinates but a different address. Usually two "
                    "real outlets in one mall (different shop number or floor), occasionally one "
                    "store listed twice with a retyped address. Counted as separate stores in "
                    "the matrix -- delete a row here and re-run if you disagree.")
        ws["A1"].font = Font(italic=True)
        ws.freeze_panes = "A3"

    # ---------------------------------------------------------------- Method
    ws = wb.create_sheet("Method")
    tag_list = ", ".join(sorted(allowed)) if allowed else "no filter"
    tally = Counter(st["gender"] for st in stores)
    sub_tally = sorted({st.get("sub_city") for st in stores if st.get("sub_city")})
    mall_only = sum(1 for st in stores if st.get("mall_only"))
    mall_folded = sum(1 for st in stores
                      if st.get("mall_only") and st.get("locality_source") != LOCALITY_FROM_DISTRICT)
    resolved = sum(v for k, v in tally.items() if k not in (GENDER_NO_TAG, GENDER_UNKNOWN))
    gender_note = (
        f"{resolved} of {len(stores)} stores ({resolved / len(stores):.0%}) carry a District "
        f"gender tag. {tally.get(GENDER_NO_TAG, 0)} are listed by District with no gender facet, "
        f"and {tally.get(GENDER_UNKNOWN, 0)} belong to brands that appear in no hub, so their "
        f"audience is genuinely unknown. District only exposes the facet on hub store "
        f"directories; it is absent from store detail pages entirely."
    )
    for line in [
        [f"{args.city} — {label} store density by locality"],
        [],
        ["Reading it", "Rows are localities, columns are brands, each cell is the number of "
                       "stores of that brand in that locality. Blank means none. Both axes are "
                       "sorted by total store count, so the dense corner is top-left. "
                       "Row 1 is rotated; the TOTAL row at the bottom gives each brand's "
                       "city-wide count."],
        ["Scope", f"{len(stores)} stores · {len(localities)} localities · {len(brands)} brands"],
        ["caters_to", "District's own audience facet, not an inference from brand names. It is a "
                      "property of the BRAND rather than the individual store: every brand seen "
                      "in more than one hub carries an identical tag (verified on 99 of 99 such "
                      "brands), so it is looked up by brand name. Unisex = tagged both Men and "
                      "Women. It appears as a band under the brand headers on the matrix, and as "
                      "a column on the Brands and Stores sheets."],
        ["Its coverage", gender_note],
        ["No tag vs Unknown", "'No tag' means District lists the brand in a hub but applies no "
                              "gender facet to it (Caratlane, Giva, Joyalukkas, Mango, Miniso, "
                              "Mufti). 'Unknown' means the brand appears in no hub at all, so "
                              "District never exposes a tag for it. Neither is guessed — the "
                              "Localities sheet counts both separately so you can see the gap."],
        ["Filter", (f"explicit brand list ({len(brands)} brands): "
                    + ", ".join(brands)) if brands is not None
                   else f"{label} = stores tagged {tag_list}"],
        *([["Why a brand list", "These brands do not survive a category filter: District leaves "
                                "most of them untagged, tags Decathlon and Columbia Sportswear "
                                "only 'Sports & Outdoors', and marks Louis Philippe's outlets "
                                "'- DELETED'. Delisted rows are kept here and flagged in the "
                                "status column rather than dropped."]] if brands is not None else []),
        [],
        ["Source", "District (Zomato Edition) store detail pages, via "
                   "POST https://www.district.in/gw/web/consumer/shopping/store/details"],
        ["Enumeration", "District's own store sitemap (cdn.district.in/sitemap/"
                        "sitemap-store-detail-pages.xml.gz) — every store page it publishes. "
                        "A census of District's listings, not a sample."],
        ["Locality", "District states each store's locality (seo_locality). For a mall store it "
                     "reads '<mall>, <locality>', so the mall name is dropped and the locality "
                     "kept; locality_raw on the Stores sheet preserves the original."],
        ["Dedupe", "Stores with two District detail pages are collapsed on brand + coordinates "
                   "+ address, after normalising whitespace, case and punctuation in the "
                   "address. Same brand at the same coordinates with a genuinely different "
                   "address (another shop number or floor) is kept as a separate store and "
                   "listed on the 'Check duplicates' sheet."],
        [],
        ["Caveat", "These are stores District lists. A brand trading in a locality without a "
                   "District listing will not appear, so sparse localities may reflect "
                   "District's onboarding rather than the actual high street."],
        ["Caveat", "Gurgaon and Gurugram are merged; District uses both spellings."],
        *([["Sub-cities", "District splits this metro across several city labels — "
                          + ", ".join(sub_tally) + " — all included here and kept in the "
                          "sub_city column. A locality name used by more than one of them "
                          "is qualified with its sub-city so the two are not merged."]]
          if len(sub_tally) > 1 else []),
        *([["Mall-only labels", f"{mall_only} stores carry a bare mall name instead of "
                                "'<mall>, <locality>' (District writes 'Phoenix Marketcity "
                                f"Shopping Mall' and nothing else). {mall_folded} of them are "
                                "placed in the locality that the majority of District's own "
                                f"stores within {FOLD_KM} km sit in, so the mall does not "
                                f"become a locality of its own; {mall_only - mall_folded} had "
                                "no such neighbour and keep the mall name. The locality_source "
                                "column on Stores says which is which, and locality_raw keeps "
                                "District's original label."]] if mall_only else []),
    ]:
        ws.append(line)
    ws["A1"].font = Font(bold=True, size=13)
    for row in ws.iter_rows(min_row=3, max_row=ws.max_row):
        row[0].font = BOLD
        if len(row) > 1 and row[1].value:
            row[1].alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 15
    ws.column_dimensions["B"].width = 100

    out = Path(args.out) if args.out else DATA / (
        f"{args.city.replace(' ', '_')}_{label}_locality_x_brand.xlsx"
    )
    wb.save(out)
    print(f"wrote {out}")
    print(f"  matrix: {len(localities)} localities x {len(brands)} brands, "
          f"{sum(locality_total.values())} stores")
    print("  caters to: " + ", ".join(f"{k} {v}" for k, v in tally.most_common()))
    print(f"  densest: " + ", ".join(
        f"{name} ({locality_total[name]})" for name in localities[:5]))


if __name__ == "__main__":
    main()
