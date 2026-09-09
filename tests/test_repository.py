"""P6.2 / P6.3 — cache-first orchestration with the auth-state machine (SPEC §7, §8)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import wafer

from consumer_reports_mcp import ingest
from consumer_reports_mcp.cache import Cache, to_iso
from consumer_reports_mcp.config import WWW, Settings
from consumer_reports_mcp.credentials import CredentialStore, SessionHealth, SessionState
from consumer_reports_mcp.discovery import Discovery
from consumer_reports_mcp.negcache import NegativeCache
from consumer_reports_mcp.repository import Repository, Served, ToolError
from consumer_reports_mcp.transport import Transport
from tests.conftest import (
    FakeResponse,
    FakeWaferSession,
    fixture_envelope,
    make_category_page,
    make_login_page,
    make_maintenance_page,
    make_reliability_page,
    until,
)

T0 = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
HASH = "h" * 36
CAT_PATH = "/appliances/refrigerators/french-door-refrigerator/c37162/"
CAT_URL = WWW + CAT_PATH
REL_URL = WWW + "/appliances/refrigerators/french-door-refrigerator/reliability/c37162/"
LOGIN_URL = "https://secure.consumerreports.org/ec/login?error"
INDEX_ROWS = [
    {"id": 37162, "path": CAT_PATH, "display_name": "French-Door Refrigerators"},
    {
        "id": 28722,
        "path": "/appliances/refrigerators/top-freezer-refrigerator/c28722/",
        "display_name": "Top-Freezer Refrigerators",
    },
    {"id": 37154, "path": "/money/banks-credit-unions/banks/c37154/", "display_name": "Banks"},
    {"id": 200228, "path": "/health/milk-milk-alternatives/c200228/", "display_name": "Plant Milk"},
    {"id": 200358, "path": "/health/milk-milk-alternatives/almond-milk/c200358/"},
]
SITEMAP_ROWS = [{"id": 33007, "path": "/home-garden/retired-thing/c33007/"}]


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class Harness:
    def __init__(self, tmp_path, *, cookie: bool, index: bool = True):
        self.settings = Settings(env={"CR_MIN_REQUEST_INTERVAL_S": "0"}, home=tmp_path)
        self.store = CredentialStore(tmp_path / "session.json", env={})
        if cookie:
            self.store.save({"hash": HASH, "userLicenses": "old"})
        self.health = SessionState(self.store.configured)
        self.sess = FakeWaferSession()
        self.transport = Transport(
            self.settings, self.store, self.health, session_factory=lambda **kw: self.sess
        )
        self.cache = Cache(tmp_path / "cr.db")
        self.discovery = Discovery(self.cache, self.transport, 90)
        self.clock = Clock()
        self.negcache = NegativeCache()
        if index:
            self.cache.upsert_category_index(INDEX_ROWS, "az", T0)
            self.cache.upsert_category_index(SITEMAP_ROWS, "sitemap", T0)
            self.cache.record_discovery_run("az", T0, len(INDEX_ROWS))
            self.cache.record_discovery_run("sitemap", T0, len(INDEX_ROWS))
        self.repo = Repository(
            self.settings,
            self.cache,
            self.transport,
            self.discovery,
            self.negcache,
            self.store,
            self.health,
            clock=self.clock,
        )

    def page(self, fixture, **kw) -> FakeResponse:
        url = kw.pop("url", CAT_URL)
        return FakeResponse(
            url=url,
            content=make_category_page(fixture, **kw),
            **{k: v for k, v in kw.items() if k in ("history", "set_cookies", "clear_cookies")},
        )

    def seed(self, env, *, tier, scored, age_days):
        return self.cache.write_category(
            env, tier=tier, scored=scored, fetched_at=T0 - timedelta(days=age_days)
        )

    @property
    def requests(self):
        return [u for u, _ in self.sess.requests]


def _page(fixture, url=CAT_URL, **kw) -> FakeResponse:
    extra = {k: kw.pop(k) for k in ("history", "set_cookies", "clear_cookies") if k in kw}
    return FakeResponse(url=url, content=make_category_page(fixture, **kw), **extra)


# --------------------------------------------------------------------------- P6.2


@pytest.mark.parametrize(
    ("cookie", "subscriber", "fill", "data_tier", "session", "cached_tier"),
    [
        (False, "false", False, "anonymous", "none", "anonymous"),
        (False, "false", True, "anonymous", "none", "anonymous"),
        (True, "true", False, "member", "active", "member"),
        (True, "true", True, "member", "active", "member"),
        (True, "false", False, "anonymous", "expired", "anonymous"),
    ],
)
async def test_auth_state_matrix_end_to_end(
    tmp_path, c37162, cookie, subscriber, fill, data_tier, session, cached_tier
):
    h = Harness(tmp_path, cookie=cookie)
    h.sess.push(_page(c37162, subscriber=subscriber, fill_scores=fill))
    out = await h.repo.get_category("c37162")
    assert isinstance(out, Served), out
    assert out.data_tier == data_tier and out.session == session
    assert out.from_cache is False and out.stale is False and out.warnings == []
    assert out.cr_url == CAT_URL and out.fetched_at == to_iso(T0)
    row = h.cache.select_category(37162, "anonymous", 30, T0)
    assert row.tier == cached_tier and row.scored is fill
    assert len(h.requests) == 1


async def test_session_expired_page_is_served_as_data_with_error_null(tmp_path, c37162):
    h = Harness(tmp_path, cookie=True)
    h.sess.push(_page(c37162, subscriber="false"))
    out = await h.repo.get_category(37162)
    assert isinstance(out, Served)
    assert out.data_tier == "anonymous" and out.session == "expired"
    assert out.fetch_kind == ingest.SESSION_EXPIRED
    assert len(out.envelope["filter_instance"]["data"]) == 8  # the catalogue is real data


async def test_credential_rejected_falls_back_to_cache_then_anonymous_retry(tmp_path, c37162):
    h = Harness(tmp_path, cookie=True)
    h.sess.push(
        FakeResponse(url=LOGIN_URL, content=make_login_page(), clear_cookies=("hash",)),
        _page(c37162, subscriber="false"),
    )
    out = await h.repo.get_category("c37162")
    assert isinstance(out, Served)
    assert out.data_tier == "anonymous" and out.session == "expired"
    assert h.store.rejected is True and h.health.health is SessionHealth.EXPIRED
    assert h.requests == [CAT_URL, CAT_URL]
    with h.cache._connect() as conn:
        rows = conn.execute("SELECT auth_tier, payload_json FROM category_raw").fetchall()
    assert len(rows) == 1 and rows[0]["auth_tier"] == "anonymous"
    assert "Sign In" not in rows[0]["payload_json"]  # nothing from the login response cached


async def test_dead_cookie_costs_one_probe_then_hits_anonymous_rows(tmp_path, c37162):
    h = Harness(tmp_path, cookie=True)
    h.seed(fixture_envelope(c37162), tier="anonymous", scored=False, age_days=1)
    h.sess.push(_page(c37162, subscriber="false"))
    first = await h.repo.get_category(37162)  # effective member → miss → probe → expired
    assert isinstance(first, Served) and first.session == "expired"
    assert len(h.requests) == 1
    second = await h.repo.get_category(37162)  # effective anonymous now → hit
    assert isinstance(second, Served) and second.from_cache is True
    assert second.data_tier == "anonymous" and second.session == "expired"
    assert len(h.requests) == 1  # request log unchanged


async def test_unverified_member_row_hit_makes_no_request(tmp_path, c37162):
    h = Harness(tmp_path, cookie=True)
    h.seed(fixture_envelope(c37162, fill_scores=True), tier="member", scored=True, age_days=3)
    out = await h.repo.get_category("french-door-refrigerator")
    assert isinstance(out, Served) and out.from_cache is True
    assert out.data_tier == "member" and out.session == "unverified"
    assert h.requests == []


async def test_unverified_anonymous_row_is_a_miss(tmp_path, c37162):
    h = Harness(tmp_path, cookie=True)
    h.seed(fixture_envelope(c37162), tier="anonymous", scored=False, age_days=1)
    h.sess.push(_page(c37162, subscriber="true", fill_scores=True))
    out = await h.repo.get_category(37162)
    assert isinstance(out, Served) and out.data_tier == "member" and out.session == "active"
    assert len(h.requests) == 1


async def test_single_flight_two_concurrent_calls_one_fetch(tmp_path, c37162):
    h = Harness(tmp_path, cookie=False)

    async def slow(url, kw):
        await asyncio.sleep(0.02)
        return _page(c37162)

    h.sess.route(CAT_URL, slow)
    a, b = await asyncio.gather(h.repo.get_category(37162), h.repo.get_category("c37162"))
    assert isinstance(a, Served) and isinstance(b, Served) and a.rowid == b.rowid
    assert len(h.requests) == 1


async def test_a_sign_in_adopted_mid_category_fetch_is_retried_once_not_surfaced(tmp_path, c37162):
    """The likely real sequence: sign-in starts, the user takes a minute, the agent runs an
    11 MB ratings fetch, validation completes mid-fetch. The straddled response is
    `policy_changed` — our own state change, not a network condition — and used to reach the
    caller as `fetch_failed`. One retry, under the new policy, serves the member row."""
    h = Harness(tmp_path, cookie=False)
    gate = asyncio.Event()

    async def slow(url, kw):
        await gate.wait()
        if h.transport.cookie_configured:  # the retry goes out under the adopted cookie
            return _page(c37162, subscriber="true", fill_scores=True)
        return _page(c37162, subscriber="false")

    h.sess.route(CAT_URL, slow)
    inflight = asyncio.ensure_future(h.repo.get_category(37162))
    await until(lambda: h.requests)
    assert h.requests == [CAT_URL]  # on the wire, anonymously
    h.store.save({"hash": HASH})
    await h.transport.adopt()
    gate.set()
    out = await inflight
    assert isinstance(out, Served), out
    assert out.data_tier == "member" and out.session == "active" and out.from_cache is False
    assert h.requests == [CAT_URL, CAT_URL]
    assert h.health.health is SessionHealth.ACTIVE and h.store.rejected is False
    # exactly one retry: a second `policy_changed` surfaces, retryable, never cached
    h2 = Harness(tmp_path / "b", cookie=False)
    attempts: list[str] = []
    h2.sess.route(
        CAT_URL,
        lambda url, kw: (
            attempts.append(url),
            _fetch_failed("policy_changed", url),
        )[1],
    )
    out2 = await h2.repo.get_category(37162)
    assert isinstance(out2, ToolError) and out2.code == "fetch_failed"
    assert out2.reason == "policy_changed" and out2.retryable is True and len(attempts) == 2
    assert not h2.cache.has_rows(37162)
    # any other reason is not retried
    attempts.clear()
    h2.sess.route(
        CAT_URL,
        lambda url, kw: (attempts.append(url), _fetch_failed("timeout", url))[1],
    )
    out3 = await h2.repo.get_category(37162)
    assert isinstance(out3, ToolError) and out3.reason == "timeout" and len(attempts) == 1


def _fetch_failed(reason: str, url: str):
    from consumer_reports_mcp.transport import FetchFailed

    return FetchFailed(reason, retryable=True, url=url)


async def test_a_sign_in_adopted_mid_reliability_fetch_is_retried_once(
    tmp_path, reliability_fixture
):
    h = Harness(tmp_path, cookie=False)
    gate = asyncio.Event()
    attempts: list[str] = []

    async def slow(url, kw):
        attempts.append(url)
        if len(attempts) == 1:
            await gate.wait()
        return FakeResponse(url=url, content=make_reliability_page(reliability_fixture))

    h.sess.route(REL_URL, slow)
    inflight = asyncio.ensure_future(h.repo.get_reliability(37162))
    await until(lambda: attempts)
    assert attempts == [REL_URL]
    h.store.save({"hash": HASH})
    await h.transport.adopt()
    gate.set()
    out = await inflight
    assert not isinstance(out, ToolError), out
    assert out.from_cache is False and attempts == [REL_URL, REL_URL]
    assert h.health.health is SessionHealth.UNVERIFIED  # reliability never moves health


async def test_refresh_appends_never_promotes(tmp_path, c37162):
    h = Harness(tmp_path, cookie=False)
    h.seed(fixture_envelope(c37162, fill_scores=True), tier="member", scored=True, age_days=10)
    h.sess.push(_page(c37162, subscriber="false"))
    out = await h.repo.get_category(37162, refresh=True)
    assert isinstance(out, Served)
    assert out.data_tier == "member" and out.fetched_at == to_iso(T0 - timedelta(days=10))
    assert out.superseded_at == to_iso(T0) and out.from_cache is True
    assert out.envelope["filter_instance"]["data"]["500001"]["overallDisplayScore"] is not None
    assert len(h.requests) == 1


async def test_drift_negative_cached_then_stale_row_served_with_refresh_failed(tmp_path, c37162):
    h = Harness(tmp_path, cookie=True)
    h.seed(fixture_envelope(c37162, fill_scores=True), tier="member", scored=True, age_days=40)
    h.sess.push(FakeResponse(url=CAT_URL, content=make_maintenance_page("Maintenance")))
    out = await h.repo.get_category(37162)
    assert isinstance(out, Served) and out.stale is True
    assert out.warnings == ["refresh_failed:payload_missing"] and out.data_tier == "member"
    assert h.health.health is SessionHealth.UNVERIFIED  # drift never touches session health
    again = await h.repo.get_category(37162)
    assert isinstance(again, Served) and again.warnings == ["refresh_failed:payload_missing"]
    assert len(h.requests) == 1  # negatively cached for an hour
    h.clock.now = T0 + timedelta(hours=2)
    h.sess.push(_page(c37162, subscriber="true", fill_scores=True))
    later = await h.repo.get_category(37162)
    assert isinstance(later, Served) and later.warnings == [] and len(h.requests) == 2


async def test_drift_with_nothing_cached_is_a_structured_error(tmp_path):
    h = Harness(tmp_path, cookie=False)
    h.sess.push(FakeResponse(url=CAT_URL, status_code=200, content=make_maintenance_page("Maint")))
    out = await h.repo.get_category(37162)
    assert isinstance(out, ToolError)
    assert out.code == "payload_missing" and out.http_status == 200 and out.title == "Maint"
    d = out.as_dict()
    assert d["code"] == "payload_missing" and "retryable" in d


async def test_marker_missing_is_distinct_from_payload_missing(tmp_path, c37162):
    h = Harness(tmp_path, cookie=False)
    h.sess.push(_page(c37162, subscriber=None))
    out = await h.repo.get_category(37162)
    assert isinstance(out, ToolError) and out.code == "marker_missing"


async def test_fetch_failed_not_negatively_cached(tmp_path):
    h = Harness(tmp_path, cookie=False)
    h.sess.push(wafer.ConnectionFailed(CAT_URL, "offline"), wafer.ConnectionFailed(CAT_URL, "x"))
    a = await h.repo.get_category(37162)
    b = await h.repo.get_category(37162)
    assert isinstance(a, ToolError) and a.code == "fetch_failed"
    assert a.reason == "connection_failed" and a.retryable is True
    assert isinstance(b, ToolError) and len(h.requests) == 2  # fetched again


async def test_fetch_failed_with_row_serves_row(tmp_path, c37162):
    h = Harness(tmp_path, cookie=False)
    h.seed(fixture_envelope(c37162), tier="anonymous", scored=False, age_days=45)
    h.sess.push(wafer.WaferTimeout(CAT_URL, 120.0))
    out = await h.repo.get_category(37162)
    assert isinstance(out, Served) and out.stale and out.warnings == ["refresh_failed:fetch_failed"]


async def test_challenged_is_negatively_cached_and_surfaced(tmp_path):
    h = Harness(tmp_path, cookie=True)
    h.sess.push(FakeResponse(url=CAT_URL, status_code=403, content=b"<html>blocked</html>"))
    a = await h.repo.get_category(37162)
    b = await h.repo.get_category(37162)
    assert isinstance(a, ToolError) and a.code == "challenged" and a.http_status == 403
    assert a.retryable is False and isinstance(b, ToolError) and len(h.requests) == 1


async def test_unknown_id_before_fetch_makes_no_request(tmp_path):
    h = Harness(tmp_path, cookie=False)
    out = await h.repo.get_category("c99999")
    assert isinstance(out, ToolError) and out.code == "unknown_category"
    assert out.reason == "not_in_index" and h.requests == []
    out2 = await h.repo.get_category("mattresses")
    assert isinstance(out2, ToolError) and out2.code == "unknown_category"


async def test_ambiguous_slug_lists_candidates(tmp_path):
    h = Harness(tmp_path, cookie=False)
    h.cache.upsert_category_index(
        [
            {"id": 1001, "path": "/a/interior-paint/c1001/"},
            {"id": 1002, "path": "/b/interior-paint/c1002/"},
        ],
        "sitemap",
        T0,
    )
    out = await h.repo.get_category("interior-paint")
    assert isinstance(out, ToolError) and out.code == "ambiguous_category"
    assert [c["id"] for c in out.candidates] == [1001, 1002] and h.requests == []


async def test_az_404_is_drift_not_retired(tmp_path):
    h = Harness(tmp_path, cookie=False)
    h.sess.push(FakeResponse(url=CAT_URL, status_code=404, content=b"<title>Not Found</title>"))
    out = await h.repo.get_category(37162)
    assert isinstance(out, ToolError) and out.code == "payload_missing"
    assert out.http_status == 404 and h.cache.index_row(37162)["dead_at"] is None
    again = await h.repo.get_category(37162)
    assert isinstance(again, ToolError) and len(h.requests) == 1  # negatively cached


async def test_double_rejection_falls_back_to_cached_row(tmp_path, c37162):
    h = Harness(tmp_path, cookie=True)
    h.seed(fixture_envelope(c37162, fill_scores=True), tier="member", scored=True, age_days=40)
    h.sess.push(
        FakeResponse(url=LOGIN_URL, content=make_login_page()),
        FakeResponse(url=LOGIN_URL, content=make_login_page()),
    )
    out = await h.repo.get_category(37162)
    assert isinstance(out, Served) and out.data_tier == "member"
    assert out.warnings == ["refresh_failed:credential_rejected"] and out.session == "expired"
    assert h.requests == [CAT_URL, CAT_URL]
    # and with nothing cached it is a structured error, never a drift alarm
    h2 = Harness(tmp_path / "b", cookie=True)
    h2.sess.push(
        FakeResponse(url=LOGIN_URL, content=make_login_page()),
        FakeResponse(url=LOGIN_URL, content=make_login_page()),
    )
    err = await h2.repo.get_category(37162)
    assert isinstance(err, ToolError) and err.code == "credential_rejected"
    assert err.retryable is False and "auth" in err.message
    assert h2.negcache.get(("cat", 37162), T0) is None


async def test_get_product_refresh_failure_serves_old_row_as_cached(tmp_path, c37162):
    h = Harness(tmp_path, cookie=False)
    h.seed(fixture_envelope(c37162), tier="anonymous", scored=False, age_days=2)
    h.sess.push(wafer.ConnectionFailed(CAT_URL, "offline"))
    out = await h.repo.get_product(500001, refresh=True)
    assert isinstance(out, Served) and out.from_cache is True
    assert out.warnings == ["refresh_failed:fetch_failed"]


async def test_sitemap_404_marks_dead(tmp_path):
    h = Harness(tmp_path, cookie=False)
    h.sess.push(
        FakeResponse(
            url=WWW + "/home-garden/retired-thing/c33007/",
            status_code=404,
            content=b"<title>Not Found</title>",
        )
    )
    out = await h.repo.get_category(33007)
    assert isinstance(out, ToolError) and out.code == "unknown_category" and out.reason == "retired"
    assert h.cache.index_row(33007)["dead_at"] == to_iso(T0)
    again = await h.repo.get_category("c33007")
    assert isinstance(again, ToolError) and again.reason == "retired" and len(h.requests) == 1


async def test_subcategory_id_files_under_args_cid_and_aliases(tmp_path, c200228):
    h = Harness(tmp_path, cookie=False)
    parent_url = WWW + "/health/milk-milk-alternatives/c200228/"
    h.sess.route(
        WWW + "/health/milk-milk-alternatives/almond-milk/c200358/",
        FakeResponse(
            url=parent_url,
            content=make_category_page(c200228),
            history=[(301, WWW + "/health/milk-milk-alternatives/almond-milk/c200358/")],
        ),
    )
    out = await h.repo.get_category("c200358")
    assert isinstance(out, Served) and out.category_id == 200228
    assert out.envelope["requested_id"] == 200358 and out.cr_url == parent_url
    assert h.cache.resolve_category(200358).via_alias
    hit = await h.repo.get_category(200358)
    assert isinstance(hit, Served) and hit.from_cache and len(h.requests) == 1
    hit2 = await h.repo.get_category(200228)
    assert isinstance(hit2, Served) and hit2.from_cache and len(h.requests) == 1


async def test_index_unavailable_is_fetch_failed_not_unknown(tmp_path):
    h = Harness(tmp_path, cookie=False, index=False)
    h.sess.push(wafer.ConnectionFailed(WWW, "offline"))
    out = await h.repo.get_category("c37162")
    assert isinstance(out, ToolError) and out.code == "fetch_failed"
    assert out.reason == "connection_failed" and out.retryable is True
    assert h.requests == [f"{WWW}/cro/a-to-z-index/products/index.htm"]


async def test_unknown_before_sitemap_pass_is_structurally_incomplete(tmp_path):
    h = Harness(tmp_path, cookie=False, index=False)
    h.cache.upsert_category_index(INDEX_ROWS, "az", T0)
    h.cache.record_discovery_run("az", T0, len(INDEX_ROWS))  # no sitemap run recorded
    out = await h.repo.get_category("c99999")
    assert isinstance(out, ToolError) and out.code == "unknown_category"
    assert out.reason == "discovery_incomplete" and h.requests == []


async def test_auth_probe_bypasses_index(tmp_path, c37162):
    from consumer_reports_mcp.config import AUTH_PROBE_CATEGORY, AUTH_PROBE_PATH

    h = Harness(tmp_path, cookie=True, index=False)
    probe = dict(c37162, final_url=WWW + AUTH_PROBE_PATH)
    probe["filter_instance"] = dict(c37162["filter_instance"])
    probe["filter_instance"]["args"] = dict(
        c37162["filter_instance"]["args"], cid=AUTH_PROBE_CATEGORY
    )
    h.sess.push(_page(probe, url=WWW + AUTH_PROBE_PATH, subscriber="true", fill_scores=True))
    out = await h.repo.get_category(AUTH_PROBE_CATEGORY, refresh=True, validate=False)
    assert isinstance(out, Served) and out.fetch_kind == ingest.MEMBER
    assert h.requests == [WWW + AUTH_PROBE_PATH]
    assert h.health.health is SessionHealth.ACTIVE


# --------------------------------------------------------------------------- products


async def test_get_product_unknown_and_refresh_refetches_only_selected_category(tmp_path, c37162):
    h = Harness(tmp_path, cookie=False)
    out = await h.repo.get_product(500001)
    assert isinstance(out, ToolError) and out.code == "unknown_product"
    assert "cr_search" in out.message and h.requests == []
    h.seed(fixture_envelope(c37162), tier="anonymous", scored=False, age_days=2)
    hit = await h.repo.get_product(500001)
    assert isinstance(hit, Served) and hit.from_cache and hit.category_id == 37162
    h.sess.push(_page(c37162))
    fresh = await h.repo.get_product(500001, refresh=True)
    assert isinstance(fresh, Served) and fresh.from_cache is False
    # CR served the same bytes: no row appended, `fetched_at` stays the first sighting
    assert fresh.rowid == hit.rowid and fresh.fetched_at == to_iso(T0 - timedelta(days=2))
    assert h.requests == [CAT_URL]


async def test_get_product_non_qualifying_row_refetches_then_serves_best(tmp_path, c37162):
    h = Harness(tmp_path, cookie=True)  # unverified → effective member
    h.seed(fixture_envelope(c37162), tier="anonymous", scored=False, age_days=2)
    h.sess.push(_page(c37162, subscriber="true", fill_scores=True))
    out = await h.repo.get_product(500003)
    assert isinstance(out, Served) and out.data_tier == "member" and out.session == "active"
    assert h.requests == [CAT_URL]


# --------------------------------------------------------------------------- P6.3 reliability


async def test_reliability_url_from_cached_args_cats(tmp_path, c37162, reliability_fixture):
    h = Harness(tmp_path, cookie=False)
    h.seed(fixture_envelope(c37162), tier="anonymous", scored=False, age_days=1)
    top_url = WWW + "/appliances/refrigerators/top-freezer-refrigerator/reliability/c28722/"
    h.sess.route(
        top_url, FakeResponse(url=top_url, content=make_reliability_page(reliability_fixture))
    )
    out = await h.repo.get_reliability(28722)
    assert not isinstance(out, ToolError)
    assert out.category_id == 28722 and out.from_cache is False and out.cr_url == top_url
    assert h.requests == [top_url]  # the real reliabilityURL from args.cats, not a guess


async def test_no_survey_published_is_answered_from_the_payload_without_a_request(tmp_path, c37162):
    """c33041 (upright freezers) filed this: `cr_reliability` 404'd and told the caller to run
    `cr_ratings` so the real URL would be cached — but they HAD run it, and the payload it
    cached is what says `reliabilityURL: false`. CR runs no survey there, so there is no URL to
    cache and the advice could never work. The answer costs no request."""
    import copy

    fx = copy.deepcopy(c37162)
    args = fx["filter_instance"]["args"]
    args.pop("reliabilityURL", None)
    for c in args["cats"]:
        c["reliabilityURL"] = False

    h = Harness(tmp_path, cookie=False)
    h.seed(fixture_envelope(fx), tier="anonymous", scored=False, age_days=1)
    out = await h.repo.get_reliability("c37162")

    assert not isinstance(out, ToolError)  # not a failure: SPEC §7
    assert out.survey_published is False and out.payload == {}
    assert out.cr_url is None and out.from_cache is True
    assert h.requests == []  # nothing was guessed, nothing was fetched


async def test_reliability_constructed_url_404_is_url_unresolved(tmp_path):
    h = Harness(tmp_path, cookie=False)  # nothing cached → constructed from the canonical path
    h.sess.push(FakeResponse(url=REL_URL, status_code=404, content=b"<title>404</title>"))
    out = await h.repo.get_reliability("c37162")
    assert isinstance(out, ToolError) and out.code == "fetch_failed"
    assert out.reason == "url_unresolved" and out.retryable is False
    assert h.requests == [REL_URL]
    assert h.negcache.get(("rel", 37162), T0) is None  # never a drift alarm


async def test_reliability_never_fetches_category_page(tmp_path, reliability_fixture):
    h = Harness(tmp_path, cookie=False)
    h.sess.route(
        REL_URL, FakeResponse(url=REL_URL, content=make_reliability_page(reliability_fixture))
    )
    out = await h.repo.get_reliability(37162)
    assert not isinstance(out, ToolError)
    assert h.requests == [REL_URL] and CAT_URL not in h.requests
    assert h.cache.has_rows(37162) is False


async def test_reliability_fanout_hits_sibling_next_call(tmp_path, reliability_fixture):
    h = Harness(tmp_path, cookie=False)
    h.sess.route(
        REL_URL, FakeResponse(url=REL_URL, content=make_reliability_page(reliability_fixture))
    )
    await h.repo.get_reliability(37162)
    sib = await h.repo.get_reliability(28722)
    assert not isinstance(sib, ToolError) and sib.from_cache is True
    assert len(h.requests) == 1
    # the fan-out list comes from the payload, so 29738 (no survey) is cached too
    assert h.cache.select_reliability(29738, 30, T0) is not None


async def test_reliability_fetch_never_changes_session_health(tmp_path, reliability_fixture):
    h = Harness(tmp_path, cookie=True)
    h.health.on_marker(True, credential_present=True)
    h.sess.route(
        REL_URL, FakeResponse(url=REL_URL, content=make_reliability_page(reliability_fixture))
    )
    out = await h.repo.get_reliability(37162)
    assert not isinstance(out, ToolError)
    assert h.health.health is SessionHealth.ACTIVE


async def test_reliability_payload_missing_is_negatively_cached(tmp_path):
    h = Harness(tmp_path, cookie=False)
    h.sess.push(FakeResponse(url=REL_URL, content=make_maintenance_page("Maint")))
    a = await h.repo.get_reliability(37162)
    b = await h.repo.get_reliability(37162)
    assert isinstance(a, ToolError) and a.code == "reliability_payload_missing"
    assert a.title == "Maint" and isinstance(b, ToolError) and len(h.requests) == 1


async def test_reliability_no_canonical_url_is_url_unresolved_without_fetch(tmp_path):
    h = Harness(tmp_path, cookie=False)
    h.cache.upsert_category_index([{"id": 4242, "path": "/x/y/c4242/"}], "sitemap", T0)
    with h.cache._connect() as conn:
        conn.execute("UPDATE category_index SET canonical_url=NULL WHERE category_id=4242")
    out = await h.repo.get_reliability(4242)
    assert isinstance(out, ToolError) and out.reason == "url_unresolved" and h.requests == []


# --------------------------------------------------------------------------- retention loop


async def test_retained_scored_row_past_ttl_refetches_once_then_serves(tmp_path, c37162):
    """The lapsed-membership case: no cookie, a 40-day-old scored member row, CR answering
    anonymously. Never-downgrade serves the retained row, so ITS `stale` never clears — the
    refetch decision has to come from when the category was last checked at a qualifying
    tier, or every call fetches 11 MB and appends a row, forever."""
    h = Harness(tmp_path, cookie=False)
    h.seed(fixture_envelope(c37162, fill_scores=True), tier="member", scored=True, age_days=40)
    h.sess.route(CAT_URL, lambda url, kw: _page(c37162, subscriber="false"))
    outs = [await h.repo.get_category(37162) for _ in range(3)]
    assert all(isinstance(o, Served) and o.data_tier == "member" for o in outs)
    assert len(h.requests) == 1  # one refetch after the TTL, not one per call
    assert outs[0].superseded_at == to_iso(T0) and outs[0].stale is True
    assert outs[1].from_cache is True and outs[1].stale is True  # provenance: still past TTL
    with h.cache._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM category_raw").fetchone()[0] == 2
    # ... until the NEWER row itself ages out
    h.clock.now = T0 + timedelta(days=31)
    later = await h.repo.get_category(37162)
    assert isinstance(later, Served) and later.data_tier == "member" and len(h.requests) == 2


async def test_get_product_retained_row_past_ttl_refetches_once_then_serves(tmp_path, c37162):
    h = Harness(tmp_path, cookie=False)
    h.seed(fixture_envelope(c37162, fill_scores=True), tier="member", scored=True, age_days=40)
    h.sess.route(CAT_URL, lambda url, kw: _page(c37162, subscriber="false"))
    outs = [await h.repo.get_product(500001) for _ in range(3)]
    assert all(isinstance(o, Served) and o.data_tier == "member" for o in outs)
    assert len(h.requests) == 1


async def test_get_product_dropped_from_the_catalogue_does_not_refetch_every_call(tmp_path, c37162):
    """A product CR removed: the only row that still contains it is the old one, so "when was
    the served row fetched" is stale forever. The check that matters is on the CATEGORY."""
    import copy

    h = Harness(tmp_path, cookie=False)
    h.seed(fixture_envelope(c37162), tier="anonymous", scored=False, age_days=40)
    trimmed = copy.deepcopy(c37162)
    del trimmed["filter_instance"]["data"]["500008"]
    h.sess.route(CAT_URL, lambda url, kw: _page(trimmed))
    outs = [await h.repo.get_product(500008) for _ in range(3)]
    assert all(isinstance(o, Served) and o.stale is True for o in outs)
    assert len(h.requests) == 1


async def test_refresh_inside_the_cooldown_is_skipped_with_a_warning(tmp_path, c37162):
    """`refresh=true` on a read-only tool never prompts, so "call it fifty times with refresh"
    must not cost fifty 11 MB fetches: inside the cooldown the young row is served and the
    call says so."""
    from consumer_reports_mcp.config import REFRESH_COOLDOWN_S

    h = Harness(tmp_path, cookie=False)
    h.sess.route(CAT_URL, lambda url, kw: _page(c37162))
    first = await h.repo.get_category(37162, refresh=True)
    assert isinstance(first, Served) and first.from_cache is False and len(h.requests) == 1
    h.clock.now = T0 + timedelta(seconds=30)
    for _ in range(5):
        again = await h.repo.get_category(37162, refresh=True)
        assert isinstance(again, Served) and again.from_cache is True
        assert again.warnings == ["refresh_skipped"] and again.rowid == first.rowid
    assert len(h.requests) == 1
    h.clock.now = T0 + timedelta(seconds=REFRESH_COOLDOWN_S + 1)
    after = await h.repo.get_category(37162, refresh=True)
    assert isinstance(after, Served) and after.warnings == [] and len(h.requests) == 2


async def test_refresh_cooldown_counts_only_qualifying_rows(tmp_path, c37162):
    # unverified → effective member: a minute-old ANONYMOUS row is not what this caller wants
    h = Harness(tmp_path, cookie=True)
    h.seed(fixture_envelope(c37162), tier="anonymous", scored=False, age_days=0)
    h.sess.push(_page(c37162, subscriber="true", fill_scores=True))
    out = await h.repo.get_category(37162, refresh=True)
    assert isinstance(out, Served) and out.data_tier == "member" and out.warnings == []
    assert len(h.requests) == 1


async def test_get_product_refresh_inside_the_cooldown_is_skipped(tmp_path, c37162):
    h = Harness(tmp_path, cookie=False)
    h.seed(fixture_envelope(c37162), tier="anonymous", scored=False, age_days=0)
    out = await h.repo.get_product(500001, refresh=True)
    assert isinstance(out, Served) and out.from_cache is True
    assert out.warnings == ["refresh_skipped"] and h.requests == []


async def test_the_auth_probe_is_exempt_from_the_refresh_cooldown(tmp_path, c37162):
    """`validate_cookies` is a verdict on THIS cookie and reads `fetch_kind`; served from a
    minute-old member row it would call a valid cookie `anonymous`."""
    from consumer_reports_mcp.config import AUTH_PROBE_CATEGORY, AUTH_PROBE_PATH

    h = Harness(tmp_path, cookie=True, index=False)
    probe = dict(c37162, final_url=WWW + AUTH_PROBE_PATH)
    probe["filter_instance"] = dict(c37162["filter_instance"])
    probe["filter_instance"]["args"] = dict(
        c37162["filter_instance"]["args"], cid=AUTH_PROBE_CATEGORY
    )
    h.sess.route(
        WWW + AUTH_PROBE_PATH,
        lambda url, kw: _page(probe, url=url, subscriber="true", fill_scores=True),
    )
    a = await h.repo.get_category(AUTH_PROBE_CATEGORY, refresh=True, validate=False, cooldown=False)
    h.clock.now = T0 + timedelta(seconds=30)
    b = await h.repo.get_category(AUTH_PROBE_CATEGORY, refresh=True, validate=False, cooldown=False)
    assert isinstance(a, Served) and isinstance(b, Served)
    assert a.fetch_kind == b.fetch_kind == ingest.MEMBER and len(h.requests) == 2


async def test_a_refetch_with_identical_content_appends_no_row(tmp_path, c37162):
    h = Harness(tmp_path, cookie=False)
    h.sess.route(CAT_URL, lambda url, kw: _page(c37162))
    first = await h.repo.get_category(37162)
    assert isinstance(first, Served)
    h.clock.now = T0 + timedelta(hours=1)  # past the cooldown, inside the TTL
    again = await h.repo.get_category(37162, refresh=True)
    assert isinstance(again, Served) and again.from_cache is False
    assert again.rowid == first.rowid and again.fetched_at == to_iso(T0)
    assert len(h.requests) == 2
    with h.cache._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM category_raw").fetchone()[0] == 1
    # past the TTL the same bytes ARE appended: the row is also the record of the check
    h.clock.now = T0 + timedelta(days=31)
    later = await h.repo.get_category(37162)
    assert isinstance(later, Served) and later.rowid != first.rowid and len(h.requests) == 3


async def test_reliability_refresh_inside_the_cooldown_is_skipped_and_content_deduped(
    tmp_path, reliability_fixture
):
    h = Harness(tmp_path, cookie=False)
    h.sess.route(
        REL_URL,
        lambda url, kw: FakeResponse(
            url=REL_URL, content=make_reliability_page(reliability_fixture)
        ),
    )
    first = await h.repo.get_reliability(37162)
    assert not isinstance(first, ToolError) and first.from_cache is False
    h.clock.now = T0 + timedelta(seconds=30)
    again = await h.repo.get_reliability(37162, refresh=True)
    assert not isinstance(again, ToolError) and again.from_cache is True
    assert again.warnings == ["refresh_skipped"] and len(h.requests) == 1
    h.clock.now = T0 + timedelta(hours=1)
    third = await h.repo.get_reliability(37162, refresh=True)
    assert not isinstance(third, ToolError) and third.from_cache is False
    assert len(h.requests) == 2
    with h.cache._connect() as conn:  # one row per named sibling, written once
        assert conn.execute("SELECT COUNT(*) FROM reliability_raw").fetchone()[0] == 3


# --------------------------------------------------------------------------- drift shapes


async def test_payload_without_cid_is_payload_missing_not_a_crash(tmp_path, c37162):
    """A complete `filterInstanceDATA` whose `args` carries no integer `cid` used to raise
    `UnboundLocalError` out of `_fetch_category` — an MCP protocol error for every waiter
    coalesced on the key, once per negative-cache hour."""
    import copy

    broken = copy.deepcopy(c37162)
    broken["filter_instance"]["args"] = {}
    h = Harness(tmp_path, cookie=False)
    h.sess.push(_page(broken))
    out = await h.repo.get_category(37162)
    assert isinstance(out, ToolError) and out.code == "payload_missing"
    assert out.http_status == 200 and out.title  # the structured detail, not a traceback
    assert h.negcache.get(("cat", 37162), T0) is not None
    assert not h.cache.has_rows(37162)


# --------------------------------------------------------------------------- leader cancellation


async def test_a_cancelled_leader_does_not_kill_a_concurrent_call_for_the_same_category(
    tmp_path, c37162
):
    """`cr_ratings("tvs")` leads the 11 MB flight, `cr_filters("tvs")` joins it, the client
    cancels the first. The second call must still be served, from the one request."""
    h = Harness(tmp_path, cookie=False)
    gate = asyncio.Event()

    async def slow(url, kw):
        await gate.wait()
        return _page(c37162)

    h.sess.route(CAT_URL, slow)
    first = asyncio.ensure_future(h.repo.get_category(37162))
    await asyncio.sleep(0.01)
    second = asyncio.ensure_future(h.repo.get_category("c37162"))
    await asyncio.sleep(0.01)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    out = await second
    assert isinstance(out, Served) and out.from_cache is False
    assert not second.cancelled() and h.requests == [CAT_URL]


def test_reliability_ids_skip_a_non_numeric_id_instead_of_raising():
    from consumer_reports_mcp.repository import _reliability_ids

    store = {
        "data": {
            "category": {"_id": "not-a-number"},
            "categories": [{"_id": "37162"}, {"_id": None}, {"_id": "x"}, {"_id": 28722}, "junk"],
        }
    }
    assert _reliability_ids(store, 37162) == [37162, 28722]
