"""The `initStore` parser and `cr_reliability` (PLAN P8.1, SPEC §7 `cr_reliability`).

The isolated second envelope: the only consumer of `reliability_raw`. Always
`auth_state: "anonymous"`, no `session`, no `sort`; `scores_available` from the brand arrays with
`absent` never `unavailable` — nothing on this page is gated.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import TYPE_CHECKING, Any

from . import envelope as E
from .repository import ToolError

if TYPE_CHECKING:
    from .runtime import Runtime

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def _text(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    return _WS.sub(" ", html_lib.unescape(_TAG.sub(" ", v))).strip() or None


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def _num(v: Any) -> int | float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    return v


def _find_category(store: dict, category_id: int) -> dict | None:
    data = store.get("data") if isinstance(store, dict) else None
    if not isinstance(data, dict):
        return None
    cat = data.get("category")
    if isinstance(cat, dict) and _int(cat.get("_id")) == category_id:
        return cat
    for c in data.get("categories") or []:
        if isinstance(c, dict) and _int(c.get("_id")) == category_id:
            return c
    return None


def parse_reliability(store: dict, category_id: int, *, full: bool = False) -> dict:
    """{brands[], methodology, has_reliability_data, has_owner_satisfaction_data, siblings}.

    Brands are joined by `brandId` across the two surveys in CR's own `sortOrder`; a brand with
    one survey and not the other keeps a `null`, never a dropped row.
    """
    cat = _find_category(store, category_id)
    data = store.get("data") if isinstance(store, dict) else {}
    siblings: list[int] = []
    for c in (data or {}).get("categories") or []:
        cid = _int(c.get("_id")) if isinstance(c, dict) else None
        if cid is not None:
            siblings.append(cid)
    if cat is None:
        return {
            "found": False,
            "brands": [],
            "methodology": None,
            "has_reliability_data": False,
            "has_owner_satisfaction_data": False,
            "siblings": siblings,
            "warnings": [],
        }
    surveys = cat.get("surveys") if isinstance(cat.get("surveys"), dict) else {}
    rel = surveys.get("reliability") if isinstance(surveys.get("reliability"), dict) else {}
    own = (
        surveys.get("ownerSatisfaction")
        if isinstance(surveys.get("ownerSatisfaction"), dict)
        else {}
    )
    has_rel = cat.get("HasReliabilityData")
    has_own = cat.get("HasOwnerSatisfactionData")
    rel_rows = rel.get("productGroupSurveyValue") or []
    own_rows = own.get("productGroupSurveyValue") or []
    if not isinstance(has_rel, bool):
        has_rel = bool(rel_rows)
    if not isinstance(has_own, bool):
        has_own = bool(own_rows)
    brands: dict[int, dict] = {}
    warnings: list[str] = []
    if isinstance(cat.get("HasReliabilityData"), bool) and has_rel != bool(rel_rows):
        warnings.append("survey_flag_mismatch:reliability")
    if isinstance(cat.get("HasOwnerSatisfactionData"), bool) and has_own != bool(own_rows):
        warnings.append("survey_flag_mismatch:owner_satisfaction")

    def add(rows: list, key: str, key100: str) -> list[tuple[float, int, int]]:
        """Merge one survey's rows; return its own (sortOrder, array index, brandId) order."""
        order: list[tuple[float, int, int]] = []
        for i, r in enumerate(rows):
            if not isinstance(r, dict):
                continue
            bid = _int(r.get("brandId"))
            if bid is None:
                continue
            entry = brands.setdefault(
                bid,
                {
                    "brand_id": bid,
                    "brand_name": r.get("brandName"),
                    "predicted_reliability": None,
                    "owner_satisfaction": None,
                    "predicted_reliability_100": None,
                    "owner_satisfaction_100": None,
                },
            )
            if entry["brand_name"] is None:
                entry["brand_name"] = r.get("brandName")
            entry[key] = _num(r.get("surveyScore"))
            entry[key100] = _num(r.get("survey100PtScore"))
            so = r.get("sortOrder")
            order.append((float(so) if isinstance(so, int | float) else float("inf"), i, bid))
        return order

    rel_order = add(rel_rows, "predicted_reliability", "predicted_reliability_100")
    own_order = add(own_rows, "owner_satisfaction", "owner_satisfaction_100")
    # "Brand-ranked predicted reliability": the RELIABILITY survey's own sortOrder is the ranking
    # (the two surveys are different permutations); brands with only an owner-satisfaction
    # score follow, in that survey's order. With no reliability survey, the OS order stands.
    primary, secondary = (rel_order, own_order) if rel_order else (own_order, rel_order)
    seen: set[int] = set()
    ordered: list[dict] = []
    for _, _, bid in sorted(primary) + sorted(secondary):
        if bid in seen:
            continue
        seen.add(bid)
        b = dict(brands[bid])
        if not full:
            b.pop("predicted_reliability_100")
            b.pop("owner_satisfaction_100")
        ordered.append(b)
    methodology = {
        "reliability": _text(rel.get("blurb")) or _text(rel.get("infoText")),
        "owner_satisfaction": _text(own.get("blurb")) or _text(own.get("infoText")),
        "footnote": _text(rel.get("footnote")) or _text(own.get("footnote")),
    }
    return {
        "found": True,
        "brands": ordered if has_rel or has_own else [],
        "methodology": methodology,
        "has_reliability_data": bool(has_rel),
        "has_owner_satisfaction_data": bool(has_own),
        "siblings": siblings,
        "warnings": warnings,
    }


def survey_scores_available(brands: list[dict]) -> dict[str, str]:
    """`available` if any brand carries the score, else `absent` — never `unavailable`."""
    rel = any(b.get("predicted_reliability") is not None for b in brands)
    own = any(b.get("owner_satisfaction") is not None for b in brands)
    return {
        "predicted_reliability": "available" if rel else "absent",
        "owner_satisfaction": "available" if own else "absent",
    }


def availability_for_product(
    rt: Runtime, category_id: int, product_id: int
) -> tuple[str | None, str | None]:
    """`modelAvailabilityName` merged ONLY when a reliability row is already cached — never
    fetched to populate it (SPEC §7). Returns (availability, warning)."""
    sel = rt.cache.select_reliability(category_id, rt.settings.cache_ttl_days, rt.clock())
    if sel is None:
        return None, "availability_not_cached"
    store = rt.cache.load_reliability(sel.rowid)
    data = store.get("data") if isinstance(store, dict) else None
    data = data if isinstance(data, dict) else {}
    # a fan-out row carries the FETCHED category's models[] only (RECON §9): a sibling's row
    # cannot say anything about this category's models
    own = data.get("category") if isinstance(data.get("category"), dict) else {}
    if _int(own.get("_id")) != category_id:
        return None, "availability_not_cached"
    for m in data.get("models") or []:
        if isinstance(m, dict) and _int(m.get("_id")) == product_id:
            name = m.get("modelAvailabilityName")
            return (str(name) if name is not None else None), (
                "availability_stale" if sel.stale else None
            )
    return None, ("availability_stale" if sel.stale else None)


async def cr_reliability(
    rt: Runtime,
    category: str | int,
    detail: str = "standard",
    include_methodology: bool = False,
    refresh: bool = False,
) -> E.ReliabilityEnvelope:
    if detail not in ("standard", "full"):
        return E.ReliabilityEnvelope(
            auth_state="anonymous",
            scores_available=None,
            provenance=None,
            warnings=list(rt.discovery.warnings()),
            error=E.ToolError(
                code="invalid_filter_value",
                message="detail must be standard or full",
                filter="detail",
            ),
            data=None,
        )
    served = await rt.repository.get_reliability(category, refresh=refresh)
    if isinstance(served, ToolError):
        return E.ReliabilityEnvelope(
            auth_state="anonymous",
            scores_available=None,
            provenance=None,
            warnings=list(rt.discovery.warnings()),
            error=E.error_model(served),
            data=None,
        )
    parsed = parse_reliability(served.payload, served.category_id, full=detail == "full")
    if not parsed["found"]:
        # the cached payload does not describe this category (a constructed URL that landed
        # elsewhere): our gap, never CR's absence
        return E.ReliabilityEnvelope(
            auth_state="anonymous",
            scores_available=None,
            provenance=None,
            warnings=list(rt.discovery.warnings()),
            error=E.ToolError(
                code="reliability_payload_missing",
                message=f"the reliability payload on hand does not describe c{served.category_id}",
                retryable=False,
            ),
            data=None,
        )
    row = rt.cache.index_row(served.category_id)
    ref = E.CategoryRef(
        id=served.category_id, slug=(row or {}).get("slug"), name=(row or {}).get("display_name")
    )
    brands = [
        (E.BrandSurveyFull if detail == "full" else E.BrandSurvey)(**b) for b in parsed["brands"]
    ]
    return E.ReliabilityEnvelope(
        auth_state="anonymous",
        scores_available=E.SurveyScoresAvailable(**survey_scores_available(parsed["brands"])),
        provenance=E.ReliabilityProvenance(
            fetched_at=served.fetched_at,
            cr_url=served.cr_url,
            from_cache=served.from_cache,
            stale=served.stale,
        ),
        warnings=list(served.warnings) + parsed["warnings"] + rt.discovery.warnings(),
        error=None,
        data=E.ReliabilityData(
            category=ref,
            brands=brands,
            methodology=E.Methodology(**parsed["methodology"])
            if include_methodology and parsed["methodology"]
            else None,
            has_reliability_data=parsed["has_reliability_data"],
            has_owner_satisfaction_data=parsed["has_owner_satisfaction_data"],
        ),
    )
