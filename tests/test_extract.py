"""P2.1 — anchors and markers (RECON §10e–§10g, §11a, §13d, §5)."""

from __future__ import annotations

import json

import pytest

from consumer_reports_mcp.extract import (
    brace_match,
    extract_cars_page,
    extract_filter_instance,
    extract_init_store,
    extract_ratings_wrapper,
    read_subscriber_marker,
    read_title,
)
from tests.conftest import (
    SYNTH_API_KEY,
    make_car_page,
    make_category_page,
    make_maintenance_page,
    make_reliability_page,
)


def test_filter_instance_roundtrip(c37162):
    page = make_category_page(c37162)
    assert extract_filter_instance(page) == c37162["filter_instance"]


def test_filter_instance_terminator_and_wrapper_in_same_script(c37162):
    page = make_category_page(c37162).decode()
    start = page.index("window.filterInstanceDATA")
    assert ";\nconsole.log('[ ratings-wrapper ]'" in page[start:]


def test_ratings_wrapper_cat_id_matches_args_cid(c37162):
    page = make_category_page(c37162)
    rw = extract_ratings_wrapper(page)
    assert rw is not None
    assert rw["cat"]["_id"] == c37162["filter_instance"]["args"]["cid"]
    assert isinstance(rw["cat"]["categoryAttributes"], list)
    assert {s["_id"] for s in rw["productFilterPayload"]["debug"]["categories"]} >= {37162}


def test_ratings_wrapper_absent_returns_none(c37162):
    assert extract_ratings_wrapper(make_category_page(c37162, omit_ratings_wrapper=True)) is None


def test_marker_true_false_none_and_both(c37162):
    assert read_subscriber_marker(make_category_page(c37162, subscriber="true")) == "true"
    assert read_subscriber_marker(make_category_page(c37162, subscriber="false")) == "false"
    assert read_subscriber_marker(make_category_page(c37162, subscriber=None)) is None
    assert (
        read_subscriber_marker(make_category_page(c37162, subscriber="true", both_markers=True))
        is None
    )
    assert (
        read_subscriber_marker(make_category_page(c37162, subscriber=None, both_markers=True))
        is None
    )


def test_sign_out_string_present_but_ignored(c37162):
    page = make_category_page(c37162, subscriber="false")
    assert page.count(b"Sign Out") == 2
    assert read_subscriber_marker(page) == "false"


def test_marker_ignores_scores(c37162):
    assert (
        read_subscriber_marker(make_category_page(c37162, subscriber="false", fill_scores=True))
        == "false"
    )
    assert (
        read_subscriber_marker(make_category_page(c37162, subscriber="true", fill_scores=False))
        == "true"
    )


def test_payload_missing_returns_none_on_maintenance_page():
    page = make_maintenance_page("Site Maintenance &amp; Upgrades")
    assert extract_filter_instance(page) is None
    assert extract_ratings_wrapper(page) is None
    assert read_subscriber_marker(page) is None
    assert read_title(page) == "Site Maintenance & Upgrades"


def test_title_is_unescaped_collapsed_and_capped():
    page = b"<html><head><title>\n  Refrigerator   Ratings &amp; Reviews\n</title></head></html>"
    assert read_title(page) == "Refrigerator Ratings & Reviews"
    long = ("<title>" + "x" * 500 + "</title>").encode()
    assert len(read_title(long)) == 200
    assert read_title(b"<html></html>") is None


def test_init_store_roundtrip(reliability_fixture):
    page = make_reliability_page(reliability_fixture)
    assert extract_init_store(page) == reliability_fixture["init_store"]
    assert extract_filter_instance(page) is None
    assert read_subscriber_marker(page) is None


def test_cars_page_key_and_marker():
    info = extract_cars_page(make_car_page(False))
    assert info.api_key == SYNTH_API_KEY and info.is_subscriber is False
    assert extract_cars_page(make_car_page(True)).is_subscriber is True
    absent = extract_cars_page(make_car_page(None, api_key=None))
    assert absent.api_key is None and absent.is_subscriber is None
    # the escaped-quote form seen inside inline JSON strings
    escaped = (
        '<script>var s = "{\\"DRS_CARS_API_API_KEY\\":\\"' + SYNTH_API_KEY + '\\"}";</script>'
    ).encode()
    assert extract_cars_page(escaped).api_key == SYNTH_API_KEY


def test_cars_page_carries_no_products_marker():
    assert read_subscriber_marker(make_car_page(True)) is None


def test_brace_match_handles_escaped_quotes_and_braces_in_strings():
    obj = {"a": "brace } in string", "b": 'quote \\" and { brace', "c": {"d": [1, {"e": "}}}"}]}}
    s = "prefix = " + json.dumps(obj) + ";\nrest {not json}"
    start = s.index("{")
    end = brace_match(s, start)
    assert json.loads(s[start:end]) == obj
    assert s[end:].startswith(";\n")


def test_brace_match_rejects_bad_start_and_unbalanced():
    with pytest.raises(ValueError):
        brace_match("abc", 0)
    with pytest.raises(ValueError):
        brace_match('{"a": {"b": 1}', 0)


def test_filter_instance_unparseable_json_is_none():
    page = b'<script>window.filterInstanceDATA = {"filters": [}, "data": {}};\n</script>'
    assert extract_filter_instance(page) is None
