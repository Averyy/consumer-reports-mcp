"""P9.4 — `auth --browser` never touches a form field; all against a fake Playwright."""

from __future__ import annotations

import ast
import asyncio
import base64
import builtins
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from consumer_reports_mcp import browser_auth as B
from tests.conftest import LoopHeartbeat

SRC = Path(B.__file__)


class FakePage:
    def __init__(self) -> None:
        self.gotos: list[str] = []
        self.checks: list[str] = []

    async def goto(self, url: str) -> None:
        self.gotos.append(url)

    async def check(self, selector: str, timeout: int = 0) -> None:
        self.checks.append(selector)


class FakeContext:
    def __init__(self, cookie_batches: list[list[dict]]) -> None:
        self.batches = list(cookie_batches)
        self.page = FakePage()
        self.closed = False

    async def new_page(self) -> FakePage:
        return self.page

    async def cookies(self) -> list[dict]:
        if len(self.batches) > 1:
            return self.batches.pop(0)
        return self.batches[0] if self.batches else []

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, batches, *, launch_ok=True) -> None:
        self.context = FakeContext(batches)
        self.closed = False
        self.context_kwargs: dict | None = None

    async def new_context(self, **kwargs) -> FakeContext:
        self.context_kwargs = kwargs
        return self.context

    async def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser | None) -> None:
        self.browser = browser
        self.launch_kwargs: dict | None = None

    async def launch(self, **kwargs) -> FakeBrowser:
        self.launch_kwargs = kwargs
        if self.browser is None:
            raise RuntimeError("Executable doesn't exist")
        return self.browser


class FakePlaywright:
    def __init__(self, browser: FakeBrowser | None) -> None:
        self.chromium = FakeChromium(browser)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


# a durable `hash`, as the "remember me" mint issues it: 365 days out, in POSIX seconds
DURABLE_EXPIRES = time.time() + 365 * 86400


def _hash_cookie(
    value: str, domain: str = ".consumerreports.org", expires: float = DURABLE_EXPIRES
) -> dict:
    """What Playwright returns for CR's `hash`: the name, the value, the cookie's domain and
    its expiry — POSIX seconds, or `-1` for a session cookie."""
    return {"name": "hash", "value": value, "domain": domain, "expires": expires}


def test_missing_extra_gives_install_hint_not_traceback(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("No module named 'playwright'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(B.BrowserExtraMissing) as ei:
        asyncio.run(B.capture_hash(timeout_s=1))
    msg = str(ei.value)
    assert "consumer-reports-mcp[browser]" in msg and "Traceback" not in msg
    assert "consumer-reports-mcp auth" in msg  # points at the paste path


def test_never_references_password_field():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    pattern = re.compile(r"password|username", re.I)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not pattern.search(node.value), node.value
        if isinstance(node, ast.Attribute):
            assert not pattern.search(node.attr)
            assert node.attr not in ("fill", "type", "press", "submit", "evaluate")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in ("fill", "type", "press", "submit", "evaluate")


async def test_polls_until_hash_then_returns():
    browser = FakeBrowser([[], [{"name": "userToken", "value": "x"}], [_hash_cookie("a" * 36)]])
    pw = FakePlaywright(browser)
    captured = await B.capture_hash(timeout_s=5, playwright_factory=lambda: pw, poll_s=0)
    assert captured.value == "a" * 36
    assert captured.expires_at == B.expiry_from_posix(DURABLE_EXPIRES)
    assert browser.context.closed and browser.closed
    assert browser.context.page.gotos == [B.LOGIN_URL]
    # ticked at launch and re-asserted on every poll (three polls here), never anything else
    assert set(browser.context.page.checks) == {B.REMEMBER_ME_SELECTOR}
    assert len(browser.context.page.checks) == 4


def test_capture_repr_never_carries_the_value():
    """A logged or formatted `Capture` is the one new place a token could leak."""
    cap = B.Capture(value="q" * 36, expires_at="2027-09-05T00:00:00Z")
    assert "q" * 36 not in repr(cap) and "q" * 36 not in str(cap)
    assert "2027-09-05T00:00:00Z" in repr(cap)


async def test_a_session_only_hash_is_never_the_capture(monkeypatch, caplog):
    """Playwright reports `expires: -1` for a session cookie. That is "remember me" NOT having
    taken — CR then issues a session that dies in days (RECON §5) — and stored under the
    assumed 365-day bound it read as a year of life until the day it stopped working (a
    2026-09-05 capture died within a day, status said 364). So it ends in `NotDurable`, after
    a grace for the durable one to land, with nothing captured."""
    monkeypatch.setattr(B, "DURABLE_GRACE_S", 0.05)
    browser = FakeBrowser([[_hash_cookie("s" * 36, expires=-1)]])
    pw = FakePlaywright(browser)
    with caplog.at_level(logging.WARNING, logger="consumer_reports_mcp.browser_auth"):
        with pytest.raises(B.NotDurable) as ei:
            await B.capture_hash(timeout_s=5, playwright_factory=lambda: pw, poll_s=0)
    assert "nothing was captured" in str(ei.value) and "Remember Me" in str(ei.value)
    assert "s" * 36 not in str(ei.value) and "s" * 36 not in caplog.text
    assert "session-only" in caplog.text
    assert not isinstance(ei.value, B.CaptureTimeout)  # a token DID appear
    assert browser.context.closed and browser.closed
    # a missing or unreadable `expires` is a session cookie too: an expiry that cannot be read
    # cannot be stored as measured
    for bad in (
        {"name": "hash", "value": "t" * 36, "domain": ".consumerreports.org"},
        _hash_cookie("t" * 36, expires="soon"),
        _hash_cookie("t" * 36, expires=0),
    ):
        assert B._expires(bad) is None
    assert B._expires(_hash_cookie("t" * 36, expires=1.5)) == 1.5


async def test_a_durable_hash_arriving_within_the_grace_wins(monkeypatch):
    """The login's redirect hops can set cookies across polls: a session-only `hash` on one
    poll and the durable one on the next is a capture, not a failure."""
    monkeypatch.setattr(B, "DURABLE_GRACE_S", 5.0)
    browser = FakeBrowser(
        [
            [_hash_cookie("u" * 36, expires=-1)],
            [_hash_cookie("u" * 36, expires=-1)],
            [
                _hash_cookie("u" * 36, expires=-1),
                _hash_cookie("v" * 36, domain="www.consumerreports.org"),
            ],
        ]
    )
    pw = FakePlaywright(browser)
    t0 = time.monotonic()
    captured = await B.capture_hash(timeout_s=5, playwright_factory=lambda: pw, poll_s=0)
    assert captured.value == "v" * 36 and time.monotonic() - t0 < 1.0
    assert captured.expires_at == B.expiry_from_posix(DURABLE_EXPIRES)


async def test_remember_me_is_reasserted_every_poll_and_a_missing_box_never_stalls_it():
    """The tick is a guard applied every poll, not once at launch: it covers a user who
    un-ticks the box (measured 2026-09-07: CR itself renders it checked on every render, so a
    re-render does not lose it). A page with no form — mid-navigation, or signed in — answers
    within the recheck budget and the poll goes on."""

    class FlakyPage(FakePage):
        async def check(self, selector, timeout=0):
            self.checks.append((selector, timeout))
            if len(self.checks) > 1:  # every re-assertion: the box is gone
                raise RuntimeError("Timeout 250ms exceeded")

    browser = FakeBrowser([[], [], [_hash_cookie("w" * 36)]])
    browser.context.page = FlakyPage()
    pw = FakePlaywright(browser)
    captured = await B.capture_hash(timeout_s=5, playwright_factory=lambda: pw, poll_s=0)
    assert captured.value == "w" * 36
    checks = browser.context.page.checks
    assert checks[0] == (B.REMEMBER_ME_SELECTOR, 5000)
    assert checks[1:] == [(B.REMEMBER_ME_SELECTOR, B.REMEMBER_ME_RECHECK_TIMEOUT_MS)] * 3


async def test_times_out_cleanly():
    browser = FakeBrowser([[]])
    pw = FakePlaywright(browser)
    with pytest.raises(B.CaptureTimeout):
        await B.capture_hash(timeout_s=0, playwright_factory=lambda: pw, poll_s=0)
    assert browser.context.closed and browser.closed


async def test_launches_headed_with_channel_chrome():
    browser = FakeBrowser([[_hash_cookie("b" * 36)]])
    pw = FakePlaywright(browser)
    await B.capture_hash(timeout_s=5, playwright_factory=lambda: pw, poll_s=0)
    assert pw.chromium.launch_kwargs == {
        "channel": "chrome",
        "headless": False,
        "args": ["--disable-blink-features=AutomationControlled"],
        "ignore_default_args": ["--enable-automation"],
    }


async def test_fresh_context_no_storage_state():
    browser = FakeBrowser([[_hash_cookie("c" * 36)]])
    pw = FakePlaywright(browser)
    await B.capture_hash(timeout_s=5, playwright_factory=lambda: pw, poll_s=0)
    assert browser.context_kwargs == {"no_viewport": True}  # no storage_state, no user data dir
    assert "storage_state" not in browser.context_kwargs
    assert "user_data_dir" not in browser.context_kwargs


async def test_window_never_advertises_automation(monkeypatch):
    """CR's sign-in form runs an invisible hCaptcha on every submit, and `navigator.webdriver`
    is what it keys on — measured on the real page: `true` (Playwright's default) opened a
    visible puzzle and issued no token; `false` issued a token silently in under a second. A
    user who solved that puzzle was still answered "We still don't recognize that sign in" for
    credentials that worked in a plain Chrome window. So on EVERY rung of the ladder the blink
    feature is disabled and `--enable-automation` is dropped, and the context sees the real
    window and screen rather than Playwright's emulated 1280×720."""
    assert B.LAUNCH_ARGS == ("--disable-blink-features=AutomationControlled",)
    assert B.IGNORE_DEFAULT_ARGS == ("--enable-automation",)
    assert B.CONTEXT_OPTIONS == {"no_viewport": True}
    monkeypatch.setattr(
        B.shutil, "which", lambda n: "/usr/bin/chromium" if n == "chromium" else None
    )
    browser = FakeBrowser([[_hash_cookie("h" * 36)]])
    pw = FakePlaywright(browser)
    pw.chromium = SelectiveChromium(browser, ok_executables=True)
    await B.capture_hash(timeout_s=5, playwright_factory=lambda: pw, poll_s=0)
    assert len(pw.chromium.attempts) >= 3  # chrome, msedge, then an executable
    for attempt in pw.chromium.attempts:
        assert attempt["headless"] is False
        assert "--disable-blink-features=AutomationControlled" in attempt["args"]
        assert attempt["ignore_default_args"] == ["--enable-automation"]
    assert browser.context_kwargs == {"no_viewport": True}


async def test_no_chrome_names_the_paste_path():
    pw = FakePlaywright(None)
    with pytest.raises(B.BrowserNotFound) as ei:
        await B.capture_hash(timeout_s=1, playwright_factory=lambda: pw, poll_s=0)
    assert "consumer-reports-mcp auth" in str(ei.value)


async def test_remember_me_selector_failure_only_warns(caplog):
    class NoCheckPage(FakePage):
        async def check(self, selector, timeout=0):
            raise RuntimeError("selector changed")

    browser = FakeBrowser([[_hash_cookie("d" * 36)]])
    browser.context.page = NoCheckPage()
    pw = FakePlaywright(browser)
    captured = await B.capture_hash(timeout_s=5, playwright_factory=lambda: pw, poll_s=0)
    assert captured.value == "d" * 36 and "remember-me" in caplog.text


async def test_polls_on_the_real_interval_and_window_close_is_clean():
    browser = FakeBrowser([[], [], [_hash_cookie("e" * 36)]])
    pw = FakePlaywright(browser)
    captured = await B.capture_hash(timeout_s=5, playwright_factory=lambda: pw, poll_s=0.01)
    assert captured.value == "e" * 36

    class ClosedContext(FakeContext):
        async def cookies(self):
            raise RuntimeError("Target page, context or browser has been closed")

    browser2 = FakeBrowser([[]])
    browser2.context = ClosedContext([[]])
    pw2 = FakePlaywright(browser2)
    with pytest.raises(B.CaptureTimeout) as ei:
        await B.capture_hash(timeout_s=5, playwright_factory=lambda: pw2, poll_s=0)
    assert "nothing was captured" in str(ei.value)
    assert browser2.context.closed and browser2.closed


# --------------------------------------------------------------------------- the launch ladder


class SelectiveChromium(FakeChromium):
    """Fails every rung except the ones named; records every attempt in order."""

    def __init__(self, browser, *, ok_channels=(), ok_executables=False):
        super().__init__(browser)
        self.ok_channels = ok_channels
        self.ok_executables = ok_executables
        self.attempts: list[dict] = []

    async def launch(self, **kwargs):
        self.attempts.append(kwargs)
        if kwargs.get("channel") in self.ok_channels or (
            self.ok_executables and "executable_path" in kwargs
        ):
            self.launch_kwargs = kwargs
            return self.browser
        raise RuntimeError("Executable doesn't exist")


def _no_path(_name):
    return None


async def test_falls_back_to_edge_when_chrome_is_absent(monkeypatch):
    monkeypatch.setattr(B.shutil, "which", _no_path)
    browser = FakeBrowser([[_hash_cookie("f" * 36)]])
    pw = FakePlaywright(browser)
    pw.chromium = SelectiveChromium(browser, ok_channels=("msedge",))
    launched: list[str] = []
    captured = await B.capture_hash(
        timeout_s=5, playwright_factory=lambda: pw, poll_s=0, on_launch=launched.append
    )
    assert captured.value == "f" * 36 and launched == ["msedge"]
    assert [a.get("channel") for a in pw.chromium.attempts] == ["chrome", "msedge"]
    assert all(a["headless"] is False for a in pw.chromium.attempts)
    # still a throwaway context on the fallback rung
    assert browser.context_kwargs == {"no_viewport": True}


async def test_falls_back_to_a_path_executable_after_both_channels(monkeypatch):
    def which(name):
        return "/usr/bin/chromium" if name == "chromium" else None

    monkeypatch.setattr(B.shutil, "which", which)
    browser = FakeBrowser([[_hash_cookie("g" * 36)]])
    pw = FakePlaywright(browser)
    pw.chromium = SelectiveChromium(browser, ok_executables=True)
    launched: list[str] = []
    await B.capture_hash(
        timeout_s=5, playwright_factory=lambda: pw, poll_s=0, on_launch=launched.append
    )
    assert launched == ["chromium"]
    assert pw.chromium.attempts[-1] == {
        "executable_path": "/usr/bin/chromium",
        "headless": False,
        "args": ["--disable-blink-features=AutomationControlled"],
        "ignore_default_args": ["--enable-automation"],
    }
    assert len(pw.chromium.attempts) == 3  # chrome, msedge, then the one executable found


def test_launch_plan_order_and_nothing_is_downloaded():
    plan = list(B.launch_plan(which=lambda n: f"/opt/{n}" if n == "brave-browser" else None))
    assert plan == [
        ("chrome", {"channel": "chrome"}),
        ("msedge", {"channel": "msedge"}),
        ("brave-browser", {"executable_path": "/opt/brave-browser"}),
    ]
    assert B.CHANNELS == ("chrome", "msedge")
    assert "google-chrome" in B.EXECUTABLE_NAMES and "brave-browser" in B.EXECUTABLE_NAMES


async def test_no_browser_on_any_rung_names_the_paste_path_and_what_was_tried(monkeypatch):
    monkeypatch.setattr(B.shutil, "which", _no_path)
    pw = FakePlaywright(None)
    pw.chromium = SelectiveChromium(None)
    with pytest.raises(B.BrowserNotFound) as ei:
        await B.capture_hash(timeout_s=1, playwright_factory=lambda: pw, poll_s=0)
    msg = str(ei.value)
    assert "consumer-reports-mcp auth" in msg and "chrome" in msg and "msedge" in msg
    assert "download" in msg and len(pw.chromium.attempts) == 2


async def test_window_closed_is_distinguishable_from_a_timeout():
    class ClosedContext(FakeContext):
        async def cookies(self):
            raise RuntimeError("Target page, context or browser has been closed")

    browser = FakeBrowser([[]])
    browser.context = ClosedContext([[]])
    pw = FakePlaywright(browser)
    with pytest.raises(B.WindowClosed) as ei:
        await B.capture_hash(timeout_s=5, playwright_factory=lambda: pw, poll_s=0)
    assert isinstance(ei.value, B.CaptureTimeout)  # every existing caller still catches it
    browser2 = FakeBrowser([[]])
    pw2 = FakePlaywright(browser2)
    with pytest.raises(B.CaptureTimeout) as ei2:
        await B.capture_hash(timeout_s=0, playwright_factory=lambda: pw2, poll_s=0)
    assert not isinstance(ei2.value, B.WindowClosed)


def test_extra_installed_does_not_import_playwright():
    import sys

    before = set(sys.modules)
    B.extra_installed()
    assert not any(m.startswith("playwright") for m in set(sys.modules) - before)


# --------------------------------------------------------------------------- off the loop


async def test_playwright_import_runs_off_the_loop_thread(monkeypatch):
    """The import is synchronous work — 0.28 s measured cold, and the Desktop bundle ships no
    `.pyc` — and it used to run inside the sign-in task, on the loop, mid tool call."""
    browser = FakeBrowser([[_hash_cookie("i" * 36)]])
    seen: list[threading.Thread] = []

    def slow_import():
        seen.append(threading.current_thread())
        time.sleep(0.3)  # a cold import
        return lambda: FakePlaywright(browser)

    monkeypatch.setattr(B, "_import_playwright", slow_import)
    async with LoopHeartbeat() as hb:
        captured = await B.capture_hash(timeout_s=5, poll_s=0)
    assert captured.value == "i" * 36
    assert seen and seen[0] is not threading.main_thread()
    assert hb.max_gap < 0.15  # the loop ticked through the import


# --------------------------------------------------------------------------- every exit path


class FakeCdp:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.detached = False

    async def send(self, method: str) -> dict:
        assert method == "SystemInfo.getProcessInfo"
        return {"processInfo": [{"type": "GPU", "id": 1}, {"type": "browser", "id": self.pid}]}

    async def detach(self) -> None:
        self.detached = True


class PidBrowser(FakeBrowser):
    """A Chromium that answers the browser-level CDP session with its pid."""

    def __init__(self, batches, *, pid: int) -> None:
        super().__init__(batches)
        self.cdp = FakeCdp(pid)

    async def new_browser_cdp_session(self) -> FakeCdp:
        return self.cdp


async def _close_against_a_dead_driver() -> None:
    """Playwright 1.62's `close()` once `asyncio.run`'s shutdown has cancelled the pipe reader
    in the same sweep as the capture: the reply never comes, and the FIRST cancellation is
    absorbed — `_inner_send` catches it, sends `__abort__` and awaits the abort's own reply,
    which never comes either. `wait_for` cancels once and then waits, so it hangs here."""
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        await asyncio.Event().wait()
        raise


class DeadContext(FakeContext):
    async def close(self) -> None:
        await _close_against_a_dead_driver()


class DeadBrowser(PidBrowser):
    async def close(self) -> None:
        await _close_against_a_dead_driver()


async def test_task_cancellation_closes_the_window():
    browser = FakeBrowser([[]])
    pw = FakePlaywright(browser)
    task = asyncio.create_task(
        B.capture_hash(timeout_s=60, playwright_factory=lambda: pw, poll_s=0.01)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert browser.context.closed and browser.closed


async def test_a_failure_before_the_page_still_closes_the_browser():
    class NoContextBrowser(FakeBrowser):
        async def new_context(self, **kwargs):
            raise RuntimeError("context failed")

    browser = NoContextBrowser([[]])
    pw = FakePlaywright(browser)
    with pytest.raises(RuntimeError):
        await B.capture_hash(timeout_s=5, playwright_factory=lambda: pw, poll_s=0)
    assert browser.closed


async def test_teardown_is_bounded_and_falls_back_to_the_pid_when_the_driver_cannot_answer(
    monkeypatch,
):
    """The shutdown deadlock in miniature: a close() whose reply never comes. On the real thing
    the process sat in `kevent` for 26 minutes with the window open. Bounded, the capture
    finishes inside the budget and the browser is terminated by pid — on the timeout path and
    on the cancellation path, the one `asyncio.run`'s shutdown takes."""
    monkeypatch.setattr(B, "CLOSE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(B, "DRIVER_STOP_TIMEOUT_S", 0.2)
    killed: list[int] = []
    monkeypatch.setattr(B, "kill_browser", lambda pid: killed.append(pid) or True)

    browser = DeadBrowser([[]], pid=4242)
    browser.context = DeadContext([[]])
    pw = FakePlaywright(browser)
    t0 = time.monotonic()
    with pytest.raises(B.CaptureTimeout):
        await asyncio.wait_for(
            B.capture_hash(timeout_s=0, playwright_factory=lambda: pw, poll_s=0), 3
        )
    assert time.monotonic() - t0 < 1.0
    assert killed == [4242] and 4242 not in B.pending_browsers()
    assert browser.cdp.detached

    killed.clear()
    browser2 = DeadBrowser([[]], pid=4343)
    browser2.context = DeadContext([[]])
    pw2 = FakePlaywright(browser2)
    task = asyncio.create_task(
        B.capture_hash(timeout_s=60, playwright_factory=lambda: pw2, poll_s=0.01)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    t0 = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert time.monotonic() - t0 < 1.0
    assert killed == [4343] and 4343 not in B.pending_browsers()


async def test_stopping_the_driver_is_bounded_too(monkeypatch):
    monkeypatch.setattr(B, "CLOSE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(B, "DRIVER_STOP_TIMEOUT_S", 0.2)

    class StuckPlaywright(FakePlaywright):
        async def __aexit__(self, *exc):
            await asyncio.Event().wait()

    browser = FakeBrowser([[_hash_cookie("j" * 36)]])
    pw = StuckPlaywright(browser)
    t0 = time.monotonic()
    token = await asyncio.wait_for(
        B.capture_hash(timeout_s=5, playwright_factory=lambda: pw, poll_s=0), 3
    )
    assert token.value == "j" * 36 and time.monotonic() - t0 < 1.0
    assert browser.context.closed and browser.closed


async def test_exit_reaper_terminates_only_browsers_still_pending(monkeypatch):
    """Interpreter shutdown: a loop torn down under a capture never reaches its `finally`, so
    the `atexit` reaper terminates what is still pending — and nothing a capture confirmed
    closed."""
    killed: list[int] = []
    monkeypatch.setattr(B, "kill_browser", lambda pid: killed.append(pid) or True)
    done = PidBrowser([[_hash_cookie("k" * 36)]], pid=5151)
    await B.capture_hash(timeout_s=5, playwright_factory=lambda: FakePlaywright(done), poll_s=0)
    assert 5151 not in B.pending_browsers() and killed == []
    B._arm_reaper(5252)  # a capture the loop was torn down under
    assert 5252 in B.pending_browsers()
    B.reap_pending_browsers()
    assert killed == [5252] and 5252 not in B.pending_browsers()
    B.reap_pending_browsers()  # idempotent
    assert killed == [5252]


def test_kill_browser_only_terminates_a_playwright_profiled_process():
    """The pid came from CDP at launch; by the time it is used the process may be gone and the
    pid recycled. A signal goes only to a process whose command line still says Playwright."""
    chrome = (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        " --user-data-dir=/var/folders/x/T/playwright_chromiumdev_profile-ab12"
        " --remote-debugging-pipe"
    )
    sent: list[tuple[int, int]] = []

    def kill(pid, sig):
        sent.append((pid, sig))

    assert B.kill_browser(4242, command_line=lambda pid: chrome, kill=kill) is True
    assert sent == [(4242, signal.SIGTERM)]
    sent.clear()
    other = "/usr/bin/vim notes.txt"  # a recycled pid: some other process now
    assert B.kill_browser(4242, command_line=lambda pid: other, kill=kill) is False
    assert B.kill_browser(4242, command_line=lambda pid: None, kill=kill) is False  # unknown
    assert sent == []

    def gone(pid, sig):
        raise ProcessLookupError

    assert B.kill_browser(4242, command_line=lambda pid: chrome, kill=gone) is False


def test_kill_browser_guard_reads_the_real_process_table():
    """The guard through the real `_command_line`: a process this test spawned, whose argv
    carries a Playwright profile marker, is matched; the test runner itself is not; and once
    the spawned process is gone its pid is unknown, so no signal is sent. Only the injected
    `kill` is ever called — nothing here signals a process it did not create.

    Not skipped on Windows: there this IS the `Get-CimInstance Win32_Process` query against a
    real process table (the CI workflow runs it on a Windows runner). The padding argument
    pushes the marker past any console width, so an output that wrapped or truncated the
    command line would fail here rather than silently never matching a real Chrome."""
    sent: list[tuple[int, int]] = []

    def record(pid, sig):
        sent.append((pid, sig))

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "--padding=" + "x" * 400,
            "--user-data-dir=/x/playwright_chromiumdev_profile-t",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # The child may still be inside `execve`; `/proc/<pid>/cmdline` is empty until it
        # lands, and this read is fast enough to get there first. Wait for the exec, then
        # assert — a guard that never matches still fails, it just is not raced into it.
        deadline = time.monotonic() + 10.0
        while True:
            cmd, status = B._query_command_line(proc.pid)
            if status == B.STATUS_FOUND or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        assert status == B.STATUS_FOUND, status
        assert cmd and B.PROFILE_MARKER in cmd and B.PLAYWRIGHT_MARKER in cmd.lower(), cmd
        assert B.kill_browser(proc.pid, kill=record) is True
        assert sent == [(proc.pid, signal.SIGTERM)]
        assert B.kill_browser(os.getpid(), kill=record) is False  # not a Playwright browser
        assert sent == [(proc.pid, signal.SIGTERM)]
    finally:
        proc.kill()
        proc.wait(timeout=10)
    # gone — and said so, not "the query failed": unknown either way, and unknown means no kill
    assert B._query_command_line(proc.pid) == (None, B.STATUS_GONE)
    assert B._command_line(proc.pid) is None
    assert B.kill_browser(proc.pid, kill=record) is False
    assert sent == [(proc.pid, signal.SIGTERM)]


def test_proc_cmdline_is_read_exactly_and_a_missing_pid_is_gone(tmp_path):
    """Linux reads the kernel's own argv. NUL-separated, a trailing NUL, no width to truncate
    at; a pid with no entry is `gone`; a machine with no procfs answers None so the caller
    falls back to `ps -ww`; and an EMPTY `cmdline` answers None too, because the kernel empties
    it for a zombie, a kernel thread AND a process still inside `execve` — reporting
    `unreadable` there short-circuited the fallback and lost a race `ps` never lost."""
    assert B._proc_command_line(1, proc=str(tmp_path / "none")) is None  # no procfs here
    (tmp_path / "self").mkdir()
    (tmp_path / "self" / "cmdline").write_bytes(b"pytest\0")
    (tmp_path / "4242").mkdir()
    (tmp_path / "4242" / "cmdline").write_bytes(
        b"/opt/google/chrome/chrome\0--disable-field-trial-config\0"
        + b"--x="
        + b"y" * 400
        + b"\0--user-data-dir=/tmp/playwright_chromiumdev_profile-abc\0--flag\0"
    )
    cmd, status = B._proc_command_line(4242, proc=str(tmp_path))
    assert status == B.STATUS_FOUND
    assert cmd == (
        "/opt/google/chrome/chrome --disable-field-trial-config --x="
        + "y" * 400
        + " --user-data-dir=/tmp/playwright_chromiumdev_profile-abc --flag"
    )
    assert B._proc_command_line(4243, proc=str(tmp_path)) == (None, B.STATUS_GONE)
    (tmp_path / "4244").mkdir()
    (tmp_path / "4244" / "cmdline").write_bytes(b"")
    assert B._proc_command_line(4244, proc=str(tmp_path)) is None  # -> `ps`, not a verdict
    (tmp_path / "4245").mkdir()
    (tmp_path / "4245" / "cmdline").write_bytes(b"\0\0\0")  # all separators, no argv
    assert B._proc_command_line(4245, proc=str(tmp_path)) is None


def test_command_line_on_posix_asks_ps_for_the_unbounded_width(monkeypatch):
    """procps `ps` cuts each line to the terminal width — 80 columns on a pipe — so without
    `-ww` a real Chrome's command line ended at `--disable-background-netw` on the Linux CI
    runner (2026-09-06) and the guard never matched. `ps -p` of a pid that is gone exits 1
    saying nothing; a `ps` that could not run complains, and that is a failed query."""
    calls: list[list[str]] = []
    answer = {"stdout": "", "stderr": "", "returncode": 0}

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        assert kwargs.get("errors") == "replace" and kwargs.get("timeout")
        assert "creationflags" not in kwargs
        return subprocess.CompletedProcess(
            argv, answer["returncode"], answer["stdout"], answer["stderr"]
        )

    monkeypatch.setattr(B.sys, "platform", "linux")
    monkeypatch.setattr(B, "_proc_command_line", lambda pid, proc="/proc": None)  # no procfs
    monkeypatch.setattr(B.subprocess, "run", fake_run)
    chrome = "/opt/google/chrome/chrome --user-data-dir=/tmp/playwright_chromiumdev_profile-a\n"
    answer["stdout"] = chrome
    assert B._query_command_line(4242) == (chrome, B.STATUS_FOUND)
    assert calls[-1] == ["ps", "-ww", "-o", "command=", "-p", "4242"]
    answer.update(stdout="", returncode=1)
    assert B._query_command_line(4242) == (None, B.STATUS_GONE)
    answer.update(stdout="", stderr="ps: illegal option -- q\nusage: ps ...\n", returncode=1)
    assert B._query_command_line(4242) == (None, "query_failed:exit_1:ps: illegal option -- q")
    answer.update(stdout="", stderr="", returncode=0)
    assert B._query_command_line(4242) == (None, B.STATUS_UNREADABLE)


def test_command_line_on_windows_queries_the_process_command_line(monkeypatch):
    """Windows was silently a no-op — `kill_browser` could never fire on a declared bundle
    platform while the docs called it unconditional. `tasklist` has no command-line column,
    so the guard uses `Get-CimInstance Win32_Process`, as a base64 `-EncodedCommand` (no
    quoting between argv and PowerShell's parser) that writes the command line to
    `[Console]::Out` and answers every other outcome with a distinct exit code. Pinned here by
    argv and parsing; the CI workflow runs the real query on a Windows runner."""
    calls: list[list[str]] = []
    answer = {"stdout": "", "returncode": 0}

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        assert kwargs.get("capture_output") and kwargs.get("text")
        assert kwargs.get("timeout") == B.COMMAND_LINE_TIMEOUT_S
        # markers are ASCII, so a byte the code page cannot decode must never become an
        # exception out of the guard (PowerShell 5.1 writes OEM, `text=True` decodes ANSI)
        assert kwargs.get("errors") == "replace"
        # and no console window may open for the query when the server has no console
        assert kwargs.get("creationflags") == 0x08000000  # CREATE_NO_WINDOW
        return subprocess.CompletedProcess(argv, answer["returncode"], answer["stdout"], "")

    monkeypatch.setattr(B.sys, "platform", "win32")
    monkeypatch.setattr(B.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(B.subprocess, "run", fake_run)
    chrome = (
        '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
        "--user-data-dir=C:\\Users\\u\\AppData\\Local\\Temp\\playwright_chromiumdev_profile-x "
        "--remote-debugging-pipe"
    )
    answer["stdout"] = chrome
    assert B._query_command_line(4242) == (chrome, B.STATUS_FOUND)
    assert B._command_line(4242) == chrome
    argv = calls[-1]
    assert argv[0] == "powershell" and "-NoProfile" in argv and "-NonInteractive" in argv
    assert argv[-2] == "-EncodedCommand" and "-Command" not in argv
    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert "Get-CimInstance Win32_Process" in script and "ProcessId=4242" in script
    assert "[Console]::Out.Write($p.CommandLine)" in script  # not the host's formatter
    assert "$ErrorActionPreference = 'Stop'" in script  # a failed query is not "gone"
    assert '"' not in script  # nothing for either quoting layer to interpret
    assert "tasklist" not in script
    sent: list[tuple[int, int]] = []
    assert B.kill_browser(4242, kill=lambda pid, sig: sent.append((pid, sig))) is True
    assert sent == [(4242, signal.SIGTERM)]
    # each non-match says which it is, and none of them is a kill
    answer.update(stdout="", returncode=3)
    assert B._query_command_line(4242) == (None, B.STATUS_GONE)
    assert B.kill_browser(4242, kill=lambda pid, sig: sent.append((pid, sig))) is False
    answer.update(stdout="", returncode=4)
    assert B._query_command_line(4242) == (None, B.STATUS_UNREADABLE)
    answer.update(stdout="", returncode=5)
    assert B._query_command_line(4242) == (None, "query_failed:exit_5")
    answer.update(stdout=chrome, returncode=1)  # a command line under a failure is no match
    assert B._query_command_line(4242) == (None, "query_failed:exit_1")
    assert B.kill_browser(4242, kill=lambda pid, sig: sent.append((pid, sig))) is False
    answer.update(stdout="", returncode=0)  # the script never does this; still not a match
    assert B._query_command_line(4242) == (None, B.STATUS_UNREADABLE)

    def slow(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(B.subprocess, "run", slow)
    assert B._query_command_line(4242) == (None, "query_failed:timeout")
    monkeypatch.setattr(B.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert B._query_command_line(4242) == (None, "query_failed:OSError")
    assert B._command_line(4242) is None
    assert sent == [(4242, signal.SIGTERM)]


async def test_a_hash_cookie_off_domain_or_malformed_is_not_the_token():
    """`context.cookies()` is every cookie in the window. A stray `hash` from another site, or
    a CR one that is not the 36-character token, used to be returned as the capture — and the
    flow then failed permanently while blaming remember-me."""
    browser = FakeBrowser(
        [
            [_hash_cookie("x" * 36, domain="tracker.example")],
            [_hash_cookie("y" * 36, domain="consumerreports.org.evil.example")],
            [_hash_cookie("short")],
            [_hash_cookie("m" * 36, domain="www.consumerreports.org")],
        ]
    )
    pw = FakePlaywright(browser)
    captured = await B.capture_hash(timeout_s=5, playwright_factory=lambda: pw, poll_s=0)
    assert captured.value == "m" * 36
    # and a bare `.consumerreports.org` domain cookie is the normal case
    pw2 = FakePlaywright(FakeBrowser([[_hash_cookie("n" * 36)]]))
    again = await B.capture_hash(timeout_s=5, playwright_factory=lambda: pw2, poll_s=0)
    assert again.value == "n" * 36
