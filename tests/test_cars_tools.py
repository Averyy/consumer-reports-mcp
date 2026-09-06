"""P10.4 — car shapes and the three cars envelopes (SPEC §5 *Cars*, §7)."""

from __future__ import annotations

import copy

from consumer_reports_mcp.cars.normalize import car_scores_available, car_shape, listing_shape, y_n
from consumer_reports_mcp.cars.tools import cr_car, cr_cars
from consumer_reports_mcp.config import CARS_API, CARS_LIMIT_STANDARD_MAX
from tests.conftest import FakeResponse, load_fixture
from tests.test_cars_repo import CarsHarness


def test_car_full_shape_from_fixture():
    payload = load_fixture("cars/modelyear.json")
    warnings: list[str] = []
    s = car_shape(payload, "full", warnings)
    assert s["make"] == "Make A" and s["model"] == "Model X" and s["year"] == 2026
    assert s["overall_score"] == 61 and s["road_test_score"] == 70 and s["rank"] == 3
    assert s["score_range"] == {"min": 45.0, "max": 80.0} and s["sort_index"] == 12
    assert s["recommended"] is True and warnings == []
    assert s["predicted_reliability"] == 52 and s["owner_satisfaction"] == 4.0
    assert s["car_type"] == {"id": 105, "name": "Sedans & Hatchbacks"}
    assert s["category"] == {"id": 11359, "name": "Electric sedans"}
    assert s["ratings"] and s["ratings"][0]["tests"]
    assert s["feature_group_ratings"] and s["crash_tests"] and s["specs"]["trims"]
    assert s["warranty"] and s["fuel_economy"]["cruise_range_miles"] is not None
    std = car_shape(payload, "standard")
    assert "ratings" not in std and "crash_tests" not in std and std["overall_score"] == 61


def test_y_n_mapping_and_warning():
    w: list[str] = []
    assert y_n("Y", w) is True and y_n("N", w) is False and y_n(None, w) is None and w == []
    assert y_n("Yes", w) is None and w == ["coercion_failed:isRecommended"]
    assert y_n(True, w) is None and w == ["coercion_failed:isRecommended"]


def test_car_nulls_are_absences_not_gating():
    payload = copy.deepcopy(load_fixture("cars/modelyear.json"))
    car = payload["response"]["modelYear"]["cars"][0]
    car["testRatings"]["overallTestScore"] = None
    car["ratingsCategory"]["overallRank"] = None
    payload["response"]["model"]["reliabilityRatings"]["predictedReliabilityScore"] = None
    s = car_shape(payload, "standard")
    assert s["overall_score"] is None and s["rank"] is None
    sa = car_scores_available(
        [s], ("overall_score", "road_test_score", "predicted_reliability", "recommended_flag")
    )
    assert sa == {
        "overall_score": "absent",
        "road_test_score": "available",
        "predicted_reliability": "absent",
        "recommended_flag": "available",
    }
    assert "unavailable" not in sa.values()


def test_summary_omits_roadtest_keys_and_reports_only_listing_keys():
    entry = load_fixture("cars/cars_listing.json")["content"][0]
    row = listing_shape(entry)
    for key in ("overall_score", "road_test_score", "rank", "predicted_reliability"):
        assert key not in row
    assert (
        row["safety_verdict"] == 5.0
        and row["popular_score"] == 71
        and row["fuel_economy"]["cruise_range_miles"] == 420
    )
    sa = car_scores_available([row], ("safety_verdict", "popular_score", "fuel_economy"))
    assert sa == {
        "safety_verdict": "available",
        "popular_score": "available",
        "fuel_economy": "available",
    }


async def test_summary_envelope_reports_only_listing_keys(tmp_path):
    h = CarsHarness(tmp_path, n=6)
    out = await cr_cars(h.rt, make="Make A")
    dumped = out.model_dump()
    assert "auth_state" not in dumped and "data_tier" not in dumped["provenance"]
    sa = dumped["scores_available"]
    assert sa["overall_score"] is None and sa["safety_verdict"] == "available"
    assert all(v in ("available", "absent", None) for v in sa.values())
    assert "overall_score" not in out.data.cars[0]
    # CR's own model order, years descending within a model — never by score, and never a
    # cross-model sort (that was not consistent across offsets; see test_cars_repo)
    assert [c["model_year_id"] for c in out.data.cars] == [700001 + i for i in range(6)]
    assert all(isinstance(c["states"], list) for c in out.data.cars)


async def test_standard_fetches_only_page_of_results(tmp_path):
    h = CarsHarness(tmp_path, n=6)
    out = await cr_cars(h.rt, make="Make A", detail="standard", limit=3)
    assert len(out.data.cars) == 3 and out.data.truncated is True and out.data.total == 6
    assert len(h.api_requests("modelYears")) == 3 and out.data.requests_made == 4
    row = out.data.cars[0]
    assert row["overall_score"] == 61 and row["ratings_from_cache"] is False
    assert (
        out.scores_available.overall_score == "available"
        and out.scores_available.recommended_flag == "available"
    )
    # the cap is 15, not 25: at ~2.16 s per row a 25-row page projected to ~51 s against Claude
    # Desktop's 60 s tool-call limit, with nothing left for one slow response
    over = await cr_cars(h.rt, make="Make A", detail="standard", limit=CARS_LIMIT_STANDARD_MAX + 1)
    assert over.error.code == "invalid_filter_value" and "15" in over.error.message
    assert CARS_LIMIT_STANDARD_MAX == 15
    at_cap = await cr_cars(h.rt, make="Make A", detail="standard", limit=CARS_LIMIT_STANDARD_MAX)
    assert at_cap.error is None and len(at_cap.data.cars) == 6


async def test_cars_category_requires_car_type(tmp_path):
    h = CarsHarness(tmp_path)
    out = await cr_cars(h.rt, category=11359)
    assert out.error.code == "invalid_filter_value" and out.error.filter == "category"
    assert out.data is None and not h.api_requests("/v2/cr/cars?")
    none = await cr_cars(h.rt)
    assert none.error.code == "invalid_filter_value"


async def test_cars_same_category_two_types_differ(tmp_path):
    h = CarsHarness(tmp_path)
    a = await cr_cars(h.rt, car_type="sedans", category=11359)
    b = await cr_cars(h.rt, car_type="hybrids-evs", category=11359)
    assert a.error is None and b.error is None
    assert (
        h.query(0)["carTypeSlugName"] == "sedans" and h.query(1)["carTypeSlugName"] == "hybrids-evs"
    )
    assert h.query(0)["categoryId"] == h.query(1)["categoryId"] == "11359"


async def test_cars_envelope_omits_auth_state_and_data_tier(tmp_path):
    h = CarsHarness(tmp_path)
    out = await cr_car(h.rt, 700001)
    dumped = out.model_dump()
    assert "auth_state" not in dumped and "data_tier" not in dumped["provenance"]
    assert dumped["session"] == "none" and out.error is None
    assert out.data["overall_score"] == 61 and out.provenance.from_cache is False
    assert out.provenance.cr_url == f"{CARS_API}/v2/cr/modelYears/700001"
    again = await cr_car(h.rt, 700001, detail="full")
    assert again.provenance.from_cache is True and again.data["ratings"]
    assert len(h.api_requests("modelYears/700001")) == 1


async def test_cr_car_unknown_id(tmp_path):
    h = CarsHarness(tmp_path)
    h.sess.routes.insert(
        0,
        (
            lambda u: "/v2/cr/modelYears/1" in u,
            FakeResponse(url="u", status_code=403, content=b"{}"),
        ),
    )
    out = await cr_car(h.rt, 1)
    assert out.error.code == "unknown_car" and "cr_car_search" in out.error.message


async def test_cr_car_junk_id_is_unknown_car_with_no_request(tmp_path):
    """`model_year_id` used to be bound to SQLite as whatever `int()` made of it: a 20-digit id
    raised OverflowError out of the tool, and a non-integer became a `-1` that cost a real
    `modelYears/-1` request. `cache.row_id` decides what can be a key; the rest is `unknown_car`
    with the envelope intact and nothing fetched."""
    h = CarsHarness(tmp_path)
    for token in (10**20, "9" * 20, "²", "①", "1" * 5000, -1, 0, "abc"):
        out = await cr_car(h.rt, token)
        assert out.error.code == "unknown_car" and "cr_car_search" in out.error.message
        assert out.session == "none" and isinstance(out.warnings, list)
    assert h.api_requests("modelYears") == []


async def test_cars_ratings_failure_per_row_is_a_warning(tmp_path):
    h = CarsHarness(tmp_path, n=2)
    h.sess.routes.insert(
        0,
        (
            lambda u: "/v2/cr/modelYears/700002" in u,
            FakeResponse(url="u", status_code=500, content=b"x"),
        ),
    )
    out = await cr_cars(h.rt, make="Make A", detail="standard")
    assert out.error is None and any(
        w.startswith("ratings_unavailable:700002") for w in out.warnings
    )
    assert out.data.cars[0]["overall_score"] == 61 and "overall_score" not in out.data.cars[1]


async def test_standard_with_no_ratings_fetched_reports_not_absent(tmp_path):
    h = CarsHarness(tmp_path, n=2)
    ok = await cr_cars(h.rt, make="Make A", detail="standard")
    assert ok.scores_available.overall_score == "available"
    # when NO row's ratings could be fetched the standard keys are not reported, never "absent"
    h2 = CarsHarness(tmp_path / "b", n=2)
    h2.sess.route(
        lambda u: "/v2/cr/modelYears/" in u, FakeResponse(url="u", status_code=500, content=b"x")
    )
    none = await cr_cars(h2.rt, make="Make A", detail="standard")
    assert none.error is None and none.scores_available.overall_score is None
    assert none.scores_available.safety_verdict == "available"
    assert sum(1 for w in none.warnings if w.startswith("ratings_unavailable:")) == 2


async def test_cars_api_429_is_challenged_and_400_is_bad_request(tmp_path):
    h = CarsHarness(tmp_path)
    h.sess.route(
        lambda u: u.endswith("/v2/cr/modelYears/2"),
        FakeResponse(url="u", status_code=429, content=b"{}"),
    )
    out = await cr_car(h.rt, 2)
    assert out.error.code == "challenged" and out.error.reason == "rate_limited"
    h.sess.route(
        lambda u: u.endswith("/v2/cr/modelYears/3"),
        FakeResponse(url="u", status_code=400, content=b"{}"),
    )
    bad = await cr_car(h.rt, 3)
    assert bad.error.code == "fetch_failed" and bad.error.reason == "bad_request"


async def test_empty_listing_carries_a_no_results_warning(tmp_path):
    """Keyed by the TOOL's parameters. A caller who passed `state="used"` cannot act on
    `modelYearStateId=1` — a key it never typed, whose mapping (used=1) reads backwards."""
    h = CarsHarness(tmp_path, n=0)
    out = await cr_cars(h.rt, make="Make A", state="used")
    assert out.error is None and out.data.cars == []
    warning = next(w for w in out.warnings if w.startswith("no_results:"))
    assert warning == "no_results:make=Make%20A,state=used"
    assert "slugMakeName" not in warning and "modelYearStateId" not in warning


def test_car_states_is_always_a_list():
    """`cr_car.state` was a string for one state, a list for several and null for none, while
    `cr_cars` rows carried a string and `cr_car_search` a list. One shape everywhere: `states`,
    a list (SPEC §5 *Cars*)."""
    payload = copy.deepcopy(load_fixture("cars/modelyear.json"))
    one = car_shape(payload, "standard")
    assert one["states"] == ["New"] and "state" not in one
    payload["response"]["modelYear"]["modelYearStates"].append({"modelYearStateName": "Used"})
    assert car_shape(payload, "standard")["states"] == ["New", "Used"]
    payload["response"]["modelYear"]["modelYearStates"] = []
    assert car_shape(payload, "standard")["states"] == []
    row = listing_shape(load_fixture("cars/cars_listing.json")["content"][0])
    assert row["states"] == ["New"] and "state" not in row
    assert listing_shape({"modelYearId": 1})["states"] == []


# --------------------------------------------------------------------------- batch-3 findings


def test_spec_names_the_standard_limit_cap():
    from pathlib import Path

    spec = (Path(__file__).resolve().parents[1] / "SPEC.md").read_text(encoding="utf-8")
    assert f"**{CARS_LIMIT_STANDARD_MAX} in `standard`**" in spec
    assert "**25 in `standard`**" not in spec


async def test_unfiltered_cr_cars_is_refused_before_any_request(tmp_path):
    """`v2/cr/cars` answers an unfiltered call with a `400` requiring one of `slugMakeName`,
    `modelYearStateId` or `carTypeSlugName` (RECON §13c-ii), so the tool refuses locally and
    costs nothing. This is ALSO what keeps `no_results:<params>` well-formed: an unfiltered
    listing with zero rows had nothing to name, and the bare `no_results:` failed the registry
    at envelope construction — a crash. `year` and `category` alone do not satisfy CR."""
    for n in (0, 3):
        h = CarsHarness(tmp_path / str(n), n=n)
        for kwargs in ({}, {"offset": 99999}, {"year": 2020}, {"detail": "standard"}):
            out = await cr_cars(h.rt, **kwargs)
            assert out.error is not None and out.error.code == "invalid_filter_value", kwargs
            assert out.error.filter == "car_type" and "car_type, make or state" in out.error.message
            assert out.data is None and out.warnings == [] and out.session == "none"
        assert h.api_requests("/v2/cr/cars") == []


async def test_offset_past_the_end_is_an_empty_page_not_no_results(tmp_path):
    """An `offset` past the end is the caller's own paging over rows the filters DID match:
    the honest answer is an empty page with `total` intact, `truncated: false`, no warning and
    no error — `no_results` would say the filters excluded everything, which they did not."""
    h = CarsHarness(tmp_path, n=3)
    out = await cr_cars(h.rt, make="Make A", offset=99999)
    assert out.error is None and out.data.cars == []
    assert out.data.total == 3 and out.data.truncated is False
    assert not any(w.startswith("no_results:") for w in out.warnings)
    assert out.scores_available.safety_verdict is None  # nothing was looked at


async def test_empty_filtered_listing_names_every_filter_and_never_paging(tmp_path):
    """The rendered detail is the tool's filters and only those: `offset`/`limit`/`detail`
    are paging, and a `no_results` naming them would send the caller to widen the wrong knob."""
    h = CarsHarness(tmp_path, n=0)
    out = await cr_cars(h.rt, make="Make A", state="new", year=2020, offset=5, limit=3)
    assert out.error is None and out.data.cars == [] and out.data.total == 0
    warning = next(w for w in out.warnings if w.startswith("no_results:"))
    assert warning == "no_results:make=Make%20A,state=new,year=2020"
    assert "offset" not in warning and "limit" not in warning


async def test_no_results_emission_does_not_depend_on_the_unfiltered_refusal(
    tmp_path, monkeypatch
):
    """The emission is gated on the tool's own filters, like `spec.active` on products — NOT
    on `build_listing_params` refusing an unfiltered call first. With that refusal lifted and
    CR answering zero rows, the answer is a proper envelope (an empty page, no `no_results`,
    since no filter excluded anything), never a construction-time crash on `no_results:`."""
    import consumer_reports_mcp.cars.tools as tools_mod

    async def unfiltered(rt, **kwargs):
        return {}

    monkeypatch.setattr(tools_mod, "build_listing_params", unfiltered)
    h = CarsHarness(tmp_path, n=0)
    out = await cr_cars(h.rt)
    assert out.error is None and out.data.cars == [] and out.data.total == 0
    assert not any(w.startswith("no_results") for w in out.warnings)
    assert h.api_requests("/v2/cr/cars")  # the listing was really asked and really empty
