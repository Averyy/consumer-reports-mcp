"""The `[ ratings-wrapper ]` canary — does CR's live page still ship the attribute dictionary?

Opt-in with CR_LIVE=1. ANONYMOUS, no cookie (see conftest). **Two requests at 2 s**, one
category page each; the pages are fetched once per module and shared by every check.

`categoryAttributes` — units, descriptions, the declared dataType, `sortOrder` — is reached
through a debug statement CR left in production: `console.log('[ ratings-wrapper ]', {…})`
in the same `<script>` as `window.filterInstanceDATA` (RECON §10f). A minifier or a cleanup
commit removes it with no other change to the page, and the server keeps answering: the join
falls back to `filterInstanceDATA.attrs`, then to each entry's own `attributeTypeName`, and
every response quietly loses units and descriptions behind one `attribute_dictionary_missing`
warning. The offline suite pins how the server behaves on either side of that line; nothing
offline can say which side the LIVE page is on. This module does, and it fails with the
failure MODE named, so a maintainer reads what drifted rather than a bare assert:

    page_unavailable       not a category page at all (challenge, 5xx) — not anchor drift
    anchor_missing         the `console.log('[ ratings-wrapper ]',` string is gone
    anchor_unparseable     the anchor is there but no object can be read after it
    cat_block_missing      it parses, but there is no `.cat` block
    cat_id_mismatch        `.cat._id` is not `args.cid` — the block is another category's
    dictionary_missing     `.cat.categoryAttributes` is absent or not a list
    dictionary_empty       it is a list with nothing in it
    definition_shape       definitions no longer carry `attributeId` / `attributeDataTypeName`,
                           or the dataType is the PRICING/SPEC/TEST_RESULT homonym (RECON §9h)
    ratings_unsortable     no `numeric-rating-score` definition carries `sortOrder`
                           (`detail="standard"` would silently select by id, RECON §10j)
    units_missing          a category measured to carry units now carries none
    descriptions_missing   no definition carries a description
    coverage               product `attrs[]` entries the dictionary no longer defines
    family_missing         the sibling blocks / score-range keys `cr_categories` reads are gone

What is deliberately NOT asserted, because it is catalogue variation, not drift: the number of
definitions, which `attributeId`s exist, the dataType SET (open — an unknown type is carried
through untouched), array ORDER (shuffles between tiers, RECON §10j), `unitName` as a KEY on
every definition (CR omits it on a category with no units — Banks `c37154`), `sortOrder` on
every rating definition (absent on some in both tiers), and the exact product count.

Two categories, not one, so the message can say whether a break is TEMPLATE-WIDE (both fail
the same mode — the console.log is gone from the build) or LOCAL to one category (a service
category with a thinner block, say). Both are small pages the smoke already fetches.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import pytest

from consumer_reports_mcp import ingest
from consumer_reports_mcp.attributes import (
    RATING_KIND,
    SOURCE_ATTRS,
    SOURCE_CATEGORY_ATTRIBUTES,
    build_definitions,
)
from consumer_reports_mcp.config import WWW
from consumer_reports_mcp.extract import (
    RATINGS_WRAPPER_ANCHOR,
    brace_match,
    extract_ratings_wrapper,
)

pytestmark = pytest.mark.skipif(os.environ.get("CR_LIVE") != "1", reason="set CR_LIVE=1 to run")

# On a `categoryAttributes` DEFINITION `attributeTypeName` is the attribute's kind, never its
# dataType (RECON §9h). Seeing one of these in `attributeDataTypeName` means the keys swapped.
DEFINITION_TYPE_NAMES = frozenset({"PRICING", "SPEC", "TEST_RESULT"})


@dataclass(frozen=True)
class Canary:
    cid: int
    path: str
    name: str
    baseline: str  # where the expectation was measured
    units_expected: bool  # measured to carry at least one non-blank `unitName`


CANARIES = (
    Canary(
        cid=35183,
        path="/home-garden/vacuum-cleaners/robotic-vacuums/c35183/",
        name="Robotic Vacuums",
        baseline="RECON §1: 20/20 product attributeIds covered; §10j: 30 definitions",
        units_expected=True,  # measured 2026-09-06 on the live page
    ),
    Canary(
        cid=200228,
        path="/health/milk-milk-alternatives/plant-milk/c200228/",
        name="Plant Milk",
        baseline="tests/fixtures/category_c200228.json: 18 definitions, units $, g, mg, fl oz",
        units_expected=True,
    ),
)
_IDS = [c.name.lower().replace(" ", "-") for c in CANARIES]

# One fetch per category per module, whatever the number of checks — the async fixture loop is
# function-scoped (pyproject), so the cache is a plain module dict rather than a wider fixture.
_pages: dict[int, object] = {}


@pytest.fixture
async def page(transport, canary):
    if canary.cid not in _pages:
        _pages[canary.cid] = await transport.fetch(f"{WWW}{canary.path}")
    return _pages[canary.cid]


@pytest.fixture(params=CANARIES, ids=_IDS)
def canary(request):
    return request.param


# --------------------------------------------------------------------------- diagnostics


def _fail(mode: str, canary: Canary, detail: str) -> None:
    pytest.fail(
        f"[{mode}] {canary.name} c{canary.cid}: {detail}\n"
        f"  baseline: {canary.baseline}\n"
        f"  if BOTH canaries fail with this mode the drift is template-wide; one alone is local",
        pytrace=False,
    )


def _classified(page, canary: Canary) -> ingest.Classification:
    """The page as a category page, or `page_unavailable` — which is NOT anchor drift."""
    c = ingest.classify(page.final_url, page.status, page.content, credential_present=False)
    if page.status != 200 or page.challenge_type is not None or not c.carries_payload:
        _fail(
            "page_unavailable",
            canary,
            f"status={page.status} challenge={page.challenge_type!r} kind={c.kind} "
            f"title={c.title!r} bytes={len(page.content)} — the anchor cannot be judged on "
            f"this response; rerun before reading anything into it",
        )
    return c


def _wrapper(page, canary: Canary, c: ingest.Classification) -> dict:
    """The parsed `[ ratings-wrapper ]` object, or the structural mode that stops it."""
    text = page.content.decode("utf-8", "replace")
    at = text.find(RATINGS_WRAPPER_ANCHOR)
    if at < 0:
        _fail(
            "anchor_missing",
            canary,
            f"{RATINGS_WRAPPER_ANCHOR!r} is not in the {len(page.content)}-byte page "
            f"(filterInstanceDATA parsed fine: {len(ingest.products_of(c.filter_instance))} "
            f"products). The console.log was stripped — every response now loses units and "
            f"descriptions behind warnings={c.warnings}. The `attrs` fallback is carrying "
            f"the server; find the dictionary's new home (RECON §9h lists the trail)",
        )
    wrapper = c.ratings_wrapper if c.ratings_wrapper is not None else extract_ratings_wrapper(text)
    if wrapper is None:
        # say WHICH step of `_object_after` broke, without quoting the page
        pos = at + len(RATINGS_WRAPPER_ANCHOR)
        start = text.find("{", pos, pos + 64)
        if start < 0:
            why = "no `{` within 64 chars of the anchor (a new second argument?)"
        else:
            try:
                end = brace_match(text, start)
            except ValueError as e:
                why = f"brace matching failed ({e}) — the object no longer closes as JSON"
            else:
                try:
                    json.loads(text[start:end])
                    why = f"{end - start} bytes brace-match and parse but are not an object"
                except (ValueError, RecursionError) as e:
                    why = f"{end - start} bytes brace-match but json.loads rejects them: {e}"
        _fail(
            "anchor_unparseable",
            canary,
            f"anchor at byte {at}, {why}; warnings={c.warnings}",
        )
    return wrapper


def _dictionary(page, canary: Canary) -> tuple[ingest.Classification, dict, list[dict]]:
    c = _classified(page, canary)
    w = _wrapper(page, canary, c)
    cat = w.get("cat")
    if not isinstance(cat, dict):
        _fail(
            "cat_block_missing",
            canary,
            f"the wrapper parsed ({len(w)} top-level keys: {sorted(w)[:12]}) but carries no "
            f"`.cat` dict — the category's own block moved; RECON §10f expected .cat",
        )
    cid = c.filter_instance["args"].get("cid")
    if cat.get("_id") != cid:
        _fail(
            "cat_id_mismatch",
            canary,
            f".cat._id={cat.get('_id')!r} but args.cid={cid!r} (requested c{canary.cid}, "
            f"final url {page.final_url}) — the block is another category's dictionary; "
            f"RECON §10f measured `.cat._id == args.cid` on 6 of 6",
        )
    defs = cat.get("categoryAttributes")
    if not isinstance(defs, list):
        _fail(
            "dictionary_missing",
            canary,
            f".cat is present ({sorted(cat)[:14]}…) but categoryAttributes is "
            f"{type(defs).__name__} — warnings={c.warnings}",
        )
    if not defs:
        _fail(
            "dictionary_empty",
            canary,
            f"categoryAttributes is [] — the join falls through to `attrs` for every entry "
            f"(warnings={c.warnings}; `attribute_dictionary_missing` must be among them)",
        )
    return c, w, defs


# --------------------------------------------------------------------------- the checks


async def test_anchor_reaches_this_categorys_dictionary(page, canary):
    c, _w, defs = _dictionary(page, canary)
    assert "attribute_dictionary_missing" not in c.warnings, c.warnings
    assert c.kind == ingest.ANONYMOUS and defs


async def test_definitions_carry_the_declared_datatype(page, canary):
    _c, _w, defs = _dictionary(page, canary)
    bad = []
    for i, d in enumerate(defs):
        if not isinstance(d, dict):
            bad.append(f"#{i}: not an object")
            continue
        aid = d.get("attributeId")
        kind = d.get("attributeDataTypeName")
        if not isinstance(aid, int) or isinstance(aid, bool):
            bad.append(f"#{i}: attributeId={aid!r}")
        elif not isinstance(kind, str) or not kind.strip():
            bad.append(f"{aid}: attributeDataTypeName={kind!r} (keys {sorted(d)})")
        elif kind in DEFINITION_TYPE_NAMES:
            bad.append(f"{aid}: attributeDataTypeName={kind!r} is the attributeTypeName homonym")
    if bad:
        _fail(
            "definition_shape",
            canary,
            f"{len(bad)} of {len(defs)} definitions: {bad[:8]} — without a declared dataType "
            f"coercion runs on the entry's own attributeTypeName, and the homonym would type "
            f"everything as SPEC (RECON §9h)",
        )


async def test_rating_definitions_are_sortable(page, canary):
    _c, _w, defs = _dictionary(page, canary)
    ratings = [d for d in defs if d.get("attributeDataTypeName") == RATING_KIND]
    with_order = [d for d in ratings if isinstance(d.get("sortOrder"), int | float)]
    if not ratings or not with_order:
        _fail(
            "ratings_unsortable",
            canary,
            f"{len(ratings)} numeric-rating-score definitions, {len(with_order)} with a "
            f'sortOrder — `detail="standard"` picks its three by sortOrder (RECON §10j); '
            f"with none it silently degrades to attributeId order",
        )


async def test_units_and_descriptions_still_ship(page, canary):
    _c, _w, defs = _dictionary(page, canary)
    units = sorted({d["unitName"] for d in defs if (d.get("unitName") or "").strip()})
    described = sum(1 for d in defs if (d.get("description") or "").strip())
    if canary.units_expected and not units:
        _fail(
            "units_missing",
            canary,
            f"no definition carries a non-blank unitName ({len(defs)} definitions, keys "
            f"{sorted(set().union(*(set(d) for d in defs)))}) — `unit` is read from the "
            f"definition and never inferred, so every attribute now serialises without one",
        )
    if not described:
        _fail(
            "descriptions_missing",
            canary,
            f"none of {len(defs)} definitions carries a description — `cr_filters` serves "
            f"them and `cr_product` joins them; both now go quiet",
        )


async def test_dictionary_covers_every_product_entry(page, canary):
    c, w, _defs = _dictionary(page, canary)
    env = ingest.build_envelope(
        c.filter_instance,
        w,
        data_subscriber=c.marker,
        final_url=page.final_url,
        requested_id=canary.cid,
        http_status=page.status,
    )
    defs = build_definitions(env)
    products = ingest.products_of(env)
    entry_ids = {
        e.get("attributeId")
        for p in products
        for e in (p.get("attrs") or [])
        if isinstance(e, dict)
    }
    entry_ids.discard(None)
    if not entry_ids:
        _fail(
            "coverage",
            canary,
            f"{len(products)} products carry no attrs[] entries at all — nothing to join; "
            f"the product shape drifted before the dictionary did",
        )
    covered = {a for a in entry_ids if a in defs and defs[a].source == SOURCE_CATEGORY_ATTRIBUTES}
    if covered != entry_ids:
        missing = sorted(entry_ids - covered)
        via_attrs = [a for a in missing if a in defs and defs[a].source == SOURCE_ATTRS]
        names = {a: (defs[a].name if a in defs else None) for a in missing}
        pct = 100 * len(covered) // len(entry_ids)
        shape = "COLLAPSED" if pct < 50 else "a gap"
        _fail(
            "coverage",
            canary,
            f"categoryAttributes defines {len(covered)}/{len(entry_ids)} ({pct}%) of the "
            f"attributeIds the {len(products)} products carry — {shape}. Undefined: {names}; "
            f"{len(via_attrs)} of those fall back to `attrs`, {len(missing) - len(via_attrs)} "
            f"to the entry alone (no unit, no description). RECON §1 measured 100% on 8 of 8; "
            f"a one-id gap with `attrs` covering it is catalogue lag, a collapse is a re-keyed "
            f"or foreign dictionary",
        )


async def test_family_blocks_still_ride_the_anchor(page, canary):
    _c, w, _defs = _dictionary(page, canary)
    cat = w["cat"]
    debug = (w.get("productFilterPayload") or {}).get("debug") or {}
    siblings = debug.get("categories")
    range_keys = ("modelMinOverallDisplayScore", "modelMaxOverallDisplayScore", "modelCounts")
    have = [k for k in range_keys if k in cat]
    if not isinstance(siblings, list) or len(have) != len(range_keys):
        _fail(
            "family_missing",
            canary,
            f"productFilterPayload.debug.categories={type(siblings).__name__}, .cat carries "
            f"{have} of {list(range_keys)} — `cr_categories` reads the family and its score "
            f"ranges from here (RECON §9h, §10f); `score_range_status` would fall back to "
            f"`not_fetched` on a page that was fetched",
        )
