"""P2.3 — the definition join and declared-type coercion (SPEC §7, RECON §1, §9h)."""

from __future__ import annotations

import pytest

from consumer_reports_mcp.attributes import (
    NOT_APPLICABLE,
    RATING_KIND,
    Definition,
    build_definitions,
    coerce,
    has_no_value,
    is_not_applicable,
    normalize_entry,
    rating_definitions,
)
from consumer_reports_mcp.ingest import build_envelope

DEF_TYPE_NAMES = {"SPEC", "TEST_RESULT", "PRICING"}


@pytest.fixture
def env(c37162):
    from tests.conftest import ratings_wrapper_of

    return build_envelope(
        c37162["filter_instance"],
        ratings_wrapper_of(c37162),
        data_subscriber="false",
        final_url=c37162["final_url"],
        requested_id=None,
        http_status=200,
    )


@pytest.fixture
def defs(env):
    return build_definitions(env)


def _entries(env):
    for p in env["filter_instance"]["data"].values():
        yield from p["attrs"]


def test_kind_read_from_attributeDataTypeName_not_attributeTypeName(env, defs):
    assert defs, "no definitions joined"
    for d in defs.values():
        assert d.kind not in DEF_TYPE_NAMES, d
    for e in _entries(env):
        n = normalize_entry(e, defs)
        assert n["kind"] not in DEF_TYPE_NAMES, n


def test_categoryAttributes_covers_every_product_entry(env, defs):
    entry_ids = {e["attributeId"] for e in _entries(env)}
    covered = {
        aid for aid in entry_ids if aid in defs and defs[aid].source == "category_attributes"
    }
    # everything except the two deliberately undefined synthetic ids
    assert entry_ids - covered == {999001, 999002}


def test_text_dial_position_not_coerced(env, defs):
    e = next(e for e in _entries(env) if e["value"] == "-1")
    n = normalize_entry(e, defs)
    assert n["kind"] == "text" and n["value"] == "-1" and n["raw_value"] == "-1"


def test_boolean_yes_no(env, defs):
    yes = next(
        e for e in _entries(env) if e["attributeTypeName"] == "boolean" and e["value"] == "Yes"
    )
    no = next(
        e for e in _entries(env) if e["attributeTypeName"] == "boolean" and e["value"] == "No"
    )
    assert normalize_entry(yes, defs)["value"] is True
    assert normalize_entry(no, defs)["value"] is False
    assert coerce("boolean", " yes ") == (True, True)
    assert coerce("boolean", "maybe") == (None, False)


def test_text_yes_no_untouched(env, defs):
    e = next(
        e for e in _entries(env) if e["attributeTypeName"] == "text" and e["value"] in ("Yes", "No")
    )
    n = normalize_entry(e, defs)
    assert n["kind"] == "text" and n["value"] == e["value"] and isinstance(n["value"], str)


def test_unit_from_definition_never_inferred(env, defs):
    # a real "Exterior width" definition carries in.; the undefined 999002 named the same must not
    real = next(d for d in defs.values() if d.name == "Exterior width")
    assert real.unit == "in."
    undefined = next(e for e in _entries(env) if e["attributeId"] == 999002)
    n = normalize_entry(undefined, defs)
    assert n["name"] == "Exterior width" and n["unit"] is None and n["description"] is None
    assert n["kind"] == "numeric-general" and n["value"] == 36


def test_unjoined_entry_kept_with_entry_type(env, defs):
    e = next(e for e in _entries(env) if e["attributeId"] == 999001)
    n = normalize_entry(e, defs)
    assert n == {
        "id": 999001,
        "name": "Mystery flag",
        "kind": "boolean",
        "status": None,
        "value": True,
        "raw_value": "Yes",
        "unit": None,
        "description": None,
        "group": None,
    }


def test_coercion_failure_keeps_raw_and_warns(env, defs):
    e = next(e for e in _entries(env) if e["value"] == "36 - 38")
    warnings: list[str] = []
    n = normalize_entry(e, defs, warnings)
    assert n["value"] is None and n["raw_value"] == "36 - 38"
    assert warnings == [f"coercion_failed:{e['attributeId']}"]


def test_empty_string_numeric_is_a_blank_cell_not_a_failure(c200228):
    env = build_envelope(
        c200228["filter_instance"],
        None,
        data_subscriber="false",
        final_url=c200228["final_url"],
        requested_id=None,
        http_status=200,
    )
    defs = build_definitions(env)
    e = next(
        e for p in env["filter_instance"]["data"].values() for e in p["attrs"] if e["value"] == ""
    )
    warnings: list[str] = []
    n = normalize_entry(e, defs, warnings)
    assert n["value"] is None and n["raw_value"] == "" and warnings == []


def test_definition_kind_wins_over_entry_kind():
    # SPEC §7 join order: categoryAttributes → attrs → the entry's own attributeTypeName
    defs = {
        7: Definition(
            id=7,
            name="Dial",
            display_name="Dial",
            kind="text",
            unit=None,
            description=None,
            group=None,
            sort_order=None,
            source="category_attributes",
        )
    }
    n = normalize_entry(
        {"attributeId": 7, "attributeTypeName": "numeric-general", "value": "-1"}, defs
    )
    assert n["kind"] == "text" and n["value"] == "-1"


def test_coercion_warning_is_deduped_across_products():
    defs = {
        9: Definition(
            id=9,
            name="W",
            display_name="W",
            kind="numeric-general",
            unit=None,
            description=None,
            group=None,
            sort_order=None,
            source="category_attributes",
        )
    }
    warnings: list[str] = []
    for _ in range(5):
        normalize_entry(
            {"attributeId": 9, "attributeTypeName": "numeric-general", "value": "a-b"},
            defs,
            warnings,
        )
    assert warnings == ["coercion_failed:9"]


def test_null_value_is_not_a_failure(env, defs):
    e = next(e for e in _entries(env) if e["attributeTypeName"] == "numeric-rating-score")
    warnings: list[str] = []
    n = normalize_entry(e, defs, warnings)
    assert n["value"] is None and n["raw_value"] is None and warnings == []


def test_unknown_type_passes_through():
    defs = {
        1: Definition(
            id=1,
            name="x",
            display_name="x",
            kind="numeric-blob",
            unit=None,
            description=None,
            group=None,
            sort_order=None,
            source="category_attributes",
        )
    }
    n = normalize_entry(
        {"attributeId": 1, "attributeTypeName": "numeric-blob", "name": "x", "value": "1-2-3"}, defs
    )
    assert n["kind"] == "numeric-blob" and n["value"] == "1-2-3"
    assert coerce("custom", "39") == ("39", True)
    assert coerce("text", "39") == ("39", True)


def test_numeric_coercion_edge_cases():
    assert coerce("numeric-general", "1,024") == (1024, True)
    assert coerce("numeric-general", "21.5") == (21.5, True)
    assert coerce("numeric-general", 7) == (7, True)
    assert coerce("numeric-general", True) == (None, False)
    assert coerce("numeric-price", "N/A") == (None, False)
    assert coerce("numeric-overall-score", "79") == (79, True)


def test_attrs_fallback_fills_gaps_and_supplies_missing_definitions(c37162):
    env = build_envelope(
        c37162["filter_instance"],
        None,
        data_subscriber="false",
        final_url=c37162["final_url"],
        requested_id=None,
        http_status=200,
    )
    defs = build_definitions(env)
    assert all(d.source == "attrs" for d in defs.values())
    assert defs[11197].kind == "numeric-rating-score"
    assert defs[6918].unit == "in."


def test_rating_definitions_order_is_sortorder_then_id(defs):
    order = rating_definitions(defs)
    keys = [(d.sort_order is None, d.sort_order or 0, d.id) for d in order]
    assert keys == sorted(keys)
    assert all(d.kind == "numeric-rating-score" for d in order)


# --------------------------------------------------------------------------- batch-3 findings


@pytest.mark.parametrize(
    "raw", ["nan", "NaN", "inf", "-inf", "Infinity", "-Infinity", float("nan"), float("inf")]
)
def test_non_finite_numerics_are_coercion_failures_not_values(raw):
    """`float("nan")` parses, and a NaN `value` serialises as literal `NaN` — not JSON (RFC
    8259), and enough to break a strict client. Kept as `raw_value`, `value: null`, warned."""
    import json

    value, ok = coerce("numeric-general", raw)
    assert value is None and ok is False
    entry = {"attributeId": 1, "attributeTypeName": "numeric-rating-score", "value": raw}
    warnings: list[str] = []
    n = normalize_entry(entry, {}, warnings)
    assert n["value"] is None and n["raw_value"] is raw and warnings == ["coercion_failed:1"]
    assert "nan" not in json.dumps({"value": n["value"]}).lower()


# ------------------------------------------------------- CR's `0` on a rating column (RECON §9i)


def test_zero_is_not_applicable_on_a_rating_column_only():
    """`0` is CR's "this test does not apply to this model" on a rating column, and a real
    measurement on every other numeric kind — a monitor with no USB-A ports, a mattress with
    no handles. Coerced by the DECLARED kind, so a string `"0"` reads the same as an int."""
    for raw in (0, 0.0, "0", " 0 "):
        assert is_not_applicable(RATING_KIND, raw)
    for kind in ("numeric-general", "numeric-price", "numeric-overall-score", "text", "boolean"):
        assert not is_not_applicable(kind, 0)
    for raw in (1, 5, 0.5, None, "", "n/a", False, True):
        assert not is_not_applicable(RATING_KIND, raw)


def test_not_applicable_nulls_the_value_and_keeps_the_raw_zero():
    entry = {
        "attributeId": 11196,
        "attributeTypeName": RATING_KIND,
        "name": "Hot Garage Ready",
        "value": 0,
    }
    warnings: list[str] = []
    n = normalize_entry(entry, {}, warnings)
    assert n["value"] is None and n["raw_value"] == 0
    assert n["status"] == NOT_APPLICABLE
    assert warnings == []  # CR's marker is data, not a coercion failure


def test_a_real_rating_and_a_zero_spec_carry_no_status():
    rated = normalize_entry(
        {"attributeId": 11196, "attributeTypeName": RATING_KIND, "value": 3}, {}
    )
    assert rated["value"] == 3 and rated["status"] is None
    ports = normalize_entry(
        {"attributeId": 11210, "attributeTypeName": "numeric-general", "value": 0}, {}
    )
    assert ports["value"] == 0 and ports["status"] is None


def test_declared_kind_decides_not_the_entry_type(c37162):
    """The definition's `attributeDataTypeName` is authoritative (RECON §9h): an entry typed
    `numeric-rating-score` whose definition declares a spec keeps its `0`."""
    d = Definition(
        id=77,
        name="Number of shelves",
        display_name=None,
        kind="numeric-general",
        unit=None,
        description=None,
        group=None,
        sort_order=None,
        source="category_attributes",
    )
    n = normalize_entry({"attributeId": 77, "attributeTypeName": RATING_KIND, "value": 0}, {77: d})
    assert n["kind"] == "numeric-general" and n["value"] == 0 and n["status"] is None


def test_has_no_value_covers_blank_and_the_not_applicable_zero():
    assert has_no_value(RATING_KIND, 0) and has_no_value(RATING_KIND, None)
    assert has_no_value(RATING_KIND, "  ") and has_no_value("numeric-price", None)
    assert not has_no_value(RATING_KIND, 1) and not has_no_value("numeric-price", 0)
