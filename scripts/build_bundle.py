#!/usr/bin/env python3
"""Build the Claude Desktop bundle (`.mcpb`) — SPEC §9 *Claude Desktop bundle*.

    uv run scripts/build_bundle.py                # → dist/consumer-reports-mcp-<version>.mcpb
    uv run scripts/build_bundle.py --out DIR      # somewhere else

What it does, and why each step exists:

* `manifest.json` is GENERATED. The template in `bundle/manifest.json` carries no `version` and
  no `tools`; both are filled in here from `pyproject.toml` and `server.DESCRIPTIONS`, so the
  number Desktop shows and the tool list it advertises cannot drift from the code — the
  `__init__.py` version-drift bug, not reintroduced. A template that does carry a version is
  refused outright.
* `bundle/pyproject.toml` depends on `consumer-reports-mcp[browser]==0`. The `==0` is a
  placeholder exactly like the wrapper's `version = "0"`: `stamp_wrapper` writes the root
  version over both — and refuses a wrapper missing either — so the bundle installs the release
  it was built for and the pin cannot drift. Nothing is vendored.
* No network. This packs files; Desktop's own `uv sync` resolves the pin from PyPI at install
  time. So a bundle built BEFORE its release is on PyPI packs fine here and fails on the user's
  machine: build from the released tag, after the publish.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {"__pycache__", ".venv", ".pytest_cache", ".ruff_cache"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo")


def project_meta(root: Path = ROOT) -> tuple[str, str]:
    """(name, version) from the ROOT pyproject.toml — the single source of truth."""
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["name"], data["project"]["version"]


def tool_list(root: Path = ROOT) -> list[dict[str, str]]:
    """The manifest's `tools` from the server's own description table, never retyped."""
    src = str(root / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from consumer_reports_mcp.server import DESCRIPTIONS

    return [{"name": name, "description": text} for name, text in DESCRIPTIONS.items()]


def render_manifest(template: dict, version: str, tools: list[dict[str, str]]) -> dict:
    if "version" in template:
        raise SystemExit(
            "bundle/manifest.json must not carry a version: it is generated from pyproject.toml"
        )
    out = dict(template)
    out["version"] = version
    out["tools"] = tools
    return out


def _copytree(src: Path, dst: Path) -> None:
    def ignore(_dir: str, names: list[str]) -> set[str]:
        return {n for n in names if n in EXCLUDE_DIRS or n.endswith(EXCLUDE_SUFFIXES)}

    shutil.copytree(src, dst, ignore=ignore)


def stamp_wrapper(text: str, version: str) -> str:
    """The wrapper's `version = "0"` and its `==0` dependency pin, both stamped with the root
    version. A wrapper missing either placeholder is refused: unstamped, the bundle would carry
    a wrong version or an unpinned dependency, and nothing downstream would notice."""
    for pattern, replacement in (
        (r'^version = "0"$', f'version = "{version}"'),
        (r'consumer-reports-mcp\[browser\]==0"', f'consumer-reports-mcp[browser]=={version}"'),
    ):
        text, n = re.subn(pattern, replacement, text, count=1, flags=re.M)
        if n != 1:
            raise SystemExit(f"bundle/pyproject.toml lacks the placeholder {pattern!r} to stamp")
    return text


def stage(root: Path, out_dir: Path) -> tuple[Path, str, str]:
    name, version = project_meta(root)
    staging = out_dir / "mcpb"
    if staging.exists():
        shutil.rmtree(staging)
    _copytree(root / "bundle", staging)
    # the wrapper carries the same version and pins that release: the artifact is self-consistent
    wrapper = staging / "pyproject.toml"
    wrapper.write_text(
        stamp_wrapper(wrapper.read_text(encoding="utf-8"), version), encoding="utf-8"
    )
    template = json.loads((root / "bundle" / "manifest.json").read_text(encoding="utf-8"))
    manifest = render_manifest(template, version, tool_list(root))
    (staging / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return staging, name, version


def pack(staging: Path, out_path: Path) -> Path:
    """A `.mcpb` is a zip with `manifest.json` at its root."""
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(staging).as_posix())
    return out_path


def build(root: Path = ROOT, out_dir: Path | None = None) -> Path:
    out_dir = out_dir or root / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    staging, name, version = stage(root, out_dir)
    return pack(staging, out_dir / f"{name}-{version}.mcpb")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Claude Desktop .mcpb bundle.")
    parser.add_argument("--out", type=Path, default=None, help="output directory (default: dist/)")
    args = parser.parse_args(argv)
    print(build(out_dir=args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
