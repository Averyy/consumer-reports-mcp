"""P1.1 — Settings from CR_* env vars (SPEC §9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from consumer_reports_mcp import config
from consumer_reports_mcp.config import ConfigError, Settings


def test_defaults(tmp_path: Path):
    s = Settings(env={}, home=tmp_path)
    assert s.cache_ttl_days == 30
    assert s.index_ttl_days == 90
    assert s.min_request_interval_s == 2.0
    assert s.timeout_s == 120
    assert s.attempt_timeout_s == 60
    assert s.cache_dir == tmp_path / ".cache" / "consumer-reports-mcp"
    assert s.config_dir == tmp_path / ".config" / "consumer-reports-mcp"
    assert s.db_path == s.cache_dir / "cr.db"


def test_constants():
    assert not hasattr(config, "CONFIG_DIR")  # never call Path.home() at import time
    assert config.default_config_dir(Path("/h")) == Path("/h/.config/consumer-reports-mcp")
    assert config.default_cache_dir(Path("/h")) == Path("/h/.cache/consumer-reports-mcp")
    assert config.MAX_RESPONSE_SIZE == 64 * 2**20
    assert config.WWW == "https://www.consumerreports.org"
    assert config.CARS_API == "https://cars-api.consumerreports.org/api/cars"
    assert config.CARS_PAGE_URL.endswith("/cars/acura/rdx/2026/overview/")
    assert config.AUTH_PROBE_CATEGORY == 35183
    assert config.NEGATIVE_TTL_S == 3600
    assert (config.LIMIT_FLAT, config.LIMIT_NESTED, config.LIMIT_MAX, config.FULL_LIMIT_MAX) == (
        25,
        10,
        200,
        10,
    )


def test_rate_limit_is_seconds():
    s = Settings(env={"CR_MIN_REQUEST_INTERVAL_S": "0.5"})
    assert s.min_request_interval_s == 0.5
    assert not any("rps" in name.lower() for name in vars(s))
    assert not hasattr(s, "rps")


def test_env_overrides(tmp_path: Path):
    s = Settings(
        env={
            "CR_CACHE_DIR": str(tmp_path / "c"),
            "CR_CACHE_TTL_DAYS": "7",
            "CR_INDEX_TTL_DAYS": "1",
            "CR_TIMEOUT_S": "10",
            "CR_ATTEMPT_TIMEOUT_S": "5",
        }
    )
    assert s.cache_dir == tmp_path / "c"
    assert (s.cache_ttl_days, s.index_ttl_days, s.timeout_s, s.attempt_timeout_s) == (7, 1, 10, 5)


def test_bad_values_name_the_variable():
    with pytest.raises(ConfigError, match="CR_CACHE_TTL_DAYS"):
        Settings(env={"CR_CACHE_TTL_DAYS": "soon"})
    with pytest.raises(ConfigError, match="CR_ATTEMPT_TIMEOUT_S"):
        Settings(env={"CR_TIMEOUT_S": "10", "CR_ATTEMPT_TIMEOUT_S": "20"})


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CR_TIMEOUT_S", "0"),
        ("CR_TIMEOUT_S", "nan"),
        ("CR_TIMEOUT_S", "inf"),
        ("CR_ATTEMPT_TIMEOUT_S", "0"),
        ("CR_MIN_REQUEST_INTERVAL_S", "-1"),
        ("CR_MIN_REQUEST_INTERVAL_S", "nan"),
        ("CR_CACHE_TTL_DAYS", "0"),
        ("CR_INDEX_TTL_DAYS", "-3"),
    ],
)
def test_zero_negative_and_non_finite_values_rejected(name, value):
    with pytest.raises(ConfigError, match=name):
        Settings(env={name: value})


def test_interval_zero_is_an_explicit_opt_out():
    assert Settings(env={"CR_MIN_REQUEST_INTERVAL_S": "0"}).min_request_interval_s == 0.0


def test_home_comes_from_the_passed_environment(tmp_path):
    s = Settings(env={"HOME": str(tmp_path / "h")})
    assert s.config_dir == tmp_path / "h" / ".config" / "consumer-reports-mcp"
    explicit = Settings(env={"HOME": str(tmp_path)}, home=tmp_path / "x")
    assert explicit.config_dir == tmp_path / "x" / ".config" / "consumer-reports-mcp"


def test_settings_is_frozen():
    s = Settings(env={})
    with pytest.raises(AttributeError):
        s.cache_ttl_days = 1  # type: ignore[misc]
