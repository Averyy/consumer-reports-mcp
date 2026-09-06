"""P9.1 — process wiring and stderr logging hygiene."""

from __future__ import annotations

import logging

from consumer_reports_mcp.config import Settings
from consumer_reports_mcp.runtime import _DropOversized, build_runtime, configure_logging
from tests.conftest import FakeWaferSession


def test_build_runtime_uses_tmp_dirs_and_one_transport(tmp_path):
    settings = Settings(env={"CR_CACHE_DIR": str(tmp_path / "cache")}, home=tmp_path)
    sess = FakeWaferSession()
    rt = build_runtime(settings, env={}, session_factory=lambda **kw: sess)
    assert rt.cache.db_path == tmp_path / "cache" / "cr.db" and rt.cache.db_path.exists()
    assert rt.credentials.path == tmp_path / ".config" / "consumer-reports-mcp" / "session.json"
    assert rt.health.health.value == "none"
    assert rt.repository.transport is rt.transport and rt.discovery.transport is rt.transport
    assert rt.cars.rt is rt
    assert rt.transport.cookie_configured is False


def test_log_filter_drops_oversized_records(capsys):
    configure_logging(logging.DEBUG)
    log = logging.getLogger("consumer_reports_mcp.test")
    log.info("short line")
    log.info("x" * 5000)  # a body-sized record never reaches stderr
    err = capsys.readouterr().err
    assert "short line" in err and "x" * 5000 not in err
    assert logging.getLogger("wafer").level == logging.WARNING
    record = logging.LogRecord("n", logging.INFO, "p", 1, "%s", ("y" * 3000,), None)
    assert _DropOversized().filter(record) is False
