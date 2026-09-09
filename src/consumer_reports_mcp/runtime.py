"""Process wiring (PLAN P9.1): settings → credentials → transport → cache → discovery →
repository, one of each per process. Logs go to stderr; no log record may carry a body."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .cache import Cache, utcnow
from .config import Settings
from .credentials import CredentialStore, SessionState, default_store, expiry_warnings
from .discovery import Discovery
from .negcache import NegativeCache
from .repository import Repository
from .transport import Transport

MAX_LOG_RECORD_CHARS = 2048


class _DropOversized(logging.Filter):
    """A response body can never reach a log line: anything over 2 KB is dropped."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return False
        return len(msg) <= MAX_LOG_RECORD_CHARS


def configure_logging(level: int = logging.INFO) -> None:
    """stderr only — stdout is the MCP protocol channel (SPEC §9)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_DropOversized())
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("wafer").setLevel(logging.WARNING)


@dataclass
class Runtime:
    settings: Settings
    credentials: CredentialStore
    health: SessionState
    transport: Transport
    cache: Cache
    negcache: NegativeCache
    discovery: Discovery
    repository: Repository
    clock: Callable[[], datetime] = utcnow
    cars: Any = None  # set by cars.api.CarsApi (P10)
    sign_in: Any = None  # set by auth_tools.SignInFlow — the one sign-in per process

    def session_warnings(self) -> list[str]:
        """`session_expiring:<days>` for every tool that carries `session` (SPEC §6)."""
        return expiry_warnings(self.credentials, self.health)


def build_runtime(
    settings: Settings,
    *,
    env: Mapping[str, str] | None = None,
    session_factory: Callable[..., Any] | None = None,
    clock: Callable[[], datetime] = utcnow,
    credentials: CredentialStore | None = None,
) -> Runtime:
    creds = credentials or default_store(settings.config_dir, env=env)
    if not creds.loaded:  # a preloaded (pasted, in-memory) store must not be re-read from disk
        creds.load()
    health = SessionState(configured=creds.configured, store=creds)
    transport = Transport(settings, creds, health, session_factory=session_factory)
    cache = Cache(settings.db_path)
    negcache = NegativeCache()
    discovery = Discovery(cache, transport, settings.index_ttl_days)
    repository = Repository(
        settings, cache, transport, discovery, negcache, creds, health, clock=clock
    )
    rt = Runtime(
        settings=settings,
        credentials=creds,
        health=health,
        transport=transport,
        cache=cache,
        negcache=negcache,
        discovery=discovery,
        repository=repository,
        clock=clock,
    )
    from .auth_tools import SignInFlow
    from .cars.api import CarsApi

    rt.cars = CarsApi(rt)
    rt.sign_in = SignInFlow(rt)
    return rt
