"""P8.1 — the isolated second envelope (SPEC §7 `cr_reliability`, RECON §9)."""

from __future__ import annotations

from consumer_reports_mcp.config import WWW
from consumer_reports_mcp.credentials import SessionHealth
from consumer_reports_mcp.reliability import cr_reliability, parse_reliability
from tests.conftest import (
    FakeResponse,
    RuntimeHarness,
    fixture_envelope,
    make_maintenance_page,
    make_reliability_page,
)

REL_URL = WWW + "/appliances/refrigerators/french-door-refrigerator/reliability/c37162/"


def test_brand_without_owner_satisfaction_is_null_not_dropped(reliability_fixture):
    parsed = parse_reliability(reliability_fixture["init_store"], 37162)
    by_name = {b["brand_name"]: b for b in parsed["brands"]}
    assert set(by_name) == {"Brand A", "Brand B", "Brand C"}
    assert by_name["Brand B"]["predicted_reliability"] == 3
    assert by_name["Brand B"]["owner_satisfaction"] is None
    # the RELIABILITY survey's sortOrder is the ranking, even though owner satisfaction ranks
    # C, A — merging the two orders would put Brand C first
    assert [b["brand_name"] for b in parsed["brands"]] == ["Brand A", "Brand B", "Brand C"]
    assert parsed["has_reliability_data"] and parsed["has_owner_satisfaction_data"]
    assert parsed["siblings"] == [37162, 28722, 29738]  # includes self, as CR ships
    assert parsed["warnings"] == []
    assert "predicted_reliability_100" not in parsed["brands"][0]


def test_survey_flag_mismatch_is_warned_not_silently_trusted(reliability_fixture):
    """CR's own `HasReliabilityData` flag disagreeing with the rows it ships is drift worth
    reporting: the rows win, and the caller is told the flag lied."""
    import copy

    store = copy.deepcopy(reliability_fixture["init_store"])
    store["data"]["category"]["HasReliabilityData"] = False
    parsed = parse_reliability(store, 37162)
    assert "survey_flag_mismatch:reliability" in parsed["warnings"]
    assert parsed["brands"], "the rows CR shipped are still served"

    store["data"]["category"]["HasOwnerSatisfactionData"] = False
    both = parse_reliability(store, 37162)
    assert both["warnings"] == [
        "survey_flag_mismatch:reliability",
        "survey_flag_mismatch:owner_satisfaction",
    ]


def test_fullscale_only_under_full(reliability_fixture):
    full = parse_reliability(reliability_fixture["init_store"], 37162, full=True)
    assert full["brands"][0]["predicted_reliability_100"] == 4 * 20 - 5
    std = parse_reliability(reliability_fixture["init_store"], 37162)
    assert "owner_satisfaction_100" not in std["brands"][0]


def test_no_survey_is_structural_false_with_empty_brands(reliability_fixture):
    parsed = parse_reliability(reliability_fixture["init_store"], 29738)
    assert parsed["has_reliability_data"] is False and parsed["brands"] == []
    assert parsed["found"] is True


def test_unknown_category_in_payload(reliability_fixture):
    parsed = parse_reliability(reliability_fixture["init_store"], 4242)
    assert parsed["found"] is False and parsed["brands"] == []


def _route(h: RuntimeHarness, fixture: dict) -> None:
    h.sess.route(REL_URL, FakeResponse(url=REL_URL, content=make_reliability_page(fixture)))


async def test_auth_state_anonymous_even_with_member_session(tmp_path, reliability_fixture):
    h = RuntimeHarness(tmp_path, cookie=True)
    h.rt.health.on_marker(True, credential_present=True)
    _route(h, reliability_fixture)
    out = await cr_reliability(h.rt, "c37162")
    assert out.error is None and out.auth_state == "anonymous"
    dumped = out.model_dump()
    assert "session" not in dumped and "sort" not in dumped
    assert h.rt.health.health is SessionHealth.ACTIVE
    assert out.data.category.id == 37162 and out.data.category.slug == "french-door-refrigerator"
    assert [b.brand_name for b in out.data.brands] == ["Brand A", "Brand B", "Brand C"]
    assert out.data.brands[1].owner_satisfaction is None
    assert out.data.methodology is None
    assert out.provenance.cr_url == REL_URL and out.provenance.from_cache is False


async def test_a_category_cr_runs_no_survey_on_is_a_success_not_an_error(tmp_path, c37162):
    """SPEC §7: "A category CR runs no survey on is not a failure" — the answer is the
    structural no-data envelope. Filed against c33041 (upright freezers), which returned
    `fetch_failed`/`url_unresolved` telling the caller to run `cr_ratings` so the real URL
    would be cached; they had, and the cached payload is what says there is no URL."""
    import copy

    fx = copy.deepcopy(c37162)
    args = fx["filter_instance"]["args"]
    args.pop("reliabilityURL", None)
    for c in args["cats"]:
        c["reliabilityURL"] = False

    h = RuntimeHarness(tmp_path)
    h.rt.cache.write_category(
        fixture_envelope(fx), tier="anonymous", scored=False, fetched_at=h.now
    )
    out = await cr_reliability(h.rt, "c37162")

    assert out.error is None  # never `fetch_failed`, never `reliability_payload_missing`
    assert out.data.brands == [] and out.data.has_reliability_data is False
    assert out.data.has_owner_satisfaction_data is False and out.data.methodology is None
    assert out.data.category.id == 37162 and out.data.category.slug == "french-door-refrigerator"
    # "absent" (CR published none), never "unavailable" (a membership would show it)
    assert out.scores_available.predicted_reliability == "absent"
    assert out.scores_available.owner_satisfaction == "absent"
    assert out.provenance.cr_url is None  # there is no reliability page to name
    assert out.provenance.from_cache is True
    assert h.sess.requests == []  # no URL guessed, no request spent


async def test_scores_available_never_unavailable(tmp_path, reliability_fixture):
    h = RuntimeHarness(tmp_path)
    _route(h, reliability_fixture)
    out = await cr_reliability(h.rt, 37162)
    assert out.scores_available.predicted_reliability == "available"
    assert out.scores_available.owner_satisfaction == "available"
    none = await cr_reliability(h.rt, 29738) if h.rt.cache.index_row(29738) else None
    if none is None:  # 29738 is not in the seeded index; resolve it through the family fan-out
        h.rt.cache.upsert_category_index(
            [{"id": 29738, "path": "/appliances/refrigerators/compact-refrigerators/c29738/"}],
            "sitemap",
            h.now,
        )
        none = await cr_reliability(h.rt, 29738)
    assert none.error is None
    assert none.data.has_reliability_data is False and none.data.brands == []
    assert none.scores_available.predicted_reliability == "absent"
    assert none.provenance.from_cache is True  # the fan-out row, no second fetch
    assert h.requests == [REL_URL]
    from consumer_reports_mcp import envelope as E

    schema = E.ReliabilityEnvelope.model_json_schema()
    enum = schema["$defs"]["SurveyScoresAvailable"]["properties"]["predicted_reliability"]["enum"]
    assert enum == ["available", "absent"]  # "unavailable" is unrepresentable here


async def test_os_only_brand_order_when_no_reliability_survey(reliability_fixture):
    import copy

    store = copy.deepcopy(reliability_fixture["init_store"])
    cat = store["data"]["category"]
    cat["surveys"]["reliability"]["productGroupSurveyValue"] = []
    cat["HasReliabilityData"] = False
    parsed = parse_reliability(store, 37162)
    assert [b["brand_name"] for b in parsed["brands"]] == ["Brand C", "Brand A"]
    assert parsed["has_reliability_data"] is False and parsed["has_owner_satisfaction_data"]


async def test_payload_that_does_not_describe_the_category_is_drift(tmp_path, reliability_fixture):
    h = RuntimeHarness(tmp_path)
    h.rt.cache.upsert_category_index(
        [{"id": 4242, "path": "/appliances/other/c4242/"}], "sitemap", h.now
    )
    other = WWW + "/appliances/other/reliability/c4242/"
    h.sess.route(other, FakeResponse(url=other, content=make_reliability_page(reliability_fixture)))
    out = await cr_reliability(h.rt, 4242)
    assert out.error is not None and out.error.code == "reliability_payload_missing"
    assert h.rt.cache.select_reliability(4242, 30, h.now) is None  # never fanned out under it
    assert out.provenance is None  # the repository refused it before any row: nothing served


async def test_drift_found_in_a_served_row_keeps_its_provenance(tmp_path, reliability_fixture):
    """The tool's own guard — a CACHED row whose payload does not describe the category — has a
    row in hand, so its provenance rides on the error as on the products tools (SPEC §7):
    `cr_url` is the URL that landed elsewhere, which is the diagnostic. `auth_state` stays the
    pinned literal; this surface derives nothing from a row."""
    h = RuntimeHarness(tmp_path)
    h.rt.cache.upsert_category_index(
        [{"id": 4242, "path": "/appliances/other/c4242/"}], "sitemap", h.now
    )
    other = WWW + "/appliances/other/reliability/c4242/"
    h.rt.cache.write_reliability([4242], reliability_fixture["init_store"], h.now, other)
    out = await cr_reliability(h.rt, 4242)
    assert out.error is not None and out.error.code == "reliability_payload_missing"
    assert out.provenance is not None and out.provenance.cr_url == other
    assert out.provenance.from_cache is True and out.auth_state == "anonymous"
    assert out.data is None and out.scores_available is None and h.requests == []


async def test_drift_code_is_reliability_payload_missing(tmp_path):
    h = RuntimeHarness(tmp_path)
    h.sess.route(REL_URL, FakeResponse(url=REL_URL, content=make_maintenance_page("Maint")))
    out = await cr_reliability(h.rt, 37162)
    assert out.error is not None and out.error.code == "reliability_payload_missing"
    assert out.error.title == "Maint" and out.data is None
    assert out.auth_state == "anonymous"


async def test_methodology_and_full_detail(tmp_path, reliability_fixture):
    h = RuntimeHarness(tmp_path)
    _route(h, reliability_fixture)
    out = await cr_reliability(h.rt, 37162, detail="full", include_methodology=True)
    assert out.data.methodology.reliability == "Synthetic methodology blurb."
    assert out.data.methodology.footnote == "Synthetic footnote."
    assert out.data.brands[0].predicted_reliability_100 is not None
    std = await cr_reliability(h.rt, 37162)
    assert "predicted_reliability_100" not in std.model_dump()["data"]["brands"][0]
    bad = await cr_reliability(h.rt, 37162, detail="everything")
    assert bad.error.code == "invalid_filter_value"


async def test_reliability_detail_error_carries_the_legal_values(tmp_path):
    h = RuntimeHarness(tmp_path)
    out = await cr_reliability(h.rt, 37162, detail="summary")
    assert out.error.code == "invalid_filter_value" and out.error.filter == "detail"
    assert out.error.candidates == [{"value": "standard"}, {"value": "full"}]
    assert out.provenance is None and h.requests == []  # no row: no provenance, no request
