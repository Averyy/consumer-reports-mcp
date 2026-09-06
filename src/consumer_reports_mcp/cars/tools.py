"""`cr_car_search`, `cr_cars`, `cr_car` (PLAN P10.2–P10.4, SPEC §7).

Cars have exactly one tier, so these envelopes carry no `auth_state`/`data_tier`; `session`
stays because "your cookie is dead" is worth knowing on any call.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .. import envelope as E
from ..cache import row_id
from ..config import (
    CARS_API,
    CARS_LIMIT_STANDARD,
    CARS_LIMIT_STANDARD_MAX,
    CARS_LIMIT_SUMMARY,
    CARS_LIMIT_SUMMARY_MAX,
    SEARCH_CAP,
)
from ..transport import Challenged, FetchFailed, failure_code, failure_fields
from .api import failure_reason
from .index import ensure_car_index, index_is_stale
from .normalize import (
    STANDARD_SCORE_KEYS,
    SUMMARY_SCORE_KEYS,
    car_scores_available,
    car_shape,
    listing_shape,
)
from .repository import (
    CarsQueryError,
    build_listing_params,
    get_model_year,
    list_cars,
)

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger(__name__)

CARS_DETAILS = ("summary", "standard")  # `cr_cars`: road-test scores cost a request per row
CAR_DETAILS = ("standard", "full")  # `cr_car`


def _transport_error(exc: FetchFailed | Challenged) -> E.ToolError:
    """Nothing cached, so the failure IS the answer: the products path's `_fallback` shape,
    from the same projection (`transport.failure_fields`) rather than re-spelled here."""
    message = (
        "the request was challenged or blocked by CR's WAF"
        if isinstance(exc, Challenged)
        else str(exc)
    )
    return E.ToolError(code=failure_code(exc), message=message, **failure_fields(exc))


def _session(rt: Runtime) -> str:
    return rt.health.health.value


def _unknown_car(rt: Runtime, model_year_id: object) -> E.CarEnvelope:
    return E.CarEnvelope(
        session=_session(rt),
        scores_available=None,
        provenance=None,
        warnings=rt.session_warnings(),
        error=E.ToolError(
            code="unknown_car",
            message=f"model-year {E.quoted(model_year_id)} is not known to Consumer Reports' "
            "cars API; find an id with cr_car_search",
            retryable=False,
        ),
        data=None,
    )


def _warn(warnings: list[str], warning: str) -> None:
    if warning not in warnings:
        warnings.append(warning)


# --------------------------------------------------------------------------- cr_car_search


async def cr_car_search(rt: Runtime, query: str, refresh: bool = False) -> E.CarSearchEnvelope:
    q = (query or "").strip()
    if (bad := E.bad_query(q)) is not None:
        return E.CarSearchEnvelope(
            session=_session(rt),
            provenance=None,
            warnings=rt.session_warnings(),
            error=bad,
            data=None,
        )
    warnings: list[str] = []
    try:
        fetched, fetched_at, refresh_failed = await ensure_car_index(rt, refresh=refresh)
    except (FetchFailed, Challenged) as exc:  # nothing cached: the convention raised
        return E.CarSearchEnvelope(
            session=_session(rt),
            provenance=None,
            warnings=rt.session_warnings(),
            error=_transport_error(exc),
            data=None,
        )
    if refresh_failed is not None:
        warnings.append(f"index_refresh_failed:{refresh_failed}")
    hits = rt.cache.search_cars(q, limit=SEARCH_CAP)
    return E.CarSearchEnvelope(
        session=_session(rt),
        provenance=E.CarProvenance(
            fetched_at=fetched_at,
            cr_url=f"{CARS_API}/v1/cr/keys",
            from_cache=not fetched,
            stale=index_is_stale(rt),
        ),
        warnings=warnings + rt.session_warnings(),
        error=None,
        data=E.CarSearchData(
            query=q,
            cars=[
                E.CarSearchHit(
                    model_year_id=h["model_year_id"],
                    make=h["make"],
                    model=h["model"],
                    year=h["year"],
                    states=h["states"],
                    car_types=h["car_types"],
                )
                for h in hits
            ],
        ),
    )


# --------------------------------------------------------------------------- cr_cars


def _resolve_cars_limit(limit: int | None, detail: str) -> int:
    default = CARS_LIMIT_STANDARD if detail == "standard" else CARS_LIMIT_SUMMARY
    cap = CARS_LIMIT_STANDARD_MAX if detail == "standard" else CARS_LIMIT_SUMMARY_MAX
    if limit is None:
        return default
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise CarsQueryError(
            "invalid_filter_value",
            f"limit must be a positive integer, not {E.quoted(limit)}",
            filter="limit",
            candidates=E.legal_range(1, cap),
        )
    if limit > cap:
        raise CarsQueryError(
            "invalid_filter_value",
            f"limit {limit} exceeds the cap of {cap} for detail={E.quoted(detail)}"
            + (" — road-test scores cost one request per car" if detail == "standard" else ""),
            filter="limit",
            candidates=E.legal_range(1, cap),
        )
    return limit


def _cars_error(rt: Runtime, err: E.ToolError) -> E.CarsEnvelope:
    return E.CarsEnvelope(
        session=_session(rt),
        scores_available=None,
        provenance=None,
        warnings=rt.session_warnings(),
        error=err,
        data=None,
    )


async def cr_cars(
    rt: Runtime,
    car_type: str | None = None,
    category: int | None = None,
    make: str | None = None,
    year: int | None = None,
    state: str | None = None,
    detail: str = "summary",
    limit: int | None = None,
    offset: int = 0,
    refresh: bool = False,
) -> E.CarsEnvelope:
    if detail not in CARS_DETAILS:
        return _cars_error(
            rt,
            E.ToolError(
                code="invalid_filter_value",
                message="detail must be summary or standard",
                filter="detail",
                candidates=E.legal_values(CARS_DETAILS),
            ),
        )
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return _cars_error(
            rt,
            E.ToolError(
                code="invalid_filter_value",
                message="offset must be a non-negative integer",
                filter="offset",
                candidates=E.legal_range(0, None),
            ),
        )
    warnings: list[str] = []
    # The TOOL's filters, by the TOOL's names — the `no_results` vocabulary, never
    # `build_listing_params`' API translation: a caller who passed `state="used"` cannot act
    # on `modelYearStateId=1`, a key it never typed whose mapping (used=1, new=2) reads
    # backwards. `limit`/`offset`/`detail` are paging, not filters: an `offset` past the end
    # is an empty PAGE of rows the filters did match, never "your filters matched nothing".
    filters = {
        k: v
        for k, v in (
            ("car_type", car_type),
            ("category", category),
            ("make", make),
            ("year", year),
            ("state", state),
        )
        if v is not None
    }
    try:
        lim = _resolve_cars_limit(limit, detail)
        # refuses an UNFILTERED listing (`invalid_filter_value`, no request) — CR's own `400`
        # requires one of `car_type`/`make`/`state` (RECON §13c-ii) — so past this line at
        # least one of those is in `filters`, and an unfiltered empty listing is not a state
        # this tool can reach
        params = await build_listing_params(
            rt, car_type=car_type, category=category, make=make, state=state, year=year
        )
        # a `400` with `modelYear` sent is CR rejecting `year` as a value — `list_cars` raises
        # it as the same `CarsQueryError` the index check does (`year_rejected`), so the
        # tool sees a filter error, never `fetch_failed(bad_request)` for a 1990 Honda
        listing = await list_cars(rt, params, need=offset + lim)
    except CarsQueryError as qe:
        return _cars_error(rt, E.error_model(qe))
    except (FetchFailed, Challenged) as exc:
        return _cars_error(rt, _transport_error(exc))
    # `offset` pages over CR's own order — years descending within a model, nothing else moved
    # (`stabilise_page`); a cross-model sort of a fetched PREFIX is not consistent across offsets
    entries, total, requests = listing.entries, listing.total, listing.requests
    page = entries[offset : offset + lim]
    # with no `counts` and pages remaining the population is unknown and rows certainly remain
    truncated = True if total is None else offset + lim < total
    rows = [listing_shape(e) for e in page]
    if not entries and filters:
        # Same renderer as `cr_ratings`, gated the same way (`FilterSpec.active`): the token
        # names the filters that excluded everything, so an empty filter dict would render the
        # bare `no_results:` — which the registry rejects at envelope construction, i.e. a
        # crash. The gate holds the renderer's contract at the emission site rather than by
        # the distance to `build_listing_params`' refusal above.
        warnings.append(E.no_results(filters))
    keys: tuple[str, ...] = SUMMARY_SCORE_KEYS
    rated = 0
    if detail == "standard":
        keys = SUMMARY_SCORE_KEYS + STANDARD_SCORE_KEYS
        for row in rows:  # one request per RETURNED row, cached per id
            myid = row["model_year_id"]
            if myid is None:
                continue
            try:
                served = await get_model_year(rt, myid, refresh=refresh)
            except (FetchFailed, Challenged) as exc:
                warnings.append(f"ratings_unavailable:{myid}:{failure_reason(exc)}")
                continue
            requests += served.requests
            rated += 1
            if served.refresh_failed is not None:
                _warn(warnings, f"refresh_failed:{served.refresh_failed}")
            full = car_shape(served.payload, "standard", warnings)
            for key in (
                "overall_score",
                "road_test_score",
                "rank",
                "score_range",
                "sort_index",
                "recommended",
                "predicted_reliability",
                "predicted_reliability_rating",
                "owner_satisfaction",
                "owner_satisfaction_percent",
                "car_type",
                "category",
                "price",
            ):
                row[key] = full.get(key)
            row["ratings_fetched_at"] = served.fetched_at
            row["ratings_from_cache"] = served.from_cache
    scores = car_scores_available(rows, keys)
    if not rows:
        # zero rows: `any()` over nothing is False, so every key read `absent` — "CR published
        # no value" derived from a page the caller's filters (or `offset`) emptied. Nothing was
        # looked at, and null is the contract's word for that (SPEC §5 *Cars*)
        for key in keys:
            scores[key] = None
    elif detail == "standard" and rated == 0:
        # no row's ratings could be fetched: "not reported", never "CR published no value"
        for key in STANDARD_SCORE_KEYS:
            scores[key] = None
    return E.CarsEnvelope(
        session=_session(rt),
        scores_available=E.CarScoresAvailable(**scores),
        provenance=E.CarProvenance(
            fetched_at=_now_iso(rt),
            cr_url=listing.url,
            from_cache=False,
            stale=False,
        ),
        warnings=warnings + rt.session_warnings(),
        error=None,
        data=E.CarsData(
            cars=rows, detail=detail, total=total, truncated=truncated, requests_made=requests
        ),
    )


def _now_iso(rt: Runtime) -> str:
    from ..cache import to_iso

    return to_iso(rt.clock())


# --------------------------------------------------------------------------- cr_car


async def cr_car(
    rt: Runtime, model_year_id: int, detail: str = "standard", refresh: bool = False
) -> E.CarEnvelope:
    if detail not in CAR_DETAILS:
        return E.CarEnvelope(
            session=_session(rt),
            scores_available=None,
            provenance=None,
            warnings=rt.session_warnings(),
            error=E.ToolError(
                code="invalid_filter_value",
                message="detail must be standard or full",
                filter="detail",
                candidates=E.legal_values(CAR_DETAILS),
            ),
            data=None,
        )
    myid = row_id(model_year_id)
    if myid is None:  # not a key any row can carry: answered without a lookup or a request
        return _unknown_car(rt, model_year_id)
    try:
        served = await get_model_year(rt, myid, refresh=refresh)
    except (FetchFailed, Challenged) as exc:
        # 403/404, or an empty 200 — a 429 or 400 is never an unknown car
        if isinstance(exc, FetchFailed) and exc.reason == "no_such_route":
            return _unknown_car(rt, model_year_id)
        return E.CarEnvelope(
            session=_session(rt),
            scores_available=None,
            provenance=None,
            warnings=rt.session_warnings(),
            error=_transport_error(exc),
            data=None,
        )
    warnings: list[str] = []
    if served.refresh_failed is not None:  # a cached row answered for a refetch that failed
        warnings.append(f"refresh_failed:{served.refresh_failed}")
    shape = car_shape(served.payload, detail, warnings)
    keys = SUMMARY_SCORE_KEYS + STANDARD_SCORE_KEYS
    return E.CarEnvelope(
        session=_session(rt),
        scores_available=E.CarScoresAvailable(**car_scores_available([shape], keys)),
        provenance=E.CarProvenance(
            fetched_at=served.fetched_at,
            cr_url=f"{CARS_API}/v2/cr/modelYears/{myid}",
            from_cache=served.from_cache,
            stale=served.stale,
        ),
        warnings=warnings + rt.session_warnings(),
        error=None,
        data=shape,
    )
