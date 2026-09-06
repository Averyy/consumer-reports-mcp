"""Settings from `CR_*` environment variables (SPEC §9 *Config surface*) plus shipped constants.

Pure: no I/O beyond reading the mapping it is handed. Everything here is a value; nothing here
touches the network, the cache or the credential file.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

# --- hosts and paths --------------------------------------------------------------------------
WWW = "https://www.consumerreports.org"
CARS_API = "https://cars-api.consumerreports.org/api/cars"
LOGIN_HOST = "secure.consumerreports.org"
LOGIN_URL = f"https://{LOGIN_HOST}/ec/login"
LOGIN_PATH_MARKER = "/ec/login"  # the rejection route, on the login host (`is_login_url`)


def is_login_url(url: str) -> bool:
    """CR rejects a credential by redirecting to `secure.consumerreports.org/ec/login?error`
    (`RECON.md` §10b). This is a ROUTE match — the login host and the `/ec/login` path segment —
    never a substring test: `"/ec/login" in url` matched `www.…/anything/ec/login-tips/`, and one
    such page latched `rejected`, expired the session and evicted the cookie for the process."""
    parts = urlsplit(url)
    if (parts.hostname or "").lower() != LOGIN_HOST:
        return False
    path = parts.path
    return path == LOGIN_PATH_MARKER or path.startswith(LOGIN_PATH_MARKER + "/")


AZ_INDEX_URL = f"{WWW}/cro/a-to-z-index/products/index.htm"
PRODUCTS_SITEMAP_URL = f"{WWW}/sitemaps/products.xml"
TYPEAHEAD_URL = f"{WWW}/api/search/typeahead"
TYPEAHEAD_MIN_CHARS = 3

# D12: the car page that carries both the public api-key and `window.isSubscriber`.
CARS_PAGE_URL = f"{WWW}/cars/acura/rdx/2026/overview/"

# The `auth` validation target: Robotic Vacuums, the smallest category measured (SPEC §6). It is a
# shipped constant, exempt from validate-the-id-first (D20).
AUTH_PROBE_CATEGORY = 35183
AUTH_PROBE_PATH = "/home-garden/vacuum-cleaners/robotic-vacuums/c35183/"

# --- sizes, TTLs, limits ------------------------------------------------------------------------
MAX_RESPONSE_SIZE = 64 * 2**20  # 64 MiB — mattresses is 14.4 MB, this stops a hostile body
NEGATIVE_TTL_S = 3600  # drift codes are negatively cached in memory for one hour
# `refresh=true` on a read-only tool never prompts the client, so "call it fifty times with
# refresh" would otherwise cost fifty 11 MB fetches and fifty appended rows. Inside this window
# a qualifying row is served with `refresh_skipped` instead. The `auth` probe is exempt — it
# is a verdict on a credential and must always fetch (SPEC §8 *Retention*).
REFRESH_COOLDOWN_S = 300

LIMIT_FLAT = 25
LIMIT_NESTED = 10
LIMIT_MAX = 200
FULL_LIMIT_MAX = 10
# `full` is the one shape that eats a context window: measured on the real 172-product page
# (scripts/measure_sizes.py) nested-10 is ~31k tokens even lean, so the nested DEFAULT is 5.
FULL_LIMIT_NESTED = 5
SEARCH_CAP = 25
# `cr_search`/`cr_car_search` echo `query` in `data` and hand it to CR's typeahead URL, so it
# is the one caller string that reaches a response — and a request — as typed. Longer than
# this it is refused before either (a model number is 40 characters; a category name 30).
SEARCH_QUERY_MAX_CHARS = 200
FILTER_VALUES_CAP = 12

CARS_LIMIT_SUMMARY = 25
CARS_LIMIT_SUMMARY_MAX = 100
CARS_LIMIT_STANDARD = 10
# `standard` costs one request per row at CR_MIN_REQUEST_INTERVAL_S apart: measured 21.6 s at
# limit=10, i.e. ~2.16 s per row. Claude Desktop kills a tool call at 60 s, so the old cap of 25
# projected to ~51 s with nothing left for a slow response. 15 rows is ~35 s worst case behind
# the listing request — comfortably under the cap. Page with `offset` for more.
CARS_LIMIT_STANDARD_MAX = 15

CONFIG_DIR_NAME = "consumer-reports-mcp"
SCHEMA_VERSION = 1


def _home(home: Path | None) -> Path:
    return home if home is not None else Path.home()


def default_config_dir(home: Path | None = None) -> Path:
    return _home(home) / ".config" / CONFIG_DIR_NAME


def default_cache_dir(home: Path | None = None) -> Path:
    return _home(home) / ".cache" / CONFIG_DIR_NAME


class ConfigError(ValueError):
    """A `CR_*` variable holds a value that cannot be used."""


def _num(
    env: Mapping[str, str], name: str, default: float, *, kind: type, positive: bool = False
) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = kind(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a {kind.__name__}") from exc
    if not math.isfinite(value):
        raise ConfigError(f"{name}={raw!r} must be finite")
    if value < 0 or (positive and value == 0):
        raise ConfigError(f"{name}={raw!r} must be {'positive' if positive else 'non-negative'}")
    return value


@dataclass(frozen=True)
class Settings:
    """The SPEC §9 config table. Construct with `Settings(env=os.environ)`; pure otherwise.

    `min_request_interval_s` is SECONDS BETWEEN REQUESTS, passed straight to wafer's
    `rate_limit`. It is deliberately not a requests-per-second figure (SPEC §9).
    """

    env: Mapping[str, str] | None = field(default=None, repr=False, compare=False)
    home: Path | None = field(default=None, repr=False, compare=False)

    cache_dir: Path = field(init=False)
    config_dir: Path = field(init=False)
    cache_ttl_days: int = field(init=False)
    index_ttl_days: int = field(init=False)
    min_request_interval_s: float = field(init=False)
    timeout_s: float = field(init=False)
    attempt_timeout_s: float = field(init=False)
    offline: bool = field(init=False)  # CR_OFFLINE=1: never touch the network (serve cache only)

    def __post_init__(self) -> None:
        env: Mapping[str, str] = self.env if self.env is not None else {}
        set_ = object.__setattr__
        home = self.home
        if home is None and env.get("HOME"):
            home = Path(env["HOME"])  # the passed environment is the authority, never the host's
            set_(self, "home", home)
        cache_dir = env.get("CR_CACHE_DIR")
        cache_path = Path(cache_dir).expanduser() if cache_dir else default_cache_dir(home)
        set_(self, "cache_dir", cache_path)
        set_(self, "config_dir", default_config_dir(home))
        set_(
            self, "cache_ttl_days", int(_num(env, "CR_CACHE_TTL_DAYS", 30, kind=int, positive=True))
        )
        set_(
            self, "index_ttl_days", int(_num(env, "CR_INDEX_TTL_DAYS", 90, kind=int, positive=True))
        )
        # 0 is an explicit opt-out of the politeness interval (wafer: 0.0 disables); never negative
        interval = _num(env, "CR_MIN_REQUEST_INTERVAL_S", 2.0, kind=float)
        set_(self, "min_request_interval_s", float(interval))
        timeout = _num(env, "CR_TIMEOUT_S", 120, kind=float, positive=True)
        attempt = _num(env, "CR_ATTEMPT_TIMEOUT_S", 60, kind=float, positive=True)
        set_(self, "timeout_s", float(timeout))
        set_(self, "attempt_timeout_s", float(attempt))
        set_(self, "offline", env.get("CR_OFFLINE", "").strip().lower() in ("1", "true", "yes"))
        if self.attempt_timeout_s > self.timeout_s:
            raise ConfigError("CR_ATTEMPT_TIMEOUT_S must not exceed CR_TIMEOUT_S (total budget)")

    @property
    def db_path(self) -> Path:
        return self.cache_dir / "cr.db"
