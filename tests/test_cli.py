"""P9.3 — `auth`: paste on stdin, validate against one real fetch, store 0600 (SPEC §6)."""

from __future__ import annotations

import io
import json
import logging
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from consumer_reports_mcp import cli
from consumer_reports_mcp.browser_auth import Capture
from consumer_reports_mcp.config import AUTH_PROBE_PATH, WWW
from consumer_reports_mcp.credentials import CredentialStore
from consumer_reports_mcp.runtime import build_runtime
from tests.conftest import FakeResponse, FakeWaferSession, make_category_page, make_login_page

HASH = "h" * 36
PROBE_URL = WWW + AUTH_PROBE_PATH


class CliHarness:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path
        self.env = {
            "HOME": str(tmp_path),
            "CR_CACHE_DIR": str(tmp_path / "cache"),
            "CR_MIN_REQUEST_INTERVAL_S": "0",  # the gate is real; the session is fake
        }
        self.sess = FakeWaferSession()
        self.rt = None

    def factory(self, settings, store):
        self.rt = build_runtime(
            settings, env={}, session_factory=lambda **kw: self.sess, credentials=store
        )
        return self.rt

    def run(self, argv: list[str], stdin: str = "") -> tuple[int, str, str]:
        args = cli.build_parser().parse_args(argv)
        out, err = io.StringIO(), io.StringIO()
        code = cli.auth_command(
            args, self.env, io.StringIO(stdin), out, err, runtime_factory=self.factory
        )
        return code, out.getvalue(), err.getvalue()

    @property
    def session_file(self) -> Path:
        return self.home / ".config" / "consumer-reports-mcp" / "session.json"

    def probe_page(self, fixture: dict, **kw) -> None:
        probe = dict(fixture, final_url=PROBE_URL)
        probe["filter_instance"] = dict(fixture["filter_instance"])
        probe["filter_instance"]["args"] = dict(fixture["filter_instance"]["args"], cid=35183)
        self.sess.route(
            PROBE_URL, FakeResponse(url=PROBE_URL, content=make_category_page(probe, **kw))
        )


def test_status_reports_how_much_life_the_cookie_has_left(tmp_path):
    """`hash` expires 365 days after the mint and using it never extends that, so age alone
    leaves the user to do the arithmetic on a credential that dies silently. A paste carries
    no expiry, so this countdown is ASSUMED, and the status says so."""
    h = CliHarness(tmp_path)
    CredentialStore(h.session_file, env={}).save({"hash": HASH})
    code, out, err = h.run(["auth", "--status"])
    assert code == 0 and "at most 365 days left" in out and "warning" not in err
    assert "assumed" in out and "upper bound" in out

    # wind the capture back to 350 days ago: still valid, but worth renewing
    path = h.session_file
    data = json.loads(path.read_text())
    old = datetime.now(UTC) - timedelta(days=350)
    data["captured_at"] = old.strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(data))
    code, out, err = h.run(["auth", "--status"])
    assert "at most 15 days left" in out
    assert "renew soon" in err and "does not extend it" in err

    data["captured_at"] = (datetime.now(UTC) - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(data))
    _code, out, _err = h.run(["auth", "--status"])
    assert "past its expiry" in out and "serve anonymously" in out and "assumed" in out


def test_status_reports_a_measured_expiry_as_cr_s_own(tmp_path):
    """A browser capture records the cookie's real expiry; the status counts from it and
    labels it, so a user can tell a measurement from the paste path's assumption. A
    version-1 `session.json` (no `expires_at`) still loads, as assumed."""
    h = CliHarness(tmp_path)
    expires = (datetime.now(UTC) + timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
    CredentialStore(h.session_file, env={}).save({"hash": HASH}, expires_at=expires)
    code, out, err = h.run(["auth", "--status"])
    assert code == 0 and f"expiry: {expires}" in out and "200 days left" in out
    assert "CR's own expiry" in out and "assumed" not in out and "warning" not in err
    # inside the window it warns, counting from the measured date, not the capture date
    soon = (datetime.now(UTC) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    CredentialStore(h.session_file, env={}).save({"hash": HASH}, expires_at=soon)
    _code, out, err = h.run(["auth", "--status"])
    assert "3 days left" in out and "renew soon" in err
    past = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    CredentialStore(h.session_file, env={}).save({"hash": HASH}, expires_at=past)
    _code, out, _err = h.run(["auth", "--status"])
    assert "past its expiry" in out and past in out and "CR's own" in out
    # a file written before expiries were recorded
    h.session_file.write_text(
        json.dumps({"schema_version": 1, "cookies": {"hash": HASH}, "captured_at": past})
    )
    _code, out, _err = h.run(["auth", "--status"])
    assert "at most 363 days left" in out and "assumed" in out


class _Tty(io.StringIO):
    """stdin that claims to be a terminal, so `auth` takes the interactive path."""

    def isatty(self) -> bool:
        return True


def _run_tty(h, text: str) -> tuple[int, str, str, _Tty]:
    tty = _Tty(text)
    out, err = io.StringIO(), io.StringIO()
    args = cli.build_parser().parse_args(["auth"])
    code = cli.auth_command(args, h.env, tty, out, err, runtime_factory=h.factory)
    return code, out.getvalue(), err.getvalue(), tty


def test_a_complete_token_needs_no_ctrl_d(tmp_path, c37162):
    """A bare token is complete the moment Enter is pressed. Waiting for an EOF the user then
    has to go and find is a step that can only cost time — and reads as a hang."""
    h = CliHarness(tmp_path)
    h.probe_page(c37162, subscriber="true", fill_scores=True)
    code, out, err, tty = _run_tty(h, HASH + "\n")
    assert code == 0 and "member session active" in out
    assert tty.read() == ""  # consumed exactly the one line it needed, never waited for more
    assert "checking the token" in err  # the wait is announced rather than silent


def test_a_multiline_curl_paste_still_reads_to_eof(tmp_path, c37162):
    h = CliHarness(tmp_path)
    h.probe_page(c37162, subscriber="true", fill_scores=True)
    curl = f"curl 'https://www.consumerreports.org/' \\\n  -H 'cookie: hash={HASH}' \\\n"
    code, out, _err, _tty = _run_tty(h, curl)
    assert code == 0 and "member session active" in out
    assert json.loads(h.session_file.read_text())["cookies"]["hash"] == HASH


def test_auth_success_writes_0600_and_caches_payload(tmp_path, c37162):
    h = CliHarness(tmp_path)
    h.probe_page(c37162, subscriber="true", fill_scores=True)
    code, out, err = h.run(["auth"], stdin=HASH + "\n")
    assert code == 0 and "member session active" in out
    assert h.session_file.exists()
    if sys.platform != "win32":
        assert stat.S_IMODE(h.session_file.stat().st_mode) == 0o600
    assert json.loads(h.session_file.read_text())["cookies"] == {"hash": HASH}
    assert h.rt.cache.has_rows(35183)  # the validation fetch is not wasted
    assert [u for u, _ in h.sess.requests] == [PROBE_URL]
    assert h.rt.cache.select_category(35183, "anonymous", 30, h.rt.clock()).tier == "member"


def test_auth_failure_writes_nothing_but_caches_anonymous_payload(tmp_path, c37162):
    h = CliHarness(tmp_path)
    h.probe_page(c37162, subscriber="false")
    code, out, err = h.run(["auth"], stdin=f"hash={HASH}; userLicenses=abc\n")
    assert code == 1 and "did not authenticate (session_expired)" in out
    assert not h.session_file.exists()
    assert h.rt.cache.select_category(35183, "anonymous", 30, h.rt.clock()).tier == "anonymous"


def test_auth_rejected_credential_is_reported(tmp_path, c37162):
    h = CliHarness(tmp_path)
    login = "https://secure.consumerreports.org/ec/login?error"
    h.sess.push(FakeResponse(url=login, content=make_login_page(), clear_cookies=("hash",)))
    probe = dict(c37162, final_url=PROBE_URL)
    probe["filter_instance"] = dict(c37162["filter_instance"])
    probe["filter_instance"]["args"] = dict(c37162["filter_instance"]["args"], cid=35183)
    h.sess.push(FakeResponse(url=PROBE_URL, content=make_category_page(probe, subscriber="false")))
    code, out, err = h.run(["auth"], stdin=HASH)
    assert code == 1 and "did not authenticate (credential_rejected)" in out
    assert not h.session_file.exists()


def test_auth_warns_when_hash_absent(tmp_path, c37162):
    h = CliHarness(tmp_path)
    h.probe_page(c37162, subscriber="true")
    code, out, err = h.run(["auth"], stdin="userLicenses=only-this\n")
    assert code == 0 and "hash" in err and "will not survive" in err
    assert json.loads(h.session_file.read_text())["cookies"] == {"userLicenses": "only-this"}


def test_auth_empty_paste_is_an_error_with_help(tmp_path):
    h = CliHarness(tmp_path)
    code, out, err = h.run(["auth"], stdin="hello world\n")
    assert code == 1 and "no `hash`" in err and "allow pasting" in err
    assert h.sess.requests == []


def test_status_no_network(tmp_path, c37162):
    h = CliHarness(tmp_path)
    h.probe_page(c37162, subscriber="true")
    h.run(["auth"], stdin=HASH)
    n = len(h.sess.requests)
    code, out, err = h.run(["auth", "--status"])
    assert (
        code == 0 and "tier: member" in out and "source: file" in out and "hash present: yes" in out
    )
    assert len(h.sess.requests) == n
    fresh = CliHarness(tmp_path / "other")
    code, out, _ = fresh.run(["auth", "--status"])
    assert code == 0 and "tier: anonymous" in out and "source: none" in out


def test_forget_deletes(tmp_path, c37162):
    h = CliHarness(tmp_path)
    h.probe_page(c37162, subscriber="true")
    h.run(["auth"], stdin=HASH)
    assert h.session_file.exists()
    code, out, _ = h.run(["auth", "--forget"])
    assert code == 0 and "deleted" in out and not h.session_file.exists()
    code, out, _ = h.run(["auth", "--forget"])
    assert code == 0 and "no stored session" in out


def test_output_never_contains_cookie_value(tmp_path, c37162, capsys):
    h = CliHarness(tmp_path)
    h.probe_page(c37162, subscriber="true")
    code, out, err = h.run(["auth"], stdin=f"hash={HASH}; userLicenses=SECRET-LIC\n")
    _, status_out, status_err = h.run(["auth", "--status"])
    captured = capsys.readouterr()
    text = out + err + status_out + status_err + captured.out + captured.err
    assert HASH not in text and "SECRET-LIC" not in text


def test_paste_file_and_env_note(tmp_path, c37162):
    h = CliHarness(tmp_path)
    h.probe_page(c37162, subscriber="true")
    paste = tmp_path / "paste.txt"
    paste.write_text(HASH)
    h.env["CR_SESSION_COOKIE"] = "hash=" + "z" * 36
    code, out, err = h.run(["auth", "--paste-file", str(paste)])
    assert code == 0 and "takes precedence" in err
    # the validation used the PASTED token, not the env var
    assert h.rt.credentials.cookies == {"hash": HASH}


def test_browser_flag_without_extra_gives_hint(tmp_path, monkeypatch):
    h = CliHarness(tmp_path)
    args = cli.build_parser().parse_args(["auth", "--browser"])
    out, err = io.StringIO(), io.StringIO()

    def capture(timeout):
        from consumer_reports_mcp.browser_auth import INSTALL_HINT, BrowserExtraMissing

        raise BrowserExtraMissing(INSTALL_HINT)

    code = cli.auth_command(
        args, h.env, io.StringIO(), out, err, runtime_factory=h.factory, capture=capture
    )
    assert code == 2 and "consumer-reports-mcp[browser]" in err.getvalue()
    assert "Traceback" not in err.getvalue()


def test_browser_flag_success_goes_through_the_same_validate_and_save(tmp_path, c37162):
    h = CliHarness(tmp_path)
    h.probe_page(c37162, subscriber="true")
    args = cli.build_parser().parse_args(["auth", "--browser", "--timeout", "7"])
    out, err = io.StringIO(), io.StringIO()
    seen = []

    expires = (datetime.now(UTC) + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def capture(timeout):
        seen.append(timeout)
        # logging must already be configured when the capture runs: its INFO record of every
        # `hash` CR set (domain, expiry) is the diagnostic this path exists to keep, and it
        # used to be configured only for the validation step that follows
        root = logging.getLogger()
        seen.append((root.getEffectiveLevel(), bool(root.handlers)))
        return Capture(value=HASH, expires_at=expires)

    code = cli.auth_command(
        args, h.env, io.StringIO(), out, err, runtime_factory=h.factory, capture=capture
    )
    assert code == 0 and seen == [7, (logging.INFO, True)]
    assert "member session active" in out.getvalue()
    stored = json.loads(h.session_file.read_text())
    assert stored["cookies"] == {"hash": HASH} and stored["expires_at"] == expires
    assert f"expires {expires}" in out.getvalue() and HASH not in out.getvalue()


def test_browser_flag_reports_a_session_only_cookie_and_stores_nothing(tmp_path):
    h = CliHarness(tmp_path)
    args = cli.build_parser().parse_args(["auth", "--browser"])
    out, err = io.StringIO(), io.StringIO()

    def capture(timeout):
        from consumer_reports_mcp.browser_auth import NotDurable

        raise NotDurable("Consumer Reports issued a session-only cookie — nothing was captured")

    code = cli.auth_command(
        args, h.env, io.StringIO(), out, err, runtime_factory=h.factory, capture=capture
    )
    assert code == 2 and "session-only" in err.getvalue() and not h.session_file.exists()


def test_paste_path_stores_no_expiry_and_says_the_bound_is_assumed(tmp_path, c37162):
    h = CliHarness(tmp_path)
    h.probe_page(c37162, subscriber="true")
    code, out, _err = h.run(["auth"], stdin=HASH + "\n")
    assert code == 0 and "assumed 365 days" in out and "upper bound" in out
    stored = json.loads(h.session_file.read_text())
    assert stored["schema_version"] == 2 and stored["expires_at"] is None


def test_main_help_lists_auth(capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["--help"])
    assert ei.value.code == 0 and "auth" in capsys.readouterr().out


def test_auth_transport_failure_is_not_a_verdict(tmp_path):
    import wafer

    h = CliHarness(tmp_path)
    h.sess.push(wafer.ConnectionFailed(PROBE_URL, "offline"))
    code, out, err = h.run(["auth"], stdin=HASH)
    assert code == 2 and "could not check the cookie (connection_failed)" in out
    assert not h.session_file.exists()


def test_bad_config_is_a_one_line_error(tmp_path):
    h = CliHarness(tmp_path)
    h.env["CR_CACHE_TTL_DAYS"] = "soon"
    code, out, err = h.run(["auth", "--status"])
    assert code == 2 and "CR_CACHE_TTL_DAYS" in err and "Traceback" not in err
