"""`auth --browser` and `cr_sign_in` (SPEC §6 *auth --browser*, *cr_sign_in*): the optional
`[browser]` extra.

Open an installed Chromium-family browser on Consumer Reports' own sign-in page in a fresh,
throwaway context that does not advertise the automation (`LAUNCH_ARGS`, `CONTEXT_OPTIONS` —
CR's form runs an invisible hCaptcha that keys on `navigator.webdriver`), keep "remember me"
ticked (it is what mints the durable 365-day `hash`), then wait for the `hash` cookie and do
nothing else. This module never reads, fills or submits any form field, and it is never on the
data path.

The capture is the cookie AND its expiry. `context.cookies()` reports `expires` — POSIX seconds,
or `-1` for a session cookie — and a `hash` with no expiry is the "remember me" mint NOT having
taken: CR then issues a session that dies in days (`RECON.md` §5), and stored under the assumed
365-day bound it reads as a year of life right up to the day it stops working. That happened: a
cookie captured 2026-09-05 was dead within a day and the status said 364 days, and because the
expiry had been read and discarded here, nothing could say whether it had ever been durable. So
a session-only `hash` is never the capture (`NotDurable`), and the expiry travels with the value
into the session file (`credentials.CredentialStore.save(expires_at=)`).

The window must close on EVERY exit path — success, timeout, the user closing it, `cancel()`,
task cancellation and interpreter shutdown — and closing it must never hang the process.
`context.close()`/`browser.close()` wait for a reply from Playwright's driver and carry no
timeout of their own; when the loop is shutting down, `asyncio.run` cancels every task in one
sweep, the driver's pipe reader included, so that reply can never be read and an unbounded close
deadlocks the interpreter with the window still open (measured: a harness sat in `kevent` for
26 minutes). THAT is where an orphaned browser comes from — a Python process that stays alive,
wedged, holding a driver that will never close the window. It is not where a killed process
leaves one: Playwright's driver `SIGKILL`s the browser's process group from its own exit hook the
moment its stdin closes, so `SIGTERM` or `SIGKILL` of this process mid-capture leaves nothing
behind (measured with real Chrome and Playwright 1.62). On Windows the same hook runs
`taskkill /pid <pid> /T /F` instead — read in the 1.62 driver (`coreBundle.js`, `killProcess`:
the browser is spawned with `detached: process.platform !== "win32"`, and the `exit` handler
force-kills by tree there because there is no process group to signal) — so a killed Python
leaves nothing on Windows either. So the fix is the bounded teardown
(`CLOSE_TIMEOUT_S`, `DRIVER_STOP_TIMEOUT_S`); the pid read at launch and the `SIGTERM` by pid
cover the one remaining case — Python alive, driver unresponsive — from the capture's `finally`
and, for a loop torn down under it, from an `atexit` reaper.
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import contextlib
import dataclasses
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from types import MappingProxyType
from typing import Any

from .credentials import BARE_HASH, expiry_from_posix

log = logging.getLogger(__name__)

LOGIN_URL = "https://secure.consumerreports.org/ec/login"
REMEMBER_ME_SELECTOR = "input[name='setAutoLogin']"
# The tick is re-asserted on EVERY poll, not once at launch. Measured 2026-09-07 on the real
# page: CR renders the box `checked="checked"` server-side on the plain page and on the
# `?error` page a failed submit lands on, the form is a plain `POST /ec/login`, and the login
# script never touches the box — so a re-render restores the tick rather than losing it. What a
# one-time tick did not cover is the user un-ticking it before submitting, or CR changing the
# default. `page.check` on an already-checked box returns without clicking, scrolling or
# focusing, so the re-assertion is invisible while the user types; the budget keeps a poll
# from stalling on a page that has no form (mid-navigation, or once signed in).
REMEMBER_ME_RECHECK_TIMEOUT_MS = 250
TOKEN_COOKIE = "hash"
TOKEN_DOMAIN = "consumerreports.org"  # the cookie's domain must be this or a subdomain of it
INSTALL_HINT = (
    "the browser extra is not installed. Install it with\n"
    '    uv sync --extra browser        (or: pip install "consumer-reports-mcp[browser]")\n'
    "or use the paste path instead: `consumer-reports-mcp auth` and follow the prompt."
)
INSTALL_COMMAND = 'uv sync --extra browser  (or: pip install "consumer-reports-mcp[browser]")'

# The launch ladder. Playwright `channel`s resolve the browser from its known install locations
# (not PATH), so they work under a Desktop-launched process with a minimal PATH; the executable
# names are the Linux/PATH fallback. Nothing is ever downloaded.
CHANNELS = ("chrome", "msedge")
EXECUTABLE_NAMES = ("google-chrome", "chromium", "chromium-browser", "brave-browser")

# The window must not advertise the automation. A Chromium under Playwright enables the
# `AutomationControlled` blink feature, so every page reads `navigator.webdriver === true` — and
# CR's sign-in form runs an INVISIBLE hCaptcha on every submit that keys on exactly that bit
# (`RECON.md` §5 *Login*). Measured 2026-09-05 on the real login page with CR's own site key:
# `webdriver: true` → a visible puzzle at ~0.9 s and no token (2/2); `webdriver: false` → a token
# issued silently at ~0.8 s and no puzzle (3/3); nothing else observable differed. A human who
# then solves the puzzle is still signing in from a browser hCaptcha has flagged, and CR answers
# "We still don't recognize that sign in" — the wrong-credential message — for credentials that
# work in a plain Chrome window. So the blink feature is disabled outright. Playwright 1.62
# passes no `--enable-automation` (its default switch list was read), so `ignore_default_args`
# alone changes nothing there; it stays for the older releases the `>=1.40` floor admits,
# which do pass it (and show the "controlled by automated test software" bar). These are
# Playwright's own options for a human-driven window — its in-tree agent mode sets the same two.
LAUNCH_ARGS = ("--disable-blink-features=AutomationControlled",)
IGNORE_DEFAULT_ARGS = ("--enable-automation",)
# Playwright's default context emulates a 1280×720 `screen` equal to the viewport with no menu
# bar — a shape no real desktop has. `no_viewport` lets the page see the window's real size and
# the real screen (measured: 1710×1107, `availHeight` 1018, 2× — what a plain Chrome reports).
# Still no `storage_state` and no profile: the context is as throwaway as before.
# Immutable, like the two tuples above: these three constants are the window's trust
# surface, pinned by `test_window_never_advertises_automation`, and a `storage_state` or
# `user_data_dir` appended at runtime would defeat both the test and the throwaway rule.
CONTEXT_OPTIONS: Mapping[str, Any] = MappingProxyType({"no_viewport": True})

# The whole graceful teardown — context, browser, then the driver — shares this budget. Long
# enough for a healthy driver (a real close takes ~0.2 s), short enough that a driver whose reply
# can no longer be read (loop shutdown) costs seconds, not a hung process.
CLOSE_TIMEOUT_S = 5.0
# Stopping the driver gets its own budget, so its first step — closing the driver's stdin, which
# is what makes the node process exit — always runs even when the close budget is spent. With
# the reader already cancelled it returns at once; healthy, the driver exits within ~2 s.
DRIVER_STOP_TIMEOUT_S = 3.0
ABANDON_GRACE_S = 0.2  # how long an abandoned step gets to finish cancelling before we move on
# Every Playwright-launched Chromium runs on a throwaway profile whose path names Playwright
# (`--user-data-dir=…/playwright_chromiumdev_profile-…`). A pid is only ever terminated while its
# command line still says so — the guard against a recycled pid.
PROFILE_MARKER = "--user-data-dir="
PLAYWRIGHT_MARKER = "playwright"


class BrowserExtraMissing(RuntimeError):
    pass


class BrowserNotFound(RuntimeError):
    pass


class CaptureTimeout(RuntimeError):
    pass


class WindowClosed(CaptureTimeout):
    """The user closed the window before a token appeared — a `CaptureTimeout` to every
    existing caller, distinguishable for the ones that want to say so."""


class NotDurable(RuntimeError):
    """CR issued a `hash`, but as a SESSION cookie (no expiry): "remember me" did not take, so
    the credential would die in days while a stored status claimed a year. Not a
    `CaptureTimeout` — a token did appear — and nothing is captured."""


# How long to keep polling after a session-only `hash` is first seen before giving up on a
# durable one: the login's redirect hops can set cookies across polls, and the durable cookie
# may land a moment after the session one. Nothing is captured either way until it does.
DURABLE_GRACE_S = 5.0
# The least life a `hash` must have to be the capture. The "remember me" mint runs 365 days
# (`RECON.md` §5); a `hash` that expires within two days is the same failure as a session
# cookie with a date on it — the 2026-09-05 capture authenticated for 24 h 02 min and then
# died — and storing it would put a credential that dies tomorrow behind "member session
# active". Two days, not a year: CR shortening the remember-me term must not refuse every
# capture, and anything shorter than this needs a new sign-in daily, which IS the failure.
MIN_DURABLE_S = 2 * 86400


@dataclasses.dataclass(frozen=True)
class Capture:
    """What `capture_hash` returns: the token and the expiry the browser reported for it. The
    value is excluded from `repr`, so a logged or formatted `Capture` never carries it."""

    value: str = dataclasses.field(repr=False)
    expires_at: str  # ISO-8601 UTC, `credentials.expiry_from_posix`; never None — see NotDurable


def extra_installed() -> bool:
    """Whether Playwright is importable — without importing it (no driver process, no cost)."""
    import importlib.util

    return importlib.util.find_spec("playwright") is not None


def _import_playwright() -> Any:
    """The heavy import. Measured at 0.28 s cold (the Desktop bundle ships no `.pyc`, so the
    first sign-in per install pays it) — synchronous work that must not run on the loop."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowserExtraMissing(INSTALL_HINT) from exc
    return async_playwright


async def load_playwright() -> Any:
    """`async_playwright`, imported on a worker thread so the loop keeps serving tool calls."""
    return await asyncio.to_thread(_import_playwright)


def launch_plan(which: Callable[[str], str | None] | None = None) -> Iterator[tuple[str, dict]]:
    """(label, launch kwargs) in the order to try: installed Chrome, then Edge, then any
    Chromium-family executable on PATH. `_launch` adds `headless=False` (headed is not a
    preference — the user has to type), `args=LAUNCH_ARGS` and
    `ignore_default_args=IGNORE_DEFAULT_ARGS`, so a rung must NOT yield those three keys:
    they are passed as explicit keywords and `**kwargs` would collide with them."""
    which = which or shutil.which  # resolved at call time, not bound at import
    for channel in CHANNELS:
        yield channel, {"channel": channel}
    for name in EXECUTABLE_NAMES:
        path = which(name)
        if path:
            yield name, {"executable_path": path}


async def _launch(pw: Any) -> tuple[Any, str]:
    failures: list[str] = []
    for label, kwargs in launch_plan():
        try:
            browser = await pw.chromium.launch(
                headless=False,
                args=list(LAUNCH_ARGS),
                ignore_default_args=list(IGNORE_DEFAULT_ARGS),
                **kwargs,
            )
        except Exception as exc:  # not installed, or not launchable: try the next rung
            failures.append(f"{label}: {type(exc).__name__}")
            continue
        return browser, label
    raise BrowserNotFound(
        "no installed Chrome, Edge or Chromium-family browser could be launched (nothing is "
        "downloaded). Install Chrome, or use the paste path: `consumer-reports-mcp auth`. "
        f"Tried: {', '.join(failures) or 'nothing'}."
    )


# --------------------------------------------------------------------------- kill of last resort


async def browser_pid(browser: Any) -> int | None:
    """The browser process id, from a browser-level CDP session (`SystemInfo.getProcessInfo`,
    ~6 ms, Chromium-family only). None when unavailable — the kill of last resort is then off,
    and only the graceful path remains."""
    try:
        cdp = await browser.new_browser_cdp_session()
        try:
            info = await cdp.send("SystemInfo.getProcessInfo")
        finally:
            with contextlib.suppress(Exception):
                await cdp.detach()
        for proc in info.get("processInfo", []):
            if proc.get("type") == "browser":
                return int(proc["id"])
    except Exception as exc:
        log.debug("browser pid unavailable (%s)", type(exc).__name__)
    return None


# The process-table query's budget. It runs only in the wedged-driver case, where a slow answer
# costs seconds and a wrong one an orphan — and on Windows a cold `Get-CimInstance` has to start
# PowerShell and the WMI provider host first, which the old 5 s did not always cover.
COMMAND_LINE_TIMEOUT_S = 15.0

# The Windows query answers with an exit code, so "no such pid", "a pid whose command line WMI
# will not show" and "the query itself failed" stay distinguishable — none of them is a match.
_EXIT_GONE = 3
_EXIT_UNREADABLE = 4
_EXIT_QUERY_FAILED = 5

# `_query_command_line`'s statuses. Only `found` carries a command line; the guard treats every
# other status as unknown, and unknown means no kill.
STATUS_FOUND = "found"
STATUS_GONE = "gone"
STATUS_UNREADABLE = "unreadable"
QUERY_FAILED = "query_failed:"  # prefix; the suffix names why


def _windows_command_line_script(pid: int) -> str:
    """The PowerShell for one pid. `tasklist` has no command-line column — it cannot show the
    profile marker the guard needs — so it is `Get-CimInstance Win32_Process`, which ships with
    every Windows PowerShell 5.1. `Win32_Process.CommandLine` is readable without elevation for
    the caller's own processes, which the browser is; WMI answers null for one it may not read.

    Written to `[Console]::Out` directly rather than left to the pipeline: PowerShell's host
    formats what falls out of a script for a console, and a long Chrome command line is the
    one thing this query returns. Every outcome is an exit code (`_EXIT_*`), never an empty
    stdout that a gone pid, a null `CommandLine` and a failed query would all produce alike."""
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"try {{ $p = Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' }}\n"
        f"catch {{ [Console]::Error.WriteLine($_.Exception.Message); exit {_EXIT_QUERY_FAILED} }}\n"
        f"if ($null -eq $p) {{ exit {_EXIT_GONE} }}\n"
        f"if ([string]::IsNullOrEmpty($p.CommandLine)) {{ exit {_EXIT_UNREADABLE} }}\n"
        "[Console]::Out.Write($p.CommandLine)\n"
        "exit 0\n"
    )


def _command_line_argv(pid: int) -> list[str]:
    """`ps -ww` on POSIX, `powershell -EncodedCommand` on Windows.

    `-ww` is load-bearing: procps `ps` cuts each line to the terminal width — 80 columns when
    stdout is a pipe — so on Linux the plain query returned a real Chrome's command line as
    `/opt/google/chrome/chrome --disable-field-trial-config --disable-background-netw` (CI,
    ubuntu-latest, 2026-09-06), never reaching the profile marker, and the guard never matched
    there. macOS `ps` never truncates a pipe and accepts `-ww` (measured), so the spelling is
    one for both. Linux reads `/proc/<pid>/cmdline` first (`_proc_command_line`); this is the
    fallback where there is no procfs.

    On Windows the script travels base64-encoded (UTF-16LE, PowerShell's `-EncodedCommand`),
    so no quoting rule between `subprocess`'s argv join and PowerShell's own parser can touch
    it. `powershell` (5.1, on the system PATH of every stock Windows), never `pwsh` (7, an
    optional install). The argv and the parsing are pinned by `tests/test_browser_auth.py`;
    the CI workflow runs the real query on a Windows runner."""
    if sys.platform == "win32":
        script = _windows_command_line_script(pid).encode("utf-16-le")
        encoded = base64.b64encode(script).decode("ascii")
        return ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    return ["ps", "-ww", "-o", "command=", "-p", str(pid)]


def _proc_command_line(pid: int, proc: str = "/proc") -> tuple[str | None, str] | None:
    """Linux: `/proc/<pid>/cmdline`, the kernel's own NUL-separated argv — exact, unbounded,
    no subprocess, and a pid that is gone is `ENOENT` rather than a parsed exit code. None
    when there is no procfs at all (macOS), which sends the caller to `ps -ww`.

    An EMPTY `cmdline` is not an answer, so it falls through to `ps` rather than reporting
    `unreadable`. The kernel empties it for three unrelated states: a zombie, a kernel thread,
    and a process still inside `execve`. Reporting `unreadable` for all three short-circuited
    the fallback and broke the third — CI 2026-09-06 failed on Linux reading a child the test
    had just spawned, because this read has no subprocess to spawn and beat the child's own
    exec, a race `ps` never lost since spawning it cost the milliseconds exec needed. `ps`
    resolves all three: `<defunct>` for a zombie, `[kthread]` for a kernel thread, the real
    argv once exec lands — and none of the first two carries the markers, so nothing that is
    already dead or was never a browser can be signalled."""
    if not os.path.exists(f"{proc}/self/cmdline"):
        return None
    try:
        with open(f"{proc}/{int(pid)}/cmdline", "rb") as fh:
            raw = fh.read()
    except (FileNotFoundError, ProcessLookupError):
        return None, STATUS_GONE
    except PermissionError:
        return None, STATUS_UNREADABLE
    except OSError as exc:
        return None, f"{QUERY_FAILED}{type(exc).__name__}"
    text = " ".join(arg.decode("utf-8", "replace") for arg in raw.split(b"\0") if arg)
    if not text.strip():
        return None  # zombie, kernel thread or mid-exec — `ps` tells them apart
    return text, STATUS_FOUND


def _query_command_line(pid: int) -> tuple[str | None, str]:
    """`(command_line, status)` for a pid from the real process table. The command line is
    non-None exactly when the status is `found`; the other statuses — `gone`, `unreadable`,
    `query_failed:<why>` — are why it is not, so a guard that never matches can say whether
    the process is gone or the query is broken. CI 2026-09-06 showed both faces of a flat
    None: on Linux the guard never matched a LIVE Chrome (truncated `ps`), and on Windows the
    real Chrome came back None while a padded `python.exe` child had matched.

    Decoded with `errors="replace"`: both markers are ASCII, and a strict decode turned the
    guard into an exception. Windows PowerShell 5.1 writes redirected stdout in the console's
    OEM code page while `text=True` decodes with the ANSI one, and the profile path carries the
    user's name (`C:\\Users\\<name>\\AppData\\Local\\Temp\\playwright_…`) — a `ü` is 0x81 in
    cp850, which Python's cp1252 refuses. `UnicodeDecodeError` is not an `OSError`, so it
    escaped `kill_browser`, out of `_close_browser`'s `finally` — replacing a captured token
    with a traceback and skipping `_disarm_reaper` — and again from the `atexit` reaper. On
    Windows the query also runs with `CREATE_NO_WINDOW`: a console program spawned from a
    process without a console (the Desktop-launched server) otherwise opens a console window
    of its own for the duration — Playwright's Python transport hides its driver the same way."""
    if sys.platform != "win32":
        answer = _proc_command_line(pid)
        if answer is not None:
            return answer
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run(
            _command_line_argv(pid),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=COMMAND_LINE_TIMEOUT_S,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        return None, f"{QUERY_FAILED}timeout"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{QUERY_FAILED}{type(exc).__name__}"
    if sys.platform == "win32":
        if out.returncode == _EXIT_GONE:
            return None, STATUS_GONE
        if out.returncode == _EXIT_UNREADABLE:
            return None, STATUS_UNREADABLE
        if out.returncode != 0:
            return None, f"{QUERY_FAILED}exit_{out.returncode}"
    elif out.returncode != 0:
        # `ps -p` of a pid that is gone exits 1 and says nothing (procps and BSD alike); a
        # query `ps` could not run complains on stderr.
        why = out.stderr.strip().splitlines()[0] if out.stderr.strip() else ""
        return (None, f"{QUERY_FAILED}exit_{out.returncode}:{why}") if why else (None, STATUS_GONE)
    if not out.stdout.strip():
        return None, STATUS_UNREADABLE
    return out.stdout, STATUS_FOUND


def _command_line(pid: int) -> str | None:
    """The process's command line, None when it cannot be known — and unknown means no kill.
    `_query_command_line` says why; this logs it, since the guard's callers only need the
    text and a `None` used to be silent about which of three things it meant."""
    cmd, status = _query_command_line(pid)
    if cmd is None:
        log.debug("command line of pid %d unknown (%s)", pid, status)
    return cmd


def kill_browser(
    pid: int,
    *,
    command_line: Callable[[int], str | None] = _command_line,
    kill: Callable[[int, int], None] = os.kill,
) -> bool:
    """SIGTERM the browser process — Playwright launches it in its own process group and Chrome
    takes its helpers down with it (measured on macOS: 7 processes gone in 0.5 s). On Windows
    there is no process group (`os.killpg` does not exist and Playwright spawns the browser
    with `detached: false` there): `os.kill` is `TerminateProcess` of the browser process alone,
    unconditional, exit code 15, and its helpers are expected to exit when their IPC channel to
    it closes — `tests/live/test_browser_hardware.py` measures exactly that on a real Chrome,
    and the CI workflow runs it on a Windows runner. Only while `pid` is still a
    Playwright-profiled browser. Returns whether a signal was sent."""
    cmd = command_line(pid) or ""
    if PROFILE_MARKER not in cmd or PLAYWRIGHT_MARKER not in cmd.lower():
        return False
    try:
        kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    except OSError as exc:
        log.debug("could not terminate browser pid %d (%s)", pid, type(exc).__name__)
        return False
    log.warning("sign-in: the browser window did not close on its own; terminated pid %d", pid)
    return True


_pending_browsers: set[int] = set()  # launched, not yet confirmed closed
_reaper_armed = False


def _arm_reaper(pid: int) -> None:
    global _reaper_armed
    _pending_browsers.add(pid)
    if not _reaper_armed:
        atexit.register(reap_pending_browsers)
        _reaper_armed = True


def _disarm_reaper(pid: int) -> None:
    _pending_browsers.discard(pid)


def pending_browsers() -> frozenset[int]:
    return frozenset(_pending_browsers)


def reap_pending_browsers() -> None:
    """`atexit`: a loop torn down under a capture never reaches the capture's `finally`, so
    every browser still pending is terminated here. Idempotent."""
    for pid in list(_pending_browsers):
        _pending_browsers.discard(pid)
        kill_browser(pid)


def _retrieve(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()  # an abandoned step's error is not news: no "never retrieved" noise


async def _await_bounded(aw: Awaitable[Any], deadline: float) -> bool:
    """Run `aw` as its own task and wait until `deadline`; False on timeout or any exception,
    never raises either. Only cancellation of the awaiting task itself propagates.

    NOT `wait_for`: on timeout `wait_for` cancels the awaitable and then WAITS for it, and a
    Playwright call absorbs that first cancellation — `_inner_send` catches it, sends
    `__abort__` and awaits the abort's own reply — so with the driver's reader gone it hangs
    exactly as before (measured: the timeout fired, `cancelling()` was 2, and the task sat in
    `_abort`'s wait until a THIRD cancel). So a step that overruns is abandoned, not awaited:
    cancelled twice (the second lands on the abort's wait, so the task finishes instead of
    being destroyed pending at loop close) and left behind for the pid kill to cover."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        if asyncio.iscoroutine(aw):
            aw.close()
        return False
    task = asyncio.ensure_future(aw)
    task.add_done_callback(_retrieve)
    done, _ = await asyncio.wait({task}, timeout=remaining)
    if task in done:
        if task.cancelled() or task.exception() is not None:
            log.debug("browser teardown step failed: %s", task.exception() or "cancelled")
            return False
        return True
    log.debug("browser teardown step overran its budget; abandoning it")
    task.cancel()
    task.cancel()
    # the two cancellations land over ~3 loop iterations; at loop shutdown those iterations
    # may not come, and a task still pending at close is destroyed with a traceback on stderr
    await asyncio.wait({task}, timeout=ABANDON_GRACE_S)
    return False


async def _close_browser(context: Any, browser: Any, pid: int | None, deadline: float) -> None:
    """Close the window: context, then browser, within the shared budget. What the graceful path
    does not confirm closed is terminated by pid — even if this task is cancelled again
    mid-teardown, because the kill is synchronous and sits in the `finally`."""
    closed = False
    try:
        closed = context is None or await _await_bounded(context.close(), deadline)
        closed = await _await_bounded(browser.close(), deadline) and closed
    finally:
        if pid is not None:
            if not closed:
                kill_browser(pid)
            _disarm_reaper(pid)


def _is_token(cookie: dict) -> bool:
    """CR's `hash`, and only that: `context.cookies()` is every cookie in the window, so a stray
    `hash` from another site — or a CR cookie of that name that is not the 36-character token —
    must keep the poll going rather than become the capture."""
    if cookie.get("name") != TOKEN_COOKIE:
        return False
    domain = str(cookie.get("domain") or "").lstrip(".").lower()
    if not (domain == TOKEN_DOMAIN or domain.endswith("." + TOKEN_DOMAIN)):
        return False
    return bool(BARE_HASH.match(str(cookie.get("value") or "")))


def _expires(cookie: dict) -> float | None:
    """The cookie's expiry as POSIX seconds, or None for a session cookie. Playwright reports
    `expires: -1` for one; a missing or unreadable field is treated the same way, because an
    expiry that cannot be read cannot be stored as measured."""
    try:
        exp = float(cookie.get("expires"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return exp if exp > 0 else None


def _durable_expires(cookie: dict, now: float | None = None) -> float | None:
    """The expiry, when it is far enough out to be the capture (`MIN_DURABLE_S`); else None —
    a session cookie and a short-lived one are the same failure here."""
    exp = _expires(cookie)
    if exp is None or exp - (time.time() if now is None else now) < MIN_DURABLE_S:
        return None
    return exp


def _describe(cookie: dict) -> str:
    """The cookie's attributes for the log — NEVER its value. This line is what would have
    answered "was the 2026-09-05 capture ever durable": the domain CR set it on and the
    expiry it carried, recorded at the moment of capture."""
    exp = _expires(cookie)
    if exp is None:
        life = "expires=session"
    else:
        life = f"expires={expiry_from_posix(exp)} ({(exp - time.time()) / 86400:.1f} d)"
    return (
        f"domain={cookie.get('domain')} path={cookie.get('path')} "
        f"secure={cookie.get('secure')} httpOnly={cookie.get('httpOnly')} {life}"
    )


def _signature(cookie: dict) -> tuple:
    """What makes two observations of a `hash` the same for logging purposes: the attributes
    and the value's identity — a re-mint with a new value is a new observation."""
    return (
        cookie.get("domain"),
        cookie.get("path"),
        cookie.get("secure"),
        cookie.get("httpOnly"),
        _expires(cookie),
        hash(str(cookie.get("value"))),  # identity only; the value itself is never kept
    )


def _prefer(cookie: dict) -> tuple:
    """Among several durable `hash` cookies in one window, the capture is the one on the apex
    domain (what the jar seeds and every re-mint reads, `credentials.COOKIE_DOMAIN`) with the
    farthest expiry. `context.cookies()` is unordered as far as this flow is concerned, so the
    first match was whichever the browser listed first."""
    domain = str(cookie.get("domain") or "").lstrip(".").lower()
    return (domain == TOKEN_DOMAIN, _expires(cookie) or 0.0)


def _short_life_text(cookie: dict) -> str:
    exp = _expires(cookie)
    if exp is None:
        return "a session-only cookie — one with no expiry"
    hours = max(exp - time.time(), 0.0) / 3600
    return f"a cookie that expires in {hours:.1f} hours (at {expiry_from_posix(exp)})"


async def _keep_remember_me(page: Any) -> bool:
    """Re-assert the tick; True when the box is confirmed checked now. Never fails the poll:
    a page with no form (mid-navigation, or once signed in) simply answers False."""
    try:
        await page.check(REMEMBER_ME_SELECTOR, timeout=REMEMBER_ME_RECHECK_TIMEOUT_MS)
    except Exception:
        return False
    return True


async def capture_hash(
    timeout_s: int = 300,
    *,
    playwright_factory: Any | None = None,
    poll_s: float = 1.0,
    on_launch: Callable[[str], None] | None = None,
) -> Capture:
    """Return the `hash` cookie and its expiry once the user has signed in, or raise.

    Only a DURABLE `hash` is the capture. A `hash` the browser reports without an expiry is a
    session cookie — "remember me" did not take — and after `DURABLE_GRACE_S` without a durable
    one arriving the poll ends in `NotDurable`, which stores nothing: the alternative, storing
    it under the assumed 365-day bound, is exactly the credential-dies-overnight-while-the-
    status-says-a-year failure this module exists to prevent.

    `on_launch(label)` is called as soon as a browser window is up, so a caller that must answer
    within seconds (`cr_sign_in`) can report which browser opened without waiting for the user.
    """
    if playwright_factory is None:
        playwright_factory = await load_playwright()
    driver = playwright_factory()
    pw = await driver.__aenter__()
    try:
        browser, label = await _launch(pw)
        pid = await browser_pid(browser)
        if pid is not None:
            _arm_reaper(pid)
        context = None
        try:
            if on_launch is not None:
                on_launch(label)
            # fresh and throwaway — never a real profile, never a storage_state
            context = await browser.new_context(**CONTEXT_OPTIONS)
            page = await context.new_page()
            await page.goto(LOGIN_URL)
            try:
                await page.check(REMEMBER_ME_SELECTOR, timeout=5000)
            except Exception as exc:  # a selector change must warn, not fail
                # Not an instruction: CR renders the box checked already (RECON §5), so the
                # tick is a guard and a selector change leaves it checked anyway.
                log.warning(
                    "could not confirm remember-me (%s); CR normally pre-checks it",
                    type(exc).__name__,
                )
            capture_deadline = time.monotonic() + timeout_s
            session_only_since: float | None = None  # when a non-durable `hash` first showed
            short_lived: dict | None = None  # the non-durable one seen, for the message
            seen: set[tuple] = set()  # each distinct `hash` observation is logged once
            while time.monotonic() < capture_deadline:
                # the guard is re-applied every poll, not once: see REMEMBER_ME_RECHECK_TIMEOUT_MS
                await _keep_remember_me(page)
                try:
                    cookies = await context.cookies()
                except Exception as exc:  # the window was closed: a clean "nothing captured"
                    raise WindowClosed(
                        "the browser window was closed before a session token appeared — "
                        "nothing was captured"
                    ) from exc
                candidates = [c for c in cookies if _is_token(c)]
                for cookie in candidates:
                    sig = _signature(cookie)
                    if sig not in seen:
                        seen.add(sig)
                        log.info("sign-in: `hash` observed: %s", _describe(cookie))
                durable = [c for c in candidates if _durable_expires(c) is not None]
                if durable:
                    best = max(durable, key=_prefer)
                    expires = _durable_expires(best)
                    assert expires is not None
                    log.info("sign-in: capturing the `hash` %s", _describe(best))
                    return Capture(value=str(best["value"]), expires_at=expiry_from_posix(expires))
                if candidates and session_only_since is None:
                    session_only_since = time.monotonic()
                    short_lived = min(candidates, key=_prefer)
                    log.warning(
                        "sign-in: CR issued a non-durable `hash` (%s); waiting %.0f s for a "
                        "durable one",
                        _describe(short_lived),
                        DURABLE_GRACE_S,
                    )
                if (
                    session_only_since is not None
                    and time.monotonic() - session_only_since >= DURABLE_GRACE_S
                ):
                    assert short_lived is not None
                    raise NotDurable(
                        f"Consumer Reports issued {_short_life_text(short_lived)} rather than "
                        'the year-long one "remember me" mints, which means "remember me" did '
                        "not take and the session would die within days — so nothing was "
                        'captured. Sign in again leaving "Remember Me" ticked.'
                    )
                await asyncio.sleep(poll_s)
            raise CaptureTimeout(
                f"no session token appeared within {timeout_s} s — nothing was captured"
            )
        finally:
            await _close_browser(context, browser, pid, time.monotonic() + CLOSE_TIMEOUT_S)
    finally:
        # stopping the driver is bounded too: with the loop shutting down its reader is already
        # cancelled, and an unbounded stop is the deadlock all over again
        await _await_bounded(
            driver.__aexit__(None, None, None), time.monotonic() + DRIVER_STOP_TIMEOUT_S
        )
