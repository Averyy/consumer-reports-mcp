"""P5 — the two-pass category index: A-Z for names, sitemaps for coverage (SPEC §5)."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta

from consumer_reports_mcp.cache import Cache
from consumer_reports_mcp.config import AZ_INDEX_URL, PRODUCTS_SITEMAP_URL, WWW, Settings
from consumer_reports_mcp.credentials import CredentialStore, SessionState
from consumer_reports_mcp.discovery import (
    Discovery,
    franchise_of,
    parse_az_index,
    parse_category_sitemap,
    parse_sitemap_index,
    slug_of,
)
from consumer_reports_mcp.transport import FetchFailed, Transport
from tests.conftest import FakeResponse, FakeWaferSession, fixture_envelope, fixture_text

T0 = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- P5.1 parsers


def test_az_parses_all_six_including_nested_span_and_entities():
    html = fixture_text("azindex.html")
    rows = {r.id: r for r in parse_az_index(html)}
    assert set(rows) == {37162, 28722, 37154, 201102, 200228, 35183}
    assert rows[37162].display_name == "French-Door Refrigerators"
    assert rows[37162].path == "/appliances/refrigerators/french-door-refrigerator/c37162/"
    assert (
        rows[28722].path == "/appliances/refrigerators/top-freezer-refrigerator/c28722/"
    )  # absolute href
    assert rows[37154].display_name == "Banks & Credit Unions"  # &amp; unescaped
    assert rows[200228].display_name == "Plant Milk"  # <b> stripped
    assert rows[201102].franchise == "cars" and rows[201102].display_name == "Dash Cams"
    # the naive regex finds fewer — the nested <span> trap (RECON §9h)
    naive = re.findall(r'href="[^"]*/c\d+/"[^>]*>([^<]+)</a>', html)
    assert len([n for n in naive if n.strip()]) < len(rows)


def test_sitemap_ids_slugs_franchises():
    assert parse_sitemap_index(fixture_text("sitemap_products.xml")) == [
        f"{WWW}/products/sitemap/28958",
        f"{WWW}/products/sitemap/28981",
    ]
    rows = {r.id: r for r in parse_category_sitemap(fixture_text("sitemap_28958.xml"))}
    assert set(rows) == {28698, 28700, 201102}
    assert (
        rows[28698].path == "/electronics-computers/sound-bars/c28698/"
    )  # shortest, not /recommended/
    assert rows[28698].slug == "sound-bars" and rows[28698].franchise == "electronics-computers"
    assert rows[28700].slug == "tvs"
    assert rows[201102].franchise == "cars" and rows[201102].display_name is None


def test_slug_is_last_segment_before_id():
    assert (
        slug_of("/appliances/refrigerators/french-door-refrigerator/c37162/")
        == "french-door-refrigerator"
    )
    assert slug_of("/electronics-computers/sound-bars/recommended/c28698/") == "recommended"
    assert slug_of("/electronics-computers/sound-bars/") is None
    assert franchise_of("/cars/dash-cams/c201102/") == "cars"
    assert franchise_of("") is None


# --------------------------------------------------------------------------- P5.2 orchestration


def _runtime(tmp_path, ttl_days: int = 90):
    settings = Settings(
        env={"CR_INDEX_TTL_DAYS": str(ttl_days), "CR_MIN_REQUEST_INTERVAL_S": "0"}, home=tmp_path
    )
    store = CredentialStore(tmp_path / "session.json", env={})
    sess = FakeWaferSession()
    transport = Transport(settings, store, SessionState(False), session_factory=lambda **kw: sess)
    cache = Cache(tmp_path / "cr.db")
    disc = Discovery(cache, transport, settings.index_ttl_days)
    sess.route(
        AZ_INDEX_URL, FakeResponse(url=AZ_INDEX_URL, content=fixture_text("azindex.html").encode())
    )
    sess.route(
        PRODUCTS_SITEMAP_URL,
        FakeResponse(
            url=PRODUCTS_SITEMAP_URL, content=fixture_text("sitemap_products.xml").encode()
        ),
    )
    sess.route(
        f"{WWW}/products/sitemap/28958",
        FakeResponse(
            url=f"{WWW}/products/sitemap/28958", content=fixture_text("sitemap_28958.xml").encode()
        ),
    )
    sess.route(
        f"{WWW}/products/sitemap/28981",
        FakeResponse(
            url=f"{WWW}/products/sitemap/28981",
            content=b'<?xml version="1.0"?><urlset xmlns="https://www.sitemaps.org/schemas/sitemap/0.9">'
            b"<url><loc>https://www.consumerreports.org/appliances/ranges/</loc></url></urlset>",
        ),
    )
    return disc, cache, sess


async def test_az_then_sitemap_union_is_superset_with_sources(tmp_path):
    disc, cache, sess = _runtime(tmp_path)
    assert await disc.ensure_az_index(now=T0) is True
    assert cache.index_count() == 6 and disc.az_done()
    assert await disc.ensure_az_index(now=T0) is False  # fresh → no second fetch
    n = await disc.run_sitemap_pass(now=T0)
    assert n == 3 and disc.sitemap_done() and disc.sitemap_errors == []
    rows = {r["category_id"]: r for r in cache.list_categories()}
    assert set(rows) == {37162, 28722, 37154, 201102, 200228, 35183, 28698, 28700}
    assert rows[28700]["source"] == "sitemap" and rows[28700]["display_name"] is None
    assert rows[201102]["source"] == "az" and rows[201102]["franchise"] == "cars"
    assert rows[37162]["source"] == "az"
    urls = [u for u, _ in sess.requests]
    assert urls == [
        AZ_INDEX_URL,
        PRODUCTS_SITEMAP_URL,
        f"{WWW}/products/sitemap/28958",
        f"{WWW}/products/sitemap/28981",
    ]
    st = disc.status()
    assert st["az"]["count"] == 6 and st["sitemap"]["count"] == 3


async def test_unknown_only_after_both_passes(tmp_path):
    disc, cache, sess = _runtime(tmp_path)
    await disc.ensure_az_index(now=T0)
    assert cache.resolve_category("tvs").kind == "unknown"  # not in the A-Z index
    task = disc.start_background_sitemap_pass()
    assert task is not None and disc.sitemap_pending
    assert disc.warnings() == ["sitemap_pass_pending"]
    res = await disc.resolve("tvs")  # awaits the pass instead of refusing
    assert res.kind == "ok" and res.category_id == 28700
    assert not disc.sitemap_pending and disc.warnings() == []
    assert disc.both_sources_ran()
    assert (await disc.resolve("mattresses")).kind == "unknown"


async def test_resolve_fetches_az_index_when_cold(tmp_path):
    disc, cache, sess = _runtime(tmp_path)
    res = await disc.resolve("c37162")
    assert res.kind == "ok" and res.category_id == 37162
    assert sess.requests[0][0] == AZ_INDEX_URL


async def test_display_names_only_from_az_until_payload(tmp_path, c37162):
    disc, cache, sess = _runtime(tmp_path)
    await disc.run_sitemap_pass(now=T0)
    assert cache.index_row(28698)["display_name"] is None
    # a payload fetch names the category from `.cat.productGroupName`
    env = fixture_envelope(c37162)
    for f in env["family"]:
        if f["id"] == 37162:
            f["name"] = "French-Door Fridges (payload name)"
    cache.write_category(env, tier="anonymous", scored=False, fetched_at=T0)
    assert cache.index_row(37162)["display_name"] == "French-Door Fridges (payload name)"
    assert cache.index_row(37162)["source"] == "payload"
    # the A-Z index is the name authority for what it lists: it overwrites a payload name
    await disc.ensure_az_index(now=T0 + timedelta(days=1))
    assert cache.index_row(37162)["display_name"] == "French-Door Refrigerators"
    assert cache.index_row(37162)["source"] == "payload"  # `source` is who found it first


async def test_sitemap_pass_runs_once_per_process(tmp_path):
    disc, cache, sess = _runtime(tmp_path)
    t1 = disc.start_background_sitemap_pass()
    t2 = disc.start_background_sitemap_pass()
    assert t1 is t2
    await disc.await_sitemap_pass()
    assert sum(1 for u, _ in sess.requests if u == PRODUCTS_SITEMAP_URL) == 1
    # a fresh run within the TTL is not started at all
    disc2 = Discovery(cache, disc.transport, 90)
    assert disc2.start_background_sitemap_pass() is None
    disc3 = Discovery(cache, disc.transport, 90)
    assert disc3.start_background_sitemap_pass(force=True) is not None
    await disc3.await_sitemap_pass()


async def test_az_fetch_failure_is_not_fatal(tmp_path):
    disc, cache, sess = _runtime(tmp_path)
    sess.routes.clear()
    sess.route(
        AZ_INDEX_URL,
        FakeResponse(url=AZ_INDEX_URL, status_code=503, content=b"<title>down</title>"),
    )
    assert await disc.ensure_az_index(now=T0) is False
    assert disc.az_error and "server_error" in disc.az_error
    assert not disc.az_done()


async def test_an_adopt_during_the_products_xml_fetch_does_not_kill_discovery(tmp_path):
    """The reviewer's scenario: a sign-in adopted in the first seconds of the 6.5-minute
    startup pass, while `products.xml` is in flight. Its response is `policy_changed`; without
    a retry nothing is recorded, `sitemap_done()` is false for the process and
    `resolve("c28700")` answers `unknown_category / discovery_incomplete` forever."""
    disc, cache, sess = _runtime(tmp_path)
    await disc.ensure_az_index(now=T0)
    gate = asyncio.Event()
    index_xml = fixture_text("sitemap_products.xml").encode()
    attempts: list[str] = []

    async def slow_index(url, kw):
        attempts.append(url)
        if len(attempts) == 1:
            await gate.wait()
        return FakeResponse(url=url, content=index_xml)

    sess.route(PRODUCTS_SITEMAP_URL, slow_index)
    task = disc.start_background_sitemap_pass()
    await asyncio.sleep(0.01)
    assert attempts == [PRODUCTS_SITEMAP_URL]  # on the wire
    await disc.transport.adopt()  # the sign-in landed
    gate.set()
    await asyncio.wait_for(task, 2)
    assert len(attempts) == 2 and disc.sitemap_done() and disc.sitemap_errors == []
    assert (await disc.resolve("c28700")).kind == "ok"
    assert disc.both_sources_ran() and disc.warnings() == []


async def test_products_xml_gets_one_retry_on_a_retryable_failure_and_no_more(tmp_path):
    disc, cache, sess = _runtime(tmp_path)
    attempts: list[str] = []

    def always_failing(url, kw):
        attempts.append(url)
        return FetchFailed("policy_changed", retryable=True, url=url)

    sess.route(PRODUCTS_SITEMAP_URL, always_failing)
    assert await disc.run_sitemap_pass(now=T0) == 0
    assert len(attempts) == 2 and not disc.sitemap_done()
    assert disc.sitemap_errors and "policy_changed" in disc.sitemap_errors[0]
    # a non-retryable failure is not retried at all
    attempts.clear()
    sess.route(
        PRODUCTS_SITEMAP_URL,
        lambda url, kw: (attempts.append(url), FetchFailed("too_large", retryable=False, url=url))[
            1
        ],
    )
    assert await disc.run_sitemap_pass(now=T0) == 0 and len(attempts) == 1


async def test_a_single_sitemap_lost_to_policy_changed_is_retried_not_skipped(tmp_path):
    """Skipped, its categories would be missing from an index recorded as authoritative — a
    confident `not_in_index` for a category that exists, for a whole TTL. A NETWORK failure on
    one sitemap keeps the existing skip-and-count rule."""
    disc, cache, sess = _runtime(tmp_path)
    url = f"{WWW}/products/sitemap/28958"
    good = FakeResponse(url=url, content=fixture_text("sitemap_28958.xml").encode())
    attempts: list[str] = []

    def once_policy_changed(u, kw):
        attempts.append(u)
        if len(attempts) == 1:
            return FetchFailed("policy_changed", retryable=True, url=u)
        return good

    sess.route(url, once_policy_changed)
    n = await disc.run_sitemap_pass(now=T0)
    assert n == 3 and len(attempts) == 2 and disc.sitemap_done() and disc.sitemap_errors == []
    assert cache.resolve_category("c28700").kind == "ok"
    # a network failure on the same sitemap: one attempt, skipped and counted as before
    disc2 = Discovery(cache, disc.transport, 90)
    attempts.clear()
    sess.route(
        url,
        lambda u, kw: (
            attempts.append(u),
            FetchFailed("connection_failed", retryable=True, url=u),
        )[1],
    )
    await disc2.run_sitemap_pass(now=T0)
    assert len(attempts) == 1 and len(disc2.sitemap_errors) == 1


async def test_background_pass_crash_is_contained(tmp_path):
    disc, cache, sess = _runtime(tmp_path)
    sess.routes.clear()
    sess.route(PRODUCTS_SITEMAP_URL, RuntimeError("boom"))
    task = disc.start_background_sitemap_pass()
    await asyncio.wait_for(task, 2)
    assert task.result() == 0
    assert any("crash" in e for e in disc.sitemap_errors)


# --------------------------------------------------------------------------- bounds


async def test_sitemap_pass_is_capped_and_its_error_list_bounded(tmp_path, monkeypatch):
    """`products.xml` is fetched content: the number of `<loc>`s it names is CR's to choose,
    and so was the length of `sitemap_errors`."""
    disc, cache, sess = _runtime(tmp_path)
    monkeypatch.setattr(Discovery, "MAX_SITEMAPS", 3)
    monkeypatch.setattr(Discovery, "MAX_RECORDED_ERRORS", 2)
    locs = "".join(f"<sitemap><loc>{WWW}/products/sitemap/{i}</loc></sitemap>" for i in range(10))
    sess.route(
        PRODUCTS_SITEMAP_URL,
        FakeResponse(
            url=PRODUCTS_SITEMAP_URL,
            content=f'<?xml version="1.0"?><sitemapindex>{locs}</sitemapindex>'.encode(),
        ),
    )
    sess.route(
        f"{WWW}/products/sitemap/",
        lambda url, kw: FetchFailed("connection_failed", retryable=True, url=url),
    )
    await disc.run_sitemap_pass(now=T0)
    fetched = [u for u, _ in sess.requests if u.startswith(f"{WWW}/products/sitemap/")]
    assert len(fetched) == 3
    assert disc.sitemap_failed == 3 and len(disc.sitemap_errors) == 2
    assert not disc.sitemap_done()


async def test_an_off_host_sitemap_loc_is_never_fetched(tmp_path):
    disc, cache, sess = _runtime(tmp_path)
    evil = "https://169.254.169.254/latest/meta-data/"
    sess.route(
        PRODUCTS_SITEMAP_URL,
        FakeResponse(
            url=PRODUCTS_SITEMAP_URL,
            content=(
                f"<sitemapindex><sitemap><loc>{evil}</loc></sitemap>"
                f"<sitemap><loc>{WWW}/products/sitemap/28958</loc></sitemap></sitemapindex>"
            ).encode(),
        ),
    )
    n = await disc.run_sitemap_pass(now=T0)
    assert evil not in [u for u, _ in sess.requests]
    assert n == 3 and disc.sitemap_failed == 1
    assert any("url_unresolved" in e for e in disc.sitemap_errors)


# --------------------------------------------------------------------------- unrecorded pass


def _offline_products_xml(sess):
    sess.route(
        PRODUCTS_SITEMAP_URL,
        lambda url, kw: FetchFailed("connection_failed", retryable=True, url=url),
    )


async def test_a_pass_that_ended_unrecorded_is_retried_by_the_next_miss(tmp_path, monkeypatch):
    """Offline at startup: the pass fails at `products.xml` and is not recorded. Once the
    network is back, the next lookup that misses the index must run the pass again — not
    answer `discovery_incomplete` until restart, which is what returning the finished task
    object forever did."""
    disc, cache, sess = _runtime(tmp_path)
    await disc.ensure_az_index(now=T0)
    good = next(item for pred, item in sess.routes if pred(PRODUCTS_SITEMAP_URL))
    _offline_products_xml(sess)
    task = disc.start_background_sitemap_pass()
    await asyncio.wait_for(task, 2)
    assert not disc.sitemap_done() and disc.warnings() == ["sitemap_pass_pending"]
    assert sum(1 for u, _ in sess.requests if u == PRODUCTS_SITEMAP_URL) == 2  # one retry
    monkeypatch.setattr(Discovery, "RETRY_COOLDOWN_S", 0)
    sess.route(PRODUCTS_SITEMAP_URL, good)  # the network is back
    res = await disc.resolve("tvs")
    assert res.kind == "ok" and res.category_id == 28700
    assert disc.sitemap_done() and disc.warnings() == [] and disc.both_sources_ran()


async def test_an_unrecorded_pass_is_retried_only_past_the_cooldown_and_never_initiated(tmp_path):
    disc, cache, sess = _runtime(tmp_path)
    await disc.ensure_az_index(now=T0)
    _offline_products_xml(sess)
    await asyncio.wait_for(disc.start_background_sitemap_pass(), 2)
    n = len(sess.requests)
    assert (await disc.resolve("tvs")).kind == "unknown"  # inside the cooldown: no request
    assert len(sess.requests) == n and not disc.sitemap_pending
    assert disc.start_background_sitemap_pass() is None
    disc._sitemap_started_at -= Discovery.RETRY_COOLDOWN_S  # the cooldown elapses
    assert (await disc.resolve("tvs")).kind == "unknown"  # still offline: retried, failed again
    assert len(sess.requests) == n + 2 and not disc.sitemap_done()
    # a miss RETRIES a pass; it never initiates one — that is the lifespan's — so with none
    # ever launched the answer is a request-free `discovery_incomplete`
    disc2 = Discovery(cache, disc.transport, 90)
    n = len(sess.requests)
    assert (await disc2.resolve("tvs")).kind == "unknown" and len(sess.requests) == n


async def test_no_pass_starts_after_cancel(tmp_path):
    disc, cache, sess = _runtime(tmp_path)
    await disc.ensure_az_index(now=T0)
    _offline_products_xml(sess)
    await asyncio.wait_for(disc.start_background_sitemap_pass(), 2)
    await disc.cancel()
    assert disc.start_background_sitemap_pass(force=True) is None


async def test_cancel_propagates_the_callers_own_cancellation(tmp_path):
    """A lifespan torn down under `cancel()`: the caller's cancellation must not be swallowed
    along with the task's (`SignInFlow.cancel` already gets this right)."""
    import pytest

    disc, cache, sess = _runtime(tmp_path)
    await disc.ensure_az_index(now=T0)
    released = asyncio.Event()

    async def slow_teardown(url, kw):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            await asyncio.sleep(0.2)  # a slow teardown, like a driver draining a pipe
            released.set()
            raise
        return FakeResponse(url=url, content=b"<sitemapindex></sitemapindex>")

    sess.route(PRODUCTS_SITEMAP_URL, slow_teardown)
    task = disc.start_background_sitemap_pass()
    await asyncio.sleep(0.01)
    canceller = asyncio.ensure_future(disc.cancel())
    await asyncio.sleep(0.01)
    canceller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await canceller
    await asyncio.wait_for(released.wait(), 2)
    await asyncio.sleep(0)
    assert task.cancelled()
