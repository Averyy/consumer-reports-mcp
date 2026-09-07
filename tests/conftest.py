"""Shared test scaffolding (PLAN P0.4).

Page factories emit the markup the extractors read; a fixture's TIER is decided solely by the
`data-subscriber` value the factory writes, never by whether scores are filled. FakeWaferSession
scripts responses and exceptions and records every request, so no test touches the network.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
SYNTH_API_KEY = "SYNTHETICKEY0000000000000000000000000ABCD"[:40]
WWW = "https://www.consumerreports.org"


# --------------------------------------------------------------------------- fixtures on disk


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- page factories


def fill_scores_in(filter_instance: dict) -> dict:
    """Deterministic synthetic member-tier data — generated at test time, never written to disk."""
    fi = copy.deepcopy(filter_instance)
    for pid_str, product in fi["data"].items():
        pid = int(pid_str)
        product["overallDisplayScore"] = 50 + pid % 30
        for entry in product.get("attrs", []):
            if entry.get("attributeTypeName") == "numeric-rating-score":
                entry["value"] = 1 + (pid + int(entry["attributeId"])) % 5
    return fi


def ratings_wrapper_of(fixture: dict) -> dict:
    """Rebuild the `console.log('[ ratings-wrapper ]', {...})` object from a category fixture."""
    cat = dict(fixture["cat"])
    cat["categoryAttributes"] = fixture["category_attributes"]
    return {
        "productFilterPayload": {"debug": {"categories": fixture["siblings"]}},
        "models": [],
        "scat": fixture["scat"],
        "cat": cat,
        "subcats": fixture["subcats"],
        "modelsGrouped": [],
    }


SIGN_OUT_MARKUP = (
    '<nav class="account-nav" hidden><a href="/ec/logout">Sign Out</a></nav>\n'
    '<span class="account-nav__item">Sign Out</span>\n'
)


def make_category_page(
    fixture: dict,
    *,
    subscriber: str | None = "false",
    fill_scores: bool = False,
    omit_ratings_wrapper: bool = False,
    omit_payload: bool = False,
    title: str | None = None,
    marker_count: int = 3,
    both_markers: bool = False,
) -> bytes:
    """A category page. `subscriber` is "true" | "false" | None (no marker at all)."""
    fi = fill_scores_in(fixture["filter_instance"]) if fill_scores else fixture["filter_instance"]
    title = title if title is not None else fixture.get("title", "Ratings - Consumer Reports")
    parts = [f"<!DOCTYPE html>\n<html><head><title>{title}</title></head><body>\n", SIGN_OUT_MARKUP]
    if subscriber is not None:
        for _ in range(marker_count):
            parts.append(f'<div class="cr-header" data-subscriber="{subscriber}"></div>\n')
    if both_markers:
        parts.append('<div data-subscriber="true"></div><div data-subscriber="false"></div>\n')
    parts.append("<script>\n")
    if not omit_payload:
        parts.append("window.filterInstanceDATA = " + json.dumps(fi) + ";\n")
    if not omit_ratings_wrapper:
        wrapper = json.dumps(ratings_wrapper_of(fixture))
        parts.append("console.log('[ ratings-wrapper ]', " + wrapper + ")\n")
    parts.append("</script>\n</body></html>\n")
    return "".join(parts).encode("utf-8")


def make_maintenance_page(title: str = "Site Maintenance") -> bytes:
    return (
        f"<!DOCTYPE html><html><head><title>{title}</title></head><body>"
        "<h1>We'll be back soon</h1></body></html>"
    ).encode()


def make_login_page() -> bytes:
    """The 51 KB login page CR serves for a rejected `hash` — no payload, no marker (RECON §10b)."""
    body = "<form action='/ec/login'><input name='username'><input name='password'></form>"
    return (
        "<!DOCTYPE html><html><head><title>Sign In - Consumer Reports</title></head><body>"
        + body
        + "</body></html>"
    ).encode()


def make_reliability_page(fixture: dict) -> bytes:
    """A reliability page: `window.initStore`, no `data-subscriber` of either value (RECON §9d)."""
    title = fixture.get("title", "Reliability - Consumer Reports")
    return (
        f"<!DOCTYPE html><html><head><title>{title}</title></head><body>\n"
        + SIGN_OUT_MARKUP
        + "<script>window.initStore = "
        + json.dumps(fixture["init_store"])
        + ";</script>\n</body></html>\n"
    ).encode("utf-8")


def make_car_page(is_subscriber: bool | None, api_key: str | None = SYNTH_API_KEY) -> bytes:
    env = {"DRS_CARS_API_BASE": "https://cars-api.consumerreports.org/api/cars/"}
    if api_key is not None:
        env["DRS_CARS_API_API_KEY"] = api_key
    init_store = json.dumps({"env": env, "modelYear": 2026}, separators=(",", ":"))
    init_store = init_store.replace("/", "\\/")
    marker = ""
    if is_subscriber is not None:
        marker = f"        window.isSubscriber = {'true' if is_subscriber else 'false'};\n"
    return (
        "<!DOCTYPE html><html><head><title>Car Reviews - Consumer Reports</title></head><body>\n"
        + SIGN_OUT_MARKUP
        + "<script>\n"
        + f"        window.initStore = {init_store};\n"
        + "        window.isAnonymous = null; // getUserInfo.ts will set it to true/false\n"
        + marker
        + "</script>\n</body></html>\n"
    ).encode("utf-8")


# --------------------------------------------------------------------------- fake wafer


class FakeResponse:
    """Exactly the WaferResponse attributes transport.py reads — nothing more."""

    def __init__(
        self,
        *,
        status_code: int = 200,
        url: str,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
        history: list[tuple[int, str]] | None = None,
        cookies: dict[str, str] | None = None,
        challenge_type: str | None = None,
        elapsed: float = 0.01,
        set_cookies: dict[str, str] | None = None,
        clear_cookies: tuple[str, ...] = (),
    ) -> None:
        self.status_code = status_code
        self.url = url
        self.content = content
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}
        self.history = history or []
        self.cookies = cookies or {}
        self.challenge_type = challenge_type
        self.elapsed = elapsed
        # test-only: how this response mutates the fake jar when received (re-mint / rejection)
        self.set_cookies = set_cookies or {}
        self.clear_cookies = clear_cookies

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


Scripted = FakeResponse | BaseException | Callable[[str, dict], "FakeResponse | BaseException"]


class FakeWaferSession:
    """Scripted stand-in for wafer.AsyncSession.

    `queue` answers requests in order; `routes` maps a URL (exact) or a predicate to a response,
    an exception, or a callable producing either. Every request is logged in `.requests`.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.queue: list[Scripted] = []
        self.routes: list[tuple[Callable[[str], bool], Scripted]] = []
        self.requests: list[tuple[str, dict]] = []
        self._jar: dict[str, dict[str, Any]] = {}
        self.add_cookie_calls: list[tuple[str, str]] = []
        self.closed = False  # set by `__aexit__`, the only teardown wafer 0.5.0 offers

    async def __aexit__(self, *exc: Any) -> bool:
        self.closed = True
        return False

    # scripting -----------------------------------------------------------------
    def push(self, *items: Scripted) -> None:
        self.queue.extend(items)

    def route(self, match: str | Callable[[str], bool], item: Scripted) -> None:
        """Newest route wins: re-routing a URL replaces what an earlier route would answer."""
        pred = (lambda u, m=match: u == m or u.startswith(m)) if isinstance(match, str) else match
        self.routes.insert(0, (pred, item))

    def set_jar(self, name: str, value: str, *, domain: str = ".consumerreports.org") -> None:
        self._jar[name] = {
            "value": value,
            "domain": domain.lstrip("."),
            "secure": True,
            "path": "/",
            "host_only": False,
        }

    def drop(self, name: str) -> None:
        self._jar.pop(name, None)

    @property
    def jar_names(self) -> list[str]:
        return sorted(self._jar)

    # wafer surface ---------------------------------------------------------------
    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((url, kwargs))
        item: Scripted | None = None
        for pred, candidate in self.routes:
            if pred(url):
                item = candidate
                break
        if item is None:
            if not self.queue:
                raise AssertionError(f"FakeWaferSession: unexpected request {url}")
            item = self.queue.pop(0)
        if callable(item) and not isinstance(item, FakeResponse | BaseException):
            item = item(url, kwargs)
            if hasattr(item, "__await__"):
                item = await item
        if isinstance(item, BaseException):
            raise item
        for name, value in item.set_cookies.items():
            self.set_jar(name, value)
        for name in item.clear_cookies:
            self.drop(name)
        return item

    def add_cookie(self, raw_set_cookie: str, url: str) -> None:
        self.add_cookie_calls.append((raw_set_cookie, url))
        parts = [p.strip() for p in raw_set_cookie.split(";")]
        name, _, value = parts[0].partition("=")
        attrs = {k.strip().lower(): v for k, _, v in (p.partition("=") for p in parts[1:])}
        host = _host_of(url)
        domain = attrs.get("domain")
        if attrs.get("max-age") == "0" or "1970" in attrs.get("expires", ""):
            self._jar.pop(name, None)  # an expired Set-Cookie evicts, as wafer/wreq does
            return
        self._jar[name] = {
            "value": value,
            "domain": (domain or host).lstrip(".").lower(),
            "secure": any(p.lower() == "secure" for p in parts[1:]),
            "path": attrs.get("path", "/"),
            "host_only": domain is None,  # no Domain attribute → offered to the exact host only
        }

    def get_cookie(self, name: str, url: str) -> str | None:
        """RFC 6265 scoping as wafer documents it: host-only cookies match the exact host, Domain
        cookies match the host and its subdomains; Secure cookies need https; Path must prefix."""
        entry = self._jar.get(name)
        if entry is None:
            return None
        if entry["secure"] and not url.startswith("https://"):
            return None
        host = _host_of(url)
        if entry["host_only"]:
            if host != entry["domain"]:
                return None
        elif not (host == entry["domain"] or host.endswith("." + entry["domain"])):
            return None
        path = (
            "/" + url.split("//", 1)[-1].split("/", 1)[1] if "/" in url.split("//", 1)[-1] else "/"
        )
        if not path.startswith(entry["path"]):
            return None
        return entry["value"]

    def cookie_scope_summary(self, url: str) -> list[dict]:
        return [
            {"name": n, "domain": e["domain"], "path": e["path"], "secure": e["secure"]}
            for n, e in self._jar.items()
        ]


def _host_of(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0].split(":")[0].lower()


def html_response(url: str, body: bytes, **kw: Any) -> FakeResponse:
    return FakeResponse(url=url, content=body, **kw)


def json_response(url: str, obj: Any, **kw: Any) -> FakeResponse:
    kw.setdefault("headers", {"content-type": "application/json"})
    return FakeResponse(url=url, content=json.dumps(obj).encode(), **kw)


async def until(pred: Callable[[], bool], *, timeout_s: float = 5.0) -> None:
    """Yield to the loop until `pred()` holds — the wait before an assertion about what is on
    the wire. A fixed `asyncio.sleep(0.02)` there lost the race on a Windows CI runner whose
    SQLite writes alone took longer; a condition cannot lose it."""
    deadline = time.monotonic() + timeout_s
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("the awaited condition never held")
        await asyncio.sleep(0.001)


# --------------------------------------------------------------------------- pytest fixtures


@pytest.fixture
def fake_session() -> FakeWaferSession:
    return FakeWaferSession()


@pytest.fixture
def c37162() -> dict:
    return load_fixture("category_c37162.json")


@pytest.fixture
def banks() -> dict:
    return load_fixture("category_c37154_banks.json")


@pytest.fixture
def c200228() -> dict:
    return load_fixture("category_c200228.json")


@pytest.fixture
def reliability_fixture() -> dict:
    return load_fixture("reliability_c37162.json")


# --------------------------------------------------------------------------- envelopes


def fixture_envelope(
    fixture: dict | str,
    *,
    fill_scores: bool = False,
    requested_id: int | None = None,
    with_wrapper: bool = True,
) -> dict:
    """An ingest envelope built from a category fixture through the real ingest code."""
    from consumer_reports_mcp.ingest import build_envelope

    fx = load_fixture(fixture) if isinstance(fixture, str) else fixture
    fi = fill_scores_in(fx["filter_instance"]) if fill_scores else fx["filter_instance"]
    return build_envelope(
        fi,
        ratings_wrapper_of(fx) if with_wrapper else None,
        data_subscriber="true" if fill_scores else "false",
        final_url=fx["final_url"],
        requested_id=requested_id if requested_id is not None else fx.get("requested_id"),
        http_status=200,
    )


# --------------------------------------------------------------------------- runtime


class RuntimeHarness:
    """A full Runtime over a tmp cr.db, a tmp config dir and one FakeWaferSession."""

    def __init__(self, tmp_path: Path, *, cookie: bool = False, index: bool = True) -> None:
        from datetime import UTC, datetime

        from consumer_reports_mcp.config import Settings
        from consumer_reports_mcp.credentials import CredentialStore
        from consumer_reports_mcp.runtime import build_runtime

        self.now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
        self.sess = FakeWaferSession()
        # the fake session never waited between requests; now that the interval is the
        # transport's own gate, the harness has to opt out of it explicitly
        settings = Settings(env={"CR_MIN_REQUEST_INTERVAL_S": "0"}, home=tmp_path)
        store = CredentialStore(tmp_path / "session.json", env={})
        if cookie:
            store.save({"hash": "h" * 36, "userLicenses": "old"})
        self.rt = build_runtime(
            settings,
            session_factory=lambda **kw: self.sess,
            clock=lambda: self.now,
            credentials=store,
        )
        if index:
            self.seed_index()

    def seed_index(self) -> None:
        rows = [
            {
                "id": 37162,
                "path": "/appliances/refrigerators/french-door-refrigerator/c37162/",
                "display_name": "French-Door Refrigerators",
            },
            {
                "id": 28722,
                "path": "/appliances/refrigerators/top-freezer-refrigerator/c28722/",
                "display_name": "Top-Freezer Refrigerators",
            },
            {
                "id": 37154,
                "path": "/money/banks-credit-unions/banks/c37154/",
                "display_name": "Banks",
            },
            {
                "id": 200228,
                "path": "/health/milk-milk-alternatives/c200228/",
                "display_name": "Plant Milk",
            },
            {"id": 28700, "path": "/electronics-computers/tvs/c28700/"},
        ]
        self.rt.cache.upsert_category_index(rows, "az", self.now)
        self.rt.cache.record_discovery_run("az", self.now, len(rows))
        self.rt.cache.record_discovery_run("sitemap", self.now, len(rows))

    def route_page(self, fixture: dict, **kw: Any) -> None:
        url = kw.pop("url", fixture["final_url"])
        self.sess.route(url, FakeResponse(url=url, content=make_category_page(fixture, **kw)))

    @property
    def requests(self) -> list[str]:
        return [u for u, _ in self.sess.requests]


@pytest.fixture
def harness(tmp_path) -> RuntimeHarness:
    return RuntimeHarness(tmp_path)


# --------------------------------------------------------------------------- loop responsiveness


class LoopHeartbeat:
    """The longest gap between loop ticks while the body ran — synchronous work on the loop shows
    up as a gap the length of that work, an idle or busy-but-yielding loop as ~`tick_s`."""

    def __init__(self, tick_s: float = 0.02) -> None:
        self.tick_s = tick_s
        self.max_gap = 0.0
        self._stop: Any = None
        self._task: Any = None

    async def __aenter__(self) -> LoopHeartbeat:
        import asyncio

        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._stop.set()
        await self._task

    async def _run(self) -> None:
        import asyncio
        import time

        last = time.monotonic()
        while not self._stop.is_set():
            await asyncio.sleep(self.tick_s)
            now = time.monotonic()
            self.max_gap = max(self.max_gap, now - last)
            last = now
