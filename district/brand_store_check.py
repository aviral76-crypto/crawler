#!/usr/bin/env python3
"""Cross-check the District store counts in "Brand Stores - Pan India.xlsx"
against each brand's own store locator, and write the corrected sheet back.

    python3 brand_store_check.py counts     # rebuild data/brand_district_counts.json
    python3 brand_store_check.py sheet      # add/replace the corrected sheet

Two files feed the corrected sheet:

  data/brand_district_counts.json   what District says, per brand per metro.
                                    Derived from the workbook, so `counts` can
                                    always rebuild it.
  data/brand_locator_counts.json    what the brand's own store locator says.
                                    Filled in by hand/by fetch, one entry per
                                    brand; brands absent from it are reported
                                    as unchecked rather than silently matching.

The locator file holds, per brand:
    site         brand's official India site
    locator      the store-locator URL the count came from
    india_total  stores the locator lists across India, None if not countable
    metros       {metro: count} for the six metros the workbook covers
    method       how the number was obtained (api / page-count / stated)
    note         anything that qualifies the number
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
WORKBOOK = DATA / "Brand Stores - Pan India.xlsx"
DISTRICT_COUNTS = DATA / "brand_district_counts.json"
#: Two sources of locator numbers, both written only from a real locator
#: response: the hand-wired fetchers and the generic scan. Where both have a
#: brand the hand-wired one wins, being the more specific reverse-engineering.
LOCATOR_FILES = (DATA / "brand_locator_scan_counts.json",
                 DATA / "brand_locator_render_counts.json",
                 DATA / "brand_locator_counts.json")
#: Excel caps a sheet name at 31 characters, so the name asked for
#: ("corrected store count brand store - pan india") is shortened to fit.
SHEET_NAME = "Corrected Store Count-Pan India"

#: Workbook sheet -> metro. The Gurgaon sheet is brand-per-row; the rest are
#: locality-per-row with a brand per column and a TOTAL row at the bottom.
SHEETS: dict[str, str] = {
    "Brand x Locality - Gurgaon": "Gurgaon",
    "Brand x Locality - Delhi NCR (E": "Delhi NCR (ex Gurgaon)",
    "Brand x Locality - Bangalore": "Bangalore",
    "Brand x Locality - Mumbai": "Mumbai",
    "Brand x Locality - Pune": "Pune",
    "Brand x Locality - Hyderabad": "Hyderabad",
}
METROS = list(SHEETS.values())

HEADER_FILL = PatternFill("solid", fgColor="1F2937")
HEADER_FONT = Font(color="FFFFFF", bold=True)
MATCH_FILL = PatternFill("solid", fgColor="DCFCE7")     # locator agrees
UNDER_FILL = PatternFill("solid", fgColor="FEF3C7")     # District lists fewer
OVER_FILL = PatternFill("solid", fgColor="FEE2E2")      # District lists more
BOLD = Font(bold=True)


def district_counts() -> dict[str, dict[str, int]]:
    """{brand: {metro: stores}} read out of the workbook itself."""
    wb = load_workbook(WORKBOOK, data_only=True)
    counts: dict[str, dict[str, int]] = defaultdict(dict)
    for sheet, metro in SHEETS.items():
        ws = wb[sheet]
        rows = list(ws.iter_rows(values_only=True))
        if metro == "Gurgaon":
            # brand | gender | total_stores | localities | <locality>...
            for row in rows[1:]:
                if row and row[0]:
                    counts[row[0]][metro] = int(row[2] or 0)
            continue
        header = list(rows[0])
        total = next((r for r in rows if r and r[0] == "TOTAL"), None)
        if total is None:
            raise SystemExit(f"{sheet!r} has no TOTAL row to read brand totals from")
        for i, brand in enumerate(header):
            if i >= 3 and brand:
                counts[brand][metro] = int(total[i] or 0)
    wb.close()  # noqa
    return dict(counts)


def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def write_sheet() -> None:
    district = load_json(DISTRICT_COUNTS, None)
    if district is None:
        raise SystemExit(f"{DISTRICT_COUNTS.name} missing -- run `counts` first")
    locator: dict[str, dict] = {}
    for path in LOCATOR_FILES:
        locator.update(load_json(path, {}))

    # A brand whose locator was found but could not be read still gets its site
    # and locator URL reported, so the gap is checkable by hand rather than
    # just blank. No numbers come from here -- District's stand untouched.
    sites = load_json(DATA / "brand_sites.json", {})
    for brand, row in load_json(DATA / "brand_locator_scan.json", {}).items():
        if brand in locator or not row.get("locator"):
            continue
        locator[brand] = {
            "site": row.get("site"), "locator": row["locator"],
            "india_total": None, "metros": {},
            "method": "locator found; store list not machine-readable",
            "checked": None, "note": row.get("why") or "",
        }
    for brand, urls in load_json(DATA / "brand_locator_urls.json", {}).items():
        if brand in locator or not urls:
            continue
        locator[brand] = {
            "site": sites.get(brand, {}).get("site"), "locator": urls[0],
            "india_total": None, "metros": {},
            "method": "locator URL found in the brand's sitemap, not read",
            "checked": None, "note": "",
        }

    wb = load_workbook(WORKBOOK)
    if SHEET_NAME in wb.sheetnames:
        del wb[SHEET_NAME]
    ws = wb.create_sheet(SHEET_NAME)

    # Three blocks of the same six metros: what District says, what the
    # brand's own locator says, and the corrected figure -- which is the
    # locator's where there is one and District's untouched where there is not.
    head: list[str] = ["brand", "District total"]
    head += [f"D: {m}" for m in METROS]
    head += ["locator: India total", "locator: 6 metros"]
    head += [f"L: {m}" for m in METROS]
    head += ["corrected: 6 metros"]
    head += [f"C: {m}" for m in METROS]
    head += ["delta vs District", "status", "official site", "store locator",
             "method", "checked", "note"]
    ws.append(head)
    for cell in ws[1]:
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(wrap_text=True, vertical="bottom")

    delta_col = head.index("delta vs District") + 1
    order = sorted(district, key=lambda b: (-sum(district[b].values()), b.lower()))
    for brand in order:
        per_metro = district[brand]
        d_total = sum(per_metro.values())
        hit = locator.get(brand) or {}
        l_metros = hit.get("metros") or {}
        checked = bool(l_metros)
        l_total = sum(l_metros.values()) if checked else None
        # Only a locator may change a number; without one District's stands.
        corrected = {m: (l_metros.get(m, 0) if checked else per_metro.get(m))
                     for m in METROS}
        c_total = sum(v or 0 for v in corrected.values())
        delta = (l_total - d_total) if checked else None
        if not hit:
            status = "unchanged - locator not found"
        elif not checked:
            status = "unchanged - locator gave no store list"
        elif delta == 0:
            status = "locator agrees"
        elif delta > 0:
            status = "corrected up - locator lists more"
        else:
            status = "corrected down - locator lists fewer"

        ws.append([brand, d_total] + [per_metro.get(m) for m in METROS] +
                  [hit.get("india_total"), l_total] +
                  [l_metros.get(m) if checked else None for m in METROS] +
                  [c_total] + [corrected[m] for m in METROS] +
                  [delta, status, hit.get("site"), hit.get("locator"),
                   hit.get("method"), hit.get("checked"), hit.get("note")])
        if delta is not None:
            ws.cell(ws.max_row, delta_col).fill = (
                MATCH_FILL if delta == 0 else UNDER_FILL if delta > 0 else OVER_FILL)

    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(head))}{ws.max_row}"
    widths = ([34, 12] + [10] * len(METROS) + [13, 13] + [10] * len(METROS) +
              [13] + [10] * len(METROS) + [12, 32, 30, 48, 34, 11, 70])
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(col)].width = width

    corrected_n = sum(1 for b in order if (locator.get(b) or {}).get("metros"))
    with_url = sum(1 for b in order if (locator.get(b) or {}).get("locator"))
    write_method(wb, len(order), corrected_n, with_url)

    wb.save(WORKBOOK)
    print(f"wrote {SHEET_NAME!r} to {WORKBOOK.name}: {len(order)} brands, "
          f"{corrected_n} counted off the brand's own locator, "
          f"{with_url - corrected_n} with a locator URL but no readable list, "
          f"{len(order) - with_url} left as District has them")


METHOD_SHEET = "Corrected Count - Method"


def write_method(wb, brands: int, corrected_n: int, with_url: int) -> None:
    if METHOD_SHEET in wb.sheetnames:
        del wb[METHOD_SHEET]
    ws = wb.create_sheet(METHOD_SHEET)
    for line in [
        ["How the corrected store counts were produced"],
        [],
        ["Question", "Every brand in this workbook, cross-checked against its own "
                     "website's store locator, with the count corrected where the "
                     "locator disagrees."],
        [],
        ["Rule", "A number is only changed when the brand's own store locator "
                 "returned a store list that says something different. Where no "
                 "locator could be read, District's figure is carried through "
                 "unchanged -- nothing here is estimated or inferred."],
        ["Coverage", f"{brands} unique brands across the six metro sheets. "
                     f"{corrected_n} were counted off their own locator. "
                     f"{with_url - corrected_n} have a locator whose URL is given "
                     "but whose store list is not machine-readable. "
                     f"{brands - with_url} had no locator found, and are unchanged."],
        [],
        ["Columns", "'D:' columns are District's counts, read from each metro "
                    "sheet's TOTAL row. 'L:' columns are the brand's own locator. "
                    "'C:' columns are the corrected figure -- the locator's where "
                    "there is one, District's where there is not."],
        ["delta", "locator's six-metro total minus District's. Positive means "
                  "District lists fewer stores than the brand does."],
        [],
        ["Why the two disagree",
         "Neither source is simply right. District lists stores it has onboarded, "
         "including brand counters inside multi-brand stores and department "
         "stores, so it can exceed a brand's own list (Levi's, U.S. Polo Assn.). "
         "A brand's locator lists the outlets it chooses to publish, usually its "
         "exclusive stores, and is often more current and more complete for "
         "newer chains (Go Colors, Pantaloons, Manyavar). Both numbers are kept "
         "so the gap is visible rather than resolved by guesswork."],
        [],
        ["Locator: India total",
         "Stores the locator lists across the whole country, not just these six "
         "metros. Blank where the locator covers the brand worldwide and no "
         "India-only figure can be separated out."],
        ["Not reachable",
         "Zudio and Westside (Trent) refuse automated requests, and the ABFRL "
         "brands (Allen Solly, Van Heusen, Peter England, Louis Philippe) load "
         "stores through a mechanism with no readable endpoint. Their District "
         "counts stand unchanged."],
        [],
        ["Rebuild", "python3 brand_store_check.py counts  then  "
                    "python3 brand_store_check.py sheet"],
    ]:
        ws.append(line)
    ws["A1"].font = Font(bold=True, size=13)
    for row in ws.iter_rows(min_row=3, max_row=ws.max_row):
        row[0].font = BOLD
        if len(row) > 1 and row[1].value:
            row[1].alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 110


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "sheet"
    if cmd == "counts":
        counts = district_counts()
        DISTRICT_COUNTS.write_text(json.dumps(counts, indent=1, ensure_ascii=False))
        print(f"wrote {DISTRICT_COUNTS.name}: {len(counts)} brands")
    elif cmd == "sheet":
        write_sheet()
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
