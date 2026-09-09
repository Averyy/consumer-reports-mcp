"""P10.1 — the cars API client: key at runtime, 403 = no such route, marker → session (D11)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from consumer_reports_mcp.config import CARS_API, CARS_PAGE_URL
from consumer_reports_mcp.credentials import SessionHealth
from consumer_reports_mcp.transport import Challenged, FetchFailed
from tests.conftest import SYNTH_API_KEY, FakeResponse, RuntimeHarness, make_car_page, until

SRC = Path(__file__).resolve().parents[1] / "src"


def test_key_read_from_page_not_pinned():
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"[\"']([A-Za-z0-9]{40})[\"']", text):
            assert m.group(1).startswith("SYNTHETIC"), (path, m.group(1)[:8])


async def test_key_and_marker_read_from_page_once(tmp_path):
    h = RuntimeHarness(tmp_path)
    h.sess.route(CARS_PAGE_URL, FakeResponse(url=CARS_PAGE_URL, content=make_car_page(False)))
    key, sub = await h.rt.cars.page_info()
    assert key == SYNTH_API_KEY and sub is False
    await h.rt.cars.page_info()
    assert h.requests == [CARS_PAGE_URL]  # cached in memory for the process
    h.sess.route(f"{CARS_API}/v2/cr/carTypes", FakeResponse(url="u", content=b'{"content": []}'))
    assert await h.rt.cars.car_types() == {"content": []}
    assert h.sess.requests[-1][1]["headers"] == {
        "x-api-key": SYNTH_API_KEY,
        "accept": "application/json",
    }


async def test_403_is_no_such_route_not_challenged(tmp_path):
    h = RuntimeHarness(tmp_path)
    h.sess.route(CARS_PAGE_URL, FakeResponse(url=CARS_PAGE_URL, content=make_car_page(False)))
    h.sess.route(
        f"{CARS_API}/v2/cr/nope",
        FakeResponse(url="u", status_code=403, content=b'{"message":"Forbidden"}'),
    )
    with pytest.raises(FetchFailed) as ei:
        await h.rt.cars.get_json("v2/cr/nope")
    assert ei.value.reason == "no_such_route" and ei.value.retryable is False


async def test_403_with_challenge_type_is_challenged(tmp_path):
    h = RuntimeHarness(tmp_path)
    h.sess.route(CARS_PAGE_URL, FakeResponse(url=CARS_PAGE_URL, content=make_car_page(False)))
    h.sess.route(
        f"{CARS_API}/v2/cr/carTypes",
        FakeResponse(url="u", status_code=403, content=b"<html>", challenge_type="cloudflare"),
    )
    with pytest.raises(Challenged):
        await h.rt.cars.car_types()


async def test_marker_true_sets_active_false_sets_expired(tmp_path):
    h = RuntimeHarness(tmp_path, cookie=True)
    h.sess.route(CARS_PAGE_URL, FakeResponse(url=CARS_PAGE_URL, content=make_car_page(True)))
    await h.rt.cars.page_info()
    assert h.rt.health.health is SessionHealth.ACTIVE
    h.sess.route(CARS_PAGE_URL, FakeResponse(url=CARS_PAGE_URL, content=make_car_page(False)))
    await h.rt.cars.page_info(refresh=True)
    assert h.rt.health.health is SessionHealth.DEAD


async def test_marker_read_from_html_never_from_cookie_presence(tmp_path):
    h = RuntimeHarness(tmp_path, cookie=True)
    h.sess.route(CARS_PAGE_URL, FakeResponse(url=CARS_PAGE_URL, content=make_car_page(False)))
    key, sub = await h.rt.cars.page_info()
    assert sub is False  # a configured cookie is not evidence of a member session
    # without a cookie the marker is recorded but health stays `none`
    h2 = RuntimeHarness(tmp_path / "b")
    h2.sess.route(CARS_PAGE_URL, FakeResponse(url=CARS_PAGE_URL, content=make_car_page(False)))
    await h2.rt.cars.page_info()
    assert h2.rt.health.health is SessionHealth.NONE


async def test_missing_marker_never_raises_and_missing_key_is_url_unresolved(tmp_path):
    h = RuntimeHarness(tmp_path, cookie=True)
    h.sess.route(
        CARS_PAGE_URL, FakeResponse(url=CARS_PAGE_URL, content=make_car_page(None, api_key=None))
    )
    key, sub = await h.rt.cars.page_info()
    assert key is None and sub is None and h.rt.health.health is SessionHealth.UNVERIFIED
    with pytest.raises(FetchFailed) as ei:
        await h.rt.cars.car_types()
    assert ei.value.reason == "url_unresolved"


async def test_a_sign_in_adopted_mid_car_page_fetch_is_retried_once(tmp_path):
    """The car page is a `www.` fetch, so an adopt while it is in flight refuses the response
    as `policy_changed`; it used to reach the caller as `fetch_failed`. Retried once, under
    the new policy — and the retry's marker moves health, truthfully."""
    import asyncio

    h = RuntimeHarness(tmp_path)
    gate = asyncio.Event()

    async def slow(url, kw):
        await gate.wait()
        return FakeResponse(url=url, content=make_car_page(h.rt.transport.cookie_configured))

    h.sess.route(CARS_PAGE_URL, slow)
    inflight = asyncio.ensure_future(h.rt.cars.page_info())
    await until(lambda: h.requests)
    assert h.requests == [CARS_PAGE_URL]
    h.rt.credentials.save({"hash": "h" * 36})
    await h.rt.transport.adopt()
    gate.set()
    key, sub = await inflight
    assert key == SYNTH_API_KEY and sub is True
    assert h.requests == [CARS_PAGE_URL, CARS_PAGE_URL]
    assert h.rt.health.health is SessionHealth.ACTIVE and h.rt.cars.page_fetches == 1
    # exactly one retry
    h2 = RuntimeHarness(tmp_path / "b")
    attempts: list[str] = []

    def always(url, kw):
        attempts.append(url)
        return FetchFailed("policy_changed", retryable=True, url=url)

    h2.sess.route(CARS_PAGE_URL, always)
    with pytest.raises(FetchFailed) as ei:
        await h2.rt.cars.page_info()
    assert ei.value.reason == "policy_changed" and len(attempts) == 2


async def test_login_check_never_applied_to_cars_api(tmp_path):
    h = RuntimeHarness(tmp_path, cookie=True)
    h.sess.route(CARS_PAGE_URL, FakeResponse(url=CARS_PAGE_URL, content=make_car_page(True)))
    h.sess.route(
        f"{CARS_API}/v2/cr/carTypes",
        FakeResponse(url=f"{CARS_API}/ec/login?x", status_code=200, content=b'{"content":[]}'),
    )
    assert await h.rt.cars.car_types() == {"content": []}
    assert h.rt.credentials.rejected is False and h.rt.health.health is SessionHealth.ACTIVE
