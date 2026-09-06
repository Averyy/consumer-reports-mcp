"""P4 — the only wafer-facing code (SPEC §6 1–3, §7 error taxonomy, RECON §10b/§10c/§10i)."""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path

import pytest
import wafer

from consumer_reports_mcp import transport as tp
from consumer_reports_mcp.config import WWW, Settings
from consumer_reports_mcp.credentials import CredentialStore, SessionHealth, SessionState
from consumer_reports_mcp.transport import Challenged, FetchFailed, FetchResult, Transport
from tests.conftest import FakeResponse, FakeWaferSession, make_category_page, make_login_page

HASH = "h" * 36
CAT_URL = f"{WWW}/appliances/refrigerators/french-door-refrigerator/c37162/"
LOGIN_URL = "https://secure.consumerreports.org/ec/login?error"
CARS_URL = "https://cars-api.consumerreports.org/api/cars/v2/cr/carTypes"


class Factory:
    """Records constructor kwargs and hands out one FakeWaferSession."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.session = FakeWaferSession()

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        self.session.kwargs = kwargs
        return self.session


def make_transport(tmp_path: Path, *, cookie: bool, env: dict | None = None):
    # no politeness interval unless a test asks for one: the gate is real now, the session fake
    settings = Settings(env={"CR_MIN_REQUEST_INTERVAL_S": "0", **(env or {})}, home=tmp_path)
    store = CredentialStore(tmp_path / "session.json", env={})
    if cookie:
        store.save({"hash": HASH, "userLicenses": "old-lic"})
    health = SessionState(configured=store.configured)
    factory = Factory()
    t = Transport(settings, store, health, session_factory=factory)
    return t, factory, store, health


# --------------------------------------------------------------------------- P4.1


async def test_policy_from_cookie_presence(tmp_path):
    t, factory, _, _ = make_transport(tmp_path, cookie=True)
    await t.session()
    kw = factory.calls[0]
    assert kw["max_rotations"] == 0 and kw["max_failures"] is None
    t2, factory2, _, _ = make_transport(tmp_path / "b", cookie=False)
    await t2.session()
    kw2 = factory2.calls[0]
    assert "max_rotations" not in kw2 and "max_failures" not in kw2


async def test_timeout_always_paired_with_attempt_timeout(tmp_path):
    t, factory, _, _ = make_transport(
        tmp_path, cookie=False, env={"CR_TIMEOUT_S": "50", "CR_ATTEMPT_TIMEOUT_S": "20"}
    )
    await t.session()
    kw = factory.calls[0]
    assert kw["timeout"] == 50 and kw["attempt_timeout"] == 20
    # wafer's limiter is OFF — the interval is the transport's own gate (test_interval_gate_*)
    assert kw["rate_limit"] == 0.0 and kw["max_response_size"] == 64 * 2**20


async def test_cookie_injected_with_domain_path_secure(tmp_path):
    t, factory, _, _ = make_transport(tmp_path, cookie=True)
    sess = await t.session()
    raws = [raw for raw, _ in sess.add_cookie_calls]
    assert f"hash={HASH}; Domain=.consumerreports.org; Path=/; Secure" in raws
    assert "userLicenses=old-lic; Domain=.consumerreports.org; Path=/; Secure" in raws
    assert all(url == WWW + "/" for _, url in sess.add_cookie_calls)
    assert sess.get_cookie("hash", "https://secure.consumerreports.org/ec/login") == HASH


def test_fetch_result_has_no_body_text_or_headers_attributes():
    fields = set(FetchResult.__dataclass_fields__)
    assert fields == {
        "status",
        "final_url",
        "content",
        "title",
        "challenge_type",
        "elapsed",
        "redirected",
        "credential_rejected",
        "credential_in_jar",
    }
    r = FetchResult(
        status=200,
        final_url="u",
        content=b"x",
        title=None,
        challenge_type=None,
        elapsed=0.1,
        redirected=False,
    )
    for forbidden in ("text", "headers", "cookies", "history"):
        assert not hasattr(r, forbidden)


async def test_single_session_per_process(tmp_path):
    t, factory, _, _ = make_transport(tmp_path, cookie=True)
    sessions = await asyncio.gather(*(t.session() for _ in range(5)))
    assert len(factory.calls) == 1 and all(s is sessions[0] for s in sessions)


def test_wafer_imported_only_here():
    import consumer_reports_mcp.transport as mod

    assert mod.wafer is wafer


# --------------------------------------------------------------------------- P4.2


@pytest.mark.parametrize(
    ("exc", "reason", "retryable"),
    [
        (wafer.ConnectionFailed(CAT_URL, "resolver refused"), "connection_failed", True),
        (wafer.WaferTimeout(CAT_URL, 120.0), "timeout", True),
        (wafer.EmptyResponse(CAT_URL, 200), "empty_response", True),
        (wafer.ResponseTooLarge(CAT_URL, 70_000_000, 67_108_864), "too_large", False),
        (wafer.TooManyRedirects(CAT_URL, 10), "redirect_loop", False),
    ],
)
async def test_reason_from_exception_type(tmp_path, exc, reason, retryable):
    t, factory, _, _ = make_transport(tmp_path, cookie=False)
    factory.session.push(exc)
    with pytest.raises(FetchFailed) as ei:
        await t.fetch(CAT_URL)
    assert ei.value.reason == reason and ei.value.retryable is retryable
    assert len(factory.session.requests) == 1


@pytest.mark.parametrize(
    "exc",
    [
        wafer.ChallengeDetected("cloudflare", CAT_URL, 403),
        wafer.RequestBlocked("cloudflare_block", CAT_URL, 403),
        wafer.RateLimited(CAT_URL, 30.0),
    ],
)
async def test_challenge_exceptions_are_challenged(tmp_path, exc):
    t, factory, _, _ = make_transport(tmp_path, cookie=False)
    factory.session.push(exc)
    with pytest.raises(Challenged) as ei:
        await t.fetch(CAT_URL)
    assert ei.value.challenge_type
    assert len(factory.session.requests) == 1


async def test_5xx_returned_is_server_error_retryable(tmp_path):
    t, factory, _, _ = make_transport(tmp_path, cookie=False)
    factory.session.push(
        FakeResponse(url=CAT_URL, status_code=500, content=b"<title>Error</title>")
    )
    with pytest.raises(FetchFailed) as ei:
        await t.fetch(CAT_URL)
    assert ei.value.reason == "server_error" and ei.value.retryable is True


async def test_challenge_with_200_is_challenged(tmp_path):
    t, factory, _, _ = make_transport(tmp_path, cookie=True)
    factory.session.push(
        FakeResponse(
            url=CAT_URL,
            status_code=200,
            content=b"<html>challenge</html>",
            challenge_type="cloudflare",
        )
    )
    with pytest.raises(Challenged) as ei:
        await t.fetch(CAT_URL)
    assert ei.value.challenge_type == "cloudflare" and ei.value.status == 200


async def test_403_under_no_rotation_is_challenged_not_fetch_failed(tmp_path):
    t, factory, _, _ = make_transport(tmp_path, cookie=True)
    factory.session.push(FakeResponse(url=CAT_URL, status_code=403, content=b"<html>denied</html>"))
    with pytest.raises(Challenged) as ei:
        await t.fetch(CAT_URL)
    assert ei.value.status == 403


async def test_cars_api_403_is_returned_for_the_caller_to_classify(tmp_path):
    t, factory, _, _ = make_transport(tmp_path, cookie=True)
    factory.session.push(
        FakeResponse(url=CARS_URL, status_code=403, content=b'{"message":"Forbidden"}')
    )
    r = await t.fetch(CARS_URL, headers={"x-api-key": "k"})
    assert r.status == 403 and r.credential_rejected is False
    assert factory.session.requests[0][1]["headers"] == {"x-api-key": "k"}


async def test_login_final_url_sets_rejected_and_retries_once_without_reseed(tmp_path, c37162):
    t, factory, store, health = make_transport(tmp_path, cookie=True)
    sess = factory.session
    sess.push(
        FakeResponse(
            url=LOGIN_URL,
            status_code=200,
            content=make_login_page(),
            history=[
                (302, CAT_URL),
                (302, "https://secure.consumerreports.org/ec/login?loginMethod=auto"),
            ],
            clear_cookies=("hash",),
        ),
        FakeResponse(
            url=CAT_URL, status_code=200, content=make_category_page(c37162, subscriber="false")
        ),
    )
    r = await t.fetch(CAT_URL)
    assert r.credential_rejected is True and r.final_url == CAT_URL
    assert len(sess.requests) == 2
    assert store.rejected is True and health.health is SessionHealth.EXPIRED
    seeds = [raw for raw, _ in sess.add_cookie_calls if "Max-Age=0" not in raw]
    assert len(seeds) == 2 and sess.get_cookie("hash", WWW + "/") is None  # construction only
    # and the credential stays dropped for the rest of the process: no re-seed on the next call
    sess.push(
        FakeResponse(
            url=CAT_URL, status_code=200, content=make_category_page(c37162, subscriber="false")
        )
    )
    await t.fetch(CAT_URL)
    assert [raw for raw, _ in sess.add_cookie_calls if "Max-Age=0" not in raw] == seeds


async def test_rejection_evicts_hash_from_the_jar_even_if_cr_did_not(tmp_path, c37162):
    t, factory, store, health = make_transport(tmp_path, cookie=True)
    sess = factory.session
    sess.push(  # CR redirects to login but does NOT clear the cookie (RECON §15 unmeasured case)
        FakeResponse(url=LOGIN_URL, status_code=200, content=make_login_page()),
        FakeResponse(
            url=CAT_URL, status_code=200, content=make_category_page(c37162, subscriber="false")
        ),
    )
    r = await t.fetch(CAT_URL)
    assert r.credential_rejected and r.credential_in_jar is False
    assert sess.get_cookie("hash", WWW + "/") is None  # evicted before the retry
    assert sess.get_cookie("userLicenses", WWW + "/") is None


async def test_marker_less_page_with_emptied_jar_is_neither_error_nor_auth_state(tmp_path):
    # the A-Z index carries no marker: an emptied jar there is reported, not classified
    t, factory, store, health = make_transport(tmp_path, cookie=True)
    sess = factory.session
    sess.push(
        FakeResponse(
            url=f"{WWW}/cro/a-to-z-index/products/index.htm",
            content=b"<html>index</html>",
            clear_cookies=("hash",),
        )
    )
    r = await t.fetch(f"{WWW}/cro/a-to-z-index/products/index.htm")
    assert r.credential_in_jar is False and r.credential_rejected is False
    assert health.health is SessionHealth.UNVERIFIED and store.rejected is False


async def test_car_page_logged_out_with_emptied_jar_is_identity_rotated(tmp_path):
    from tests.conftest import make_car_page

    t, factory, store, health = make_transport(tmp_path, cookie=True)
    sess = factory.session
    car_url = f"{WWW}/cars/acura/rdx/2026/overview/"
    sess.push(FakeResponse(url=car_url, content=make_car_page(False), clear_cookies=("hash",)))
    with pytest.raises(FetchFailed) as ei:
        await t.fetch(car_url)
    assert ei.value.reason == "identity_rotated"
    assert health.health is SessionHealth.UNVERIFIED
    # with the jar intact the same page is fine and reports the jar state for the cars surface
    sess.push(FakeResponse(url=car_url, content=make_car_page(False)))
    r = await t.fetch(car_url)
    assert r.credential_in_jar is True


async def test_cars_api_skips_login_check_and_rotation_writeback(tmp_path):
    t, factory, store, _ = make_transport(tmp_path, cookie=True)
    sess = factory.session
    sess.push(
        FakeResponse(
            url=CARS_URL,
            status_code=200,
            content=b'{"content":[]}',
            set_cookies={"userLicenses": "api-side"},
        )
    )
    r = await t.fetch(CARS_URL, headers={"x-api-key": "k"})
    assert r.credential_in_jar is None and r.credential_rejected is False
    assert store.cookies["userLicenses"] == "old-lic"  # never written back from an API call
    sess.push(
        FakeResponse(
            url="https://cars-api.consumerreports.org/ec/login?error",
            status_code=200,
            content=b"{}",
        )
    )
    r2 = await t.fetch(CARS_URL)
    assert r2.credential_rejected is False and store.rejected is False


async def test_token_mint_failed_is_challenged_not_fetch_failed(tmp_path):
    t, factory, _, _ = make_transport(tmp_path, cookie=False)
    factory.session.push(wafer.TokenMintFailed("no anchor token", stage="anchor"))
    with pytest.raises(Challenged) as ei:
        await t.fetch(CAT_URL)
    assert ei.value.challenge_type == "token_mint_failed"


async def test_single_flight_cancelled_waiter_does_not_cancel_others():
    sf = tp.SingleFlight()

    async def work():
        await asyncio.sleep(0.05)
        return "done"

    leader = asyncio.ensure_future(sf.run("k", work))
    await asyncio.sleep(0)
    waiter_a = asyncio.ensure_future(sf.run("k", work))
    waiter_b = asyncio.ensure_future(sf.run("k", work))
    await asyncio.sleep(0.01)
    waiter_a.cancel()
    assert await leader == "done" and await waiter_b == "done"
    with pytest.raises(asyncio.CancelledError):
        await waiter_a


async def test_login_in_history_only_is_healthy(tmp_path, c37162):
    t, factory, store, health = make_transport(tmp_path, cookie=True)
    factory.session.push(
        FakeResponse(
            url=CAT_URL,
            status_code=200,
            content=make_category_page(c37162, subscriber="true", fill_scores=True),
            history=[
                (302, CAT_URL),
                (302, "https://secure.consumerreports.org/ec/login?loginMethod=auto"),
            ],
            set_cookies={"userLicenses": "re-minted"},
        )
    )
    r = await t.fetch(CAT_URL)
    assert r.credential_rejected is False and r.redirected is True
    assert (
        store.rejected is False and health.health is SessionHealth.UNVERIFIED
    )  # transport never confirms
    assert len(factory.session.requests) == 1


async def test_identity_rotated_when_hash_missing_after_logged_out_page(tmp_path, c37162):
    t, factory, store, health = make_transport(tmp_path, cookie=True)
    sess = factory.session
    sess.push(
        FakeResponse(
            url=CAT_URL,
            status_code=200,
            content=make_category_page(c37162, subscriber="false"),
            clear_cookies=("hash",),
        )
    )  # the jar lost the credential mid-request
    with pytest.raises(FetchFailed) as ei:
        await t.fetch(CAT_URL)
    assert ei.value.reason == "identity_rotated" and ei.value.retryable is False
    assert store.rejected is False and health.health is SessionHealth.UNVERIFIED


async def test_reseed_before_request_when_hash_missing(tmp_path, c37162):
    t, factory, _, _ = make_transport(tmp_path, cookie=True)
    sess = await t.session()
    sess.drop("hash")
    n = len(sess.add_cookie_calls)
    sess.push(
        FakeResponse(
            url=CAT_URL, status_code=200, content=make_category_page(c37162, subscriber="true")
        )
    )
    await t.fetch(CAT_URL)
    assert len(sess.add_cookie_calls) > n
    assert sess.get_cookie("hash", WWW + "/") == HASH


async def test_userlicenses_rotation_recorded_from_jar_not_response(tmp_path, c37162):
    t, factory, store, _ = make_transport(tmp_path, cookie=True)
    sess = factory.session
    # the re-mint lands on a redirect hop: `cookies` (this response) is empty, the JAR has it
    sess.push(
        FakeResponse(
            url=CAT_URL,
            status_code=200,
            content=make_category_page(c37162, subscriber="true"),
            cookies={},
            set_cookies={"userLicenses": "fresh"},
        )
    )
    await t.fetch(CAT_URL)
    assert store.cookies["userLicenses"] == "fresh"
    import json

    assert json.loads((tmp_path / "session.json").read_text())["cookies"]["userLicenses"] == "fresh"


async def test_no_rotation_writeback_for_env_source(tmp_path, c37162):
    settings = Settings(env={}, home=tmp_path)
    store = CredentialStore(tmp_path / "session.json", env={"CR_SESSION_COOKIE": f"hash={HASH}"})
    health = SessionState(configured=store.configured)
    factory = Factory()
    t = Transport(settings, store, health, session_factory=factory)
    factory.session.push(
        FakeResponse(
            url=CAT_URL,
            status_code=200,
            content=make_category_page(c37162, subscriber="true"),
            set_cookies={"userLicenses": "fresh"},
        )
    )
    await t.fetch(CAT_URL)
    assert not (tmp_path / "session.json").exists()


async def test_no_retry_by_server_after_wafer_error(tmp_path):
    t, factory, _, _ = make_transport(tmp_path, cookie=False)
    factory.session.push(wafer.ConnectionFailed(CAT_URL, "boom"))
    with pytest.raises(FetchFailed):
        await t.fetch(CAT_URL)
    assert len(factory.session.requests) == 1


async def test_fetch_failed_detail_never_contains_body(tmp_path):
    t, factory, _, _ = make_transport(tmp_path, cookie=False)
    body = b"<html><title>Maintenance</title>SECRET-BODY-TOKEN</html>"
    factory.session.push(FakeResponse(url=CAT_URL, status_code=503, content=body))
    with pytest.raises(FetchFailed) as ei:
        await t.fetch(CAT_URL)
    text = str(ei.value) + str(ei.value.detail) + repr(ei.value.__dict__)
    assert "SECRET-BODY-TOKEN" not in text and "Maintenance" not in text


async def test_empty_body_returned_is_empty_response(tmp_path):
    t, factory, _, _ = make_transport(tmp_path, cookie=True)
    factory.session.push(FakeResponse(url=CAT_URL, status_code=200, content=b""))
    with pytest.raises(FetchFailed) as ei:
        await t.fetch(CAT_URL)
    assert ei.value.reason == "empty_response" and ei.value.retryable


async def test_no_cookie_process_never_touches_the_jar(tmp_path, c37162):
    t, factory, store, health = make_transport(tmp_path, cookie=False)
    factory.session.push(
        FakeResponse(
            url=CAT_URL, status_code=200, content=make_category_page(c37162, subscriber="false")
        )
    )
    r = await t.fetch(CAT_URL)
    assert r.status == 200 and not factory.session.add_cookie_calls
    assert health.health is SessionHealth.NONE and store.rejected is False


async def test_single_flight_coalesces_concurrent_calls():
    sf = tp.SingleFlight()
    calls = []

    async def work():
        calls.append(1)
        await asyncio.sleep(0.01)
        return "row"

    results = await asyncio.gather(*(sf.run(("cat", 37162), work) for _ in range(4)))
    assert results == ["row"] * 4 and len(calls) == 1

    async def boom():
        raise FetchFailed("timeout", retryable=True, url="u")

    with pytest.raises(FetchFailed):
        await asyncio.gather(sf.run("k", boom), sf.run("k", boom))
    assert sf._inflight == {}


def test_host_classification():
    assert tp.is_www(CAT_URL) and tp.is_www("https://secure.consumerreports.org/ec/login")
    assert not tp.is_www(CARS_URL) and tp.is_cars_api(CARS_URL)
    assert not tp.is_www("https://example.com/")
    assert not tp.is_www("https://member-service-api.consumerreports.org/")  # allowlist, not deny


# --------------------------------------------------------------------------- adopt (SPEC §6)


class MultiFactory:
    """Hands out a FRESH FakeWaferSession per construction, so a rebuild is observable."""

    def __init__(self, n: int = 3) -> None:
        self.sessions = [FakeWaferSession() for _ in range(n)]
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        sess = self.sessions[len(self.calls) - 1]
        sess.kwargs = kwargs
        return sess


def make_adoptable(tmp_path: Path, *, cookie: bool):
    settings = Settings(env={"CR_MIN_REQUEST_INTERVAL_S": "0"}, home=tmp_path)
    store = CredentialStore(tmp_path / "session.json", env={})
    if cookie:
        store.save({"hash": HASH})
    health = SessionState(configured=store.configured)
    factory = MultiFactory()
    return Transport(settings, store, health, session_factory=factory), factory, store, health


async def test_adopt_rebuilds_the_session_under_the_cookie_policy(tmp_path, c37162):
    """The policy is a constructor kwarg, so a cookie saved mid-process is a no-op until the
    session is rebuilt — that rebuild is what `adopt()` is for."""
    t, factory, store, health = make_adoptable(tmp_path, cookie=False)
    first, second = factory.sessions[0], factory.sessions[1]
    first.push(FakeResponse(url=CAT_URL, content=make_category_page(c37162, subscriber="false")))
    await t.fetch(CAT_URL)
    assert "max_rotations" not in factory.calls[0] and not first.add_cookie_calls
    store.save({"hash": HASH})  # exactly what cr_sign_in does after validation
    assert await t.adopt() is True
    assert t.cookie_configured is True and health.health is SessionHealth.UNVERIFIED
    assert first.closed is True  # the old session was retired, not leaked
    assert len(factory.calls) == 1  # …and nothing is built until a request needs it
    second.push(
        FakeResponse(
            url=CAT_URL, content=make_category_page(c37162, subscriber="true", fill_scores=True)
        )
    )
    r = await t.fetch(CAT_URL)
    assert len(factory.calls) == 2
    assert factory.calls[1]["max_rotations"] == 0 and factory.calls[1]["max_failures"] is None
    assert r.credential_in_jar is True and len(second.requests) == 1
    assert f"hash={HASH}; Domain=.consumerreports.org; Path=/; Secure" in [
        raw for raw, _ in second.add_cookie_calls
    ]


async def test_adopt_is_idempotent(tmp_path, c37162):
    t, factory, store, health = make_adoptable(tmp_path, cookie=False)
    store.save({"hash": HASH})
    assert await t.adopt() is True and await t.adopt() is True
    assert t.adoptions == 2 and len(factory.calls) == 0  # no session existed: nothing to retire
    assert t.cookie_configured is True and health.health is SessionHealth.UNVERIFIED
    factory.sessions[0].push(
        FakeResponse(url=CAT_URL, content=make_category_page(c37162, subscriber="true"))
    )
    await t.fetch(CAT_URL)
    assert len(factory.calls) == 1 and factory.calls[0]["max_rotations"] == 0
    # adopting the same state again after a session exists: one rebuild, same policy
    await t.adopt()
    factory.sessions[1].push(
        FakeResponse(url=CAT_URL, content=make_category_page(c37162, subscriber="true"))
    )
    await t.fetch(CAT_URL)
    assert len(factory.calls) == 2 and factory.calls[1] == factory.calls[0]
    assert store.rejected is False and health.health is SessionHealth.UNVERIFIED


async def test_adopt_while_a_request_is_in_flight_does_not_corrupt_state(tmp_path, c37162):
    """A response that arrives after the policy moved was made under the OLD credential: it is
    a retryable transport failure, never classified, never cached — so a rejection of the old
    cookie can never latch onto the new one, and health stays at the cold-start state."""
    t, factory, store, health = make_adoptable(tmp_path, cookie=False)
    first = factory.sessions[0]
    gate = asyncio.Event()

    async def slow(url, kw):
        await gate.wait()
        return FakeResponse(url=url, content=make_category_page(c37162, subscriber="false"))

    first.push(slow)
    inflight = asyncio.ensure_future(t.fetch(CAT_URL))
    await asyncio.sleep(0.01)
    assert len(first.requests) == 1  # the request is on the wire
    store.save({"hash": HASH})
    await t.adopt()
    gate.set()
    with pytest.raises(FetchFailed) as ei:
        await inflight
    assert ei.value.reason == "policy_changed" and ei.value.retryable is True
    assert health.health is SessionHealth.UNVERIFIED and store.rejected is False
    assert t.cookie_configured is True and first.closed is True
    # the same straddle on a REJECTION must not latch the new credential as rejected either
    t2, factory2, store2, health2 = make_adoptable(tmp_path / "b", cookie=True)
    s0 = factory2.sessions[0]
    gate2 = asyncio.Event()

    async def slow_reject(url, kw):
        await gate2.wait()
        return FakeResponse(url=LOGIN_URL, content=make_login_page(), clear_cookies=("hash",))

    s0.push(slow_reject)
    inflight2 = asyncio.ensure_future(t2.fetch(CAT_URL))
    await asyncio.sleep(0.01)
    store2.save({"hash": "n" * 36})  # a fresh sign-in landed while the old cookie was in flight
    await t2.adopt()
    gate2.set()
    with pytest.raises(FetchFailed) as ei2:
        await inflight2
    assert ei2.value.reason == "policy_changed"
    assert store2.rejected is False and health2.health is SessionHealth.UNVERIFIED
    # and a cars-API response is unaffected: there is no credential logic to misapply
    s1 = factory2.sessions[1]
    s1.push(FakeResponse(url=CARS_URL, content=b'{"content":[]}'))
    r = await t2.fetch(CARS_URL, headers={"x-api-key": "k"})
    assert r.status == 200


async def test_rejection_latch_still_holds_after_adopt(tmp_path, c37162):
    t, factory, store, health = make_adoptable(tmp_path, cookie=False)
    store.save({"hash": HASH})
    await t.adopt()
    sess = factory.sessions[0]
    sess.push(
        FakeResponse(url=LOGIN_URL, content=make_login_page(), clear_cookies=("hash",)),
        FakeResponse(url=CAT_URL, content=make_category_page(c37162, subscriber="false")),
    )
    r = await t.fetch(CAT_URL)
    assert r.credential_rejected is True and len(sess.requests) == 2
    assert store.rejected is True and health.health is SessionHealth.EXPIRED
    seeds = [raw for raw, _ in sess.add_cookie_calls if "Max-Age=0" not in raw]
    sess.push(FakeResponse(url=CAT_URL, content=make_category_page(c37162, subscriber="false")))
    await t.fetch(CAT_URL)  # still no re-seed: the latch holds for this session
    assert [raw for raw, _ in sess.add_cookie_calls if "Max-Age=0" not in raw] == seeds
    # a NEW sign-in clears the latch and seeds the new session with the new cookie
    store.save({"hash": "n" * 36})
    await t.adopt()
    assert store.rejected is False and health.health is SessionHealth.UNVERIFIED
    fresh = factory.sessions[1]
    fresh.push(FakeResponse(url=CAT_URL, content=make_category_page(c37162, subscriber="true")))
    r2 = await t.fetch(CAT_URL)
    assert r2.credential_rejected is False and r2.credential_in_jar is True
    assert fresh.get_cookie("hash", WWW + "/") == "n" * 36


async def test_adopt_drops_the_credential_when_the_store_is_empty(tmp_path, c37162):
    """`adopt()` reflects the store, so `auth --forget` semantics work mid-process too."""
    t, factory, store, health = make_adoptable(tmp_path, cookie=True)
    factory.sessions[0].push(
        FakeResponse(url=CAT_URL, content=make_category_page(c37162, subscriber="true"))
    )
    await t.fetch(CAT_URL)
    assert factory.calls[0]["max_rotations"] == 0
    store.forget()
    assert await t.adopt() is False
    assert t.cookie_configured is False and health.health is SessionHealth.NONE
    factory.sessions[1].push(
        FakeResponse(url=CAT_URL, content=make_category_page(c37162, subscriber="false"))
    )
    r = await t.fetch(CAT_URL)
    assert "max_rotations" not in factory.calls[1] and not factory.sessions[1].add_cookie_calls
    assert r.credential_in_jar is None and health.health is SessionHealth.NONE


# --------------------------------------------------------------------------- the interval gate


async def test_interval_gate_holds_across_concurrent_callers(tmp_path):
    """wafer's limiter is wait → send → record with no lock, so N concurrent callers read one
    timestamp, compute the same delay and fire together — N× the configured rate. The gate
    is ours: a lock held across the wait, and wafer's own limiter is off so they never stack."""
    import time

    t, factory, _, _ = make_transport(
        tmp_path, cookie=False, env={"CR_MIN_REQUEST_INTERVAL_S": "0.05"}
    )
    sent: list[float] = []

    def record(url, kw):
        sent.append(time.monotonic())
        return FakeResponse(url=url, content=b"<html>ok</html>")

    factory.session.route(f"{WWW}/products/sitemap/", record)
    await asyncio.gather(*(t.fetch(f"{WWW}/products/sitemap/{i}") for i in range(5)))
    stamps = sorted(sent)
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    assert len(sent) == 5 and all(g >= 0.045 for g in gaps), gaps
    assert factory.calls[0]["rate_limit"] == 0.0  # wafer's limiter is off: the gate is ours


async def test_interval_gate_is_not_held_across_the_request(tmp_path):
    # holding it across the request would serialise 60-second downloads behind each other
    t, factory, _, _ = make_transport(
        tmp_path, cookie=False, env={"CR_MIN_REQUEST_INTERVAL_S": "0.01"}
    )
    gate = asyncio.Event()
    order: list[str] = []

    async def slow(url, kw):
        order.append("a-sent")
        await gate.wait()
        return FakeResponse(url=url, content=b"<html>a</html>")

    def fast(url, kw):
        order.append("b-sent")
        return FakeResponse(url=url, content=b"<html>b</html>")

    factory.session.route(f"{WWW}/a/", slow)
    factory.session.route(f"{WWW}/b/", fast)
    ta = asyncio.ensure_future(t.fetch(f"{WWW}/a/"))
    await asyncio.sleep(0.005)
    tb = asyncio.ensure_future(t.fetch(f"{WWW}/b/"))
    await asyncio.sleep(0.05)
    assert order == ["a-sent", "b-sent"]  # b went out while a was still downloading
    gate.set()
    await asyncio.gather(ta, tb)


async def test_interval_gate_is_per_host_like_the_config_says(tmp_path):
    import time

    t, factory, _, _ = make_transport(
        tmp_path, cookie=False, env={"CR_MIN_REQUEST_INTERVAL_S": "0.05"}
    )
    sent: dict[str, float] = {}

    def record(url, kw):
        sent[url] = time.monotonic()
        return FakeResponse(url=url, content=b'{"content":[]}')

    factory.session.route(f"{WWW}/a/", record)
    factory.session.route(CARS_URL, record)
    await asyncio.gather(t.fetch(f"{WWW}/a/"), t.fetch(CARS_URL, headers={"x-api-key": "k"}))
    assert abs(sent[f"{WWW}/a/"] - sent[CARS_URL]) < 0.03  # different hosts do not queue


async def test_interval_of_zero_disables_the_gate(tmp_path):
    import time

    t, factory, _, _ = make_transport(
        tmp_path, cookie=False, env={"CR_MIN_REQUEST_INTERVAL_S": "0"}
    )
    factory.session.route(
        f"{WWW}/", lambda url, kw: FakeResponse(url=url, content=b"<html>x</html>")
    )
    t0 = time.monotonic()
    await asyncio.gather(*(t.fetch(f"{WWW}/{i}/") for i in range(5)))
    assert time.monotonic() - t0 < 0.05


# --------------------------------------------------------------------------- host allowlist


@pytest.mark.parametrize(
    "url",
    [
        "http://www.consumerreports.org/appliances/c37162/",  # scheme
        "https://169.254.169.254/latest/meta-data/",
        "https://localhost:8080/",
        "https://127.0.0.1/",
        "https://evil.example/",
        "https://member-service-api.consumerreports.org/",  # a CR subdomain is not ours
        "https://www.consumerreports.org.evil.example/",
        "https://www.consumerreports.org@evil.example/",
        "file:///etc/passwd",
        "//www.consumerreports.org/",
    ],
)
async def test_off_host_urls_are_refused_before_any_request(tmp_path, url):
    """URLs come from fetched content — every `<loc>` in `products.xml`, `reliabilityURL` in
    the payload, a cached `final_url` — and the jar seeds `hash` for `.consumerreports.org`.
    One check at the choke point: https, and a host we actually talk to."""
    t, factory, _, _ = make_transport(tmp_path, cookie=True)
    with pytest.raises(FetchFailed) as ei:
        await t.fetch(url)
    assert ei.value.reason == "url_unresolved" and ei.value.retryable is False
    assert factory.session.requests == [] and t.request_count == 0


async def test_allowed_hosts_are_exactly_the_three_we_talk_to(tmp_path):
    t, factory, _, _ = make_transport(tmp_path, cookie=False)
    for url in (CAT_URL, CARS_URL):
        factory.session.push(FakeResponse(url=url, content=b"<html>ok</html>"))
        assert (await t.fetch(url)).status == 200
    assert tp.ALLOWED_HOSTS == frozenset(
        {"www.consumerreports.org", "secure.consumerreports.org", "cars-api.consumerreports.org"}
    )


# --------------------------------------------------------------------------- /ec/login is a route


async def test_login_marker_is_a_parsed_route_not_a_substring(tmp_path, c37162):
    """`"/ec/login" in final_url` matched `/anything/ec/login-tips/` on `www.` — one such page
    latched `rejected`, marked the session expired and evicted the cookie for the process."""
    from consumer_reports_mcp.config import is_login_url

    t, factory, store, health = make_transport(tmp_path, cookie=True)
    tips = f"{WWW}/anything/ec/login-tips/"
    page = make_category_page(c37162, subscriber="true", fill_scores=True)
    factory.session.push(FakeResponse(url=tips, content=page))
    r = await t.fetch(tips)
    assert r.credential_rejected is False and store.rejected is False
    assert health.health is SessionHealth.UNVERIFIED and len(factory.session.requests) == 1
    assert factory.session.get_cookie("hash", WWW + "/") == HASH  # not evicted
    for url in (
        LOGIN_URL,
        "https://secure.consumerreports.org/ec/login",
        "https://secure.consumerreports.org/ec/login/",
        "https://secure.consumerreports.org/ec/login/step?x=1",
        "https://SECURE.consumerreports.org/ec/login?loginMethod=auto",
    ):
        assert is_login_url(url), url
    for url in (
        tips,
        "https://secure.consumerreports.org/ec/login-tips/",
        "https://secure.consumerreports.org/x/ec/login",
        "https://www.consumerreports.org/ec/login?error",  # the login page lives on `secure.`
        "https://evil.example/ec/login",
    ):
        assert not is_login_url(url), url


# --------------------------------------------------------------------------- leader cancellation


async def test_single_flight_cancelled_leader_does_not_cancel_waiters():
    """The reviewer's sequence: `cr_ratings("tvs")` leads the flight, `cr_filters("tvs")` joins,
    the client cancels the FIRST call (mcp 2.1.1 interrupts the tool task). The leader's
    `CancelledError` used to be stored on the shared future and re-raised in every waiter — a
    task nobody cancelled ended `cancelled()` with `cancelling() == 0`."""
    sf = tp.SingleFlight()
    started = asyncio.Event()

    async def work():
        started.set()
        await asyncio.sleep(0.05)
        return "done"

    leader = asyncio.ensure_future(sf.run("k", work))
    await started.wait()
    waiter = asyncio.ensure_future(sf.run("k", work))
    await asyncio.sleep(0.01)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    assert await waiter == "done"
    assert not waiter.cancelled() and waiter.cancelling() == 0
    assert sf._inflight == {}


async def test_single_flight_lone_caller_cancellation_still_stops_the_work():
    """Unchanged: with nobody else waiting, cancelling the one caller cancels the flight (the
    11 MB download stops), and a newcomer starts afresh rather than joining a dying flight."""
    sf = tp.SingleFlight()
    cancelled = asyncio.Event()
    runs = 0

    async def work():
        nonlocal runs
        runs += 1
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "done"

    leader = asyncio.ensure_future(sf.run("k", work))
    await asyncio.sleep(0.01)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    await asyncio.wait_for(cancelled.wait(), 1)
    assert sf._inflight == {}
    fresh = asyncio.ensure_future(sf.run("k", work))
    await asyncio.sleep(0.01)
    assert runs == 2 and not fresh.done()
    fresh.cancel()
    with pytest.raises(asyncio.CancelledError):
        await fresh


async def test_single_flight_stops_the_work_only_when_the_last_caller_leaves():
    sf = tp.SingleFlight()
    cancelled = asyncio.Event()

    async def work():
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    leader = asyncio.ensure_future(sf.run("k", work))
    await asyncio.sleep(0)
    waiter = asyncio.ensure_future(sf.run("k", work))
    await asyncio.sleep(0.01)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    await asyncio.sleep(0.01)
    assert not cancelled.is_set() and sf._inflight  # the waiter still wants it
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.wait_for(cancelled.wait(), 1)
    assert sf._inflight == {}


# --------------------------------------------------------------------------- failure projection


def _attributes_read_by(exc: Exception, fn) -> set[str]:
    """Which public attributes `fn` reads off `exc` — via an instance of a recording subclass,
    so `isinstance` inside `fn` still sees the real type."""
    read: set[str] = set()

    class Spy(type(exc)):  # type: ignore[misc]
        def __getattribute__(self, name: str):
            if not name.startswith("_"):
                read.add(name)
            return super().__getattribute__(name)

    spy = Spy.__new__(Spy)
    spy.__dict__.update(vars(exc))
    fn(spy)
    return read


@pytest.mark.parametrize(
    "exc",
    [
        FetchFailed(
            "server_error", retryable=True, url=CAT_URL, detail="http 503", http_status=503
        ),
        Challenged("cloudflare", 403, CAT_URL),
    ],
    ids=["fetch_failed", "challenged"],
)
def test_failure_fields_projects_every_attribute_it_does_not_declare_diagnostic(exc):
    """The drift check for the ONE projection (SPEC §7 *Error taxonomy*): every attribute a
    transport exception stores is either read by `failure_fields` or declared in
    `FAILURE_DIAGNOSTIC_ONLY` (`url`, `detail` — never returned). Adding `http_status` to
    `FetchFailed` once required editing five hand-spelled sites, and the one left behind
    answered `http_status: null` silently — the schema allows null and every consumer used
    `.get()`. Now there is one site, and this fails the moment an attribute is added to either
    class without a decision about whether it escapes."""
    stored = set(vars(exc))
    read = _attributes_read_by(exc, tp.failure_fields)
    unprojected = stored - tp.FAILURE_DIAGNOSTIC_ONLY - read
    assert not unprojected, f"{type(exc).__name__} stores {sorted(unprojected)}: not projected"
    assert not read & tp.FAILURE_DIAGNOSTIC_ONLY, "a diagnostic-only attribute escaped"
    assert set(tp.failure_fields(exc)) == {"reason", "retryable", "http_status"}


def test_failure_fields_check_detects_an_attribute_left_behind():
    """Sanity: an attribute the projection never reads is reported."""
    exc = FetchFailed("timeout", retryable=True, url=CAT_URL)
    exc.title = "Site Maintenance"  # type: ignore[attr-defined]  # a hypothetical new attribute
    read = _attributes_read_by(exc, tp.failure_fields)
    assert set(vars(exc)) - tp.FAILURE_DIAGNOSTIC_ONLY - read == {"title"}


def test_failure_fields_and_code_for_both_types():
    """A `FetchFailed` carries a status only when one caused it; a `Challenged` always has the
    one the challenge arrived on, and is never retryable (surfaced, negatively cached)."""
    assert tp.failure_fields(FetchFailed("timeout", retryable=True, url=CAT_URL)) == {
        "reason": "timeout",
        "retryable": True,
        "http_status": None,
    }
    bad = FetchFailed("bad_request", retryable=False, url=CARS_URL, detail="x", http_status=400)
    assert tp.failure_fields(bad) == {
        "reason": "bad_request",
        "retryable": False,
        "http_status": 400,
    }
    assert tp.failure_fields(Challenged("rate_limited", 429, CARS_URL)) == {
        "reason": "rate_limited",
        "retryable": False,
        "http_status": 429,
    }
    assert tp.failure_code(bad) == "fetch_failed"
    assert tp.failure_code(Challenged("cloudflare", 200, CAT_URL)) == "challenged"


# --------------------------------------------------------------------------- the wafer contract

WAFER_MEASURED = ">=0.4.9,<0.6"
"""The wafer-py range every rule in CLAUDE.md *HTTP* was measured against — both ends.

wafer-py is pre-1.0 (`Development Status :: 4 - Beta`, 32 releases from 0.0.1 to 0.5.0 in seven
months, no changelog), and its minors are where the surface has moved: 0.3.0 added
`max_response_size`, `attempt_timeout` and `get_cookie`; 0.4.2 `RequestBlocked`; 0.5.0 a second
`_rebuild_client` caller. What this project encodes about wafer is behaviour, not signatures —
rotation empties the jar without raising, the policy is constructor-only, a JSON 403 is RETURNED,
`__aexit__` is the only teardown, the limiter is lockless — and a PyPI install has no lockfile,
so an open-ended pin would let a 0.6 change those rules silently. The suite passes on released
0.4.9 and 0.5.0 alike. Moving this constant means re-measuring, then changing `pyproject.toml`,
CLAUDE.md and SPEC §9 in the same step; the tests below hold the four together."""

ROOT = Path(__file__).resolve().parents[1]


def _declared_wafer_requirement():
    import tomllib

    from packaging.requirements import Requirement

    with (ROOT / "pyproject.toml").open("rb") as fh:
        deps = tomllib.load(fh)["project"]["dependencies"]
    reqs = [Requirement(d) for d in deps]
    (req,) = [r for r in reqs if r.name == "wafer-py"]
    return req


def test_wafer_pin_is_the_measured_range_in_pyproject_and_both_docs():
    from packaging.specifiers import SpecifierSet

    assert _declared_wafer_requirement().specifier == SpecifierSet(WAFER_MEASURED)
    spelled = f"`wafer-py{WAFER_MEASURED}`"
    for doc in ("CLAUDE.md", "SPEC.md"):
        assert spelled in (ROOT / doc).read_text(encoding="utf-8"), f"{spelled} not in {doc}"


def test_installed_wafer_is_inside_the_measured_range():
    """`uv sync` enforces the pin on the dev path; this catches an install that bypassed it."""
    from importlib.metadata import version

    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    assert Version(version("wafer-py")) in SpecifierSet(WAFER_MEASURED)


def test_wafer_session_surface_matches_the_rules(tmp_path):
    """The real class, not the fake: the constructor kwargs `session_kwargs` emits under both
    policies are named parameters of wafer's session (so a renamed kwarg cannot be swallowed and
    ignored), the four methods the transport calls exist, `_rebuild_client` — the thing the
    `identity_rotated` rule is about — still exists, and there is still no `close()`: `_retire`
    tears down through `__aexit__` because that is the only teardown wafer offers, and a
    `close()` appearing means that rule and `_retire` need re-measuring, not silent staleness."""
    import inspect

    from wafer._base import BaseSession

    params = inspect.signature(BaseSession.__init__).parameters
    assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    settings = Settings(env={}, home=tmp_path)
    for configured in (False, True):
        assert set(tp.session_kwargs(settings, configured)) <= set(params)
    for name in ("add_cookie", "get_cookie", "__aenter__", "__aexit__", "_rebuild_client"):
        assert callable(getattr(wafer.AsyncSession, name)), name
    for name in ("close", "aclose"):
        assert not hasattr(wafer.AsyncSession, name), f"wafer grew {name}(): re-measure teardown"
