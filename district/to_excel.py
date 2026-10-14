#!/usr/bin/env python3
"""
Build an Excel workbook for one locality's brand/store density.

    python3 to_excel.py --locality "Sector 29" --city Gurgaon
    python3 to_excel.py --locality "DLF Phase 3" --city Gurgaon --categories all

Sheets produced:
    Summary          headline counts for the locality, and where it ranks in its city
    Stores           every store in the locality, apparel flagged, with address + coords
    City localities  all localities in that city ranked by apparel store count
    Brand x Locality apparel brand (rows) x locality (columns) matrix for the city
    Method           what the numbers mean and where they came from

Reads data/store_details.json, so run `python3 crawl.py sitemap` and
`python3 crawl.py enrich` first.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from district_api import CITY_TOKENS, norm_city, norm_locality, resolve_subcity

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

APPAREL_TAGS = {"apparel"}
FASHION_TAGS = {"apparel", "footwear", "bags", "luggage", "accessories",
                "jewellery", "watches", "eyewear"}

HEADER_FILL = PatternFill("solid", fgColor="1F2937")
HEADER_FONT = Font(color="FFFFFF", bold=True)
ACCENT_FONT = Font(bold=True)


def is_match(store: dict[str, Any], allowed: set[str] | None) -> bool:
    if allowed is None:
        return True
    tags = {t.strip().lower() for t in (store.get("categories") or "").split("|") if t.strip()}
    tags |= {t.strip().lower() for t in (store.get("categories") or "").split("•") if t.strip()}
    return bool(tags & allowed)


def style_header(ws, ncols: int) -> None:
    for col in range(1, ncols + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
    ws.freeze_panes = "A2"


def autosize(ws, max_width: int = 52) -> None:
    for col in ws.columns:
        letter = get_column_letter(col[0].column)
        longest = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[letter].width = min(max(10, longest + 2), max_width)


def add_table(wb: Workbook, title: str, columns: list[str], rows: list[list[Any]]):
    ws = wb.create_sheet(title[:31])
    ws.append(columns)
    for row in rows:
        ws.append(row)
    style_header(ws, len(columns))
    autosize(ws)
    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{len(rows) + 1}"
    return ws


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--locality", required=True, help='e.g. "Sector 29"')
    ap.add_argument("--city", default="Gurgaon")
    ap.add_argument("--categories", choices=["apparel", "fashion", "all"], default="apparel")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    allowed = {"apparel": APPAREL_TAGS, "fashion": FASHION_TAGS, "all": None}[args.categories]

    path = DATA / "store_details.json"
    if not path.exists():
        raise SystemExit("data/store_details.json not found -- run `python3 crawl.py sitemap` "
                         "then `python3 crawl.py enrich` first.")
    details: dict[str, dict[str, Any]] = json.loads(path.read_text())

    target_city = norm_city(args.city)
    target_locality = args.locality.strip().lower()

    # District sometimes publishes two detail pages for one physical store.
    # Collapse on brand + coordinates + address so counts are not inflated.
    rows_all = [dict(v, slug=k) for k, v in details.items()]
    subcity = resolve_subcity(rows_all)
    for row in rows_all:
        if id(row) in subcity:
            row["city"] = subcity[id(row)].title()

    city_stores, locality_stores = [], []
    seen: set[tuple] = set()
    duplicates = 0
    for store in rows_all:
        slug = store["slug"]
        if norm_city(store.get("city")) != target_city:
            continue
        fingerprint = (
            (store.get("brand") or "").strip().lower(),
            store.get("lat"), store.get("lng"),
            (store.get("street_address") or "").strip().lower(),
        )
        if fingerprint in seen:
            duplicates += 1
            continue
        seen.add(fingerprint)
        row = dict(store, slug=slug, locality_norm=norm_locality(store.get("locality")))
        city_stores.append(row)
        if row["locality_norm"].lower() == target_locality:
            locality_stores.append(row)

    if not city_stores:
        cities = sorted({norm_city(s.get("city")) for s in details.values() if s.get("city")})
        raise SystemExit(f"No stores for city {args.city!r}. Available: {', '.join(cities)}")

    if not locality_stores:
        near = Counter(s["locality_norm"] for s in city_stores)
        hint = ", ".join(f"{k} ({v})" for k, v in near.most_common(15))
        raise SystemExit(f"No stores in {args.locality!r}, {args.city}.\nLocalities present: {hint}")

    matched = [s for s in locality_stores if is_match(s, allowed)]

    # ------------------------------------------------------- city-wide ranking
    per_locality: dict[str, dict[str, Any]] = {}
    for s in city_stores:
        slot = per_locality.setdefault(
            s["locality_norm"], {"all": 0, "matched": 0, "brands": set()}
        )
        slot["all"] += 1
        if is_match(s, allowed):
            slot["matched"] += 1
            slot["brands"].add(s.get("brand") or "")
    ranked = sorted(per_locality.items(), key=lambda kv: (-kv[1]["matched"], -kv[1]["all"]))
    rank_of = {name: i + 1 for i, (name, _) in enumerate(ranked)}

    label = args.categories
    wb = Workbook()
    wb.remove(wb.active)

    # ------------------------------------------------------------- Summary
    ws = wb.create_sheet("Summary")
    brands = sorted({s.get("brand") or "" for s in matched})
    ws.append([f"{args.locality}, {args.city} — brand store density"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    facts = [
        ("Category filter", label),
        (f"{label.title()} stores", len(matched)),
        (f"Distinct {label} brands", len(brands)),
        ("All-category stores on District", len(locality_stores)),
        (f"{label.title()} share of locality", f"{len(matched) / len(locality_stores):.0%}"),
        ("", ""),
        (f"Rank in {args.city} by {label} stores", f"{rank_of.get(args.locality, '-')} of {len(ranked)}"),
        (f"{args.city} {label} stores (total)", sum(v["matched"] for v in per_locality.values())),
        (f"{args.city} localities covered", len(ranked)),
        ("", ""),
        ("Densest locality in city", f"{ranked[0][0]} ({ranked[0][1]['matched']} {label} stores)"),
        ("Duplicate listings merged", duplicates),
        ("Source", "District (Zomato Edition) store detail pages"),
    ]
    for key, value in facts:
        ws.append([key, value])
        if key:
            ws.cell(row=ws.max_row, column=1).font = ACCENT_FONT
    ws.append([])
    ws.append([f"{label.title()} brands here:"])
    ws.cell(row=ws.max_row, column=1).font = ACCENT_FONT
    for brand in brands:
        ws.append(["", brand])
    autosize(ws)

    # -------------------------------------------------------------- Stores
    store_rows = []
    for s in sorted(locality_stores, key=lambda x: (not is_match(x, allowed), x.get("brand") or "")):
        store_rows.append([
            s.get("brand"),
            "yes" if is_match(s, allowed) else "",
            s.get("vertical"),
            s.get("categories"),
            s.get("locality"),
            s.get("street_address"),
            s.get("postal_code"),
            s.get("lat"),
            s.get("lng"),
            s.get("url"),
        ])
    add_table(wb, "Stores",
              ["brand", f"is_{label}", "vertical", "categories", "locality_raw",
               "street_address", "pincode", "lat", "lng", "url"],
              store_rows)

    # ----------------------------------------------------- City localities
    city_rows = []
    for name, slot in ranked:
        city_rows.append([
            rank_of[name], name, slot["matched"], len(slot["brands"]), slot["all"],
            round(slot["matched"] / slot["all"], 3) if slot["all"] else 0,
            "<<<" if name.lower() == target_locality else "",
        ])
    ws = add_table(wb, f"{args.city} localities".title()[:31],
                   ["rank", "locality", f"{label}_stores", "distinct_brands",
                    "all_category_stores", f"{label}_share", "this_locality"],
                   city_rows)
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        if row[-1].value == "<<<":
            for cell in row:
                cell.font = ACCENT_FONT

    # ---------------------------------------------------- Brand x Locality
    matrix: dict[str, Counter] = defaultdict(Counter)
    for s in city_stores:
        if is_match(s, allowed):
            matrix[s.get("brand") or ""][s["locality_norm"]] += 1
    cols = [name for name, _ in ranked if per_locality[name]["matched"]]
    brand_order = sorted(matrix, key=lambda b: (-sum(matrix[b].values()), b))
    pivot_rows = []
    for brand in brand_order:
        counts = matrix[brand]
        pivot_rows.append([brand, sum(counts.values()), len(counts),
                           *(counts.get(c) or "" for c in cols)])
    ws = add_table(wb, "Brand x Locality",
                   ["brand", "total_stores", "localities", *cols], pivot_rows)
    ws.freeze_panes = "D2"

    # -------------------------------------------------------------- Method
    ws = wb.create_sheet("Method")
    for line in [
        ["How this was built"],
        [],
        ["Source", "District (Zomato Edition) public store detail pages, via "
                   "POST https://www.district.in/gw/web/consumer/shopping/store/details"],
        ["Enumeration", "District's own store sitemap (cdn.district.in/sitemap/"
                        "sitemap-store-detail-pages.xml.gz) — 25,432 store pages nationwide, "
                        "5,637 of them in Delhi NCR. This is a census, not a sample."],
        ["Locality field", "District states each store's locality itself (seo_locality). For a "
                           "store inside a mall it reads '<mall>, <locality>', so the mall name "
                           "is dropped and the locality kept. locality_raw on the Stores sheet "
                           "preserves the original."],
        ["Category filter", "Each store carries tags such as 'Fashion | Apparel • Accessories'. "
                            "apparel = tagged Apparel; fashion also includes footwear, bags, "
                            "luggage, accessories, jewellery, watches, eyewear."],
        [],
        ["Caveat", "Counts reflect stores District lists, not all retail in the locality. A "
                   "brand absent here may still trade there without a District listing."],
        ["Caveat", "Gurgaon and Gurugram are merged; District uses both spellings."],
    ]:
        ws.append(line)
    ws["A1"].font = Font(bold=True, size=13)
    for row in ws.iter_rows(min_row=3, max_row=ws.max_row):
        row[0].font = ACCENT_FONT
        if len(row) > 1 and row[1].value:
            row[1].alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 96

    out = Path(args.out) if args.out else DATA / (
        f"{args.locality.replace(' ', '_').replace(',', '')}_{args.city}_{label}_density.xlsx"
    )
    wb.save(out)
    print(f"wrote {out}")
    print(f"  {args.locality}, {args.city}: {len(matched)} {label} stores "
          f"({len(brands)} brands) of {len(locality_stores)} total on District")
    print(f"  rank {rank_of.get(args.locality, '-')} of {len(ranked)} localities in {args.city}")
    if duplicates:
        print(f"  merged {duplicates} duplicate District listings in {args.city}")


if __name__ == "__main__":
    main()
