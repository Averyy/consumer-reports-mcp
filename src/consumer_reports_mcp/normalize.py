"""Product shapes, within-group rank and `scores_available` (PLAN P2.4, SPEC §7/§10). Pure."""

from __future__ import annotations

import re
from typing import Any

from .attributes import (
    RATING_KIND,
    SOURCE_ATTRS,
    SOURCE_CATEGORY_ATTRIBUTES,
    Definition,
    is_blank,
    normalize_entry,
    rating_definitions,
)
from .ingest import products_of

_ASCII_INT = re.compile(r"^-?[0-9]+$")

DETAILS = ("summary", "standard", "full")
STANDARD_RATINGS = 3
AVAILABLE, ABSENT, UNAVAILABLE = "available", "absent", "unavailable"
SCORE_KEYS = (
    "overall_score",
    "attribute_ratings",
    "owner_satisfaction",
    "predicted_reliability",
    "recommended_flag",
)


def standard_attribute_ids(envelope: dict, defs: dict[int, Definition]) -> list[int]:
    """The first three rating attributes by `sortOrder` (missing last, tie on id) — RECON §10j.

    Source order: `categoryAttributes` definitions, else `attrs` definitions, else the entry
    types on the products themselves.
    """
    for source in (SOURCE_CATEGORY_ATTRIBUTES, SOURCE_ATTRS):
        chosen = [d for d in rating_definitions(defs) if d.source == source]
        if chosen:
            return [d.id for d in chosen[:STANDARD_RATINGS]]
    ids: set[int] = set()
    for p in products_of(envelope):
        for e in p.get("attrs") or []:
            if e.get("attributeTypeName") == RATING_KIND and e.get("attributeId") is not None:
                try:
                    ids.add(int(e["attributeId"]))
                except (TypeError, ValueError):
                    continue
    return sorted(ids)[:STANDARD_RATINGS]


def _pid(p: dict) -> int | None:
    v = p.get("id")
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def _sort_index(p: dict) -> float | None:
    v = p.get("_overallSortIndex")
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return v


def group_id_of(p: dict) -> int | None:
    """`_groupId` as an int — an int, a digit string or an integral float; anything else (a
    product CR ships without a group) is None. The one reading every group-keyed derivation
    uses: rank, nesting, sizes, the multi-group test."""
    return as_int(p.get("_groupId"))


def as_int(v: Any) -> int | None:
    """An int, an integer-valued float, or a string of ASCII digits (optionally signed) → int;
    anything else, a bool included, → None. Never raises: `str.isdigit()` accepts `'²'` and
    `'①'`, which `int()` rejects, and `int()` refuses a string past 4,300 digits — a
    caller-supplied `group` used to crash the tool on both."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and _ASCII_INT.match(v.strip()):
        try:
            return int(v.strip())
        except ValueError:  # past Python's digit limit
            return None
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def rank_table(filter_instance: dict) -> dict[int, int | None]:
    """Dense rank on `_overallSortIndex` within `_groupId`, over CR's UNFILTERED group.

    Ties share a position (1, 2, 2, 3). A null sort index has a null rank, and so does a
    product without a `_groupId`: `rank` is a position in a group's table, and a product in no
    group has no table to be ranked in (ranking the groupless among themselves handed such a
    product `rank: 1`). Identical in both tiers because the index is identical in both
    (RECON §9g).
    """
    by_group: dict[int, list[dict]] = {}
    ranks: dict[int, int | None] = {}
    for p in products_of(filter_instance):
        if _pid(p) is None:
            continue
        gid = group_id_of(p)
        if gid is None:
            ranks[_pid(p)] = None
            continue
        by_group.setdefault(gid, []).append(p)
    for members in by_group.values():
        ranked = sorted(
            (p for p in members if _sort_index(p) is not None), key=lambda p: _sort_index(p)
        )
        rank = 0
        prev: float | None = None
        for p in ranked:
            idx = _sort_index(p)
            if idx != prev:
                rank += 1
                prev = idx
            ranks[_pid(p)] = rank
        for p in members:
            if _sort_index(p) is None:
                ranks[_pid(p)] = None
    return ranks


def _survey(p: dict, key: str) -> int | float | None:
    block = (p.get("surveys") or {}).get(key)
    if not isinstance(block, dict):
        return None
    v = block.get("surveyScore")
    return v if isinstance(v, int | float) and not isinstance(v, bool) else None


def _expert(p: dict, key: str) -> bool | None:
    block = p.get("expertRatings") or {}
    v = block.get(key)
    return v if isinstance(v, bool) else None


def _shopping(p: dict) -> dict:
    s = p.get("_shoppingParsed")
    return s if isinstance(s, dict) else {}


def _entries_by_id(p: dict) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for e in p.get("attrs") or []:
        try:
            out[int(e["attributeId"])] = e
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _def_sort_key(d: Definition | None, aid: int) -> tuple:
    group = (d.group if d else None) or "~"
    so = d.sort_order if d else None
    return (group, so is None, so or 0.0, aid)


def product_shape(
    product: dict,
    detail: str,
    envelope: dict,
    defs: dict[int, Definition],
    ranks: dict[int, int | None],
    *,
    standard_ids: list[int] | None = None,
    extra_attribute_ids: tuple[int, ...] | list[int] = (),
    warnings: list[str] | None = None,
    include_descriptions: bool = False,
) -> dict:
    """SPEC §7 *Product shape*. `group`, `rank`, `dont_buy`, `smart_buy`, `overall_score` ship at
    every level; `overall_score` is `null`, never absent.

    Pass `standard_ids` (from `standard_attribute_ids`) when shaping many products — computing
    it per product walks every entry in the payload on the fallback path."""
    if detail not in DETAILS:
        raise ValueError(f"unknown detail {detail!r}")
    pid = _pid(product)
    if pid is None:
        raise ValueError("product without an id")
    entries = _entries_by_id(product)
    shape: dict[str, Any] = {
        "id": pid,
        "brand": product.get("brandName"),
        "model": product.get("modelName"),
        "group": product.get("_groupName"),
        "rank": ranks.get(pid),
        "price": product.get("price"),
        "overall_score": product.get("overallDisplayScore"),
        "recommended": _expert(product, "isRecommended"),
        "dont_buy": _expert(product, "isDontBuy"),
        "smart_buy": _shopping(product).get("isSmartBuy"),
    }
    if detail in ("standard", "full"):
        ids = standard_ids if standard_ids is not None else standard_attribute_ids(envelope, defs)
        ratings = []
        for aid in ids:
            d = defs.get(aid)
            entry = entries.get(aid)
            if entry is not None:
                n = normalize_entry(entry, defs, warnings, include_description=False)
                ratings.append({"id": aid, "name": n["name"], "value": n["value"]})
            else:
                ratings.append({"id": aid, "name": d.name if d else None, "value": None})
        shape["ratings"] = ratings
        shape["owner_satisfaction"] = _survey(product, "ownerSatisfaction")
        shape["predicted_reliability"] = _survey(product, "reliability")
    if detail == "full":
        shop = _shopping(product)
        shape["retailers"] = shop.get("count")
        shape["retailer_prices"] = list(shop.get("prices") or [])
        ordered = sorted(entries.items(), key=lambda kv: _def_sort_key(defs.get(kv[0]), kv[0]))
        shape["attributes"] = [
            normalize_entry(e, defs, warnings, include_description=include_descriptions)
            for _, e in ordered
        ]
    if extra_attribute_ids:
        projected = []
        for aid in extra_attribute_ids:
            entry = entries.get(int(aid))
            if entry is None:
                d = defs.get(int(aid))
                projected.append(
                    {
                        "id": int(aid),
                        "name": d.name if d else None,
                        "kind": d.kind if d else None,
                        "value": None,
                        "raw_value": None,
                        "unit": d.unit if d else None,
                        # the same key set `normalize_entry` returns: `Attribute.description`
                        # is required-but-nullable, and a product that lacks a requested
                        # attribute used to raise a ValidationError out of `cr_ratings`
                        "description": None,
                        "group": d.group if d else None,
                    }
                )
            else:
                projected.append(normalize_entry(entry, defs, warnings, include_description=False))
        shape["projected_attributes"] = projected  # one key at every detail level
    return shape


def scores_available(filter_instance: dict, data_tier: str) -> dict[str, str]:
    """SPEC §10: `available` if ANY product has a non-null value, evaluated over the WHOLE payload;
    otherwise `absent` for a member row and `unavailable` for an anonymous one."""
    products = products_of(filter_instance)
    found = dict.fromkeys(SCORE_KEYS, False)
    for p in products:
        if p.get("overallDisplayScore") is not None:
            found["overall_score"] = True
        if _survey(p, "ownerSatisfaction") is not None:
            found["owner_satisfaction"] = True
        if _survey(p, "reliability") is not None:
            found["predicted_reliability"] = True
        if _expert(p, "isRecommended") is not None:
            found["recommended_flag"] = True
        if not found["attribute_ratings"]:
            for e in p.get("attrs") or []:
                if e.get("attributeTypeName") == RATING_KIND and not is_blank(e.get("value")):
                    found["attribute_ratings"] = True
                    break
        if all(found.values()):
            break
    fallback = ABSENT if data_tier == "member" else UNAVAILABLE
    return {k: (AVAILABLE if v else fallback) for k, v in found.items()}


def attribute_all_null(filter_instance: dict, attribute_id: int) -> bool:
    """True when every product's value for this attribute is null, blank (`""`, CR's empty
    cell) or missing in the payload — `is_blank`, the same reading `coerce` gives a blank."""
    for p in products_of(filter_instance):
        for e in p.get("attrs") or []:
            try:
                if int(e.get("attributeId")) == attribute_id and not is_blank(e.get("value")):
                    return False
            except (TypeError, ValueError):
                continue
    return True


def price_all_null(filter_instance: dict) -> bool:
    return all(p.get("price") is None for p in products_of(filter_instance))
