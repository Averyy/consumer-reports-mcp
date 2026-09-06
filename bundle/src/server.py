"""Entry-point shim for the Claude Desktop bundle (manifest.json → `server.entry_point`).

Desktop starts the console script named in `mcp_config`; this file exists because the `uv`
server type expects an `entry_point`, and so that `uv run src/server.py` serves too. It is the
whole of the bundle's own code — everything else is the installed `consumer-reports-mcp`.
"""

from consumer_reports_mcp.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
