"""The Claude Desktop bundle (SPEC §9 *Claude Desktop bundle*): a generated manifest whose
version comes from pyproject.toml, vendored source, no secrets, and no network."""

from __future__ import annotations

import importlib.util
import json
import tomllib
import zipfile
from pathlib import Path

import pytest

from consumer_reports_mcp.server import DESCRIPTIONS

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "bundle"


def _build_module():
    spec = importlib.util.spec_from_file_location(
        "build_bundle", ROOT / "scripts" / "build_bundle.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _declared_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


def test_template_carries_no_version_and_declares_the_uv_shape():
    """A version typed into the template is exactly the drift `__init__.py` just stopped."""
    template = json.loads((BUNDLE / "manifest.json").read_text(encoding="utf-8"))
    assert "version" not in template and "tools" not in template
    assert template["manifest_version"] == "0.4"
    server = template["server"]
    assert server["type"] == "uv" and server["entry_point"] == "src/server.py"
    assert server["mcp_config"]["command"] == "uv"
    assert server["mcp_config"]["args"] == [
        "run",
        "--directory",
        "${__dirname}",
        "consumer-reports-mcp",
    ]
    assert server["mcp_config"]["env"] == {"CR_SESSION_COOKIE": "${user_config.session_cookie}"}
    field = template["user_config"]["session_cookie"]
    assert field["type"] == "string" and field["sensitive"] is True and field["required"] is False
    assert "overrides" in field["description"]  # the env-override rule is stated where it bites
    # Desktop's Linux beta shipped 2026-06-30 (Ubuntu/Debian); the `uv` server type is
    # cross-platform and nothing in the bundle names a platform, so all three are declared
    assert template["compatibility"]["platforms"] == ["darwin", "win32", "linux"]
    assert template["name"] == "consumer-reports-mcp" and template["author"]["name"]


def test_bundle_pyproject_puts_the_browser_extra_on_the_dependency():
    """`uv sync` skips the bundle project's own extras but honours extras in a dependency spec —
    so the extra must be spelled on the dependency for Desktop users to get Playwright."""
    text = (BUNDLE / "pyproject.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    assert data["project"]["dependencies"] == ["consumer-reports-mcp[browser]"]
    assert "optional-dependencies" not in data["project"]
    assert data["tool"]["uv"]["sources"]["consumer-reports-mcp"] == {
        "path": "vendor/consumer-reports-mcp"
    }
    assert "build-system" not in data  # a virtual project: uv installs its dependencies only
    assert "AFTER PUBLICATION" in text and "==<version>" in text  # the documented switch
    assert data["project"]["version"] == "0"  # the wrapper's own number is a placeholder…


def test_build_generates_the_version_and_packs_a_self_contained_bundle(tmp_path):
    mod = _build_module()
    version = _declared_version()
    out = mod.build(out_dir=tmp_path)
    assert out == tmp_path / f"consumer-reports-mcp-{version}.mcpb" and out.exists()
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))
        wrapper = tomllib.loads(zf.read("pyproject.toml").decode("utf-8"))
        vendored = tomllib.loads(
            zf.read("vendor/consumer-reports-mcp/pyproject.toml").decode("utf-8")
        )
    assert manifest["version"] == version and manifest["manifest_version"] == "0.4"
    assert sorted(t["name"] for t in manifest["tools"]) == sorted(DESCRIPTIONS)
    assert {t["name"]: t["description"] for t in manifest["tools"]} == DESCRIPTIONS
    assert wrapper["project"]["version"] == version  # …stamped with the real one at build time
    assert vendored["project"]["version"] == version
    assert {
        "manifest.json",
        "pyproject.toml",
        "README.md",
        ".mcpbignore",
        "src/server.py",
        "vendor/consumer-reports-mcp/pyproject.toml",
        "vendor/consumer-reports-mcp/README.md",
        "vendor/consumer-reports-mcp/LICENSE",
        "vendor/consumer-reports-mcp/src/consumer_reports_mcp/server.py",
        "vendor/consumer-reports-mcp/src/consumer_reports_mcp/auth_tools.py",
        "vendor/consumer-reports-mcp/src/consumer_reports_mcp/cars/tools.py",
    } <= names
    # nothing that is not the server ships: no caches, no tests, no captures, no credentials
    for forbidden in ("__pycache__", ".pyc", ".venv", "tests/", "scratch/", "session.json", ".db"):
        assert not any(forbidden in n for n in names), forbidden
    assert not any(n.startswith("vendor/consumer-reports-mcp/scripts") for n in names)
    # building twice is idempotent (the staging dir is rebuilt, not appended to)
    again = mod.build(out_dir=tmp_path)
    with zipfile.ZipFile(again) as zf:
        assert set(zf.namelist()) == names


def test_a_template_with_a_version_is_refused():
    mod = _build_module()
    with pytest.raises(SystemExit, match="must not carry a version"):
        mod.render_manifest({"version": "9.9.9"}, "0.0.1", [])
    rendered = mod.render_manifest({"name": "x"}, "0.0.1", [{"name": "t", "description": "d"}])
    assert rendered["version"] == "0.0.1" and rendered["tools"][0]["name"] == "t"


def test_shim_serves_through_the_cli_entry_point():
    text = (BUNDLE / "src" / "server.py").read_text(encoding="utf-8")
    compile(text, "server.py", "exec")
    assert "from consumer_reports_mcp.cli import main" in text
