"""Console entry points (PLAN P9.3, SPEC §6 *consumer-reports-mcp auth*, §9).

`consumer-reports-mcp`        → the MCP server over stdio
`consumer-reports-mcp auth`   → paste a session token on stdin (never argv), validate it against
                                one real fetch, store it 0600. `--status`, `--forget`, `--browser`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, TextIO

from .auth_tools import validate_cookies
from .config import ConfigError, Settings
from .credentials import (
    DURABLE_COOKIE,
    DURABLE_COOKIE_DAYS,
    ENV_VAR,
    EXPIRY_MEASURED,
    CredentialStore,
    default_store,
    parse_cookie_input,
)
from .runtime import Runtime, configure_logging

if TYPE_CHECKING:
    from .browser_auth import Capture

PASTE_HELP = (
    "Paste a Consumer Reports session token and press Enter.\n"
    "  In your browser, logged in to consumerreports.org, open the DevTools Console and run:\n"
    "    document.cookie.match(/(?:^|;\\s*)hash=([^;]*)/)[1]\n"
    "  That prints a 36-character token; right-click it and choose `Copy string contents`.\n"
    "  (Chrome and Firefox ask you to type `allow pasting` before they accept the line above.)\n"
    '  A `Cookie:` header or a full "Copy as cURL" paste is accepted too; a cURL paste spans\n'
    "  several lines, so end that one with Ctrl-D."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="consumer-reports-mcp",
        description="Consumer Reports ratings as MCP tools over stdio; no arguments serves.",
    )
    sub = parser.add_subparsers(dest="command")
    auth = sub.add_parser(
        "auth",
        help="capture, inspect or forget a member session",
        description=PASTE_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    auth.add_argument(
        "--status", action="store_true", help="report the stored session (no network)"
    )
    auth.add_argument("--forget", action="store_true", help="delete the stored session")
    auth.add_argument(
        "--browser",
        action="store_true",
        help="open the installed Chrome on CR's login page and capture the token (needs the "
        "[browser] extra)",
    )
    auth.add_argument("--timeout", type=int, default=300, help="seconds to wait for --browser")
    auth.add_argument("--paste-file", help="read the paste from a file instead of stdin")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        from .server import main as server_main

        return server_main()
    return auth_command(args, os.environ, sys.stdin, sys.stdout, sys.stderr)


# --------------------------------------------------------------------------- auth


def auth_command(
    args: argparse.Namespace,
    env: dict[str, str] | os._Environ,
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
    *,
    runtime_factory: Callable[[Settings, CredentialStore], Runtime] | None = None,
    capture: Callable[[int], Capture] | None = None,
) -> int:
    try:
        settings = Settings(env=env)
    except ConfigError as exc:
        print(f"consumer-reports-mcp: {exc}", file=stderr)
        return 2
    store = default_store(settings.config_dir, env=env)
    if args.status:
        return _status(store, stdout, stderr)
    if args.forget:
        existed = store.forget()
        print("stored session deleted" if existed else "no stored session", file=stdout)
        return 0
    if args.browser:
        # The window opens on the user's screen and this blocks for up to --timeout. Without
        # a line here the terminal is silent for five minutes while a browser appears
        # unannounced, which reads as a hung command.
        print(
            "Opening Consumer Reports' sign-in page in a browser window.\n"
            "Sign in there — the password goes to CR's own form and is never seen here; only "
            f"the resulting session cookie is kept.\nWaiting up to {args.timeout} s; closing "
            "the window cancels.",
            file=stderr,
        )
        try:
            captured = (capture or _browser_capture)(args.timeout)
        except Exception as exc:  # BrowserExtraMissing, BrowserNotFound, NotDurable, timeout, …
            print(str(exc), file=stderr)
            return 2
        cookies = {DURABLE_COOKIE: captured.value}
        expires_at: str | None = captured.expires_at  # measured: the browser reported it
    else:
        text = _read_paste(args.paste_file, stdin, stderr)
        cookies = parse_cookie_input(text)
        expires_at = None  # a paste carries no attributes: the status assumes the 365-day bound
        if not cookies:
            print("no `hash` or `userLicenses` cookie found in the paste", file=stderr)
            print(PASTE_HELP, file=stderr)
            return 1
    if DURABLE_COOKIE not in cookies:
        print(
            "warning: `hash` is absent from the paste — the session will authenticate now but "
            "will not survive (only `hash` is durable). Re-run with the console one-liner.",
            file=stderr,
        )
    if env.get(ENV_VAR):
        print(f"note: {ENV_VAR} is set and takes precedence over the stored file", file=stderr)
    print(
        "checking the token against consumerreports.org — one request, usually a few seconds "
        "but up to a minute on a cold start...",
        file=stderr,
        flush=True,
    )
    outcome = _validate(settings, cookies, runtime_factory)
    if outcome == "member":
        store.save(cookies, expires_at=expires_at)
        print("member session active", file=stdout)
        if expires_at is not None:
            print(f"expires {expires_at} (CR's own expiry, read from the browser)", file=stdout)
        else:
            print(
                f"expiry not known from a paste — assumed {DURABLE_COOKIE_DAYS} days from now, an "
                "upper bound (`auth --browser` records the real one)",
                file=stdout,
            )
        print(f"stored at {store.path} (0600)", file=stderr)
        return 0
    if outcome.startswith("could_not_check:"):
        # a transport failure is not a verdict on the cookie: nothing stored, nothing judged
        reason = outcome.split(":", 1)[1]
        print(f"could not check the cookie ({reason}) — nothing was stored", file=stdout)
        return 2
    print(f"that cookie did not authenticate ({outcome})", file=stdout)
    return 1


def _status(store: CredentialStore, stdout: TextIO, stderr: TextIO) -> int:
    st = store.status()
    print(f"tier: {st['configured_tier']}", file=stdout)
    print(f"source: {st['source'] or 'none'}", file=stdout)
    if st["captured_at"]:
        print(f"captured: {st['captured_at']} ({st['age_days']} days ago)", file=stdout)
    left = st.get("remaining_days_max")
    if left is not None:
        # the countdown is read off CR's own expiry when the browser sign-in measured one, and
        # only assumed — 365 days from capture — for a paste, which carries no expiry
        measured = st.get("expiry_basis") == EXPIRY_MEASURED
        if left <= 0:
            basis = (
                f"{st['expires_at']}, CR's own"
                if measured
                else f"assumed: {DURABLE_COOKIE_DAYS} days from capture"
            )
            print(
                f"expiry: the cookie is past its expiry ({basis}) — CR will reject it and the "
                "server will serve anonymously. Re-run `auth` to renew.",
                file=stdout,
            )
        elif measured:
            print(
                f"expiry: {st['expires_at']} — {int(left)} days left (CR's own expiry, read from "
                "the browser at capture)",
                file=stdout,
            )
        else:
            print(
                f"expiry: at most {int(left)} days left (assumed: a pasted cookie carries no "
                f"expiry, so this is {DURABLE_COOKIE_DAYS} days from capture — an upper bound, "
                "not a measurement)",
                file=stdout,
            )
        if 0 < left <= 30:
            print(
                "warning: renew soon — re-run `auth`. Using the cookie does not extend it; "
                "only a fresh sign-in with remember-me mints a new one.",
                file=stderr,
            )
    print(f"hash present: {'yes' if st['hash_present'] else 'no'}", file=stdout)
    print(f"userLicenses present: {'yes' if st['userLicenses_present'] else 'no'}", file=stdout)
    return 0


def _read_paste(path: str | None, stdin: TextIO, stderr: TextIO) -> str:
    if path:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    if not stdin.isatty():  # piped or redirected: the writer decides where the input ends
        return stdin.read()
    print(PASTE_HELP, file=stderr)
    # Stop at the first line that is already a usable credential. A bare token and a one-line
    # `Cookie:` header are complete the moment Enter is pressed, and asking for a Ctrl-D to
    # confirm something complete is a step that can only cost time — the durable cookie is all
    # `auth` needs. A cURL paste spans lines, so keep reading while a line continues with `\`
    # or nothing has parsed yet, and let EOF end it as before.
    lines: list[str] = []
    for line in stdin:
        lines.append(line)
        if line.rstrip().endswith("\\"):
            continue
        if DURABLE_COOKIE in parse_cookie_input("".join(lines)):
            break
    return "".join(lines)


def _browser_capture(timeout_s: int) -> Capture:
    from .browser_auth import capture_hash

    def announce(label: str) -> None:
        print(f"  browser: {label}", file=sys.stderr)

    return asyncio.run(capture_hash(timeout_s=timeout_s, on_launch=announce))


def _validate(
    settings: Settings,
    cookies: dict[str, str],
    runtime_factory: Callable[[Settings, CredentialStore], Runtime] | None,
) -> str:
    """The CLI's synchronous wrapper over the one shared verdict routine (`auth_tools
    .validate_cookies`, which `cr_sign_in` also uses): one real fetch of the probe category with
    the pasted cookies held in MEMORY — nothing is written unless it authenticates."""
    configure_logging()
    return asyncio.run(validate_cookies(settings, cookies, runtime_factory))
