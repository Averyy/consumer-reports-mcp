"""Cars listing and per-model-year ratings with request-cost discipline (PLAN P10.3).

`v2/cr/cars` is the listing source: its filters combine and its paging works, but `size` counts
MODELS, so `limit`/`offset` are applied after the fetch. There is no bulk road-test call:
`v2/cr/modelYears/{id}` is one request per car, cached per id in `car_raw` (one tier).

THE CARS STALE-FALLBACK CONVENTION — the same contract the products path keeps (SPEC §7 *Partial
and empty payloads*), stated once and used by `get_model_year`, `ensure_car_index` and
`ensure_taxonomy` alike:

- Every fetch runs through `cars.api.guarded`: one flight per key, and a `Challenged` replayed
  from the negative cache for an hour.
- The orchestrator catches exactly `(FetchFailed, Challenged)` — the two transport outcomes.
  Never a bare `Exception` (a bug in our own parser must surface), never one of the two alone
  (a 429 with a row in hand used to be a tool error).
- When a cached row exists it is served, `error: null`, naming the failure — `refresh_failed:
  <code>` on a served row, `index_refresh_failed:<reason>` on the index. The exception
  propagates only when there is nothing to serve, and the tool turns it into `error`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from .. import envelope as E
from ..cache import to_iso
from ..config import CARS_API
from ..normalize import as_int
from ..transport import Challenged, FetchFailed, failure_code
from .api import guarded
from .index import ensure_taxonomy, known_makes, resolve_car_type, slugify

if TYPE_CHECKING:
    from ..runtime import Runtime

STATE_IDS = {"new": 2, "used": 1}
LISTING_MAX_SIZE = 50  # CR: "default and maximum value of 'size' is 50" (measured 2026-09-03)
YEARS_PER_MODEL = 8  # a model brings ~12 years on average; `size` is in MODELS, `need` in rows
# `year` up to this far ABOVE the index's newest model year is sent to CR rather than refused
# locally: a new model year lands at exactly max+1, and the index can lag it by a 90-day TTL
YEAR_LAG_ALLOWANCE = 1


class CarsQueryError(Exception):
    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


async def build_listing_params(
    rt: Runtime,
    *,
    car_type: str | None,
    category: int | None,
    make: str | None,
    state: str | None,
    year: int | None,
) -> dict[str, Any]:
    """Every `cr_cars` filter maps to a real API parameter (SPEC §5). `state` defaults to
    UNFILTERED — both New and Used — because 6,698 of 7,095 model-years are Used."""
    params: dict[str, Any] = {}
    cat: int | None = None
    if category is not None:
        cat = as_int(category)
        if cat is None:  # `int()` on junk used to escape the tool as a ValueError
            raise CarsQueryError(
                "invalid_filter_value",
                f"category must be a cars category id such as 28958, not {E.quoted(category)}",
                filter="category",
            )
    if category is not None and car_type is None:
        raise CarsQueryError(
            "invalid_filter_value",
            "category is scoped by its car type: pass car_type as well "
            "(the same category id has different counts under different types)",
            filter="category",
        )
    if car_type is not None:
        taxonomy, _, _ = await ensure_taxonomy(rt)
        t = resolve_car_type(taxonomy, car_type)
        if t is None:
            legal = E.quoted_list(x["slug"] for x in taxonomy["types"] if x.get("slug"))
            raise CarsQueryError(
                "invalid_filter_value",
                f"car_type {E.quoted(car_type)} is not a Consumer Reports car type; legal: {legal}",
                filter="car_type",
                candidates=E.candidates(
                    {"id": x["id"], "slug": x["slug"], "name": x["name"]}
                    for x in taxonomy["types"]
                    if x.get("slug")
                ),
            )
        params["carTypeSlugName"] = t["slug"]
        if cat is not None:
            if (t["id"], cat) not in taxonomy["pairs"]:
                legal = E.quoted_list(
                    t["categories"], lambda c: f"{c['id']} ({E.quoted(c['name'])})"
                )
                raise CarsQueryError(
                    "invalid_filter_value",
                    f"category {cat} is not under car type {E.quoted(t['slug'])}; legal: {legal}",
                    filter="category",
                    candidates=E.candidates(
                        {"id": c["id"], "name": c["name"]} for c in t["categories"]
                    ),
                )
            params["categoryId"] = cat
    if make is not None:
        slug = slugify(make)
        known = known_makes(rt)
        if known and slug not in known:
            near = sorted(k for k in known if slug in k or k in slug)[:5]
            raise CarsQueryError(
                "invalid_filter_value",
                f"make {E.quoted(make)} (slug {E.quoted(slug)}) is not a Consumer Reports make"
                + (
                    f"; did you mean {E.quoted_list(near)}?"
                    if near
                    else "; run cr_car_search to find it"
                ),
                filter="make",
                candidates=E.candidates({"slug": k, "name": known[k]} for k in near),
            )
        params["slugMakeName"] = slug
    if state is not None:
        sid = STATE_IDS.get(str(state).strip().lower())
        if sid is None:
            raise CarsQueryError(
                "invalid_filter_value",
                "state must be 'new' or 'used'",
                filter="state",
                candidates=E.legal_values(STATE_IDS),
            )
        params["modelYearStateId"] = sid
    if year is not None:
        params["modelYear"] = validate_year(rt, year)
    if not any(k in params for k in ("carTypeSlugName", "slugMakeName", "modelYearStateId")):
        raise CarsQueryError(
            "invalid_filter_value",
            "cr_cars needs at least one of car_type, make or state (CR's listing requires it)",
            filter="car_type",
        )
    return params


def validate_year(rt: Runtime, year: Any) -> int:
    """`year` as an int inside the catalogue's own span, else `invalid_filter_value`.

    `v2/cr/cars` answers a `modelYear` outside the catalogue with a `400` — "'modelYear' must
    be a valid year integer" — which reached the caller as `fetch_failed(bad_request)`: a
    transport error for an ordinary question about a 1990 Honda. CR's accepted span IS the
    catalogue's, on both ends: measured 2026-09-06 with the index at 2000–2028, 1999 and 2029
    (and 2030, 2031, 2040) → 400, 2000 and 2028 → 200. There is no sliver of years CR accepts
    with an empty 200 beyond the index. Like `make`, the legal set is therefore the index's: a
    year below it, or more than `YEAR_LAG_ALLOWANCE` above it, is refused here, with the range,
    and costs no request.

    The one year above the index max is NOT refused locally, because the index is a snapshot
    (`car_index` TTL 90 days, refreshed only by `cr_car_search`) and a new model year lands at
    exactly max+1: while the index lags, a local refusal would answer "2029 is outside CR's
    catalogue" for cars CR lists. Passed through, CR is the authority — it either lists them
    or answers the `400` that `year_rejected` translates to this same error, so the answer
    does not depend on whether `cr_car_search` ran first. The lower bound stays exact: 1999 is
    a measured 400, and a catalogue only ever retires its oldest years (that lag is harmless —
    a stale, lower min passes a year CR then answers empty or 400, both handled).

    With the index not yet fetched the value passes through, and `year_rejected` translates
    CR's answer instead."""
    if isinstance(year, int) and not isinstance(year, bool):
        value = year
    else:  # a bool, a float, or a string: only an integer's text passes (`"2024"`, not `True`)
        try:
            value = int(str(year).strip())
        except (TypeError, ValueError):
            span = rt.cache.car_year_range()
            raise CarsQueryError(
                "invalid_filter_value",
                f"year must be a model year such as 2024, not {E.quoted(year)}",
                filter="year",
                candidates=E.legal_range(*span) if span is not None else None,
            ) from None
    span = rt.cache.car_year_range()
    if span is not None and not (span[0] <= value <= span[1] + YEAR_LAG_ALLOWANCE):
        raise CarsQueryError(
            "invalid_filter_value",
            f"year {value} is outside Consumer Reports' catalogue, which spans {span[0]}–{span[1]}",
            filter="year",
            candidates=E.legal_range(*span),
        )
    return value


def year_rejected(rt: Runtime, exc: FetchFailed, params: dict[str, Any]) -> CarsQueryError | None:
    """A `400` from the listing while `modelYear` was sent is CR rejecting `year` — the one
    caller value the listing forwards that CR validates as a value (car type, category and
    state resolve against fetched vocabularies; a make slug is `[a-z0-9-]`). Translated to the
    same `invalid_filter_value` the index check raises, so the answer is the same whether or
    not the index has been fetched yet. Anything else stays the transport failure it is.

    Decided HERE, beside the request that `params` describes, as the products repository
    decides which statuses are tool-level answers — not in the tool, which would have to be
    handed the caller's raw `year` back to make the same call from the exception path."""
    year = params.get("modelYear")
    if exc.reason != "bad_request" or year is None:
        return None
    span = rt.cache.car_year_range()
    hint = (
        f"; the catalogue spans {span[0]}–{span[1]}"
        if span is not None
        else "; cr_car_search shows the years Consumer Reports covers"
    )
    return CarsQueryError(
        "invalid_filter_value",
        f"Consumer Reports' cars API rejected year {year} as not a valid model year{hint}",
        filter="year",
        candidates=E.legal_range(*span) if span is not None else None,
    )


@dataclass
class CarListing:
    """What `list_cars` fetched: at least `need` rows in the ORDER `offset` pages over."""

    entries: list[dict]
    total: int | None  # the model-year population; None only when CR sent no `counts` and
    # pages remain, so the fetched prefix is not a count of anything
    last: bool  # whether the last page was fetched
    url: str  # the first page's URL
    requests: int


def _model_key(entry: dict) -> Any:
    mid = entry.get("modelId")
    if mid is not None:
        return ("id", mid)
    return (
        "slug",
        entry.get("slugMakeName") or entry.get("makeName"),
        entry.get("slugModelName") or entry.get("modelName"),
    )


def _year_desc(entry: dict) -> int:
    try:
        return -int(entry.get("modelYear") or 0)
    except (TypeError, ValueError):
        return 0


def stabilise_page(batch: list[dict]) -> list[dict]:
    """Years descending within each contiguous run of one model; NOTHING else moves.

    `offset` pages over CR's own order, and a page holds whole models (`size` counts models —
    RECON §13c-ii), so a model's years always arrive together. Reordering only inside such a
    run keeps every prefix of the sequence a prefix of the same sequence, whatever `size` a
    later call derives from its `need` — which is exactly what makes `offset` consistent. The
    earlier cross-model sort by `(make, model, year)` was NOT prefix-stable: sorting a longer
    prefix moved rows across the slice boundary, so a native order that differed from Python's
    string collation in one place (`McLaren` before `MINI`) returned rows twice and skipped
    others across offsets."""
    out: list[dict] = []
    run: list[dict] = []
    run_key: Any = None
    for entry in batch:
        key = _model_key(entry)
        if run and key != run_key:
            out.extend(sorted(run, key=_year_desc))  # stable: equal years keep CR's order
            run = []
        run_key = key
        run.append(entry)
    out.extend(sorted(run, key=_year_desc))
    return out


async def list_cars(rt: Runtime, params: dict[str, Any], *, need: int) -> CarListing:
    """Fetch listing pages until at least `need` model-years are in hand (or the last page).

    `size` and `totalElements` count MODELS while the rows are model-years (RECON §13c-ii, and
    measured: `totalElements == counts.models`), so `size` is derived from `need` (never from
    `limit`), CR caps it at 50, and the population comes from `counts[name == "cars"]`.

    Raises `CarsQueryError` for the one `400` that is a filter answer (`year_rejected`), and
    `FetchFailed`/`Challenged` untouched otherwise."""
    entries: list[dict] = []
    requests = 0
    page = 1
    population: int | None = None
    first_url = ""
    # New model-years are ~1 per model (397 New across 677 models); Used ones ~10
    per_model = 1 if params.get("modelYearStateId") == STATE_IDS["new"] else YEARS_PER_MODEL
    size = max(2, min(LISTING_MAX_SIZE, -(-need // per_model)))
    while True:
        q = dict(params, size=size, page=page)
        try:
            payload = await rt.cars.cars(q)
        except FetchFailed as exc:
            rejected = year_rejected(rt, exc, params)
            if rejected is not None:
                raise rejected from exc
            raise
        requests += 1
        if not first_url:
            first_url = f"{CARS_API}/v2/cr/cars?{urlencode(q)}"
        content = payload.get("content") if isinstance(payload, dict) else None
        # an EMPTY listing ships `"content": {}` — a dict, not `[]` (measured 2026-09-05 on a
        # make/year with no cars); only a list carries rows
        batch = [e for e in content if isinstance(e, dict)] if isinstance(content, list) else []
        entries.extend(stabilise_page(batch))
        if population is None:
            population = _population(payload)
        last = bool(payload.get("last", True)) if isinstance(payload, dict) else True
        if last or not batch or len(entries) >= need:
            break
        page += 1
    if population is None and last:
        population = len(entries)  # everything was fetched, so the prefix IS the population
    return CarListing(entries, population, last, first_url, requests)


def _population(payload: Any) -> int | None:
    """`counts` is a list of `{name, value}` in the real payload; `cars` is the model-year
    population. A dict form is tolerated."""
    if not isinstance(payload, dict):
        return None
    counts = payload.get("counts")
    if isinstance(counts, list):
        for c in counts:
            if isinstance(c, dict) and c.get("name") == "cars":
                try:
                    return int(c.get("value"))
                except (TypeError, ValueError):
                    return None
    if isinstance(counts, dict) and counts.get("cars") is not None:
        try:
            return int(counts["cars"])
        except (TypeError, ValueError):
            return None
    return None


@dataclass
class CarServed:
    """One model-year's ratings payload and how it was obtained."""

    payload: dict
    fetched_at: str
    from_cache: bool
    stale: bool
    requests: int
    # `fetch_failed` | `challenged` when a cached row answered for a refetch that failed —
    # the caller emits `refresh_failed:<code>` (SPEC §7)
    refresh_failed: str | None = None


async def get_model_year(rt: Runtime, model_year_id: int, *, refresh: bool = False) -> CarServed:
    """Cache-first per id, one flight, under the module's stale-fallback convention."""
    now = rt.clock()
    ttl = rt.settings.cache_ttl_days
    myid = int(model_year_id)
    sel = rt.cache.select_car_raw(myid, ttl, now)
    if sel is not None and not refresh and not sel.stale:
        return CarServed(rt.cache.load_car_raw(sel.rowid), sel.fetched_at, True, False, 0)
    url = f"{CARS_API}/v2/cr/modelYears/{myid}"
    made = 0

    async def fetch() -> dict:
        nonlocal made
        made = 1
        payload = await rt.cars.model_year(myid)
        if not isinstance(payload, dict) or not isinstance(payload.get("response"), dict):
            raise FetchFailed(
                "empty_response", retryable=True, url=url, detail="no response object"
            )
        # An unknown model-year answers 200 with `{"response": {}, "responseSummary":
        # {"responseCount": 0}}` — cars-api never 404s this route (measured 2026-09-04). Caught
        # here, before the write, because `car_shape({})` is an all-null car whose every score
        # reads `absent`, i.e. "CR published no value" for a car that does not exist — and the
        # empty row would then serve that answer from cache for the full 30-day TTL.
        summary = payload.get("responseSummary")
        count = summary.get("responseCount") if isinstance(summary, dict) else None
        if not payload["response"] or count == 0:
            raise FetchFailed(
                "no_such_route",
                retryable=False,
                url=url,
                detail="cars-api 200 with an empty response",
            )
        rt.cache.write_car_raw(myid, payload, now)
        return payload

    try:
        payload = await guarded(rt, ("car", myid), fetch, url=url)
    except (FetchFailed, Challenged) as exc:
        if sel is not None:  # the convention: a cached row beats an error
            return CarServed(
                rt.cache.load_car_raw(sel.rowid),
                sel.fetched_at,
                True,
                sel.stale,
                made,
                refresh_failed=failure_code(exc),
            )
        raise
    return CarServed(payload, to_iso(now), False, False, made)
