"""BOUNDARY 1: the only module that imports wafer (PLAN P4, SPEC §6 *Driving this through wafer*).

One `AsyncSession` per process, built lazily under a lock. The rotation policy is chosen ONCE at
construction from a single fact — is a cookie configured? — because wafer has no per-request form
and a second session would double the request rate. A credential adopted mid-process therefore
cannot be applied to the live session: `adopt()` retires it and the next request builds a new
one under the new policy (SPEC §6 *cr_sign_in*). Nothing wafer-typed leaves this module: a
`FetchResult` carries status, final URL, body bytes and a title, never headers or cookies.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

import wafer

from .config import LOGIN_HOST, MAX_RESPONSE_SIZE, WWW, Settings, is_login_url
from .credentials import (
    COOKIE_ATTRIBUTES,
    COOKIE_NAMES,
    DURABLE_COOKIE,
    CredentialStore,
    SessionState,
)
from .extract import extract_cars_page, read_subscriber_marker, read_title

log = logging.getLogger(__name__)

WWW_HOST = "www.consumerreports.org"
CARS_API_HOST = "cars-api.consumerreports.org"
CR_SUFFIX = ".consumerreports.org"
# The only hosts this process ever talks to, over https only. URLs come from FETCHED content —
# every `<loc>` in `products.xml`, `reliabilityURL` in a payload, a cached `final_url` — and the
# jar seeds `hash` for `.consumerreports.org`, so without this one check anyone shaping what CR
# serves could point the process at a metadata endpoint, localhost, or a CR subdomain that then
# receives the user's 365-day credential. An exact set, not a suffix: a subdomain we do not
# talk to is not ours.
ALLOWED_HOSTS = frozenset({WWW_HOST, LOGIN_HOST, CARS_API_HOST})
# The one `fetch_failed` reason that is OUR state change rather than a network condition: a
# credential was adopted while the request was in flight (SPEC §6 *cr_sign_in*). Every caller
# that owns a whole tool call retries it exactly once, so a sign-in landing mid-fetch never
# surfaces as a tool error.
POLICY_CHANGED = "policy_changed"


class FetchFailed(Exception):
    """A transport failure. `reason` is derived from the EXCEPTION TYPE (SPEC §7), never by
    parsing free text; `detail` carries that text for diagnostics only."""

    def __init__(
        self,
        reason: str,
        *,
        retryable: bool,
        url: str,
        detail: str | None = None,
        http_status: int | None = None,
    ):
        super().__init__(f"fetch_failed({reason}) {_safe_url(url)}")
        self.reason = reason
        self.retryable = retryable
        self.url = url
        self.detail = detail
        # the status that caused it, when one did (`server_error`, `no_such_route`,
        # `bad_request`) — SPEC §7 *Error taxonomy*: `http_status` "when a status caused it"
        self.http_status = http_status


def is_policy_change(exc: FetchFailed) -> bool:
    return exc.reason == POLICY_CHANGED


async def retry_once[T](
    fetch: Callable[[], Awaitable[T]], *, when: Callable[[FetchFailed], bool], what: str
) -> T:
    """Run `fetch`; on a `FetchFailed` that `when` accepts, run it exactly once more. Anything
    else — a second failure, a `Challenged`, a failure `when` declines — propagates untouched.
    The second attempt runs under whatever policy the transport has NOW, which is the point."""
    try:
        return await fetch()
    except FetchFailed as exc:
        if not when(exc):
            raise
        log.info("%s: fetch_failed(%s); retrying once", what, exc.reason)
        return await fetch()


class Challenged(Exception):
    """A WAF challenge, block or rate limit — surfaced, never retried around (SPEC §7)."""

    def __init__(self, challenge_type: str, status: int, url: str):
        super().__init__(f"challenged({challenge_type}, {status}) {_safe_url(url)}")
        self.challenge_type = challenge_type
        self.status = status
        self.url = url


# What a transport failure projects into `error` (SPEC §7 *Error taxonomy*): `reason`,
# `retryable` and `http_status`, from ONE function, at every catch site. The attributes below
# never escape — `detail` is free text (a wafer reason, a byte count) and a URL can carry a
# query — and declaring them is what lets `tests/test_transport.py` hold `failure_fields` to the
# exceptions' own attributes: a new attribute goes into the projection or into this set, never
# a third place, and never into a hand-spelled dict at a catch site
# (`tests/test_boundaries.py` scans for those). Before this, the same three fields were spelled
# at ten sites and the one that missed `http_status` produced `null` silently.
FAILURE_DIAGNOSTIC_ONLY = frozenset({"url", "detail"})


def failure_code(exc: FetchFailed | Challenged) -> str:
    """The error-taxonomy code a transport failure carries: `challenged` or `fetch_failed`."""
    return "challenged" if isinstance(exc, Challenged) else "fetch_failed"


def failure_fields(exc: FetchFailed | Challenged) -> dict[str, Any]:
    """`{reason, retryable, http_status}` for `error` — the same three keys for both types.

    One function, because the two exceptions answer the same three questions and the
    asymmetry between them is a fact about CR, not a gap in the projection: a `Challenged`'s
    sub-reason is its `challenge_type`; its status is the one the challenge arrived on, which
    is always known (a challenge is a response, so it is never null — a `FetchFailed` carries
    a status only when one caused it); and it is never retryable, because a challenge is
    surfaced, not retried around (SPEC §7), and is negatively cached for an hour, so a retry
    would replay it without a request. Every catch site unpacks this dict — a `**` into a
    `ToolError`, a `detail` handed to a fallback, a negative-cache entry — so a site can no
    longer project two fields of the three."""
    if isinstance(exc, Challenged):
        return {"reason": exc.challenge_type, "retryable": False, "http_status": exc.status}
    return {"reason": exc.reason, "retryable": exc.retryable, "http_status": exc.http_status}


@dataclass(frozen=True)
class FetchResult:
    """What a fetch yields. No body text, no headers, no cookies — by construction."""

    status: int
    final_url: str
    content: bytes
    title: str | None
    challenge_type: str | None
    elapsed: float
    redirected: bool
    credential_rejected: bool = False
    # On a `www.` fetch with a cookie configured: whether `hash` was still in the jar AFTER the
    # response. None when no cookie is configured or the host is not `www.`. Any surface about
    # to say `session_expired` must see True here (SPEC §6 rule 1).
    credential_in_jar: bool | None = None


def _safe_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _check_url(url: str) -> None:
    """https, and a host in `ALLOWED_HOSTS` — the one choke point every fetch passes."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in ALLOWED_HOSTS:
        raise FetchFailed(
            "url_unresolved",
            retryable=False,
            url=url,
            detail=f"refusing {parts.scheme or 'no scheme'}://{host or 'no host'}: not a CR host",
        )


def host_of(url: str) -> str:
    return urlsplit(url).hostname or ""


def is_www(url: str) -> bool:
    """A page fetch on the `www.`/`secure.` hosts — where the `/ec/login` rule applies (D10).
    An allowlist: an API subdomain must never inherit the page rules."""
    return host_of(url) in (WWW_HOST, LOGIN_HOST)


def is_cars_api(url: str) -> bool:
    return host_of(url) == CARS_API_HOST


def session_kwargs(settings: Settings, cookie_configured: bool) -> dict[str, Any]:
    """The constructor kwargs — `timeout` ALWAYS paired with `attempt_timeout`, and the
    no-rotation policy iff a cookie is configured (SPEC §6 rule 1).

    `rate_limit` is 0.0 — OFF. The politeness interval is enforced by `Transport`'s own gate
    (`_space_out`), because wafer's limiter is wait → send → record with no lock: concurrent
    callers read one timestamp, compute the same delay and fire together. Two limiters would
    also stack, so wafer's is disabled rather than doubled."""
    kwargs: dict[str, Any] = {
        "rate_limit": 0.0,
        "timeout": settings.timeout_s,
        "attempt_timeout": settings.attempt_timeout_s,
        "max_response_size": MAX_RESPONSE_SIZE,
    }
    if cookie_configured:
        kwargs["max_rotations"] = 0
        kwargs["max_failures"] = None
    return kwargs


def seed_cookies(session: Any, set_cookie_strings: list[str]) -> None:
    for raw in set_cookie_strings:
        session.add_cookie(raw, WWW + "/")


@dataclass
class _Flight:
    task: asyncio.Future
    callers: int = 0  # how many `run()` calls are awaiting this task right now


class SingleFlight:
    """Coalesce concurrent calls for the same key onto one in-flight task.

    The work runs as its OWN task and every caller — the one that started it included — awaits
    it through `shield`, so no caller's cancellation reaches the flight while another caller
    still wants its result. An earlier shape awaited the factory in the first caller's task and
    stored whatever it raised on a shared future: that caller's `CancelledError` then landed on
    every waiter, and a task that sees `CancelledError` is marked cancelled whether or not
    anyone cancelled it — an MCP client cancelling `cr_ratings("tvs")` mid-download killed a
    concurrent `cr_filters("tvs")`, and a cancelled `cr_car` killed the `cr_cars(
    detail="standard")` listing sharing its id. The flight is cancelled only when its LAST
    caller leaves, so a lone caller's cancellation still stops the 11 MB download, as before."""

    def __init__(self) -> None:
        self._inflight: dict[Any, _Flight] = {}

    async def run(self, key: Any, factory: Callable[[], Awaitable[Any]]) -> Any:
        flight = self._inflight.get(key)
        if flight is None or flight.task.cancelled():
            task = asyncio.ensure_future(factory())
            task.add_done_callback(_retrieve)  # no "exception was never retrieved" on stderr
            flight = _Flight(task)
            self._inflight[key] = flight
            task.add_done_callback(lambda _t, k=key, f=flight: self._forget(k, f))
        flight.callers += 1
        try:
            return await asyncio.shield(flight.task)
        finally:
            flight.callers -= 1
            if flight.callers == 0 and not flight.task.done():
                # the last caller left (cancelled): nobody wants the result, so stop the work —
                # and unlist it NOW, so a caller arriving before the cancellation lands starts
                # a fresh flight rather than joining a dying one
                self._forget(key, flight)
                flight.task.cancel()

    def _forget(self, key: Any, flight: _Flight) -> None:
        if self._inflight.get(key) is flight:
            del self._inflight[key]


def _retrieve(fut: asyncio.Future) -> None:
    if not fut.cancelled():
        fut.exception()


def _policy_word(configured: bool) -> str:
    return "adopted (no-rotation policy)" if configured else "dropped (rotation policy)"


async def _retire(sess: Any) -> None:
    """Best-effort release of a session that is no longer current. wafer 0.5.0's `AsyncSession`
    has no `close()`; its `__aexit__` releases an owned challenge solver and nothing else, so an
    in-flight request holding the old object is not interrupted. Never raises."""
    try:
        closer = getattr(sess, "aclose", None) or getattr(sess, "close", None)
        if closer is not None:
            result = closer()
            if inspect.isawaitable(result):
                await result
        elif hasattr(sess, "__aexit__"):
            await sess.__aexit__(None, None, None)
    except Exception as exc:  # a retired session's teardown must never fail a caller
        log.debug("retiring the previous session raised %s", type(exc).__name__)


def _logged_out(content: bytes) -> bool:
    """Either surface's logged-out marker: `data-subscriber="false"` (products) or
    `window.isSubscriber = false` (cars). A page with neither cannot say anything."""
    if read_subscriber_marker(content) == "false":
        return True
    return extract_cars_page(content).is_subscriber is False


class Transport:
    """The one session, and `fetch()` — classification by exception type, the login-URL check
    on every `www.` fetch, the jar assertion, and rotation write-back (SPEC §6 1–3)."""

    def __init__(
        self,
        settings: Settings,
        credentials: CredentialStore,
        health: SessionState,
        *,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self.credentials = credentials
        self.health = health
        # fixed for the life of a SESSION: `adopt()` is the one way it moves, and it retires
        # the session in the same step so the policy and the session never disagree
        self.cookie_configured = credentials.configured
        self._factory = session_factory or wafer.AsyncSession
        self._session: Any = None
        self._lock = asyncio.Lock()
        self.single_flight = SingleFlight()
        self.request_count = 0
        self.adoptions = 0  # how many times `adopt()` rebuilt the policy (diagnostics)
        # the politeness gate, per host as the config documents it: one lock and one last-send
        # stamp per host, the lock held across the WAIT only (see `_space_out`)
        self._gates: dict[str, asyncio.Lock] = {}
        self._last_send: dict[str, float] = {}

    async def session(self) -> Any:
        if self._session is None:
            async with self._lock:
                if self._session is None:
                    sess = self._factory(**session_kwargs(self.settings, self.cookie_configured))
                    if self.cookie_configured:
                        seed_cookies(sess, self.credentials.set_cookie_strings())
                    self._session = sess
        return self._session

    # --- live credential adoption -------------------------------------------------------
    async def adopt(self) -> bool:
        """Make a credential saved (or forgotten) mid-process take effect without a restart.

        Re-reads the store — the store, not an argument, so it cannot disagree with what the
        next `session()` will seed — and, under the lock: flips the policy, clears the
        rejection latch (a rejection was about the OLD cookie), resets health to the cold-start
        state for the new configuration, and drops the session so the next request builds one
        with `max_rotations=0, max_failures=None`. Idempotent: adopting the same state twice
        costs one more session build and nothing else.

        A request in flight keeps its reference to the old session and finishes on it, and
        its `www.` response is then refused by `_guard_generation` as
        `fetch_failed(policy_changed)` — retryable, never classified, never cached — because it
        was made under the old credential (or none) and must not be read against the new one.
        Callers that own a whole tool call (`get_category`, `get_reliability`,
        `CarsApi.page_info`, the sitemap pass) retry that exactly once via `retry_once`, so the
        adopt never surfaces as a tool error. Returns whether a cookie is now configured."""
        async with self._lock:
            configured = self.credentials.configured
            old, self._session = self._session, None
            self.cookie_configured = configured
            self.credentials.rejected = False
            self.health.reconfigure(configured)
            self.adoptions += 1
        log.info("credential %s; the next request builds a new session", _policy_word(configured))
        if old is not None:
            await _retire(old)
        return configured

    async def aclose(self) -> None:
        """Release the current session (a probe transport that is done, or shutdown)."""
        async with self._lock:
            old, self._session = self._session, None
        if old is not None:
            await _retire(old)

    # --- the fetch --------------------------------------------------------------------
    async def fetch(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResult:
        _check_url(url)  # before anything: an off-host URL never builds a session
        if self.settings.offline:
            raise FetchFailed("connection_failed", retryable=True, url=url, detail="CR_OFFLINE")
        sess = await self.session()
        generation = self.adoptions  # the policy this request runs under
        www = is_www(url)
        self._reseed_if_missing(sess, www)
        result = await self._one(sess, url, headers)
        self._guard_generation(generation, www, url)
        if www and is_login_url(result.final_url):
            # step 1 — the FINAL url, as a ROUTE: CR rejected the credential (RECON §10b). Drop
            # it from the jar for the rest of the process — CR usually clears it, but a
            # lapsed-not-malformed cookie was never measured (RECON §15), so evict explicitly —
            # and retry ONCE. The latch is the jar's business (no re-seed, no rotation
            # write-back) and lives on the store; the health transition is the event.
            self.credentials.rejected = True
            self.health.on_rejected()
            self._evict_credentials(sess)
            log.warning("credential rejected by %s; retrying once anonymously", WWW_HOST)
            retry = await self._one(sess, url, headers)
            self._guard_generation(generation, www, url)
            return replace(retry, credential_rejected=True, credential_in_jar=False)
        in_jar: bool | None = None
        if www and self.cookie_configured and not self.credentials.rejected:
            in_jar = sess.get_cookie(DURABLE_COOKIE, WWW + "/") is not None
            if not in_jar and _logged_out(result.content):
                # a logged-out page with `hash` gone is a transport failure, never an auth
                # state (SPEC §6 rule 1: `identity_rotated`) — on EITHER marker surface
                raise FetchFailed("identity_rotated", retryable=False, url=url)
            self.credentials.record_rotation(
                "userLicenses", sess.get_cookie("userLicenses", WWW + "/")
            )
        return replace(result, credential_in_jar=in_jar)

    def _guard_generation(self, generation: int, www: bool, url: str) -> None:
        """A `www.` response that arrives AFTER `adopt()` moved the policy was made under the
        old credential (or none) and must not be classified under the new one: a rejection of
        the old cookie would latch `rejected`/`expired` onto the new, and a logged-out page
        would be read against a jar the new policy has not seeded. It is a transport failure —
        retryable, never cached, never an auth state. The cars API has no credential logic, so
        its responses are unaffected."""
        if www and generation != self.adoptions:
            raise FetchFailed(
                "policy_changed",
                retryable=True,
                url=url,
                detail="a credential was adopted while this request was in flight",
            )

    @staticmethod
    def _evict_credentials(sess: Any) -> None:
        for name in COOKIE_NAMES:
            sess.add_cookie(f"{name}=; {COOKIE_ATTRIBUTES}; Max-Age=0", WWW + "/")

    def _reseed_if_missing(self, sess: Any, www: bool) -> None:
        """Re-seed BEFORE a request if the jar lost `hash` (a rebuild empties it) — a
        precondition, not a retry (SPEC §6)."""
        if not (www and self.cookie_configured) or self.credentials.rejected:
            return
        if sess.get_cookie(DURABLE_COOKIE, WWW + "/") is None:
            log.info("hash missing from the jar before a request; re-seeding")
            seed_cookies(sess, self.credentials.set_cookie_strings())

    async def _space_out(self, url: str) -> None:
        """The politeness interval, held across concurrent callers.

        One lock per host, held across the wait and released BEFORE the request goes out — a
        gate, not a serialiser: holding it across the request would queue 60-second downloads
        behind each other. The stamp is taken as the lock is released, so N concurrent callers
        leave at 0, i, 2i, … rather than all at once. `CR_MIN_REQUEST_INTERVAL_S=0` disables."""
        interval = self.settings.min_request_interval_s
        if interval <= 0:
            return
        host = host_of(url)
        gate = self._gates.setdefault(host, asyncio.Lock())
        async with gate:
            last = self._last_send.get(host)
            if last is not None:
                wait = last + interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            self._last_send[host] = time.monotonic()

    async def _one(self, sess: Any, url: str, headers: dict[str, str] | None) -> FetchResult:
        await self._space_out(url)
        self.request_count += 1
        kwargs: dict[str, Any] = {}
        if headers:
            kwargs["headers"] = headers
        try:
            resp = await sess.get(url, **kwargs)
        except wafer.ConnectionFailed as exc:
            raise FetchFailed(
                "connection_failed", retryable=True, url=url, detail=getattr(exc, "reason", None)
            ) from exc
        except wafer.WaferTimeout as exc:
            raise FetchFailed("timeout", retryable=True, url=url) from exc
        except wafer.EmptyResponse as exc:
            raise FetchFailed("empty_response", retryable=True, url=url) from exc
        except wafer.ResponseTooLarge as exc:
            raise FetchFailed(
                "too_large", retryable=False, url=url, detail=f"{exc.size} > {exc.limit}"
            ) from exc
        except wafer.TooManyRedirects as exc:
            raise FetchFailed("redirect_loop", retryable=False, url=url) from exc
        except wafer.ChallengeDetected as exc:  # includes RequestBlocked
            raise Challenged(exc.challenge_type, exc.status_code, url) from exc
        except wafer.RateLimited as exc:
            raise Challenged("rate_limited", 429, url) from exc
        except wafer.TokenMintFailed as exc:  # a challenge-solve failure, not a network one
            raise Challenged("token_mint_failed", 0, url) from exc
        except wafer.WaferError as exc:
            raise FetchFailed(
                "connection_failed", retryable=True, url=url, detail=type(exc).__name__
            ) from exc
        status = int(resp.status_code)
        content: bytes = resp.content or b""
        title = read_title(content) if content else None
        log.debug(
            "GET %s -> %s (%d bytes, %.2fs)", _safe_url(url), status, len(content), resp.elapsed
        )
        # a challenge can arrive with a 200 — check the type, not just the status
        if resp.challenge_type is not None:
            raise Challenged(str(resp.challenge_type), status, url)
        if status in (403, 429) and not is_cars_api(url):
            # under no-rotation wafer RETURNS these; on the page host they are a block. On the
            # cars API a 403 is "no such route" (SPEC §5) and is the caller's to classify.
            raise Challenged("blocked", status, url)
        if status >= 500:
            raise FetchFailed(
                "server_error", retryable=True, url=url, detail=f"http {status}", http_status=status
            )
        if not content:
            raise FetchFailed("empty_response", retryable=True, url=url)
        return FetchResult(
            status=status,
            final_url=str(resp.url),
            content=content,
            title=title,
            challenge_type=None,
            elapsed=float(resp.elapsed),
            redirected=bool(resp.history),
        )
