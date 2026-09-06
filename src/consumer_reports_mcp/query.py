"""The local filter engine, ordering, nesting and paging for `cr_ratings` (PLAN P7.1/P7.2).

Filtering is local: one fetch holds the whole category, so filters are operations over cached
data. Rank is never renumbered under a filter; sorting is on `_overallSortIndex` within a group in
both tiers, always; a cross-group score sort is honoured and labelled, never refused (SPEC §5/§7).
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

from . import envelope as E
from .attributes import BOOLEAN_KIND, NUMERIC_KINDS, Definition, coerce
from .config import FULL_LIMIT_MAX, FULL_LIMIT_NESTED, LIMIT_FLAT, LIMIT_MAX, LIMIT_NESTED
from .ingest import products_of
from .normalize import as_int, attribute_all_null, group_id_of, price_all_null

WITHIN_GROUP, CROSS_GROUP = "within_group", "cross_group"
SORT_KEYS = ("overallScore", "price")
ORDERS = ("asc", "desc")
CROSS_GROUP_NOTICE = (
    "sort.scope is cross_group: these products come from separately rated display groups, and "
    "Consumer Reports' Overall Scores are not comparable across groups. Each product still "
    "carries its group and its within-group rank."
)


class QueryError(Exception):
    """A structured filter error (SPEC §7 *Error taxonomy*)."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


@dataclass
class FilterSpec:
    group: Any = None
    brands: list[Any] | None = None
    price_min: float | None = None
    price_max: float | None = None
    recommended: bool | None = None
    features: dict[str, Any] | None = None

    @property
    def _set(self) -> dict[str, Any]:
        """The filters that actually CONSTRAIN the result, walked from the dataclass fields so
        `active` and `params` cannot drift apart. A seventh filter added to this class joins
        both at once; two hand-written lists would let `params` name a SUBSET of what the
        caller passed, i.e. a `no_results:` pointing at the wrong filter.

        An empty list or dict is NOT active: `features={}` passes validation and excludes
        nothing, so it must not make an empty result read as "your filters matched nothing".
        `price_min=0` and `recommended=False` are real constraints and stay active."""
        out: dict[str, Any] = {}
        for f in fields(self):
            v = getattr(self, f.name)
            if v is None or (isinstance(v, list | dict | tuple | set) and not v):
                continue
            out[f.name] = v
        return out

    @property
    def active(self) -> bool:
        return bool(self._set)

    @property
    def params(self) -> dict[str, Any]:
        """The active filters, keyed by `cr_ratings`' own parameter names. Rendering lives in
        `envelope.no_results` — the grammar is the response contract's, not the query engine's,
        and both surfaces share it."""
        return self._set


# --------------------------------------------------------------------------- helpers


def _filter(fi: dict, fid: str) -> dict | None:
    for f in fi.get("filters") or []:
        if isinstance(f, dict) and f.get("id") == fid:
            return f
    return None


def _norm(s: Any) -> str:
    return " ".join(str(s).strip().lower().split())


def groups_in_data(fi: dict) -> dict[int, str]:
    """Distinct `_groupId` → `_groupName` present in `data` — the nesting key (never subcats)."""
    out: dict[int, str] = {}
    for p in products_of(fi):
        gid = group_id_of(p)
        if gid is not None and gid not in out:
            out[gid] = str(p.get("_groupName") or gid)
    return out


def group_order(fi: dict) -> dict[int, float]:
    """`subcats.sortOrder` (else the `categories` filter order) → sort key per group id."""
    order: dict[int, float] = {}
    for s in (fi.get("args") or {}).get("subcats") or []:
        gid = as_int(s.get("id"))
        if gid is not None:
            so = s.get("sortOrder")
            order[gid] = float(so) if isinstance(so, int | float) else float("inf")
    cats = _filter(fi, "categories")
    for i, opt in enumerate((cats or {}).get("data") or []):
        gid = as_int(opt.get("id"))
        if gid is not None and gid not in order:
            so = opt.get("sortOrder")
            order[gid] = float(so) if isinstance(so, int | float) else float(i)
    return order


def is_multi_group(fi: dict) -> bool:
    return len(groups_in_data(fi)) > 1


# --------------------------------------------------------------------------- resolution


def resolve_group(fi: dict, value: Any) -> int:
    """A display-group id or name → `_groupId`. Options come from the `categories` filter and
    from the groups actually present in `data`."""
    known: dict[int, str] = dict(groups_in_data(fi))
    cats = _filter(fi, "categories")
    for opt in (cats or {}).get("data") or []:
        gid = as_int(opt.get("id"))
        if gid is not None and gid not in known:
            known[gid] = str(opt.get("label") or gid)
    gid = as_int(value)
    if gid is not None and gid in known:
        return gid
    wanted = _norm(value)
    hits = [g for g, name in known.items() if _norm(name) == wanted]
    if len(hits) == 1:
        return hits[0]
    legal = E.quoted_list(known.items(), lambda kv: f"{E.quoted(kv[1])} ({kv[0]})") or "none"
    raise QueryError(
        "invalid_filter_value",
        f"group {E.quoted(value)} is not a display group of this category; legal values: {legal}",
        filter="group",
    )


def resolve_brands(fi: dict, values: list[Any]) -> set[int]:
    """Brand names (case-insensitive, exact after trim) or ids → brand ids, OR within."""
    by_id: dict[int, str] = {}
    brands = _filter(fi, "brands")
    for opt in (brands or {}).get("data") or []:
        bid = as_int(opt.get("id"))
        if bid is not None:
            by_id[bid] = str(opt.get("label") or "")
    for p in products_of(fi):
        bid = as_int(p.get("brandId"))
        if bid is not None and bid not in by_id:
            by_id[bid] = str(p.get("brandName") or "")
    out: set[int] = set()
    unknown: list[Any] = []
    for v in values:
        bid = as_int(v)
        if bid is not None and bid in by_id:
            out.add(bid)
            continue
        wanted = _norm(v)
        hits = [b for b, name in by_id.items() if _norm(name) == wanted]
        if len(hits) == 1:
            out.add(hits[0])
        elif len(hits) > 1:
            raise QueryError(
                "ambiguous_filter_name",
                f"brand {E.quoted(v)} matches more than one brand id: "
                f"{E.quoted_list(sorted(hits))}",
                filter="brands",
                candidates=E.candidates(sorted(hits)),
            )
        else:
            unknown.append(v)
    if unknown:
        raise QueryError(
            "invalid_filter_value",
            f"unknown brand(s) {E.quoted_list(unknown)} for this category; call cr_filters for "
            "the legal list",
            filter="brands",
        )
    return out


def resolve_attribute(
    defs: dict[int, Definition], key: Any, *, filter_name: str = "features"
) -> Definition:
    """`attributeId`, `name` or `displayName` → definition. A string matching two different
    definitions is `ambiguous_filter_name` with both ids; name+displayName of ONE definition
    is not ambiguous (SPEC §7). `filter_name` names the parameter the key came from, so the
    error points at what the caller actually passed."""
    aid = as_int(key)
    if aid is not None and aid in defs:
        return defs[aid]
    wanted = _norm(key)
    hits = {
        d.id: d
        for d in defs.values()
        if (d.name and _norm(d.name) == wanted)
        or (d.display_name and _norm(d.display_name) == wanted)
    }
    if len(hits) == 1:
        return next(iter(hits.values()))
    if len(hits) > 1:
        raise QueryError(
            "ambiguous_filter_name",
            f"attribute {E.quoted(key)} matches more than one definition: "
            f"{E.quoted_list(sorted(hits))}",
            filter=filter_name,
            candidates=E.candidates(sorted(hits)),
        )
    raise QueryError(
        "unknown_filter",
        f"attribute {E.quoted(key)} is not defined for this category; call cr_filters for the list",
        filter=filter_name,
    )


# --------------------------------------------------------------------------- filtering


def _unavailable(name: str, data_tier: str) -> QueryError:
    reason = "unavailable" if data_tier == "anonymous" else "absent"
    hint = (
        "sign in to find out whether Consumer Reports publishes it"
        if reason == "unavailable"
        else "Consumer Reports publishes no value for it in this category"
    )
    return QueryError(
        "filter_on_unavailable_attribute",
        f"every value of {E.quoted(name)} is null in the served payload — {hint}",
        reason=reason,
        attribute=E.clipped(name),
    )


def _entry_value(p: dict, aid: int) -> Any:
    for e in p.get("attrs") or []:
        if as_int(e.get("attributeId")) == aid:
            return e.get("value")
    return None


def _bool_spec(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("yes", "true"):
        return True
    if isinstance(v, str) and v.strip().lower() in ("no", "false"):
        return False
    raise QueryError(
        "invalid_filter_value",
        f'a boolean attribute takes true/false (or "Yes"/"No"), not {E.quoted(v)}',
        filter="features",
    )


def _num_spec(v: Any) -> tuple[float | None, float | None] | float:
    if isinstance(v, bool):
        raise QueryError(
            "invalid_filter_value",
            f"a numeric attribute takes a number or [min, max], not {E.quoted(v)}",
            filter="features",
        )
    if isinstance(v, int | float):
        return float(v)
    if isinstance(v, list | tuple) and len(v) == 2:
        lo, hi = v
        for bound in (lo, hi):
            if bound is not None and (
                isinstance(bound, bool) or not isinstance(bound, int | float)
            ):
                raise QueryError(
                    "invalid_filter_value",
                    f"range bounds must be numbers or null, not {E.quoted(bound)}",
                    filter="features",
                )
        return (None if lo is None else float(lo), None if hi is None else float(hi))
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            pass
    raise QueryError(
        "invalid_filter_value",
        f"a numeric attribute takes a number or [min, max], not {E.quoted(v)}",
        filter="features",
    )


def _matches_feature(p: dict, d: Definition, spec: Any) -> bool:
    raw = _entry_value(p, d.id)
    value, ok = coerce(d.kind, raw)
    if not ok or value is None:
        return False
    if d.kind in NUMERIC_KINDS:
        want = _num_spec(spec)
        if isinstance(want, tuple):
            lo, hi = want
            return (lo is None or value >= lo) and (hi is None or value <= hi)
        return float(value) == want
    if d.kind == BOOLEAN_KIND:
        return value is _bool_spec(spec)
    return _norm(value) == _norm(spec)


def apply_filters(
    products: list[dict],
    spec: FilterSpec,
    defs: dict[int, Definition],
    fi: dict,
    data_tier: str,
) -> list[dict]:
    """AND across parameters. Errors are raised, never silently dropped (SPEC §7)."""
    out = list(products)
    if spec.group is not None:
        gid = resolve_group(fi, spec.group)
        out = [p for p in out if group_id_of(p) == gid]
    if spec.brands is not None:
        if not isinstance(spec.brands, list | tuple) or not spec.brands:
            raise QueryError(
                "invalid_filter_value", "brands takes a non-empty list", filter="brands"
            )
        ids = resolve_brands(fi, list(spec.brands))
        out = [p for p in out if as_int(p.get("brandId")) in ids]
    if spec.price_min is not None or spec.price_max is not None:
        # `all(...)` over zero products is vacuously true, so an EMPTY category used to answer
        # "every value of 'price' is null — sign in to find out whether CR publishes it". CR
        # ships nothing there and signing in cannot change that: the exact inversion of the
        # rule that a session limit must never read as absent data. `empty_category` owns this
        # case, and `recommended`/`group`/`brands` already fall through to it.
        if products_of(fi) and price_all_null(fi):
            raise _unavailable("price", data_tier)
        lo, hi = spec.price_min, spec.price_max
        kept = []
        for p in out:
            price = p.get("price")
            if not isinstance(price, int | float) or isinstance(price, bool):
                continue
            if (lo is None or price >= lo) and (hi is None or price <= hi):
                kept.append(p)
        out = kept
    if spec.recommended is not None:
        want = bool(spec.recommended)
        out = [p for p in out if (p.get("expertRatings") or {}).get("isRecommended") is want]
    if spec.features is not None:
        if not isinstance(spec.features, dict):
            raise QueryError(
                "invalid_filter_value",
                "features takes an object keyed by attribute",
                filter="features",
            )
        for key, value in spec.features.items():
            d = resolve_attribute(defs, key)
            if products_of(fi) and attribute_all_null(fi, d.id):
                raise _unavailable(d.name or str(d.id), data_tier)
            # validate the spec once, so a bad value is an error even on an empty result
            if d.kind in NUMERIC_KINDS:
                _num_spec(value)
            elif d.kind == BOOLEAN_KIND:
                _bool_spec(value)
            out = [p for p in out if _matches_feature(p, d, value)]
    return out


# --------------------------------------------------------------------------- ordering


@dataclass
class SortInfo:
    key: str
    order: str
    scope: str
    notice: str | None = field(default=None)

    def as_dict(self) -> dict:
        return {"key": self.key, "order": self.order, "scope": self.scope}


def _sort_index(p: dict) -> float | None:
    v = p.get("_overallSortIndex")
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _price(p: dict) -> float | None:
    v = p.get("price")
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _display_score(p: dict) -> float | None:
    v = p.get("overallDisplayScore")
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def order_products(
    products: list[dict],
    fi: dict,
    ranks: dict[int, int | None],
    *,
    sort: str | None,
    order: str,
    flat: bool,
) -> tuple[list[dict], SortInfo]:
    """SPEC §7 ordering. Returns the ordered list and the `sort` the caller actually got."""
    if sort is not None and sort not in SORT_KEYS:
        raise QueryError(
            "invalid_filter_value",
            f"sort must be one of {SORT_KEYS}, not {E.quoted(sort)}",
            filter="sort",
        )
    if order not in ORDERS:
        raise QueryError(
            "invalid_filter_value",
            f"order must be asc or desc, not {E.quoted(order)}",
            filter="order",
        )
    # "multi-group" is a property of the products being ORDERED: a group-filtered result is one
    # population and stays within_group on `_overallSortIndex` (SPEC §7). A product without a
    # `_groupId` is in no group, so it does not make a second one: a single-group category
    # carrying one must not be labelled `cross_group`.
    multi = len({group_id_of(p) for p in products} - {None}) > 1
    gorder = group_order(fi)

    def gkey(p: dict) -> tuple:
        # a product in no group sorts after every group — also when `group_order` is empty
        # (a single-group category), where every group shares the `inf` first key
        gid = group_id_of(p)
        return (gorder.get(gid, float("inf")), float("inf") if gid is None else gid)

    desc = order == "desc"
    if sort == "price":

        def pkey(p: dict) -> tuple:
            v = _price(p)
            return (
                v is None,
                (-v if desc else v) if v is not None else 0.0,
                gkey(p),
                _sort_index(p) or 0.0,
            )

        ordered = sorted(products, key=pkey)  # nulls last in both directions
        scope = CROSS_GROUP if (flat and multi) else WITHIN_GROUP
        return ordered, SortInfo("price", order, scope)

    explicit_cross = sort == "overallScore" and flat and multi
    if explicit_cross:
        # no shared index across groups: fall back to overallDisplayScore (member-only,
        # anonymously every value is null and the order degenerates to page order)
        def skey(p: dict) -> tuple:
            v = _display_score(p)
            return (
                v is None,
                (-v if desc else v) if v is not None else 0.0,
                gkey(p),
                _sort_index(p) or 0.0,
            )

        return sorted(products, key=skey), SortInfo(
            "overallScore", order, CROSS_GROUP, notice=CROSS_GROUP_NOTICE
        )

    # within group: `_overallSortIndex` ascending is CR's own best-first order in both tiers
    def wkey(p: dict) -> tuple:
        idx = _sort_index(p)
        return (
            gkey(p),
            idx is None,
            (-idx if not desc else idx) if idx is not None else 0.0,
            as_int(p.get("id")) or 0,
        )

    return sorted(products, key=wkey), SortInfo("overallScore", order, WITHIN_GROUP)


# --------------------------------------------------------------------------- nesting, paging


def group_sizes(fi: dict) -> dict[int | None, int]:
    """Products per group in CR's UNFILTERED table — `size` (SPEC §7). The `None` key counts
    products shipped without a `_groupId`, the ungrouped block's `size`."""
    sizes: dict[int | None, int] = {}
    for p in products_of(fi):
        gid = group_id_of(p)
        sizes[gid] = sizes.get(gid, 0) + 1
    return sizes


def ungrouped_count(fi: dict) -> int:
    """How many products the payload ships without a `_groupId` — `ungrouped_products:<n>`."""
    return sum(1 for p in products_of(fi) if group_id_of(p) is None)


def nest(products: list[dict], fi: dict) -> list[dict]:
    """Group an ordered product list by `_groupId`, in group order. Only groups present in
    `data` exist (RECON §10g); a group filtered to zero still appears with `total: 0`.

    Products without a `_groupId` go into ONE trailing block with `group_id: null` — present
    only when there are any. They used to vanish from nested mode (the default), which is
    withholding, and structure is this project's guard: `group: null` names the fact."""
    names = groups_in_data(fi)
    gorder = group_order(fi)
    buckets: dict[int, list[dict]] = {gid: [] for gid in names}
    loose: list[dict] = []
    for p in products:
        gid = group_id_of(p)
        if gid in buckets:
            buckets[gid].append(p)
        else:
            loose.append(p)
    ordered_ids = sorted(buckets, key=lambda g: (gorder.get(g, float("inf")), g))
    blocks = [
        {"group_id": gid, "group": names[gid], "products": buckets[gid]} for gid in ordered_ids
    ]
    if loose:
        blocks.append({"group_id": None, "group": None, "products": loose})
    return blocks


def validate_paging(limit: int | None, offset: int, *, detail: str) -> None:
    """The half of paging validation that needs no payload — so a bad `limit` costs a rejection
    rather than an 11 MB download (only the *default* limit depends on the group count)."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise QueryError(
            "invalid_filter_value",
            f"offset must be a non-negative integer, not {E.quoted(offset)}",
            filter="offset",
        )
    if limit is None:
        return
    cap = FULL_LIMIT_MAX if detail == "full" else LIMIT_MAX
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise QueryError(
            "invalid_filter_value",
            f"limit must be a positive integer, not {E.quoted(limit)}",
            filter="limit",
        )
    if limit > cap:
        raise QueryError(
            "invalid_filter_value",
            f"limit {limit} exceeds the cap of {cap} for detail={E.quoted(detail)}"
            + (
                " (full is capped at 10 — page, or use `attributes` to project fields)"
                if detail == "full"
                else ""
            ),
            filter="limit",
        )


def resolve_limit(limit: int | None, *, detail: str, nested: bool) -> int:
    default = LIMIT_NESTED if nested else LIMIT_FLAT
    cap = FULL_LIMIT_MAX if detail == "full" else LIMIT_MAX
    if detail == "full" and nested:
        default = FULL_LIMIT_NESTED
    if limit is None:
        return min(default, cap)
    validate_paging(limit, 0, detail=detail)
    return limit


def page(items: list[Any], limit: int, offset: int) -> tuple[list[Any], int, bool]:
    """(slice, total, truncated). `offset >= total` is an empty slice with the real total."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise QueryError(
            "invalid_filter_value",
            f"offset must be a non-negative integer, not {E.quoted(offset)}",
            filter="offset",
        )
    total = len(items)
    sliced = items[offset : offset + limit]
    return sliced, total, offset + limit < total
