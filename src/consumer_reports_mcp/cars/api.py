"""The cars API client over the shared transport (PLAN P10.1, SPEC §5 *Cars*).

The public `x-api-key` is read from a car page at runtime and never pinned. A `403` from
`cars-api` is "no such route", never an auth problem. The car page's `window.isSubscriber` is a
free confirmation of session health for the PRODUCTS side (D11) — it gates nothing here.

`guarded` is the fetch discipline the products path already has (SPEC §7, §8 *Concurrency*),
applied to cars: one flight per key, and a `Challenged` negatively cached for an hour under that
key. Every cars fetch that can be coalesced or challenged goes through it — the car page,
`v1/cr/keys`, `v2/cr/carTypes` and each `v2/cr/modelYears/{id}`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from ..config import CARS_API, CARS_PAGE_URL
from ..extract import extract_cars_page
from ..transport import (
    Challenged,
    FetchFailed,
    failure_fields,
    is_policy_change,
    retry_once,
)

if TYPE_CHECKING:
    from ..runtime import Runtime

log = logging.getLogger(__name__)


async def guarded[T](rt: Runtime, key: tuple, fetch: Callable[[], Awaitable[T]], *, url: str) -> T:
    """Run `fetch` once per `key` across concurrent callers, and replay a `Challenged` from the
    negative cache — without a request — for an hour after one is raised.

    Raises `FetchFailed`/`Challenged` untouched otherwise. Whether a stale row answers instead
    is the CALLER's decision, under one convention (`cars/repository.py`): catch exactly those
    two, serve what is cached naming the failure, raise only when nothing is."""
    now = rt.clock()
    neg = rt.negcache.get(key, now)
    if neg is not None:
        _code, detail = neg
        raise Challenged(
            detail.get("reason") or "blocked", int(detail.get("http_status") or 0), url
        )
    try:
        return await rt.transport.single_flight.run(key, fetch)
    except Challenged as exc:
        rt.negcache.put(key, "challenged", now, **failure_fields(exc))
        raise


def failure_reason(exc: FetchFailed | Challenged) -> str:
    """The sub-reason the cars warnings carry (`index_refresh_failed:<reason>`,
    `ratings_unavailable:<id>:<reason>`): a `FetchFailed`'s `reason`, else `challenged`."""
    return exc.reason if isinstance(exc, FetchFailed) else "challenged"


class CarsApi:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._api_key: str | None = None
        self.is_subscriber: bool | None = None
        self.page_fetches = 0

    async def page_info(self, *, refresh: bool = False) -> tuple[str | None, bool | None]:
        """Fetch the car page for the api-key (cached in memory for the process) and read
        `window.isSubscriber`. With a cookie configured the marker updates session health —
        but ONLY when the transport asserted `hash` was still in the jar (SPEC §6 rule 1).
        One flight per process: N cold cars calls fetch the page once, not N times."""
        if self._api_key is not None and not refresh:
            return self._api_key, self.is_subscriber
        await guarded(self.rt, ("car_page",), self._fetch_page, url=CARS_PAGE_URL)
        return self._api_key, self.is_subscriber

    async def _fetch_page(self) -> None:
        # the car page is a `www.` fetch, so a sign-in adopted while it is in flight refuses
        # the response as `policy_changed`: retry once, under the new policy (SPEC §6)
        result = await retry_once(
            lambda: self.rt.transport.fetch(CARS_PAGE_URL), when=is_policy_change, what="car page"
        )
        self.page_fetches += 1
        info = extract_cars_page(result.content)
        self.is_subscriber = info.is_subscriber
        if info.api_key:
            self._api_key = info.api_key
        else:
            log.error("car page carried no DRS_CARS_API_API_KEY (title=%r)", result.title)
        if info.is_subscriber is None:
            log.warning("car page carried no window.isSubscriber marker (title=%r)", result.title)
        # the marker is a fact about the configured credential only when the transport asserted
        # `hash` was in the jar after the response — `credential_in_jar` is None without a
        # cookie, or once it was rejected — and `SessionState` applies the rule (SPEC §6 rule 1)
        self.rt.health.on_marker(
            info.is_subscriber, credential_present=result.credential_in_jar is True
        )

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET `{CARS_API}/{path}` with the api-key. Raises FetchFailed(no_such_route) on 403."""
        key, _ = await self.page_info()
        if key is None:
            raise FetchFailed(
                "url_unresolved",
                retryable=True,
                url=CARS_PAGE_URL,
                detail="no api key on the car page",
            )
        url = f"{CARS_API}/{path.lstrip('/')}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urlencode(clean)}"
        result = await self.rt.transport.fetch(
            url, headers={"x-api-key": key, "accept": "application/json"}
        )
        # API-Gateway answers an unknown route with a 403 and a 43-byte JSON body — never an
        # auth problem. wafer RETURNS that response under BOTH policies: JSON is never
        # challenge-detected, so its bare-403 branch rotates and then hands the response back
        # (measured on wafer 0.5.0 against a local server: 3 requests / ~2 s of rotation delay
        # under the anonymous policy, 1 request with a cookie). So this branch sees it, and a
        # `Challenged` raised by the transport for this host is a real WAF page, never a route.
        detail = f"cars-api {result.status}"
        if result.status in (403, 404):
            raise FetchFailed(
                "no_such_route", retryable=False, url=url, detail=detail, http_status=result.status
            )
        if result.status == 429:
            raise Challenged("rate_limited", 429, url)
        if result.status >= 500:
            raise FetchFailed(
                "server_error", retryable=True, url=url, detail=detail, http_status=result.status
            )
        if result.status >= 400:  # our request was malformed: not a data absence, not a route
            raise FetchFailed(
                "bad_request", retryable=False, url=url, detail=detail, http_status=result.status
            )
        try:
            return json.loads(result.content)
        except ValueError as exc:
            raise FetchFailed("empty_response", retryable=True, url=url, detail="not JSON") from exc

    # --- endpoints -------------------------------------------------------------------
    async def keys(self, car_type_id: int | None = None) -> Any:
        return await self.get_json("v1/cr/keys", {"modelYearCarTypeId": car_type_id})

    async def car_types(self) -> Any:
        return await self.get_json("v2/cr/carTypes")

    async def model_year(self, model_year_id: int) -> Any:
        return await self.get_json(f"v2/cr/modelYears/{int(model_year_id)}")

    async def cars(self, params: dict[str, Any]) -> Any:
        return await self.get_json("v2/cr/cars", params)

    async def glossary(self) -> Any:
        return await self.get_json("v1/cr/glossary/1")


__all__ = ["CarsApi", "Challenged", "FetchFailed", "failure_reason", "guarded"]
