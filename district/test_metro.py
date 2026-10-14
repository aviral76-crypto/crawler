#!/usr/bin/env python3
"""Self-check for the locator-to-metro mapping, the one place where a brand's
own city names have to line up with District's city groups.

    python3 test_metro.py
"""

from brand_locator_fetch import METRO_OF, metro_of
from city_matrix import CITY_GROUPS
from district_api import norm_city

CASES = [
    # the spellings District normalises away, which locators still use
    ("Gurugram", "Gurgaon"), ("Gurgaon", "Gurgaon"),
    ("Bengaluru", "Bangalore"), ("Bangalore", "Bangalore"),
    ("Secunderabad", "Hyderabad"), ("Hyderabad", "Hyderabad"),
    ("Pimpri-Chinchwad", "Pune"), ("Pune", "Pune"),
    # qualified forms a locator writes, which must still land
    ("Thane West", "Mumbai"), ("Navi Mumbai", "Mumbai"),
    ("New Delhi", "Delhi NCR (ex Gurgaon)"), ("Noida", "Delhi NCR (ex Gurgaon)"),
    # "greater noida" must not read as "noida" -- same answer here, but the
    # longest-key rule is what keeps Navi Mumbai out of plain Mumbai
    ("Greater Noida", "Delhi NCR (ex Gurgaon)"),
    # a whole address, which is what a locator with no city field gives us
    ("Shop 5, Nexus Mall, Koramangala, Bengaluru 560034", "Bangalore"),
    # outside the six metros, and outside District's metro groups
    ("Chennai", None), ("Kolkata", None), ("Patna", None), ("Nashik", None),
    # part of the wider Mumbai region but NOT of District's Mumbai group,
    # so counting them would beat District's side unfairly
    ("Kalyan", None), ("Mira Road", None), ("Vasai", None),
    (None, None), ("", None),
]


def main() -> None:
    for raw, want in CASES:
        got = metro_of(raw)
        assert got == want, f"metro_of({raw!r}) == {got!r}, want {want!r}"

    # Every city this mapping claims must be one District actually groups into
    # that metro, or the two sides of the sheet are measuring different places.
    district = {
        "Gurgaon": {"gurgaon"},
        "Delhi NCR (ex Gurgaon)": set(CITY_GROUPS["delhi ncr ex gurgaon"]),
        "Bangalore": {"bangalore"},
        "Mumbai": set(CITY_GROUPS["mumbai"]),
        "Pune": set(CITY_GROUPS["pune"]),
        "Hyderabad": set(CITY_GROUPS["hyderabad"]),
    }
    for city, metro in METRO_OF.items():
        assert norm_city(city) in district[metro] | {"delhi", "pimpri", "chinchwad",
                                                     "pimpri-chinchwad"}, (
            f"{city!r} is mapped to {metro!r} but District does not group it there")

    print(f"ok: {len(CASES)} city cases, {len(METRO_OF)} mappings agree with "
          "District's city groups")


if __name__ == "__main__":
    main()
