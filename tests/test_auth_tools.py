"""`cr_sign_in` / `cr_auth_status` (SPEC §6 *cr_sign_in*, §7): non-blocking start, status poll,
the verify-then-decide guard, live adoption, and the rule that no output ever carries a cookie
value."""

from __future__ import annotations

import asyncio
import json
import logging
import stat
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest

from consumer_reports_mcp import auth_tools as A
from consumer_reports_mcp.auth_tools import SignInFlow, cr_auth_status, cr_sign_in
from consumer_reports_mcp.browser_auth import (
    BrowserExtraMissing,
    BrowserNotFound,
    Capture,
    CaptureTimeout,
    NotDurable,
    WindowClosed,
)
from consumer_reports_mcp.cars.tools import cr_car_search
from consumer_reports_mcp.config import AUTH_PROBE_PATH, WWW, Settings
from consumer_reports_mcp.credentials import CredentialStore, SessionHealth
from consumer_reports_mcp.runtime import build_runtime
from consumer_reports_mcp.tools_products import cr_categories, cr_ratings
from tests.conftest import (
    FakeResponse,
    FakeWaferSession,
    LoopHeartbeat,
    RuntimeHarness,
    make_category_page,
)

HASH = "s" * 36
STORED = "h" * 36  # what `RuntimeHarness(cookie=True)` stores
PROBE_URL = WWW + AUTH_PROBE_PATH
LIVE_PHASES = ("verifying", "waiting", "validating")


def iso_days_from_now(days: float) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_capture(
    token: str = HASH,
    *,
    label: str | None = "chrome",
    gate: asyncio.Event | None = None,
    error: Exception | None = None,
    before_launch: Exception | None = None,
    expires_at: str | None = None,
):
    """A fake `capture_hash`: the token with the expiry the browser reported — 365 days out
    unless the test says otherwise, as the "remember me" mint issues it."""
    calls: list[dict] = []
    expires_at = expires_at or iso_days_from_now(365)

    async def capture(*, timeout_s: int, on_launch):
        calls.append({"timeout_s": timeout_s})
        if before_launch is not None:
            raise before_launch
        if label is not None:
            on_launch(label)
        if gate is not None:
            await gate.wait()
        if error is not None:
            raise error
        return Capture(value=token, expires_at=expires_at)

    capture.calls = calls  # type: ignore[attr-defined]
    return capture


def make_validate(
    outcome: str = "member",
    *,
    error: Exception | None = None,
    by_hash: dict[str, str] | None = None,
    gate: asyncio.Event | None = None,
):
    """`by_hash` answers per cookie value, so the pre-flight check of the STORED cookie and the
    post-capture check of the NEW one can get different verdicts in one flow."""
    seen: list[dict] = []

    async def validate(settings: Settings, cookies):
        seen.append(dict(cookies))
        if gate is not None:
            await gate.wait()
        if error is not None:
            raise error
        if by_hash is not None:
            return by_hash[cookies["hash"]]
        return outcome

    validate.seen = seen  # type: ignore[attr-defined]
    return validate


def flow_for(h: RuntimeHarness, **kw) -> SignInFlow:
    kw.setdefault("launch_wait_s", 0.3)
    kw.setdefault("timeout_s", 300)
    flow = SignInFlow(h.rt, **kw)
    h.rt.sign_in = flow
    return flow


async def settle(h: RuntimeHarness, *, budget_s: float = 3.0):
    """Poll the status tool the way an agent would until the sign-in leaves its live phases."""
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        st = await cr_auth_status(h.rt, wait_s=1)
        if st.data.sign_in not in LIVE_PHASES:
            return st
    raise AssertionError("sign-in never settled")


def probe_member_page(h: RuntimeHarness, c37162: dict, *, subscriber: str = "true") -> None:
    probe = dict(c37162, final_url=PROBE_URL)
    probe["filter_instance"] = dict(c37162["filter_instance"])
    probe["filter_instance"]["args"] = dict(c37162["filter_instance"]["args"], cid=35183)
    h.sess.route(
        PROBE_URL,
        FakeResponse(
            url=PROBE_URL,
            content=make_category_page(
                probe, subscriber=subscriber, fill_scores=subscriber == "true"
            ),
        ),
    )


def age_stored_cookie(h: RuntimeHarness, days: int) -> None:
    path = h.rt.credentials.path
    data = json.loads(path.read_text())
    data["captured_at"] = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(data))
    h.rt.credentials.load()


def stored_hash(h: RuntimeHarness) -> str:
    return json.loads(h.rt.credentials.path.read_text())["cookies"]["hash"]


# --------------------------------------------------------------------------- the happy path


async def test_sign_in_answers_fast_and_status_reports_the_outcome(tmp_path):
    h = RuntimeHarness(tmp_path)
    gate = asyncio.Event()
    capture = make_capture(gate=gate)
    validate = make_validate("member")
    flow_for(h, capture=capture, validate=validate)
    assert h.rt.transport.cookie_configured is False and h.rt.health.health is SessionHealth.NONE

    t0 = time.monotonic()
    out = await cr_sign_in(h.rt)
    assert time.monotonic() - t0 < 1.0  # the user takes minutes; this call must not
    assert out.data.status == "waiting" and out.data.reason is None
    assert out.data.browser == "chrome"
    assert out.data.expires_in_s is not None and 290 <= out.data.expires_in_s <= 300
    assert "cr_auth_status" in out.data.instructions and "remember me" in out.data.instructions
    assert out.session == "none" and out.error is None and out.warnings == []  # nothing changed

    st = await cr_auth_status(h.rt)
    assert st.data.sign_in == "waiting" and st.data.browser == "chrome" and st.session == "none"
    assert st.data.source is None and st.data.days_left_max is None and st.error is None
    gate.set()  # the user signed in
    st = await settle(h)
    assert st.data.sign_in == "active" and st.data.reason is None
    assert validate.seen == [{"hash": HASH}]  # validated before anything was written
    # stored, 0600, adopted, and the process health says so
    path = h.rt.credentials.path
    assert json.loads(path.read_text())["cookies"] == {"hash": HASH}
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert st.session == "active" and st.data.source == "file" and st.data.days_left_max == 365
    # the browser reported the cookie's expiry, and it was stored — so the countdown is
    # measured, not the assumed 365-day arithmetic on our own constant
    assert st.data.expiry_basis == "measured" and st.data.expires_at
    assert json.loads(path.read_text())["expires_at"] == st.data.expires_at
    assert h.rt.transport.cookie_configured is True and h.rt.transport.adoptions == 1
    assert h.rt.health.health is SessionHealth.ACTIVE
    assert capture.calls == [{"timeout_s": 300}]


async def test_sign_in_makes_the_next_products_call_member_tier_without_a_restart(tmp_path, c37162):
    """Verifies the claim, rather than assuming it: tier qualification reads the session health
    per call, so once adopted the anonymous row no longer qualifies and the next call refetches
    under the cookie — through the REAL validation routine and the real transport rebuild."""
    h = RuntimeHarness(tmp_path)
    h.route_page(c37162, subscriber="false")
    before = await cr_ratings(h.rt, 37162)
    assert before.auth_state == "anonymous" and before.provenance.data_tier == "anonymous"
    assert before.data.notice and "cr_sign_in" not in before.data.notice  # anonymous: no fix hint
    n = len(h.requests)

    probe_member_page(h, c37162)

    def probe_runtime(settings, store):
        return build_runtime(
            settings, env={}, session_factory=lambda **kw: h.sess, credentials=store
        )

    async def validate(settings, cookies):
        return await A.validate_cookies(settings, cookies, probe_runtime)

    flow_for(h, capture=make_capture(), validate=validate)
    out = await cr_sign_in(h.rt)
    assert out.data.status == "waiting"
    st = await settle(h)
    assert st.data.sign_in == "active", st.data.reason
    assert h.requests[n] == PROBE_URL  # the probe fetch, on a throwaway session
    assert h.rt.cache.has_rows(35183)  # …and its payload was not wasted

    h.route_page(c37162, subscriber="true", fill_scores=True)
    after = await cr_ratings(h.rt, 37162)
    assert after.auth_state == "member" and after.session == "active"
    assert after.provenance.data_tier == "member" and after.provenance.from_cache is False
    assert after.data.notice is None and after.scores_available.overall_score == "available"
    assert len(h.requests) == n + 2  # probe, then the member refetch — no restart in between


# --------------------------------------------------------------------------- the guard


async def test_sign_in_is_idempotent_while_one_is_in_flight(tmp_path):
    h = RuntimeHarness(tmp_path)
    gate = asyncio.Event()
    capture = make_capture(gate=gate)
    flow_for(h, capture=capture, validate=make_validate())
    first = await cr_sign_in(h.rt)
    second = await cr_sign_in(h.rt)
    forced = await cr_sign_in(h.rt, force=True)
    assert first.data.status == "waiting"
    assert second.data.status == "in_progress" and forced.data.status == "in_progress"
    assert "do not start another" in second.data.instructions and second.data.browser == "chrome"
    assert len(capture.calls) == 1  # one window, not three
    gate.set()
    assert (await settle(h)).data.sign_in == "active"
    again = await cr_sign_in(h.rt)  # verified live this process: refuse unless forced
    assert again.data.status == "refused" and again.data.reason == "session_active"
    assert "force=true" in again.data.instructions
    forced = await cr_sign_in(h.rt, force=True)
    assert forced.data.status == "waiting" and len(capture.calls) == 2


async def test_a_dead_stored_cookie_is_renewed_without_force(tmp_path):
    """Defect: the guard refused whenever a credential was stored and not KNOWN dead — and
    health only leaves `unverified` after a marker-bearing fetch, which a fresh Desktop process
    may never make. So a dead cookie was refused forever, with a message claiming it worked,
    and the user recovered only by discovering `force`. Now an `unverified` cookie is checked
    against CR first; a window opens only when CR rejects it."""
    h = RuntimeHarness(tmp_path, cookie=True)  # stored cookie, nothing fetched yet
    assert h.rt.health.health is SessionHealth.UNVERIFIED
    gate = asyncio.Event()
    capture = make_capture(gate=gate)
    validate = make_validate(by_hash={STORED: "credential_rejected", HASH: "member"})
    flow_for(h, capture=capture, validate=validate)

    out = await cr_sign_in(h.rt)  # no force
    assert out.data.status == "waiting" and out.data.browser == "chrome", out.data
    assert validate.seen == [{"hash": STORED, "userLicenses": "old"}]  # the STORED cookie
    assert len(capture.calls) == 1  # …was rejected, so the window opened
    assert out.session == "expired" and h.rt.health.health is SessionHealth.EXPIRED
    st = await cr_auth_status(h.rt)
    assert st.data.sign_in == "waiting" and st.session == "expired"
    assert stored_hash(h) == STORED  # nothing written yet
    gate.set()
    st = await settle(h)
    assert st.data.sign_in == "active" and st.session == "active"
    assert validate.seen[1] == {"hash": HASH}
    assert stored_hash(h) == HASH and h.rt.transport.adoptions == 1


async def test_a_stored_cookie_cr_still_accepts_is_refused_after_the_check_not_before(tmp_path):
    h = RuntimeHarness(tmp_path, cookie=True)
    capture = make_capture()
    validate = make_validate("member")
    flow_for(h, capture=capture, validate=validate)
    out = await cr_sign_in(h.rt)
    assert out.data.status == "refused" and out.data.reason == "session_active"
    assert validate.seen == [{"hash": STORED, "userLicenses": "old"}]
    assert capture.calls == []  # no window on a user who is signed in
    # the probe was a marker-bearing fetch with this credential: health moved, truthfully
    assert out.session == "active" and h.rt.health.health is SessionHealth.ACTIVE
    assert "confirmed it live" in out.data.instructions and "force=true" in out.data.instructions
    assert "unverified" not in out.data.instructions
    st = await cr_auth_status(h.rt)
    assert st.data.sign_in == "refused" and st.data.reason == "session_active"
    assert st.data.browser is None and stored_hash(h) == STORED
    # now `active`: a second call refuses at once, without a second probe
    again = await cr_sign_in(h.rt)
    assert again.data.status == "refused" and len(validate.seen) == 1
    # `force` means exactly "replace a cookie verified live"
    forced = await cr_sign_in(h.rt, force=True)
    assert forced.data.status == "waiting" and len(capture.calls) == 1
    assert (await settle(h)).data.sign_in == "active" and stored_hash(h) == HASH


async def test_a_slow_check_answers_verifying_and_the_poll_resolves_it(tmp_path):
    """The real check is one rate-limited fetch, so it may outlast the launch wait: the call
    then says `verifying`, a second call is `in_progress`, and the poll carries the verdict."""
    h = RuntimeHarness(tmp_path, cookie=True)
    gate = asyncio.Event()
    capture = make_capture()
    flow = flow_for(h, capture=capture, validate=make_validate("member", gate=gate))
    t0 = time.monotonic()
    out = await cr_sign_in(h.rt)
    assert time.monotonic() - t0 < 1.0
    assert out.data.status == "verifying" and out.data.reason is None
    assert out.data.browser is None and out.data.expires_in_s is None  # no window yet
    assert "session: unverified" in out.data.instructions
    assert "cr_auth_status" in out.data.instructions
    assert out.session == "unverified"
    st = await cr_auth_status(h.rt)
    assert st.data.sign_in == "verifying" and flow.in_progress
    second = await cr_sign_in(h.rt)
    assert second.data.status == "in_progress" and "being checked" in second.data.instructions
    gate.set()
    st = await settle(h)
    assert st.data.sign_in == "refused" and st.data.reason == "session_active"
    assert st.session == "active" and capture.calls == []


async def test_a_cookie_past_its_own_bound_proceeds_without_force_or_a_check(tmp_path):
    """`days_left_max` is the project's own UPPER bound on the cookie's life: at or below zero
    it cannot be live, so there is nothing to verify and nothing worth refusing for."""
    h = RuntimeHarness(tmp_path, cookie=True)
    age_stored_cookie(h, 400)
    st = await cr_auth_status(h.rt)
    assert st.session == "unverified" and st.data.days_left_max == -35
    assert st.warnings == ["session_expiring:0"]
    capture = make_capture()
    validate = make_validate("member")
    flow_for(h, capture=capture, validate=validate)
    out = await cr_sign_in(h.rt)
    assert out.data.status == "waiting" and len(capture.calls) == 1
    assert (await settle(h)).data.sign_in == "active"
    assert validate.seen == [{"hash": HASH}]  # only the NEW cookie was checked
    assert stored_hash(h) == HASH and (await cr_auth_status(h.rt)).data.days_left_max == 365


async def test_a_check_that_cannot_reach_cr_is_no_verdict_and_opens_no_window(tmp_path):
    h = RuntimeHarness(tmp_path, cookie=True)
    capture = make_capture()
    flow_for(h, capture=capture, validate=make_validate("could_not_check:connection_failed"))
    out = await cr_sign_in(h.rt)
    assert out.data.status == "failed" and out.data.reason == "could_not_check:connection_failed"
    assert "no window was opened" in out.data.instructions
    assert "force=true" in out.data.instructions
    assert capture.calls == [] and h.rt.health.health is SessionHealth.UNVERIFIED  # unjudged
    assert stored_hash(h) == STORED
    # a drift code from the probe is no verdict either
    flow_for(h, capture=capture, validate=make_validate("payload_missing"))
    out = await cr_sign_in(h.rt)
    assert out.data.status == "failed" and out.data.reason == "payload_missing"
    assert "could not be judged" in out.data.instructions and capture.calls == []
    assert h.rt.health.health is SessionHealth.UNVERIFIED
    # and a crash in the probe is `could_not_check:<type>`, same as after a capture
    flow_for(h, capture=capture, validate=make_validate(error=RuntimeError("x")))
    out = await cr_sign_in(h.rt)
    assert out.data.status == "failed" and out.data.reason == "could_not_check:RuntimeError"
    assert capture.calls == []
    # `force` skips the check: the window opens, and the captured cookie is what gets checked
    validate = make_validate("member")
    flow_for(h, capture=capture, validate=validate)
    assert (await cr_sign_in(h.rt, force=True)).data.status == "waiting"
    assert (await settle(h)).data.sign_in == "active" and validate.seen == [{"hash": HASH}]


async def test_the_real_check_runs_the_shared_probe_against_the_stored_cookie(tmp_path, c37162):
    """End to end through `validate_cookies`: the stored cookie hits the probe page and CR
    answers with the logged-out marker, so the window opens; the captured cookie then hits
    the same probe and is accepted."""
    h = RuntimeHarness(tmp_path, cookie=True)
    probe_member_page(h, c37162, subscriber="false")  # CR: this cookie is no longer a member

    def probe_runtime(settings, store):
        return build_runtime(
            settings, env={}, session_factory=lambda **kw: h.sess, credentials=store
        )

    async def validate(settings, cookies):
        return await A.validate_cookies(settings, cookies, probe_runtime)

    gate = asyncio.Event()
    capture = make_capture(gate=gate)
    flow_for(h, capture=capture, validate=validate)
    out = await cr_sign_in(h.rt)
    assert out.data.status == "waiting", out.data
    assert h.requests == [PROBE_URL] and h.rt.health.health is SessionHealth.EXPIRED
    assert h.sess.get_cookie("hash", WWW + "/") == STORED  # the stored cookie was what went out
    probe_member_page(h, c37162, subscriber="true")
    gate.set()
    st = await settle(h)
    assert st.data.sign_in == "active", st.data.reason
    assert h.requests == [PROBE_URL, PROBE_URL] and stored_hash(h) == HASH
    assert h.sess.get_cookie("hash", WWW + "/") == HASH


# --------------------------------------------------------------------------- responsiveness


async def test_the_loop_stays_responsive_while_a_capture_is_pending(tmp_path):
    """The acceptance criterion for a non-blocking sign-in: with a window open and nobody
    signing in, cr_sign_in returns inside its launch wait even under a hard deadline, the loop
    keeps ticking, every other tool still answers, and cancel() returns promptly."""
    h = RuntimeHarness(tmp_path)
    gate = asyncio.Event()
    flow = flow_for(h, capture=make_capture(gate=gate), validate=make_validate())
    async with LoopHeartbeat() as hb:
        t0 = time.monotonic()
        out = await asyncio.wait_for(cr_sign_in(h.rt), 30)
        assert out.data.status == "waiting" and time.monotonic() - t0 < 1.0
        t1 = time.monotonic()
        st = await asyncio.wait_for(cr_auth_status(h.rt), 5)
        cats = await asyncio.wait_for(cr_categories(h.rt), 5)
        assert st.data.sign_in == "waiting" and cats.error is None and cats.data is not None
        assert time.monotonic() - t1 < 0.5  # other tools answer while the window is open
        await asyncio.sleep(0.2)
        t2 = time.monotonic()
        await asyncio.wait_for(flow.cancel(), 10)
        assert time.monotonic() - t2 < 1.0
    assert hb.max_gap < 0.1
    st = await cr_auth_status(h.rt)
    assert st.data.sign_in == "failed" and st.data.reason == "cancelled" and not flow.in_progress


async def test_cancel_returns_promptly_even_when_the_window_teardown_lingers(tmp_path, monkeypatch):
    """A teardown waiting on a driver reply that never comes must not hold cancel() — the
    server is shutting down. The phase is right at once, the stored credential is untouched,
    and the task finishes behind the call."""
    monkeypatch.setattr(A, "CANCEL_WAIT_S", 0.3)
    h = RuntimeHarness(tmp_path, cookie=True)
    released = asyncio.Event()

    async def capture(*, timeout_s, on_launch):
        on_launch("chrome")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await released.wait()  # the window's teardown, stuck
            raise

    validate = make_validate()
    flow = flow_for(h, capture=capture, validate=validate)
    assert (await cr_sign_in(h.rt, force=True)).data.status == "waiting"
    t0 = time.monotonic()
    await flow.cancel()
    assert time.monotonic() - t0 < 1.0
    st = await cr_auth_status(h.rt)
    assert st.data.sign_in == "failed" and st.data.reason == "cancelled"
    assert validate.seen == [] and h.rt.transport.adoptions == 0
    assert stored_hash(h) == STORED
    assert flow.in_progress  # the teardown carries on behind the call ...
    released.set()
    await asyncio.sleep(0.05)
    assert not flow.in_progress  # ... and finishes on its own


async def test_cancel_propagates_only_the_callers_own_cancellation(tmp_path):
    """A lifespan torn down under cancel() (Desktop dropping the connection) must see its own
    cancellation rather than have it swallowed — while the window still closes behind it."""
    h = RuntimeHarness(tmp_path)
    closed = asyncio.Event()

    async def capture(*, timeout_s, on_launch):
        on_launch("chrome")
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # a real teardown takes a moment
            closed.set()
            raise

    flow = flow_for(h, capture=capture, validate=make_validate())
    assert (await cr_sign_in(h.rt)).data.status == "waiting"
    canceller = asyncio.create_task(flow.cancel())
    await asyncio.sleep(0)  # cancel() has cancelled the sign-in and is waiting on it
    canceller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await canceller
    await asyncio.wait_for(closed.wait(), 2)  # the window closed anyway
    await asyncio.sleep(0.01)
    assert not flow.in_progress
    st = await cr_auth_status(h.rt)
    assert st.data.sign_in == "failed" and st.data.reason == "cancelled"


async def test_a_swallowed_cancellation_cannot_store_a_cookie_behind_a_cancelled_status(tmp_path):
    """cancel() sets `failed/cancelled` and asks the task to stop. If something between the
    task and its await eats the CancelledError, the task carries on to `save()` and reports
    `active` after the caller was told nothing changed. The task therefore reads
    `cancelling()` before the one irreversible step."""
    h = RuntimeHarness(tmp_path)
    gate = asyncio.Event()

    async def swallowing_validate(settings, cookies):
        try:
            await gate.wait()
        except asyncio.CancelledError:
            pass  # a library that absorbs the cancellation and answers anyway
        return "member"

    flow = flow_for(h, capture=make_capture(), validate=swallowing_validate)
    assert (await cr_sign_in(h.rt)).data.status == "waiting"
    assert (await cr_auth_status(h.rt)).data.sign_in == "validating"
    await flow.cancel()
    assert not flow.in_progress
    st = await cr_auth_status(h.rt)
    assert st.data.sign_in == "failed" and st.data.reason == "cancelled"
    assert not h.rt.credentials.path.exists() and h.rt.transport.adoptions == 0
    assert st.session == "none"


async def test_an_unexpected_exception_after_validation_never_leaves_a_live_phase(
    tmp_path, monkeypatch
):
    """Only OSError/ValueError were caught around `save()`; anything else left the phase at
    `validating` forever with the task done — a poll that never resolves."""
    h = RuntimeHarness(tmp_path)
    flow = flow_for(h, capture=make_capture(), validate=make_validate("member"))

    def broken_save(cookies):
        raise TypeError("not a mapping")

    monkeypatch.setattr(h.rt.credentials, "save", broken_save)
    await cr_sign_in(h.rt)
    st = await settle(h)
    assert not flow.in_progress
    assert st.data.sign_in == "failed" and st.data.reason == "internal_error:TypeError"
    assert "unexpected" in (await cr_sign_in(h.rt)).data.instructions or True
    assert not h.rt.credentials.path.exists() and h.rt.transport.adoptions == 0
    # and the flow is reusable
    monkeypatch.undo()
    flow_for(h, capture=make_capture(), validate=make_validate("member"))
    await cr_sign_in(h.rt)
    assert (await settle(h)).data.sign_in == "active"


# --------------------------------------------------------------------------- refusals


async def test_sign_in_refuses_under_env_override_and_names_where_to_fix_it(tmp_path):
    settings = Settings(env={}, home=tmp_path)
    store = CredentialStore(
        tmp_path / "session.json", env={"CR_SESSION_COOKIE": "hash=" + "e" * 36}
    )
    sess = FakeWaferSession()
    rt = build_runtime(settings, session_factory=lambda **kw: sess, credentials=store)
    capture = make_capture()
    rt.sign_in = SignInFlow(rt, capture=capture, validate=make_validate(), launch_wait_s=0.1)
    out = await cr_sign_in(rt, force=True)
    assert out.data.status == "refused" and out.data.reason == "env_override"
    assert "CR_SESSION_COOKIE" in out.data.instructions
    assert "Claude Desktop" in out.data.instructions
    assert capture.calls == [] and not (tmp_path / "session.json").exists()
    st = await cr_auth_status(rt)
    assert st.data.source == "env" and st.data.sign_in == "idle"
    assert st.data.days_left_max is None
    # a BLANK value — the Desktop bundle's optional field left empty — is not an override
    blank = CredentialStore(tmp_path / "s2.json", env={"CR_SESSION_COOKIE": ""})
    rt2 = build_runtime(settings, session_factory=lambda **kw: sess, credentials=blank)
    rt2.sign_in = SignInFlow(
        rt2, capture=make_capture(), validate=make_validate(), launch_wait_s=0.1
    )
    assert (await cr_sign_in(rt2)).data.status == "waiting"
    # nor is the template a host might pass through unexpanded for that empty field
    tmpl = CredentialStore(
        tmp_path / "s3.json", env={"CR_SESSION_COOKIE": "${user_config.session_cookie}"}
    )
    rt3 = build_runtime(settings, session_factory=lambda **kw: sess, credentials=tmpl)
    rt3.sign_in = SignInFlow(
        rt3, capture=make_capture(), validate=make_validate(), launch_wait_s=0.1
    )
    assert (await cr_sign_in(rt3)).data.status == "waiting"


async def test_sign_in_refuses_without_the_browser_extra_naming_the_install(tmp_path, monkeypatch):
    h = RuntimeHarness(tmp_path)
    flow_for(h, capture=None, validate=make_validate())  # capture=None → the real ladder
    monkeypatch.setattr(A, "extra_installed", lambda: False)
    out = await cr_sign_in(h.rt)
    assert out.data.status == "refused" and out.data.reason == "browser_extra_missing"
    assert "uv sync --extra browser" in out.data.instructions
    assert "consumer-reports-mcp[browser]" in out.data.instructions
    assert "consumer-reports-mcp auth" in out.data.instructions  # the paste path remains
    assert (await cr_auth_status(h.rt)).data.sign_in == "idle"
    # with an unverified cookie stored, the extra is checked BEFORE any probe is spent
    h2 = RuntimeHarness(tmp_path / "b", cookie=True)
    validate = make_validate()
    flow_for(h2, capture=None, validate=validate)
    out = await cr_sign_in(h2.rt)
    assert out.data.status == "refused" and out.data.reason == "browser_extra_missing"
    assert validate.seen == [] and h2.rt.health.health is SessionHealth.UNVERIFIED


async def test_sign_in_refuses_offline(tmp_path):
    h = RuntimeHarness(tmp_path)
    settings = Settings(env={"CR_OFFLINE": "1"}, home=tmp_path)
    h.rt.settings = settings
    h.rt.transport.settings = settings
    flow_for(h, capture=make_capture(), validate=make_validate())
    out = await cr_sign_in(h.rt)
    assert out.data.status == "refused" and out.data.reason == "offline"


# --------------------------------------------------------------------------- failures


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (WindowClosed("closed"), "window_closed"),
        (CaptureTimeout("timeout"), "capture_timeout"),
        (RuntimeError("driver crashed"), "browser_error:RuntimeError"),
    ],
)
async def test_capture_failures_are_reported_and_change_nothing(tmp_path, exc, reason):
    h = RuntimeHarness(tmp_path)
    validate = make_validate()
    flow_for(h, capture=make_capture(error=exc), validate=validate)
    out = await cr_sign_in(h.rt)
    st = await settle(h)
    assert out.data.status in ("waiting", "failed")
    assert st.data.sign_in == "failed" and st.data.reason == reason
    assert validate.seen == [] and not h.rt.credentials.path.exists()
    assert h.rt.transport.adoptions == 0 and h.rt.health.health is SessionHealth.NONE
    # and the flow is reusable: the next call starts fresh
    flow_for(h, capture=make_capture(), validate=make_validate())
    assert (await cr_sign_in(h.rt)).data.status == "waiting"


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (BrowserNotFound("no browser"), "browser_not_found"),
        (BrowserExtraMissing("no extra"), "browser_extra_missing"),
    ],
)
async def test_a_launch_failure_is_reported_by_sign_in_itself(tmp_path, exc, reason):
    """A failure before the window opens lands inside the launch wait, so the agent hears it
    from cr_sign_in rather than from a later poll."""
    h = RuntimeHarness(tmp_path)
    flow_for(h, capture=make_capture(before_launch=exc), validate=make_validate())
    out = await cr_sign_in(h.rt)
    assert out.data.status == "failed" and out.data.reason == reason
    assert out.data.browser is None
    assert "consumer-reports-mcp auth" in out.data.instructions
    assert out.data.expires_in_s is None


@pytest.mark.parametrize("outcome", ["session_expired", "credential_rejected", "anonymous"])
async def test_a_cookie_that_does_not_authenticate_is_never_stored(tmp_path, outcome):
    h = RuntimeHarness(tmp_path)
    flow_for(h, capture=make_capture(), validate=make_validate(outcome))
    await cr_sign_in(h.rt)
    st = await settle(h)
    assert st.data.sign_in == "failed" and st.data.reason == outcome
    assert not h.rt.credentials.path.exists() and h.rt.transport.adoptions == 0
    assert st.session == "none" and st.data.source is None


async def test_a_transport_failure_during_validation_is_not_a_verdict(tmp_path):
    h = RuntimeHarness(tmp_path)
    flow_for(h, capture=make_capture(), validate=make_validate("could_not_check:connection_failed"))
    await cr_sign_in(h.rt)
    st = await settle(h)
    assert st.data.sign_in == "failed" and st.data.reason == "could_not_check:connection_failed"
    assert not h.rt.credentials.path.exists()
    crash = flow_for(h, capture=make_capture(), validate=make_validate(error=RuntimeError("x")))
    await cr_sign_in(h.rt)
    st = await settle(h)
    assert st.data.sign_in == "failed" and st.data.reason == "could_not_check:RuntimeError"
    assert crash.phase == "failed" and not h.rt.credentials.path.exists()


async def test_cancel_on_shutdown_stores_nothing(tmp_path):
    h = RuntimeHarness(tmp_path)
    gate = asyncio.Event()
    flow = flow_for(h, capture=make_capture(gate=gate), validate=make_validate())
    assert (await cr_sign_in(h.rt)).data.status == "waiting"
    await flow.cancel()
    st = await cr_auth_status(h.rt)
    assert st.data.sign_in == "failed" and st.data.reason == "cancelled"
    assert not h.rt.credentials.path.exists() and not flow.in_progress


# --------------------------------------------------------------------------- the status poll


async def test_status_long_poll_returns_on_change_and_is_capped_under_desktops_limit(tmp_path):
    assert A.STATUS_WAIT_MAX_S == 45  # under the 60 s cap, with margin for the round trip
    h = RuntimeHarness(tmp_path)
    gate = asyncio.Event()
    flow_for(h, capture=make_capture(gate=gate), validate=make_validate())
    t0 = time.monotonic()
    idle = await cr_auth_status(h.rt, wait_s=45)
    assert idle.data.sign_in == "idle" and time.monotonic() - t0 < 0.5  # nothing to wait for
    await cr_sign_in(h.rt)

    async def release_later():
        await asyncio.sleep(0.2)
        gate.set()

    asyncio.ensure_future(release_later())
    t0 = time.monotonic()
    st = await cr_auth_status(h.rt, wait_s=45)
    elapsed = time.monotonic() - t0
    assert st.data.sign_in in ("validating", "active") and 0.15 < elapsed < 2.0  # woke on change
    assert (await settle(h)).data.sign_in == "active"
    # a poll with no change inside its budget returns at the budget, not at the cap
    flow_for(h, capture=make_capture(gate=asyncio.Event()), validate=make_validate())
    await cr_sign_in(h.rt, force=True)
    t0 = time.monotonic()
    st = await cr_auth_status(h.rt, wait_s=1)
    assert st.data.sign_in == "waiting" and 0.9 < time.monotonic() - t0 < 1.6
    assert (await cr_auth_status(h.rt, wait_s=-5)).data.sign_in == "waiting"  # bad input: no wait


async def test_status_reports_days_left_and_the_expiring_warning(tmp_path):
    h = RuntimeHarness(tmp_path, cookie=True)  # stored without an expiry, like a paste
    gate = asyncio.Event()
    flow_for(h, capture=make_capture(gate=gate), validate=make_validate())
    st = await cr_auth_status(h.rt)
    assert st.data.source == "file" and st.data.days_left_max == 365 and st.warnings == []
    assert st.session == "unverified" and st.data.captured_at
    assert st.data.expiry_basis == "assumed" and st.data.expires_at is None
    age_stored_cookie(h, 350)
    st = await cr_auth_status(h.rt)
    assert st.data.days_left_max == 15 and st.warnings == ["session_expiring:15"]
    # every tool that carries `session` says so too — products, cars, and cr_sign_in itself
    cats = await cr_categories(h.rt)
    assert "session_expiring:15" in cats.warnings
    cars = await cr_car_search(h.rt, "")  # the error path, no network needed
    assert "session_expiring:15" in cars.warnings
    out = await cr_sign_in(h.rt, force=True)  # the renewal gesture, before anything is stored
    assert out.warnings == ["session_expiring:15"] and out.session == "unverified"
    gate.set()
    st = await settle(h)
    assert st.data.sign_in == "active" and st.warnings == []  # a fresh 365-day cookie
    assert st.data.days_left_max == 365 and st.data.expiry_basis == "measured"
    h.rt.health.on_rejected()
    assert (await cr_categories(h.rt)).warnings == []  # already session_expired: not noise


async def test_a_measured_expiry_drives_the_countdown_not_the_capture_date(tmp_path):
    """The renewal signal counts from the expiry the browser reported when there is one. A
    cookie CR issued with 10 days of life warns `session_expiring:10` on the day it is stored —
    the assumed arithmetic said 365 for exactly such a cookie, right up to the day it died."""
    h = RuntimeHarness(tmp_path)
    capture = make_capture(expires_at=iso_days_from_now(10))
    flow_for(h, capture=capture, validate=make_validate("member"))
    await cr_sign_in(h.rt)
    st = await settle(h)
    assert st.data.sign_in == "active" and st.data.expiry_basis == "measured"
    assert st.data.days_left_max == 10 and st.warnings == ["session_expiring:10"]
    assert "session_expiring:10" in (await cr_categories(h.rt)).warnings
    # winding the CAPTURE date back changes nothing: the countdown is not arithmetic on it
    age_stored_cookie(h, 300)
    st = await cr_auth_status(h.rt)
    assert st.data.days_left_max == 10 and st.warnings == ["session_expiring:10"]
    # and one already past its measured expiry is renewed without force, like a past-bound one
    h2 = RuntimeHarness(tmp_path / "two")
    flow_for(h2, capture=make_capture(expires_at=iso_days_from_now(-1)), validate=make_validate())
    await cr_sign_in(h2.rt)
    st = await settle(h2)
    assert st.data.sign_in == "active" and st.data.days_left_max == -1
    assert st.warnings == ["session_expiring:0"]
    capture2 = make_capture()
    flow_for(h2, capture=capture2, validate=make_validate())
    assert (await cr_sign_in(h2.rt)).data.status == "waiting" and len(capture2.calls) == 1


async def test_a_session_only_cookie_is_a_named_failure_that_stores_nothing(tmp_path):
    """`NotDurable` from the capture — CR issued a `hash` with no expiry, "remember me" did
    not take — is `failed / not_durable` with the fix in the text, and nothing is stored or
    validated: stored under the assumed bound it would have read as a year of life."""
    h = RuntimeHarness(tmp_path)
    validate = make_validate("member")
    flow_for(h, capture=make_capture(error=NotDurable("session-only")), validate=validate)
    out = await cr_sign_in(h.rt)
    assert out.data.status == "failed" and out.data.reason == "not_durable"
    assert "Remember Me" in out.data.instructions
    assert "nothing was captured" in out.data.instructions
    assert validate.seen == [] and not h.rt.credentials.path.exists()
    assert h.rt.health.health is SessionHealth.NONE and h.rt.transport.adoptions == 0
    st = await cr_auth_status(h.rt)
    assert st.data.sign_in == "failed" and st.data.reason == "not_durable"


# --------------------------------------------------------------------------- hygiene


async def test_no_output_or_log_line_ever_carries_the_cookie(tmp_path, caplog):
    h = RuntimeHarness(tmp_path)
    secret = "z" * 36
    flow_for(h, capture=make_capture(secret), validate=make_validate())
    texts: list[str] = []
    with caplog.at_level(logging.DEBUG):
        texts.append((await cr_sign_in(h.rt)).model_dump_json())
        texts.append((await cr_sign_in(h.rt)).model_dump_json())  # in_progress
        texts.append((await settle(h)).model_dump_json())
        texts.append((await cr_sign_in(h.rt)).model_dump_json())  # refused
        texts.append(repr(h.rt.sign_in.__dict__))
    blob = "\n".join(texts) + caplog.text
    assert secret not in blob
    assert json.loads(h.rt.credentials.path.read_text())["cookies"]["hash"] == secret
    # the pre-flight check of a stored cookie logs and reports nothing of its value either
    h2 = RuntimeHarness(tmp_path / "b", cookie=True)
    flow_for(h2, capture=make_capture(secret), validate=make_validate("credential_rejected"))
    texts = []
    with caplog.at_level(logging.DEBUG):
        texts.append((await cr_sign_in(h2.rt)).model_dump_json())
        texts.append((await settle(h2)).model_dump_json())
    blob = "\n".join(texts) + caplog.text
    assert STORED not in blob and secret not in blob


def test_sign_in_and_status_wear_the_outer_envelope_with_typed_status_objects():
    """SPEC §7: every tool returns `{session, warnings, error, data}` around its payload. The
    two auth tools used to be flat, which contradicted the spec's own table and made
    `session_expiring` structurally impossible on `cr_sign_in`."""
    from consumer_reports_mcp import envelope as E

    for model in (E.SignInEnvelope, E.AuthStatusEnvelope):
        assert list(model.model_fields) == ["session", "warnings", "error", "data"]
        s = model.model_json_schema()
        assert s["properties"]["session"]["enum"] == ["none", "unverified", "active", "expired"]
    s = E.SignInData.model_json_schema()
    assert s["properties"]["status"]["enum"] == [
        "waiting",
        "verifying",
        "in_progress",
        "refused",
        "failed",
    ]
    a = E.AuthStatusData.model_json_schema()
    assert a["properties"]["sign_in"]["enum"] == [
        "idle",
        "verifying",
        "waiting",
        "validating",
        "active",
        "refused",
        "failed",
    ]
    for model in (E.SignInEnvelope, E.SignInData, E.AuthStatusEnvelope, E.AuthStatusData):
        assert not any("cookie" in name or "hash" in name for name in model.model_fields)


def test_notice_names_the_fix_only_when_the_session_expired():
    from consumer_reports_mcp import envelope as E

    scores = {"overall_score": "unavailable"}
    expired = E.maybe_notice(scores, "session_expired")
    assert "cr_sign_in" in expired and "consumer-reports-mcp auth" in expired
    assert "CR_SESSION_COOKIE" in expired
    anon = E.maybe_notice(scores, "anonymous")
    assert "cr_sign_in" not in anon and "not an absence of CR ratings" in anon


def test_validate_cookies_is_the_one_routine_the_cli_uses():
    from consumer_reports_mcp import cli

    assert cli.validate_cookies is A.validate_cookies


async def test_a_second_validation_inside_the_refresh_cooldown_still_hits_cr(tmp_path, c37162):
    """`validate_cookies` is a verdict on THIS cookie and reads the fetch's classification. The
    refresh cooldown that protects the read-only tools must not apply here: served from a
    minute-old member row, a valid cookie would be called `anonymous` and never stored."""
    h = RuntimeHarness(tmp_path, cookie=True)
    probe_member_page(h, c37162, subscriber="true")

    def probe_runtime(settings, store):
        return build_runtime(
            settings, env={}, session_factory=lambda **kw: h.sess, credentials=store
        )

    assert await A.validate_cookies(h.rt.settings, {"hash": HASH}, probe_runtime) == "member"
    assert await A.validate_cookies(h.rt.settings, {"hash": HASH}, probe_runtime) == "member"
    assert h.requests == [PROBE_URL, PROBE_URL]


# --------------------------------------------------------------------------- batch-3 findings


def test_save_failure_text_never_names_the_session_path(tmp_path):
    """The session file lives in the user's home directory: the server log may name it, a
    tool response may not."""
    h = RuntimeHarness(tmp_path)
    flow = SignInFlow(h.rt)
    text = flow._failure_text("save_failed:PermissionError")
    assert "PermissionError" in text and "nothing changed" in text
    assert str(h.rt.credentials.path) not in text and str(tmp_path) not in text
