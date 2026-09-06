"""Pure classification of a fetched category page and the ingest envelope (PLAN P2.2, SPEC §7/§8).

`classify` runs in SPEC §7 order: the FINAL URL first (a rejected `hash` never reaches a
marker), then the payload, then the marker. `payload_missing` beats `marker_missing`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import is_login_url
from .extract import (
    extract_filter_instance,
    extract_ratings_wrapper,
    filter_instance_is_complete,
    read_subscriber_marker,
    read_title,
)

SCHEMA_VERSION = 1
RATING_KIND = "numeric-rating-score"

CREDENTIAL_REJECTED = "credential_rejected"
MEMBER = "member"
SESSION_EXPIRED = "session_expired"
ANONYMOUS = "anonymous"
MARKER_MISSING = "marker_missing"
PAYLOAD_MISSING = "payload_missing"

DATA_KINDS = frozenset({MEMBER, SESSION_EXPIRED, ANONYMOUS})
DRIFT_KINDS = frozenset({MARKER_MISSING, PAYLOAD_MISSING})


@dataclass
class Classification:
    kind: str
    http_status: int
    title: str | None = None
    marker: str | None = None
    warnings: list[str] = field(default_factory=list)
    filter_instance: dict | None = None
    ratings_wrapper: dict | None = None

    @property
    def data_tier(self) -> str | None:
        """The tier the page was served at, from the marker alone — never from scores."""
        if self.marker == "true":
            return "member"
        if self.marker == "false":
            return "anonymous"
        return None

    @property
    def is_member(self) -> bool | None:
        """The marker as a fact for `SessionState.on_marker`: True/False for a read marker,
        None when the page carried none (or both values — `marker_missing`)."""
        if self.marker == "true":
            return True
        if self.marker == "false":
            return False
        return None

    @property
    def carries_payload(self) -> bool:
        return self.kind in DATA_KINDS


def classify(final_url: str, status: int, html: bytes, credential_present: bool) -> Classification:
    """`credential_present` must be True ONLY when a cookie is configured, has not been rejected,
    and the transport has asserted `hash` is still in the jar (SPEC §6). "A cookie is set" is
    not a sufficient test — a rebuilt jar answers anonymously and would read as expired."""
    title = read_title(html)
    # step 1 — the FINAL url only, as a route, before any body inspection (RECON §10b, §10i)
    if is_login_url(final_url):
        return Classification(kind=CREDENTIAL_REJECTED, http_status=status, title=title)
    # step 2 — the payload; a thin or absent payload is drift, never cached
    fi = extract_filter_instance(html)
    if not filter_instance_is_complete(fi):
        return Classification(kind=PAYLOAD_MISSING, http_status=status, title=title)
    assert fi is not None
    warnings: list[str] = []
    wrapper = extract_ratings_wrapper(html)
    if not _wrapper_has_dictionary(wrapper):
        warnings.append("attribute_dictionary_missing")
    if not fi["data"]:
        warnings.append("empty_category")
    # step 3 — the marker, and only the marker (D18: both values present is also missing)
    marker = read_subscriber_marker(html)
    if marker is None:
        return Classification(
            kind=MARKER_MISSING,
            http_status=status,
            title=title,
            warnings=warnings,
            filter_instance=fi,
            ratings_wrapper=wrapper,
        )
    if marker == "true":
        kind = MEMBER
    elif credential_present:
        kind = SESSION_EXPIRED
    else:
        kind = ANONYMOUS
    return Classification(
        kind=kind,
        http_status=status,
        title=title,
        marker=marker,
        warnings=warnings,
        filter_instance=fi,
        ratings_wrapper=wrapper,
    )


def _wrapper_has_dictionary(wrapper: dict | None) -> bool:
    """A dictionary the join can use: a NON-EMPTY `categoryAttributes` list. An empty list is
    the same fall-through to `attrs` as no list at all, so it warns the same — before this,
    `[]` passed and the degraded join ran with no `attribute_dictionary_missing` at all."""
    if not isinstance(wrapper, dict):
        return False
    cat = wrapper.get("cat")
    if not isinstance(cat, dict):
        return False
    defs = cat.get("categoryAttributes")
    return isinstance(defs, list) and len(defs) > 0


# --------------------------------------------------------------------------- envelope


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _num(v: Any) -> float | int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int | float):
        return v
    return None


def _slug_from_path(path: str | None) -> str | None:
    """Last path segment before `/cNNNN/` (SPEC §7)."""
    if not path:
        return None
    parts = [p for p in path.split("?")[0].split("/") if p]
    if not parts:
        return None
    if len(parts) >= 2 and parts[-1].startswith("c") and parts[-1][1:].isdigit():
        return parts[-2]
    return parts[-1]


def _groups_from(subcats: list[dict] | None) -> list[dict]:
    groups: list[dict] = []
    for s in subcats or []:
        gid = _int(s.get("id") if s.get("id") is not None else s.get("_id"))
        name = s.get("productGroupName") or s.get("label") or s.get("name")
        if gid is None or not name:
            continue
        groups.append({"id": gid, "name": name, "sort_order": _num(s.get("sortOrder"))})
    groups.sort(key=lambda g: (g["sort_order"] is None, g["sort_order"] or 0, g["id"]))
    return groups


def _has_reliability(block: dict) -> bool | None:
    has = block.get("HasReliabilityData")
    if isinstance(has, bool):
        return has
    # `.cat` carries no HasReliabilityData key — only the sibling blocks do. Its own survey
    # block (`reliability.brands`) says the same thing.
    rel = block.get("reliability")
    if isinstance(rel, dict) and "brands" in rel:
        return bool(rel.get("brands"))
    return None


def _family_entry_from_block(block: dict, *, groups: list[dict] | None) -> dict:
    counts = block.get("modelCounts") if isinstance(block.get("modelCounts"), dict) else {}
    rated = counts.get("ratedModelsCount", block.get("ratedModelsCount"))
    return {
        "id": _int(block.get("_id")),
        "name": block.get("productGroupName"),
        "slug": block.get("targetPath") or block.get("productGroupSlugName"),
        "score_min": _num(block.get("modelMinOverallDisplayScore")),
        "score_max": _num(block.get("modelMaxOverallDisplayScore")),
        "rated_count": _int(rated),
        "has_reliability_data": _has_reliability(block),
        "groups": groups,
        "score_range_block_present": True,  # a null range inside a present block is real
    }


def _family_entry_from_args(cat: dict, *, groups: list[dict] | None) -> dict:
    return {
        "id": _int(cat.get("id")),
        "name": cat.get("pluralName") or cat.get("productGroupName") or cat.get("label"),
        "slug": _slug_from_path(cat.get("typeURL")) or cat.get("targetPath"),
        "score_min": None,
        "score_max": None,
        "rated_count": None,
        "has_reliability_data": None,
        "groups": groups,
        "score_range_block_present": False,  # no range block on hand: "we have not looked"
    }


def build_envelope(
    filter_instance: dict,
    ratings_wrapper: dict | None,
    *,
    data_subscriber: str | None,
    final_url: str,
    requested_id: int | None,
    http_status: int,
) -> dict:
    """SPEC §8 `payload_json`: everything the parser needs, extracted once at ingest."""
    args = filter_instance.get("args") or {}
    cid = _int(args.get("cid"))
    own_groups = _groups_from(args.get("subcats"))
    if not own_groups and isinstance(ratings_wrapper, dict):
        own_groups = _groups_from(ratings_wrapper.get("subcats"))

    category_attributes: list = []
    family: list[dict] = []
    supercategory: dict | None = None
    if isinstance(ratings_wrapper, dict):
        cat = ratings_wrapper.get("cat") if isinstance(ratings_wrapper.get("cat"), dict) else {}
        if isinstance(cat.get("categoryAttributes"), list):
            category_attributes = cat["categoryAttributes"]
        debug = (ratings_wrapper.get("productFilterPayload") or {}).get("debug") or {}
        siblings = debug.get("categories") if isinstance(debug.get("categories"), list) else []
        seen: set[int] = set()
        for block in siblings:
            if not isinstance(block, dict) or _int(block.get("_id")) is None:
                continue
            bid = _int(block["_id"])
            if bid in seen:
                continue
            seen.add(bid)
            family.append(
                _family_entry_from_block(block, groups=own_groups if bid == cid else None)
            )
        if cat and _int(cat.get("_id")) is not None and _int(cat["_id"]) not in seen:
            family.append(_family_entry_from_block(cat, groups=own_groups))
        scat = ratings_wrapper.get("scat")
        if isinstance(scat, dict) and _int(scat.get("_id")) is not None:
            supercategory = {"id": _int(scat["_id"]), "name": scat.get("productGroupName")}
    if not family:
        for c in args.get("cats") or []:
            if isinstance(c, dict) and _int(c.get("id")) is not None:
                fid = _int(c["id"])
                family.append(_family_entry_from_args(c, groups=own_groups if fid == cid else None))
        if cid is not None and all(f["id"] != cid for f in family):
            family.append(
                {
                    "id": cid,
                    "name": None,
                    "slug": _slug_from_path(args.get("typeURL")),
                    "score_min": None,
                    "score_max": None,
                    "rated_count": None,
                    "has_reliability_data": None,
                    "groups": own_groups,
                    "score_range_block_present": False,
                }
            )
    if supercategory is None:
        scat = args.get("scat")
        if isinstance(scat, dict) and _int(scat.get("_id")) is not None:
            supercategory = {"id": _int(scat["_id"]), "name": scat.get("productGroupName")}
        elif _int(args.get("scid")) is not None:
            supercategory = {"id": _int(args["scid"]), "name": None}

    return {
        "schema_version": SCHEMA_VERSION,
        "filter_instance": filter_instance,
        "category_attributes": category_attributes,
        "family": family,
        "supercategory": supercategory,
        "data_subscriber": data_subscriber,
        "final_url": final_url,
        "requested_id": requested_id
        if (requested_id is not None and requested_id != cid)
        else None,
        "http_status": http_status,
    }


def category_id_of(envelope: dict) -> int | None:
    """The identity of a payload is `args.cid` — never the requested id or the final URL."""
    return _int((envelope.get("filter_instance") or {}).get("args", {}).get("cid"))


def display_name_of(envelope: dict) -> str | None:
    cid = category_id_of(envelope)
    for f in envelope.get("family") or []:
        if f.get("id") == cid and f.get("name"):
            return f["name"]
    return None


def is_scored(filter_instance: dict) -> bool:
    """True when any gated value is populated — the same derivation `scores_available` uses,
    on the same reading of "populated" (`is_blank`): a rating column shipped as `""` must not
    write a `scored=1` row that never-downgrade then retains over a real one."""
    from .attributes import is_blank

    for product in _products(filter_instance):
        if not is_blank(product.get("overallDisplayScore")):
            return True
        for entry in product.get("attrs") or []:
            if entry.get("attributeTypeName") == RATING_KIND and not is_blank(entry.get("value")):
                return True
    return False


def _products(filter_instance: dict) -> list[dict]:
    data = filter_instance.get("data")
    if isinstance(data, dict):
        return [p for p in data.values() if isinstance(p, dict)]
    if isinstance(data, list):
        return [p for p in data if isinstance(p, dict)]
    return []


def products_of(envelope_or_fi: dict) -> list[dict]:
    fi = envelope_or_fi.get("filter_instance", envelope_or_fi)
    return _products(fi)


def product_index_rows(envelope: dict) -> list[tuple[int, int, str | None, str | None]]:
    cid = category_id_of(envelope)
    rows = []
    if cid is None:
        return rows
    for p in products_of(envelope):
        pid = _int(p.get("id"))
        if pid is None:
            continue
        rows.append((pid, cid, p.get("brandName"), p.get("modelName")))
    return rows
