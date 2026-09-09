"""Cache-first orchestration (PLAN P6.2/P6.3, SPEC §7/§8).

`get_category`: resolve → effective tier → select → (miss/stale/refresh) single-flight fetch →
classify → session health (marker-bearing pages only) → append never-downgrade → serve the row
the selection rule now picks. Drift is negatively cached; a cached row is served with
`error: null` whenever one exists; `fetch_failed` is never cached.

`get_reliability`: the isolated second envelope. Never fetches a category page, never touches
session health.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import envelope as E
from . import ingest
from .cache import RANGE_NONE, Cache, Resolution, Selection, from_iso, is_stale, utcnow
from .config import AUTH_PROBE_CATEGORY, AUTH_PROBE_PATH, REFRESH_COOLDOWN_S, WWW, Settings
from .credentials import ENV_VAR, CredentialStore, SessionState
from .discovery import Discovery
from .extract import extract_init_store
from .negcache import NegativeCache
from .transport import (
    Challenged,
    FetchFailed,
    Transport,
    failure_code,
    failure_fields,
    is_policy_change,
    retry_once,
)

log = logging.getLogger(__name__)


@dataclass
class ToolError:
    """A structured error value (SPEC §7 *Error taxonomy*) — never an MCP protocol error."""

    code: str
    message: str
    reason: str | None = None
    retryable: bool | None = None
    http_status: int | None = None
    title: str | None = None
    candidates: list[dict] | None = None

    def as_dict(self) -> dict:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        for k in ("reason", "retryable", "http_status", "title", "candidates"):
            v = getattr(self, k)
            if v is not None:
                out[k] = v
        return out


@dataclass
class Served:
    """A category row that answered, with its provenance and the session's health."""

    envelope: dict
    category_id: int
    rowid: int
    data_tier: str
    fetched_at: str
    cr_url: str | None
    from_cache: bool
    stale: bool
    superseded_at: str | None
    session: str
    warnings: list[str] = field(default_factory=list)
    fetch_kind: str | None = None  # the classification of the fetch this call made, if any


@dataclass
class ReliabilityServed:
    payload: dict
    category_id: int
    fetched_at: str | None
    cr_url: str | None
    from_cache: bool
    stale: bool
    warnings: list[str] = field(default_factory=list)
    # False when CR publishes no survey for this category and said so in its own category
    # payload (`reliabilityURL: false`). `payload` is then empty and must not be parsed: the
    # answer is SPEC §7's structural no-data envelope, which is a success, not an error.
    survey_published: bool = True


class Repository:
    def __init__(
        self,
        settings: Settings,
        cache: Cache,
        transport: Transport,
        discovery: Discovery,
        negcache: NegativeCache,
        credentials: CredentialStore,
        health: SessionState,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.transport = transport
        self.discovery = discovery
        self.negcache = negcache
        self.credentials = credentials
        self.health = health
        self.clock = clock

    # --- resolution -------------------------------------------------------------------
    async def resolve(self, token: Any) -> Resolution | ToolError:
        res = await self.discovery.resolve(token)
        if res.kind == "ambiguous":
            ids = E.quoted_list(res.candidates, lambda c: f"c{c['id']}")
            return ToolError(
                "ambiguous_category",
                f"{E.quoted(token)} matches more than one category: {ids}",
                retryable=False,
                candidates=E.candidates(res.candidates),
            )
        if res.kind == "unknown":
            # the id the cache parsed (None for a slug — a slug is never a group id); the
            # token is never re-parsed here, the grammar is `cache._CID`'s alone
            owner = self.cache.group_owner(res.category_id) if res.category_id is not None else None
            if owner is not None:
                # a display-group id passed as a category — the likeliest source of a bare
                # `cNNNN` that is in no index, since `cr_ratings` prints `group_id` on every
                # block. "Not a category" is true; where it IS is the useful half of the answer.
                # The `reason` still reports discovery honestly: a group id can also be a
                # subcategory URL CR publishes (it then aliases to the parent's payload), so
                # while only one source has run the miss is not final
                final = self.discovery.both_sources_ran()
                group_name, owner_name = (
                    E.quoted(owner["group"]),
                    E.quoted(owner["slug"] or owner["name"]),
                )
                return ToolError(
                    "unknown_category",
                    f"c{owner['group_id']} is a display group ({group_name}) of category "
                    f"c{owner['id']} ({owner_name}), not a category: call the "
                    f"tool on c{owner['id']} and pass group={owner['group_id']} to scope to it",
                    reason="not_in_index" if final else "discovery_incomplete",
                    retryable=not final,
                    candidates=E.candidates([owner]),
                )
            if not self.discovery.az_done():
                failure = self.discovery.az_failure or {"code": "fetch_failed"}
                return ToolError(
                    failure.get("code", "fetch_failed"),
                    "the category index could not be fetched, so the id cannot be validated",
                    reason=failure.get("reason"),
                    retryable=failure.get("retryable"),
                    http_status=failure.get("http_status"),
                    title=failure.get("title"),
                )
            if not self.discovery.both_sources_ran():
                # only one source has run: "not found yet" is a different answer from
                # "not found", and it is structural (SPEC §5), not prose
                return ToolError(
                    "unknown_category",
                    f"{E.quoted(token)} is not in the category index yet; the sitemap pass has not "
                    "completed, so this is not a final answer",
                    reason="discovery_incomplete",
                    retryable=True,
                )
            return ToolError(
                "unknown_category",
                f"{E.quoted(token)} is not a Consumer Reports category id or slug",
                reason="not_in_index",
                retryable=False,
            )
        if res.dead:
            return ToolError(
                "unknown_category",
                f"c{res.category_id} was retired by Consumer Reports (its page returns 404)",
                reason="retired",
                retryable=False,
            )
        return res

    # --- categories -------------------------------------------------------------------
    async def get_category(
        self,
        token: Any,
        *,
        refresh: bool = False,
        validate: bool = True,
        cooldown: bool = True,
    ) -> Served | ToolError:
        """`refresh` forces a fetch past a cache hit — subject to the refresh cooldown unless
        `cooldown=False`, which only the `auth` probe passes: it is a verdict on a credential
        and reads the fetch's classification, so it must always go to the network."""
        now = self.clock()
        canonical_url: str | None
        index_row: dict | None = None
        if validate:
            res = await self.resolve(token)
            if isinstance(res, ToolError):
                return res
            cid = res.category_id
            index_row = res.row
            canonical_url = (index_row or {}).get("canonical_url")
        else:
            # D20: the `auth` probe is a shipped constant, exempt from validate-the-id-first
            cid = int(str(token).lstrip("cC"))
            canonical_url = WWW + AUTH_PROBE_PATH if cid == AUTH_PROBE_CATEGORY else None
            index_row = self.cache.index_row(cid)
            canonical_url = canonical_url or (index_row or {}).get("canonical_url")
        ttl = self.settings.cache_ttl_days
        effective = self.health.effective_tier
        sel = self.cache.select_category(cid, effective, ttl, now)
        if sel is not None:
            # "refetch?" is decided from the last CHECK at a qualifying tier, never from the
            # served row's age: under never-downgrade that row can be a retained scored one,
            # stale on every call for the rest of its life (SPEC §8 *Retention*)
            if not refresh and not self._needs_refresh(sel, ttl, now):
                return self._serve(sel, from_cache=True)
            if refresh and cooldown and self._inside_cooldown(sel, now):
                return self._serve(sel, from_cache=True, warnings=["refresh_skipped"])
        if canonical_url is None:
            existing = self.cache.any_row(cid, ttl, now)
            if existing and existing.final_url:
                canonical_url = existing.final_url
        if canonical_url is None:
            fallback = self.cache.any_row(cid, ttl, now)
            if fallback:
                return self._serve(fallback, from_cache=True, warnings=["refresh_failed:no_url"])
            return ToolError(
                "fetch_failed",
                f"no URL is known for c{cid}",
                reason="url_unresolved",
                retryable=False,
            )
        neg = self.negcache.get(("cat", cid), now)
        if neg is not None:
            code, detail = neg
            return self._fallback(cid, sel, ttl, now, code, detail)
        url = canonical_url

        async def once():
            # the key is re-read per attempt: a sign-in adopted mid-fetch moves the effective
            # tier, and the retry must coalesce under the tier it will actually fetch at
            return await self.transport.single_flight.run(
                ("cat", cid, self.health.effective_tier),
                lambda: self._fetch_category(cid, url, index_row, now),
            )

        try:
            # a `policy_changed` is our own state change (a sign-in landed while this 11 MB
            # request was in flight), never a network condition: one retry, under the new policy
            outcome = await retry_once(once, when=is_policy_change, what=f"c{cid}")
        except (FetchFailed, Challenged) as exc:
            if isinstance(exc, Challenged):  # replayed for an hour; a fetch failure never is
                self.negcache.put(("cat", cid), "challenged", now, **failure_fields(exc))
            return self._fallback(cid, sel, ttl, now, failure_code(exc), failure_fields(exc))
        if isinstance(outcome, ToolError):
            return outcome
        kind, new_rowid, payload_cid, detail = outcome
        if new_rowid is None:  # drift (negatively cached) or a double rejection: fall back
            return self._fallback(cid, sel, ttl, now, kind, detail)
        # the row lives under the PAYLOAD's id (args.cid), which may differ from the one asked for
        effective = self.health.effective_tier  # may have moved (expired) during the fetch
        served = self.cache.select_category(payload_cid, effective, ttl, now) or self.cache.any_row(
            payload_cid, ttl, now
        )
        assert served is not None
        return self._serve(served, from_cache=served.rowid != new_rowid, fetch_kind=kind)

    async def _fetch_category(
        self, cid: int, url: str, index_row: dict | None, now: datetime
    ) -> tuple[str, int | None, int, dict] | ToolError:
        """One network fetch → classify → health → write. Returns (kind, rowid, payload_cid,
        detail) with rowid None for a drift kind (already negatively cached) or a double
        rejection, or a ToolError for a retired id."""
        result = await self.transport.fetch(url)
        if result.status == 404:
            if (index_row or {}).get("source") == "sitemap":
                # a sitemap id is a candidate, not a guarantee: 404 is "retired" (SPEC §5)
                self.cache.mark_dead(cid, now)
                log.warning("c%d returned 404 — marked retired", cid)
                return ToolError(
                    "unknown_category",
                    f"c{cid} was retired by Consumer Reports (its page returns 404)",
                    reason="retired",
                    retryable=False,
                )
            # an index-listed id whose page is gone: CR moved something — drift, negatively cached
            detail = {"http_status": 404, "title": result.title}
            self.negcache.put(("cat", cid), ingest.PAYLOAD_MISSING, now, **detail)
            return ingest.PAYLOAD_MISSING, None, cid, detail
        # True only when the transport asserted `hash` was still in the jar after this response
        credential_present = result.credential_in_jar is True
        c = ingest.classify(result.final_url, result.status, result.content, credential_present)
        if c.kind == ingest.CREDENTIAL_REJECTED:
            # the retry also landed on the login page: nothing to cache, the credential is
            # dead — fall back to the cache like any other failed fetch (SPEC §7). The
            # transport latched the rejection and reported it when it saw the route on the
            # FIRST response; a final URL can only be the login page after that.
            return c.kind, None, cid, {"http_status": result.status, "title": result.title}
        if c.kind in ingest.DRIFT_KINDS:
            detail = {"http_status": result.status, "title": result.title}
            self.negcache.put(("cat", cid), c.kind, now, **detail)
            log.error(
                "schema drift on c%d: %s (status %s, title %r)",
                cid,
                c.kind,
                result.status,
                result.title,
            )
            return c.kind, None, cid, detail
        # only marker-bearing pages move session health (SPEC §7): report the marker and the
        # jar's state as facts; `SessionState` alone chooses the transition
        self.health.on_marker(c.is_member, credential_present=credential_present)
        assert c.filter_instance is not None
        requested_id = self._requested_id(index_row, url)
        envelope = ingest.build_envelope(
            c.filter_instance,
            c.ratings_wrapper,
            data_subscriber=c.marker,
            final_url=result.final_url,
            requested_id=requested_id,
            http_status=result.status,
        )
        payload_cid = ingest.category_id_of(envelope)
        if payload_cid is None:
            # a complete payload with no integer `args.cid`: drift, like a missing payload
            detail = {"http_status": result.status, "title": result.title}
            self.negcache.put(("cat", cid), ingest.PAYLOAD_MISSING, now, **detail)
            log.error("c%d: payload carries no args.cid (title %r)", cid, result.title)
            return ingest.PAYLOAD_MISSING, None, cid, detail
        tier = c.data_tier or "anonymous"
        rowid = self.cache.write_category(
            envelope,
            tier=tier,
            scored=ingest.is_scored(c.filter_instance),
            fetched_at=now,
            ttl_days=self.settings.cache_ttl_days,
        )
        if payload_cid != cid:
            # the requested id was a subcategory: the row is filed under args.cid, aliased
            log.info("c%d served the payload of c%d (aliased)", cid, payload_cid)
        for w in c.warnings:
            log.info("c%d: %s", payload_cid, w)
        return c.kind, rowid, payload_cid, {}

    @staticmethod
    def _requested_id(index_row: dict | None, url: str) -> int | None:
        if index_row and index_row.get("category_id") is not None:
            return int(index_row["category_id"])
        from .discovery import _CAT_PATH

        m = _CAT_PATH.match(url)
        return int(m.group(2)) if m else None

    def _fallback(
        self,
        cid: int,
        sel: Selection | None,
        ttl: int,
        now: datetime,
        code: str,
        detail: dict,
    ) -> Served | ToolError:
        """A cached row is served with `error: null` whenever one exists (SPEC §7)."""
        row = sel or self.cache.any_row(cid, ttl, now)
        if row is not None:
            return self._serve(row, from_cache=True, warnings=[f"refresh_failed:{code}"])
        messages = {
            "payload_missing": "window.filterInstanceDATA is absent or unparseable — schema drift",
            "marker_missing": "neither data-subscriber value is on the page — schema drift",
            "challenged": "the request was challenged or blocked by CR's WAF",
            "fetch_failed": "the fetch failed",
            "credential_rejected": "Consumer Reports rejected the configured session cookie; "
            f"re-run `consumer-reports-mcp auth` (or update {ENV_VAR})",
        }
        return ToolError(
            code,
            messages.get(code, code),
            reason=detail.get("reason"),
            retryable=detail.get("retryable", False if code != "fetch_failed" else None),
            http_status=detail.get("http_status"),
            title=detail.get("title"),
        )

    def _serve(
        self,
        sel: Selection,
        *,
        from_cache: bool,
        warnings: list[str] | None = None,
        fetch_kind: str | None = None,
    ) -> Served:
        return Served(
            envelope=self.cache.load_envelope(sel.rowid),
            category_id=sel.category_id,
            rowid=sel.rowid,
            data_tier=sel.tier,
            fetched_at=sel.fetched_at,
            cr_url=sel.final_url,
            from_cache=from_cache,
            stale=sel.stale,
            superseded_at=sel.superseded_at,
            session=self.health.health.value,
            warnings=list(warnings or []),
            fetch_kind=fetch_kind,
        )

    @staticmethod
    def _needs_refresh(sel: Selection, ttl: int, now: datetime) -> bool:
        """Past the TTL since the last look at a qualifying tier — not since the served row."""
        return is_stale(sel.checked_at or sel.fetched_at, ttl, now)

    @staticmethod
    def _inside_cooldown(sel: Selection, now: datetime) -> bool:
        """A qualifying row younger than `REFRESH_COOLDOWN_S` answers a `refresh` instead of
        the network — the bound on what a prompt-injected refresh loop can cost."""
        last = from_iso(sel.checked_at or sel.fetched_at)
        return last + timedelta(seconds=REFRESH_COOLDOWN_S) > now

    # --- products -----------------------------------------------------------------------
    async def get_product(self, product_id: int, *, refresh: bool = False) -> Served | ToolError:
        now = self.clock()
        ttl = self.settings.cache_ttl_days
        sel = self.cache.select_product_row(product_id, self.health.effective_tier, ttl, now)
        if sel is None:
            return ToolError(
                "unknown_product",
                f"product {product_id} is not in any cached category; run cr_ratings on its "
                "category or cr_search first",
                retryable=False,
            )
        if sel.qualified and not refresh and not self._needs_refresh(sel, ttl, now):
            return self._serve(sel, from_cache=True)
        if sel.qualified and refresh and self._inside_cooldown(sel, now):
            return self._serve(sel, from_cache=True, warnings=["refresh_skipped"])
        # refetch ONLY the category the selection rule chose (SPEC §7)
        outcome = await self.get_category(sel.category_id, refresh=True)
        warnings = (
            outcome.warnings if isinstance(outcome, Served) else [f"refresh_failed:{outcome.code}"]
        )
        again = self.cache.select_product_row(product_id, self.health.effective_tier, ttl, now)
        if again is None:
            return ToolError(
                "unknown_product",
                f"product {product_id} is no longer in c{sel.category_id}",
            )
        from_cache = (
            not isinstance(outcome, Served) or outcome.from_cache or again.rowid != outcome.rowid
        )
        return self._serve(again, from_cache=from_cache, warnings=warnings)

    # --- reliability ------------------------------------------------------------------------
    async def get_reliability(
        self, token: Any, *, refresh: bool = False
    ) -> ReliabilityServed | ToolError:
        now = self.clock()
        res = await self.resolve(token)
        if isinstance(res, ToolError):
            return res
        cid = res.category_id
        ttl = self.settings.cache_ttl_days
        sel = self.cache.select_reliability(cid, ttl, now)
        if sel is not None:
            if not refresh and not sel.stale:
                return self._serve_reliability(sel, from_cache=True)
            if refresh and from_iso(sel.fetched_at) + timedelta(seconds=REFRESH_COOLDOWN_S) > now:
                return self._serve_reliability(sel, from_cache=True, warnings=["refresh_skipped"])
        neg = self.negcache.get(("rel", cid), now)
        if neg is not None:
            return self._rel_fallback(cid, sel, neg[0], neg[1])
        known = self.cache.reliability_url_status(cid)
        url = known.url
        constructed = False
        if known.status == RANGE_NONE:
            # CR's own category payload says there is no survey page. Answering from it costs no
            # request and is the honest answer; guessing a URL here is what produced a 404 and an
            # error telling the caller to cache a URL that does not exist (SPEC §7).
            # provenance is the CATEGORY payload that carried the answer — `seen_at`, not a
            # tier-scoped selection: `reliabilityURL` sits in `args` and is identical in both
            # tiers, so an anonymous row answers a member caller here without a downgrade
            return ReliabilityServed(
                payload={},
                category_id=cid,
                fetched_at=known.seen_at,
                cr_url=None,
                from_cache=True,
                stale=is_stale(known.seen_at, ttl, now) if known.seen_at else False,
                survey_published=False,
            )
        if url is None:
            canonical = (res.row or {}).get("canonical_url")
            if canonical is None:
                return self._rel_fallback(
                    cid,
                    sel,
                    "fetch_failed",
                    {
                        "reason": "url_unresolved",
                        "retryable": False,
                        "message": f"no canonical URL is known for c{cid}; "
                        "run cr_ratings on it first",
                    },
                )
            url = _constructed_reliability_url(canonical, cid)
            constructed = True
        try:
            result = await retry_once(
                lambda: self.transport.single_flight.run(
                    ("rel", cid), lambda: self.transport.fetch(url)
                ),
                when=is_policy_change,
                what=f"reliability c{cid}",
            )
        except (FetchFailed, Challenged) as exc:
            if isinstance(exc, Challenged):
                self.negcache.put(("rel", cid), "challenged", now, **failure_fields(exc))
            return self._rel_fallback(cid, sel, failure_code(exc), failure_fields(exc))
        if result.status == 404 and constructed:
            # a guessed URL failing is not schema drift (SPEC §7)
            return self._rel_fallback(
                cid,
                sel,
                "fetch_failed",
                {
                    "reason": "url_unresolved",
                    "retryable": False,
                    "message": f"the constructed reliability URL for c{cid} returned 404; run "
                    "cr_ratings on the category so the real URL is cached",
                },
            )
        store = extract_init_store(result.content)
        siblings = _reliability_ids(store, cid)
        if store is None or cid not in siblings:
            # absent, unparseable, or a page that describes OTHER categories (a constructed URL
            # that redirected elsewhere): drift for this id, never a fan-out under it
            self.negcache.put(
                ("rel", cid),
                "reliability_payload_missing",
                now,
                http_status=result.status,
                title=result.title,
            )
            return self._rel_fallback(
                cid,
                sel,
                "reliability_payload_missing",
                {"http_status": result.status, "title": result.title},
            )
        self.cache.write_reliability(siblings, store, now, result.final_url, ttl_days=ttl)
        fresh = self.cache.select_reliability(cid, ttl, now)
        assert fresh is not None
        return self._serve_reliability(fresh, from_cache=False)

    def _serve_reliability(self, sel, *, from_cache: bool, warnings=None) -> ReliabilityServed:
        return ReliabilityServed(
            payload=self.cache.load_reliability(sel.rowid),
            category_id=sel.category_id,
            fetched_at=sel.fetched_at,
            cr_url=sel.final_url,
            from_cache=from_cache,
            stale=sel.stale,
            warnings=list(warnings or []),
        )

    def _rel_fallback(
        self, cid: int, sel, code: str, detail: dict
    ) -> ReliabilityServed | ToolError:
        row = sel or self.cache.select_reliability(cid, self.settings.cache_ttl_days, self.clock())
        if row is not None:
            return self._serve_reliability(
                row, from_cache=True, warnings=[f"refresh_failed:{code}"]
            )
        messages = {
            "reliability_payload_missing": "window.initStore is absent or unparseable "
            "— schema drift",
            "challenged": "the request was challenged or blocked by CR's WAF",
            "fetch_failed": detail.get("message", "the fetch failed"),
        }
        return ToolError(
            code,
            messages.get(code, code),
            reason=detail.get("reason"),
            retryable=detail.get("retryable", False if code != "fetch_failed" else None),
            http_status=detail.get("http_status"),
            title=detail.get("title"),
        )


def _constructed_reliability_url(canonical_url: str, cid: int) -> str:
    base = canonical_url.split("?")[0].rstrip("/")
    parts = base.split("/")
    if parts and parts[-1].lower() == f"c{cid}":
        parts = parts[:-1]
    return "/".join(parts) + f"/reliability/c{cid}/"


def _reliability_ids(store: dict | None, cid: int) -> list[int]:
    """Every sibling id the payload names — from the payload being parsed, never a count. A
    non-numeric `_id` is fetched content that does not name a category: skipped, never raised
    out of `get_reliability` past the error taxonomy."""
    if not isinstance(store, dict):
        return []
    data = store.get("data")
    if not isinstance(data, dict):
        return []
    ids: list[int] = []
    cat = data.get("category")
    own = ingest._int(cat.get("_id")) if isinstance(cat, dict) else None
    if own is not None:
        ids.append(own)
    for c in data.get("categories") or []:
        sib = ingest._int(c.get("_id")) if isinstance(c, dict) else None
        if sib is not None and sib not in ids:
            ids.append(sib)
    return ids
