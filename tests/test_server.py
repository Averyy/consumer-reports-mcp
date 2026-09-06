"""P9.2 / P10.5 — the process boundary: eleven tools, spec descriptions, typed output schemas."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from consumer_reports_mcp import server as S
from tests.conftest import RuntimeHarness

ROOT = Path(__file__).resolve().parents[1]
NINE = [
    "cr_ratings",
    "cr_product",
    "cr_filters",
    "cr_reliability",
    "cr_categories",
    "cr_search",
    "cr_car_search",
    "cr_cars",
    "cr_car",
]
AUTH = ["cr_sign_in", "cr_auth_status"]
ALL_TOOLS = NINE + AUTH


def _spec_descriptions() -> dict[str, str]:
    text = (ROOT / "SPEC.md").read_text(encoding="utf-8")
    out = {}
    for name in ALL_TOOLS:
        m = re.search(rf"^\| `{name}` \| (.+?) \|$", text, re.M)
        assert m, name
        out[name] = m.group(1).replace("**", "").strip()
    return out


def test_descriptions_match_spec_table_verbatim():
    spec = _spec_descriptions()
    for name in ALL_TOOLS:
        assert S.DESCRIPTIONS[name] == spec[name], name


async def test_products_tools_with_spec_descriptions_and_output_schema(tmp_path):
    h = RuntimeHarness(tmp_path)
    server = S.build_server(h.rt)
    tools = {t.name: t for t in await server.list_tools()}
    assert set(tools) >= set(NINE[:6])
    spec = _spec_descriptions()
    for name in NINE[:6]:
        assert tools[name].description == spec[name]
        assert tools[name].output_schema and tools[name].output_schema.get("properties")
        assert tools[name].annotations.read_only_hint is True
    props = tools["cr_ratings"].output_schema["properties"]
    # nullable (`anyOf`) because a no-row error carries `auth_state: null` (SPEC §7); the enum
    # is still in the schema, which is the structural guard
    assert props["auth_state"]["anyOf"][0]["enum"] == ["anonymous", "member", "session_expired"]
    assert {"type": "null"} in props["auth_state"]["anyOf"]
    assert tools["cr_ratings"].input_schema["properties"]["sort"]["default"] is None
    assert tools["cr_ratings"].input_schema["properties"]["detail"]["default"] == "standard"
    assert "session" not in tools["cr_reliability"].output_schema["properties"]


async def test_registered_tools_match_the_spec(tmp_path):
    """Nine data tools plus the two auth tools (SPEC §6 *cr_sign_in*). Asserts the NAME SET,
    not a count, so adding a tool changes the list rather than the test's name."""
    h = RuntimeHarness(tmp_path)
    server = S.build_server(h.rt)
    tools = {t.name: t for t in await server.list_tools()}
    assert sorted(tools) == sorted(ALL_TOOLS)
    car = tools["cr_car"].output_schema["properties"]
    assert "error" in car and "scores_available" in car and "auth_state" not in car
    assert tools["cr_cars"].input_schema["properties"]["state"]["default"] is None
    assert tools["cr_cars"].input_schema["properties"]["limit"]["default"] is None
    assert tools["cr_sign_in"].input_schema["properties"]["force"]["default"] is False
    assert tools["cr_auth_status"].input_schema["properties"]["wait_s"]["default"] == 0


async def test_sign_in_is_not_read_only_so_clients_prompt(tmp_path):
    """The one tool that opens a window and writes a file must not wear the shared read-only
    annotations: both clients decide whether to prompt from exactly these hints."""
    h = RuntimeHarness(tmp_path)
    server = S.build_server(h.rt)
    tools = {t.name: t for t in await server.list_tools()}
    sign = tools["cr_sign_in"].annotations
    assert sign.read_only_hint is False and sign.idempotent_hint is False
    assert sign.open_world_hint is True
    assert sign is not S.ANNOTATIONS and S.SIGN_IN_ANNOTATIONS is not S.ANNOTATIONS
    status = tools["cr_auth_status"].annotations
    assert status.read_only_hint is True and status.idempotent_hint is True
    for name in NINE:
        assert tools[name].annotations.read_only_hint is True
    assert "explicitly asks" in tools["cr_sign_in"].description
    assert "never sees the password" in tools["cr_sign_in"].description
    # the project's outer shape (SPEC §7), with the status object under `data`
    sign_schema = tools["cr_sign_in"].output_schema
    assert sorted(sign_schema["properties"]) == ["data", "error", "session", "warnings"]
    status_enum = _nested_props(sign_schema, "SignInData")["status"]["enum"]
    assert status_enum == ["waiting", "verifying", "in_progress", "refused", "failed"]
    st_schema = tools["cr_auth_status"].output_schema
    assert sorted(st_schema["properties"]) == ["data", "error", "session", "warnings"]
    phase_enum = _nested_props(st_schema, "AuthStatusData")["sign_in"]["enum"]
    assert phase_enum == [
        "idle",
        "verifying",
        "waiting",
        "validating",
        "active",
        "refused",
        "failed",
    ]
    # no field at ANY depth could hold a cookie value — by field name, not by description text
    for schema in (sign_schema, st_schema):
        names = _all_property_names(schema)
        assert names and not any("cookie" in n or "hash" in n for n in names), names


def _nested_props(schema: dict, model: str) -> dict:
    return schema["$defs"][model]["properties"]


def _all_property_names(node) -> set[str]:
    """Every key of every `properties` object in a JSON schema, at any depth."""
    names: set[str] = set()
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            names |= set(props)
        for v in node.values():
            names |= _all_property_names(v)
    elif isinstance(node, list):
        for v in node:
            names |= _all_property_names(v)
    return names


async def test_sign_in_and_status_round_trip_through_the_server(tmp_path):
    from consumer_reports_mcp.auth_tools import SignInFlow

    h = RuntimeHarness(tmp_path)
    gate = __import__("asyncio").Event()

    async def capture(*, timeout_s, on_launch):
        on_launch("chrome")
        await gate.wait()
        return "q" * 36

    async def validate(settings, cookies):
        return "member"

    h.rt.sign_in = SignInFlow(h.rt, capture=capture, validate=validate, launch_wait_s=0.2)
    server = S.build_server(h.rt)
    t0 = time.monotonic()
    res = await server.call_tool("cr_sign_in", {})
    sc = getattr(res, "structured_content", None) or res.structuredContent
    assert time.monotonic() - t0 < 1.0 and not getattr(res, "is_error", False)
    assert sorted(sc) == ["data", "error", "session", "warnings"] and sc["error"] is None
    assert sc["data"]["status"] == "waiting" and sc["data"]["browser"] == "chrome"
    assert "q" * 36 not in json.dumps(sc)
    gate.set()
    for _ in range(10):
        res = await server.call_tool("cr_auth_status", {"wait_s": 1})
        sc = getattr(res, "structured_content", None) or res.structuredContent
        if sc["data"]["sign_in"] == "active":
            break
    assert sorted(sc) == ["data", "error", "session", "warnings"]
    assert sc["data"]["sign_in"] == "active" and sc["session"] == "active"
    assert sc["data"]["source"] == "file" and sc["warnings"] == []
    assert "q" * 36 not in json.dumps(sc)


async def test_tool_call_returns_structured_content(tmp_path):
    h = RuntimeHarness(tmp_path)
    server = S.build_server(h.rt)
    result = await server.call_tool("cr_categories", {})
    sc = (
        result.structured_content
        if hasattr(result, "structured_content")
        else result.structuredContent
    )
    assert sc["session"] == "none" and sc["data"]["total"] == 5
    assert sc["error"] is None
    from tests.conftest import load_fixture

    h.route_page(load_fixture("category_c37162.json"))
    # an integer id and integer brand ids must pass the SDK boundary (SPEC §7 tool contracts)
    as_int = await server.call_tool("cr_ratings", {"category": 37162, "brands": [900001]})
    sc3 = getattr(as_int, "structured_content", None) or as_int.structuredContent
    assert not getattr(as_int, "is_error", False)
    assert sc3["error"] is None and sc3["data"]["group_mode"] == "nested"
    assert sum(g["total"] for g in sc3["data"]["groups"]) == 3  # Brand A's three products
    fam = await server.call_tool("cr_categories", {"family": 28978})
    assert not getattr(fam, "is_error", False)
    err = await server.call_tool("cr_ratings", {"category": "c99999"})
    sc2 = err.structured_content if hasattr(err, "structured_content") else err.structuredContent
    assert sc2["error"]["code"] == "unknown_category" and sc2["data"] is None
    assert not getattr(err, "is_error", False)  # structured error VALUE, not a protocol error


# What a spawned Python needs from the host on Windows and nothing else: `SystemRoot` is where
# Winsock finds its service providers, and without it `import asyncio` dies in `_overlapped`
# with `WinError 10106` ("the requested service provider could not be loaded or initialized")
# — measured on the windows-latest CI runner, 2026-09-06, when this test handed the server only
# `PATH`, `HOME` and the `CR_*` variables. None of these names a user directory, so the
# isolation below (the server's home is `tmp_path`, never the real one) is untouched; on POSIX
# none of them exists and nothing is added.
WINDOWS_SYSTEM_VARS = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT")


def test_stdio_initialize_and_tools_list(tmp_path):
    env = {  # a minimal environment: no ambient CR_* variable can perturb the handshake
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "CR_CACHE_DIR": str(tmp_path / "cache"),
        "CR_OFFLINE": "1",
        "PYTHONUNBUFFERED": "1",
    }
    env.update({k: os.environ[k] for k in WINDOWS_SYSTEM_VARS if k in os.environ})
    exe = ROOT / ".venv" / "bin" / "consumer-reports-mcp"
    if not exe.exists():
        exe = Path(sys.executable).with_name("consumer-reports-mcp")
    frames = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.Popen(
        [str(exe)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    replies: dict[int, dict] = {}
    try:
        assert proc.stdin and proc.stdout
        for frame in frames:  # keep stdin OPEN until both replies are in — EOF ends the server
            proc.stdin.write(json.dumps(frame) + "\n")
            proc.stdin.flush()
        deadline = time.monotonic() + 60
        while len(replies) < 2 and time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if not line.strip():
                continue
            msg = json.loads(line)  # stdout carries ONLY JSON-RPC frames
            if "id" in msg:
                replies[msg["id"]] = msg
    finally:
        proc.stdin.close()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        stderr = proc.stderr.read() if proc.stderr else ""
        for pipe in (proc.stdout, proc.stderr):
            if pipe:
                pipe.close()
    assert set(replies) == {1, 2}, stderr[-2000:]
    assert replies[1]["result"]["serverInfo"]["name"] == "consumer-reports"
    names = sorted(t["name"] for t in replies[2]["result"]["tools"])
    assert names == sorted(ALL_TOOLS)
    assert "hash=" not in stderr


async def test_lifespan_prunes_the_cache_at_startup(tmp_path, c37162):
    """`Cache.prune()` had no runtime caller, so the append-only cache had no bound at all."""
    from datetime import timedelta

    import pytest

    from tests.conftest import fixture_envelope

    h = RuntimeHarness(tmp_path)
    env = fixture_envelope(c37162)
    old = h.rt.cache.write_category(
        env, tier="anonymous", scored=False, fetched_at=h.now - timedelta(days=200)
    )
    new = h.rt.cache.write_category(
        env, tier="anonymous", scored=False, fetched_at=h.now - timedelta(days=1)
    )
    server = S.build_server(h.rt)
    async with server.settings.lifespan(server):
        pass
    assert h.requests == []  # discovery is fresh: startup made no request
    with pytest.raises(KeyError):
        h.rt.cache.load_envelope(old)
    assert h.rt.cache.load_envelope(new)
