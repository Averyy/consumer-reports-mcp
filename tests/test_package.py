import tomllib
from pathlib import Path

import consumer_reports_mcp

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_version_matches_pyproject():
    """Pinning a literal here is what let the advertised version drift: `__init__` said 0.1.0
    while pyproject said 0.1.6, and this test held the drift in place instead of catching it.
    The server hands `__version__` to MCP clients, so the two must not diverge."""
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    installed = consumer_reports_mcp.__version__
    if installed == "0.0.0+unknown":  # source tree, never installed
        return
    assert installed == declared, (
        f"installed metadata says {installed}, pyproject says {declared} — "
        "reinstall (`uv sync`) or the server will advertise the stale one"
    )
