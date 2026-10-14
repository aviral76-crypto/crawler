"""
Thin client for District's (Zomato Edition) consumer gateway.

Base: https://www.district.in/gw  -- the same backend the native app talks to.
Auth: there is none for discovery/read endpoints; the web client sends a
constant guest token ("1212") plus a client-id/app-type/app-version triplet.

Endpoints used here
-------------------
POST /web/pre_home              lat/lng -> city, p_city, locality, zomato subzone id
POST /web/get_discovery_results layout_type="shopping_home" -> hub_rail (nearby malls/markets)
POST /web/hub_page              layout_type="hub_page"             -> hub metadata (address, lat/lng, counts)
                                layout_type="hub_categories_shops" -> paginated store list for that hub
POST /web/consumer/shopping/store/details   slug -> single store detail
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterator

BASE = "https://www.district.in/gw"
GUEST_TOKEN = "1212"

# Delhi NCR is a single District city; the sub-cities live in p_city_id.
CITY_DELHI_NCR = 1


class DistrictError(RuntimeError):
    pass


@dataclass
class Location:
    """A point plus whatever District resolved it to."""

    lat: float
    lng: float
    city_id: int = CITY_DELHI_NCR
    p_city_id: int | None = None
    p_city_name: str | None = None
    subzone_id: str | None = None
    zone_id: str | None = None
    locality: str | None = None
    locality_detail: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "city_id": self.city_id,
            "user_lat": self.lat,
            "user_lng": self.lng,
            "gps_lat": self.lat,
            "gps_lng": self.lng,
        }

    def as_headers(self) -> dict[str, str]:
        h = {
            "x-city-id": str(self.city_id),
            "x-user-lat": str(self.lat),
            "x-user-lng": str(self.lng),
            "x-gps-lat": str(self.lat),
            "x-gps-lng": str(self.lng),
        }
        if self.p_city_id:
            h["x-pcity-id"] = str(self.p_city_id)
        if self.zone_id:
            h["x-z-zone-id"] = str(self.zone_id)
        return h


class DistrictClient:
    """Rate-limited, retrying JSON client. Safe to share across threads."""

    def __init__(
        self,
        qps: float = 4.0,
        retries: int = 4,
        timeout: float = 45.0,
        device_id: str = GUEST_TOKEN,
    ) -> None:
        self.retries = retries
        self.timeout = timeout
        self.device_id = device_id
        self._min_gap = 1.0 / qps if qps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def _throttle(self) -> None:
        if not self._min_gap:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self._min_gap
        if wait > 0:
            time.sleep(wait)

    def _headers(self, extra: dict[str, str] | None) -> dict[str, str]:
        h = {
            "accept": "application/json",
            "content-type": "application/json",
            "x-client-id": "district-web",
            "x-app-type": "ed_web",
            "x-app-version": "11.11.1",
            "x-guest-token": GUEST_TOKEN,
            "x-device-id": self.device_id,
            "x-is-events-supported": "true",
            "x-is-movies-supported": "true",
            "x-is-dining-supported": "true",
            "origin": "https://www.district.in",
            "referer": "https://www.district.in/stores/",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        h.update(extra or {})
        return h

    def post(
        self, path: str, body: dict[str, Any], headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self.retries):
            self._throttle()
            req = urllib.request.Request(
                BASE + path,
                data=json.dumps(body).encode(),
                headers=self._headers(headers),
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read() or b"{}")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:400]
                # 4xx other than 429 is a bad request -- retrying will not help.
                if exc.code != 429 and 400 <= exc.code < 500:
                    raise DistrictError(f"{path} -> HTTP {exc.code}: {detail}") from exc
                last = DistrictError(f"{path} -> HTTP {exc.code}: {detail}")
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc
            time.sleep((2**attempt) * 0.5 + random.random() * 0.4)
        raise DistrictError(f"{path} failed after {self.retries} attempts: {last}")

    # ---------------------------------------------------------------- endpoints

    def resolve_location(self, lat: float, lng: float) -> Location:
        """lat/lng -> District's own idea of city + locality + Zomato subzone."""
        r = self.post(
            "/web/pre_home",
            {"gps_lat": lat, "user_lat": lat, "gps_lng": lng, "user_lng": lng},
            {"x-user-lat": str(lat), "x-user-lng": str(lng)},
        )
        d = r.get("location_data") or {}
        if not d:
            raise DistrictError(f"pre_home returned no location_data for {lat},{lng}")
        return Location(
            lat=lat,
            lng=lng,
            city_id=int(d.get("city_id") or CITY_DELHI_NCR),
            p_city_id=int(d["p_city_id"]) if d.get("p_city_id") else None,
            p_city_name=d.get("p_city_name"),
            subzone_id=d.get("z_subzone_id"),
            zone_id=d.get("z_zone_id"),
            locality=d.get("display_title"),
            locality_detail=d.get("display_subtitle"),
        )

    def nearby_hubs(self, loc: Location) -> list[dict[str, Any]]:
        """The shopping home feed's hub_rail: nearest malls / markets / high streets.

        Returns ~5-14 hubs per call, so callers sweep a grid of points to
        enumerate a whole city.
        """
        r = self.post(
            "/web/get_discovery_results",
            {
                "location": loc.as_payload(),
                "layout_type": "shopping_home",
                "request_type": "tab_switch",
            },
            loc.as_headers(),
        )
        rails = (r.get("EDSResponse") or {}).get("rails") or []
        hubs = []
        for rail in rails:
            if rail.get("rail_identifier") != "hub_rail":
                continue
            for item in rail.get("items") or []:
                ed = ((item or {}).get("ItemDetails") or {}).get("EditorialData") or {}
                meta = ed.get("display_meta") or {}
                entity = ed.get("entity_data") or {}
                if not ed.get("entity_id"):
                    continue
                hubs.append(
                    {
                        "hub_id": str(ed["entity_id"]),
                        "hub_name": meta.get("title") or entity.get("hub_name"),
                        "hub_type": entity.get("hub_type"),
                        # subtitle looks like "12.3 km | 148 stores"
                        "rail_subtitle": meta.get("subtitle"),
                    }
                )
        return hubs

    def hub_details(self, hub_id: str, loc: Location) -> dict[str, Any]:
        r = self.post(
            "/web/hub_page",
            {
                "hub_id": str(hub_id),
                "location": loc.as_payload(),
                "layout_type": "hub_page",
                "page_type": "hub_page",
                "request_type": "tab_switch",
            },
            loc.as_headers(),
        )
        return r.get("data") or {}

    def hub_stores(self, hub_id: str, loc: Location, max_pages: int = 60) -> Iterator[dict[str, Any]]:
        """Every store in a hub, following the section's postback pagination.

        The first page arrives with the section itself; each subsequent page is a
        `load_more` carrying the previous page's postbackParams.
        """
        payload = {
            "hub_id": str(hub_id),
            "location": loc.as_payload(),
            "layout_type": "hub_categories_shops",
            "page_type": "hub_page",
            "request_type": "tab_switch",
        }
        headers = loc.as_headers()
        postback: str | None = None
        emitted: set[str] = set()

        for _ in range(max_pages):
            body = dict(payload)
            if postback:
                body["request_type"] = "load_more"
                body["postback_params"] = postback
            data = (self.post("/web/hub_page", body, headers).get("data") or {})
            sections = data.get("sections") or []
            if not sections:
                return
            section = sections[0]
            fresh = 0
            for item in section.get("items") or []:
                sid = str(item.get("id") or "")
                if not sid or sid in emitted:
                    continue
                emitted.add(sid)
                fresh += 1
                yield item
            pagination = section.get("pagination") or {}
            if not pagination.get("hasMore") or not fresh:
                return
            postback = pagination.get("postbackParams")
            if not postback:
                return

    #: The gender facet District exposes on store listings. Only the hub store
    #: directory honours it on the public gateway -- the all-stores rail that
    #: also carries it is not served to web clients.
    GENDERS = ("Men", "Women", "Kids")

    @staticmethod
    def gender_filter(value: str) -> str:
        """The post_filters entry for one gender. post_filters is []string."""
        return json.dumps({
            "FilterType": "gender_filter",
            "FilterData": json.dumps(
                {"Value": value, "AnyGender": [value], "ID": f"gender_filter_{value}"}
            ),
            "Source": "FILTER_SOURCE_MODAL",
        })

    def hub_store_ids(
        self, hub_id: str, loc: Location, gender: str | None = None, max_pages: int = 60
    ) -> dict[str, str]:
        """{store_id: brand} for a hub, optionally filtered to one gender.

        Same endpoint and pagination as hub_stores. Set-differencing the three
        genders against the unfiltered list gives each brand's audience.
        """
        payload = {
            "hub_id": str(hub_id),
            "location": loc.as_payload(),
            "layout_type": "hub_categories_shops",
            "page_type": "hub_page",
            "request_type": "tab_switch",
        }
        if gender:
            payload["post_filters"] = [self.gender_filter(gender)]
        headers = loc.as_headers()
        postback: str | None = None
        found: dict[str, str] = {}

        for _ in range(max_pages):
            body = dict(payload)
            if postback:
                body["request_type"] = "load_more"
                body["postback_params"] = postback
            data = self.post("/web/hub_page", body, headers).get("data") or {}
            sections = data.get("sections") or []
            if not sections:
                return found
            section = sections[0]
            fresh = 0
            for item in section.get("items") or []:
                sid = str(item.get("id") or "")
                if sid and sid not in found:
                    found[sid] = (item.get("title") or "").strip()
                    fresh += 1
            pagination = section.get("pagination") or {}
            if not pagination.get("hasMore") or not fresh:
                return found
            postback = pagination.get("postbackParams")
            if not postback:
                return found
        return found

    def store_details(self, slug: str) -> dict[str, Any]:
        return self.post("/web/consumer/shopping/store/details", {"slug": slug})


def parse_store_details(slug: str, resp: dict[str, Any]) -> dict[str, Any]:
    """Flatten a store detail page into one row.

    The SEO block is the useful part: District states the brand, city and
    locality itself, and the JSON-LD carries the exact street address and
    coordinates -- so a store's locality never has to be inferred from its
    mall, and standalone high-street stores get a locality too.
    """
    meta = resp.get("metaData") or {}
    json_ld = ((resp.get("pageSeoData") or {}).get("jsonLd") or {})
    address = json_ld.get("address") or {}
    geo = json_ld.get("geo") or {}
    header = resp.get("headerData") or {}
    tags = [seg.get("text") for seg in (header.get("subtitle") or []) if seg.get("text")]

    def as_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return {
        "slug": slug,
        "brand": meta.get("seo_brand_name") or (header.get("title") or {}).get("text"),
        "city": meta.get("seo_city"),
        "locality": meta.get("seo_locality") or address.get("addressLocality"),
        "vertical": tags[0] if tags else None,
        "categories": " | ".join(tags[1:]),
        "street_address": address.get("streetAddress"),
        "postal_code": address.get("postalCode"),
        "region": address.get("addressRegion"),
        "lat": as_float(geo.get("latitude")),
        "lng": as_float(geo.get("longitude")),
        "url": json_ld.get("url"),
    }


# --------------------------------------------------------------------- parsing

#: Category tags that count as apparel/clothing, as they appear in a store's
#: `subTitle` ("Fashion | Apparel • Footwear") and `metaText`.
APPAREL_TAGS = {"apparel"}

#: Wider fashion set, for --categories fashion.
FASHION_TAGS = {
    "apparel",
    "footwear",
    "bags",
    "luggage",
    "accessories",
    "jewellery",
    "watches",
    "eyewear",
}


def parse_subtitle(subtitle: str | None) -> tuple[str | None, list[str]]:
    """"Fashion | Apparel • Accessories" -> ("Fashion", ["Apparel", "Accessories"])."""
    if not subtitle:
        return None, []
    if "|" in subtitle:
        vertical, _, rest = subtitle.partition("|")
        vertical = vertical.strip() or None
    else:
        vertical, rest = subtitle.strip(), ""
    tags = [t.strip() for t in rest.split("•") if t.strip()]
    if not tags and vertical and not rest:
        tags = []
    return vertical, tags


def parse_floor(meta_text: str | None) -> str | None:
    """"Floor 1 • Apparel" -> "Floor 1"."""
    if not meta_text:
        return None
    head = meta_text.split("•")[0].strip()
    return head or None


def store_matches(item: dict[str, Any], allowed_tags: set[str] | None) -> bool:
    """True if the store carries one of `allowed_tags`. None means keep everything."""
    if allowed_tags is None:
        return True
    _, tags = parse_subtitle(item.get("subTitle"))
    lowered = {t.lower() for t in tags}
    # metaText's trailing segment repeats the primary category; use it as a
    # fallback for the handful of items with an empty subTitle.
    meta = (item.get("metaText") or "").split("•")[-1].strip().lower()
    if meta:
        lowered.add(meta)
    return bool(lowered & allowed_tags)


#: Tokens that name a city or region rather than a locality.
CITY_TOKENS = {
    # Delhi NCR
    "gurgaon", "gurugram", "new delhi", "delhi", "noida", "greater noida",
    "ghaziabad", "faridabad", "south delhi", "west delhi", "east delhi",
    "north delhi", "central delhi", "delhi ncr", "haryana", "uttar pradesh",
    # Mumbai metropolitan region
    "mumbai", "navi mumbai", "thane", "thane west", "thane east",
    "mumbai suburban", "maharashtra",
    # Pune -- District calls the whole metro "Pune", so Pimpri and Chinchwad
    # are localities here, not cities.
    "pune", "pimpri chinchwad",
    # Hyderabad
    "hyderabad", "secunderabad", "telangana",
    # Bengaluru
    "bangalore", "bengaluru", "karnataka",
}

#: District writes some localities as "<direction> <city>" ("Central Bangalore",
#: "South Mumbai"), which is a zone, not a locality -- so those compounds count
#: as city tokens too.
DIRECTIONS = {"north", "south", "east", "west", "central"}
CITY_TOKENS |= {
    f"{d} {c}"
    for d in DIRECTIONS
    for c in ("delhi", "mumbai", "bangalore", "bengaluru", "pune", "hyderabad")
}

#: District uses both spellings for the same city.
CITY_ALIASES = {"gurugram": "gurgaon", "bengaluru": "bangalore"}


def norm_city(city: str | None) -> str:
    key = (city or "").strip().lower()
    return CITY_ALIASES.get(key, key)


def norm_locality(raw: str | None) -> str:
    """Reduce District's locality label to a bare locality.

    A mall store's locality reads "<mall>, <locality>" and a high-street
    store's reads "<locality>, <city>". Dropping city tokens and keeping the
    last remaining segment handles both:
        "Ambience Mall, DLF Phase 3"    -> "DLF Phase 3"
        "Sector 29, Gurugram"           -> "Sector 29"
        "MG Road Gurugram"              -> "MG Road"
        "Viviana Mall, Majiwada Thane West" -> "Majiwada"
        "Khan Market"                   -> "Khan Market"
    """
    if not raw:
        return ""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    kept = [p for p in parts if p.lower() not in CITY_TOKENS]
    chosen = kept[-1] if kept else (parts[-1] if parts else "")
    # A label that is nothing but a city name stays as-is -- stripping word by
    # word would turn "New Delhi" into "New".
    if chosen.lower() in CITY_TOKENS:
        return chosen
    # Some labels bury the city in the segment itself ("MG Road Gurugram",
    # "Majiwada Thane West"), which would otherwise split one locality in two.
    words = chosen.split()
    while len(words) > 1:
        if (len(words) > 2 and words[-1].lower() in DIRECTIONS
                and words[-2].lower() in CITY_TOKENS):
            words = words[:-2]
        elif words[-1].lower() in CITY_TOKENS:
            words.pop()
        else:
            break
    return " ".join(words)


def norm_address(address: str | None) -> str:
    """Normalise a street address for duplicate detection.

    District's duplicate listings differ only in whitespace (including NBSP),
    case, punctuation and a trailing ", India", so flattening those makes the
    real duplicates collide while leaving different shop numbers distinct.
    """
    if not address:
        return ""
    text = address.replace("\xa0", " ").lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for tail in (" india", " haryana", " delhi"):
        if text.endswith(tail):
            text = text[: -len(tail)].strip()
    return text


def store_fingerprint(store: dict[str, Any]) -> tuple:
    """Identity of a physical store: brand + coordinates + normalised address.

    Address is part of the key on purpose -- two outlets of one brand in the
    same mall share coordinates but differ by shop number, and those are
    separate stores, not duplicates.
    """
    return (
        (store.get("brand") or "").strip().lower(),
        store.get("lat"),
        store.get("lng"),
        norm_address(store.get("street_address")),
    )


def dedupe_stores(stores: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int, list[list[dict[str, Any]]]]:
    """Drop District's duplicate listings.

    Returns (kept, dropped_count, ambiguous_groups) where an ambiguous group is
    a set of surviving stores sharing brand and coordinates but not address --
    usually two real outlets in one mall, occasionally a duplicate with a
    retyped address. They are reported rather than merged.
    """
    seen: set[tuple] = set()
    kept: list[dict[str, Any]] = []
    dropped = 0
    for store in stores:
        fingerprint = store_fingerprint(store)
        if fingerprint in seen:
            dropped += 1
            continue
        seen.add(fingerprint)
        kept.append(store)

    by_point: dict[tuple, list[dict[str, Any]]] = {}
    for store in kept:
        key = (store_fingerprint(store)[0], store.get("lat"), store.get("lng"))
        if key[1] is None:
            continue
        by_point.setdefault(key, []).append(store)
    ambiguous = [group for group in by_point.values() if len(group) > 1]
    return kept, dropped, ambiguous


#: District leaves seo_city as the region for some stores instead of naming the
#: sub-city, which would split one locality across two "cities".
REGION_CITY = "delhi ncr"

#: Sub-city names as they appear inside a postal address, most specific first
#: so "Greater Noida" is not read as "Noida". Needed because District labels
#: *every* Noida store "Delhi NCR" -- there is no seo_city to vote with, so the
#: locality vote below can never reach them.
ADDRESS_SUBCITIES = (
    ("greater noida", "greater noida"),
    ("noida", "noida"),
    ("ghaziabad", "ghaziabad"),
    ("faridabad", "faridabad"),
    ("gurugram", "gurgaon"),
    ("gurgaon", "gurgaon"),
    ("new delhi", "new delhi"),
    ("delhi", "new delhi"),
)


def subcity_from_address(address: str | None) -> str | None:
    """The NCR sub-city named in a street address, if any."""
    text = (address or "").lower()
    for needle, city in ADDRESS_SUBCITIES:
        if needle in text:
            return city
    return None


def resolve_subcity(stores: list[dict[str, Any]]) -> dict[int, str]:
    """Assign a sub-city to stores whose city is just "Delhi NCR".

    A locality that appears under exactly one real sub-city elsewhere in the
    data (Sector 18 under Noida, say) is attributed to it; failing that, the
    sub-city named in the store's postal address is used. Anything neither
    settles keeps the region label. Returns {id(store): city} for the ones
    changed.
    """
    by_locality: dict[str, set[str]] = {}
    for store in stores:
        city = norm_city(store.get("city"))
        if not city or city == REGION_CITY:
            continue
        locality = norm_locality(store.get("locality"))
        if locality:
            by_locality.setdefault(locality, set()).add(city)

    fixed: dict[int, str] = {}
    for store in stores:
        if norm_city(store.get("city")) != REGION_CITY:
            continue
        candidates = by_locality.get(norm_locality(store.get("locality")) or "", set())
        if len(candidates) == 1:
            fixed[id(store)] = next(iter(candidates))
            continue
        from_address = subcity_from_address(store.get("street_address"))
        if from_address:
            fixed[id(store)] = from_address
    return fixed


#: Audience label from District's gender tags.
def gender_label(men: bool, women: bool, kids: bool) -> str:
    if men and women:
        return "Unisex"
    if men:
        return "Men"
    if women:
        return "Women"
    if kids:
        return "Kids"
    return "Unspecified"


def parse_latlng(maps_link: str | None) -> tuple[float | None, float | None]:
    """"https://www.google.com/maps?q=28.503571,77.097313" -> (28.503571, 77.097313)."""
    if not maps_link or "q=" not in maps_link:
        return None, None
    try:
        lat_s, _, lng_s = maps_link.split("q=", 1)[1].partition(",")
        return float(lat_s), float(lng_s.split("&")[0])
    except (ValueError, IndexError):
        return None, None


def parse_count(text: str | None, unit: str) -> int | None:
    """"148 Stores" / "12.3 km | 148 stores" -> 148."""
    if not text:
        return None
    tokens = text.lower().replace("|", " ").split()
    for i, tok in enumerate(tokens):
        if tok.startswith(unit) and i:
            try:
                return int(tokens[i - 1].replace(",", ""))
            except ValueError:
                return None
    return None
