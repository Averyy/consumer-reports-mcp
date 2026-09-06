"""Shared by every module under tests/live/, all opt-in with CR_LIVE=1 and all ANONYMOUS.

One `Transport` per module, built exactly as the smoke always built it: `Settings(env={},
home=tmp)` so nothing is read from the real home, and a `CredentialStore` with `env={}` and no
`session.json` — NO cookie, ever. A member score can therefore never reach a live test.
"""

from __future__ import annotations

import pytest

from consumer_reports_mcp.config import Settings
from consumer_reports_mcp.credentials import CredentialStore, SessionState
from consumer_reports_mcp.transport import Transport


@pytest.fixture(scope="module")
def transport(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("live")
    settings = Settings(env={}, home=tmp)
    store = CredentialStore(tmp / "session.json", env={})  # NO cookie, ever
    return Transport(settings, store, SessionState(False))
