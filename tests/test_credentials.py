"""P1.2 — parse, store, precedence, rotation write-back, health (SPEC §6)."""

from __future__ import annotations

import json
import logging
import stat
import sys
from pathlib import Path

import pytest

from consumer_reports_mcp.credentials import (
    CredentialStore,
    SessionHealth,
    SessionState,
    parse_cookie_input,
)

HASH = "a" * 36
LICENSES = "lic-" + "b" * 60
CURL = (
    "curl 'https://www.consumerreports.org/appliances/refrigerators/c37162/' \\\n"
    "  -H 'accept: text/html' \\\n"
    f"  -H 'Cookie: JSESSIONID=zzz; hash={HASH}; userToken=tok; "
    f"userLicenses={LICENSES}; other=1' \\\n"
    "  -H 'user-agent: Mozilla/5.0'"
)


# --------------------------------------------------------------------------- parsing


def test_parse_curl_keeps_only_hash_and_userlicenses():
    assert parse_cookie_input(CURL) == {"hash": HASH, "userLicenses": LICENSES}


def test_parse_curl_double_quoted_lowercase_header():
    text = f'curl "https://x/" -H "cookie: userLicenses={LICENSES}; hash={HASH}"'
    assert parse_cookie_input(text) == {"hash": HASH, "userLicenses": LICENSES}


def test_parse_curl_cookie_flag():
    assert parse_cookie_input(f"curl --cookie 'hash={HASH}; a=b' https://x/") == {"hash": HASH}
    assert parse_cookie_input(f"curl -b 'hash={HASH}' https://x/") == {"hash": HASH}


def test_parse_bare_hash():
    assert parse_cookie_input(f"  {HASH}\n") == {"hash": HASH}


def test_parse_cookie_header_and_bare_string():
    assert parse_cookie_input(f"Cookie: hash={HASH}; foo=bar") == {"hash": HASH}
    assert parse_cookie_input(f"foo=bar; hash={HASH}; userLicenses={LICENSES}") == {
        "hash": HASH,
        "userLicenses": LICENSES,
    }


def test_parse_garbage_is_empty():
    assert parse_cookie_input("") == {}
    assert parse_cookie_input("hello world") == {}
    assert parse_cookie_input("userToken=abc") == {}


# --------------------------------------------------------------------------- store


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "cfg" / "session.json"


def test_env_var_wins_over_file(store_path: Path):
    file_store = CredentialStore(store_path, env={})
    file_store.save({"hash": "f" * 36})
    env_store = CredentialStore(store_path, env={"CR_SESSION_COOKIE": f"hash={HASH}"})
    assert env_store.load() == {"hash": HASH}
    assert env_store.source == "env"
    assert env_store.captured_at is None


def test_env_var_accepts_bare_hash(store_path: Path):
    s = CredentialStore(store_path, env={"CR_SESSION_COOKIE": HASH})
    assert s.load() == {"hash": HASH}


def test_file_source_loads_schema(store_path: Path):
    s = CredentialStore(store_path, env={})
    s.save({"hash": HASH, "userLicenses": LICENSES, "userToken": "dropped"})
    data = json.loads(store_path.read_text())
    assert data["schema_version"] == 2
    # `userToken` is not ours to keep, and `userLicenses` is dropped beside a `hash`
    assert set(data["cookies"]) == {"hash"}
    assert data["captured_at"].endswith("Z")
    assert data["expires_at"] is None  # not measured: the key is there, and null
    fresh = CredentialStore(store_path, env={})
    assert fresh.load() == {"hash": HASH}
    assert fresh.source == "file" and fresh.expires_at is None


def test_a_measured_expiry_is_stored_used_and_survives_a_rotation(store_path: Path):
    """The browser sign-in reads the cookie's own expiry off the jar; stored, it is what the
    countdown counts from — `captured_at` is when WE stored it, which says nothing about
    when CR stops honouring it. A rotation write-back is not a mint and keeps it."""
    from datetime import UTC, datetime, timedelta

    from consumer_reports_mcp.credentials import expiry_from_posix

    expires_dt = datetime.now(UTC) + timedelta(days=200)
    expires = expiry_from_posix(expires_dt.timestamp())
    assert expires.endswith("Z") and expires.startswith(expires_dt.strftime("%Y-%m-%dT"))
    s = CredentialStore(store_path, env={})
    s.save({"hash": HASH}, expires_at=expires)
    assert json.loads(store_path.read_text())["expires_at"] == expires
    st = s.status()
    assert st["expires_at"] == expires and st["expiry_basis"] == "measured"
    assert st["remaining_days_max"] == 200 and st["age_days"] == 0
    # reloaded, and unmoved by a capture date that says otherwise
    data = json.loads(store_path.read_text())
    data["captured_at"] = (datetime.now(UTC) - timedelta(days=300)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store_path.write_text(json.dumps(data))
    fresh = CredentialStore(store_path, env={})
    fresh.load()
    assert fresh.expires_at == expires and fresh.status()["remaining_days_max"] == 200
    # only the durable cookie has a measured expiry to report — and a rotation write, which
    # only a `userLicenses`-only store can now take, carries `expires_at` through unchanged
    lic = CredentialStore(store_path.parent / "lic.json", env={})
    lic.save({"userLicenses": "x"}, expires_at=expires)
    assert lic.status()["expiry_basis"] is None and lic.status()["remaining_days_max"] is None
    assert lic.record_rotation("userLicenses", "new") is True
    assert json.loads((store_path.parent / "lic.json").read_text())["expires_at"] == expires
    with pytest.raises(ValueError):
        CredentialStore(store_path.parent / "bad.json", env={}).save(
            {"hash": HASH}, expires_at="next year"
        )


def test_a_version_one_file_loads_as_assumed(store_path: Path):
    """`session.json` written before expiries were recorded has no `expires_at`: it loads,
    and the countdown falls back to the assumed bound and says so. An unparseable
    `expires_at` is no expiry either, never a crash."""
    store_path.parent.mkdir(parents=True)
    v1 = {"schema_version": 1, "cookies": {"hash": HASH}, "captured_at": "2026-09-05T00:00:00Z"}
    store_path.write_text(json.dumps(v1))
    s = CredentialStore(store_path, env={})
    assert s.load() == {"hash": HASH} and s.expires_at is None
    st = s.status()
    assert st["expiry_basis"] == "assumed" and st["expires_at"] is None
    assert st["remaining_days_max"] == round(365 - st["age_days"], 1)
    assert store_path.read_text() == json.dumps(v1)  # load never writes
    store_path.write_text(json.dumps(dict(v1, schema_version=2, expires_at="whenever")))
    s.load()
    assert s.expires_at is None and s.status()["expiry_basis"] == "assumed"


@pytest.mark.skipif(sys.platform == "win32", reason="mode bits are not meaningful on Windows")
def test_file_mode_0600(store_path: Path):
    s = CredentialStore(store_path, env={})
    s.save({"hash": HASH})
    assert stat.S_IMODE(store_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store_path.parent.stat().st_mode) == 0o700
    assert not store_path.with_name("session.json.tmp").exists()


def test_rotation_writes_back_only_for_file_source(store_path: Path):
    # a `userLicenses`-only store is the only one a rotation can still persist to
    file_store = CredentialStore(store_path, env={})
    file_store.save({"userLicenses": "old"})
    assert file_store.record_rotation("userLicenses", "new") is True
    assert json.loads(store_path.read_text())["cookies"]["userLicenses"] == "new"
    assert file_store.record_rotation("userLicenses", "new") is False  # unchanged → no write

    before = store_path.read_text()
    env = {"CR_SESSION_COOKIE": "userLicenses=x"}
    env_store = CredentialStore(store_path, env=env)
    env_store.load()
    assert env_store.record_rotation("userLicenses", "rotated") is False
    assert store_path.read_text() == before  # env source never touches the file


def test_rotation_not_written_when_rejected(store_path: Path):
    s = CredentialStore(store_path, env={})
    s.save({"userLicenses": "old"})
    s.rejected = True
    assert s.record_rotation("userLicenses", "new") is False
    assert json.loads(store_path.read_text())["cookies"]["userLicenses"] == "old"


def test_a_stored_userlicenses_never_travels_beside_a_hash(store_path: Path):
    """RECON §5: a lapsed `userLicenses` SUPPRESSES the `hash` re-mint — CR serves the ordinary
    anonymous page instead of logging the durable credential back in, and the session reads as
    expired while the `hash` is still good for its year. A stored copy is always older than the
    process that would send it, so it is dropped on every path: seeded, saved, or written back.
    Seeding and persistence have to agree, or the next write puts it straight back."""
    s = CredentialStore(store_path, env={})
    s.save({"hash": HASH, "userLicenses": LICENSES})
    assert s.load() == {"hash": HASH}
    assert json.loads(store_path.read_text())["cookies"] == {"hash": HASH}
    assert s.set_cookie_strings() == [f"hash={HASH}; Domain=.consumerreports.org; Path=/; Secure"]
    # the write side: a rotation off the jar is held in memory, never persisted, beside a hash
    assert s.record_rotation("userLicenses", "fresh") is False
    assert json.loads(store_path.read_text())["cookies"] == {"hash": HASH}
    # the env var carries the same hazard and gets the same rule
    env_store = CredentialStore(
        store_path, env={"CR_SESSION_COOKIE": f"hash={HASH}; userLicenses=L"}
    )
    assert env_store.load() == {"hash": HASH}
    # a file written BEFORE the rule self-heals on read, without a write
    store_path.write_text(
        json.dumps({"schema_version": 2, "cookies": {"hash": HASH, "userLicenses": LICENSES}})
    )
    migrated = CredentialStore(store_path, env={})
    assert migrated.load() == {"hash": HASH}
    assert migrated.status()["userLicenses_present"] is False
    # alone, it is the whole credential and is kept
    solo = CredentialStore(store_path.parent / "solo.json", env={})
    solo.save({"userLicenses": LICENSES})
    assert solo.load() == {"userLicenses": LICENSES}


def test_rotation_ignores_unknown_names_and_none(store_path: Path):
    s = CredentialStore(store_path, env={})
    s.save({"hash": HASH})
    assert s.record_rotation("userToken", "x") is False
    assert s.record_rotation("userLicenses", None) is False


def test_forget_deletes(store_path: Path):
    s = CredentialStore(store_path, env={})
    s.save({"hash": HASH})
    assert s.forget() is True
    assert not store_path.exists()
    assert s.load() == {}
    assert s.forget() is False


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        '{"schema_version": 1, "cookies": ["hash"]}',
        '{"schema_version": 1, "cookies": {"hash": 123}}',
        '{"schema_version": 1, "cookies": null}',
        "[1, 2]",
    ],
)
def test_corrupt_file_is_anonymous(store_path: Path, text: str):
    store_path.parent.mkdir(parents=True)
    store_path.write_text(text)
    s = CredentialStore(store_path, env={})
    assert s.load() == {}
    assert s.source is None
    assert store_path.read_text() == text  # load never writes


def test_unparseable_env_does_not_fall_back_to_file(store_path: Path):
    CredentialStore(store_path, env={}).save({"hash": HASH})
    s = CredentialStore(store_path, env={"CR_SESSION_COOKIE": "userToken=zzz; JSESSIONID=q"})
    assert s.load() == {}
    assert s.source == "env"
    assert s.load_warning and "CR_SESSION_COOKIE" in s.load_warning
    assert "zzz" not in s.load_warning


def test_status_for_env_source(store_path: Path):
    s = CredentialStore(store_path, env={"CR_SESSION_COOKIE": f"hash={HASH}"})
    st = s.status()
    assert st["configured_tier"] == "member" and st["source"] == "env"
    assert st["captured_at"] is None and st["age_days"] is None


def test_set_cookie_strings_carry_domain_path_secure(store_path: Path):
    s = CredentialStore(store_path, env={"CR_SESSION_COOKIE": f"hash={HASH}"})
    assert s.set_cookie_strings() == [f"hash={HASH}; Domain=.consumerreports.org; Path=/; Secure"]
    lic = CredentialStore(store_path, env={"CR_SESSION_COOKIE": "userLicenses=L=="})
    assert lic.set_cookie_strings() == [
        "userLicenses=L==; Domain=.consumerreports.org; Path=/; Secure"
    ]


def test_parse_value_with_equals_and_ansi_c_quoting():
    text = f"curl 'https://x/' -H $'Cookie: userLicenses=abc==; hash={HASH}'"
    assert parse_cookie_input(text) == {"hash": HASH, "userLicenses": "abc=="}


def test_rotation_before_load_works_and_keeps_capture_date(store_path: Path):
    CredentialStore(store_path, env={}).save({"userLicenses": "old"})
    captured = json.loads(store_path.read_text())["captured_at"]
    s = CredentialStore(store_path, env={})  # not loaded yet
    assert s.record_rotation("userLicenses", "new") is True
    data = json.loads(store_path.read_text())
    assert data["cookies"]["userLicenses"] == "new" and data["captured_at"] == captured


def test_health_none_without_cookie_unverified_with():
    assert SessionState(configured=False).health is SessionHealth.NONE
    st = SessionState(configured=True)
    assert st.health is SessionHealth.UNVERIFIED
    assert st.effective_tier == "member"
    st.on_rejected()
    assert st.health is SessionHealth.DEAD
    assert st.effective_tier == "anonymous"
    st.on_marker(True, credential_present=True)
    assert st.health is SessionHealth.ACTIVE
    assert st.effective_tier == "member"
    none = SessionState(configured=False)
    none.on_marker(True, credential_present=True)  # cannot promote a process with no credential
    none.on_probe_verdict("member")
    assert none.health is SessionHealth.NONE
    assert none.effective_tier == "anonymous"


def test_health_moves_only_on_events_that_report_facts():
    """The four writers (transport, repository, cars page, sign-in) report what they saw;
    `SessionState` alone chooses. The jar rule — never `expired` (nor `active`) unless the
    transport asserted `hash` was in the jar after the response — is encoded here, once."""
    from consumer_reports_mcp import ingest
    from consumer_reports_mcp.credentials import DEAD_VERDICTS, VERDICT_MEMBER

    # the probe verdict vocabulary is `ingest`'s classification kinds, pinned
    assert VERDICT_MEMBER == ingest.MEMBER
    assert DEAD_VERDICTS == {ingest.SESSION_EXPIRED, ingest.CREDENTIAL_REJECTED}
    # no setter chooses a state from outside: the events are the whole surface
    assert not hasattr(SessionState, "mark_active") and not hasattr(SessionState, "mark_expired")

    st = SessionState(configured=True)
    st.on_marker(False, credential_present=False)  # a rebuilt jar answers anonymously
    assert st.health is SessionHealth.UNVERIFIED
    st.on_marker(None, credential_present=True)  # marker_missing is drift, not a state
    assert st.health is SessionHealth.UNVERIFIED
    st.on_marker(True, credential_present=False)  # a logged-in page this credential did not make
    assert st.health is SessionHealth.UNVERIFIED
    st.on_marker(False, credential_present=True)
    assert st.health is SessionHealth.DEAD
    st.on_marker(True, credential_present=True)
    assert st.health is SessionHealth.ACTIVE

    st = SessionState(configured=True)
    for non_verdict in ("could_not_check:connection_failed", "payload_missing", "anonymous", ""):
        st.on_probe_verdict(non_verdict)
        assert st.health is SessionHealth.UNVERIFIED, non_verdict
    st.on_probe_verdict("credential_rejected")
    assert st.health is SessionHealth.DEAD
    st.on_probe_verdict("member")
    assert st.health is SessionHealth.ACTIVE
    st.on_probe_verdict("session_expired")
    assert st.health is SessionHealth.DEAD
    st.on_rejected()
    assert st.health is SessionHealth.DEAD


def test_no_cookie_value_in_repr_or_logs(store_path: Path, caplog):
    s = CredentialStore(store_path, env={})
    s.save({"hash": HASH, "userLicenses": LICENSES})
    with caplog.at_level(logging.DEBUG):
        text = repr(s) + json.dumps(s.status()) + repr(SessionState(True))
        logging.getLogger("consumer_reports_mcp").debug("store=%r status=%s", s, s.status())
    assert HASH not in text and LICENSES not in text
    assert HASH not in caplog.text and LICENSES not in caplog.text
    status = s.status()
    assert status["hash_present"] is True and status["userLicenses_present"] is False
    assert status["configured_tier"] == "member" and status["source"] == "file"
    assert status["age_days"] is not None and status["age_days"] < 1


# --------------------------------------------------------------------------- live adoption


def test_reconfigure_moves_health_to_the_cold_start_state_for_the_new_configuration():
    """Every other event is a no-op while unconfigured by design, so a mid-process sign-in
    needs a way to go none → unverified; and a forget needs the reverse."""
    st = SessionState(configured=False)
    st.on_marker(True, credential_present=True)
    assert st.health is SessionHealth.NONE
    st.reconfigure(True)
    assert st.health is SessionHealth.UNVERIFIED and st.effective_tier == "member"
    st.on_rejected()
    st.reconfigure(True)  # a NEW cookie: the old rejection is not its problem
    assert st.health is SessionHealth.UNVERIFIED
    st.reconfigure(False)
    assert st.health is SessionHealth.NONE and st.effective_tier == "anonymous"


def test_env_override_is_the_same_non_blank_rule_load_uses(store_path: Path):
    assert CredentialStore(store_path, env={}).env_override is False
    assert CredentialStore(store_path, env={"CR_SESSION_COOKIE": ""}).env_override is False
    assert CredentialStore(store_path, env={"CR_SESSION_COOKIE": "   "}).env_override is False
    assert CredentialStore(store_path, env={"CR_SESSION_COOKIE": f"hash={HASH}"}).env_override
    # set to garbage still overrides: load() would not fall back to the file either
    assert CredentialStore(store_path, env={"CR_SESSION_COOKIE": "nonsense"}).env_override


def test_an_unexpanded_desktop_placeholder_is_unset_not_an_override(store_path: Path):
    """The bundle maps its optional field onto the env var as `${user_config.session_cookie}`.
    A host that passed that template through unexpanded for a blank field would otherwise be
    an override forever — `cr_sign_in` refusing on every start with nowhere to clear it."""
    from consumer_reports_mcp.credentials import env_value_set

    CredentialStore(store_path, env={}).save({"hash": HASH})
    for raw in ("${user_config.session_cookie}", "  ${user_config.session_cookie} "):
        s = CredentialStore(store_path, env={"CR_SESSION_COOKIE": raw})
        assert env_value_set(raw) is False and s.env_override is False
        assert s.load() == {"hash": HASH} and s.source == "file"  # falls through to the file
        assert s.load_warning is None
    # only a WHOLE `${…}` is a placeholder; anything else non-blank is a value, however odd
    assert env_value_set("${x") is True and env_value_set(f"hash={HASH}") is True
    assert env_value_set(None) is False and env_value_set("  ") is False


def test_expiry_warning_fires_inside_thirty_days_and_only_for_a_live_file_session(
    store_path: Path,
):
    from datetime import UTC, datetime, timedelta

    from consumer_reports_mcp.credentials import EXPIRING_DAYS, expiry_warnings

    assert EXPIRING_DAYS == 30
    s = CredentialStore(store_path, env={})
    s.save({"hash": HASH})
    health = SessionState(configured=True)
    assert expiry_warnings(s, health) == []  # 365 days left, assumed
    # a measured expiry inside the window warns at once, whatever the capture date says
    s.save({"hash": HASH}, expires_at=(datetime.now(UTC) + timedelta(days=12)).isoformat())
    assert expiry_warnings(s, health) == ["session_expiring:12"]
    s.save({"hash": HASH})

    def age(days: int) -> None:
        data = json.loads(store_path.read_text())
        data["captured_at"] = (datetime.now(UTC) - timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        store_path.write_text(json.dumps(data))
        s.load()

    age(334)
    assert expiry_warnings(s, health) == []  # 31 days: outside the window
    age(350)
    assert expiry_warnings(s, health) == ["session_expiring:15"]
    age(400)
    assert expiry_warnings(s, health) == ["session_expiring:0"]  # clamped, never negative
    health.on_rejected()
    assert expiry_warnings(s, health) == []  # already `session_expired`: not noise on top
    # the env source has no capture date, so no bound and no warning; nor an anonymous process
    env = CredentialStore(store_path, env={"CR_SESSION_COOKIE": f"hash={HASH}"})
    assert expiry_warnings(env, SessionState(configured=True)) == []
    assert expiry_warnings(CredentialStore(store_path / "x", env={}), SessionState(False)) == []


# --------------------------------------------------------------------------- one writer


def test_session_health_has_one_writer():
    """No module chooses a health state: outside `credentials.py` nothing assigns `.health`
    on a SessionState or calls a setter — the four writers (transport, repository, cars page,
    sign-in) report facts through the events, so the jar rule cannot diverge per surface."""
    import re

    src = Path(__file__).resolve().parents[1] / "src" / "consumer_reports_mcp"
    for path in src.rglob("*.py"):
        if path.name == "credentials.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\.mark_(active|expired)\(", text), path
        assert not re.search(r"health\.health\s*=[^=]", text), path
        assert not re.search(r"\.health\s*=\s*SessionHealth", text), path


# --------------------------------------------------------------------------- expired ≠ rejected


def test_expired_needs_the_clock_rejected_is_what_a_fetch_sees(store_path: Path):
    """Filed against a cookie with 363.7 days left that the server called `expired`: two
    conditions wore one word. `expired` is a claim about the CLOCK and is made only with local
    proof; everything a fetch can observe is CR declining to honour a cookie that has not run
    out. Reported as "expired", the natural next sentence to the owner — "your cookie expired,
    re-capture it" — was false, and sent them looking for a problem that did not exist."""
    from datetime import UTC, datetime, timedelta

    from consumer_reports_mcp.credentials import expiry_from_posix

    live = CredentialStore(store_path, env={})
    live.save(
        {"hash": HASH},
        expires_at=expiry_from_posix((datetime.now(UTC) + timedelta(days=363)).timestamp()),
    )
    st = SessionState(configured=True, store=live)
    assert st.reported == "unverified" and st.reason is None

    st.on_rejected()  # CR redirected to /ec/login
    assert st.health is SessionHealth.DEAD
    assert st.reported == "rejected" and st.reason == "login_redirect"
    assert live.status()["remaining_days_max"] > 360  # and that is NOT a contradiction

    st.reconfigure(True)
    st.on_marker(False, credential_present=True)  # an ordinary anonymous page, cookie in jar
    assert st.reported == "rejected" and st.reason == "served_anonymous"

    # the clock, and only the clock, earns the word "expired" — and it beats what a fetch saw
    dead_path = store_path.parent / "dead.json"
    lapsed = CredentialStore(dead_path, env={})
    lapsed.save(
        {"hash": HASH},
        expires_at=expiry_from_posix((datetime.now(UTC) - timedelta(days=2)).timestamp()),
    )
    old = SessionState(configured=True, store=lapsed)
    old.on_marker(False, credential_present=True)
    assert old.reported == "expired" and old.reason == "cookie_past_expiry"


def test_without_a_store_a_dead_session_is_rejected_never_expired():
    """Not knowing the expiry is not proof of one. A `SessionState` with nothing to ask reports
    what it actually observed."""
    st = SessionState(configured=True)
    st.on_probe_verdict("session_expired")
    assert st.reported == "rejected" and st.reason == "served_anonymous"
    st.reconfigure(True)
    st.on_probe_verdict("credential_rejected")
    assert st.reported == "rejected" and st.reason == "login_redirect"
    # a live session carries no reason at all
    st.on_probe_verdict("member")
    assert st.reported == "active" and st.reason is None
