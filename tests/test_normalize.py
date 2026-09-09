"""P2.4 — product shapes, within-group rank, scores_available (SPEC §7, §10; RECON §10j, §9f)."""

from __future__ import annotations

import random

import pytest

from consumer_reports_mcp.attributes import build_definitions
from consumer_reports_mcp.ingest import build_envelope
from consumer_reports_mcp.normalize import (
    attribute_all_null,
    product_shape,
    rank_table,
    scores_available,
    standard_attribute_ids,
)
from tests.conftest import fill_scores_in, ratings_wrapper_of


def _env(fx, *, fill=False, wrapper=True):
    fi = fill_scores_in(fx["filter_instance"]) if fill else fx["filter_instance"]
    return build_envelope(
        fi,
        ratings_wrapper_of(fx) if wrapper else None,
        data_subscriber="true" if fill else "false",
        final_url=fx["final_url"],
        requested_id=None,
        http_status=200,
    )


@pytest.fixture
def env(c37162):
    return _env(c37162)


def test_standard_ids_identical_after_shuffling_definitions(c37162):
    env = _env(c37162)
    ids = standard_attribute_ids(env, build_definitions(env))
    assert len(ids) == 3
    for seed in range(5):
        shuffled = dict(env, category_attributes=list(env["category_attributes"]))
        random.Random(seed).shuffle(shuffled["category_attributes"])
        assert standard_attribute_ids(shuffled, build_definitions(shuffled)) == ids


def test_standard_ids_with_missing_sortOrder_sorts_last(c37162):
    env = _env(c37162)
    defs = build_definitions(env)
    ratings = [d for d in defs.values() if d.kind == "numeric-rating-score"]
    # strip sortOrder from the definition that would otherwise come first
    first = min(ratings, key=lambda d: (d.sort_order is None, d.sort_order or 0, d.id))
    cats = [dict(d) for d in env["category_attributes"]]
    for d in cats:
        if d["attributeId"] == first.id:
            d.pop("sortOrder", None)
    env2 = dict(env, category_attributes=cats)
    assert len(ratings) > 3  # precondition: there is something to be displaced
    ids = standard_attribute_ids(env2, build_definitions(env2))
    assert first.id not in ids


def test_standard_ids_fall_back_to_attrs_then_entries(c37162):
    env = _env(c37162, wrapper=False)
    defs = build_definitions(env)
    ids = standard_attribute_ids(env, defs)
    assert len(ids) == 3 and all(defs[i].source == "attrs" for i in ids)
    bare = dict(env, filter_instance=dict(env["filter_instance"], attrs=[]))
    ids2 = standard_attribute_ids(bare, build_definitions(bare))
    assert len(ids2) == 3 and ids2 == sorted(ids2)


def test_summary_has_group_rank_dont_buy_smart_buy_overall_score_null(env):
    defs = build_definitions(env)
    ranks = rank_table(env["filter_instance"])
    p = env["filter_instance"]["data"]["500001"]
    s = product_shape(p, "summary", env, defs, ranks)
    assert set(s) == {
        "id",
        "brand",
        "model",
        "group",
        "rank",
        "price",
        "overall_score",
        "recommended",
        "dont_buy",
        "smart_buy",
    }
    assert s["overall_score"] is None and s["group"] == "30 Inch and Narrower Widths"
    assert s["rank"] == 1 and s["recommended"] is True and s["dont_buy"] is False
    assert s["smart_buy"] is False


def test_dont_buy_true_survives_summary(env):
    defs = build_definitions(env)
    ranks = rank_table(env["filter_instance"])
    s = product_shape(env["filter_instance"]["data"]["500004"], "summary", env, defs, ranks)
    assert s["dont_buy"] is True
    s7 = product_shape(env["filter_instance"]["data"]["500007"], "summary", env, defs, ranks)
    assert s7["smart_buy"] is True


def test_standard_and_full_shapes(env):
    defs = build_definitions(env)
    ranks = rank_table(env["filter_instance"])
    p = env["filter_instance"]["data"]["500003"]
    st = product_shape(p, "standard", env, defs, ranks)
    assert len(st["ratings"]) == 3 and all(r["value"] is None for r in st["ratings"])
    assert all(r["name"] for r in st["ratings"])
    assert st["owner_satisfaction"] == 5 and st["predicted_reliability"] == 3
    warnings: list[str] = []
    full = product_shape(p, "full", env, defs, ranks, warnings=warnings)
    assert full["retailers"] == 3 and full["retailer_prices"] == [2299, 2399, 2299.99]
    assert len(full["attributes"]) == len(p["attrs"])
    assert full["attributes"][0]["description"] is None
    assert warnings == [
        f"coercion_failed:{next(a['attributeId'] for a in p['attrs'] if a['value'] == '36 - 38')}"
    ]
    groups = [a["group"] for a in full["attributes"]]
    assert groups == sorted(groups, key=lambda g: g or "~")
    with_desc = product_shape(p, "full", env, defs, ranks, include_descriptions=True)
    assert any(a["description"] for a in with_desc["attributes"])


def test_attributes_projection_at_summary(env):
    defs = build_definitions(env)
    ranks = rank_table(env["filter_instance"])
    p = env["filter_instance"]["data"]["500001"]
    s = product_shape(p, "summary", env, defs, ranks, extra_attribute_ids=(6918,))
    assert s["projected_attributes"][0]["id"] == 6918
    assert s["projected_attributes"][0]["unit"] == "in."
    assert s["projected_attributes"][0]["description"] is None
    full = product_shape(p, "full", env, defs, ranks, extra_attribute_ids=(6918,))
    assert full["projected_attributes"][0]["id"] == 6918 and len(full["attributes"]) > 1


def test_rank_dense_within_group_ties_share(env):
    ranks = rank_table(env["filter_instance"])
    assert ranks[500001] == 1 and ranks[500002] == 2
    assert ranks[500003] == 1 and ranks[500004] == 2 and ranks[500005] == 3
    assert ranks[500006] == 1 and ranks[500007] == 2 and ranks[500008] == 2  # tie shares


def test_rank_null_when_sort_index_null(c37162):
    fi = dict(c37162["filter_instance"])
    fi["data"] = {k: dict(v) for k, v in fi["data"].items()}
    fi["data"]["500001"]["_overallSortIndex"] = None
    ranks = rank_table(fi)
    assert ranks[500001] is None and ranks[500002] == 1


def test_rank_identical_with_and_without_scores(c37162):
    assert rank_table(c37162["filter_instance"]) == rank_table(
        fill_scores_in(c37162["filter_instance"])
    )


def test_scores_available_unavailable_anon_absent_member(c37162):
    fi = c37162["filter_instance"]
    anon = scores_available(fi, "anonymous")
    member = scores_available(fi, "member")
    assert anon["overall_score"] == "unavailable" and anon["attribute_ratings"] == "unavailable"
    assert member["overall_score"] == "absent" and member["attribute_ratings"] == "absent"
    assert anon["owner_satisfaction"] == "available" == member["owner_satisfaction"]
    filled = scores_available(fill_scores_in(fi), "member")
    assert filled["overall_score"] == "available" and filled["attribute_ratings"] == "available"
    assert set(anon) == {
        "overall_score",
        "attribute_ratings",
        "owner_satisfaction",
        "predicted_reliability",
        "recommended_flag",
    }
    assert all(v in ("available", "absent", "unavailable") for v in anon.values())


def test_recommended_flag_available_on_banks_with_zero_true(banks):
    fi = banks["filter_instance"]
    assert all(p["expertRatings"]["isRecommended"] is False for p in fi["data"].values())
    assert scores_available(fi, "anonymous")["recommended_flag"] == "available"
    assert scores_available(fi, "member")["owner_satisfaction"] == "absent"
    assert scores_available(fi, "anonymous")["owner_satisfaction"] == "unavailable"


def test_scores_available_any_not_all(c37162):
    fi = c37162["filter_instance"]
    nulls = sum(1 for p in fi["data"].values() if p["surveys"]["ownerSatisfaction"] is None)
    assert nulls == 2  # 6 of 8 carry survey scores
    assert scores_available(fi, "anonymous")["owner_satisfaction"] == "available"


def test_banks_single_group_names_itself(banks):
    env = _env(banks)
    defs = build_definitions(env)
    ranks = rank_table(env["filter_instance"])
    shapes = [
        product_shape(p, "summary", env, defs, ranks)
        for p in env["filter_instance"]["data"].values()
    ]
    assert {s["group"] for s in shapes} == {"Banks"}
    assert all(s["price"] is None for s in shapes)
    assert sorted(s["rank"] for s in shapes) == [1, 2, 3, 4]


# --------------------------------------------------------------------------- batch-3 findings


def test_a_product_without_a_group_has_a_null_rank(c37162):
    """`rank` is a position in a group's table; a product in no group has no table. Ranking
    the groupless among themselves handed such a product `rank: 1`."""
    import copy

    fi = copy.deepcopy(c37162["filter_instance"])
    pid, p = next(iter(fi["data"].items()))
    del p["_groupId"]
    ranks = rank_table(fi)
    assert ranks[int(pid)] is None
    assert sum(1 for r in ranks.values() if r == 1) == 3  # the three real groups' leaders


def test_blank_rating_cells_are_not_scores(c37162):
    """A rating column shipped as `""` — CR's empty cell — is not a score: not `scored=1`
    (a row never-downgrade would then retain), not `attribute_ratings: available`, and
    `attribute_all_null` for the filter engine. One reading, `is_blank`, in all three."""
    import copy

    from consumer_reports_mcp.attributes import is_blank
    from consumer_reports_mcp.ingest import is_scored
    from consumer_reports_mcp.normalize import attribute_all_null

    assert is_blank(None) and is_blank("") and is_blank("   ")
    assert not is_blank(0) and not is_blank("0") and not is_blank(False)
    fi = copy.deepcopy(c37162["filter_instance"])
    aids: set[int] = set()
    for p in fi["data"].values():
        for e in p.get("attrs", []):
            if e.get("attributeTypeName") == "numeric-rating-score":
                e["value"] = ""
                aids.add(int(e["attributeId"]))
    assert aids
    assert is_scored(fi) is False
    assert scores_available(fi, "anonymous")["attribute_ratings"] == "unavailable"
    assert scores_available(fi, "member")["attribute_ratings"] == "absent"
    assert all(attribute_all_null(fi, aid) for aid in aids)


def test_a_product_lacking_a_requested_attribute_projects_a_complete_null_record(env):
    """CR ships different attribute sets across products in one category. A requested
    attribute a product does not carry is projected as a null-valued record with the SAME
    key set as a present one — `Attribute.description` is required-but-nullable, and the
    record used to omit it, so `cr_ratings(attributes=[…])` raised a ValidationError out of
    the tool for any page where one product lacked the attribute."""
    from consumer_reports_mcp import envelope as E

    defs = build_definitions(env)
    ranks = rank_table(env["filter_instance"])
    p = dict(env["filter_instance"]["data"]["500001"])
    p["attrs"] = [a for a in p["attrs"] if a["attributeId"] != 6918]
    s = product_shape(p, "summary", env, defs, ranks, extra_attribute_ids=(6918,))
    (rec,) = s["projected_attributes"]
    present = product_shape(
        env["filter_instance"]["data"]["500001"],
        "summary",
        env,
        defs,
        ranks,
        extra_attribute_ids=(6918,),
    )["projected_attributes"][0]
    assert set(rec) == set(present)
    assert rec["id"] == 6918 and rec["value"] is None and rec["description"] is None
    E.Attribute(**rec)  # the envelope accepts it
    E.ProductSummary(**s)


def test_product_names_and_group_are_decoded(env):
    """`brandName`/`modelName`/`_groupName` arrive entity-encoded from CR's JSON and used to be
    served raw — `Aspire 14&quot; AI` to the caller."""
    import copy

    defs = build_definitions(env)
    ranks = rank_table(env["filter_instance"])
    p = copy.deepcopy(env["filter_instance"]["data"]["500001"])
    p["modelName"], p["brandName"] = "Aspire 14&quot; AI  Copilot+", "Black &amp; Decker"
    p["_groupName"] = "30 &ndash; 32 Inch"
    s = product_shape(p, "summary", env, defs, ranks)
    assert s["model"] == 'Aspire 14" AI Copilot+' and s["brand"] == "Black & Decker"
    assert s["group"] == "30 – 32 Inch"


# ------------------------------------------------------- CR's `0` on a rating column (RECON §9i)

HOT_GARAGE = 11196


def _rating_fi(*values, kind="numeric-rating-score"):
    """One product per value, each carrying the one attribute."""
    return {
        "data": {
            str(1000 + i): {
                "id": 1000 + i,
                "_groupId": 1,
                "_groupName": "G",
                "_overallSortIndex": float(i),
                "overallDisplayScore": None,
                "attrs": [
                    {
                        "attributeId": HOT_GARAGE,
                        "attributeTypeName": kind,
                        "name": "Hot Garage Ready",
                        "value": v,
                    }
                ],
            }
            for i, v in enumerate(values)
        }
    }


def _rating_env(fi):
    return {
        "filter_instance": fi,
        "category_attributes": [
            {
                "attributeId": HOT_GARAGE,
                "name": "Hot Garage Ready",
                "attributeDataTypeName": "numeric-rating-score",
                "sortOrder": 8,
            }
        ],
    }


def test_standard_ratings_carry_the_status_and_a_null_value():
    fi = _rating_fi(0, 4)
    env = _rating_env(fi)
    defs = build_definitions(env)
    products = list(fi["data"].values())
    ranks = rank_table(fi)
    na = product_shape(products[0], "standard", env, defs, ranks)
    rated = product_shape(products[1], "standard", env, defs, ranks)
    assert na["ratings"] == [
        {"id": HOT_GARAGE, "name": "Hot Garage Ready", "value": None, "status": "not_applicable"}
    ]
    assert rated["ratings"] == [
        {"id": HOT_GARAGE, "name": "Hot Garage Ready", "value": 4, "status": None}
    ]


def test_a_product_missing_the_attribute_is_not_not_applicable():
    """ "CR shipped no entry" and "CR shipped its not-applicable marker" are different facts."""
    fi = _rating_fi(4)
    env = _rating_env(fi)
    bare = {"id": 2000, "_groupId": 1, "_overallSortIndex": 9.0, "attrs": []}
    shape = product_shape(bare, "standard", env, build_definitions(env), {})
    assert shape["ratings"] == [
        {"id": HOT_GARAGE, "name": "Hot Garage Ready", "value": None, "status": None}
    ]
    projected = product_shape(
        bare, "summary", env, build_definitions(env), {}, extra_attribute_ids=(HOT_GARAGE,)
    )["projected_attributes"]
    assert projected[0]["status"] is None and projected[0]["raw_value"] is None


def test_a_column_of_not_applicable_markers_reports_no_ratings():
    """SPEC §10: `available` means a product carries a value. CR's `0` is not one, so a member
    row whose only rating column is all-`0` is `absent`, not `available`."""
    assert scores_available(_rating_fi(0, 0), "member")["attribute_ratings"] == "absent"
    assert scores_available(_rating_fi(0, 0), "anonymous")["attribute_ratings"] == "unavailable"
    assert scores_available(_rating_fi(0, 3), "member")["attribute_ratings"] == "available"


def test_attribute_all_null_reads_the_marker_but_not_a_zero_spec():
    assert attribute_all_null(_rating_fi(0, 0), HOT_GARAGE)
    assert not attribute_all_null(_rating_fi(0, 3), HOT_GARAGE)
    # the same zeros on a spec column are measurements: a filter on it is legitimate
    assert not attribute_all_null(_rating_fi(0, 0, kind="numeric-general"), HOT_GARAGE)
