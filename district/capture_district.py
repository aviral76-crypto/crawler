"""
mitmproxy addon: dump District API traffic to disk.

    mitmdump -s capture_district.py

Writes one file per API call into data/captured/, named
<seq>_<path>_<layout_type>.json, each holding the request body, response body and
the headers that matter. Use it to recover the endpoints the public web client is
not served -- notably the paginated all-stores listing and the store_id-based
store lookup.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

OUT = Path(__file__).resolve().parent / "data" / "captured"
HOSTS = ("district.in", "edition.in")
INTERESTING_HEADERS = (
    "x-city-id", "x-pcity-id", "x-user-lat", "x-user-lng", "x-z-zone-id",
    "x-z-subzone-id", "x-app-type", "x-app-version", "x-client-id",
    "x-guest-token", "x-device-id", "content-type",
)

_seq = 0


def _decode(raw: bytes | None) -> object:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        # Protobuf or gzip that mitmproxy did not unwrap; keep it as text so the
        # call is still visible in the dump.
        return {"__raw__": raw[:4000].decode("utf-8", "replace")}


def _label(flow) -> str:
    body = _decode(flow.request.raw_content)
    hint = ""
    if isinstance(body, dict):
        for key in ("layout_type", "page_type", "hub_id", "slug", "store_id", "keyword"):
            if body.get(key):
                hint = f"_{key}-{body[key]}"
                break
    path = re.sub(r"[^A-Za-z0-9]+", "-", flow.request.path.split("?")[0]).strip("-")
    return f"{path[:80]}{hint}"[:150]


def response(flow) -> None:
    global _seq
    if not any(h in flow.request.pretty_host for h in HOSTS):
        return
    if "/gw/" not in flow.request.path and "/api" not in flow.request.path:
        return

    _seq += 1
    OUT.mkdir(parents=True, exist_ok=True)
    record = {
        "method": flow.request.method,
        "url": flow.request.pretty_url,
        "request_headers": {
            k: v for k, v in flow.request.headers.items() if k.lower() in INTERESTING_HEADERS
        },
        "request_body": _decode(flow.request.raw_content),
        "status": flow.response.status_code,
        "response_body": _decode(flow.response.raw_content),
    }
    path = OUT / f"{_seq:04d}_{_label(flow)}.json"
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False))
    print(f"[district] {flow.response.status_code} {flow.request.path.split('?')[0]} -> {path.name}")
