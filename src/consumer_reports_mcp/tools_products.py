"""The six products tools as plain async functions over a `Runtime` (PLAN P7.4/P7.5). No mcp
import: every tool is testable with a fake transport and a tmp database (D4)."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode

from . import envelope as E
from . import lexical
from .attributes import NUMERIC_KINDS, Definition, build_definitions, coerce
from .cache import row_id
from .config import FILTER_VALUES_CAP, SEARCH_CAP, TYPEAHEAD_MIN_CHARS, TYPEAHEAD_URL
from .extract import clean_text
from .ingest import category_id_of, display_name_of, products_of
from .normalize import (
    DETAILS,
    product_shape,
    rank_table,
    scores_available,
    standard_attribute_ids,
)
from .query import (
    FilterSpec,
    QueryError,
    apply_filters,
    group_sizes,
    is_multi_group,
    nest,
    order_products,
    page,
    resolve_attribute,
    resolve_group,
    resolve_limit,
    ungrouped_count,
    validate_paging,
)
from .reliability import availability_for_product
from .repository import Served, ToolError
from .runtime import Runtime
from .transport import Challenged, FetchFailed

log = logging.getLogger(__name__)

GROUP_MODES = ("nested", "flat")


# --------------------------------------------------------------------------- shared


def _provenance(served: Served) -> E.Provenance:
    return E.Provenance(
        data_tier=served.data_tier,
        fetched_at=served.fetched_at,
        cr_url=served.cr_url,
        from_cache=served.from_cache,
        stale=served.stale,
        superseded_at=served.superseded_at,
    )


def _category_ref(rt: Runtime, envelope: dict) -> E.CategoryRef:
    cid = category_id_of(envelope)
    row = rt.cache.index_row(cid) if cid is not None else None
    slug = (row or {}).get("slug")
    name = (row or {}).get("display_name") or display_name_of(envelope)
    if slug is None:
        for f in envelope.get("family") or []:
            if f.get("id") == cid:
                slug = f.get("slug")
    return E.CategoryRef(id=cid or 0, slug=slug, name=name)


def _base_warnings(rt: Runtime) -> list[str]:
    """What every products envelope carries regardless of outcome: the discovery state and the
    credential's renewal state (`session_expiring:<days>`, SPEC §6)."""
    return rt.discovery.warnings() + rt.session_warnings()


def _envelope_warnings(served: Served) -> list[str]:
    out = list(served.warnings)
    fi = served.envelope.get("filter_instance") or {}
    if not products_of(fi):
        out.append("empty_category")
    if not served.envelope.get("category_attributes"):
        out.append("attribute_dictionary_missing")
    loose = ungrouped_count(fi)
    if loose:  # products in no display group: `group: null`, `rank: null`, nested last
        out.append(f"ungrouped_products:{loose}")
    return out


def _shape_model(shape: dict, detail: str) -> E.Product:
    if detail == "summary":
        return E.ProductSummary(**shape)
    if detail == "standard":
        return E.ProductStandard(**shape)
    return E.ProductFull(**shape)


def _ratings_error(rt: Runtime, err: Any, *, served: Served | None = None) -> E.RatingsEnvelope:
    """The `cr_ratings` error envelope. With a row in hand — a filter rejected AFTER the fetch —
    it describes that row like a success does: its tier in `auth_state`, its provenance, its
    session, and the row's own warnings (`empty_category`, `attribute_dictionary_missing` — the
    second is exactly what an `unknown_filter` on `features` is about). `auth_state` and
    `provenance` travel together or not at all (SPEC §7; `E.RowEnvelope` refuses the split):
    a caller who mistyped `sort` still learns the category was fetched, when, and from where.
    With no row both are null, and `session` alone says how the credential is."""
    return E.RatingsEnvelope(
        auth_state=E.auth_state(served.data_tier, served.session) if served is not None else None,
        session=served.session if served is not None else rt.health.reported,
        scores_available=None,
        provenance=_provenance(served) if served is not None else None,
        sort=None,
        warnings=(_envelope_warnings(served) if served is not None else []) + _base_warnings(rt),
        error=E.error_model(err),
        data=None,
    )


# --------------------------------------------------------------------------- cr_ratings


async def cr_ratings(
    rt: Runtime,
    category: str | int,
    group: str | int | None = None,
    brands: list[str | int] | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    recommended: bool | None = None,
    features: dict[str, Any] | None = None,
    sort: str | None = None,
    order: str = "desc",
    group_mode: str = "nested",
    attributes: list[str | int] | None = None,
    limit: int | None = None,
    offset: int = 0,
    detail: str = "standard",
    refresh: bool = False,
) -> E.RatingsEnvelope:
    if detail not in DETAILS:
        return _ratings_error(
            rt,
            QueryError(
                "invalid_filter_value",
                f"detail must be one of {DETAILS}",
                filter="detail",
                candidates=E.legal_values(DETAILS),
            ),
        )
    if group_mode not in GROUP_MODES:
        return _ratings_error(
            rt,
            QueryError(
                "invalid_filter_value",
                "group_mode must be nested or flat",
                filter="group_mode",
                candidates=E.legal_values(GROUP_MODES),
            ),
        )
    try:  # paging is knowable without the payload; rejecting after the fetch costs 11 MB
        validate_paging(limit, offset, detail=detail)
    except QueryError as exc:
        return _ratings_error(rt, exc)
    served = await rt.repository.get_category(category, refresh=refresh)
    if isinstance(served, ToolError):
        return _ratings_error(rt, served)
    env = served.envelope
    fi = env["filter_instance"]
    defs = build_definitions(env)
    ranks = rank_table(fi)
    std_ids = standard_attribute_ids(env, defs)
    warnings = _envelope_warnings(served)
    session = served.session
    state = E.auth_state(served.data_tier, session)
    scores = scores_available(fi, served.data_tier)
    spec = FilterSpec(
        group=group,
        brands=brands,
        price_min=price_min,
        price_max=price_max,
        recommended=recommended,
        features=features,
    )
    try:
        extra_ids = tuple(
            resolve_attribute(defs, k, filter_name="attributes").id for k in (attributes or [])
        )
        all_products = products_of(fi)
        filtered = apply_filters(all_products, spec, defs, fi, served.data_tier)
        # Filters that exclude everything are structurally distinct from a category CR ships
        # empty: `empty_category` already carries the latter, so an agent given a bare `[]`
        # otherwise cannot tell "your filter was too narrow" from "CR rates nothing here".
        # The two are mutually exclusive, and `all_products` is the ONE predicate behind both
        # (`_envelope_warnings` reads the same emptiness) — re-derived, they could diverge and
        # a response could carry both. The guard is load-bearing, not defensive: `recommended`,
        # `group` and `brands` all resolve fine against an empty payload and would otherwise
        # emit `no_results` alongside `empty_category`.
        if spec.active and not filtered and all_products:
            warnings.append(E.no_results(spec.params))
        multi = is_multi_group(fi)
        nested = group_mode == "nested" and multi and group is None
        lim = resolve_limit(limit, detail=detail, nested=nested)
        ordered, sort_info = order_products(
            filtered, fi, ranks, sort=sort, order=order, flat=not nested
        )
        session_notice = E.maybe_notice(
            scores, state, has_products=bool(filtered), session=served.session
        )
        notice_parts = [n for n in (session_notice, sort_info.notice) if n]
        ref = _category_ref(rt, env)
        sizes = group_sizes(fi)

        def shape(p: dict) -> E.Product:
            s = product_shape(
                p,
                detail,
                env,
                defs,
                ranks,
                standard_ids=std_ids,
                extra_attribute_ids=extra_ids,
                warnings=warnings,
            )
            return _shape_model(s, detail)

        if nested:
            blocks = []
            for g in nest(ordered, fi):
                sliced, total, truncated = page(g["products"], lim, offset)
                blocks.append(
                    E.GroupBlock(
                        group=g["group"],
                        group_id=g["group_id"],
                        size=sizes.get(g["group_id"], 0),
                        total=total,
                        truncated=truncated,
                        products=[shape(p) for p in sliced],
                    )
                )
            data: E.NestedRatings | E.FlatRatings = E.NestedRatings(
                category=ref,
                detail=detail,
                group_mode="nested",
                groups=blocks,
                notice=" ".join(notice_parts) or None,
            )
        else:
            sliced, total, truncated = page(ordered, lim, offset)
            if group is not None:  # `size` is CR's unfiltered group table, even on an empty hit
                size = sizes.get(resolve_group(fi, group), 0)
            else:
                size = len(all_products)
            data = E.FlatRatings(
                category=ref,
                detail=detail,
                group_mode="flat",
                products=[shape(p) for p in sliced],
                size=size,
                total=total,
                truncated=truncated,
                notice=" ".join(notice_parts) or None,
            )
    except QueryError as qe:
        return _ratings_error(rt, qe, served=served)
    return E.RatingsEnvelope(
        auth_state=state,
        session=session,
        scores_available=E.ScoresAvailable(**scores),
        provenance=_provenance(served),
        sort=E.SortInfo(**sort_info.as_dict()),
        warnings=warnings + _base_warnings(rt),
        error=None,
        data=data,
    )


# --------------------------------------------------------------------------- cr_filters


PARAMETER_MAP = {
    "type": None,
    "sort": None,
    "custom": "recommended",
    "categories": "group",
    "brands": "brands",
    "price": "price_min/price_max",
    "features": "features",
}


def _numeric_range(values: list[Any]) -> E.NumericRange | None:
    nums = [float(v) for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
    if not nums:
        return None
    return E.NumericRange(min=min(nums), max=max(nums))


def _feature_block(d: Definition, products: list[dict], shipped: list[Any]) -> E.FeatureFilter:
    observed: list[Any] = []
    for p in products:
        for e in p.get("attrs") or []:
            if e.get("attributeId") == d.id and e.get("value") is not None:
                v, ok = coerce(d.kind, e.get("value"))
                if ok and v is not None:
                    observed.append(v)
    pool = observed or [coerce(d.kind, v)[0] for v in shipped]
    if d.kind in NUMERIC_KINDS:
        return E.FeatureFilter(
            id=d.id,
            name=d.name,
            display_name=d.display_name,
            kind=d.kind,
            unit=d.unit,
            description=d.description,
            group=d.group,
            range=_numeric_range(pool),
            values=None,
            values_truncated=False,
        )
    distinct: list[Any] = []
    for v in pool:
        if v is not None and v not in distinct:
            distinct.append(v)
    truncated = len(distinct) > FILTER_VALUES_CAP
    return E.FeatureFilter(
        id=d.id,
        name=d.name,
        display_name=d.display_name,
        kind=d.kind,
        unit=d.unit,
        description=d.description,
        group=d.group,
        range=None,
        values=distinct[:FILTER_VALUES_CAP],
        values_truncated=truncated,
    )


async def cr_filters(rt: Runtime, category: str | int, refresh: bool = False) -> E.FiltersEnvelope:
    served = await rt.repository.get_category(category, refresh=refresh)
    if isinstance(served, ToolError):
        return E.FiltersEnvelope(
            auth_state=None,  # no row was served (SPEC §7)
            session=rt.health.reported,
            scores_available=None,
            provenance=None,
            warnings=list(_base_warnings(rt)),
            error=E.error_model(served),
            data=None,
        )
    env = served.envelope
    fi = env["filter_instance"]
    products = products_of(fi)
    defs = build_definitions(env)
    blocks: list[E.FilterBlock] = []
    groups: list[E.FilterOption] = []
    for f in fi.get("filters") or []:
        fid = str(f.get("id"))
        parameter = PARAMETER_MAP.get(fid)
        data = f.get("data") or []
        options: list[E.FilterOption] | None = None
        rng: E.NumericRange | None = None
        feats: list[E.FeatureFilter] | None = None
        truncated = False
        if fid == "price":
            prices = [p.get("price") for p in products]
            rng = _numeric_range(prices) or _numeric_range([o.get("label") for o in data])
        elif fid == "features":
            feats = []
            emitted: set[int] = set()
            for opt in data:
                aid = opt.get("id")
                d = defs.get(aid) if isinstance(aid, int) else None
                if d is None and isinstance(aid, int):
                    d = Definition(
                        id=aid,
                        name=opt.get("name"),
                        display_name=clean_text(opt.get("label")),
                        kind=opt.get("dataType"),
                        unit=clean_text(opt.get("unitName")),
                        description=clean_text(opt.get("description")),
                        group=opt.get("attributeGroup"),
                        sort_order=None,
                        source="attrs",
                    )
                if d is not None and d.id not in emitted:
                    emitted.add(d.id)
                    feats.append(_feature_block(d, products, opt.get("data") or []))
            # every definition a product actually carries — this tool is the ONLY home for
            # descriptions and units, and `features=` resolves against them (SPEC §7);
            # archived definitions no product ships are noise and are left out
            carried = {e.get("attributeId") for p in products for e in p.get("attrs") or []}
            for d in sorted(
                defs.values(),
                key=lambda d: (d.group or "~", d.sort_order is None, d.sort_order or 0.0, d.id),
            ):
                if d.id not in emitted and d.id in carried:
                    emitted.add(d.id)
                    feats.append(_feature_block(d, products, []))
        elif fid == "custom":
            options = [
                E.FilterOption(id=o.get("id"), label=clean_text(o.get("label"))) for o in data
            ]
        else:
            # brands / categories / type: the complete legal list, never capped (the 12-value
            # cap is for attribute VALUE lists, SPEC §7)
            options = [
                E.FilterOption(id=o.get("id"), label=clean_text(o.get("label"))) for o in data
            ]
            if fid == "categories":
                groups = [
                    E.FilterOption(id=o.get("id"), label=clean_text(o.get("label"))) for o in data
                ]
        blocks.append(
            E.FilterBlock(
                id=fid,
                label=clean_text(f.get("label")),
                type=f.get("type"),
                parameter=parameter,
                options=options,
                range=rng,
                features=feats,
                values_truncated=truncated,
            )
        )
    std = [
        E.FilterOption(id=aid, label=defs[aid].name if aid in defs else None)
        for aid in standard_attribute_ids(env, defs)
    ]
    state = E.auth_state(served.data_tier, served.session)
    return E.FiltersEnvelope(
        auth_state=state,
        session=served.session,
        scores_available=E.ScoresAvailable(**scores_available(fi, served.data_tier)),
        provenance=_provenance(served),
        warnings=_envelope_warnings(served) + _base_warnings(rt),
        error=None,
        data=E.FiltersData(
            category=_category_ref(rt, env), filters=blocks, groups=groups, standard_ratings=std
        ),
    )


# --------------------------------------------------------------------------- cr_product


async def cr_product(
    rt: Runtime, id: int, include_descriptions: bool = False, refresh: bool = False
) -> E.ProductEnvelope:
    pid = row_id(id)
    served: Served | ToolError
    if pid is None:  # not a key any row can carry: answered without a lookup or a request
        served = ToolError(
            "unknown_product",
            f"{E.quoted(id)} is not a product id; ids come from cr_ratings or cr_search",
            retryable=False,
        )
    else:
        served = await rt.repository.get_product(pid, refresh=refresh)
    if isinstance(served, ToolError):
        return E.ProductEnvelope(
            auth_state=None,  # no row was served (SPEC §7)
            session=rt.health.reported,
            scores_available=None,
            provenance=None,
            warnings=list(_base_warnings(rt)),
            error=E.error_model(served),
            data=None,
        )
    env = served.envelope
    fi = env["filter_instance"]
    product = next((p for p in products_of(fi) if p.get("id") == pid), None)
    if product is None:
        return E.ProductEnvelope(
            auth_state=E.auth_state(served.data_tier, served.session),
            session=served.session,
            scores_available=None,
            provenance=_provenance(served),
            warnings=_envelope_warnings(served) + _base_warnings(rt),
            error=E.ToolError(
                code="unknown_product",
                message=f"product {pid} is not in the served payload of c{served.category_id}; "
                "run cr_ratings on its category or cr_search first",
                retryable=False,
            ),
            data=None,
        )
    defs = build_definitions(env)
    ranks = rank_table(fi)
    warnings = _envelope_warnings(served)
    shape = product_shape(
        product,
        "full",
        env,
        defs,
        ranks,
        standard_ids=standard_attribute_ids(env, defs),
        warnings=warnings,
        include_descriptions=include_descriptions,
    )
    availability, avail_warning = availability_for_product(rt, served.category_id, pid)
    if avail_warning:
        warnings.append(avail_warning)
    state = E.auth_state(served.data_tier, served.session)
    scores = scores_available(fi, served.data_tier)
    return E.ProductEnvelope(
        auth_state=state,
        session=served.session,
        scores_available=E.ScoresAvailable(**scores),
        provenance=_provenance(served),
        warnings=warnings + _base_warnings(rt),
        error=None,
        data=E.ProductData(
            category=_category_ref(rt, env),
            product=E.ProductFull(**shape),
            availability=availability,
            notice=E.maybe_notice(scores, state, session=served.session),
        ),
    )


# --------------------------------------------------------------------------- cr_categories


def _index_provenance(rt: Runtime, fetched_now: bool) -> E.IndexProvenance:
    prov = rt.discovery.index_provenance()
    return E.IndexProvenance(
        fetched_at=prov["fetched_at"], cr_url=prov["cr_url"], from_cache=not fetched_now
    )


def _lean(row: dict) -> E.CategoryLean:
    return E.CategoryLean(
        id=f"c{row['category_id']}",
        slug=row.get("slug"),
        name=row.get("display_name"),
        franchise=row.get("franchise"),
        score_range_status=row.get("score_range_status") or "not_fetched",
    )


def _enriched(row: dict) -> E.CategoryEnriched:
    status = row.get("score_range_status") or "not_fetched"
    fetched = status != "not_fetched"
    family = (
        E.FamilyRef(id=row["family_id"], name=row.get("family_name"))
        if fetched and row.get("family_id") is not None
        else None
    )
    rng = (
        E.NumericRange(min=row.get("score_min"), max=row.get("score_max"))
        if status == "known"
        else None
    )
    # gated like `family`: under `not_fetched` an empty list would be a positive claim that CR
    # ships no groups — what a single-group category looks like — when nothing was looked at
    groups = row.get("groups") if fetched else None
    return E.CategoryEnriched(
        id=f"c{row['category_id']}",
        slug=row.get("slug"),
        name=row.get("display_name"),
        franchise=row.get("franchise"),
        score_range_status=status,
        family=family,
        score_range=rng,
        rated_count=row.get("rated_count") if fetched else None,
        groups=[E.GroupRef(id=g["id"], name=g["name"]) for g in groups]
        if groups is not None
        else None,
    )


async def cr_categories(
    rt: Runtime,
    franchise: str | None = None,
    family: int | str | None = None,
    refresh: bool = False,
) -> E.CategoriesEnvelope:
    fetched = await rt.discovery.ensure_az_index(refresh=refresh)
    if refresh:
        # a sitemap pass that ended unrecorded is retried here too (never a fresh one — that
        # is 195 fetches; `force` is not a refresh), in the background: the answer below still
        # says `sitemap_pass_pending` until it is recorded
        rt.discovery.start_background_sitemap_pass()
    if not rt.discovery.az_done():
        failure = rt.discovery.az_failure or {"code": "fetch_failed"}
        return E.CategoriesEnvelope(
            session=rt.health.reported,
            provenance=None,
            warnings=list(_base_warnings(rt)),
            error=E.ToolError(
                code=failure.get("code", "fetch_failed"),
                message="the category index could not be fetched",
                reason=failure.get("reason"),
                retryable=failure.get("retryable"),
                http_status=failure.get("http_status"),
                title=failure.get("title"),
            ),
            data=None,
        )
    fam: int | None = None
    if family is not None:
        known = rt.cache.known_families()
        try:
            fam = int(str(family).lstrip("cC"))
        except ValueError:
            return E.CategoriesEnvelope(
                session=rt.health.reported,
                provenance=_index_provenance(rt, fetched),
                warnings=list(_base_warnings(rt)),
                error=E.ToolError(
                    code="invalid_filter_value",
                    message=f"family must be a supercategory id, not {E.quoted(family)}",
                    filter="family",
                    candidates=E.candidates(known),
                ),
                data=None,
            )
        if fam not in {f["id"] for f in known}:
            # a member category id is the likely mistake, and an empty list would read as
            # "this family holds nothing" rather than "that is not a family"
            legal = E.quoted_list(known, lambda f: f"{f['id']} ({E.quoted(f['name'])})")
            return E.CategoriesEnvelope(
                session=rt.health.reported,
                provenance=_index_provenance(rt, fetched),
                warnings=list(_base_warnings(rt)),
                error=E.ToolError(
                    code="invalid_filter_value",
                    message=(
                        f"{E.quoted(family)} is not a known supercategory id; legal: {legal}"
                        if known
                        else f"{E.quoted(family)} is not a known supercategory id, and no "
                        "families are "
                        "known yet — a family is learned when a category in it is fetched, so "
                        "call cr_ratings or cr_filters on one category first"
                    ),
                    filter="family",
                    candidates=E.candidates(known),
                ),
                data=None,
            )
    rows = rt.cache.list_categories(franchise=franchise, family=fam)
    scoped = franchise is not None or fam is not None
    items = [_enriched(r) if scoped else _lean(r) for r in rows]
    return E.CategoriesEnvelope(
        session=rt.health.reported,
        provenance=_index_provenance(rt, fetched),
        warnings=list(_base_warnings(rt)),
        error=None,
        data=E.CategoriesData(
            categories=items, total=len(items), scoped=scoped, discovery=rt.discovery.status()
        ),
    )


# --------------------------------------------------------------------------- cr_search


def _typeahead_hits(payload: Any) -> list[dict]:
    """CR's typeahead: `[{id, label, type, links:{ratings: '/…/cNNNN/'}}]` (RECON §14)."""
    from .discovery import _CAT_PATH

    hits = []
    if not isinstance(payload, list):
        return hits
    for item in payload:
        if not isinstance(item, dict):
            continue
        links = item.get("links") if isinstance(item.get("links"), dict) else {}
        ratings = links.get("ratings") or links.get("recommended")
        m = _CAT_PATH.match(str(ratings)) if ratings else None
        if not m:
            continue
        hits.append({"id": int(m.group(2)), "path": m.group(1), "label": item.get("label")})
    return hits


async def cr_search(rt: Runtime, query: str, refresh: bool = False) -> E.SearchEnvelope:
    q = (query or "").strip()
    warnings: list[str] = []
    if (bad := E.bad_query(q)) is not None:
        return E.SearchEnvelope(
            session=rt.health.reported,
            provenance=None,
            warnings=list(_base_warnings(rt)),
            error=bad,
            data=None,
        )
    fetched = await rt.discovery.ensure_az_index(refresh=refresh)
    # (match.key, source rank, source order, hit): ranked once across BOTH sources, so a
    # better lexical fit wins whichever produced it, and CR's typeahead order decides ties —
    # in source order, 'pressure cookers' answered Pressure Washers first (SPEC §7)
    ranked: list[tuple[tuple, int, int, E.SearchCategoryHit]] = []
    seen: set[int] = set()
    q_tokens = lexical.tokens(q)
    if len(q) >= TYPEAHEAD_MIN_CHARS:
        try:
            result = await rt.transport.fetch(
                f"{TYPEAHEAD_URL}?{urlencode({'query': q})}",
                headers={"accept": "application/json"},
            )
            payload = json.loads(result.content)
        except (FetchFailed, Challenged, ValueError) as exc:
            warnings.append("typeahead_unavailable")
            log.info("typeahead unavailable: %s", type(exc).__name__)
        else:
            from .discovery import franchise_of, slug_of

            for hit in _typeahead_hits(payload):
                if hit["id"] in seen:
                    continue
                seen.add(hit["id"])
                row = rt.cache.index_row(hit["id"])
                slug = (row or {}).get("slug") or slug_of(hit["path"])
                name = (row or {}).get("display_name") or hit.get("label")
                # CR's label is CR's own synonym for the category ("televisions" for TVs,
                # "washing machines" for Front-load washers): it counts as one of its texts
                m = lexical.match(q_tokens, [name, slug, hit.get("label")])
                ranked.append(
                    (
                        m.key,
                        0,
                        len(ranked),
                        E.SearchCategoryHit(
                            kind="category",
                            id=f"c{hit['id']}",
                            slug=slug,
                            name=name,
                            franchise=(row or {}).get("franchise") or franchise_of(hit["path"]),
                            source="typeahead",
                            match=m.kind,
                        ),
                    )
                )
    for row in rt.cache.search_categories(q, limit=SEARCH_CAP):
        if row["category_id"] in seen:
            continue
        seen.add(row["category_id"])
        m = lexical.match(q_tokens, [row.get("display_name"), row.get("slug")])
        ranked.append(
            (
                m.key,
                1,
                len(ranked),
                E.SearchCategoryHit(
                    kind="category",
                    id=f"c{row['category_id']}",
                    slug=row.get("slug"),
                    name=row.get("display_name"),
                    franchise=row.get("franchise"),
                    source="index",
                    match=m.kind,
                ),
            )
        )
    ranked.sort(key=lambda r: r[:3])
    cats = [r[3] for r in ranked]
    products = [
        E.SearchProductHit(
            kind="product",
            id=h["id"],
            brand=h["brand"],
            model=h["model"],
            category_id=f"c{h['category_id']}",
            category_name=h.get("category_name"),
            data_tier=h.get("data_tier"),
            fetched_at=h.get("fetched_at"),
        )
        for h in rt.cache.search_products(q, limit=SEARCH_CAP)
    ]
    searched = [
        E.CategoryRef(id=c["id"], slug=c["slug"], name=c["name"])
        for c in rt.cache.cached_category_ids()
    ]
    return E.SearchEnvelope(
        session=rt.health.reported,
        provenance=_index_provenance(rt, fetched),
        warnings=warnings + _base_warnings(rt),
        error=None,
        data=E.SearchData(
            query=q, categories=cats[:SEARCH_CAP], products=products, searched_categories=searched
        ),
    )
