"""P0.4 — the page factories decide tier by the marker only, never by the scores."""

from __future__ import annotations

import json
import re

from tests.conftest import (
    FakeWaferSession,
    load_fixture,
    make_car_page,
    make_category_page,
    make_reliability_page,
)


def _scores(page: bytes) -> list[int | None]:
    html = page.decode()
    start = html.index("window.filterInstanceDATA = ") + len("window.filterInstanceDATA = ")
    end = html.index(";\nconsole.log", start)
    fi = json.loads(html[start:end])
    return [p["overallDisplayScore"] for p in fi["data"].values()]


def test_factory_tier_is_marker_only():
    fx = load_fixture("category_c37162.json")
    anon_with_scores = make_category_page(fx, subscriber="false", fill_scores=True)
    assert b'data-subscriber="false"' in anon_with_scores
    assert b'data-subscriber="true"' not in anon_with_scores
    assert all(s is not None for s in _scores(anon_with_scores))

    member_without_scores = make_category_page(fx, subscriber="true", fill_scores=False)
    assert b'data-subscriber="true"' in member_without_scores
    assert b'data-subscriber="false"' not in member_without_scores
    assert all(s is None for s in _scores(member_without_scores))


def test_sign_out_markup_in_every_variant():
    fx = load_fixture("category_c37162.json")
    for kwargs in ({"subscriber": "false"}, {"subscriber": "true"}, {"subscriber": None}):
        page = make_category_page(fx, **kwargs)
        assert page.count(b"Sign Out") == 2


def test_omit_variants():
    fx = load_fixture("category_c37162.json")
    assert b"filterInstanceDATA" not in make_category_page(fx, omit_payload=True)
    assert b"ratings-wrapper" not in make_category_page(fx, omit_ratings_wrapper=True)
    assert b"ratings-wrapper" in make_category_page(fx)


def test_reliability_page_has_no_marker():
    page = make_reliability_page(load_fixture("reliability_c37162.json"))
    assert b"data-subscriber" not in page
    assert b"window.initStore" in page


def test_car_page_marker_and_key():
    page = make_car_page(True).decode()
    assert re.search(r"window\.isSubscriber\s*=\s*true", page)
    assert re.search(r'"DRS_CARS_API_API_KEY":"[^"]{40}"', page)
    assert "data-subscriber" not in page
    assert b"isSubscriber" not in make_car_page(None)


async def test_fake_session_scripting_and_jar():
    s = FakeWaferSession(rate_limit=2.0)
    from tests.conftest import FakeResponse

    s.push(FakeResponse(url="https://a/", content=b"x", set_cookies={"userLicenses": "new"}))
    s.add_cookie(
        "hash=abc; Domain=.consumerreports.org; Path=/; Secure", "https://www.consumerreports.org/"
    )
    assert s.get_cookie("hash", "https://secure.consumerreports.org/") == "abc"
    assert s.get_cookie("hash", "http://www.consumerreports.org/") is None  # Secure
    r = await s.get("https://a/")
    assert r.text == "x" and s.requests[0][0] == "https://a/"
    assert s.get_cookie("userLicenses", "https://www.consumerreports.org/") == "new"
    assert s.kwargs == {"rate_limit": 2.0}
    assert all("value" not in e for e in s.cookie_scope_summary("https://www.consumerreports.org/"))


def test_ratings_wrapper_double_is_faithful():
    """RECON §10f: the anchor the parser depends on — assert the double's shape, not presence."""
    from tests.conftest import ratings_wrapper_of

    fx = load_fixture("category_c37162.json")
    rw = ratings_wrapper_of(fx)
    assert rw["cat"]["_id"] == fx["filter_instance"]["args"]["cid"]
    assert all("attributeDataTypeName" in d for d in rw["cat"]["categoryAttributes"])
    sibs = rw["productFilterPayload"]["debug"]["categories"]
    assert all(
        "_id" in s and "modelCounts" in s and "ratedModelsCount" in s["modelCounts"] for s in sibs
    )
    assert all(
        "modelMinOverallDisplayScore" in s and "modelMaxOverallDisplayScore" in s for s in sibs
    )
    assert len(rw["subcats"]) == 4  # while only 3 groups carry products (RECON §10g)
    assert len({p["_groupId"] for p in fx["filter_instance"]["data"].values()}) == 3
    assert rw["scat"]["_id"] == fx["filter_instance"]["args"]["scid"]


def test_fake_jar_scoping_matches_rfc6265():
    s = FakeWaferSession()
    s.add_cookie(
        "hash=abc; Domain=.consumerreports.org; Path=/; Secure", "https://www.consumerreports.org/"
    )
    assert s.get_cookie("hash", "https://secure.consumerreports.org/ec/login") == "abc"
    assert s.get_cookie("hash", "https://www.consumerreports.org/x/") == "abc"
    assert s.get_cookie("hash", "https://example.com/") is None
    s.add_cookie("hostonly=1; Path=/; Secure", "https://www.consumerreports.org/")
    assert s.get_cookie("hostonly", "https://www.consumerreports.org/") == "1"
    assert s.get_cookie("hostonly", "https://secure.consumerreports.org/") is None  # not offered
    s.add_cookie(
        "scoped=1; Domain=.consumerreports.org; Path=/ec; Secure",
        "https://www.consumerreports.org/",
    )
    assert s.get_cookie("scoped", "https://www.consumerreports.org/ec/login") == "1"
    assert s.get_cookie("scoped", "https://www.consumerreports.org/appliances/") is None
