"""BOUNDARY 4: the only module that reads or writes the credential file and `CR_SESSION_COOKIE`.

SPEC §6. Cookie-only: `hash` (durable — 365 d when "remember me" took) and `userLicenses`
(derived, rotates). Never a password, never a cookie value in a log line, a repr or a status
report.

The cookie's expiry is stored when it was MEASURED — `auth --browser` and `cr_sign_in` read it
off the browser's jar — and only then. A paste carries no attributes, so that path stores none
and the status falls back to `captured_at + DURABLE_COOKIE_DAYS`, an assumed upper bound. The
two are told apart by `expiry_basis` everywhere the countdown is reported; before this the
countdown was arithmetic on our own constant and would have read "364 days left" on a cookie
that CR had already stopped honouring (a capture of 2026-09-05 died within a day, and nothing
stored could say whether it had ever been durable).
"""

from __future__ import annotations

import enum
import json
import os
import re
import shlex
import stat
import sys
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

SESSION_FILE_NAME = "session.json"
ENV_VAR = "CR_SESSION_COOKIE"
COOKIE_NAMES = ("hash", "userLicenses")
DURABLE_COOKIE = "hash"
# RECON §5: the "remember me" mint gives `hash` a fixed 365-day expiry, not a sliding window.
# This is the ASSUMED lifetime, used only when no expiry was measured (the paste path).
DURABLE_COOKIE_DAYS = 365
# `session_expiring:<days>` is emitted once the upper bound on the cookie's life is inside this
# window (SPEC §6 *renewal*). Using the cookie never extends it; only a fresh sign-in does.
EXPIRING_DAYS = 30
# 1: `{schema_version, cookies, captured_at}`. 2 adds `expires_at` — the cookie's own expiry as
# the browser reported it, or null when the credential came from a paste. A version-1 file
# loads as version 2 with `expires_at: null`, so nothing stored before needs rewriting.
SESSION_SCHEMA_VERSION = 2
# What `status()["expiry_basis"]` says about the countdown: read off the cookie the browser
# issued (`measured`), or `captured_at + DURABLE_COOKIE_DAYS` (`assumed`, the paste path).
EXPIRY_MEASURED = "measured"
EXPIRY_ASSUMED = "assumed"
# Injected with an explicit Domain so the re-mint redirect through secure.consumerreports.org
# carries it (RECON §10a); a pasted `name=value` has no attributes of its own.
COOKIE_DOMAIN = ".consumerreports.org"
COOKIE_ATTRIBUTES = f"Domain={COOKIE_DOMAIN}; Path=/; Secure"

# the shape of the durable credential: exactly 36 URL-safe characters (RECON §5)
BARE_HASH = re.compile(r"^[A-Za-z0-9\-_]{36}$")
# A Desktop bundle maps its optional "Session cookie" field onto the env var as a
# `${user_config.session_cookie}` template. A blank field is expected to arrive as "", but a
# host that passed the template through unexpanded would otherwise count as an override — and
# `cr_sign_in` would refuse forever. An unexpanded placeholder is unset, like a blank.
_UNEXPANDED_PLACEHOLDER = re.compile(r"^\s*\$\{[^}]*\}\s*$")


def env_value_set(raw: str | None) -> bool:
    """The one rule for "is the env var set": non-blank and not an unexpanded `${…}` template.
    `load()` and `env_override` both use it, so they cannot disagree."""
    return raw is not None and bool(raw.strip()) and not _UNEXPANDED_PLACEHOLDER.match(raw)


_CURL_COOKIE_HEADER = re.compile(
    r"""(?:-H|--header)\s+\$?(?P<q>['"])\s*cookie:\s*(?P<v>.*?)(?P=q)""", re.I | re.S
)
_CURL_COOKIE_FLAG = re.compile(r"""(?:-b|--cookie)\s+\$?(?P<q>['"])(?P<v>.*?)(?P=q)""", re.S)
_HEADER_PREFIX = re.compile(r"^\s*cookie\s*:\s*", re.I)


class SessionHealth(enum.StrEnum):
    """The credential's health, process-level (SPEC §7). Never derived from cookie presence alone
    beyond the cold-start `unverified`."""

    NONE = "none"  # no cookie configured
    UNVERIFIED = "unverified"  # configured, no marker-bearing fetch yet this process
    ACTIVE = "active"  # a fetch this process saw the logged-in marker
    EXPIRED = "expired"  # a fetch this process saw the logged-out marker, or a rejection


# The verdict vocabulary `validate_cookies` speaks and `on_probe_verdict` listens to — the
# `ingest` classification kinds a probe fetch can end in. Spelled here rather than imported
# because this module is a leaf (BOUNDARY 4); `tests/test_credentials.py` pins them to `ingest`'s.
VERDICT_MEMBER = "member"
DEAD_VERDICTS = frozenset({"session_expired", "credential_rejected"})


class SessionState:
    """Holder for the process-level health, moved ONLY by the events below.

    Every writer reports a FACT it observed — a login redirect, a marker together with the
    jar's state, a probe verdict, a credential adopted — and this class alone chooses the
    state. The rule the project calls its most dangerous failure mode, "never say `expired`
    unless the transport asserted `hash` was still in the jar" (SPEC §6 rule 1), is therefore
    encoded once, in `on_marker`, rather than once per surface that reads a marker."""

    def __init__(self, configured: bool) -> None:
        self.health = SessionHealth.UNVERIFIED if configured else SessionHealth.NONE

    @property
    def configured(self) -> bool:
        return self.health is not SessionHealth.NONE

    def _move(self, health: SessionHealth) -> None:
        """A transition while configured. Unconfigured, nothing moves: a marker cannot promote
        (or expire) a process that holds no credential."""
        if self.configured:
            self.health = health

    # --- events ----------------------------------------------------------------------
    def on_rejected(self) -> None:
        """CR answered a `www.` request by redirecting to `/ec/login` (RECON §10b): the
        configured credential is dead for the process. Reported by the transport, the one
        place the route is seen."""
        self._move(SessionHealth.EXPIRED)

    def on_marker(self, is_member: bool | None, *, credential_present: bool) -> None:
        """A marker-bearing page was read — `data-subscriber` on a product page,
        `window.isSubscriber` on the car page — with the configured credential.

        `credential_present` is the transport's assertion that `hash` was in the jar AFTER the
        response (`FetchResult.credential_in_jar is True`). Without it nothing moves, in either
        direction: a rebuilt jar answers anonymously and would read as `expired`, and a
        logged-in page the configured credential did not produce says nothing about that
        credential. No marker (`None`) says nothing either — `marker_missing` is drift, not a
        state."""
        if not credential_present or is_member is None:
            return
        self._move(SessionHealth.ACTIVE if is_member else SessionHealth.EXPIRED)

    def on_probe_verdict(self, verdict: str) -> None:
        """`validate_cookies`' verdict on the STORED credential (the probe IS a marker-bearing
        fetch with it): `member` → active, a dead verdict → expired. Anything else —
        `could_not_check:<reason>`, a drift code, `anonymous` — is no verdict and moves
        nothing."""
        if verdict == VERDICT_MEMBER:
            self._move(SessionHealth.ACTIVE)
        elif verdict in DEAD_VERDICTS:
            self._move(SessionHealth.EXPIRED)

    def reconfigure(self, configured: bool) -> None:
        """A credential adopted (or forgotten) mid-process: back to the cold-start state for the
        new configuration — `unverified` with a cookie, `none` without. No other event can do
        this because every other transition is a no-op while unconfigured, by design."""
        self.health = SessionHealth.UNVERIFIED if configured else SessionHealth.NONE

    @property
    def effective_tier(self) -> str:
        """SPEC §8: what the caller can currently OBTAIN — `member` for active/unverified."""
        if self.health in (SessionHealth.ACTIVE, SessionHealth.UNVERIFIED):
            return "member"
        return "anonymous"

    def __repr__(self) -> str:
        return f"SessionState(health={self.health.value})"


# --------------------------------------------------------------------------- parsing


def _split_cookie_string(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in s.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name.strip():
            out[name.strip()] = value.strip()
    return out


def parse_cookie_input(text: str) -> dict[str, str]:
    """Accept a bare `hash`, a `Cookie:` header, a `name=value; …` string, or a cURL paste.

    Keeps only `hash` and `userLicenses` (SPEC §6). Returns {} when nothing usable is present.
    """
    text = text.strip()
    if not text:
        return {}
    if BARE_HASH.match(text):
        return {DURABLE_COOKIE: text}
    cookie_str: str | None = None
    m = _CURL_COOKIE_HEADER.search(text)
    if m:
        cookie_str = m.group("v")
    else:
        m = _CURL_COOKIE_FLAG.search(text)
        if m:
            cookie_str = m.group("v")
    if cookie_str is None and text.lstrip().lower().startswith("curl"):
        # a cURL paste whose quoting we could not match: walk the tokens
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = []
        for i, tok in enumerate(tokens):
            if tok in ("-b", "--cookie") and i + 1 < len(tokens):
                cookie_str = tokens[i + 1]
                break
            is_header = tok in ("-H", "--header") and i + 1 < len(tokens)
            if is_header and tokens[i + 1].lower().startswith("cookie:"):
                cookie_str = _HEADER_PREFIX.sub("", tokens[i + 1])
                break
    if cookie_str is None:
        cookie_str = _HEADER_PREFIX.sub("", text)
    cookie_str = cookie_str.replace("\\;", ";")
    parsed = _split_cookie_string(cookie_str)
    return {k: v for k, v in parsed.items() if k in COOKIE_NAMES and v}


# --------------------------------------------------------------------------- store


def _now_iso() -> str:
    return _iso(datetime.now(UTC))


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(text: object) -> datetime | None:
    """A stored timestamp back to a datetime; None for anything that is not one."""
    if not isinstance(text, str):
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def expiry_from_posix(seconds: float) -> str:
    """The stored form of a cookie expiry a browser reports as POSIX seconds."""
    return _iso(datetime.fromtimestamp(float(seconds), tz=UTC))


class CredentialStore:
    """`session.json` (0600, atomic) with the env var taking precedence (SPEC §6).

    Values never appear in `repr`, `status()` or any exception message raised here.
    """

    def __init__(self, path: Path, env: Mapping[str, str] | None = None) -> None:
        self.path = Path(path)
        self._env: Mapping[str, str] = env if env is not None else os.environ
        self.source: str | None = None
        self.captured_at: str | None = None
        self.expires_at: str | None = None  # measured at capture, or None (paste, env, v1 file)
        self.rejected = False  # latched for the process on `credential_rejected` (D9)
        self.load_warning: str | None = None  # names a problem, never a value
        self._cookies: dict[str, str] = {}
        self._loaded = False

    # --- loading -------------------------------------------------------------
    def load(self) -> dict[str, str]:
        """Env var first (and if set, ONLY the env var — it never falls back to the file), else
        the stored file, else anonymous. Never writes."""
        self._loaded = True
        self.load_warning = None
        raw_env = self._env.get(ENV_VAR)
        if env_value_set(raw_env):
            parsed = parse_cookie_input(raw_env)
            self._cookies = parsed
            self.source = "env"
            self.captured_at = None
            self.expires_at = None
            if not parsed:
                self.load_warning = f"{ENV_VAR} is set but carries no hash or userLicenses cookie"
            return dict(self._cookies)
        data = self._read_file()
        if data:
            raw = data.get("cookies")
            if isinstance(raw, dict):
                self._cookies = {
                    k: v for k, v in raw.items() if k in COOKIE_NAMES and isinstance(v, str) and v
                }
            else:
                self._cookies = {}
                self.load_warning = f"{self.path.name} is malformed and was ignored"
            if self._cookies:
                self.source = "file"
                captured = data.get("captured_at")
                self.captured_at = captured if isinstance(captured, str) else None
                # a version-1 file has no `expires_at`; an unparseable one is no expiry either
                expires = data.get("expires_at")
                self.expires_at = expires if _parse_iso(expires) is not None else None
                return dict(self._cookies)
        self._cookies = {}
        self.source = None
        self.captured_at = None
        self.expires_at = None
        return {}

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def preload(self, cookies: Mapping[str, str]) -> None:
        """Hold pasted cookies in MEMORY for a validation fetch — source `memory` never writes
        back and nothing reaches the file unless `save()` is called (SPEC §6)."""
        self._cookies = {k: v for k, v in cookies.items() if k in COOKIE_NAMES and v}
        self.source = "memory"
        self.captured_at = None
        self.expires_at = None
        self._loaded = True

    def _read_file(self) -> dict | None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def cookies(self) -> dict[str, str]:
        self._ensure_loaded()
        return dict(self._cookies)

    def set_cookie_strings(self) -> list[str]:
        """`Set-Cookie`-format strings for the jar, with the Domain/Path/Secure attributes this
        project requires. The only sanctioned way a value leaves this module."""
        return [f"{name}={value}; {COOKIE_ATTRIBUTES}" for name, value in self.cookies.items()]

    @property
    def configured(self) -> bool:
        return bool(self.cookies)

    @property
    def has_durable(self) -> bool:
        return DURABLE_COOKIE in self.cookies

    @property
    def env_override(self) -> bool:
        """Whether the environment variable is set to something non-blank — in which case it
        wins over the file (SPEC §6), so a credential stored by a sign-in would be ignored on
        the next start. The same rule `load()` applies (`env_value_set`), so a Desktop bundle
        whose optional field was left empty — or arrived as an unexpanded `${…}` template —
        does not count as an override."""
        return env_value_set(self._env.get(ENV_VAR))

    # --- writing -------------------------------------------------------------
    def save(self, cookies: Mapping[str, str], *, expires_at: str | None = None) -> None:
        """Store the credential. `expires_at` is the durable cookie's OWN expiry as the browser
        reported it (`expiry_from_posix`), when the caller measured one; a paste has none, and
        the status then falls back to the assumed `DURABLE_COOKIE_DAYS` bound and says so."""
        kept = {k: v for k, v in cookies.items() if k in COOKIE_NAMES and v}
        if not kept:
            raise ValueError("nothing to save: no hash or userLicenses cookie present")
        if expires_at is not None and _parse_iso(expires_at) is None:
            raise ValueError("expires_at is not an ISO-8601 timestamp")
        self._cookies = kept
        self.captured_at = _now_iso()
        self.expires_at = expires_at
        self._write_atomic(self._payload())
        self.source = "file"
        self.rejected = False
        self._loaded = True

    def _payload(self) -> dict:
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "cookies": dict(self._cookies),
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
        }

    def _write_atomic(self, payload: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=".session.", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.write("\n")
            if sys.platform != "win32":
                os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # mkstemp already used 0600
            os.replace(tmp, self.path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def forget(self) -> bool:
        """Delete the stored file. Returns whether a file existed."""
        existed = self.path.exists()
        if existed:
            self.path.unlink()
        if self.source == "file":
            self._cookies = {}
            self.source = None
            self.captured_at = None
            self.expires_at = None
        return existed

    def record_rotation(self, name: str, value: str | None) -> bool:
        """Persist a re-minted cookie read from the jar — only for the file source, only when the
        value differs, never after a rejection (SPEC §6: env is read-only by nature)."""
        if value is None or name not in COOKIE_NAMES or self.rejected:
            return False
        self._ensure_loaded()
        if self.source != "file":
            return False
        if self._cookies.get(name) == value:
            return False
        self._cookies[name] = value
        if not self.captured_at:
            self.captured_at = _now_iso()
        self._write_atomic(self._payload())  # `expires_at` rides along: a rotation is not a mint
        return True

    # --- inspection ----------------------------------------------------------
    def status(self) -> dict:
        """No network, no values: tier, source, capture age, the expiry and what it rests on,
        which cookies are present."""
        cookies = self.cookies
        now = datetime.now(UTC)
        age_days: float | None = None
        captured = _parse_iso(self.captured_at)
        if captured is not None:
            age_days = round((now - captured).total_seconds() / 86400, 1)
        # `remaining_days_max` is an upper bound either way — CR can revoke a cookie before its
        # expiry — but what it is COUNTED FROM differs, and `expiry_basis` says which:
        #   measured: the cookie's own `expires`, read off the browser's jar at capture. The
        #             browser sign-in is the only path that sees it.
        #   assumed:  `captured_at + DURABLE_COOKIE_DAYS`. `hash` from a "remember me" login
        #             runs 365 days from the mint and does NOT slide (RECON §5), and a paste
        #             carries no attributes, so this is the best a paste can say. Capture can
        #             post-date the mint by any amount, and a cookie minted WITHOUT remember-me
        #             is not durable at all, so this can overstate the life left by up to a
        #             year — which is what made the 2026-09-05 capture unexplainable.
        remaining_days: float | None = None
        basis: str | None = None
        expires = _parse_iso(self.expires_at) if DURABLE_COOKIE in cookies else None
        if expires is not None:
            remaining_days = round((expires - now).total_seconds() / 86400, 1)
            basis = EXPIRY_MEASURED
        elif age_days is not None and DURABLE_COOKIE in cookies:
            remaining_days = round(DURABLE_COOKIE_DAYS - age_days, 1)
            basis = EXPIRY_ASSUMED
        return {
            "configured_tier": "member" if cookies else "anonymous",
            "source": self.source,
            "captured_at": self.captured_at,
            "age_days": age_days,
            "expires_at": self.expires_at if expires is not None else None,
            "expiry_basis": basis,
            "remaining_days_max": remaining_days,
            "hash_present": DURABLE_COOKIE in cookies,
            "userLicenses_present": "userLicenses" in cookies,
            "path": str(self.path),
        }

    def __repr__(self) -> str:
        names = ",".join(sorted(self._cookies)) or "-"  # no I/O, no values
        return f"CredentialStore(source={self.source}, cookies=[{names}], rejected={self.rejected})"


def default_store(config_dir: Path, env: Mapping[str, str] | None = None) -> CredentialStore:
    return CredentialStore(Path(config_dir) / SESSION_FILE_NAME, env=env)


def expiry_warnings(store: CredentialStore, health: SessionState) -> list[str]:
    """`session_expiring:<days>` once the cookie's remaining life is within `EXPIRING_DAYS`.

    Only for a configured, not-yet-dead session: once CR has rejected the cookie the envelope
    already says `session_expired`, and "expiring" on top of that is noise. `<days>` is
    `remaining_days_max` truncated and clamped at 0 — counted from the cookie's own expiry when
    the browser sign-in measured one, else from the capture date plus the assumed lifetime, in
    which case the real deadline is that day or earlier, never later (`status()`)."""
    if not health.configured or health.health is SessionHealth.EXPIRED:
        return []
    left = store.status().get("remaining_days_max")
    if left is None or left > EXPIRING_DAYS:
        return []
    return [f"session_expiring:{max(int(left), 0)}"]
