"""Pure byte-to-structure extraction (PLAN P2.1). No I/O, no wafer types.

Anchors (RECON §10e–§10g, §11a, §13d):
- `window.filterInstanceDATA = {…};\\n` — the category payload
- `console.log('[ ratings-wrapper ]', {…})` — the attribute dictionary, in the same <script>.
  A debug statement left in production: the most fragile anchor in the project.
- `data-subscriber="true|false"` — the products auth marker; "Sign Out" is NEVER used (RECON §5)
- `window.initStore = {…}` — the reliability payload
- `"DRS_CARS_API_API_KEY":"…"` and `window.isSubscriber = true|false` — the car page
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass

FILTER_INSTANCE_ANCHOR = "window.filterInstanceDATA"
RATINGS_WRAPPER_ANCHOR = "console.log('[ ratings-wrapper ]',"
INIT_STORE_ANCHOR = "window.initStore"
MARKER_TRUE = b'data-subscriber="true"'
MARKER_FALSE = b'data-subscriber="false"'
FILTER_INSTANCE_KEYS = ("filters", "data", "args", "attrs")

# JSON strings are skipped as whole tokens, so braces inside them never count. The `;\n`
# terminator RECON §10e measured after filterInstanceDATA is deliberately NOT relied on: the
# object is brace-matched, which survives a terminator change.
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[{}]')
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_WS = re.compile(r"\s+")
_CARS_KEY = re.compile(r'"DRS_CARS_API_API_KEY"\s*:\s*"([^"]{10,60})"')
_CARS_KEY_ESCAPED = re.compile(r'\\"DRS_CARS_API_API_KEY\\"\s*:\s*\\"([^"\\]{10,60})\\"')
_IS_SUBSCRIBER = re.compile(r"window\.isSubscriber\s*=\s*(true|false)")


def _text(html: bytes | str) -> str:
    if isinstance(html, bytes):
        return html.decode("utf-8", "replace")
    return html


def brace_match(s: str, start: int) -> int:
    """Return the index one past the `}` that closes the `{` at `s[start]`.

    String- and escape-aware for JSON strings. Raises ValueError when `s[start]` is not `{` or
    the object never closes.
    """
    if start < 0 or start >= len(s) or s[start] != "{":
        raise ValueError("brace_match: start is not an opening brace")
    depth = 0
    for m in _TOKEN.finditer(s, start):
        tok = m.group(0)
        if tok == "{":
            depth += 1
        elif tok == "}":
            depth -= 1
            if depth == 0:
                return m.end()
    raise ValueError("brace_match: unbalanced braces")


def _object_after(text: str, anchor: str, *, require_assignment: bool) -> dict | None:
    at = text.find(anchor)
    if at < 0:
        return None
    pos = at + len(anchor)
    if require_assignment:
        eq = text.find("=", pos, pos + 64)
        if eq < 0:
            return None
        pos = eq + 1
    start = text.find("{", pos, pos + 64)
    if start < 0:
        return None
    try:
        end = brace_match(text, start)
        obj = json.loads(text[start:end])
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def extract_filter_instance(html: bytes | str) -> dict | None:
    """`window.filterInstanceDATA` as a dict; None when absent or unparseable (payload_missing)."""
    return _object_after(_text(html), FILTER_INSTANCE_ANCHOR, require_assignment=True)


def filter_instance_is_complete(fi: dict | None) -> bool:
    if not isinstance(fi, dict):
        return False
    return all(k in fi for k in FILTER_INSTANCE_KEYS) and isinstance(fi["data"], dict | list)


def extract_ratings_wrapper(html: bytes | str) -> dict | None:
    """The object passed to `console.log('[ ratings-wrapper ]', …)`, or None."""
    return _object_after(_text(html), RATINGS_WRAPPER_ANCHOR, require_assignment=False)


def extract_init_store(html: bytes | str) -> dict | None:
    """`window.initStore` as a dict, or None (→ reliability_payload_missing)."""
    return _object_after(_text(html), INIT_STORE_ANCHOR, require_assignment=True)


def read_subscriber_marker(html: bytes | str) -> str | None:
    """ "true" | "false" | None. Both values present is None too (D18: never a pick)."""
    raw = html if isinstance(html, bytes) else html.encode("utf-8", "replace")
    has_true = MARKER_TRUE in raw
    has_false = MARKER_FALSE in raw
    if has_true and has_false:
        return None
    if has_true:
        return "true"
    if has_false:
        return "false"
    return None


def read_title(html: bytes | str, *, limit: int = 200) -> str | None:
    """The page <title> from the first 64 KB, entity-unescaped, whitespace-collapsed, capped."""
    head = _text(html[: 64 * 1024])
    m = _TITLE.search(head)
    if not m:
        return None
    title = _WS.sub(" ", html_lib.unescape(m.group(1))).strip()
    return title[:limit] if title else None


@dataclass(frozen=True)
class CarsPageInfo:
    api_key: str | None
    is_subscriber: bool | None


def extract_cars_page(html: bytes | str) -> CarsPageInfo:
    """The public `x-api-key` and the `window.isSubscriber` marker from a car page (RECON §13d)."""
    text = _text(html)
    m = _CARS_KEY.search(text) or _CARS_KEY_ESCAPED.search(text)
    key = m.group(1) if m else None
    s = _IS_SUBSCRIBER.search(text)
    is_subscriber = None if s is None else s.group(1) == "true"
    return CarsPageInfo(api_key=key, is_subscriber=is_subscriber)
