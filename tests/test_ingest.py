"""P2.2 — classification in SPEC §7 order, and the SPEC §8 ingest envelope."""

from __future__ import annotations

import json

import pytest

from consumer_reports_mcp import ingest
from consumer_reports_mcp.ingest import build_envelope, classify, is_scored, product_index_rows
from tests.conftest import (
    fill_scores_in,
    make_category_page,
    make_login_page,
    make_maintenance_page,
)

CAT_URL = (
    "https://www.consumerreports.org/appliances/refrigerators/french-door-refrigerator/c37162/"
)
LOGIN_URL = "https://secure.consumerreports.org/ec/login?error"


@pytest.mark.parametrize(
    ("cookie", "subscriber", "fill", "expected", "tier"),
    [
        (False, "false", False, ingest.ANONYMOUS, "anonymous"),
        (False, "false", True, ingest.ANONYMOUS, "anonymous"),  # scores are not evidence
        (True, "true", False, ingest.MEMBER, "member"),  # detection ignores scores
        (True, "true", True, ingest.MEMBER, "member"),
        (True, "false", False, ingest.SESSION_EXPIRED, "anonymous"),
        (True, "false", True, ingest.SESSION_EXPIRED, "anonymous"),
        (False, "true", False, ingest.MEMBER, "member"),  # marker wins over configuration
        (True, None, False, ingest.MARKER_MISSING, None),
        (False, None, True, ingest.MARKER_MISSING, None),
    ],
)
def test_auth_matrix(c37162, cookie, subscriber, fill, expected, tier):
    page = make_category_page(c37162, subscriber=subscriber, fill_scores=fill)
    c = classify(CAT_URL, 200, page, credential_present=cookie)
    assert c.kind == expected
    assert c.data_tier == tier
    assert c.http_status == 200
    if expected in ingest.DATA_KINDS:
        assert c.filter_instance is not None and c.ratings_wrapper is not None
        assert c.warnings == []


def test_login_url_beats_body(c37162):
    # a full anonymous body under a login final URL is still a rejection — URL first
    page = make_category_page(c37162, subscriber="false")
    c = classify(LOGIN_URL, 200, page, credential_present=True)
    assert c.kind == ingest.CREDENTIAL_REJECTED
    assert c.filter_instance is None
    c2 = classify(LOGIN_URL, 200, make_login_page(), credential_present=True)
    assert c2.kind == ingest.CREDENTIAL_REJECTED
    assert c2.title == "Sign In - Consumer Reports"


def test_login_in_history_only_is_not_rejection(c37162):
    # `classify` sees only the FINAL url — a healthy re-mint's redirect chain is invisible here
    # by design; the history-vs-final distinction is pinned in tests/test_transport.py
    page = make_category_page(c37162, subscriber="true", fill_scores=True)
    c = classify(CAT_URL, 200, page, credential_present=True)
    assert c.kind == ingest.MEMBER


def test_payload_missing_wins_over_marker_missing():
    page = make_maintenance_page("Maintenance")
    c = classify(CAT_URL, 200, page, credential_present=True)
    assert c.kind == ingest.PAYLOAD_MISSING
    assert c.title == "Maintenance" and c.http_status == 200
    c500 = classify(CAT_URL, 500, b"<title>Error</title>", credential_present=False)
    assert c500.kind == ingest.PAYLOAD_MISSING and c500.http_status == 500


def test_payload_missing_on_thin_payload(c37162):
    fi = {k: v for k, v in c37162["filter_instance"].items() if k != "attrs"}
    page = (
        b'<html><body><div data-subscriber="false"></div><script>window.filterInstanceDATA = '
        + json.dumps(fi).encode()
        + b";\n</script></body></html>"
    )
    assert classify(CAT_URL, 200, page, False).kind == ingest.PAYLOAD_MISSING


def test_empty_category_and_dictionary_missing_warnings(c37162):
    fi = dict(c37162["filter_instance"], data={})
    fx = dict(c37162, filter_instance=fi)
    c = classify(CAT_URL, 200, make_category_page(fx, omit_ratings_wrapper=True), False)
    assert c.kind == ingest.ANONYMOUS
    assert set(c.warnings) == {"attribute_dictionary_missing", "empty_category"}


def test_empty_dictionary_warns_like_a_missing_one(c37162):
    """`categoryAttributes: []` is the same fall-through to `attrs` as no block at all, so it
    must carry the same warning — `[]` used to pass `_wrapper_has_dictionary` silently."""
    fx = dict(c37162, category_attributes=[])
    c = classify(CAT_URL, 200, make_category_page(fx), False)
    assert c.kind == ingest.ANONYMOUS and c.ratings_wrapper is not None
    assert c.warnings == ["attribute_dictionary_missing"]
    present = classify(CAT_URL, 200, make_category_page(c37162), False)
    assert present.warnings == []


def test_envelope_keys_exact(c37162):
    c = classify(CAT_URL, 200, make_category_page(c37162), False)
    env = build_envelope(
        c.filter_instance,
        c.ratings_wrapper,
        data_subscriber=c.marker,
        final_url=CAT_URL,
        requested_id=37162,
        http_status=200,
    )
    assert list(env.keys()) == [
        "schema_version",
        "filter_instance",
        "category_attributes",
        "family",
        "supercategory",
        "data_subscriber",
        "final_url",
        "requested_id",
        "http_status",
    ]
    assert env["schema_version"] == 1
    assert env["data_subscriber"] == "false"
    assert env["requested_id"] is None  # equals args.cid → null
    assert env["supercategory"] == {"id": 28978, "name": "Refrigerators"}
    assert len(env["category_attributes"]) == 48
    assert ingest.category_id_of(env) == 37162
    assert ingest.display_name_of(env) == "French-Door Refrigerators"


def test_requested_id_alias_when_cid_differs(c200228):
    c = classify(c200228["final_url"], 200, make_category_page(c200228), False)
    env = build_envelope(
        c.filter_instance,
        c.ratings_wrapper,
        data_subscriber=c.marker,
        final_url=c200228["final_url"],
        requested_id=200358,
        http_status=200,
    )
    assert ingest.category_id_of(env) == 200228
    assert env["requested_id"] == 200358


def test_scored_false_on_anon_true_after_fill(c37162):
    assert is_scored(c37162["filter_instance"]) is False
    assert is_scored(fill_scores_in(c37162["filter_instance"])) is True


def test_family_groups_null_for_siblings_list_for_self(c37162):
    c = classify(CAT_URL, 200, make_category_page(c37162), False)
    env = build_envelope(
        c.filter_instance,
        c.ratings_wrapper,
        data_subscriber="false",
        final_url=CAT_URL,
        requested_id=None,
        http_status=200,
    )
    fam = {f["id"]: f for f in env["family"]}
    assert len(fam) == 8
    own = fam[37162]
    assert own["name"] == "French-Door Refrigerators"
    assert own["slug"] == "french-door-refrigerator"  # targetPath, the URL slug
    assert (own["score_min"], own["score_max"], own["rated_count"]) == (43, 79, 172)
    assert own["has_reliability_data"] is True
    assert [g["id"] for g in own["groups"]] == [200367, 200369, 200371]
    assert own["score_range_block_present"] is True
    for sid, f in fam.items():
        if sid != 37162:
            assert f["groups"] is None, sid
    assert fam[29738]["score_min"] is None and fam[29738]["score_range_block_present"] is True
    assert fam[29738]["has_reliability_data"] is False
    assert own["has_reliability_data"] is True
    assert fam[28722]["rated_count"] == 58


def test_own_entry_from_cat_block_derives_has_reliability(c37162):
    # the real `.cat` has no HasReliabilityData key; its own `reliability.brands` says it
    from tests.conftest import ratings_wrapper_of

    rw = ratings_wrapper_of(c37162)
    rw["productFilterPayload"]["debug"]["categories"] = [
        s for s in rw["productFilterPayload"]["debug"]["categories"] if s["_id"] != 37162
    ]
    rw["cat"] = dict(rw["cat"], reliability={"brands": [{"brandId": 1}], "blurb": ""})
    rw["cat"].pop("HasReliabilityData", None)
    env = build_envelope(
        c37162["filter_instance"],
        rw,
        data_subscriber="false",
        final_url=CAT_URL,
        requested_id=None,
        http_status=200,
    )
    own = next(f for f in env["family"] if f["id"] == 37162)
    assert own["has_reliability_data"] is True and own["groups"]


def test_family_from_args_when_wrapper_missing(c37162):
    page = make_category_page(c37162, omit_ratings_wrapper=True)
    c = classify(CAT_URL, 200, page, False)
    env = build_envelope(
        c.filter_instance,
        None,
        data_subscriber="false",
        final_url=CAT_URL,
        requested_id=None,
        http_status=200,
    )
    assert env["category_attributes"] == []
    ids = {f["id"] for f in env["family"]}
    assert 37162 in ids and 28722 in ids
    own = next(f for f in env["family"] if f["id"] == 37162)
    assert own["score_range_block_present"] is False and own["score_min"] is None
    assert own["groups"] and own["slug"] == "french-door-refrigerator"
    assert env["supercategory"] == {"id": 28978, "name": "Refrigerators"}


def test_product_index_rows(c37162):
    env = build_envelope(
        c37162["filter_instance"],
        None,
        data_subscriber="false",
        final_url=CAT_URL,
        requested_id=None,
        http_status=200,
    )
    rows = product_index_rows(env)
    assert len(rows) == 8
    assert (500001, 37162, "Brand A", "MODEL-001") in rows


def test_single_group_category_groups_is_empty_list(banks):
    c = classify(banks["final_url"], 200, make_category_page(banks), False)
    env = build_envelope(
        c.filter_instance,
        c.ratings_wrapper,
        data_subscriber="false",
        final_url=banks["final_url"],
        requested_id=None,
        http_status=200,
    )
    own = next(f for f in env["family"] if f["id"] == 37154)
    assert own["groups"] == []  # a positive claim: CR ships no display groups here


def test_login_path_is_matched_as_a_route_not_a_substring(c37162):
    page = make_category_page(c37162, subscriber="true", fill_scores=True)
    c = classify(f"{CAT_URL}ec/login-tips/", 200, page, credential_present=True)
    assert c.kind == ingest.MEMBER
    # on the login host too, only the login route is a rejection: a false rejection silently
    # downgrades a member session, a drift alarm is at least visible
    c2 = classify(
        "https://secure.consumerreports.org/ec/login-tips/",
        200,
        make_login_page(),
        credential_present=True,
    )
    assert c2.kind == ingest.PAYLOAD_MISSING
