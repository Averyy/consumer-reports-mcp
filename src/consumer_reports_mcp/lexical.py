"""Token matching for `cr_search`'s category ranking (SPEC §7 *`cr_search`*).

`cr_search` merges two category sources — CR's typeahead and the local index — and used to hand
them back in source order, so a fuzzy remote suggestion outranked an exact local match:
`'pressure cookers'` answered *Pressure Washers* first. This module is the one place the
lexical score lives; both the cache's local search and the merge in `tools_products` rank with
`Match.key`, so the two sources are scored by the same rule and CR's own order decides only
among equally-scoring hits.

Plain token logic, deliberately: no edit distance and no dependency. Hyphens split like
spaces, matching is case-insensitive, and a query token matches a hay token when the two agree
after a plural strip (`microwaves` ≈ `microwave`, `mattresses` ≈ `mattress`), when both are
derived forms sharing a root once the gerund and agent-noun suffixes come off (`washing` ≈
`washers`, `cooking` ≈ `cookers` — measured 2026-09-07: `washing machines` reached only the one
category CR labels with that phrase and missed both top-load washer categories), or when the
hay token merely extends it by up to two characters (`tv` → `tvs`, `robot` → `robotic`) —
never by substring, which would let `"the"` in `over-the-range` reach *thermostats*.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_TOKEN = re.compile(r"[a-z0-9]+")
# a hay token may be this many characters longer than the query token it extends
PREFIX_SLACK = 2

MatchKind = Literal["exact", "full", "partial", "none"]


# function words carry no meaning of their own: dropped from both sides whenever anything else
# remains, so `over-the-range` does not reach *Over-the-Counter Hearing Aids* through `the`
STOPWORDS = frozenset({"a", "an", "and", "for", "in", "of", "the", "to", "with"})


def tokens(text: str | None) -> list[str]:
    """Lower-cased alphanumeric runs minus `STOPWORDS`: `"Over-the-Range Microwave Ovens"` →
    `["over", "range", "microwave", "ovens"]`."""
    raw = _TOKEN.findall((text or "").lower())
    kept = [t for t in raw if t not in STOPWORDS]
    return kept or raw


def stem(token: str) -> str:
    """A plural strip, nothing more: `ovens` → `oven`, `dishes` → `dish`, `batteries` →
    `battery`; `gas`, `tvs` and `glass` are left alone. Applied to both sides, so it only
    has to be consistent, not correct English."""
    if len(token) <= 3:
        return token
    if token.endswith("ies"):
        return token[:-3] + "y"
    if token.endswith(("sses", "shes", "ches", "xes")):
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def root(token: str) -> str | None:
    """The stem with the gerund or agent-noun suffix taken off — `washing` → `wash`, `washers`
    → `wash`, `filtering` → `filt` (both, in order) — or None when there was none to take.
    Two DERIVED forms of a word agree (`washing` ≈ `washers`, `cooking` ≈ `cookers`, `heating`
    ≈ `heaters`); the bare word never joins them: swept over the index vocabulary 2026-09-07, a
    bare root equated `blends` with `blender` and `heat` with `heaters`, and nothing else the
    derived pairs did not already cover. At least four characters must remain, which is what
    keeps a word that merely ENDS in `er` — `water`, `paper`, `cover` — from posing as one
    (`watering` ≈ `water` was the sweep's other false pair); `dryer`/`drying` and `ring` are
    left alone with them. Like `stem`, consistent rather than English."""
    t = stem(token)
    stripped = False
    for suffix in ("ing", "er"):
        if t.endswith(suffix) and len(t) - len(suffix) >= 4:
            t = t[: -len(suffix)]
            stripped = True
    return t if stripped else None


def token_matches(query_token: str, hay_token: str) -> bool:
    if stem(query_token) == stem(hay_token):
        return True
    r = root(query_token)
    if r is not None and r == root(hay_token):
        return True
    return (
        len(query_token) >= 2
        and hay_token.startswith(query_token)
        and len(hay_token) - len(query_token) <= PREFIX_SLACK
    )


@dataclass(frozen=True)
class Match:
    """How well one candidate's texts (name, slug, CR's label) cover a query.

    `key` sorts better matches first: an exact text, then a hit carrying the query's HEAD
    token (its last word — `cookers` in `pressure cookers`, the thing being asked for, where
    `pressure` is only a modifier), then more query tokens matched, then the tightest text
    (fewest unmatched words of its own — *Mattresses* over *Mattress Toppers*). Everything
    the key leaves tied is decided by the caller, which for `cr_search` is CR's own typeahead
    order: CR knows popularity and this module does not.
    """

    total: int  # query tokens
    matched: int  # of them found in any text
    head: bool  # the last query token was found
    extra: int  # unmatched hay tokens in the tightest text
    exact: bool  # some text's stemmed tokens equal the query's, order aside

    @property
    def kind(self) -> MatchKind:
        if self.exact:
            return "exact"
        if not self.matched:
            return "none"  # before `full`: zero of zero tokens is not coverage
        return "full" if self.matched == self.total else "partial"

    @property
    def key(self) -> tuple[int, int, int, int]:
        return (0 if self.exact else 1, 0 if self.head else 1, -self.matched, self.extra)

    @property
    def qualifies(self) -> bool:
        """Whether a LOCAL row is worth returning: the head token matched, or at least half
        the query's tokens did. `window air conditioner` therefore reaches *Portable Air
        Conditioners* but not *Air Fryers*; a typeahead hit is never subjected to this — CR
        returned it, and the ranking, not this module, says how well it fits."""
        return self.matched > 0 and (self.head or self.matched * 2 >= self.total)


def match(query_tokens: list[str], texts: list[str | None]) -> Match:
    hays = [tokens(t) for t in texts if t]
    hays = [h for h in hays if h]
    if not query_tokens or not hays:
        return Match(total=len(query_tokens), matched=0, head=False, extra=0, exact=False)
    found = [any(token_matches(q, h) for hay in hays for h in hay) for q in query_tokens]
    stems = sorted(stem(q) for q in query_tokens)
    exact = any(sorted(stem(h) for h in hay) == stems for hay in hays)
    extra = min(
        sum(1 for h in hay if not any(token_matches(q, h) for q in query_tokens)) for hay in hays
    )
    return Match(
        total=len(query_tokens),
        matched=sum(found),
        head=found[-1],
        extra=extra,
        exact=exact,
    )
