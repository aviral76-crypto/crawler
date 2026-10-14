#!/usr/bin/env python3
"""Pull the text out of a PDF, with no dependencies.

    python3 pdftext.py filing.pdf                  # all of it
    python3 pdftext.py filing.pdf "U.S. Polo"      # only lines matching a term

Written because the filings this project needs -- investor presentations and
annual reports -- are PDFs, and the box has neither poppler nor a PDF library.
It handles what those files actually are: FlateDecode content streams holding
text-showing operators. That is enough to read numbers off a results deck; it
is not a general PDF renderer and makes no attempt at layout, tables or
embedded fonts with custom encodings.
"""

from __future__ import annotations

import re
import sys
import zlib
from pathlib import Path

#: A PDF string is (literal) or <hex>; these are the operators that draw one.
SHOW = re.compile(rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]+>")


def streams(raw: bytes):
    """Every content stream in the file, inflated where it is deflated."""
    for match in re.finditer(rb"stream\r?\n", raw):
        start = match.end()
        end = raw.find(b"endstream", start)
        if end == -1:
            continue
        blob = raw[start:end]
        try:
            yield zlib.decompress(blob)
        except zlib.error:
            try:                                   # some writers pad the stream
                yield zlib.decompressobj().decompress(blob)
            except zlib.error:
                yield blob                         # not compressed at all


def unescape(token: bytes) -> str:
    if token.startswith(b"<"):
        hexed = re.sub(rb"\s+", b"", token[1:-1])
        if len(hexed) % 2:
            hexed += b"0"
        text = bytes.fromhex(hexed.decode("ascii", "ignore"))
        # Hex strings in these decks are usually UTF-16BE.
        return text.decode("utf-16-be", "replace") if b"\x00" in text else \
            text.decode("latin-1", "replace")
    body = token[1:-1]
    body = re.sub(rb"\\([nrtbf()\\])", lambda m: {
        b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"", b"f": b"",
        b"(": b"(", b")": b")", b"\\": b"\\"}[m.group(1)], body)
    body = re.sub(rb"\\([0-7]{1,3})", lambda m: bytes([int(m.group(1), 8) & 0xFF]), body)
    return body.decode("latin-1", "replace")


def readable(line: str) -> bool:
    """Is this line real text, or a custom-encoded font coming out as noise?

    A designed annual report subsets its fonts and remaps the glyph codes, so
    the bytes in its content stream are not the characters on the page. Such a
    line decodes to mostly accented and control characters; refusing it is
    better than printing noise a reader might mistake for data.
    """
    if not line.strip():
        return False
    good = sum(c.isascii() and (c.isprintable() or c.isspace()) for c in line)
    return good / len(line) > 0.85


def text_of(path: str | Path) -> str:
    raw = Path(path).read_bytes()
    out: list[str] = []
    dropped = 0
    for stream in streams(raw):
        if b"Tj" not in stream and b"TJ" not in stream:
            continue
        for line in stream.split(b"\n"):
            if b"Tj" not in line and b"TJ" not in line:
                continue
            shown = "".join(unescape(t) for t in SHOW.findall(line))
            if not shown.strip():
                continue
            if readable(shown):
                out.append(shown)
            else:
                dropped += 1
    text = "\n".join(out)
    # A subset font can also remap glyphs to *printable* ASCII, so the output
    # looks like text but is shifted nonsense ("&RUSRUDWH" for "Corporate").
    # Real English prose from a filing contains these words many times over.
    hits = sum(len(re.findall(rf"\b{w}\b", text, re.I))
               for w in ("the", "and", "total", "revenue", "limited", "year"))
    if dropped > len(out) or (len(text) > 2000 and hits < 10):
        raise SystemExit(
            f"{Path(path).name}: text is custom-encoded and cannot be read "
            "without the embedded fonts (a designed annual report usually is). "
            "Use the company's results presentation, which is normally plain, "
            "or a real PDF renderer.")
    return text


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    text = text_of(sys.argv[1])
    if len(sys.argv) > 2:
        term = sys.argv[2].lower()
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if term in line.lower():
                lo, hi = max(0, i - 2), min(len(lines), i + 3)
                print(" / ".join(x.strip() for x in lines[lo:hi] if x.strip()))
    else:
        print(text)


if __name__ == "__main__":
    main()
