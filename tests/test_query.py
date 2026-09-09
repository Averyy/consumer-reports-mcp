"""P7.1 / P7.2 — the local filter engine, ordering, nesting and paging (SPEC §5, §7)."""

from __future__ import annotations

import pytest

from consumer_reports_mcp.attributes import build_definitions
from consumer_reports_mcp.normalize import rank_table
from consumer_reports_mcp.query import (
    CROSS_GROUP,
    WITHIN_GROUP,
    FilterSpec,
    QueryError,
    apply_filters,
    group_sizes,
    nest,
    order_products,
    page,
    resolve_attribute,
    resolve_brands,
    resolve_group,
    resolve_limit,
)
from tests.conftest import fill_scores_in, fixture_envelope


@pytest.fixture
def env(c37162):
    return fixture_envelope(c37162)


@pytest.fixture
def fi(env):
    return env["filter_instance"]


@pytest.fixture
def defs(env):
    return build_definitions(env)


def _products(fi):
    return list(fi["data"].values())


def _apply(fi, defs, tier="anonymous", **kw):
    return apply_filters(_products(fi), FilterSpec(**kw), defs, fi, tier)


# --------------------------------------------------------------------------- P7.1 filters


def test_brand_by_name_and_id_or_within(fi, defs):
    by_name = _apply(fi, defs, brands=["brand a", "Brand B "])
    by_id = _apply(fi, defs, brands=[900001, "900002"])
    assert [p["id"] for p in by_name] == [p["id"] for p in by_id]
    assert {p["brandName"] for p in by_name} == {"Brand A", "Brand B"}
    assert resolve_brands(fi, ["Brand C"]) == {900003}


def test_unknown_brand_is_error_not_dropped(fi, defs):
    with pytest.raises(QueryError) as ei:
        _apply(fi, defs, brands=["Brand Z"])
    assert ei.value.code == "invalid_filter_value" and ei.value.extra["filter"] == "brands"
    with pytest.raises(QueryError):
        _apply(fi, defs, brands=[])


def test_price_range_local(fi, defs):
    out = _apply(fi, defs, price_min=2000, price_max=2500)
    assert sorted(p["price"] for p in out) == [2099, 2299, 2499]
    assert all(p["price"] is not None for p in _apply(fi, defs, price_min=0))  # null price excluded
    assert len(_apply(fi, defs, price_max=1600)) == 1


def test_recommended_false_filters_to_non_recommended(fi, defs):
    yes = _apply(fi, defs, recommended=True)
    no = _apply(fi, defs, recommended=False)
    assert {p["id"] for p in yes} == {500001, 500006}
    assert len(no) == 6 and not any(p["expertRatings"]["isRecommended"] for p in no)
    assert len(_apply(fi, defs)) == 8  # omitted = no filter


def test_group_by_name_and_id(fi, defs):
    a = _apply(fi, defs, group="31 - 33 Inch Widths")
    b = _apply(fi, defs, group=200369)
    c = _apply(fi, defs, group="200369")
    assert [p["id"] for p in a] == [p["id"] for p in b] == [p["id"] for p in c]
    assert len(a) == 3
    with pytest.raises(QueryError) as ei:
        resolve_group(fi, "Giant Widths")
    assert ei.value.code == "invalid_filter_value" and "31 - 33 Inch Widths" in ei.value.message


def test_boolean_feature_accepts_true_and_Yes(fi, defs):
    boolean_id = next(
        d.id for d in defs.values() if d.kind == "boolean" and d.source == "category_attributes"
    )
    a = _apply(fi, defs, features={boolean_id: True})
    b = _apply(fi, defs, features={str(boolean_id): "Yes"})
    c = _apply(fi, defs, features={defs[boolean_id].name: "yes"})
    assert [p["id"] for p in a] == [p["id"] for p in b] == [p["id"] for p in c]
    assert a and len(a) < 8
    no = _apply(fi, defs, features={boolean_id: False})
    assert len(a) + len(no) == 8
    with pytest.raises(QueryError) as ei:
        _apply(fi, defs, features={boolean_id: "maybe"})
    assert ei.value.code == "invalid_filter_value"


def test_numeric_feature_range_and_scalar(fi, defs):
    width = defs[6918]  # Exterior width, numeric-general, in.
    assert width.kind == "numeric-general"
    values = sorted(
        e["value"] for p in _products(fi) for e in p["attrs"] if e["attributeId"] == 6918
    )
    lo, hi = values[1], values[-2]
    ranged = _apply(fi, defs, features={"Exterior width": [lo, hi]})
    assert ranged and all(
        lo <= next(e["value"] for e in p["attrs"] if e["attributeId"] == 6918) <= hi for p in ranged
    )
    open_ended = _apply(fi, defs, features={6918: [None, hi]})
    assert len(open_ended) >= len(ranged)
    exact = _apply(fi, defs, features={6918: values[0]})
    assert exact
    with pytest.raises(QueryError):
        _apply(fi, defs, features={6918: "wide"})


def test_text_feature_exact_match(fi, defs):
    present = {e["attributeId"] for p in _products(fi) for e in p["attrs"]}
    text_id = next(
        d.id
        for d in defs.values()
        if d.kind == "text" and d.source == "category_attributes" and d.id in present
    )
    value = next(
        e["value"] for p in _products(fi) for e in p["attrs"] if e["attributeId"] == text_id
    )
    out = _apply(fi, defs, features={text_id: value.upper()})
    assert out and all(
        next(e["value"] for e in p["attrs"] if e["attributeId"] == text_id) == value for p in out
    )


def test_rating_filter_anonymous_is_filter_on_unavailable_attribute_unavailable(fi, defs):
    with pytest.raises(QueryError) as ei:
        _apply(fi, defs, tier="anonymous", features={"Thermostat performance": [4, 5]})
    assert ei.value.code == "filter_on_unavailable_attribute"
    assert ei.value.extra["reason"] == "unavailable"
    # the dictionary's `name` ("Thermostat Performance") is what the error names, not the entry's
    assert ei.value.extra["attribute"].lower() == "thermostat performance"


def test_rating_filter_on_retained_member_row_works_for_anonymous_caller(c37162, defs):
    fi = fill_scores_in(c37162["filter_instance"])
    out = apply_filters(
        list(fi["data"].values()),
        FilterSpec(features={"Thermostat performance": [1, 5]}),
        defs,
        fi,
        "member",  # the SERVED payload's tier, regardless of who asked
    )
    assert len(out) == 8


def test_not_applicable_marker_matches_no_rating_filter(c37162, defs):
    """CR's `0` is not a score (RECON §9i), so it matches no rating filter — including a range
    that spans it, which would otherwise return the models CR never ran the test on."""
    fi = fill_scores_in(c37162["filter_instance"])
    rating_id = next(d.id for d in defs.values() if d.kind == "numeric-rating-score")
    marked = next(iter(fi["data"].values()))
    for e in marked["attrs"]:
        if e["attributeId"] == rating_id:
            e["value"] = 0

    def run(spec):
        return apply_filters(
            list(fi["data"].values()), FilterSpec(features={rating_id: spec}), defs, fi, "member"
        )

    assert marked["id"] not in {p["id"] for p in run([0, 2])}
    assert marked["id"] not in {p["id"] for p in run(0)}
    assert marked["id"] not in {p["id"] for p in run([None, None])}


def test_a_rating_column_of_markers_is_an_unavailable_attribute(c37162, defs):
    fi = fill_scores_in(c37162["filter_instance"])
    rating_id = next(d.id for d in defs.values() if d.kind == "numeric-rating-score")
    for product in fi["data"].values():
        for e in product["attrs"]:
            if e["attributeId"] == rating_id:
                e["value"] = 0
    with pytest.raises(QueryError) as ei:
        apply_filters(
            list(fi["data"].values()), FilterSpec(features={rating_id: [1, 5]}), defs, fi, "member"
        )
    assert ei.value.code == "filter_on_unavailable_attribute"
    assert ei.value.extra["reason"] == "absent"


def test_banks_price_filter_is_absent_not_gated(banks):
    env = fixture_envelope(banks)
    fi = env["filter_instance"]
    defs = build_definitions(env)
    with pytest.raises(QueryError) as ei:
        apply_filters(list(fi["data"].values()), FilterSpec(price_min=1), defs, fi, "member")
    assert (
        ei.value.code == "filter_on_unavailable_attribute" and ei.value.extra["reason"] == "absent"
    )
    with pytest.raises(QueryError) as ei2:
        apply_filters(list(fi["data"].values()), FilterSpec(price_max=1), defs, fi, "anonymous")
    assert ei2.value.extra["reason"] == "unavailable"


def test_ambiguous_name_lists_both_ids(defs):
    from consumer_reports_mcp.attributes import Definition

    dup = dict(defs)
    dup[999901] = Definition(
        id=999901,
        name="Twin",
        display_name="Twin A",
        kind="text",
        unit=None,
        description=None,
        group=None,
        sort_order=None,
        source="attrs",
    )
    dup[999902] = Definition(
        id=999902,
        name="Other",
        display_name="Twin",
        kind="text",
        unit=None,
        description=None,
        group=None,
        sort_order=None,
        source="attrs",
    )
    with pytest.raises(QueryError) as ei:
        resolve_attribute(dup, "twin")
    assert ei.value.code == "ambiguous_filter_name"
    assert ei.value.extra["candidates"] == [999901, 999902]


def test_name_and_displayName_of_same_def_not_ambiguous(defs):
    d = next(d for d in defs.values() if d.display_name and d.name)
    assert resolve_attribute(defs, d.name).id == d.id
    assert resolve_attribute(defs, d.display_name).id == d.id
    assert resolve_attribute(defs, d.id).id == d.id


def test_unknown_filter_is_error_not_dropped(fi, defs):
    with pytest.raises(QueryError) as ei:
        _apply(fi, defs, features={"Warp drive": True})
    assert ei.value.code == "unknown_filter"


def test_filters_and_across_parameters(fi, defs):
    out = _apply(fi, defs, brands=["Brand A"], recommended=True, price_max=2000)
    assert [p["id"] for p in out] == [500001]


# ------------------------------------------------------------------ P7.2 order / nest / page


def _ids(products):
    return [p["id"] for p in products]


def test_within_group_order_identical_with_and_without_scores(c37162, fi):
    ranks = rank_table(fi)
    anon, info = order_products(_products(fi), fi, ranks, sort=None, order="desc", flat=False)
    filled = fill_scores_in(c37162["filter_instance"])
    member, _ = order_products(
        list(filled["data"].values()),
        filled,
        rank_table(filled),
        sort=None,
        order="desc",
        flat=False,
    )
    assert _ids(anon) == _ids(member)
    assert info.scope == WITHIN_GROUP and info.key == "overallScore" and info.notice is None


def test_never_sorted_on_overallDisplayScore_within_group(c37162):
    filled = fill_scores_in(c37162["filter_instance"])
    base, _ = order_products(
        list(filled["data"].values()),
        filled,
        rank_table(filled),
        sort="overallScore",
        order="desc",
        flat=False,
    )
    perturbed = fill_scores_in(c37162["filter_instance"])
    for p in perturbed["data"].values():
        p["overallDisplayScore"] = 100 - p["overallDisplayScore"]  # reverse every display score
    again, _ = order_products(
        list(perturbed["data"].values()),
        perturbed,
        rank_table(perturbed),
        sort="overallScore",
        order="desc",
        flat=False,
    )
    assert _ids(base) == _ids(again)


def test_within_group_order_is_index_asc_and_groups_in_subcats_order(fi):
    ordered, _ = order_products(
        _products(fi), fi, rank_table(fi), sort=None, order="desc", flat=False
    )
    assert _ids(ordered) == [500001, 500002, 500003, 500004, 500005, 500006, 500007, 500008]
    reverse, info = order_products(
        _products(fi), fi, rank_table(fi), sort="overallScore", order="asc", flat=False
    )
    assert _ids(reverse)[:2] == [500002, 500001] and info.order == "asc"


def test_nested_count_from_data_not_subcats(c200228):
    env = fixture_envelope(c200228)
    fi = env["filter_instance"]
    assert len(fi["args"]["subcats"]) == 4
    groups = nest(_products(fi), fi)
    assert len(groups) == 3
    assert [g["group"] for g in groups] == ["Almond Milk", "Oat Milk", "Soy Milk"]


def test_single_group_is_flat_and_group_named(banks):
    from consumer_reports_mcp.query import is_multi_group

    fi = fixture_envelope(banks)["filter_instance"]
    assert is_multi_group(fi) is False
    groups = nest(_products(fi), fi)
    assert len(groups) == 1 and groups[0]["group"] == "Banks" and groups[0]["group_id"] == 37154


def test_flat_default_is_page_order_scope_within_group(fi):
    ordered, info = order_products(
        _products(fi), fi, rank_table(fi), sort=None, order="desc", flat=True
    )
    assert _ids(ordered) == [500001, 500002, 500003, 500004, 500005, 500006, 500007, 500008]
    assert info.scope == WITHIN_GROUP and info.notice is None


def test_flat_explicit_overallScore_multi_group_is_cross_group_with_notice(c37162, fi):
    anon, info = order_products(
        _products(fi), fi, rank_table(fi), sort="overallScore", order="desc", flat=True
    )
    assert info.scope == CROSS_GROUP and info.notice
    assert _ids(anon) == [
        500001,
        500002,
        500003,
        500004,
        500005,
        500006,
        500007,
        500008,
    ]  # all null → page order
    filled = fill_scores_in(c37162["filter_instance"])
    member, info2 = order_products(
        list(filled["data"].values()),
        filled,
        rank_table(filled),
        sort="overallScore",
        order="desc",
        flat=True,
    )
    scores = [p["overallDisplayScore"] for p in member]
    assert scores == sorted(scores, reverse=True) and info2.scope == CROSS_GROUP


def test_price_nulls_last_both_directions(fi):
    asc, info = order_products(
        _products(fi), fi, rank_table(fi), sort="price", order="asc", flat=True
    )
    desc, _ = order_products(
        _products(fi), fi, rank_table(fi), sort="price", order="desc", flat=True
    )
    assert asc[-1]["price"] is None and desc[-1]["price"] is None
    assert [p["price"] for p in asc[:-1]] == sorted(p["price"] for p in asc[:-1])
    assert [p["price"] for p in desc[:-1]] == sorted((p["price"] for p in desc[:-1]), reverse=True)
    assert info.key == "price"


def test_rank_not_renumbered_under_filter(fi, defs):
    ranks = rank_table(fi)
    only_b = _apply(fi, defs, brands=["Brand B"])
    assert [ranks[p["id"]] for p in only_b] == [3]  # Brand B's best keeps rank 3
    assert ranks[500005] == 3


def test_size_vs_total(fi, defs):
    sizes = group_sizes(fi)
    assert sizes == {200367: 2, 200369: 3, 200371: 3}
    filtered = _apply(fi, defs, brands=["Brand A"])
    groups = nest(filtered, fi)
    assert [(g["group_id"], len(g["products"])) for g in groups] == [
        (200367, 1),
        (200369, 1),
        (200371, 1),
    ]


def test_truncated_and_offset_past_total():
    items = list(range(7))
    sliced, total, truncated = page(items, 3, 0)
    assert sliced == [0, 1, 2] and total == 7 and truncated is True
    sliced, total, truncated = page(items, 3, 6)
    assert sliced == [6] and truncated is False
    sliced, total, truncated = page(items, 3, 7)
    assert sliced == [] and total == 7 and truncated is False
    with pytest.raises(QueryError):
        page(items, 3, -1)


def test_limit_per_group_nested():
    assert resolve_limit(None, detail="standard", nested=True) == 10
    assert resolve_limit(None, detail="standard", nested=False) == 25
    assert resolve_limit(200, detail="summary", nested=False) == 200
    with pytest.raises(QueryError):
        resolve_limit(201, detail="summary", nested=False)
    with pytest.raises(QueryError):
        resolve_limit(0, detail="summary", nested=False)


def test_full_capped_at_10():
    assert resolve_limit(None, detail="full", nested=True) == 5  # measured default, cap stays 10
    assert resolve_limit(10, detail="full", nested=True) == 10
    assert resolve_limit(None, detail="full", nested=False) == 10
    with pytest.raises(QueryError) as ei:
        resolve_limit(11, detail="full", nested=False)
    assert "10" in ei.value.message and ei.value.code == "invalid_filter_value"


def test_bad_sort_and_order_are_errors(fi):
    with pytest.raises(QueryError):
        order_products(_products(fi), fi, rank_table(fi), sort="score", order="desc", flat=True)
    with pytest.raises(QueryError):
        order_products(_products(fi), fi, rank_table(fi), sort=None, order="down", flat=True)


# --------------------------------------------------------------------------- batch-3 findings


def test_a_product_without_a_group_is_no_second_group(banks):
    """A single-group category carrying one groupless product stayed single-group: an explicit
    flat `overallScore` sort must not be labelled `cross_group` on its account."""
    import copy

    from consumer_reports_mcp.query import is_multi_group

    fi = copy.deepcopy(banks["filter_instance"])
    pid, loose = next(iter(fi["data"].items()))
    del loose["_groupId"]
    assert not is_multi_group(fi)
    products = _products(fi)
    ordered, info = order_products(
        products, fi, rank_table(fi), sort="overallScore", order="desc", flat=True
    )
    assert info.scope == WITHIN_GROUP and info.notice is None
    assert ordered[-1]["id"] == loose["id"]  # in no group: sorted after every group
    blocks = nest(products, fi)
    assert [b["group_id"] for b in blocks] == [37154, None]
    assert [p["id"] for p in blocks[-1]["products"]] == [loose["id"]]
    assert group_sizes(fi)[None] == 1 and group_sizes(fi)[37154] == len(products) - 1


def test_a_feature_filter_on_an_all_blank_column_is_unavailable_not_empty(c200228):
    """`""` is CR's empty cell (measured 13× on c200228's `Price per serving`). A column of
    them is not "values that match nothing" — it is `filter_on_unavailable_attribute`, the
    reading `coerce` already gives a blank."""
    import copy

    from consumer_reports_mcp.ingest import build_envelope
    from tests.conftest import ratings_wrapper_of

    fx = copy.deepcopy(c200228)
    fi = fx["filter_instance"]
    for p in fi["data"].values():
        for e in p.get("attrs", []):
            if int(e["attributeId"]) == 6687:
                e["value"] = ""
    env = build_envelope(
        fi,
        ratings_wrapper_of(fx),
        data_subscriber="true",
        final_url=fx["final_url"],
        requested_id=None,
        http_status=200,
    )
    defs = build_definitions(env)
    assert defs[6687].kind == "numeric-general"
    with pytest.raises(QueryError) as ei:
        _apply(fi, defs, tier="member", features={"Price per serving": [40, 60]})
    assert ei.value.code == "filter_on_unavailable_attribute"
    assert ei.value.extra["reason"] == "absent"
