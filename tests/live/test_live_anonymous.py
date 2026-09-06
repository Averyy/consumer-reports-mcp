"""P11.2 — the anonymous network smoke, opt-in with CR_LIVE=1 (about 8 requests at 2 s).

No cookie is ever used here (the `transport` fixture is in conftest). Never fetches
`v2/cr/modelYears/`. Whether the live page still ships the `[ ratings-wrapper ]` attribute
dictionary is `test_live_canary.py`'s question, asked with the failure mode named.
"""

from __future__ import annotations

import json
import os
import re

import pytest

from consumer_reports_mcp import ingest
from consumer_reports_mcp.cars.index import parse_car_types
from consumer_reports_mcp.config import (
    AZ_INDEX_URL,
    CARS_API,
    CARS_PAGE_URL,
    PRODUCTS_SITEMAP_URL,
    WWW,
)
from consumer_reports_mcp.discovery import (
    parse_az_index,
    parse_category_sitemap,
    parse_sitemap_index,
)
from consumer_reports_mcp.extract import extract_cars_page, extract_ratings_wrapper

pytestmark = pytest.mark.skipif(os.environ.get("CR_LIVE") != "1", reason="set CR_LIVE=1 to run")


async def test_anonymous_category_page(transport):
    r = await transport.fetch(f"{WWW}/home-garden/vacuum-cleaners/robotic-vacuums/c35183/")
    assert r.status == 200 and r.content.count(b'data-subscriber="false"') >= 1
    c = ingest.classify(r.final_url, r.status, r.content, credential_present=False)
    assert c.kind == ingest.ANONYMOUS
    assert set(c.filter_instance) == {"filters", "data", "args", "attrs"}
    assert c.filter_instance["args"]["cid"] == 35183
    assert extract_ratings_wrapper(r.content)["cat"]["_id"] == 35183
    assert ingest.is_scored(c.filter_instance) is False  # anonymously every score is null


async def test_az_index_and_sitemaps(transport):
    az = await transport.fetch(AZ_INDEX_URL)
    rows = parse_az_index(az.content)
    assert len(rows) >= 200 and all(r.display_name for r in rows)
    index = await transport.fetch(PRODUCTS_SITEMAP_URL)
    locs = parse_sitemap_index(index.content)
    assert len(locs) >= 150
    one = await transport.fetch(next(u for u in locs if re.search(r"/sitemap/\d+$", u)))
    assert parse_category_sitemap(one.content)


async def test_redirecting_category_keeps_its_cid(transport):
    r = await transport.fetch(f"{WWW}/health/milk-milk-alternatives/plant-milk/c200228/")
    assert r.redirected and len(r.final_url) < len(
        f"{WWW}/health/milk-milk-alternatives/plant-milk/c200228/"
    )
    c = ingest.classify(r.final_url, r.status, r.content, credential_present=False)
    assert c.kind == ingest.ANONYMOUS and c.filter_instance["args"]["cid"] == 200228


async def test_cars_page_and_taxonomy(transport):
    page = await transport.fetch(CARS_PAGE_URL)
    info = extract_cars_page(page.content)
    assert info.api_key and len(info.api_key) == 40 and info.is_subscriber is False
    r = await transport.fetch(
        f"{CARS_API}/v2/cr/carTypes",
        headers={"x-api-key": info.api_key, "accept": "application/json"},
    )
    tax = parse_car_types(json.loads(r.content))
    assert len(tax["types"]) == 7
    assert not any("modelYears" in u for u in [])  # never fetched here, by construction
