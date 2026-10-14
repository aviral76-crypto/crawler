#!/usr/bin/env python3
"""Revenue per store, per brand, from each brand's own published accounts.

    python3 brand_revenue.py check     # validate the data file, print coverage
    python3 brand_revenue.py sheet     # write the sheet into the workbook

Every figure comes from a filing, an investor presentation or a report citing
one, and carries its source URL. Brands that publish nothing are left blank --
no peer estimates, no inference -- and the coverage line says how much of the
store footprint that leaves uncovered.

Why revenue per store needs this much bookkeeping:

  * A company's revenue is not its stores' revenue. It usually includes
    e-commerce, wholesale to multi-brand outlets, exports and licensing, and
    only some filings split own-retail out. `basis` records which it is.
  * A franchised brand's disclosed revenue is the wholesale sale to the
    franchisee, not what a shopper spends. Manyavar and Raymond trade mostly
    through franchisees, so their as-disclosed revenue per store is roughly
    half the retail sale-through. `gross_up` records the factor applied and
    why, and `basis` becomes "retail (grossed up)".
  * Store counts move through the year, so the divisor is the average of
    opening and closing count where both are published, not the closing count,
    which would understate every expanding brand.
  * Revenue per store is not comparable across brands -- a Go Colors store is
    about 500 sqft and an H&M about 25,000 -- so revenue per sqft is carried
    wherever store area is disclosed, and is the figure to compare on.

Data lives in data/brand_revenue.json so it can be audited and corrected
without touching code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
WORKBOOK = DATA / "Brand Stores - Pan India.xlsx"
REVENUE = DATA / "brand_revenue.json"
DISTRICT_COUNTS = DATA / "brand_district_counts.json"
SHEET_NAME = "Revenue per Store by Brand"

HEADER_FILL = PatternFill("solid", fgColor="1F2937")
HEADER_FONT = Font(color="FFFFFF", bold=True)
BOLD = Font(bold=True)
GROSSED_FILL = PatternFill("solid", fgColor="FEF3C7")   # a figure we adjusted

#: What the revenue figure covers. Anything other than "own retail" means
#: revenue per store is an upper bound on what the stores alone turn over.
BASES = {
    "own retail",            # company split out its own-store revenue
    "own retail (grossed up)",   # franchisee wholesale, grossed to sale-through
    "total company",         # includes online, wholesale, exports, licensing
    "not separable",         # entity covers several brands, no split published
}

REQUIRED = ("entity", "basis", "source")


def load() -> dict[str, dict]:
    return json.loads(REVENUE.read_text()) if REVENUE.exists() else {}


CRORE = 1e7


def per_store(row: dict) -> float | None:
    """Revenue per store in rupees crore.

    Two routes, because companies disclose one or the other. Either brand
    revenue and a store count, or -- as Trent does for Westside and Zudio --
    sales per square foot and average store area, which multiply to the same
    thing without the brand's revenue ever being stated.
    """
    gross = row.get("gross_up") or 1.0
    rev, stores = row.get("revenue_cr"), row.get("stores")
    if rev is not None and stores:
        return round(rev * gross / stores, 2)
    sqft, density = row.get("sqft"), row.get("rev_per_sqft_disclosed")
    if sqft and density:
        return round(sqft * density * gross / CRORE, 2)
    return None


def per_sqft(row: dict) -> int | None:
    """Revenue per square foot in rupees a year, disclosed or derived."""
    if row.get("rev_per_sqft_disclosed"):
        return round(row["rev_per_sqft_disclosed"] * (row.get("gross_up") or 1.0))
    rev, stores, sqft = row.get("revenue_cr"), row.get("stores"), row.get("sqft")
    if rev is None or not stores or not sqft:
        return None
    return round(rev * (row.get("gross_up") or 1.0) * CRORE / (stores * sqft))


def check() -> None:
    rows = load()
    district = json.loads(DISTRICT_COUNTS.read_text())
    footprint = {b: sum(v.values()) for b, v in district.items()}

    problems = []
    for brand, row in rows.items():
        if brand not in district:
            problems.append(f"{brand!r} is not a brand in the workbook")
        for field in REQUIRED:
            if not row.get(field):
                problems.append(f"{brand}: missing {field}")
        if row.get("basis") not in BASES:
            problems.append(f"{brand}: basis {row.get('basis')!r} not one of {sorted(BASES)}")
        if row.get("gross_up") and "grossed up" not in (row.get("basis") or ""):
            problems.append(f"{brand}: gross_up set but basis does not say so")
        if row.get("revenue_cr") is not None and not row.get("fy"):
            problems.append(f"{brand}: revenue without a financial year")
    for p in problems:
        print(f"  !! {p}")

    priced = [b for b, r in rows.items() if per_store(r) is not None]
    covered = sum(footprint.get(b, 0) for b in priced)
    total = sum(footprint.values())
    print(f"\n{len(rows)} brands researched, {len(priced)} with a revenue per store")
    print(f"covers {covered} of {total} store-rows ({covered / total:.0%} of footprint)")
    if problems:
        raise SystemExit(f"{len(problems)} problems to fix")


def write_sheet() -> None:
    rows = load()
    district = json.loads(DISTRICT_COUNTS.read_text())
    footprint = {b: sum(v.values()) for b, v in district.items()}

    wb = load_workbook(WORKBOOK)
    if SHEET_NAME in wb.sheetnames:
        del wb[SHEET_NAME]
    ws = wb.create_sheet(SHEET_NAME)

    head = ["brand", "stores in this workbook (6 metros)", "parent entity",
            "listed", "financial year", "revenue (Rs cr)", "revenue basis",
            "stores (India, same period)", "revenue per store (Rs cr)",
            "avg store size (sqft)", "revenue per sqft (Rs/year)",
            "franchise gross-up", "source", "note"]
    ws.append(head)
    for cell in ws[1]:
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="bottom")

    order = sorted(district, key=lambda b: (-footprint[b], b.lower()))
    for brand in order:
        row = rows.get(brand) or {}
        ws.append([
            brand, footprint[brand], row.get("entity"),
            ("yes" if row.get("listed") else "no") if row.get("entity") else None,
            row.get("fy"), row.get("revenue_cr"), row.get("basis"),
            row.get("stores"), per_store(row), row.get("sqft"), per_sqft(row),
            row.get("gross_up"), row.get("source"), row.get("note"),
        ])
        if row.get("gross_up"):
            ws.cell(ws.max_row, head.index("revenue per store (Rs cr)") + 1).fill = GROSSED_FILL

    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(head))}{ws.max_row}"
    for col, width in enumerate([34, 14, 34, 8, 12, 13, 22, 14, 13, 13, 14, 12, 56, 70], 1):
        ws.column_dimensions[get_column_letter(col)].width = width

    priced = sum(1 for b in order if per_store(rows.get(b) or {}) is not None)
    covered = sum(footprint[b] for b in order if per_store(rows.get(b) or {}) is not None)
    write_method(wb, len(order), len(rows), priced, covered, sum(footprint.values()))
    wb.save(WORKBOOK)
    print(f"wrote {SHEET_NAME!r}: {len(order)} brands, {priced} with revenue per "
          f"store, covering {covered}/{sum(footprint.values())} store-rows "
          f"({covered / sum(footprint.values()):.0%})")


METHOD_SHEET = "Revenue per Store - Method"


def write_method(wb, brands: int, researched: int, priced: int,
                 covered: int, total: int) -> None:
    if METHOD_SHEET in wb.sheetnames:
        del wb[METHOD_SHEET]
    ws = wb.create_sheet(METHOD_SHEET)
    for line in [
        ["Revenue per store, and why most rows are blank"],
        [],
        ["Rule", "Every figure comes from a filing, a results presentation or a "
                 "report citing one, and carries its source. Nothing is estimated "
                 "from peers or annualised from a quarter. A brand gets a revenue "
                 "per store only when a precise revenue for a stated financial year "
                 "and a store count for the same period are both published."],
        ["Coverage", f"{researched} of {brands} brands researched so far; {priced} "
                     f"have a revenue per store, covering {covered} of {total} "
                     f"store-rows ({covered / total:.0%} of the footprint). The "
                     "researched rows that are blank say in their note exactly what "
                     "was found and what was missing."],
        [],
        ["Why so few", "Brand-level revenue is usually not published. Multi-brand "
                       "groups report by segment: Arvind groups U.S. Polo Assn., "
                       "Arrow, Flying Machine, Tommy and Calvin Klein as 'Power "
                       "Brands'; TCNS stated W was 'over 50%' and Aurelia '30-40%' "
                       "of income without ever giving either a revenue or a store "
                       "count. Those brands cannot be split without inventing the "
                       "split, so they are left blank."],
        [],
        ["Not comparable", "Revenue per store must not be compared across brands "
                           "directly. A Go Colors store is about 540 sqft and turns "
                           "over roughly Rs 1.2 cr; a Zara flagship turns over about "
                           "Rs 126 cr. Use revenue per sqft for comparison, and even "
                           "then check the revenue basis first."],
        ["Revenue basis", "'own retail' is store sale-through. 'total company' "
                          "includes e-commerce, wholesale to multi-brand outlets, "
                          "department-store counters, exports and licensing -- "
                          "dividing it by exclusive stores alone OVERSTATES revenue "
                          "per store, often badly: Levi's and Monte Carlo sell mostly "
                          "outside their own stores. 'not separable' means the entity "
                          "covers several brands with no published split."],
        ["Franchise", "A franchised store's revenue reaches the brand at wholesale, "
                      "not at what the shopper pays, so an as-disclosed figure "
                      "understates the store's trade. Vedant Fashions publishes both "
                      "(FY25 revenue Rs 1,387 cr, sales to customers Rs 1,893 cr) so "
                      "the sale-through figure is used directly and no gross-up was "
                      "needed. Any row where a gross-up was applied names the factor "
                      "and is shaded."],
        ["Store counts", "Where a company gives opening and closing counts, the "
                         "average is the right divisor; a closing count understates "
                         "every expanding brand. Some rows use the brand's own "
                         "locator count from this workbook, which is stated in the "
                         "note."],
        [],
        ["Rebuild", "python3 brand_revenue.py check   then   python3 brand_revenue.py sheet"],
        ["Data", "data/brand_revenue.json -- edit there, not in the sheet"],
    ]:
        ws.append(line)
    ws["A1"].font = Font(bold=True, size=13)
    for row in ws.iter_rows(min_row=3, max_row=ws.max_row):
        row[0].font = BOLD
        if len(row) > 1 and row[1].value:
            row[1].alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 18
    ws.column_dimensions["B"].width = 110


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        check()
    elif cmd == "sheet":
        write_sheet()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
