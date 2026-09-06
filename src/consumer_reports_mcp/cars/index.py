"""Cars discovery: `v1/cr/keys` → `car_index`, `v2/cr/carTypes` → the taxonomy (PLAN P10.2).

A cars category is scoped by its car type: `(carTypeId, categoryId)` is the address (RECON §13b).

Both orchestrators follow the cars stale-fallback convention (`cars/repository.py`): a
`FetchFailed`/`Challenged` serves the cached index or taxonomy, naming the failure; it propagates
only when nothing is cached. And a payload that parses to ZERO rows is a failure, not a run — it
is never written and never recorded, because a recorded empty index would answer every search
and every `car_type` for the full 90-day index TTL (the products path's `ensure_az_index` makes
the same refusal).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from ..cache import from_iso, to_iso, utcnow
from ..config import CARS_API
from ..transport import Challenged, FetchFailed
from .api import failure_reason, guarded

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger(__name__)

KEYS_URL = f"{CARS_API}/v1/cr/keys"
CAR_TYPES_URL = f"{CARS_API}/v2/cr/carTypes"


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).strip().lower()).strip("-")


# --------------------------------------------------------------------------- parsers (pure)


def parse_keys(payload: Any) -> list[dict]:
    """Flatten makes → models → model-years into `car_index` rows."""
    rows: list[dict] = []
    makes = payload.get("response") if isinstance(payload, dict) else None
    for make in makes or []:
        if not isinstance(make, dict):
            continue
        for model in make.get("models") or []:
            if not isinstance(model, dict):
                continue
            for my in model.get("modelYears") or []:
                if not isinstance(my, dict) or _int(my.get("modelYearId")) is None:
                    continue
                states = [
                    s.get("modelYearStateName")
                    for s in my.get("modelYearStates") or []
                    if isinstance(s, dict) and s.get("modelYearStateName")
                ]
                types = []
                primary = None
                for t in my.get("modelYearCarTypes") or []:
                    if not isinstance(t, dict) or _int(t.get("carTypeId")) is None:
                        continue
                    tid = _int(t["carTypeId"])
                    is_primary = t.get("isPrimary") == "Y"
                    types.append(
                        {"id": tid, "name": t.get("carTypeDisplayName"), "primary": is_primary}
                    )
                    if is_primary and primary is None:
                        primary = tid
                rows.append(
                    {
                        "model_year_id": _int(my["modelYearId"]),
                        "make_id": _int(make.get("makeId")),
                        "make": make.get("makeName"),
                        "slug_make": make.get("slugMakeName") or slugify(make.get("makeName", "")),
                        "model_id": _int(model.get("modelId")),
                        "model": model.get("modelName"),
                        "slug_model": model.get("slugModelName")
                        or slugify(model.get("modelName", "")),
                        "year": _int(my.get("modelYear")),
                        "states": states,
                        "car_types": types,
                        "primary_car_type_id": primary,
                    }
                )
    return rows


def parse_car_types(payload: Any) -> dict:
    """{types: [...], pairs: {(car_type_id, category_id): {...}}} — the same category under two
    parents keeps two entries with their own counts (RECON §13b)."""
    types: list[dict] = []
    pairs: dict[tuple[int, int], dict] = {}
    content = payload.get("content") if isinstance(payload, dict) else None
    for t in content or []:
        if not isinstance(t, dict) or _int(t.get("carTypeId")) is None:
            continue
        tid = _int(t["carTypeId"])
        cats = []
        for c in t.get("categories") or []:
            if not isinstance(c, dict) or _int(c.get("categoryId")) is None:
                continue
            cid = _int(c["categoryId"])
            entry = {
                "id": cid,
                "name": c.get("categoryName"),
                "slug": c.get("slugCategoryName"),
                "sort_order": c.get("sortOrder"),
                "tested": _int(c.get("testedCarCount")),
                "not_tested": _int(c.get("notTestedCarCount")),
                "in_test": _int(c.get("inTestCarCount")),
                "price_min": c.get("carTypeCategoryPriceMin"),
                "price_max": c.get("carTypeCategoryPriceMax"),
                "car_type_id": tid,
            }
            cats.append(entry)
            pairs[(tid, cid)] = entry
        types.append(
            {
                "id": tid,
                "name": t.get("carTypeName"),
                "slug": t.get("slugCarTypeName") or slugify(t.get("carTypeName") or str(tid)),
                "sort_order": t.get("carTypeSortOrder"),
                "tested": _int(t.get("testedCarCount")),
                "not_tested": _int(t.get("notTestedCarCount")),
                "in_test": _int(t.get("inTestCarCount")),
                "price_min": t.get("carTypePriceMin"),
                "price_max": t.get("carTypePriceMax"),
                "categories": cats,
            }
        )
    return {"types": types, "pairs": pairs}


def resolve_car_type(taxonomy: dict, token: Any) -> dict | None:
    """A car type by slug, id or name (case-insensitive)."""
    tid = _int(token)
    wanted = slugify(str(token))
    for t in taxonomy.get("types") or []:
        if tid is not None and t["id"] == tid:
            return t
        if t.get("slug") == wanted or slugify(t.get("name") or "") == wanted:
            return t
    return None


# --------------------------------------------------------------------------- orchestration


async def ensure_car_index(rt: Runtime, *, refresh: bool = False) -> tuple[bool, str, str | None]:
    """`v1/cr/keys` unfiltered (7,095 model-years) into `car_index`, on the 90-day index TTL.
    Returns (fetched_now, fetched_at, refresh_failed) — the third is the failure's reason when
    a cached index answered for a fetch that failed, else None. Raises only when nothing is
    cached. One flight per process: concurrent cold searches fetch the 3.1 MB index once."""
    status = rt.cache.car_index_status()
    if status and not refresh and not index_is_stale(rt):
        return False, status["fetched_at"], None

    async def fetch() -> str:
        payload = await rt.cars.keys()
        rows = parse_keys(payload)
        if not rows:
            # a `200` with no model-years is a failed fetch, never a 90-day run of nothing
            raise FetchFailed(
                "empty_response",
                retryable=True,
                url=KEYS_URL,
                detail="v1/cr/keys parsed to zero model-years",
            )
        now = rt.clock()
        rt.cache.write_car_index(rows, now)
        return to_iso(now)

    try:
        fetched_at = await guarded(rt, ("car_index",), fetch, url=KEYS_URL)
    except (FetchFailed, Challenged) as exc:
        if status is not None:  # the convention: a cached index beats an error
            log.warning("car index refresh failed (%s); serving the cached index", exc)
            return False, status["fetched_at"], failure_reason(exc)
        raise
    return True, fetched_at, None


def index_is_stale(rt: Runtime) -> bool:
    status = rt.cache.car_index_status()
    if not status:
        return True
    return from_iso(status["fetched_at"]) <= rt.clock() - _days(rt.settings.index_ttl_days)


def known_makes(rt: Runtime) -> dict[str, str | None]:
    """slug → CR's make name, from the index: the legal `make` vocabulary with the name an
    agent will recognise beside the slug it must pass (`{"slug": "mini", "name": "MINI"}`)."""
    return {m["slug"]: m["name"] for m in rt.cache.car_makes()}


async def ensure_taxonomy(rt: Runtime, *, refresh: bool = False) -> tuple[dict, str, bool]:
    """`v2/cr/carTypes` parsed, on the 90-day TTL. Returns (taxonomy, fetched_at, from_cache).
    A failed refresh serves the cached taxonomy (it is a validator for `cr_cars` filters, not
    served data, so the envelope does not name that); raises only when nothing is cached."""
    cached = rt.cache.select_car_taxonomy(rt.settings.index_ttl_days, rt.clock())
    if cached is not None and not refresh and not cached[2]:
        return parse_car_types(cached[0]), cached[1], True

    async def fetch() -> tuple[dict, str]:
        payload = await rt.cars.car_types()
        taxonomy = parse_car_types(payload)
        if not taxonomy["types"]:
            # zero car types would make every `car_type` invalid for 90 days: a failed fetch
            raise FetchFailed(
                "empty_response",
                retryable=True,
                url=CAR_TYPES_URL,
                detail="v2/cr/carTypes parsed to zero car types",
            )
        now = rt.clock()
        rt.cache.write_car_taxonomy(payload, now)
        return taxonomy, to_iso(now)

    try:
        taxonomy, fetched_at = await guarded(rt, ("car_taxonomy",), fetch, url=CAR_TYPES_URL)
    except (FetchFailed, Challenged) as exc:
        if cached is not None:  # the convention: a cached taxonomy beats an error
            log.warning("car taxonomy refresh failed (%s); serving the cached one", exc)
            return parse_car_types(cached[0]), cached[1], True
        raise
    return taxonomy, fetched_at, False


def _days(n: int):
    from datetime import timedelta

    return timedelta(days=n)


__all__ = [
    "ensure_car_index",
    "ensure_taxonomy",
    "index_is_stale",
    "known_makes",
    "parse_car_types",
    "parse_keys",
    "resolve_car_type",
    "slugify",
    "utcnow",
]
