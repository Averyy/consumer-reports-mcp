"""Real-browser measurements of the kill of last resort, opt-in with CR_BROWSER_LIVE=1.

NO Consumer Reports traffic: the window opens on `about:blank`, so the module needs the
`[browser]` extra and an installed Chrome, and nothing else. What `tests/test_browser_auth.py`
pins by argv and parsing, this measures on the machine it runs on — and `.github/workflows/
ci.yml` runs it on Windows, macOS and Linux runners, which is what makes the Windows branch of
`kill_browser` MEASURED rather than pinned:

  * `_command_line(pid)` through the real process table — `ps`, or `Get-CimInstance
    Win32_Process` on Windows — shows the Playwright profile marker for a real Chrome.
  * `kill_browser(pid)` with the real `os.kill` takes the browser AND its helper processes
    down. On Windows `os.kill` is `TerminateProcess` of the browser process alone (no process
    group exists to signal), so the helpers leaving is the claim under test there.
  * A Python process killed mid-capture (`SIGKILL`, `TerminateProcess` on Windows) leaves no
    browser behind: Playwright's driver force-kills from its own exit hook when its stdin
    closes — `process.kill(-pid, "SIGKILL")` on POSIX, `taskkill /pid <pid> /T /F` on Windows
    (read in the 1.62 driver). This is the measured finding SPEC §6 rests on, re-run per OS.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import subprocess
import sys
import threading
import time

import pytest

from consumer_reports_mcp import browser_auth as B

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("CR_BROWSER_LIVE") != "1", reason="set CR_BROWSER_LIVE=1 to run"
    ),
    pytest.mark.skipif(not B.extra_installed(), reason="the [browser] extra is not installed"),
]

# Playwright's throwaway profile directory: `playwright_chromiumdev_profile-<random>`. Unique
# per launch, no spaces, so it identifies the browser and every helper it spawned.
PROFILE_RE = re.compile(r"playwright_\w+?_profile-[\w.-]+")


def _profile_marker(command_line: str) -> str:
    m = PROFILE_RE.search(command_line)
    assert m, command_line
    return m.group(0)


def _pids_with(marker: str) -> list[int]:
    """Every process whose command line carries `marker`, from the real process table. The
    query process itself carries it too, and is excluded."""
    if sys.platform == "win32":
        script = (
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*"
            + marker.replace("'", "''")
            + "*' -and $_.Name -notlike 'powershell*' } | ForEach-Object { $_.ProcessId }"
        )
        argv = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
        out = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=30)
        return [int(line) for line in out.stdout.split() if line.strip().isdigit()]
    out = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
    )
    pids: list[int] = []
    for line in out.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and marker in parts[1] and int(parts[0]) != os.getpid():
            pids.append(int(parts[0]))
    return pids


def _wait_gone(marker: str, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pids_with(marker):
            return True
        time.sleep(0.5)
    return not _pids_with(marker)


async def test_kill_browser_takes_a_real_chrome_and_its_helpers_down():
    """The pid from CDP, the guard through the real process table, the real `os.kill`, and
    the process family gone afterwards — the browser and every helper on its profile."""
    factory = await B.load_playwright()
    async with factory() as pw:
        browser, label = await B._launch(pw)
        try:
            pid = await B.browser_pid(browser)
            assert pid is not None, label
            cmd = B._command_line(pid)
            assert cmd and B.PROFILE_MARKER in cmd and B.PLAYWRIGHT_MARKER in cmd.lower(), cmd
            marker = _profile_marker(cmd)
            family = _pids_with(marker)
            assert pid in family, (pid, family)
            assert len(family) >= 2, family  # the browser plus at least one helper process
            assert B.kill_browser(pid) is True  # the real os.kill: SIGTERM / TerminateProcess
            assert _wait_gone(marker, 20), _pids_with(marker)
            # gone: unknown to the guard, and unknown means no second signal
            assert B.kill_browser(pid) is False
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(browser.close(), 10)


CHILD = r"""
import asyncio, re, sys
from consumer_reports_mcp import browser_auth as B

async def main():
    factory = await B.load_playwright()
    driver = factory()
    pw = await driver.__aenter__()
    browser, _ = await B._launch(pw)
    pid = await B.browser_pid(browser)
    marker = re.search(r"playwright_\w+?_profile-[\w.-]+", B._command_line(pid) or "").group(0)
    print(f"{pid} {marker}", flush=True)
    await asyncio.sleep(300)  # until the parent kills this process

asyncio.run(main())
"""


def test_a_python_process_killed_mid_capture_leaves_no_browser_behind():
    """`SIGKILL` (`TerminateProcess` on Windows) of a Python holding an open capture: the
    driver's stdin closes, its exit hook force-kills the browser tree, nothing is left. The
    `finally` and `atexit` paths never run here — this is the driver's hook alone."""
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert proc.stdout is not None
    line: list[str] = []
    reader = threading.Thread(target=lambda: line.append(proc.stdout.readline()), daemon=True)
    reader.start()
    reader.join(90)
    try:
        assert line and line[0].strip(), "the child never reported a launched browser"
        pid_text, marker = line[0].split()
        pid = int(pid_text)
        family = _pids_with(marker)
        assert pid in family and len(family) >= 2, (pid, family)
        proc.kill()
        proc.wait(timeout=15)
        # the driver closes the browser gracefully first and force-kills after 30 s at most
        assert _wait_gone(marker, 45), _pids_with(marker)
    finally:
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=15)
        proc.stdout.close()
