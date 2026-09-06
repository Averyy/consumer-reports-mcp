"""Category discovery: the A-Z index (names) plus the sitemaps (coverage) — PLAN P5, SPEC §5.

The A-Z index lists 236 categories; the sitemaps list 346. `unknown_category` is only correct
once BOTH have run. The sitemap pass (195 fetches ≈ 6.5 min at the politeness interval) runs in
the background at startup; a lookup that misses the index AWAITS it rather than refusing (D6).
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .cache import Cache, Resolution, from_iso, utcnow
from .config import AZ_INDEX_URL, PRODUCTS_SITEMAP_URL, REFRESH_COOLDOWN_S, WWW

if TYPE_CHECKING:
    from .transport import Transport

log = logging.getLogger(__name__)

# A miss AWAITS the sitemap pass (SPEC §5) — but the pass is 195 fetches, about 6.5 minutes,
# and Claude Desktop kills a local tool call at 60 s. Unbounded, a cold-start lookup of any of
# the 110 sitemap-only ids — TVs c28700, Mattresses c28705, Dishwashers c28687, the ones people
# actually ask for — was a dead tool call rather than the retryable `discovery_incomplete` the
# design promises. Bounded, the caller gets that answer inside its deadline and the pass keeps
# running, so the next call (or a retry) resolves. Well under 60 s so a slow fetch and the
# response still fit.
SITEMAP_AWAIT_TIMEOUT_S = 25.0

SOURCE_AZ = "az"
SOURCE_SITEMAP = "sitemap"

_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
_HREF = re.compile(r"""(?<![-\w])href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
_CAT_PATH = re.compile(r"^(?:https?://[^/]+)?(/.*?/c(\d{4,7})/?)(?:[?#].*)?$", re.I)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_CAT_LOC = re.compile(r"/c(\d{4,7})/?$")


@dataclass(frozen=True)
class IndexRow:
    id: int
    path: str  # root-relative, normalised, trailing slash
    display_name: str | None = None

    @property
    def canonical_url(self) -> str:
        return WWW + self.path

    @property
    def slug(self) -> str | None:
        return slug_of(self.path)

    @property
    def franchise(self) -> str | None:
        return franchise_of(self.path)

    def as_index(self) -> dict:
        return {
            "id": self.id,
            "path": self.path,
            "canonical_url": self.canonical_url,
            "display_name": self.display_name,
            "slug": self.slug,
            "franchise": self.franchise,
        }


def _normalise_path(href: str) -> str | None:
    m = _CAT_PATH.match(href.strip())
    if not m:
        return None
    path = m.group(1)
    if not path.endswith("/"):
        path += "/"
    return path


def slug_of(path: str) -> str | None:
    """The last path segment before `/cNNNN/` (SPEC §7)."""
    parts = [p for p in path.split("?")[0].split("/") if p]
    if len(parts) >= 2 and re.fullmatch(r"c\d{4,7}", parts[-1], re.I):
        return parts[-2]
    return None


def franchise_of(path: str) -> str | None:
    """The first path segment (D17): the six products under `/cars/` report `cars`."""
    parts = [p for p in path.split("?")[0].split("/") if p]
    return parts[0] if parts else None


# --------------------------------------------------------------------------- parsers (pure)


def parse_az_index(html: bytes | str) -> list[IndexRow]:
    """Every `<a href=…/cNNNN/…>` with its inner text, tags stripped, entities unescaped.

    The anchor text sits inside a nested <span> (RECON §9h), so `>([^<]+)</a>` finds 2 of 236;
    hrefs are a mix of absolute and root-relative.
    """
    text = html.decode("utf-8", "replace") if isinstance(html, bytes) else html
    rows: dict[int, IndexRow] = {}
    for m in _ANCHOR.finditer(text):
        attrs, inner = m.group(1), m.group(2)
        h = _HREF.search(attrs)
        if not h:
            continue
        href = h.group(1) or h.group(2) or h.group(3) or ""
        path = _normalise_path(href)
        if path is None:
            continue
        cid = int(_CAT_PATH.match(href.strip()).group(2))
        name = _WS.sub(" ", html_lib.unescape(_TAG.sub("", inner))).strip() or None
        existing = rows.get(cid)
        if existing is None:
            rows[cid] = IndexRow(cid, path, name)
            continue
        # the same id can appear under a longer path (`/recommended/cNNNN/`): keep the shortest
        # path (the canonical one) and the first non-empty name
        best_path = path if len(path) < len(existing.path) else existing.path
        rows[cid] = IndexRow(cid, best_path, existing.display_name or name)
    return list(rows.values())


def parse_sitemap_index(xml: bytes | str) -> list[str]:
    text = xml.decode("utf-8", "replace") if isinstance(xml, bytes) else xml
    return [html_lib.unescape(u) for u in _LOC.findall(text)]


def parse_category_sitemap(xml: bytes | str) -> list[IndexRow]:
    """Category URLs (`…/cNNNN/`) from a per-supercategory sitemap. Several URLs can carry the
    same id (`/recommended/cNNNN/`); the SHORTEST path is the canonical one."""
    text = xml.decode("utf-8", "replace") if isinstance(xml, bytes) else xml
    best: dict[int, str] = {}
    for loc in _LOC.findall(text):
        loc = html_lib.unescape(loc)
        if not _CAT_LOC.search(loc.rstrip("/") + "/"):
            continue
        path = _normalise_path(loc)
        if path is None:
            continue
        cid = int(_CAT_PATH.match(loc.strip()).group(2))
        if cid not in best or len(path) < len(best[cid]):
            best[cid] = path
    return [IndexRow(cid, path) for cid, path in best.items()]


# --------------------------------------------------------------------------- orchestration


class Discovery:
    """Two passes over the shared session, recorded per source in `discovery_runs`."""

    def __init__(self, cache: Cache, transport: Transport, index_ttl_days: int) -> None:
        self.cache = cache
        self.transport = transport
        self.index_ttl_days = index_ttl_days
        self._sitemap_task: asyncio.Task | None = None
        self._sitemap_lock = asyncio.Lock()
        self._sitemap_started_at: float | None = None  # monotonic; the retry cooldown reads it
        self._closed = False  # `cancel()` ran: shutdown, no pass may start after it
        self.az_error: str | None = None
        self.az_failure: dict | None = None  # {code, reason?, retryable?} of the last A-Z failure
        self.sitemap_errors: list[str] = []  # the first MAX_RECORDED_ERRORS, for diagnostics
        self.sitemap_failed = 0  # every failure, counted — the health ratio reads this

    # --- status ---------------------------------------------------------------------
    def status(self) -> dict[str, dict]:
        return self.cache.discovery_status()

    def _fresh(self, source: str, now: datetime) -> bool:
        run = self.status().get(source)
        if not run:
            return False
        return from_iso(run["fetched_at"]) > now - _days(self.index_ttl_days)

    def az_done(self) -> bool:
        return SOURCE_AZ in self.status()

    def sitemap_done(self) -> bool:
        return SOURCE_SITEMAP in self.status()

    @property
    def sitemap_pending(self) -> bool:
        return self._sitemap_task is not None and not self._sitemap_task.done()

    def index_provenance(self) -> dict:
        run = self.status().get(SOURCE_AZ)
        return {"fetched_at": run["fetched_at"] if run else None, "cr_url": AZ_INDEX_URL}

    # --- the A-Z index ---------------------------------------------------------------
    async def ensure_az_index(self, *, refresh: bool = False, now: datetime | None = None) -> bool:
        """One fetch, synchronous, recorded as source `az`. Returns whether a fetch happened."""
        now = now or utcnow()
        if not refresh and self._fresh(SOURCE_AZ, now):
            return False
        from .transport import Challenged, FetchFailed, failure_code, failure_fields

        try:
            result = await self.transport.fetch(AZ_INDEX_URL)
        except (FetchFailed, Challenged) as exc:
            self.az_error = str(exc)
            self.az_failure = {"code": failure_code(exc), **failure_fields(exc)}
            log.warning("A-Z index fetch failed: %s", exc)
            return False
        rows = parse_az_index(result.content)
        if not rows:
            # the anchors moved: schema drift on the index, not a network failure
            self.az_error = "a-z index parsed to zero rows"
            self.az_failure = {
                "code": "payload_missing",
                "retryable": False,
                "http_status": result.status,
                "title": result.title,
            }
            log.error("A-Z index parsed to zero rows (title=%r) — schema drift", result.title)
            return False
        self.az_error = None
        self.az_failure = None
        self.cache.upsert_category_index((r.as_index() for r in rows), SOURCE_AZ, now)
        self.cache.record_discovery_run(SOURCE_AZ, now, len(rows))
        return True

    # --- the sitemap pass --------------------------------------------------------------
    MAX_CONSECUTIVE_CHALLENGES = 3
    MAX_FAILURE_RATIO = 0.1
    # `products.xml` is fetched content: how many `<loc>`s it names is CR's to choose, and so
    # would be the length of the pass and of the error list. 195 measured; ~5× headroom.
    MAX_SITEMAPS = 1000
    MAX_RECORDED_ERRORS = 50

    async def run_sitemap_pass(self, *, now: datetime | None = None) -> int:
        """195 small XML fetches through the shared (rate-limited) session.

        Rows are upserted per sitemap so an interrupted pass keeps what it found. Individual
        failures are logged and skipped; consecutive WAF challenges abort the pass (politeness);
        the run is RECORDED — and so treated as authoritative for `unknown_category` — only when
        the index itself was fetched and at most 10% of the sitemaps failed.
        """
        async with self._sitemap_lock:
            return await self._sitemap_pass(now or utcnow())

    async def _sitemap_pass(self, now: datetime) -> int:
        from .transport import Challenged, FetchFailed, is_policy_change, retry_once

        self.sitemap_errors = []
        self.sitemap_failed = 0
        try:
            # `products.xml` is the one fetch the whole pass hangs on: unfetched, nothing is
            # recorded and `sitemap_done()` stays false for the PROCESS. So a retryable failure
            # — a `policy_changed` from a sign-in adopted during the first seconds of startup
            # above all, but a timeout or a 5xx too — gets exactly one more attempt.
            index = await retry_once(
                lambda: self.transport.fetch(PRODUCTS_SITEMAP_URL),
                when=lambda exc: exc.retryable,
                what="products.xml",
            )
        except (FetchFailed, Challenged) as exc:
            self._sitemap_error(f"products.xml: {exc}")
            log.warning("sitemap index fetch failed: %s", exc)
            return 0
        urls = parse_sitemap_index(index.content)
        if len(urls) > self.MAX_SITEMAPS:
            log.warning(
                "products.xml names %d sitemaps; walking the first %d", len(urls), self.MAX_SITEMAPS
            )
            urls = urls[: self.MAX_SITEMAPS]
        found: dict[int, IndexRow] = {}
        consecutive_challenges = 0
        aborted = False
        for url in urls:
            try:
                # a single sitemap lost to `policy_changed` would drop its categories from the
                # index for a TTL while the pass is still recorded as authoritative — so a
                # confident `not_in_index` for a category that exists. Our own state change
                # is retried once; a network failure keeps the skip-and-count rule below.
                page = await retry_once(
                    lambda url=url: self.transport.fetch(url), when=is_policy_change, what=url
                )
            except Challenged as exc:
                self._sitemap_error(f"{url}: {exc}")
                consecutive_challenges += 1
                if consecutive_challenges >= self.MAX_CONSECUTIVE_CHALLENGES:
                    log.warning(
                        "sitemap pass aborted after %d consecutive challenges",
                        consecutive_challenges,
                    )
                    aborted = True
                    break
                continue
            except FetchFailed as exc:
                # an off-host `<loc>` lands here too: the transport refuses it unfetched
                self._sitemap_error(f"{url}: {exc}")
                log.warning("sitemap %s failed: %s", url, exc)
                continue
            consecutive_challenges = 0
            rows = parse_category_sitemap(page.content)
            fresh = []
            for row in rows:
                prev = found.get(row.id)
                if prev is None or len(row.path) < len(prev.path):
                    found[row.id] = row
                    fresh.append(row)
            if fresh:
                self.cache.upsert_category_index((r.as_index() for r in fresh), SOURCE_SITEMAP, now)
        failed = self.sitemap_failed
        healthy = not aborted and urls and failed <= self.MAX_FAILURE_RATIO * len(urls)
        if healthy:
            self.cache.record_discovery_run(SOURCE_SITEMAP, now, len(found))
        else:
            log.warning(
                "sitemap pass NOT recorded as complete: %d of %d sitemaps failed%s",
                failed,
                len(urls),
                " (aborted)" if aborted else "",
            )
        log.info(
            "sitemap pass: %d categories from %d sitemaps (%d failed)",
            len(found),
            len(urls),
            failed,
        )
        return len(found)

    def _sitemap_error(self, message: str) -> None:
        self.sitemap_failed += 1
        if len(self.sitemap_errors) < self.MAX_RECORDED_ERRORS:
            self.sitemap_errors.append(message)

    # A pass that ended WITHOUT being recorded — offline at startup, `products.xml` failing
    # twice, three consecutive challenges — is retried, but not sooner than this after the
    # previous attempt started: the same bound the refresh loop has (`REFRESH_COOLDOWN_S`),
    # so a burst of misses while offline costs one attempt, not one per miss, and an aborted
    # pass does not walk back into a WAF that is blocking it every few seconds.
    RETRY_COOLDOWN_S = REFRESH_COOLDOWN_S

    def start_background_sitemap_pass(self, *, force: bool = False) -> asyncio.Task | None:
        """The one entry point for the pass: the lifespan at startup, a lookup that misses
        (`resolve`), and `cr_categories(refresh=True)` all come through here.

        Returns the running task while one is in flight; `None` when the source is fresh (a
        warm cache within the index TTL skips the pass entirely, D6) or while the retry
        cooldown holds; otherwise starts a pass. So a pass that ended unrecorded is not the
        end of discovery for the process — an earlier shape returned the finished task object
        forever, and `sitemap_done()` stayed false until restart however long ago the network
        had come back. `force` starts one regardless of freshness and cooldown."""
        task = self._sitemap_task
        if task is not None and not task.done():
            return task
        if self._closed:
            return None
        if not force:
            if self._fresh(SOURCE_SITEMAP, utcnow()):
                return None
            started = self._sitemap_started_at
            if started is not None and time.monotonic() - started < self.RETRY_COOLDOWN_S:
                return None
        if task is not None:
            log.info("sitemap pass ended unrecorded; starting another")
        self._sitemap_started_at = time.monotonic()
        self._sitemap_task = asyncio.create_task(self._guarded_pass(), name="cr-sitemap-pass")
        return self._sitemap_task

    async def _guarded_pass(self) -> int:
        try:
            return await self.run_sitemap_pass()
        except Exception as exc:  # never let a background failure kill the server
            log.error("sitemap pass crashed: %s", type(exc).__name__)
            self._sitemap_error(f"crash: {type(exc).__name__}")
            return 0

    async def cancel(self) -> None:
        """Stop a running background pass (server shutdown). Rows already upserted stay, and no
        pass starts after this. Only a cancellation of the CALLER — a lifespan torn down under
        this call — propagates: the task is shielded, so the `CancelledError` this call sees is
        the task's own only once the task is done (the same shape as `SignInFlow.cancel`)."""
        self._closed = True
        task = self._sitemap_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():  # the caller's own cancellation, not the task's: propagate
                raise
        except Exception:
            pass

    async def await_sitemap_pass(self, timeout: float | None = None) -> bool:
        """Wait for the running pass. False when `timeout` elapsed first and it is still going.

        The wait is bounded because the caller is a tool call with a deadline; the PASS is not
        cancelled by giving up on it — `shield` keeps it running, and `wait_for` cancels only
        the wrapper — so the work continues and the next lookup finds it done."""
        task = self._sitemap_task
        if task is None or task.done():
            return True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except TimeoutError:
            return False
        except Exception:
            pass
        return True

    # --- resolution ----------------------------------------------------------------------
    async def resolve(self, token: Any, *, now: datetime | None = None) -> Resolution:
        """`cache.resolve_category` → if unknown and the sitemap source is not authoritative,
        (re)start the pass, await it and retry. `unknown` is only returned once both sources
        have run (SPEC §5): a miss is exactly what the pass exists to answer, so a pass that
        ended unrecorded is started again here (subject to the retry cooldown) rather than
        answering `discovery_incomplete` for the rest of the process."""
        res = self.cache.resolve_category(token)
        if res.kind != "unknown":
            return res
        if not self.az_done():
            await self.ensure_az_index(now=now)
            res = self.cache.resolve_category(token)
            if res.kind != "unknown":
                return res
        if self.sitemap_done():
            return res
        if self.sitemap_pending:
            if not await self.await_sitemap_pass(SITEMAP_AWAIT_TIMEOUT_S):
                return res  # still running: `discovery_incomplete`, retryable, pass still going
        elif self._sitemap_task is not None and self.az_done():
            # a pass ran in this process and ended unrecorded: retry it (subject to the
            # cooldown) rather than answer `discovery_incomplete` for the rest of the process.
            # Only RETRY, never initiate — launching the pass is the lifespan's, so a miss with
            # no pass ever launched stays a request-free `discovery_incomplete`; and only once
            # the A-Z source is authoritative — with the index itself unfetched (offline) the
            # answer is `fetch_failed` regardless, and 195 more requests would not change it
            if self.start_background_sitemap_pass() is None:
                return res
            if not await self.await_sitemap_pass(SITEMAP_AWAIT_TIMEOUT_S):
                return res
        else:
            return res
        return self.cache.resolve_category(token)

    def warnings(self) -> list[str]:
        """`sitemap_pass_pending` whenever the sitemap source is not yet authoritative — running,
        not started, or failed — so a caller can tell "not found yet" from "not found"."""
        if self.sitemap_pending or not self.sitemap_done():
            return ["sitemap_pass_pending"]
        return []

    def both_sources_ran(self) -> bool:
        return self.az_done() and self.sitemap_done()


def _days(n: int):
    from datetime import timedelta

    return timedelta(days=n)


def index_rows_from(rows: Iterable[IndexRow]) -> list[dict]:
    return [r.as_index() for r in rows]
