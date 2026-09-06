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
* The server's own source is VENDORED into `vendor/consumer-reports-mcp/` because PyPI
  publication has not happened; `bundle/pyproject.toml` points uv at that path and its comment
  names the one-line switch once it has (then `vendor()` below goes too).
* No network. This packs files; Desktop's own `uv sync` resolves dependencies at install time.
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
VENDORED_FILES = ("pyproject.toml", "README.md", "LICENSE")  # what hatchling needs, plus src/
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


def vendor(root: Path, staging: Path, name: str) -> Path:
    """Copy the server's source tree into the bundle (the pre-publication path)."""
    target = staging / "vendor" / name
    target.mkdir(parents=True)
    for filename in VENDORED_FILES:
        shutil.copy2(root / filename, target / filename)
    _copytree(root / "src", target / "src")
    return target


def stage(root: Path, out_dir: Path) -> tuple[Path, str, str]:
    name, version = project_meta(root)
    staging = out_dir / "mcpb"
    if staging.exists():
        shutil.rmtree(staging)
    _copytree(root / "bundle", staging)
    # the wrapper project carries the same version, so the artifact is self-consistent
    wrapper = staging / "pyproject.toml"
    wrapper.write_text(
        re.sub(
            r'^version = "0"',
            f'version = "{version}"',
            wrapper.read_text(encoding="utf-8"),
            count=1,
            flags=re.M,
        ),
        encoding="utf-8",
    )
    vendor(root, staging, name)
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
