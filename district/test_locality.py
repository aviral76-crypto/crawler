#!/usr/bin/env python3
"""Self-check for the two bits of city logic that are easy to get wrong:
reducing District's locality labels, and scoping its slugs to a metro.

    python3 test_locality.py
"""

from crawl import CITY_SLUG_TOKENS, slug_city_token
from district_api import norm_city, norm_locality, subcity_from_address

LOCALITIES = [
    # NCR, the cases the Delhi run already relied on
    ("Ambience Mall, DLF Phase 3", "DLF Phase 3"),
    ("Sector 29, Gurugram", "Sector 29"),
    ("MG Road Gurugram", "MG Road"),
    ("Khan Market", "Khan Market"),
    ("New Delhi", "New Delhi"),
    # the four new metros
    ("Viviana Mall, Majiwada Thane West", "Majiwada"),
    ("Khopat, Thane West", "Khopat"),
    ("Nexus Seawoods, Seawoods", "Seawoods"),
    ("Bhartiya Mall Of Bengaluru, Jakkur", "Jakkur"),
    ("UB City, Lavelle Road, Central Bangalore", "Lavelle Road"),
    ("Koramangala, South Bangalore", "Koramangala"),
    ("Panjagutta", "Panjagutta"),
    ("Bowenpally, Secunderabad", "Bowenpally"),
    ("Chinchwad, Pune", "Chinchwad"),
    ("The Pavillion Mall, Senapati Bapat Road", "Senapati Bapat Road"),
    # a mall with no locality attached stays the mall -- nothing to recover
    ("Phoenix Marketcity Shopping Mall", "Phoenix Marketcity Shopping Mall"),
    (None, ""),
]

SLUGS = [
    ("go-colors-khopat-thane-west-SDP-24348367", "mumbai", "thane"),
    ("allen-solly-nexus-seawoods-navi-mumbai-SDP-1", "mumbai", "navi-mumbai"),
    ("mochi-mumbai-cst-area-SDP-73401255", "mumbai", "mumbai"),
    ("go-colors-panjagutta-hyderabad-SDP-2", "hyderabad", "hyderabad"),
    ("indian-terrain-bowenpally-secunderabad-SDP-3", "hyderabad", "secunderabad"),
    ("lenskart-hyderabad-kukatpally-SDP-4", "hyderabad", "hyderabad"),
    ("rareism-chinchwad-pune-SDP-5", "pune", "pune"),
    ("mango-bhartiya-mall-of-bengaluru-jakkur-bangalore-SDP-6", "bangalore", "bangalore"),
    ("snitch-sector-29-gurgaon-SDP-7", "ncr", "gurgaon"),
    # another metro's slug must not be claimed
    ("woodland-perintalmanna-locality-SDP-32112313", "pune", None),
    ("trends-footwear-gaya-locality-SDP-39200538", "mumbai", None),
]


def main() -> None:
    for raw, want in LOCALITIES:
        got = norm_locality(raw)
        assert got == want, f"norm_locality({raw!r}) == {got!r}, want {want!r}"

    for slug, metro, want in SLUGS:
        got = slug_city_token(slug, CITY_SLUG_TOKENS[metro])
        assert got == want, f"slug_city_token({slug!r}, {metro}) == {got!r}, want {want!r}"

    assert norm_city("Bengaluru") == norm_city("Bangalore") == "bangalore"
    assert norm_city("Gurugram") == "gurgaon"

    for addr, want in [
        ("Sector 32, Noida, Uttar Pradesh 201307", "noida"),
        ("Gaur City Mall, Greater Noida West, UP", "greater noida"),
        ("Sector 29, Gurugram, Haryana", "gurgaon"),
        ("Khan Market, Delhi 110003", "new delhi"),
        ("Shop 12, Plot 58, Block BW", None),
        (None, None),
    ]:
        got = subcity_from_address(addr)
        assert got == want, f"subcity_from_address({addr!r}) == {got!r}, want {want!r}"

    print(f"ok: {len(LOCALITIES)} localities, {len(SLUGS)} slugs")


if __name__ == "__main__":
    main()
