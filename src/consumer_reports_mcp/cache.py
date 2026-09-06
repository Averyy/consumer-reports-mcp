"""BOUNDARY 2: the only module that imports sqlite3 (PLAN P3, SPEC §8).

`category_raw` is append-only on `(category_id, auth_tier, fetched_at)`; never-downgrade is a
QUERY ("newest scored qualifying row"), not update logic. One connection per call, WAL mode.
Rows are keyed on `args.cid` — never the requested id, never the final URL.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import OrderedDict
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .ingest import _int, category_id_of, display_name_of, product_index_rows, products_of

USER_VERSION = 2  # 2: category_slug_alias — a published slug must keep resolving
SOURCES = ("az", "sitemap", "payload")
RANGE_KNOWN, RANGE_NONE, RANGE_NOT_FETCHED = "known", "none_published", "not_fetched"
WWW = "https://www.consumerreports.org"

SCHEMA = """
CREATE TABLE IF NOT EXISTS category_raw (
    rowid INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL,
    auth_tier TEXT NOT NULL CHECK (auth_tier IN ('anonymous', 'member')),
    fetched_at TEXT NOT NULL,
    scored INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    final_url TEXT,
    requested_id INTEGER
);
CREATE INDEX IF NOT EXISTS category_raw_cat ON category_raw (category_id, fetched_at);
CREATE TABLE IF NOT EXISTS product_index (
    product_id INTEGER NOT NULL,
    category_id INTEGER NOT NULL,
    brand_name TEXT,
    model_name TEXT,
    PRIMARY KEY (product_id, category_id)
);
CREATE INDEX IF NOT EXISTS product_index_names ON product_index (brand_name, model_name);
CREATE TABLE IF NOT EXISTS category_index (
    category_id INTEGER PRIMARY KEY,
    slug TEXT,
    display_name TEXT,
    franchise TEXT,
    canonical_url TEXT,
    source TEXT NOT NULL CHECK (source IN ('az', 'sitemap', 'payload')),
    family_id INTEGER,
    family_name TEXT,
    score_min REAL,
    score_max REAL,
    rated_count INTEGER,
    has_reliability_data INTEGER,
    groups_json TEXT,
    reliability_url TEXT,
    score_range_status TEXT NOT NULL DEFAULT 'not_fetched'
        CHECK (score_range_status IN ('known', 'none_published', 'not_fetched')),
    dead_at TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS category_index_slug ON category_index (slug);
CREATE TABLE IF NOT EXISTS category_alias (
    alias_id INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL
);
-- Slugs a category has been published under before. Discovery prefers the sitemap's shorter
-- canonical URL, which changes the slug derived from it; a slug the server has already handed
-- out must keep resolving, or `cr_ratings("cruise-lines")` starts answering "not_in_index".
CREATE TABLE IF NOT EXISTS category_slug_alias (
    slug TEXT PRIMARY KEY,
    category_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_runs (
    source TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reliability_raw (
    rowid INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    final_url TEXT
);
CREATE INDEX IF NOT EXISTS reliability_raw_cat ON reliability_raw (category_id, fetched_at);
CREATE TABLE IF NOT EXISTS car_index (
    model_year_id INTEGER PRIMARY KEY,
    make_id INTEGER,
    make TEXT,
    slug_make TEXT,
    model_id INTEGER,
    model TEXT,
    slug_model TEXT,
    year INTEGER,
    states_json TEXT NOT NULL,
    car_types_json TEXT NOT NULL,
    primary_car_type_id INTEGER
);
CREATE TABLE IF NOT EXISTS car_taxonomy (
    rowid INTEGER PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS car_raw (
    rowid INTEGER PRIMARY KEY,
    model_year_id INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS car_raw_id ON car_raw (model_year_id, fetched_at);
"""

# The category-id token grammar. It lives HERE and nowhere else: `resolve_category` parses a
# token once and carries the id on the resolution it returns (an `unknown` too), so no caller
# ever re-parses. A repository re-parse (`isdigit()` then `int()`) shipped once: `'²'` and `'①'`
# are `isdigit()` and not `int()`, a 20-digit id overflows SQLite's bind, and a 5,000-digit one
# trips Python's conversion limit — all raised out of the tool instead of `unknown_category`.
# `[0-9]`, not `\d`: `\d` matches Unicode digits, and `int()` rejects them.
_CID = re.compile(r"^c?([0-9]{4,7})$", re.I)

# SQLite INTEGER is a signed 64-bit; sqlite3 raises OverflowError binding anything past it
SQLITE_INT_MAX = 2**63 - 1
_ROW_ID = re.compile(r"^[0-9]{1,19}$")


def row_id(token: Any) -> int | None:
    """The positive integer key a product or model-year token names, or None: a bool, a
    non-integer, non-ASCII digits and an integer SQLite cannot bind are all "not an id" — CR's
    ids are 5–8 digits, so nothing past `SQLITE_INT_MAX` can be one, and binding it raised
    OverflowError through `cr_product`/`cr_car` instead of `unknown_product`/`unknown_car`. The
    cache owns this grammar as it owns `_CID`."""
    if isinstance(token, bool):
        return None
    if isinstance(token, int):
        value = token
    elif isinstance(token, str) and _ROW_ID.match(token.strip()):
        value = int(token.strip())
    else:
        return None
    return value if 0 < value <= SQLITE_INT_MAX else None


# --------------------------------------------------------------------------- time helpers


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


def is_stale(fetched_at: str, ttl_days: int, now: datetime) -> bool:
    return from_iso(fetched_at) + timedelta(days=ttl_days) < now


# --------------------------------------------------------------------------- results


@dataclass
class Selection:
    """The row that answered, with the two separate facts SPEC §8 keeps apart.

    `stale` is about the SERVED row — past the TTL and nothing else. `checked_at` is when the
    category was last fetched at a tier this caller can use, whichever row that produced: under
    never-downgrade the served row can be a scored row retained for months, and "refetch?" must
    be decided from the last look, or a lapsed membership refetches on every call forever."""

    rowid: int
    category_id: int
    tier: str
    fetched_at: str
    scored: bool
    stale: bool
    superseded_at: str | None
    final_url: str | None
    schema_version: int
    qualified: bool = True
    checked_at: str | None = None  # the newest row this caller could have been served


@dataclass
class Resolution:
    kind: str  # ok | ambiguous | unknown
    # the resolved category; on `unknown` the id the token parsed to under `_CID` (None for a
    # slug), so a caller that wants to look it up elsewhere — the display-group owner — never
    # re-parses the token
    category_id: int | None = None
    candidates: list[dict] = field(default_factory=list)
    row: dict | None = None
    via_alias: bool = False

    @property
    def dead(self) -> bool:
        return bool(self.row and self.row.get("dead_at"))


@dataclass
class ReliabilitySelection:
    rowid: int
    category_id: int
    fetched_at: str
    stale: bool
    final_url: str | None


@dataclass
class CarSelection:
    rowid: int
    model_year_id: int
    fetched_at: str
    stale: bool
    schema_version: int


def _franchise_of(url: str | None) -> str | None:
    if not url:
        return None
    path = url.split("//", 1)[-1]
    path = path.split("/", 1)[1] if "/" in path else ""
    parts = [p for p in path.split("/") if p]
    return parts[0] if parts else None


def _absolute(url: str) -> str:
    return url if url.startswith("http") else WWW + url


def _slug_of(url: str | None) -> str | None:
    if not url:
        return None
    parts = [p for p in url.split("?")[0].split("/") if p]
    if len(parts) >= 2 and re.fullmatch(r"c\d{4,7}", parts[-1]):
        return parts[-2]
    return None


class Cache:
    """SQLite-backed store. Every public method opens and closes its own connection.

    Envelopes returned by `load_envelope` are shared through the LRU and must be treated as
    READ-ONLY by callers; every consumer builds new dicts."""

    def __init__(self, db_path: Path, *, lru_size: int = 8) -> None:
        self.db_path = Path(db_path)
        self._lru: OrderedDict[int, dict] = OrderedDict()
        self._lru_size = lru_size
        self.ensure_schema()

    # --- connection ------------------------------------------------------------
    @contextmanager
    def _connect(self):
        """One connection per call, autocommit, WAL, a 30 s busy handler.

        Every multi-statement transaction below opens with `BEGIN IMMEDIATE`, never a plain
        `BEGIN`: a deferred transaction that reads first takes a snapshot, and when another
        connection has committed since, its first write fails with SQLITE_BUSY_SNAPSHOT at
        once — the busy handler is NOT consulted, because waiting cannot make that snapshot
        current. Two processes on one `cr.db` (a second MCP host, `consumer-reports-mcp auth`
        beside the server) made `upsert_category_index` and `prune` raise "database is locked"
        after 0.000 s despite `busy_timeout=30000`. IMMEDIATE takes the write lock up front,
        where the busy handler does wait."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version < USER_VERSION:
                conn.executescript(SCHEMA)  # runs in its own implicit transaction
                conn.execute(f"PRAGMA user_version={USER_VERSION}")

    def user_version(self) -> int:
        with self._connect() as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    # --- category_raw: append ------------------------------------------------------
    def write_category(
        self,
        envelope: dict,
        *,
        tier: str,
        scored: bool,
        fetched_at: datetime,
        ttl_days: int | None = None,
    ) -> int:
        """Append a row keyed on `args.cid`, project the product index, enrich the family.

        With `ttl_days`, a payload byte-identical to the newest row at the same tier that is
        still inside the TTL is NOT appended — that row's id is returned instead. Only inside
        the TTL: a row is also the record of a check, and past it the same bytes must land as
        a new row or the served selection stays stale and refetches on every call."""
        if tier not in ("anonymous", "member"):
            raise ValueError(f"bad tier {tier!r}")
        cid = category_id_of(envelope)
        if cid is None:
            raise ValueError("envelope carries no args.cid")
        stamp = to_iso(fetched_at)
        payload = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
        with self._connect() as conn:
            if ttl_days is not None:
                newest = conn.execute(
                    "SELECT rowid, fetched_at, payload_json FROM category_raw "
                    "WHERE category_id=? AND auth_tier=? ORDER BY fetched_at DESC, rowid DESC "
                    "LIMIT 1",
                    (cid, tier),
                ).fetchone()
                if (
                    newest is not None
                    and not is_stale(newest["fetched_at"], ttl_days, fetched_at)
                    and newest["payload_json"] == payload
                ):
                    return newest["rowid"]
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "INSERT INTO category_raw (category_id, auth_tier, fetched_at, scored, "
                "schema_version, payload_json, final_url, requested_id) VALUES (?,?,?,?,?,?,?,?)",
                (
                    cid,
                    tier,
                    stamp,
                    1 if scored else 0,
                    int(envelope.get("schema_version") or 1),
                    payload,
                    envelope.get("final_url"),
                    envelope.get("requested_id"),
                ),
            )
            rowid = cur.lastrowid
            conn.executemany(
                "INSERT INTO product_index (product_id, category_id, brand_name, model_name) "
                "VALUES (?,?,?,?) ON CONFLICT (product_id, category_id) DO UPDATE SET "
                "brand_name=excluded.brand_name, model_name=excluded.model_name",
                product_index_rows(envelope),
            )
            self._enrich_family(conn, envelope, cid, stamp)
            requested = envelope.get("requested_id")
            if requested is not None and int(requested) != cid:
                conn.execute(
                    "INSERT INTO category_alias (alias_id, category_id) VALUES (?, ?) "
                    "ON CONFLICT (alias_id) DO UPDATE SET category_id=excluded.category_id",
                    (int(requested), cid),
                )
            conn.execute("COMMIT")
        self._lru[rowid] = json.loads(payload)  # what a cold process would read, not the input
        self._trim_lru()
        return rowid

    def _enrich_family(
        self, conn: sqlite3.Connection, envelope: dict, cid: int, stamp: str
    ) -> None:
        args = (envelope.get("filter_instance") or {}).get("args") or {}
        paths: dict[int, str] = {}
        rel_urls: dict[int, str] = {}
        for c in args.get("cats") or []:
            if not isinstance(c, dict):
                continue
            # CR's `cats[]` mixes category ids with PRODUCT-TYPE entries whose `id` is a slug
            # (`washer-dryer-pairs` on Front-Load Washers c28739 and Electric Dryers c30562,
            # measured 2026-09-06). `int()` on one of those raised straight out of
            # `write_category` and the tool, so those two categories answered nothing at all.
            # A slug keys no category, so it is skipped — never guessed at.
            key = _int(c.get("id"))
            if key is None:
                continue
            if c.get("typeURL"):
                paths[key] = str(c["typeURL"])
            if isinstance(c.get("reliabilityURL"), str) and c["reliabilityURL"]:
                rel_urls[key] = _absolute(c["reliabilityURL"])
        if (
            isinstance(args.get("reliabilityURL"), str)
            and args["reliabilityURL"]
            and cid is not None
        ):
            rel_urls.setdefault(cid, _absolute(args["reliabilityURL"]))
        super_ = envelope.get("supercategory") or {}
        final_url = envelope.get("final_url")
        own_name = display_name_of(envelope)
        for entry in envelope.get("family") or []:
            fid = _int(entry.get("id"))
            if fid is None:
                continue
            if fid == cid:
                url = final_url.split("?")[0] if final_url else None
            else:
                url = (WWW + paths[fid]) if fid in paths else None
            slug = entry.get("slug") or _slug_of(url)
            franchise = _franchise_of(url)
            score_min, score_max = entry.get("score_min"), entry.get("score_max")
            if score_min is not None and score_max is not None:
                status = RANGE_KNOWN
            elif entry.get("score_range_block_present"):
                status = RANGE_NONE
            else:
                status = None  # leave whatever the row already says (default not_fetched)
            groups = entry.get("groups")
            groups_json = json.dumps(groups) if groups is not None else None
            name = entry.get("name") if fid != cid else (own_name or entry.get("name"))
            has_rel = entry.get("has_reliability_data")
            rel_url = rel_urls.get(fid)
            row = conn.execute(
                "SELECT category_id FROM category_index WHERE category_id=?", (fid,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO category_index (category_id, slug, display_name, franchise, "
                    "canonical_url, source, family_id, family_name, score_min, score_max, "
                    "rated_count, has_reliability_data, groups_json, reliability_url, "
                    "score_range_status, first_seen, last_seen) "
                    "VALUES (?,?,?,?,?,'payload',?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        fid,
                        slug,
                        name,
                        franchise,
                        url,
                        super_.get("id"),
                        super_.get("name"),
                        score_min if status else None,
                        score_max if status else None,
                        entry.get("rated_count"),
                        None if has_rel is None else int(bool(has_rel)),
                        groups_json,
                        rel_url,
                        status or RANGE_NOT_FETCHED,
                        stamp,
                        stamp,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE category_index SET "
                    "slug=COALESCE(?, slug), display_name=COALESCE(?, display_name), "
                    "franchise=COALESCE(?, franchise), canonical_url=COALESCE(?, canonical_url), "
                    "family_id=COALESCE(?, family_id), family_name=COALESCE(?, family_name), "
                    # the range and its status move TOGETHER, only when the page carried a block
                    "score_min=CASE WHEN ? IS NOT NULL THEN ? ELSE score_min END, "
                    "score_max=CASE WHEN ? IS NOT NULL THEN ? ELSE score_max END, "
                    "rated_count=COALESCE(?, rated_count), "
                    "has_reliability_data=COALESCE(?, has_reliability_data), "
                    "groups_json=COALESCE(?, groups_json), "
                    "reliability_url=COALESCE(?, reliability_url), "
                    "score_range_status=COALESCE(?, score_range_status), last_seen=? "
                    "WHERE category_id=?",
                    (
                        slug,
                        name,
                        franchise,
                        url,
                        super_.get("id"),
                        super_.get("name"),
                        status,
                        score_min,
                        status,
                        score_max,
                        entry.get("rated_count"),
                        None if has_rel is None else int(bool(has_rel)),
                        groups_json,
                        rel_url,
                        status,
                        stamp,
                        fid,
                    ),
                )

    # --- category_raw: select ------------------------------------------------------
    def _rows_for(self, conn: sqlite3.Connection, category_id: int) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT rowid, category_id, auth_tier, fetched_at, scored, schema_version, final_url "
            "FROM category_raw WHERE category_id=? ORDER BY fetched_at DESC, rowid DESC",
            (category_id,),
        ).fetchall()

    @staticmethod
    def _qualifies(row: sqlite3.Row, effective_tier: str) -> bool:
        return row["auth_tier"] == "member" or effective_tier == "anonymous"

    @staticmethod
    def _check_tier(effective_tier: str) -> None:
        if effective_tier not in ("anonymous", "member"):
            raise ValueError(f"bad effective tier {effective_tier!r}")

    @staticmethod
    def _pick(rows: list[sqlite3.Row]) -> sqlite3.Row | None:
        """Newest scored, else newest. `rows` is newest-first."""
        for r in rows:
            if r["scored"]:
                return r
        return rows[0] if rows else None

    def _selection(
        self,
        chosen: sqlite3.Row,
        all_rows: list[sqlite3.Row],
        ttl_days: int,
        now: datetime,
        *,
        qualified: bool,
        checked_at: str | None,
    ) -> Selection:
        superseded_at = None
        if chosen["scored"]:  # "a SCORED row was retained over a newer unscored one" (SPEC §8)
            for r in all_rows:  # newest first
                if r["rowid"] == chosen["rowid"]:
                    break
                if not r["scored"] and r["fetched_at"] > chosen["fetched_at"]:
                    superseded_at = r["fetched_at"]
                    break
        return Selection(
            rowid=chosen["rowid"],
            category_id=chosen["category_id"],
            tier=chosen["auth_tier"],
            fetched_at=chosen["fetched_at"],
            scored=bool(chosen["scored"]),
            stale=is_stale(chosen["fetched_at"], ttl_days, now),
            superseded_at=superseded_at,
            final_url=chosen["final_url"],
            schema_version=chosen["schema_version"],
            qualified=qualified,
            checked_at=checked_at,
        )

    def select_category(
        self, category_id: int, effective_tier: str, ttl_days: int, now: datetime
    ) -> Selection | None:
        """SPEC §8 row selection against the EFFECTIVE tier. None is a miss."""
        self._check_tier(effective_tier)
        with self._connect() as conn:
            rows = self._rows_for(conn, category_id)
        qualifying = [r for r in rows if self._qualifies(r, effective_tier)]
        chosen = self._pick(qualifying)
        if chosen is None:
            return None
        return self._selection(
            chosen, rows, ttl_days, now, qualified=True, checked_at=qualifying[0]["fetched_at"]
        )

    def any_row(self, category_id: int, ttl_days: int, now: datetime) -> Selection | None:
        """The best row of ANY tier — the "serve what exists on failure" path."""
        with self._connect() as conn:
            rows = self._rows_for(conn, category_id)
        chosen = self._pick(rows)
        if chosen is None:
            return None
        return self._selection(
            chosen, rows, ttl_days, now, qualified=False, checked_at=rows[0]["fetched_at"]
        )

    def has_rows(self, category_id: int) -> bool:
        with self._connect() as conn:
            r = conn.execute(
                "SELECT 1 FROM category_raw WHERE category_id=? LIMIT 1", (category_id,)
            ).fetchone()
        return r is not None

    def load_envelope(self, rowid: int) -> dict:
        """The parsed ingest envelope for a row, through an 8-entry LRU (D5)."""
        cached = self._lru.get(rowid)
        if cached is not None:
            self._lru.move_to_end(rowid)
            return cached
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM category_raw WHERE rowid=?", (rowid,)
            ).fetchone()
        if row is None:
            raise KeyError(rowid)
        envelope = json.loads(row["payload_json"])
        self._lru[rowid] = envelope
        self._trim_lru()
        return envelope

    def _trim_lru(self) -> None:
        while len(self._lru) > self._lru_size:
            self._lru.popitem(last=False)

    def select_product_row(
        self, product_id: int, effective_tier: str, ttl_days: int, now: datetime
    ) -> Selection | None:
        """The newest scored row that STILL CONTAINS the product, else the newest that does.

        Qualifying rows first; a non-qualifying row is returned with `qualified=False` so the
        caller can refetch and still serve it if that fails. Containment is decided by parsing
        the envelope (through the LRU) — never by a substring test. An index entry no payload
        contains any more is pruned.
        """
        self._check_tier(effective_tier)
        with self._connect() as conn:
            cids = [
                r["category_id"]
                for r in conn.execute(
                    "SELECT category_id FROM product_index WHERE product_id=?", (product_id,)
                )
            ]
            rows_by_cid = {cid: self._rows_for(conn, cid) for cid in cids}
        dangling: list[int] = []
        best: tuple[tuple, sqlite3.Row, list[sqlite3.Row], bool, str] | None = None
        for cid, rows in rows_by_cid.items():
            containing = [r for r in rows if self._row_contains(r["rowid"], product_id)]
            if not containing:
                dangling.append(cid)
                continue
            qualifying = [r for r in containing if self._qualifies(r, effective_tier)]
            pick = self._pick(qualifying)
            qualified = pick is not None
            if pick is None:
                pick = self._pick(containing)
            # the last look is at the CATEGORY, containing this product or not: a product CR
            # dropped is only ever in old rows, and must not refetch the category on every call
            usable = [r for r in rows if self._qualifies(r, effective_tier)] if qualified else rows
            key = (int(qualified), int(pick["scored"]), pick["fetched_at"])
            if best is None or key > best[0]:
                best = (key, pick, containing, qualified, usable[0]["fetched_at"])
        if dangling:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                for cid in dangling:
                    conn.execute(
                        "DELETE FROM product_index WHERE product_id=? AND category_id=?",
                        (product_id, cid),
                    )
                conn.execute("COMMIT")
        if best is None:
            return None
        _, pick, containing, qualified, checked_at = best
        return self._selection(
            pick, containing, ttl_days, now, qualified=qualified, checked_at=checked_at
        )

    def _row_contains(self, rowid: int, product_id: int) -> bool:
        envelope = self.load_envelope(rowid)
        data = (envelope.get("filter_instance") or {}).get("data")
        if isinstance(data, dict):
            return str(product_id) in data
        return any(_int(p.get("id")) == product_id for p in products_of(envelope))

    def prune(self, now: datetime, ttl_days: int) -> int:
        """Delete rows older than 3×TTL, never the newest scored row per category (SPEC §8)."""
        cutoff = to_iso(now - timedelta(days=3 * ttl_days))
        deleted = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            keep = {
                r["rowid"]
                for r in conn.execute(
                    "SELECT rowid FROM category_raw WHERE scored=1 AND fetched_at = ("
                    "SELECT MAX(fetched_at) FROM category_raw c2 WHERE c2.category_id="
                    "category_raw.category_id AND c2.scored=1)"
                )
            }
            old = [
                r["rowid"]
                for r in conn.execute(
                    "SELECT rowid FROM category_raw WHERE fetched_at < ?", (cutoff,)
                )
            ]
            for rowid in old:
                if rowid in keep:
                    continue
                conn.execute("DELETE FROM category_raw WHERE rowid=?", (rowid,))
                self._lru.pop(rowid, None)
                deleted += 1
            for table, key in (("reliability_raw", "category_id"), ("car_raw", "model_year_id")):
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE fetched_at < ? AND rowid NOT IN ("
                    f"SELECT rowid FROM {table} t2 WHERE t2.{key}={table}.{key} "
                    f"ORDER BY fetched_at DESC LIMIT 1)",
                    (cutoff,),
                )
                deleted += cur.rowcount
            conn.execute("COMMIT")
        return deleted

    # --- category_index / discovery ------------------------------------------------
    def upsert_category_index(self, rows: Iterable[dict], source: str, now: datetime) -> int:
        """Discovery rows: {id, path|canonical_url, display_name?}. `source` is recorded on first
        sight and never changed; names only ever fill a null or refresh an A-Z name."""
        if source not in SOURCES:
            raise ValueError(source)
        stamp = to_iso(now)
        n = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for r in rows:
                # One malformed row must not abort the whole index write.
                cid = _int(r.get("id"))
                if cid is None:
                    continue
                url = r.get("canonical_url") or (WWW + r["path"] if r.get("path") else None)
                slug = r.get("slug") or _slug_of(url)
                name = r.get("display_name")
                franchise = r.get("franchise") or _franchise_of(url)
                existing = conn.execute(
                    "SELECT source, canonical_url, slug FROM category_index WHERE category_id=?",
                    (cid,),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO category_index (category_id, slug, display_name, franchise, "
                        "canonical_url, source, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)",
                        (cid, slug, name, franchise, url, source, stamp, stamp),
                    )
                else:
                    # sitemaps carry the canonical (shortest) path; the A-Z URL may be longer
                    prefer_url = url is not None and (
                        existing["canonical_url"] is None
                        or source == "sitemap"
                        or len(url) < len(existing["canonical_url"])
                    )
                    conn.execute(
                        "UPDATE category_index SET "
                        "display_name=CASE WHEN ? IS NOT NULL AND (display_name IS NULL OR ?='az') "
                        "THEN ? ELSE display_name END, "
                        "canonical_url=CASE WHEN ? THEN ? ELSE canonical_url END, "
                        "slug=CASE WHEN ? THEN ? ELSE COALESCE(slug, ?) END, "
                        "franchise=COALESCE(franchise, ?), last_seen=?, dead_at=NULL "
                        "WHERE category_id=?",
                        (
                            name,
                            source,
                            name,
                            int(prefer_url),
                            url,
                            int(prefer_url and slug is not None),
                            slug,
                            slug,
                            franchise,
                            stamp,
                            cid,
                        ),
                    )
                    # Every slug this category has been seen under stays resolvable: the one
                    # being superseded here, and the one this source offered but did not win
                    # with. Aliases are consulted only after `category_index` misses, so a live
                    # slug always beats a retired one.
                    stale_slug = existing["slug"] if prefer_url else slug
                    kept = slug if prefer_url else existing["slug"]
                    if stale_slug and stale_slug != kept:
                        conn.execute(
                            "INSERT INTO category_slug_alias (slug, category_id) VALUES (?,?) "
                            "ON CONFLICT (slug) DO UPDATE SET category_id=excluded.category_id",
                            (stale_slug.lower(), cid),
                        )
                n += 1
            conn.execute("COMMIT")
        return n

    def record_discovery_run(self, source: str, fetched_at: datetime, count: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO discovery_runs (source, fetched_at, count) VALUES (?,?,?) "
                "ON CONFLICT (source) DO UPDATE SET fetched_at=excluded.fetched_at, "
                "count=excluded.count",
                (source, to_iso(fetched_at), count),
            )

    def discovery_status(self) -> dict[str, dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT source, fetched_at, count FROM discovery_runs").fetchall()
        return {r["source"]: {"fetched_at": r["fetched_at"], "count": r["count"]} for r in rows}

    def mark_dead(self, category_id: int, now: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE category_index SET dead_at=? WHERE category_id=?",
                (to_iso(now), category_id),
            )

    def index_row(self, category_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM category_index WHERE category_id=?", (category_id,)
            ).fetchone()
        return self._index_dict(row) if row else None

    @staticmethod
    def _index_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["groups"] = json.loads(d["groups_json"]) if d.get("groups_json") else None
        d.pop("groups_json", None)
        if d.get("has_reliability_data") is not None:
            d["has_reliability_data"] = bool(d["has_reliability_data"])
        return d

    def resolve_category(self, token: Any) -> Resolution:
        """`cNNNN`, int, numeric str or slug (hyphens, spaces and underscores are equivalent),
        consulting aliases."""
        text = str(token).strip()
        m = _CID.match(text)
        with self._connect() as conn:
            if m:
                cid = int(m.group(1))
                # an alias wins over an index row: a subcategory URL is real AND serves the
                # parent's payload, so the cache answers under the parent (SPEC §5)
                alias = conn.execute(
                    "SELECT category_id FROM category_alias WHERE alias_id=?", (cid,)
                ).fetchone()
                if alias:
                    row = conn.execute(
                        "SELECT * FROM category_index WHERE category_id=?", (alias["category_id"],)
                    ).fetchone()
                    return Resolution(
                        "ok",
                        alias["category_id"],
                        row=self._index_dict(row) if row else None,
                        via_alias=True,
                    )
                row = conn.execute(
                    "SELECT * FROM category_index WHERE category_id=?", (cid,)
                ).fetchone()
                if row:
                    return Resolution("ok", cid, row=self._index_dict(row))
                raw = conn.execute(
                    "SELECT final_url FROM category_raw WHERE category_id=? "
                    "ORDER BY fetched_at DESC LIMIT 1",
                    (cid,),
                ).fetchone()
                if raw:  # a cached payload with no index row: the row's own URL is the address
                    synthetic = {
                        "category_id": cid,
                        "canonical_url": raw["final_url"],
                        "source": None,
                        "dead_at": None,
                        "slug": _slug_of(raw["final_url"]),
                        "display_name": None,
                    }
                    return Resolution("ok", cid, row=synthetic)
                return Resolution("unknown", cid)
            norm = re.sub(r"[\s_]+", "-", text.lower())
            rows = conn.execute(
                "SELECT * FROM category_index WHERE lower(slug)=? ORDER BY category_id", (norm,)
            ).fetchall()
            if not rows:
                rows = conn.execute(
                    "SELECT * FROM category_index WHERE lower(display_name)=? ORDER BY category_id",
                    (text.lower(),),
                ).fetchall()
            if not rows:  # a slug this category was published under before discovery moved it
                rows = conn.execute(
                    "SELECT i.* FROM category_slug_alias a JOIN category_index i "
                    "ON i.category_id = a.category_id WHERE a.slug=? ORDER BY i.category_id",
                    (norm,),
                ).fetchall()
        if len(rows) == 1:
            r = self._index_dict(rows[0])
            return Resolution("ok", r["category_id"], row=r)
        if len(rows) > 1:
            cands = [
                {"id": r["category_id"], "slug": r["slug"], "name": r["display_name"]} for r in rows
            ]
            return Resolution("ambiguous", candidates=cands)
        return Resolution("unknown")

    def group_owner(self, group_id: int) -> dict | None:
        """The fetched category whose display groups include `group_id`, with the group's name —
        or None. `groups_json` is projected at ingest for the category the page was fetched for
        (never for its siblings), so this answers only for ids an agent can actually have seen
        in a `cr_ratings` response. One indexed-by-nothing scan over `category_index`, which is
        a few hundred rows."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT i.category_id, i.slug, i.display_name, json_extract(g.value, '$.name') "
                "AS group_name FROM category_index i, json_each(i.groups_json) g "
                "WHERE i.groups_json IS NOT NULL AND json_extract(g.value, '$.id') = ? "
                "ORDER BY i.category_id LIMIT 1",
                (int(group_id),),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["category_id"],
            "slug": row["slug"],
            "name": row["display_name"],
            "group_id": int(group_id),
            "group": row["group_name"],
        }

    def list_categories(
        self, *, franchise: str | None = None, family: int | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM category_index WHERE dead_at IS NULL"
        params: list[Any] = []
        if franchise:
            sql += " AND franchise=?"
            params.append(franchise)
        if family is not None:
            sql += " AND family_id=?"
            params.append(int(family))
        sql += " ORDER BY COALESCE(display_name, slug), category_id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._index_dict(r) for r in rows]

    def known_families(self) -> list[dict]:
        """The supercategories learned so far. A family is projected onto `category_index` when
        any category in it is ingested, so this grows with what has been fetched — an empty list
        means "nothing fetched yet", not "CR has no families"."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT family_id, family_name FROM category_index "
                "WHERE family_id IS NOT NULL ORDER BY family_name, family_id"
            ).fetchall()
        return [{"id": r["family_id"], "name": r["family_name"]} for r in rows]

    def index_count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM category_index").fetchone()[0]

    def search_categories(self, q: str, limit: int = 25) -> list[dict]:
        """Case-insensitive substring on display name OR slug (hyphens as spaces), exact first."""
        needle = q.strip().lower()
        if not needle:
            return []
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM category_index WHERE dead_at IS NULL").fetchall()
        hits = []
        for r in rows:
            name = (r["display_name"] or "").lower()
            slug = (r["slug"] or "").replace("-", " ").lower()
            hay_name = name
            hay_slug = slug
            n2 = needle.replace("-", " ")
            if n2 in hay_name or n2 in hay_slug or needle in (r["slug"] or "").lower():
                exact = n2 in (hay_name, hay_slug)
                hits.append((0 if exact else 1, name or slug, self._index_dict(r)))
        hits.sort(key=lambda h: (h[0], h[1], h[2]["category_id"]))
        return [h[2] for h in hits[:limit]]

    def search_products(self, q: str, limit: int = 25) -> list[dict]:
        needle = q.strip().lower()
        if not needle:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT product_id, category_id, brand_name, model_name FROM product_index"
            ).fetchall()
            hits = []
            for r in rows:
                hay = f"{r['brand_name'] or ''} {r['model_name'] or ''}".strip().lower()
                if needle in hay:
                    exact = needle in ((r["model_name"] or "").lower(), hay)
                    hits.append((0 if exact else 1, hay, r))
            hits.sort(key=lambda h: (h[0], h[1], h[2]["product_id"]))
            out = []
            for _, _, r in hits[:limit]:
                newest = self._pick(self._rows_for(conn, r["category_id"]))
                idx = conn.execute(
                    "SELECT slug, display_name FROM category_index WHERE category_id=?",
                    (r["category_id"],),
                ).fetchone()
                out.append(
                    {
                        "id": r["product_id"],
                        "brand": r["brand_name"],
                        "model": r["model_name"],
                        "category_id": r["category_id"],
                        "category_slug": idx["slug"] if idx else None,
                        "category_name": idx["display_name"] if idx else None,
                        "data_tier": newest["auth_tier"] if newest else None,
                        "fetched_at": newest["fetched_at"] if newest else None,
                    }
                )
        return out

    def cached_category_ids(self) -> list[dict]:
        """Every category with at least one payload row — what `cr_search` actually searched."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT r.category_id, i.slug, i.display_name FROM "
                "(SELECT DISTINCT category_id FROM category_raw) r "
                "LEFT JOIN category_index i ON i.category_id=r.category_id ORDER BY r.category_id"
            ).fetchall()
        return [
            {"id": r["category_id"], "slug": r["slug"], "name": r["display_name"]} for r in rows
        ]

    # --- reliability_raw -------------------------------------------------------------
    def write_reliability(
        self,
        category_ids: Iterable[int],
        payload: dict,
        fetched_at: datetime,
        final_url: str | None,
        *,
        ttl_days: int | None = None,
    ) -> int:
        """Fan out one payload to every sibling id it names (SPEC §7). With `ttl_days`, a
        sibling whose newest row already carries these bytes inside the TTL is skipped — the
        same rule as `write_category`. Returns how many rows were written."""
        stamp = to_iso(fetched_at)
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        n = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for cid in category_ids:
                if ttl_days is not None:
                    newest = conn.execute(
                        "SELECT fetched_at, payload_json FROM reliability_raw WHERE category_id=? "
                        "ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
                        (int(cid),),
                    ).fetchone()
                    if (
                        newest is not None
                        and not is_stale(newest["fetched_at"], ttl_days, fetched_at)
                        and newest["payload_json"] == text
                    ):
                        continue
                conn.execute(
                    "INSERT INTO reliability_raw "
                    "(category_id, fetched_at, payload_json, final_url) VALUES (?,?,?,?)",
                    (int(cid), stamp, text, final_url),
                )
                n += 1
            conn.execute("COMMIT")
        return n

    def select_reliability(
        self, category_id: int, ttl_days: int, now: datetime
    ) -> ReliabilitySelection | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT rowid, category_id, fetched_at, final_url FROM reliability_raw "
                "WHERE category_id=? ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
                (category_id,),
            ).fetchone()
        if row is None:
            return None
        return ReliabilitySelection(
            rowid=row["rowid"],
            category_id=row["category_id"],
            fetched_at=row["fetched_at"],
            stale=is_stale(row["fetched_at"], ttl_days, now),
            final_url=row["final_url"],
        )

    def load_reliability(self, rowid: int) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM reliability_raw WHERE rowid=?", (rowid,)
            ).fetchone()
        if row is None:
            raise KeyError(rowid)
        return json.loads(row["payload_json"])

    def reliability_url_from_cache(self, category_id: int) -> str | None:
        """The real `reliabilityURL` for a category: projected into `category_index` at write
        time from `args.cats[]`, else read from the category's own newest envelope. No scan."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT reliability_url FROM category_index WHERE category_id=?", (category_id,)
            ).fetchone()
            own = conn.execute(
                "SELECT rowid FROM category_raw WHERE category_id=? "
                "ORDER BY fetched_at DESC LIMIT 1",
                (category_id,),
            ).fetchone()
        if row and row["reliability_url"]:
            return row["reliability_url"]
        if own:
            env = self.load_envelope(own["rowid"])
            args = (env.get("filter_instance") or {}).get("args") or {}
            for c in args.get("cats") or []:
                if isinstance(c, dict) and c.get("id") == category_id and c.get("reliabilityURL"):
                    return _absolute(str(c["reliabilityURL"]))
            if isinstance(args.get("reliabilityURL"), str) and args["reliabilityURL"]:
                return _absolute(args["reliabilityURL"])
        return None

    # --- cars ------------------------------------------------------------------------
    def write_car_index(self, rows: Iterable[dict], now: datetime) -> int:
        n = 0
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for r in rows:
                conn.execute(
                    "INSERT INTO car_index (model_year_id, make_id, make, slug_make, model_id, "
                    "model, slug_model, year, states_json, car_types_json, primary_car_type_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (model_year_id) DO UPDATE SET "
                    "make_id=excluded.make_id, make=excluded.make, slug_make=excluded.slug_make, "
                    "model_id=excluded.model_id, model=excluded.model, "
                    "slug_model=excluded.slug_model, year=excluded.year, "
                    "states_json=excluded.states_json, car_types_json=excluded.car_types_json, "
                    "primary_car_type_id=excluded.primary_car_type_id",
                    (
                        int(r["model_year_id"]),
                        r.get("make_id"),
                        r.get("make"),
                        r.get("slug_make"),
                        r.get("model_id"),
                        r.get("model"),
                        r.get("slug_model"),
                        r.get("year"),
                        json.dumps(r.get("states") or []),
                        json.dumps(r.get("car_types") or []),
                        r.get("primary_car_type_id"),
                    ),
                )
                n += 1
            conn.execute("COMMIT")
        self.record_discovery_run("car_index", now, n)
        return n

    def car_index_status(self) -> dict | None:
        return self.discovery_status().get("car_index")

    def car_makes(self) -> list[dict]:
        """`[{slug, name}]`, one per make slug, sorted by slug — the `make` vocabulary with the
        display name beside the slug. A slug CR spells under two names (it does not, measured;
        this guards the join) keeps the first name in `make` order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT slug_make, make FROM car_index WHERE slug_make IS NOT NULL "
                "ORDER BY slug_make, make"
            ).fetchall()
        out: dict[str, str | None] = {}
        for slug, name in rows:
            out.setdefault(slug, name)
        return [{"slug": s, "name": n} for s, n in out.items()]

    def car_year_range(self) -> tuple[int, int] | None:
        """`(min, max)` model year across the cars index, or None while the index is empty —
        the legal `year` range for `cr_cars`, from the catalogue rather than a pinned constant.
        Measured 2026-09-06: 2000–2028 in the index, and `cars-api` answers `400` for a
        `modelYear` outside exactly that span — 1999 and 2029 both 400 (`RECON.md` §13c-ii).
        `validate_year` still lets max+1 through, because this is a snapshot that can lag a
        new model year by its TTL."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MIN(year), MAX(year) FROM car_index WHERE year IS NOT NULL"
            ).fetchone()
        if row is None or row[0] is None or row[1] is None:
            return None
        return int(row[0]), int(row[1])

    @staticmethod
    def _car_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["states"] = json.loads(d.pop("states_json") or "[]")
        d["car_types"] = json.loads(d.pop("car_types_json") or "[]")
        return d

    def car_index_row(self, model_year_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM car_index WHERE model_year_id=?", (model_year_id,)
            ).fetchone()
        return self._car_dict(row) if row else None

    def search_cars(self, q: str, limit: int = 25) -> list[dict]:
        """Every whitespace-separated token must appear in `"{make} {model} {year}"` — substring
        per token, no fuzzy matching. Exact matches first, then make, model, year desc."""
        needle = q.strip().lower()
        tokens = needle.split()
        if not tokens:
            return []
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM car_index").fetchall()
        hits = []
        for r in rows:
            hay = f"{r['make']} {r['model']} {r['year']}".lower()
            if all(t in hay for t in tokens):
                exact = needle == hay or needle == f"{r['make']} {r['model']}".lower()
                hits.append((0 if exact else 1, r["make"], r["model"], -(r["year"] or 0), r))
        hits.sort(key=lambda h: h[:4])
        return [self._car_dict(h[4]) for h in hits[:limit]]

    def write_car_taxonomy(self, payload: dict, fetched_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO car_taxonomy (fetched_at, payload_json) VALUES (?, ?)",
                (to_iso(fetched_at), json.dumps(payload, separators=(",", ":"))),
            )

    def select_car_taxonomy(self, ttl_days: int, now: datetime) -> tuple[dict, str, bool] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT fetched_at, payload_json FROM car_taxonomy ORDER BY fetched_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return (
            json.loads(row["payload_json"]),
            row["fetched_at"],
            is_stale(row["fetched_at"], ttl_days, now),
        )

    def write_car_raw(self, model_year_id: int, payload: dict, fetched_at: datetime) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO car_raw (model_year_id, fetched_at, schema_version, payload_json) "
                "VALUES (?,?,?,?)",
                (
                    int(model_year_id),
                    to_iso(fetched_at),
                    USER_VERSION,
                    json.dumps(payload, separators=(",", ":")),
                ),
            )
            return cur.lastrowid

    def select_car_raw(
        self, model_year_id: int, ttl_days: int, now: datetime
    ) -> CarSelection | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT rowid, model_year_id, fetched_at, schema_version FROM car_raw "
                "WHERE model_year_id=? ORDER BY fetched_at DESC, rowid DESC LIMIT 1",
                (model_year_id,),
            ).fetchone()
        if row is None:
            return None
        return CarSelection(
            rowid=row["rowid"],
            model_year_id=row["model_year_id"],
            fetched_at=row["fetched_at"],
            stale=is_stale(row["fetched_at"], ttl_days, now),
            schema_version=row["schema_version"],
        )

    def load_car_raw(self, rowid: int) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM car_raw WHERE rowid=?", (rowid,)
            ).fetchone()
        if row is None:
            raise KeyError(rowid)
        return json.loads(row["payload_json"])
