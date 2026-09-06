"""In-memory negative cache for drift codes and challenges (PLAN P6.1, SPEC §7).

`payload_missing`, `marker_missing`, `reliability_payload_missing` and `challenged` are cached
for one hour so a broken category does not cost an 11 MB fetch on every call. `fetch_failed` is
NEVER cached here — an offline laptop that comes back should work on the next call.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .config import NEGATIVE_TTL_S

NEGATIVE_CODES = frozenset(
    {"payload_missing", "marker_missing", "reliability_payload_missing", "challenged"}
)


class NegativeCache:
    def __init__(self, ttl_s: float = NEGATIVE_TTL_S) -> None:
        self.ttl = timedelta(seconds=ttl_s)
        self._entries: dict[Any, tuple[str, datetime, dict]] = {}

    def put(self, key: Any, code: str, now: datetime, **detail: Any) -> None:
        if code not in NEGATIVE_CODES:
            raise ValueError(f"{code!r} is not negatively cacheable (fetch_failed never is)")
        self._entries[key] = (code, now, detail)

    def get(self, key: Any, now: datetime) -> tuple[str, dict] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        code, at, detail = entry
        if now - at >= self.ttl:
            del self._entries[key]
            return None
        return code, detail

    def clear(self, key: Any | None = None) -> None:
        if key is None:
            self._entries.clear()
        else:
            self._entries.pop(key, None)
