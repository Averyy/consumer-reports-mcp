"""P6.1 — the one-hour in-memory negative cache (SPEC §7 *Partial and empty payloads*)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from consumer_reports_mcp.negcache import NegativeCache

T0 = datetime(2026, 9, 3, tzinfo=UTC)


def test_expires_after_hour():
    nc = NegativeCache()
    nc.put(("cat", 37162, "anonymous"), "payload_missing", T0, http_status=200, title="Maint")
    assert nc.get(("cat", 37162, "anonymous"), T0 + timedelta(minutes=59)) == (
        "payload_missing",
        {"http_status": 200, "title": "Maint"},
    )
    assert nc.get(("cat", 37162, "anonymous"), T0 + timedelta(hours=1)) is None
    assert nc.get(("other",), T0) is None


@pytest.mark.parametrize("code", ["fetch_failed", "session_expired", "unknown_category", "x"])
def test_rejects_fetch_failed(code):
    nc = NegativeCache()
    with pytest.raises(ValueError):
        nc.put("k", code, T0)


def test_accepts_all_drift_codes_and_clear():
    nc = NegativeCache()
    for code in ("payload_missing", "marker_missing", "reliability_payload_missing", "challenged"):
        nc.put(code, code, T0)
        assert nc.get(code, T0)[0] == code
    nc.clear("challenged")
    assert nc.get("challenged", T0) is None
    nc.clear()
    assert nc.get("payload_missing", T0) is None
