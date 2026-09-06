"""Refuse to publish an artifact carrying a capture, a credential or a member score.

Run against `dist/` after `uv build`. The release workflow runs it between the build and the
upload, because PyPI files are immutable: a wheel that ships something it should not can be
yanked but never replaced, and by then it has been mirrored.

Two checks, because they fail differently. PATHS catches the accident of a build backend
picking up a directory that is only ignored by convention — `scratch/`, `notes/`, a cached
`session.json`. CONTENT catches the accident of a real capture being committed as a fixture:
the `hash` cookie is a 36-character UUID-ish token, the cars api-key is 40 characters, and
every fixture in this repository is supposed to carry the synthetic one. Neither check knows
what a Consumer Reports score looks like — `tests/test_fixtures.py` owns that, on the source
tree, where it can be specific.
"""

from __future__ import annotations

import re
import sys
import tarfile
import zipfile
from pathlib import Path

# Paths that must never be inside a distribution, matched anywhere in the entry name.
FORBIDDEN_PATHS = (
    "session.json",
    "scratch/",
    "notes/",
    ".playwright-mcp",
    ".codex-dobby",
    ".claude/",
    ".har",
    ".sqlite",
    ".db",
)

# A `hash` cookie is 36 chars of hex-and-dashes; a bare UUID is the same shape. The synthetic
# api-key every fixture uses is allowed through by name so a real 40-char key still trips.
UUIDISH = re.compile(
    rb"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
COOKIE_ASSIGN = re.compile(rb"\bhash=[A-Za-z0-9_-]{20,}")
SYNTHETIC_KEY = b"SYNTHETICKEY"
API_KEY_FIELD = re.compile(rb"DRS_CARS_API_API_KEY\"?\s*:\s*\"([A-Za-z0-9]{20,})\"")

# Text-ish members are the only ones worth scanning; a font or an image cannot carry a cookie
# in a form this would catch, and decoding them wastes the run.
SCANNABLE = (".py", ".json", ".html", ".xml", ".md", ".toml", ".txt", ".cfg", ".ini")


def members(path: Path) -> list[tuple[str, bytes]]:
    """Every entry in a wheel or sdist as `(name, content)`; content empty when not scannable."""
    out: list[tuple[str, bytes]] = []
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                body = z.read(name) if name.endswith(SCANNABLE) else b""
                out.append((name, body))
    else:
        with tarfile.open(path) as t:
            for info in t.getmembers():
                if not info.isfile():
                    continue
                body = b""
                if info.name.endswith(SCANNABLE):
                    fh = t.extractfile(info)
                    body = fh.read() if fh else b""
                out.append((info.name, body))
    return out


def failures(name: str, body: bytes) -> list[str]:
    bad = [f"{name}: forbidden path segment {seg!r}" for seg in FORBIDDEN_PATHS if seg in name]
    if UUIDISH.search(body):
        bad.append(f"{name}: contains a UUID-shaped token (a `hash` cookie is this shape)")
    if COOKIE_ASSIGN.search(body):
        bad.append(f"{name}: contains a `hash=` cookie assignment")
    for found in API_KEY_FIELD.findall(body):
        if not found.startswith(SYNTHETIC_KEY):
            bad.append(f"{name}: carries a cars api-key that is not the synthetic one")
    return bad


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path("dist")
    artifacts = sorted([*root.glob("*.whl"), *root.glob("*.tar.gz")])
    if not artifacts:
        print(f"no artifacts in {root}/ — run `uv build --no-sources` first", file=sys.stderr)
        return 2

    bad: list[str] = []
    for art in artifacts:
        entries = members(art)
        for name, body in entries:
            bad += failures(name, body)
        print(f"{art.name}: {len(entries)} entries checked")

    if bad:
        print("\nREFUSING TO PUBLISH:", file=sys.stderr)
        for line in bad:
            print(f"  {line}", file=sys.stderr)
        return 1
    print("clean: no captures, cookies or non-synthetic keys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
