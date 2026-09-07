"""`cr_sign_in` and `cr_auth_status` (SPEC §6 *cr_sign_in*, §7): the in-conversation sign-in.

No mcp import (D4): plain async functions over a `Runtime`, wrapped by `server.py`. Nothing here
returns, logs or formats a cookie value — the token travels `capture_hash()` → the probe
validation → `CredentialStore.save()` and nowhere else. Its expiry travels with it: the browser
reports one, and it is stored so the renewal countdown counts from CR's own date rather than
from the assumed 365-day bound (`credentials`).

Why a background task and a status poll rather than one blocking call: Claude Desktop kills a
local tool call at 60 s and progress notifications do not extend it, while a human sign-in takes
as long as it takes. So `cr_sign_in` starts the flow and answers within ~2 s, and
`cr_auth_status(wait_s=…)` long-polls under the cap. Elicitation is not an option: Desktop
declares no elicitation capability to a local stdio server.

Why the guard VERIFIES rather than refuses: session health only leaves `unverified` after a
marker-bearing fetch, and in a fresh Desktop process nothing may ever make one. A guard that
refuses on `unverified` therefore refuses a dead cookie forever and the user recovers only by
discovering `force` — which makes the guard advisory, and defeats the renewal it exists for. So
an `unverified` credential is checked against CR first (the same probe as after a capture, one
request), and a window opens only when CR rejects it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from . import envelope as E
from . import ingest
from .browser_auth import (
    INSTALL_COMMAND,
    BrowserExtraMissing,
    BrowserNotFound,
    Capture,
    CaptureTimeout,
    NotDurable,
    WindowClosed,
    capture_hash,
    extra_installed,
)
from .config import AUTH_PROBE_CATEGORY, Settings
from .credentials import (
    DEAD_VERDICTS,
    DURABLE_COOKIE,
    ENV_VAR,
    VERDICT_MEMBER,
    CredentialStore,
    SessionHealth,
    default_store,
)
from .repository import Served, ToolError

if TYPE_CHECKING:
    from .runtime import Runtime

log = logging.getLogger(__name__)

SIGN_IN_TIMEOUT_S = 300  # how long the window stays open — the same default as `auth --timeout`
LAUNCH_WAIT_S = 1.5  # cr_sign_in waits this long for the browser to report before answering
STATUS_WAIT_MAX_S = 45  # the long-poll cap: under Claude Desktop's 60 s tool-call limit
CANCEL_WAIT_S = 10  # cancel() returns within this even if the window's teardown is slower

RuntimeFactory = Callable[[Settings, CredentialStore], "Runtime"]
CaptureFn = Callable[..., Awaitable[Capture]]
ValidateFn = Callable[[Settings, Mapping[str, str]], Awaitable[str]]

# The verdicts `validate_cookies` can return that mean "CR looked at this cookie and said no"
# are `credentials.DEAD_VERDICTS` — the vocabulary `SessionState.on_probe_verdict` listens to.
# Anything else — `could_not_check:<reason>`, a drift code, `anonymous` — is not a verdict.
assert DEAD_VERDICTS == {ingest.SESSION_EXPIRED, ingest.CREDENTIAL_REJECTED}
assert VERDICT_MEMBER == ingest.MEMBER


# --------------------------------------------------------------------------- validation


async def validate_cookies(
    settings: Settings,
    cookies: Mapping[str, str],
    runtime_factory: RuntimeFactory | None = None,
) -> str:
    """One real fetch of the probe category with the cookies held in MEMORY — nothing is written
    unless the caller decides to. Shared by `auth` (CLI), `cr_sign_in`'s post-capture check and
    its pre-flight check of a stored-but-unverified cookie, so there is one verdict routine.
    Returns `member`, `session_expired`, `credential_rejected`, `could_not_check:<reason>` for a
    transport failure that is no verdict on the cookie, or another fetch code. The fetched
    payload is cached like any other."""
    from .runtime import build_runtime

    probe_store = default_store(settings.config_dir, env={})
    probe_store.preload(cookies)
    rt = (runtime_factory or (lambda s, c: build_runtime(s, env={}, credentials=c)))(
        settings, probe_store
    )
    try:
        # `cooldown=False`: this is a verdict on the cookie and reads the fetch's
        # classification, so it must always go to the network (SPEC §8 *Retention*)
        outcome = await rt.repository.get_category(
            AUTH_PROBE_CATEGORY, refresh=True, validate=False, cooldown=False
        )
        if isinstance(outcome, ToolError):
            if outcome.code in ("fetch_failed", "challenged"):
                return f"could_not_check:{outcome.reason or outcome.code}"
            return outcome.reason or outcome.code
        assert isinstance(outcome, Served)
        if outcome.fetch_kind == ingest.MEMBER:
            return "member"
        if rt.credentials.rejected:  # CR redirected to /ec/login and the retry was anonymous
            return "credential_rejected"
        if outcome.fetch_kind == ingest.SESSION_EXPIRED:
            return "session_expired"
        for w in outcome.warnings:
            if w.startswith("refresh_failed:"):
                code = w.split(":", 1)[1]
                if code in ("fetch_failed", "challenged"):
                    return f"could_not_check:{code}"
                return code
        return outcome.fetch_kind or "anonymous"
    finally:
        # the probe runtime's session is a second wafer session for exactly one request; it is
        # released here so it never competes with the process session for the rate limit
        await rt.transport.aclose()


# --------------------------------------------------------------------------- the flow


class SignInFlow:
    """One sign-in at a time per process: a background task plus a phase the status tool reads.

    Phases: idle → [verifying →] waiting (a window is open) → validating (a token was captured)
    → active | refused | failed. `verifying` is the pre-flight check of a stored `unverified`
    cookie; it ends in `refused` (CR still accepts it: nothing changed) or moves on to `waiting`
    (CR rejected it: the window opens). `reason` is machine-readable and never a value.
    Injection points exist for tests only: `capture` replaces `browser_auth.capture_hash`,
    `validate` replaces `validate_cookies` bound to a runtime factory."""

    def __init__(
        self,
        rt: Runtime,
        *,
        capture: CaptureFn | None = None,
        validate: ValidateFn | None = None,
        timeout_s: int = SIGN_IN_TIMEOUT_S,
        launch_wait_s: float = LAUNCH_WAIT_S,
    ) -> None:
        self.rt = rt
        self._capture = capture
        self._validate: ValidateFn = validate or validate_cookies
        self.timeout_s = timeout_s
        self.launch_wait_s = launch_wait_s
        self.phase: str = "idle"
        self.reason: str | None = None
        self.browser: str | None = None
        self._deadline: float | None = None
        self._task: asyncio.Task | None = None
        self._changed = asyncio.Event()
        self._version = 0
        self._captured = False  # whether the CURRENT run got as far as capturing a token

    # --- state ---------------------------------------------------------------------
    @property
    def in_progress(self) -> bool:
        return self._task is not None and not self._task.done()

    def _pulse(self) -> None:
        self._version += 1
        self._changed.set()  # wake every waiter …
        self._changed.clear()  # … and arm for the next change

    def _set(self, phase: str, reason: str | None = None) -> None:
        self.phase, self.reason = phase, reason
        self._pulse()

    def _on_launch(self, label: str) -> None:
        self.browser = label
        log.info("sign-in: %s opened on the sign-in page", label)
        self._pulse()

    async def _wait_until(self, done: Callable[[], bool], budget_s: float) -> None:
        """Wait for phase changes until `done()` holds or the budget is spent."""
        deadline = time.monotonic() + budget_s
        while not done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except TimeoutError:
                return

    async def _wait_for_change(self, seen: int, budget_s: float) -> None:
        await self._wait_until(lambda: self._version != seen, budget_s)

    def expires_in_s(self) -> int | None:
        if self._deadline is None or not self.in_progress:
            return None
        return max(int(self._deadline - time.monotonic()), 0)

    @staticmethod
    def _cancel_requested() -> bool:
        """Whether `cancel()` has asked this task to stop — even if a library between here and
        the await swallowed the `CancelledError` (py3.11+ `cancelling()` counts requests, not
        deliveries). Read before the one irreversible step, `save()`."""
        task = asyncio.current_task()
        return task is not None and task.cancelling() > 0

    # --- cr_sign_in ------------------------------------------------------------------
    async def start(self, *, force: bool = False) -> E.SignInEnvelope:
        rt = self.rt
        if self.in_progress:
            if self.phase == "verifying":
                text = (
                    "A sign-in is already in progress: the stored session is being checked "
                    "against Consumer Reports, and a window opens only if it is rejected. Call "
                    "cr_auth_status(wait_s=45) to wait for it; do not start another."
                )
            else:
                text = (
                    "A sign-in is already in progress"
                    + (f" in {self.browser}" if self.browser else "")
                    + ". Call cr_auth_status(wait_s=45) to wait for it; do not start another."
                )
            return self._answer("in_progress", None, text)
        if rt.credentials.env_override:
            return self._answer(
                "refused",
                "env_override",
                f"{ENV_VAR} is set, and it takes precedence over anything a sign-in would store, "
                "so a captured cookie would be ignored on the next start. Clear it where the "
                "server is configured — the extension's settings in Claude Desktop (the "
                '"Session cookie" field), or the env block of the MCP server entry, or the '
                "shell — then call cr_sign_in again; or keep it and update its value instead.",
            )
        if rt.settings.offline:
            return self._answer(
                "refused",
                "offline",
                "CR_OFFLINE is set, so a captured cookie could not be checked against Consumer "
                "Reports. Unset it and call cr_sign_in again.",
            )
        # The guard. `force` means one thing only: replace a cookie verified live. Without it:
        #   - a cookie past its expiry — measured, or the assumed 365-day bound — cannot be
        #     live → proceed (renewal);
        #   - `active` (a marker-bearing fetch THIS process saw it live) → refuse;
        #   - `unverified` → resolve, not refuse blind: check the stored cookie against CR in
        #     the background and open a window only if CR rejects it. Health only ever leaves
        #     `unverified` on a marker-bearing fetch, which a fresh Desktop process may never
        #     make, so a blind refusal here would refuse a dead cookie forever;
        #   - `none` / `expired` → proceed.
        health = rt.health.health
        verify: dict[str, str] | None = None
        if not force and health in (SessionHealth.ACTIVE, SessionHealth.UNVERIFIED):
            left = rt.credentials.status().get("remaining_days_max")
            if left is not None and left <= 0:
                log.info(
                    "sign-in: the stored cookie is past its expiry (%s days, %s); renewing",
                    left,
                    rt.credentials.status().get("expiry_basis"),
                )
            elif health is SessionHealth.ACTIVE:
                return self._answer("refused", "session_active", self._session_active_text())
            else:
                verify = rt.credentials.cookies
        if self._capture is None and not extra_installed():
            return self._answer(
                "refused",
                "browser_extra_missing",
                "The browser extra is not installed, so no window can be opened. Install it "
                f"with `{INSTALL_COMMAND}` and restart the server, or run "
                "`consumer-reports-mcp auth` in a terminal and paste the cookie there.",
            )
        self.browser = None
        self._deadline = None
        self._captured = False
        self._set("verifying" if verify is not None else "waiting")
        self._task = asyncio.create_task(self._run(verify), name="cr-sign-in")
        # answer within seconds either way: a launch failure, or a stored cookie CR still
        # accepts, is reported now; a slow launch or a slow check is reported by
        # cr_auth_status — the user, not this call, is what takes minutes
        await self._wait_until(self._answerable, self.launch_wait_s)
        return self._answer_for_phase()

    def _answerable(self) -> bool:
        """`start()` has something definite to say: not still verifying, and not `waiting`
        with the browser yet to report."""
        if self.phase == "verifying":
            return False
        return not (self.phase == "waiting" and self.browser is None)

    def _answer_for_phase(self) -> E.SignInEnvelope:
        if self.phase == "failed":
            return self._answer("failed", self.reason, self._failure_text(self.reason))
        if self.phase == "refused":
            return self._answer("refused", self.reason, self._session_active_text())
        if self.phase == "verifying":
            return self._answer(
                "verifying",
                None,
                "A member session is stored but has not been confirmed in this process "
                "(session: unverified), so it is being checked against Consumer Reports before "
                "any window opens: if CR still accepts it, nothing changes and the sign-in is "
                "refused (session_active); if CR rejects it, a sign-in window opens. Call "
                "cr_auth_status(wait_s=45) for the outcome.",
            )
        return self._answer(
            "waiting",
            None,
            f"A {self.browser or 'browser'} window has opened on Consumer Reports' sign-in page. "
            'Ask the user to sign in there, leaving "remember me" ticked; the server never '
            "sees the password and keeps only the resulting session cookie. The window closes "
            f"itself once signed in and gives up after {self.expires_in_s() or 0} s. Call "
            "cr_auth_status(wait_s=45) to wait for the outcome.",
        )

    def _answer(self, status: str, reason: str | None, instructions: str) -> E.SignInEnvelope:
        rt = self.rt
        return E.SignInEnvelope(
            session=rt.health.health.value,
            warnings=rt.session_warnings(),
            error=None,
            data=E.SignInData(
                status=status,
                reason=reason,
                instructions=instructions,
                browser=self.browser,
                expires_in_s=self.expires_in_s(),
            ),
        )

    def _session_active_text(self) -> str:
        # only ever said once a marker-bearing fetch in THIS process saw the cookie live — a
        # prior data fetch, or the pre-flight check moments ago — so "working" is known, not
        # assumed
        return (
            "A member session is stored and Consumer Reports confirmed it live in this process "
            f"(session: {self.rt.health.health.value}), so signing in again would only replace "
            "a working cookie. Pass force=true to replace it anyway — for example to renew a "
            "cookie that is about to expire (cr_auth_status reports days_left_max). A cookie CR "
            "has rejected (session: expired), or one past its expiry, is renewed without force."
        )

    def _failure_text(self, reason: str | None) -> str:
        r = reason or ""
        if r == "browser_not_found":
            return (
                "No installed Chrome, Edge or Chromium-family browser could be launched; nothing "
                "is downloaded. Install Chrome, or run `consumer-reports-mcp auth` in a terminal "
                "and paste the cookie there."
            )
        if r == "browser_extra_missing":
            return (
                f"The browser extra is not installed. Install it with `{INSTALL_COMMAND}` and "
                "restart the server, or run `consumer-reports-mcp auth` in a terminal."
            )
        if r == "window_closed":
            return (
                "The window was closed before a session appeared; nothing was captured and "
                "nothing changed. Call cr_sign_in again to retry."
            )
        if r == "capture_timeout":
            return (
                f"No session appeared within {self.timeout_s} s; nothing was captured and "
                "nothing changed. Call cr_sign_in again to retry."
            )
        if r == "not_durable":
            return (
                "Consumer Reports issued a session-only cookie — one with no expiry, meaning "
                '"Remember Me" did not take — so it would have died within days while the '
                "status claimed a year; nothing was captured and nothing changed. Call "
                'cr_sign_in again and ask the user to leave "Remember Me" ticked when signing in.'
            )
        if r in DEAD_VERDICTS:
            # NOT "tick remember me": CR renders `setAutoLogin` already checked and the flow
            # re-ticks it (RECON §5), so that was the one actionable instruction here and it
            # was a guaranteed no-op. CR issued a cookie but it did not read as a member, so
            # name the causes that remain, and the paste path the other branches already name.
            return (
                "Consumer Reports issued a session but it did not authenticate as a member, "
                "so nothing was stored. That usually means a different account was used, or "
                "the membership has lapsed. Check the account at consumerreports.org, then "
                "retry cr_sign_in — or paste a cookie with `consumer-reports-mcp auth`."
            )
        if r.startswith("could_not_check:"):
            detail = r.split(":", 1)[1]
            if not self._captured:
                return (
                    "The stored session could not be checked against Consumer Reports "
                    f"({detail}), so no window was opened and nothing changed. Retry once the "
                    "network is back, or pass force=true to open a window without the check."
                )
            return (
                "A cookie was captured but could not be checked against Consumer Reports "
                f"({detail}); nothing was stored. Retry once the network is back."
            )
        if r.startswith("save_failed:"):
            # the file's path is the user's home directory: it goes to the server log, never
            # into a tool response
            return (
                "The cookie authenticated but could not be written to the session file in the "
                f"server's config directory ({r.split(':', 1)[1]}); nothing changed. Check that "
                "the directory is writable (the server log names it), or run "
                "`consumer-reports-mcp auth` in a terminal."
            )
        if r.startswith("internal_error:"):
            return (
                f"The sign-in hit an unexpected error ({r.split(':', 1)[1]}). cr_auth_status "
                "reports what is stored; call cr_sign_in again to retry."
            )
        if r == "cancelled":
            return "The sign-in was cancelled by server shutdown; nothing changed."
        if not self._captured:
            return (
                f"The stored session could not be judged ({r or 'unknown'}), so no window was "
                "opened and nothing changed. Pass force=true to open a window without the check."
            )
        return f"The sign-in failed ({r or 'unknown'}); nothing changed."

    async def _run(self, verify: Mapping[str, str] | None) -> None:
        try:
            if verify is not None and not await self._verify_stored(verify):
                return
            await self._capture_and_store()
        except asyncio.CancelledError:
            self._set("failed", "cancelled")
            raise
        except Exception as exc:  # the catch-all: a live phase must never outlive the task
            log.warning("sign-in: unexpected %s; the flow is abandoned", type(exc).__name__)
            self._set("failed", f"internal_error:{type(exc).__name__}")

    async def _verify_stored(self, cookies: Mapping[str, str]) -> bool:
        """The pre-flight check of a stored `unverified` cookie. Returns whether to open a
        window: only when CR itself rejected the cookie. The probe IS a marker-bearing fetch
        with this credential, so health moves on its verdict."""
        rt = self.rt
        log.info("sign-in: a session is stored but unverified; checking it before any window")
        try:
            outcome = await self._validate(rt.settings, cookies)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a crash in the probe is not a verdict on the cookie
            log.warning("sign-in: the stored-session check crashed with %s", type(exc).__name__)
            self._set("failed", f"could_not_check:{type(exc).__name__}")
            return False
        rt.health.on_probe_verdict(outcome)  # a verdict moves health; a non-verdict does not
        if outcome == VERDICT_MEMBER:
            log.info("sign-in: the stored session is live; no window opened, nothing changed")
            self._set("refused", "session_active")
            return False
        if outcome in DEAD_VERDICTS:
            log.info("sign-in: Consumer Reports rejected the stored session (%s)", outcome)
            return True
        # could_not_check:<reason>, a drift code, or `anonymous`: no verdict on the cookie, so
        # neither "it works" nor "it is dead" may be claimed — and no window opens on a guess
        log.info("sign-in: the stored session could not be judged (%s); nothing changed", outcome)
        self._set("failed", outcome)
        return False

    async def _capture_and_store(self) -> None:
        rt = self.rt
        capture = self._capture or capture_hash
        self._deadline = time.monotonic() + self.timeout_s
        if self.phase != "waiting":
            self._set("waiting")
        try:
            captured = await capture(timeout_s=self.timeout_s, on_launch=self._on_launch)
        except BrowserExtraMissing:
            self._set("failed", "browser_extra_missing")
            return
        except BrowserNotFound as exc:
            log.warning("sign-in: %s", exc)
            self._set("failed", "browser_not_found")
            return
        except WindowClosed:
            self._set("failed", "window_closed")
            return
        except CaptureTimeout:
            self._set("failed", "capture_timeout")
            return
        except NotDurable:
            log.warning("sign-in: CR issued a session-only cookie; nothing captured")
            self._set("failed", "not_durable")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the browser step must never take the server down
            log.warning("sign-in: the browser step failed with %s", type(exc).__name__)
            self._set("failed", f"browser_error:{type(exc).__name__}")
            return
        self._captured = True
        log.info(
            "sign-in: a token was captured (expires %s); checking it against consumerreports.org",
            captured.expires_at,
        )
        self._set("validating")
        cookies = {DURABLE_COOKIE: captured.value}
        try:
            outcome = await self._validate(rt.settings, cookies)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a crash in the probe is not a verdict on the cookie
            log.warning("sign-in: validation crashed with %s", type(exc).__name__)
            self._set("failed", f"could_not_check:{type(exc).__name__}")
            return
        if outcome != "member":
            log.info("sign-in: the captured cookie did not authenticate (%s)", outcome)
            self._set("failed", outcome)
            return
        if self._cancel_requested():
            # shutdown asked us to stop and something below swallowed the cancellation: the
            # status already says `cancelled`, and storing now would make that a lie
            log.info("sign-in: cancelled before the cookie was stored; nothing changed")
            return
        try:
            rt.credentials.save(cookies, expires_at=captured.expires_at)
        except (OSError, ValueError) as exc:
            log.error(
                "sign-in: could not store the session at %s (%s)",
                rt.credentials.path,
                type(exc).__name__,
            )
            self._set("failed", f"save_failed:{type(exc).__name__}")
            return
        await rt.transport.adopt()
        # the probe fetch saw `data-subscriber="true"` with this exact credential moments ago:
        # that is a marker-bearing fetch in this process, so `active` is the truthful state
        rt.health.on_probe_verdict(outcome)
        log.info("sign-in: member session active and stored at %s", rt.credentials.path)
        self._set("active")

    # --- cr_auth_status ---------------------------------------------------------------
    async def status(self, wait_s: int = 0) -> E.AuthStatusEnvelope:
        try:
            budget = min(max(int(wait_s or 0), 0), STATUS_WAIT_MAX_S)
        except (TypeError, ValueError):
            budget = 0
        if budget and self.in_progress:
            await self._wait_for_change(self._version, budget)
        return self.snapshot()

    def snapshot(self) -> E.AuthStatusEnvelope:
        rt = self.rt
        st = rt.credentials.status()
        return E.AuthStatusEnvelope(
            session=rt.health.health.value,
            warnings=rt.session_warnings(),
            error=None,
            data=E.AuthStatusData(
                source=st["source"],
                captured_at=st["captured_at"],
                expires_at=st["expires_at"],
                expiry_basis=st["expiry_basis"],
                days_left_max=st["remaining_days_max"],
                sign_in=self.phase,
                reason=self.reason,
                browser=self.browser,
            ),
        )

    async def cancel(self) -> None:
        """Server shutdown: stop a sign-in in flight. The window closes; nothing is stored.

        Returns within `CANCEL_WAIT_S` whatever the window's teardown does: the task is
        shielded, so a slow teardown carries on behind this call, and `browser_auth`'s exit
        reaper covers a loop torn down under it. The phase is set here, not only by the task's
        own handler, so a status poll is right the moment cancellation is requested — and the
        task reads `cancelling()` before `save()`, so a cancellation swallowed on the way
        cannot turn this `cancelled` into a stored, `active` session behind the caller's back.
        Only a cancellation of the CALLER — a lifespan being torn down under this call —
        propagates."""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        self._set("failed", "cancelled")
        try:
            await asyncio.wait_for(asyncio.shield(task), CANCEL_WAIT_S)
        except asyncio.CancelledError:
            if not task.done():  # the caller's own cancellation, not the task's: propagate
                raise
        except (TimeoutError, Exception):
            pass


# --------------------------------------------------------------------------- tool bodies


async def cr_sign_in(rt: Runtime, force: bool = False) -> E.SignInEnvelope:
    return await rt.sign_in.start(force=bool(force))


async def cr_auth_status(rt: Runtime, wait_s: int = 0) -> E.AuthStatusEnvelope:
    return await rt.sign_in.status(wait_s=wait_s)
