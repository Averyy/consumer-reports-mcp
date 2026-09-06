"""P0.3 — committed fixtures are content-minimal and score-free (SPEC §12, CLAUDE.md)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
REAL_BRANDS = {
    "bosch",
    "lg",
    "samsung",
    "ge",
    "whirlpool",
    "lexus",
    "acura",
    "maytag",
    "frigidaire",
    "kitchenaid",
    "thermador",
    "toyota",
    "honda",
    "ford",
    "café",
    "cafe",
    "kenmore",
    "miele",
}


def _all_fixture_files() -> list[Path]:
    return sorted(p for p in FIXTURES.rglob("*") if p.is_file())


def _products(fixture: dict) -> list[dict]:
    return list(fixture["filter_instance"]["data"].values())


CATEGORY_FIXTURES = ["category_c37162.json", "category_c37154_banks.json", "category_c200228.json"]


def test_fixtures_are_content_minimal():
    files = _all_fixture_files()
    assert files, "no fixtures built — run scripts/build_fixtures.py"
    for path in files:
        assert path.stat().st_size < 120 * 1024, f"{path.name} is {path.stat().st_size} bytes"
        text = path.read_text(encoding="utf-8")
        assert "hash=" not in text, path.name
        assert "userLicenses" not in text, path.name
        # a CDN asset id survives every rename and still identifies the real product: CR's
        # ids embed the make and model year (e.g. "/2026ACS1220...") long after "Acura" is gone
        for asset in re.findall(r'"fileName"\s*:\s*"([^"]*)"', text):
            assert asset == "/synthetic-asset", f"{path.name} carries a real asset id: {asset}"


@pytest.mark.parametrize("name", CATEGORY_FIXTURES)
def test_product_fixtures_are_score_free_and_synthetic(name):
    fx = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    products = _products(fx)
    assert products
    for p in products:
        assert p["overallDisplayScore"] is None
        assert p["brandName"].lower() not in REAL_BRANDS
        assert re.fullmatch(r"MODEL-\d{3}", p["modelName"])
        for a in p["attrs"]:
            if a["attributeTypeName"] == "numeric-rating-score":
                assert a["value"] is None, (p["id"], a)


def test_car_fixtures_use_synthetic_makes():
    for name in ("keys.json", "modelyear.json", "cars_listing.json"):
        text = (FIXTURES / "cars" / name).read_text(encoding="utf-8").lower()
        for brand in ("acura", "lexus", "honda", "toyota", "rdx"):
            assert brand not in text, (name, brand)


def test_no_real_api_key_in_fixtures():
    # the real key is 40 chars of mixed case; the synthetic one is recognisably synthetic
    for name in ("carpage_anon.html", "carpage_member.html"):
        text = (FIXTURES / "cars" / name).read_text(encoding="utf-8")
        m = re.search(r'"DRS_CARS_API_API_KEY":"([^"]+)"', text)
        assert m and len(m.group(1)) == 40 and m.group(1).startswith("SYNTHETIC")


def test_fixture_design_invariants():
    """The P0.3 design requirements later phases lean on — a rebuild must not drop them silently."""
    fx = json.loads((FIXTURES / "category_c37162.json").read_text())
    products = _products(fx)
    assert len(products) == 8
    by_group: dict[int, list[dict]] = {}
    for p in products:
        by_group.setdefault(p["_groupId"], []).append(p)
    assert set(by_group) == {200367, 200369, 200371}
    ties = 0
    for members in by_group.values():
        idx = [p["_overallSortIndex"] for p in members]
        assert idx == sorted(idx)
        ties += len(idx) - len(set(idx))
    assert ties == 1
    assert sum(1 for p in products if p["expertRatings"]["isDontBuy"]) == 1
    assert sum(1 for p in products if p["_shoppingParsed"]["isSmartBuy"]) == 1
    assert sum(1 for p in products if p["price"] is None) == 1
    assert sum(1 for p in products if p["surveys"]["ownerSatisfaction"] is not None) == 6
    entries = [a for p in products for a in p["attrs"]]
    assert any(
        a["value"] == "36 - 38" and a["attributeTypeName"] == "numeric-general" for a in entries
    )
    assert any(a["value"] == "-1" and a["attributeTypeName"] == "text" for a in entries)
    defined = {d["attributeId"] for d in fx["category_attributes"]} | {
        d["id"] for d in fx["filter_instance"]["attrs"]
    }
    undefined = {a["attributeId"] for a in entries} - defined
    assert undefined == {999001, 999002}
    assert len(fx["subcats"]) == 4 and fx["requested_id"] is None

    banks = json.loads((FIXTURES / "category_c37154_banks.json").read_text())
    bp = _products(banks)
    assert {p["_groupId"] for p in bp} == {banks["filter_instance"]["args"]["cid"]}
    assert [f for f in banks["filter_instance"]["filters"] if f["id"] == "categories"][0][
        "data"
    ] == []
    assert all(p["price"] is None for p in bp)
    assert not any(p["expertRatings"]["isRecommended"] for p in bp)
    assert all(p["surveys"]["ownerSatisfaction"] is None for p in bp)

    milk = json.loads((FIXTURES / "category_c200228.json").read_text())
    assert len(milk["filter_instance"]["args"]["subcats"]) == 4
    assert len({p["_groupId"] for p in _products(milk)}) == 3
    assert milk["requested_id"] != milk["filter_instance"]["args"]["cid"]

    rel = json.loads((FIXTURES / "reliability_c37162.json").read_text())["init_store"]["data"]
    surveys = rel["category"]["surveys"]
    rel_ids = {b["brandId"] for b in surveys["reliability"]["productGroupSurveyValue"]}
    os_ids = {b["brandId"] for b in surveys["ownerSatisfaction"]["productGroupSurveyValue"]}
    assert rel_ids - os_ids  # a brand with reliability but no owner satisfaction
    assert any(c["HasReliabilityData"] is False for c in rel["categories"])
    assert any(m["modelAvailabilityName"] != "Available" for m in rel["models"])

    keys = json.loads((FIXTURES / "cars" / "keys.json").read_text())
    years = [y for mk in keys["response"] for m in mk["models"] for y in m["modelYears"]]
    assert any(len(y["modelYearCarTypes"]) == 2 for y in years)
    assert {s["modelYearStateName"] for y in years for s in y["modelYearStates"]} == {"New", "Used"}
    types = json.loads((FIXTURES / "cars" / "cartypes.json").read_text())["content"]
    seen: dict[int, set[tuple[int, int]]] = {}
    for t in types:
        for c in t["categories"]:
            seen.setdefault(c["categoryId"], set()).add(
                (c["testedCarCount"], c["notTestedCarCount"])
            )
    assert any(len(v) > 1 for v in seen.values())  # same category, different counts per parent
    my = json.loads((FIXTURES / "cars" / "modelyear.json").read_text())["response"]
    rc = my["modelYear"]["cars"][0]["ratingsCategory"]
    assert (rc["carTypeId"], rc["categoryId"]) in {
        (t["carTypeId"], c["categoryId"]) for t in types for c in t["categories"]
    }
    assert my["modelYear"]["expertRatings"]["isRecommended"] in ("Y", "N")


def test_car_fixture_name_fields_are_synthetic():
    text = (FIXTURES / "cars" / "modelyear.json").read_text()
    for key in ("tireBrandName", "tireModelName", "makeName", "modelName", "trimName"):
        for value in re.findall(rf'"{key}":"([^"]*)"', text):
            assert re.match(r"^(Make|Model|Tire|Trim|Brand|Synthetic) ", value), (key, value)
