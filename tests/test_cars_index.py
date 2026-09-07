"""P10.2 — `v1/cr/keys` → car_index, `v2/cr/carTypes` → the (car_type, category) address."""

from __future__ import annotations

from consumer_reports_mcp.cars.index import parse_car_types, parse_keys, resolve_car_type
from consumer_reports_mcp.cars.tools import cr_car_search, cr_cars
from consumer_reports_mcp.config import CARS_API, CARS_PAGE_URL
from tests.conftest import (
    FakeResponse,
    RuntimeHarness,
    json_response,
    load_fixture,
    make_car_page,
    until,
)


def test_keys_flatten_counts_and_primary_type():
    rows = parse_keys(load_fixture("cars/keys.json"))
    assert len(rows) == 12
    by_id = {r["model_year_id"]: r for r in rows}
    r = by_id[700001]
    assert (r["make"], r["model"], r["year"]) == ("Make A", "Model X", 2026)
    assert r["states"] == ["New"] and r["primary_car_type_id"] == 105
    assert [t["id"] for t in r["car_types"]] == [105, 116]  # one model-year in two car types
    assert by_id[700002]["states"] == ["New", "Used"]
    assert {r["slug_make"] for r in rows} == {"make-a", "make-b"}


def test_category_scoped_by_car_type_pair_with_different_counts():
    tax = parse_car_types(load_fixture("cars/cartypes.json"))
    assert [t["slug"] for t in tax["types"]] == ["sedans", "hybrids-evs"]
    a = tax["pairs"][(105, 11359)]
    b = tax["pairs"][(107, 11359)]
    assert (a["tested"], a["not_tested"]) == (14, 9) and (b["tested"], b["not_tested"]) == (13, 8)
    assert resolve_car_type(tax, "sedans")["id"] == 105
    assert resolve_car_type(tax, 107)["slug"] == "hybrids-evs"
    assert resolve_car_type(tax, "Sedans & Hatchbacks")["id"] == 105
    assert resolve_car_type(tax, "trucks") is None


async def test_index_refresh_failure_serves_the_cached_index_with_a_warning(tmp_path):
    """A failed refresh must not turn a working search into an error: the cached index still
    answers, and `index_refresh_failed` says the rows may be behind CR."""
    import wafer

    h = RuntimeHarness(tmp_path)
    h.sess.route(CARS_PAGE_URL, FakeResponse(url=CARS_PAGE_URL, content=make_car_page(False)))
    keys_url = f"{CARS_API}/v1/cr/keys"
    h.sess.route(keys_url, json_response(keys_url, load_fixture("cars/keys.json")))
    seeded = await cr_car_search(h.rt, "make b")
    assert seeded.error is None and seeded.data.cars

    h.sess.route(keys_url, wafer.ConnectionFailed(keys_url, "offline"))
    out = await cr_car_search(h.rt, "make b", refresh=True)
    assert out.error is None
    assert [c.model_year_id for c in out.data.cars] == [c.model_year_id for c in seeded.data.cars]
    assert any(w.startswith("index_refresh_failed:") for w in out.warnings)


async def test_car_search_anonymous_makes_no_ratings_call(tmp_path):
    h = RuntimeHarness(tmp_path)
    h.sess.route(CARS_PAGE_URL, FakeResponse(url=CARS_PAGE_URL, content=make_car_page(False)))
    h.sess.route(
        f"{CARS_API}/v1/cr/keys",
        json_response(f"{CARS_API}/v1/cr/keys", load_fixture("cars/keys.json")),
    )
    out = await cr_car_search(h.rt, "model x 2026")
    assert out.error is None and [c.model_year_id for c in out.data.cars] == [700001]
    assert out.data.cars[0].states == ["New"] and out.data.cars[0].car_types[0]["id"] == 105
    assert not any("modelYears" in u for u in h.requests)
    assert h.requests == [CARS_PAGE_URL, f"{CARS_API}/v1/cr/keys"]
    dumped = out.model_dump()
    assert "auth_state" not in dumped and dumped["session"] == "none"
    again = await cr_car_search(h.rt, "make b")
    assert len(again.data.cars) == 6 and len(h.requests) == 2  # index cached (90-day TTL)
    assert again.provenance.from_cache is True
    empty = await cr_car_search(h.rt, "")
    assert empty.error.code == "invalid_filter_value"


# --------------------------------------------------------------------------- empty payloads


async def test_empty_keys_payload_is_a_failed_fetch_not_a_recorded_index(tmp_path):
    """A `200` with zero model-years used to be written and recorded as a fresh 90-day run:
    every search then answered from an empty index, `from_cache: true`, for three months."""
    h = RuntimeHarness(tmp_path)
    h.sess.route(CARS_PAGE_URL, FakeResponse(url=CARS_PAGE_URL, content=make_car_page(False)))
    keys_url = f"{CARS_API}/v1/cr/keys"
    h.sess.route(keys_url, json_response(keys_url, {"response": []}))
    out = await cr_car_search(h.rt, "make b")
    assert out.error is not None and out.error.code == "fetch_failed" and out.data is None
    assert out.error.reason == "empty_response" and out.error.retryable is True
    assert h.rt.cache.car_index_status() is None  # neither written nor recorded
    h.sess.route(keys_url, json_response(keys_url, load_fixture("cars/keys.json")))
    again = await cr_car_search(h.rt, "make b")  # the next call retries
    assert again.error is None and len(again.data.cars) == 6
    assert again.provenance.from_cache is False and again.warnings == []


async def test_empty_taxonomy_is_a_failed_fetch_not_a_recorded_one(tmp_path):
    """Zero car types recorded for 90 days made every `car_type` `invalid_filter_value` with an
    empty legal list — and a healthy API never got asked again."""
    from tests.test_cars_repo import CarsHarness

    h = CarsHarness(tmp_path)
    types_url = f"{CARS_API}/v2/cr/carTypes"
    h.sess.route(types_url, json_response(types_url, {"content": []}))
    out = await cr_cars(h.rt, car_type="sedans")
    assert out.error is not None and out.error.code == "fetch_failed"
    assert out.error.reason == "empty_response" and out.error.retryable is True
    assert h.rt.cache.select_car_taxonomy(90, h.rt.clock()) is None
    h.sess.route(types_url, json_response(types_url, load_fixture("cars/cartypes.json")))
    again = await cr_cars(h.rt, car_type="sedans")
    assert again.error is None and again.data.total == 20


# --------------------------------------------------------------------------- single flight


async def test_concurrent_cold_searches_fetch_the_car_page_and_index_once(tmp_path):
    """Only `("car", id)` was single-flighted: two cold `cr_car_search` calls fetched the car
    page twice and the 3.1 MB `v1/cr/keys` twice (SPEC §8 *Concurrency*)."""
    import asyncio

    h = RuntimeHarness(tmp_path)
    page_gate, keys_gate = asyncio.Event(), asyncio.Event()

    async def slow_page(url, kw):
        await page_gate.wait()
        return FakeResponse(url=url, content=make_car_page(False))

    async def slow_keys(url, kw):
        await keys_gate.wait()
        return json_response(url, load_fixture("cars/keys.json"))

    h.sess.route(CARS_PAGE_URL, slow_page)
    h.sess.route(f"{CARS_API}/v1/cr/keys", slow_keys)
    tasks = [asyncio.ensure_future(cr_car_search(h.rt, "make b")) for _ in range(3)]
    await until(lambda: CARS_PAGE_URL in h.requests)  # the leader is at the page gate
    page_gate.set()
    await until(lambda: f"{CARS_API}/v1/cr/keys" in h.requests)  # …and at the keys gate
    keys_gate.set()
    results = await asyncio.gather(*tasks)
    assert all(r.error is None and len(r.data.cars) == 6 for r in results)
    assert h.requests == [CARS_PAGE_URL, f"{CARS_API}/v1/cr/keys"]
    assert h.rt.cars.page_fetches == 1


async def test_concurrent_cold_listings_fetch_the_taxonomy_once(tmp_path):
    import asyncio

    from tests.test_cars_repo import CarsHarness

    h = CarsHarness(tmp_path, n=4)
    gate = asyncio.Event()

    async def slow_types(url, kw):
        await gate.wait()
        return json_response(url, load_fixture("cars/cartypes.json"))

    h.sess.route(f"{CARS_API}/v2/cr/carTypes", slow_types)
    tasks = [asyncio.ensure_future(cr_cars(h.rt, car_type="sedans")) for _ in range(3)]
    await asyncio.sleep(0.01)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert all(r.error is None and len(r.data.cars) == 4 for r in results)
    assert len(h.api_requests("/v2/cr/carTypes")) == 1
    assert len(h.api_requests("/v2/cr/cars?")) == 3  # listings are per call; the taxonomy is not
