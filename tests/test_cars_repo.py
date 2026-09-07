"""P10.3 — request-cost discipline on the cars listing (PLAN §4.4)."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from consumer_reports_mcp.cars.repository import (
    CarsQueryError,
    build_listing_params,
    get_model_year,
    list_cars,
)
from consumer_reports_mcp.cars.tools import cr_car, cr_cars
from consumer_reports_mcp.config import CARS_API, CARS_LIMIT_SUMMARY_MAX, CARS_PAGE_URL
from consumer_reports_mcp.transport import FetchFailed
from tests.conftest import (
    FakeResponse,
    RuntimeHarness,
    json_response,
    load_fixture,
    make_car_page,
    until,
)


def _listing(n: int) -> dict:
    base = load_fixture("cars/cars_listing.json")
    entries = []
    for i in range(n):
        e = copy.deepcopy(base["content"][i % len(base["content"])])
        e["modelYearId"] = 700001 + i
        e["modelYear"] = 2026 - i
        entries.append(e)
    counts = [
        {"name": "makes", "value": 1},
        {"name": "cars", "value": n},
        {"name": "models", "value": max(1, n // 3)},
    ]
    return dict(base, content=entries, totalElements=max(1, n // 3), responseSize=n, counts=counts)


def _model_year(myid: int) -> dict:
    payload = copy.deepcopy(load_fixture("cars/modelyear.json"))
    payload["response"]["modelYear"]["modelYearId"] = myid
    return payload


class CarsHarness(RuntimeHarness):
    def __init__(self, tmp_path, n: int = 20) -> None:
        super().__init__(tmp_path)
        self.sess.route(
            CARS_PAGE_URL, FakeResponse(url=CARS_PAGE_URL, content=make_car_page(False))
        )
        self.sess.route(
            f"{CARS_API}/v2/cr/carTypes", json_response("u", load_fixture("cars/cartypes.json"))
        )
        self.listing = _listing(n)
        self.sess.route(f"{CARS_API}/v2/cr/cars", lambda url, kw: json_response(url, self.listing))
        self.sess.route(
            lambda u: "/v2/cr/modelYears/" in u,
            lambda url, kw: json_response(url, _model_year(int(url.rsplit("/", 1)[1]))),
        )

    def api_requests(self, fragment: str) -> list[str]:
        return [u for u in self.requests if fragment in u]

    def query(self, i: int = 0) -> dict:
        url = self.api_requests("/v2/cr/cars?")[i]
        return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


async def test_summary_issues_exactly_one_request(tmp_path):
    h = CarsHarness(tmp_path, n=20)
    out = await cr_cars(h.rt, make="Make A")
    assert out.error is None and len(out.data.cars) == 20
    assert (
        out.data.requests_made == 1
        and h.api_requests("/v2/cr/cars?")
        and not h.api_requests("modelYears")
    )
    assert len(h.requests) == 2  # the car page (key) plus ONE listing request


async def test_standard_issues_one_request_per_row(tmp_path):
    h = CarsHarness(tmp_path, n=10)
    out = await cr_cars(h.rt, make="Make A", detail="standard")
    assert out.error is None and len(out.data.cars) == 10
    assert out.data.requests_made == 11
    assert len(h.api_requests("modelYears")) == 10
    again = await cr_cars(h.rt, make="Make A", detail="standard")
    assert again.data.requests_made == 1 and len(h.api_requests("modelYears")) == 10  # all cached


async def test_limit_applied_after_fetch_not_passed_as_size(tmp_path):
    h = CarsHarness(tmp_path, n=20)
    out = await cr_cars(h.rt, make="Make A", limit=3, offset=2)
    assert len(out.data.cars) == 3 and out.data.total == 20 and out.data.truncated is True
    # `size` counts MODELS and is derived from `need`, never from `limit`; CR caps it at 50
    assert int(h.query()["size"]) <= 50 and h.query()["size"] not in ("3", "5")
    await cr_cars(h.rt, make="Make A", limit=100)
    assert int(h.query(1)["size"]) == 13  # ceil(100 rows / 8 years per model)


async def test_filters_combine(tmp_path):
    h = CarsHarness(tmp_path)
    out = await cr_cars(h.rt, car_type="sedans", make="Make A", year=2026, state="used")
    assert out.error is None
    q = h.query()
    assert q["carTypeSlugName"] == "sedans" and q["slugMakeName"] == "make-a"
    assert q["modelYear"] == "2026" and q["modelYearStateId"] == "1"
    with_cat = await cr_cars(h.rt, car_type="sedans", category=11359)
    assert with_cat.error is None and h.query(1)["categoryId"] == "11359"


async def test_state_defaults_to_both(tmp_path):
    h = CarsHarness(tmp_path, n=6)
    out = await cr_cars(h.rt, make="Make A")
    assert "modelYearStateId" not in h.query()
    assert {s for c in out.data.cars for s in c["states"]} == {"New", "Used"}
    assert all(isinstance(c["states"], list) and "state" not in c for c in out.data.cars)
    new_only = await cr_cars(h.rt, make="Make A", state="new")
    assert h.query(1)["modelYearStateId"] == "2" and new_only.error is None


async def test_cached_model_year_not_refetched(tmp_path):
    h = CarsHarness(tmp_path)
    s1 = await get_model_year(h.rt, 700001)
    s2 = await get_model_year(h.rt, 700001)
    assert (s1.from_cache, s1.requests) == (False, 1) and (s2.from_cache, s2.requests) == (True, 0)
    assert s1.fetched_at == s2.fetched_at and s1.payload == s2.payload
    assert len(h.api_requests("modelYears/700001")) == 1
    s3 = await get_model_year(h.rt, 700001, refresh=True)
    assert s3.from_cache is False and s3.requests == 1 and s3.refresh_failed is None


async def test_unknown_model_year_is_unknown_car_and_is_never_cached(tmp_path):
    """cars-api answers an unknown id with 200 + `{"response": {}, "responseCount": 0}`, not a
    404. Untranslated that becomes an all-null car whose every score reads `absent` — "CR
    published no value" for a car that does not exist — cached for the full 30-day TTL."""
    h = CarsHarness(tmp_path)
    empty = {"response": {}, "responseSummary": {"responseCount": 0, "requestQueryParameters": []}}
    h.sess.route(
        lambda u: "/v2/cr/modelYears/999999" in u, lambda url, kw: json_response(url, empty)
    )
    with pytest.raises(FetchFailed) as exc:
        await get_model_year(h.rt, 999999)
    assert exc.value.reason == "no_such_route"
    assert h.rt.cache.select_car_raw(999999, 30, h.rt.clock()) is None

    env = await cr_car(h.rt, 999999)
    assert env.error is not None and env.error.code == "unknown_car"
    assert env.data is None and env.scores_available is None


async def test_listing_pages_until_need_met(tmp_path):
    h = CarsHarness(tmp_path, n=5)
    page1 = dict(h.listing, last=False, totalPages=2, totalElements=8)
    page1["counts"] = [{"name": "cars", "value": 8}]
    page2 = dict(_listing(3), last=True, pageNumber=2, totalElements=8)
    for e in page2["content"]:
        e["modelYearId"] += 100

    def route(url, kw):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        return json_response(url, page2 if q.get("page") == "2" else page1)

    h.sess.route(f"{CARS_API}/v2/cr/cars", route)  # newest route wins
    out = await cr_cars(h.rt, make="Make A", limit=8)
    assert out.data.total == 8 and len(out.data.cars) == 8 and out.data.requests_made == 2
    small = await cr_cars(h.rt, make="Make A", limit=2)
    assert small.data.requests_made == 1  # no need for page 2


async def test_param_validation(tmp_path):
    h = CarsHarness(tmp_path)
    with pytest.raises(CarsQueryError) as ei:
        await build_listing_params(
            h.rt, car_type=None, category=11359, make=None, state=None, year=None
        )
    assert ei.value.extra["filter"] == "category"
    with pytest.raises(CarsQueryError):
        await build_listing_params(
            h.rt, car_type=None, category=None, make=None, state=None, year=2026
        )
    with pytest.raises(CarsQueryError):
        await build_listing_params(
            h.rt, car_type="trucks", category=None, make=None, state=None, year=None
        )
    with pytest.raises(CarsQueryError):
        await build_listing_params(
            h.rt, car_type="sedans", category=13762, make=None, state=None, year=None
        )
    listing = await list_cars(h.rt, {"slugMakeName": "make-a"}, need=5)
    assert listing.requests == 1 and listing.total == 20 and "size=2" in listing.url
    assert json.dumps(listing.entries[0]) and listing.last is True
    big = await list_cars(h.rt, {"slugMakeName": "make-a"}, need=400)
    assert "size=50" in big.url  # never above CR's cap


async def test_unknown_make_is_an_error_once_the_index_exists(tmp_path):
    h = CarsHarness(tmp_path)
    out = await cr_cars(h.rt, make="Nobody Motors")  # no index yet → sent through, no rows
    assert out.error is None and out.data.total == 20  # the fake listing answers regardless
    h.rt.cache.write_car_index(
        [
            {
                "model_year_id": 1,
                "make": "Make A",
                "slug_make": "make-a",
                "states": [],
                "car_types": [],
            }
        ],
        h.rt.clock(),
    )
    bad = await cr_cars(h.rt, make="Make Z")
    assert bad.error.code == "invalid_filter_value" and bad.error.filter == "make"
    assert bad.error.candidates is None  # nothing near `make-z`; the message points at the tool
    near = await cr_cars(h.rt, make="Make")
    assert near.error.filter == "make" and "did you mean" in near.error.message
    assert near.error.candidates == [{"slug": "make-a", "name": "Make A"}]  # slug AND CR's name
    ok = await cr_cars(h.rt, make="make a")
    assert ok.error is None


async def test_population_from_counts_list_not_total_elements(tmp_path):
    h = CarsHarness(tmp_path, n=20)
    assert h.listing["totalElements"] == 6  # models
    out = await cr_cars(h.rt, make="Make A", limit=5)
    assert out.data.total == 20 and out.data.truncated is True


# --------------------------------------------------------------------------- paging order


def _paged_route(makes: list[tuple[str, int]], years: int) -> tuple[list[int], Callable]:
    """A `v2/cr/cars` fake that pages like CR: `size` counts MODELS, a page holds whole models
    with their years contiguous (ASCENDING here, so the within-model stabilisation shows), and
    the native order is CR's own — case-insensitive by make, so `McLaren` precedes `MINI`,
    where Python's `str` order puts `MINI` first."""
    base = load_fixture("cars/cars_listing.json")["content"][0]
    models: list[list[dict]] = []
    ids: list[int] = []
    next_id = 900000
    for mi, (make, n_models) in enumerate(makes):
        for m in range(n_models):
            rows = []
            for y in range(years):
                next_id += 1
                e = copy.deepcopy(base)
                e.update(
                    modelYearId=next_id,
                    makeName=make,
                    slugMakeName=make.lower(),
                    modelName=f"{make} M{m}",
                    slugModelName=f"m{m}",
                    modelId=mi * 100 + m,
                    modelYear=2015 + y,
                    modelYearStateName="Used",
                )
                rows.append(e)
                ids.append(next_id)
            models.append(rows)
    population = len(ids)

    def route(url, kw):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        size, page = int(q["size"]), int(q["page"])
        chunk = models[(page - 1) * size : page * size]
        payload = {
            "content": [e for rows in chunk for e in rows],
            "counts": [
                {"name": "cars", "value": population},
                {"name": "models", "value": len(models)},
            ],
            "totalElements": len(models),
            "pageNumber": page,
            "size": size,
            "first": page == 1,
            "last": page * size >= len(models),
        }
        return json_response(url, payload)

    return ids, route


async def test_offsets_reach_every_model_year_exactly_once(tmp_path):
    """`offset` pages over a PREFIX sized from `need`, so the order must be prefix-stable. The
    earlier cross-model `(make, model, year)` sort of that prefix was not: with CR's native order
    differing from Python's string collation in one place (`McLaren` before `MINI`), rows came
    back at more than one offset and others at none."""
    h = CarsHarness(tmp_path)
    ids, route = _paged_route([("Acura", 4), ("McLaren", 4), ("MINI", 4)], years=8)
    h.sess.route(f"{CARS_API}/v2/cr/cars", route)
    rows: list[dict] = []
    offset = 0
    calls = 0
    while True:
        out = await cr_cars(h.rt, state="used", limit=25, offset=offset)
        assert out.error is None and out.data.total == 96
        rows.extend(out.data.cars)
        calls += 1
        if not out.data.truncated:
            break
        offset += 25
    seen = [r["model_year_id"] for r in rows]
    assert calls == 4 and len(seen) == 96
    assert len(set(seen)) == 96, "a model-year was returned at more than one offset"
    assert set(seen) == set(ids), "a model-year was never returned at any offset"
    # CR's model order is kept — McLaren before MINI, as CR sent it, not as Python sorts it
    makes = [m for i, m in enumerate(r["make"] for r in rows) if i == 0 or m != rows[i - 1]["make"]]
    assert makes == ["Acura", "McLaren", "MINI"]
    # and years descend within a model although the fake sent them ascending
    by_model: dict[str, list[int]] = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r["year"])
    assert all(ys == sorted(ys, reverse=True) for ys in by_model.values()) and len(by_model) == 12


async def test_total_is_unknown_not_the_prefix_when_counts_absent_and_pages_remain(tmp_path):
    """With no `counts`, `total` used to fall back to the fetched PREFIX, so `truncated` read
    false with pages remaining. Now it is null (and `truncated` true) until the last page."""
    h = CarsHarness(tmp_path, n=5)
    page1 = dict(h.listing, last=False, totalPages=2)
    page1.pop("counts")
    page2 = dict(_listing(3), last=True, pageNumber=2)
    page2.pop("counts")
    for e in page2["content"]:
        e["modelYearId"] += 100

    def route(url, kw):
        q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        return json_response(url, page2 if q.get("page") == "2" else page1)

    h.sess.route(f"{CARS_API}/v2/cr/cars", route)
    out = await cr_cars(h.rt, make="Make A", limit=5)  # page 1 alone satisfies need=5
    assert out.error is None and len(out.data.cars) == 5 and out.data.requests_made == 1
    assert out.data.total is None and out.data.truncated is True
    both = await cr_cars(h.rt, make="Make A", limit=8)  # fetched to the last page
    assert both.data.total == 8 and both.data.truncated is False and len(both.data.cars) == 8


# --------------------------------------------------------------------------- cache-first


async def test_challenged_refetch_serves_the_cached_row_with_a_warning(tmp_path):
    """SPEC §7: a cached row is served with `error: null` whenever one exists — on a 429 too.
    `get_model_year` used to fall back on `FetchFailed` only, so a rate-limited refetch of a
    stale row was a tool error with the row in hand; and a served stale row never said
    `refresh_failed` at all."""
    h = CarsHarness(tmp_path)
    first = await cr_car(h.rt, 700001)
    assert first.error is None and first.provenance.from_cache is False
    h.now += timedelta(days=31)  # past the 30-day TTL: the next call refetches
    h.sess.route(
        lambda u: u.endswith("/v2/cr/modelYears/700001"),
        FakeResponse(url="u", status_code=429, content=b"{}"),
    )
    out = await cr_car(h.rt, 700001)
    assert out.error is None and out.data["overall_score"] == 61
    assert out.provenance.from_cache is True and out.provenance.stale is True
    assert out.provenance.fetched_at == first.provenance.fetched_at
    assert "refresh_failed:challenged" in out.warnings
    # the same under `cr_cars(detail="standard")`: the row is served, the failure named once
    h.now += timedelta(hours=2)  # past the negative cache, so the 429 is hit again
    listing = await cr_cars(h.rt, make="Make A", detail="standard", limit=1)
    assert listing.error is None and listing.data.cars[0]["overall_score"] == 61
    assert listing.warnings.count("refresh_failed:challenged") == 1
    assert listing.data.cars[0]["ratings_from_cache"] is True
    # and a plain transport failure names itself the same way
    h.now += timedelta(hours=2)
    h.sess.route(
        lambda u: u.endswith("/v2/cr/modelYears/700001"),
        FakeResponse(url="u", status_code=500, content=b"x"),
    )
    failed = await cr_car(h.rt, 700001)
    assert failed.error is None and "refresh_failed:fetch_failed" in failed.warnings


async def test_challenged_car_is_negatively_cached_for_an_hour(tmp_path):
    """SPEC §7 negatively caches `challenged` for an hour; the cars path re-hit a challenged
    API on every call."""
    h = CarsHarness(tmp_path)
    h.sess.route(
        lambda u: u.endswith("/v2/cr/modelYears/700002"),
        FakeResponse(url="u", status_code=429, content=b"{}"),
    )
    a = await cr_car(h.rt, 700002)
    assert a.error.code == "challenged" and a.error.reason == "rate_limited"
    n = len(h.api_requests("modelYears/700002"))
    b = await cr_car(h.rt, 700002)
    assert b.error.code == "challenged" and b.error.http_status == 429
    assert len(h.api_requests("modelYears/700002")) == n, "re-hit inside the negative TTL"
    h.now += timedelta(hours=1)
    h.sess.route(
        lambda u: u.endswith("/v2/cr/modelYears/700002"),
        lambda url, kw: json_response(url, _model_year(700002)),
    )
    c = await cr_car(h.rt, 700002)
    assert c.error is None and len(h.api_requests("modelYears/700002")) == n + 1


# --------------------------------------------------------------------------- leader cancellation


async def test_a_cancelled_cr_car_does_not_kill_a_concurrent_standard_listing(tmp_path):
    """The reviewer's cars shape: `cr_car(700001)` leads the `("car", 700001)` flight,
    `cr_cars(detail="standard")` joins it for its first row, the client cancels `cr_car`. The
    listing's `except (FetchFailed, Challenged)` never expected a `CancelledError`, so the whole
    listing died with it."""
    import asyncio

    h = CarsHarness(tmp_path, n=2)
    gate = asyncio.Event()

    async def slow(url, kw):
        await gate.wait()
        return json_response(url, _model_year(int(url.rsplit("/", 1)[1])))

    h.sess.route(lambda u: "/v2/cr/modelYears/" in u, slow)
    car = asyncio.ensure_future(cr_car(h.rt, 700001))
    await until(lambda: h.api_requests("modelYears/700001"))  # the car page, then the model-year
    assert h.api_requests("modelYears/700001") == [f"{CARS_API}/v2/cr/modelYears/700001"]
    listing = asyncio.ensure_future(cr_cars(h.rt, make="Make A", detail="standard", limit=2))
    flights = h.rt.transport.single_flight._inflight
    await until(lambda: flights[("car", 700001)].callers == 2)  # row 0 joined the flight
    car.cancel()
    with pytest.raises(asyncio.CancelledError):
        await car
    gate.set()
    out = await listing
    assert out.error is None and len(out.data.cars) == 2
    assert not any(w.startswith("ratings_unavailable") for w in out.warnings)
    assert [c["overall_score"] for c in out.data.cars] == [61, 61]
    assert len(h.api_requests("modelYears/700001")) == 1  # the one flight served both


# --------------------------------------------------------------------------- `year`


def _index_rows(years: list[int]) -> list[dict]:
    return [
        {
            "model_year_id": 900000 + i,
            "make": "Make A",
            "slug_make": "make-a",
            "model": "Model X",
            "slug_model": "model-x",
            "year": y,
            "states": ["Used"],
            "car_types": [],
        }
        for i, y in enumerate(years)
    ]


async def test_year_outside_the_catalogue_is_a_filter_error_and_costs_no_request(tmp_path):
    """`v2/cr/cars` answers a `modelYear` outside the catalogue with a 400 — "'modelYear' must
    be a valid year integer" (RECON §13c-ii: 1999 and 2029 both 400 against an index of
    2000–2028) — which reached the caller as `fetch_failed(bad_request)`: a transport error for
    an ordinary question about a 1990 Honda. The legal span is the catalogue's own, read from
    the index like `make`'s legal slugs. Below it, or more than one year above it, costs no
    request; max+1 is CR's to answer (next test)."""
    h = CarsHarness(tmp_path)
    assert h.rt.cache.car_year_range() is None  # no index yet
    h.rt.cache.write_car_index(_index_rows([2000, 2014, 2028]), h.rt.clock())
    assert h.rt.cache.car_year_range() == (2000, 2028)
    before = len(h.api_requests("/v2/cr/cars?"))
    for bad in (1990, 1999, 2030, 2035):
        out = await cr_cars(h.rt, make="Make A", year=bad)
        assert out.error is not None and out.data is None, bad
        assert out.error.code == "invalid_filter_value" and out.error.filter == "year"
        assert "2000–2028" in out.error.message
        assert out.error.candidates == [{"min": 2000, "max": 2028}]
    assert len(h.api_requests("/v2/cr/cars?")) == before  # never sent
    ok = await cr_cars(h.rt, make="Make A", year=2014)
    assert ok.error is None and h.query(-1)["modelYear"] == "2014"


def _cr_rejects_model_year(url, kw):
    return FakeResponse(
        url=url,
        status_code=400,
        content=b'{"errors":[{"httpCode":400,"category":"USER","key":"default",'
        b'"text":"\'modelYear\' must be a valid year integer."}]}',
    )


async def test_one_year_above_the_index_max_is_sent_to_cr_and_answered_the_same(tmp_path):
    """The index is a snapshot (90-day TTL, refreshed only by `cr_car_search`) and a new model
    year lands at exactly max+1, so refusing max+1 locally would answer "2029 is outside CR's
    catalogue" for cars CR lists until the index refreshes. Sent through, CR is the authority:
    while the index is current it answers the 400 (measured 2026-09-06: 2029 → 400 with the
    index at 2028), translated to the SAME error the local check raises — code, filter,
    span in the message, candidates — at the cost of one request. Max+2 is still refused
    locally: a catalogue does not gain two model years inside one index TTL."""
    h = CarsHarness(tmp_path)
    h.rt.cache.write_car_index(_index_rows([2000, 2014, 2028]), h.rt.clock())
    local = await cr_cars(h.rt, make="Make A", year=2030)
    assert local.error.code == "invalid_filter_value" and not h.api_requests("/v2/cr/cars?")
    h.sess.route(f"{CARS_API}/v2/cr/cars", _cr_rejects_model_year)
    out = await cr_cars(h.rt, make="Make A", year=2029)
    assert len(h.api_requests("/v2/cr/cars?")) == 1 and h.query()["modelYear"] == "2029"
    assert out.error is not None and out.data is None
    assert out.error.code == local.error.code == "invalid_filter_value"
    assert out.error.filter == local.error.filter == "year"
    assert out.error.candidates == local.error.candidates == [{"min": 2000, "max": 2028}]
    assert "2029" in out.error.message and "2000–2028" in out.error.message
    assert out.error.reason is None  # a filter error, never `fetch_failed(bad_request)`


async def test_a_lagging_index_does_not_refuse_the_model_year_cr_lists(tmp_path):
    """The case the allowance exists for: CR has added 2029 model-years, the local index (max
    2028) has not been refreshed. A local refusal was a false absence — "outside Consumer
    Reports' catalogue" — that `refresh=True` on `cr_cars` cannot clear. CR's rows come back
    as a normal data envelope."""
    h = CarsHarness(tmp_path, n=4)
    h.rt.cache.write_car_index(_index_rows([2000, 2014, 2028]), h.rt.clock())
    for e in h.listing["content"]:
        e["modelYear"] = 2029
    out = await cr_cars(h.rt, make="Make A", year=2029)
    assert out.error is None and len(out.data.cars) == 4
    assert {c["year"] for c in out.data.cars} == {2029}
    assert h.query()["modelYear"] == "2029"
    assert not any(w.startswith("no_results") for w in out.warnings)


async def test_year_answer_does_not_depend_on_whether_the_index_was_fetched(tmp_path):
    """The invariant the allowance must preserve (CLAUDE.md): for every year, the envelope's
    `error.code`/`filter` and `data` presence are the same with and without an index. Only the
    request count and the hint text (the span, once known) may differ."""
    years = (1990, 1999, 2000, 2014, 2028, 2029, 2030)

    async def answers(with_index: bool) -> dict[int, tuple]:
        h = CarsHarness(tmp_path / ("indexed" if with_index else "cold"))
        if with_index:
            h.rt.cache.write_car_index(_index_rows([2000, 2014, 2028]), h.rt.clock())

        def cr(url, kw):
            q = parse_qs(urlsplit(url).query)
            y = int(q["modelYear"][0])
            if not 2000 <= y <= 2028:  # CR's measured rule: the catalogue's span, exactly
                return _cr_rejects_model_year(url, kw)
            return json_response(url, h.listing)

        h.sess.route(f"{CARS_API}/v2/cr/cars", cr)
        seen = {}
        for y in years:
            out = await cr_cars(h.rt, make="Make A", year=y)
            code = out.error.code if out.error else None
            flt = out.error.filter if out.error else None
            seen[y] = (code, flt, out.data is not None)
        return seen

    cold = await answers(False)
    warm = await answers(True)
    assert cold == warm
    assert warm[2029] == ("invalid_filter_value", "year", False)
    assert warm[2028] == (None, None, True) and warm[2000] == (None, None, True)


async def test_year_must_be_an_integer_model_year(tmp_path):
    h = CarsHarness(tmp_path)
    for bad in (True, "next year", 2024.5):
        out = await cr_cars(h.rt, make="Make A", year=bad)  # type: ignore[arg-type]
        assert out.error.code == "invalid_filter_value" and out.error.filter == "year", bad
    assert not h.api_requests("/v2/cr/cars?")
    ok = await cr_cars(h.rt, make="Make A", year="2024")  # type: ignore[arg-type]
    assert ok.error is None and h.query()["modelYear"] == "2024"


async def test_cr_rejecting_model_year_is_the_same_filter_error_without_an_index(tmp_path):
    """With the index not fetched yet the value passes through, and CR's 400 is translated to
    the same `invalid_filter_value` the index check raises — the answer does not depend on
    whether `cr_car_search` happened to run first. A 400 with NO `year` stays what it is."""
    h = CarsHarness(tmp_path)
    h.sess.route(
        f"{CARS_API}/v2/cr/cars",
        lambda url, kw: FakeResponse(
            url=url,
            status_code=400,
            content=b'{"errors":[{"httpCode":400,"text":"modelYear must be a valid year"}]}',
        ),
    )
    out = await cr_cars(h.rt, make="Make A", year=1990)
    assert out.error.code == "invalid_filter_value" and out.error.filter == "year"
    assert "1990" in out.error.message and out.error.candidates is None
    assert "cr_car_search" in out.error.message  # no span to quote yet: point at the tool
    plain = await cr_cars(h.rt, make="Make A")
    assert plain.error.code == "fetch_failed" and plain.error.reason == "bad_request"
    assert plain.error.http_status == 400 and plain.error.retryable is False


async def test_empty_listing_content_is_a_dict(tmp_path):
    """An empty `v2/cr/cars` page ships `"content": {}` — a dict, not `[]` (RECON §13c-ii)."""
    h = CarsHarness(tmp_path)
    h.listing = {
        "content": {},
        "last": True,
        "counts": [{"name": "makes", "value": 1}, {"name": "cars", "value": 0}],
        "totalElements": 0,
    }
    out = await cr_cars(h.rt, make="Make A", year=2027)
    assert out.error is None and out.data.cars == [] and out.data.total == 0
    assert out.data.truncated is False
    assert "no_results:make=Make%20A,year=2027" in out.warnings
    # zero rows were looked at, so no key may claim CR published no value: null, not `absent`
    assert all(v is None for v in out.scores_available.model_dump().values())


async def test_empty_page_reports_scores_as_not_looked_not_absent(tmp_path):
    """`any()` over zero rows is False, so an emptied page — `offset` past the end here —
    used to report every key `absent`: "CR published no value", derived from nothing. The
    products side made the same vacuous-truth mistake on empty categories."""
    h = CarsHarness(tmp_path, n=4)
    out = await cr_cars(h.rt, make="Make A", offset=10, detail="standard")
    assert out.error is None and out.data.cars == [] and out.data.total == 4
    assert all(v is None for v in out.scores_available.model_dump().values())
    full = await cr_cars(h.rt, make="Make A", detail="standard")
    assert full.scores_available.safety_verdict == "available"


async def test_transport_errors_carry_the_status_that_caused_them(tmp_path):
    h = CarsHarness(tmp_path)
    h.sess.route(
        lambda u: u.endswith("/v2/cr/modelYears/5"),
        FakeResponse(url="u", status_code=503, content=b"{}"),
    )
    out = await cr_car(h.rt, 5)
    assert out.error.code == "fetch_failed" and out.error.reason == "server_error"
    assert out.error.http_status == 503 and out.error.retryable is True


async def test_list_cars_raises_the_year_rejection_itself(tmp_path):
    """The decision that CR's `400` on `modelYear` is a filter answer, not a transport failure,
    is made beside the request — in the repository, where the products path makes the same
    kind of call — so `cr_cars` catches `CarsQueryError` like any other and is never handed
    the caller's raw `year` back to re-decide from the exception path. The `400` is still
    the cause. Without `modelYear` sent it stays what it is."""
    h = CarsHarness(tmp_path)
    h.sess.route(f"{CARS_API}/v2/cr/cars", _cr_rejects_model_year)
    with pytest.raises(CarsQueryError) as qe:
        await list_cars(h.rt, {"slugMakeName": "make-a", "modelYear": 1990}, need=5)
    assert qe.value.code == "invalid_filter_value" and qe.value.extra["filter"] == "year"
    assert "1990" in qe.value.message
    assert isinstance(qe.value.__cause__, FetchFailed)
    assert qe.value.__cause__.reason == "bad_request"
    with pytest.raises(FetchFailed) as fe:
        await list_cars(h.rt, {"slugMakeName": "make-a"}, need=5)
    assert fe.value.reason == "bad_request" and fe.value.http_status == 400


async def test_cars_filter_errors_quote_caller_text_bounded(tmp_path):
    """The same bound on the cars surface: `car_type`, `make` and `year` echo the caller's
    token through `envelope.quoted`; a `category` that is not an int is a typed error rather
    than the `ValueError` `int()` used to raise out of the tool."""
    h = CarsHarness(tmp_path)
    out = await cr_cars(h.rt, car_type="T" * 200_000)
    assert out.error.filter == "car_type" and len(out.error.message) < 600
    assert "(200,000 chars)" in out.error.message
    out = await cr_cars(h.rt, car_type="sedans", category="abc")
    assert out.error.code == "invalid_filter_value" and out.error.filter == "category"
    assert "'abc'" in out.error.message
    out = await cr_cars(h.rt, make="m", year="Y\n" * 1000)
    assert out.error.filter == "year" and "\n" not in out.error.message
    assert len(out.error.message) < 300 and h.api_requests("/v2/cr/cars?") == []
    h.rt.cache.write_car_index(
        [
            {
                "model_year_id": 1,
                "make": "Make A",
                "slug_make": "make-a",
                "states": [],
                "car_types": [],
            }
        ],
        h.rt.clock(),
    )
    out = await cr_cars(h.rt, make="M" * 200_000)
    assert out.error.filter == "make" and len(out.error.message) < 400
    assert out.error.candidates is None


async def test_cars_invalid_filter_value_carries_the_legal_set_in_candidates(tmp_path):
    """The cars surface, same rule (SPEC §7 *Error taxonomy*): `car_type` was seven legal slugs
    in the prose with `candidates: null`; now `{id, slug, name}` rows from the taxonomy,
    `{id, name}` for a category under a type, `{value}` for `state`/`detail`, `{min, max}` for
    `limit`/`offset`/`year`. No listing request is spent on any of them."""
    h = CarsHarness(tmp_path)
    types = await cr_cars(h.rt, car_type="suv")
    assert types.error.filter == "car_type" and types.error.candidates == [
        {"id": 105, "slug": "sedans", "name": "Sedans & Hatchbacks"},
        {"id": 107, "slug": "hybrids-evs", "name": "Hybrids/EVs"},
    ]
    cat = await cr_cars(h.rt, car_type="sedans", category=99999)
    assert cat.error.filter == "category" and cat.error.candidates == [
        {"id": 11331, "name": "Small sedans"},
        {"id": 11359, "name": "Electric sedans"},
    ]
    # a category that is not an id, or one with no `car_type` to scope it, has no legal set
    # to offer under `filter: "category"` — the fix is a companion parameter, not a value
    for kwargs in ({"car_type": "sedans", "category": "abc"}, {"category": 11359}):
        out = await cr_cars(h.rt, **kwargs)
        assert out.error.filter == "category" and out.error.candidates is None, kwargs
    state = await cr_cars(h.rt, make="Make A", state="certified")
    assert state.error.filter == "state" and state.error.candidates == [
        {"value": "new"},
        {"value": "used"},
    ]
    detail = await cr_cars(h.rt, make="Make A", detail="full")
    assert detail.error.candidates == [{"value": "summary"}, {"value": "standard"}]
    for kwargs, span in (
        ({"detail": "standard", "limit": 16}, {"min": 1, "max": 15}),
        ({"limit": 0}, {"min": 1, "max": CARS_LIMIT_SUMMARY_MAX}),
    ):
        out = await cr_cars(h.rt, make="Make A", **kwargs)
        assert out.error.filter == "limit" and out.error.candidates == [span], kwargs
    offset = await cr_cars(h.rt, make="Make A", offset=-1)
    assert offset.error.filter == "offset" and offset.error.candidates == [{"min": 0, "max": None}]
    # the unfiltered refusal names no value at all: `car_type` is the parameter to ADD
    none = await cr_cars(h.rt, year=2020)
    assert none.error.filter == "car_type" and none.error.candidates is None
    assert h.api_requests("/v2/cr/cars?") == []
    car = await cr_car(h.rt, 700001, detail="summary")
    assert car.error.filter == "detail" and car.error.candidates == [
        {"value": "standard"},
        {"value": "full"},
    ]
    assert car.provenance is None and h.api_requests("modelYears") == []


async def test_cars_year_that_is_not_a_year_offers_the_span_once_known(tmp_path):
    """`year="soon"` is the same `filter: "year"` error as 1990, so it carries the same
    `{min, max}` — once the index has been fetched; before that there is no span to offer."""
    h = CarsHarness(tmp_path)
    cold = await cr_cars(h.rt, make="Make A", year="soon")
    assert cold.error.filter == "year" and cold.error.candidates is None
    h.rt.cache.write_car_index(
        [
            {
                "model_year_id": i,
                "make": "Make A",
                "slug_make": "make-a",
                "year": y,
                "states": [],
                "car_types": [],
            }
            for i, y in ((1, 2000), (2, 2028))
        ],
        h.rt.clock(),
    )
    warm = await cr_cars(h.rt, make="Make A", year="soon")
    assert warm.error.filter == "year" and warm.error.candidates == [{"min": 2000, "max": 2028}]
    assert h.api_requests("/v2/cr/cars?") == []
