"""Attribute definition join and value coercion (PLAN P2.3, SPEC §7 *Attribute normalization*).

Join order: `categoryAttributes` (dataType in `attributeDataTypeName`) → `filterInstanceDATA.attrs`
(dataType in `dataType`, keyed `id`) → the entry's own `attributeTypeName`. The definition's
`attributeTypeName` is PRICING/SPEC/TEST_RESULT and is never read as a dataType (RECON §9h).

Coerce by DECLARED type only. `text` and `custom` and any unknown type pass through untouched.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .extract import clean_text

NUMERIC_KINDS = frozenset(
    {"numeric-rating-score", "numeric-general", "numeric-price", "numeric-overall-score"}
)
BOOLEAN_KIND = "boolean"
RATING_KIND = "numeric-rating-score"
NOT_APPLICABLE = "not_applicable"  # the one `Attribute.status` token (SPEC §7)
SOURCE_CATEGORY_ATTRIBUTES = "category_attributes"
SOURCE_ATTRS = "attrs"
SOURCE_ENTRY = "entry"


@dataclass
class Definition:
    id: int
    name: str | None
    display_name: str | None
    kind: str | None  # the declared dataType
    unit: str | None
    description: str | None
    group: str | None  # attributeGroup: "Specs" / "Features" / …
    sort_order: float | None
    source: str
    is_filter_suppressed: bool = False


def _int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int | float):
        return float(v)
    return None


def _clean(v: Any) -> str | None:
    return clean_text(v)


def build_definitions(envelope: dict) -> dict[int, Definition]:
    """Definitions keyed by `attributeId`, categoryAttributes first, gaps filled from `attrs`."""
    defs: dict[int, Definition] = {}
    for d in envelope.get("category_attributes") or []:
        if not isinstance(d, dict):
            continue
        aid = _int(d.get("attributeId"))
        if aid is None:
            continue
        defs[aid] = Definition(
            id=aid,
            name=_clean(d.get("name")),
            display_name=_clean(d.get("displayName")),
            kind=_clean(d.get("attributeDataTypeName")),
            unit=_clean(d.get("unitName")),
            description=_clean(d.get("description")),
            group=_clean(d.get("attributeGroup")),
            sort_order=_num(d.get("sortOrder")),
            source=SOURCE_CATEGORY_ATTRIBUTES,
            is_filter_suppressed=bool(d.get("isFilterSuppressed", False)),
        )
    fi = envelope.get("filter_instance") or {}
    for d in fi.get("attrs") or []:
        if not isinstance(d, dict):
            continue
        aid = _int(d.get("id"))
        if aid is None:
            continue
        existing = defs.get(aid)
        if existing is None:
            defs[aid] = Definition(
                id=aid,
                name=_clean(d.get("name")),
                display_name=_clean(d.get("label")) or _clean(d.get("name")),
                kind=_clean(d.get("dataType")),
                unit=_clean(d.get("unitName")),
                description=_clean(d.get("description")),
                group=_clean(d.get("attributeGroup")),
                sort_order=_num(d.get("sortOrder")),
                source=SOURCE_ATTRS,
                is_filter_suppressed=bool(d.get("isFilterSuppressed", False)),
            )
            continue
        # fill gaps only — the dictionary block stays authoritative for what it carries
        if existing.unit is None:
            existing.unit = _clean(d.get("unitName"))
        if existing.description is None:
            existing.description = _clean(d.get("description"))
        if existing.kind is None:
            existing.kind = _clean(d.get("dataType"))
        if existing.group is None:
            existing.group = _clean(d.get("attributeGroup"))
    return defs


def is_blank(value: Any) -> bool:
    """CR's "no value": `null`, or an empty / whitespace-only string — measured 13× as `""` on
    a numeric attribute of c200228. The ONE definition every availability check reads
    (`scores_available`, `attribute_all_null`, `is_scored`) and the one `coerce` applies, so a
    column of blanks cannot pass one check as "a value" and another as "nothing"."""
    return value is None or (isinstance(value, str) and not value.strip())


def is_not_applicable(kind: str | None, value: Any) -> bool:
    """CR's off-scale `0` on a RATING column: "this test does not apply to this model", never a
    score of zero. Rating columns only — `0` is a real measurement on the other numeric kinds
    (a laptop with no USB-A ports, a mattress with no handles), and this must not touch them.

    Measured over the 52 categories cached locally (RECON §9i): a rating column uses `null` or
    `0` for its empty cell and **never both** — 22 columns of `0`, the rest of `null`, not one
    mixing them. Two natural experiments say what the `0` means: every one of the 83 humidifiers
    scoring `0` on *Humidistat accuracy* ships `Humidistat: No`, and all 38 with a humidistat are
    scored (121/121); every TV scoring `0` on *UHD picture quality* or *HDR* is an FHD or HD set,
    and all 297 4K sets are scored. A `0` also appears on no anonymous row, so it is not the
    gated tier leaking a placeholder where a score would be.

    Read as a score it is CR rating the model worst-possible on a test CR never ran — the exact
    fabrication `overall_score: null` exists to prevent, in the one place an agent cannot tell.
    """
    if kind != RATING_KIND:
        return False
    v, ok = coerce(kind, value)
    return ok and isinstance(v, int | float) and not isinstance(v, bool) and v == 0


def has_no_value(kind: str | None, value: Any) -> bool:
    """`is_blank`, plus a rating column's off-scale `0`. The reading every availability check
    gives a cell — `scores_available`, `attribute_all_null`, `is_scored` — so a column of CR's
    not-applicable markers cannot pass one check as "a value" and another as "nothing"."""
    return is_blank(value) or is_not_applicable(kind, value)


def coerce(kind: str | None, value: Any) -> tuple[Any, bool]:
    """(value, ok). `None` is never a failure — it is a gated or absent value. Unknown kinds,
    `text` and `custom` pass through untouched."""
    if value is None:
        return None, True
    if kind in NUMERIC_KINDS:
        if isinstance(value, bool):
            return None, False
        if isinstance(value, int | float):
            # a non-finite float is not a number CR publishes, and it is not JSON either:
            # `NaN` / `Infinity` in the envelope breaks a strict client (RFC 8259 has neither)
            return (value, True) if math.isfinite(value) else (None, False)
        if isinstance(value, str):
            s = value.strip().replace(",", "")
            if not s:
                return None, True  # CR's blank cell (`is_blank`); not malformed
            try:
                return int(s), True
            except ValueError:
                pass
            try:
                f = float(s)
            except ValueError:
                return None, False
            # `float()` accepts "nan", "inf", "-Infinity": a coercion failure, never a value
            return (f, True) if math.isfinite(f) else (None, False)
        return None, False
    if kind == BOOLEAN_KIND:
        if isinstance(value, bool):
            return value, True
        if isinstance(value, str):
            s = value.strip().lower()
            if s == "yes":
                return True, True
            if s == "no":
                return False, True
        return None, False
    return value, True


def normalize_entry(
    entry: dict,
    defs: dict[int, Definition],
    warnings: list[str] | None = None,
    *,
    include_description: bool = True,
) -> dict:
    """{id, name, kind, status, value, raw_value, unit, description, group} — lossless (SPEC §7).

    `status` is `not_applicable` where CR shipped a rating column's off-scale `0`
    (`is_not_applicable`); `value` is then null and `raw_value` keeps the `0` CR sent."""
    aid = _int(entry.get("attributeId"))
    d = defs.get(aid) if aid is not None else None
    entry_kind = _clean(entry.get("attributeTypeName"))
    kind = (d.kind if d and d.kind else None) or entry_kind
    raw = entry.get("value")
    value, ok = coerce(kind, raw)
    status = None
    if not ok:
        value = None
        if warnings is not None:
            w = f"coercion_failed:{aid if aid is not None else '?'}"
            if w not in warnings:  # one warning per attribute, however many products carry it
                warnings.append(w)
    elif is_not_applicable(kind, raw):
        value, status = None, NOT_APPLICABLE
    return {
        "id": aid,
        "name": (d.name if d and d.name else None) or _clean(entry.get("name")),
        "kind": kind,
        "status": status,
        "value": value,
        "raw_value": raw,
        "unit": d.unit if d else None,
        "description": (d.description if d else None) if include_description else None,
        "group": d.group if d else None,
    }


def rating_definitions(defs: dict[int, Definition]) -> list[Definition]:
    """The `numeric-rating-score` definitions in tier-stable order (RECON §10j): `sortOrder`
    ascending, missing sortOrder last, ties on `attributeId`."""
    scored = [d for d in defs.values() if d.kind == RATING_KIND]
    scored.sort(key=lambda d: (d.sort_order is None, d.sort_order or 0.0, d.id))
    return scored
