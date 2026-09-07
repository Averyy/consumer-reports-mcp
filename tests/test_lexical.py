"""`lexical` — the one scoring rule behind `cr_search`'s category ranking (SPEC §7)."""

from __future__ import annotations

from consumer_reports_mcp import lexical
from consumer_reports_mcp.lexical import Match, match, root, stem, token_matches, tokens


def test_tokens_split_hyphens_and_drop_function_words():
    assert tokens("Over-the-Range Microwave Ovens") == ["over", "range", "microwave", "ovens"]
    assert tokens("Furnace Filters, Air Conditioner Filters & Air Filters") == [
        "furnace",
        "filters",
        "air",
        "conditioner",
        "filters",
        "air",
        "filters",
    ]
    assert tokens("the") == ["the"]  # a stopword alone is kept: an empty query is not this
    assert tokens(None) == [] and tokens("  ") == []


def test_stem_is_a_plural_strip_only():
    assert stem("ovens") == "oven" and stem("microwaves") == "microwave"
    assert stem("mattresses") == "mattress" and stem("mattress") == "mattress"
    assert stem("dishes") == "dish" and stem("boxes") == "box" and stem("batteries") == "battery"
    assert stem("gas") == "gas" and stem("tvs") == "tvs" and stem("glass") == "glass"


def test_root_strips_the_gerund_and_agent_suffixes_after_the_plural():
    assert root("washing") == "wash" and root("washers") == "wash" and root("washer") == "wash"
    assert root("cooking") == "cook" and root("cookers") == "cook"
    assert root("heating") == "heat" and root("heaters") == "heat"
    assert root("filtering") == "filt" and root("filters") == "filt"  # both suffixes, in order
    assert root("dishwashers") == "dishwash" != root("washing")  # a compound keeps its head
    # a bare word has no root: it must not join the derived forms (`blends` ≠ `blender`)
    assert root("wash") is None and root("blends") is None and root("heat") is None
    # four characters must remain: a word that merely ends in `er` (or `ing`) is not derived
    assert root("water") is None and root("paper") is None
    assert root("watering") == "water"  # `water` is left whole, so the two never agree
    assert root("ring") is None and root("string") is None and root("over") is None
    assert root("dryer") is None and root("drying") is None  # the price of that rule


def test_token_matches_by_stem_root_or_short_extension_never_substring():
    assert token_matches("microwaves", "microwave") and token_matches("microwave", "microwaves")
    assert token_matches("tv", "tvs") and token_matches("robot", "robotic")
    assert token_matches("washing", "washers") and token_matches("washer", "washing")
    assert token_matches("cooking", "cookers") and token_matches("heaters", "heating")
    assert not token_matches("the", "thermostats")  # eight characters longer: not a prefix hit
    assert not token_matches("range", "orange") and not token_matches("washing", "dishwashers")
    assert not token_matches("blender", "blends") and not token_matches("water", "watering")
    assert not token_matches("dish", "dishwasher")  # substring search is what this replaces
    assert not token_matches("a", "air")


def test_match_kinds_and_key():
    q = tokens("pressure cookers")
    washers = match(q, ["Pressure Washers", "pressure-washer"])
    rice = match(q, [None, "rice-cookers"])  # sitemap-only: no display name
    assert washers.kind == "partial" and rice.kind == "partial"
    assert rice.head and not washers.head
    assert rice.key < washers.key  # the head noun beats the modifier

    micro = match(tokens("over-the-range microwaves"), ["Over-the-Range Microwave Ovens"])
    assert micro.kind == "full" and micro.matched == 3 and micro.extra == 1  # "ovens"
    counter = match(tokens("over-the-range microwaves"), ["Countertop Microwave Ovens"])
    assert counter.kind == "partial" and counter.head and micro.key < counter.key

    assert match(tokens("air fryer"), ["Air Fryers", "air-fryers"]).kind == "exact"
    assert match(tokens("laptop"), ["Laptops"]).kind == "exact"
    assert match(tokens("tv"), ["TVs", "tvs"]).kind == "full"  # an extension, not a stem
    # exact against ANY text, including CR's label — its own synonym for the category
    assert match(tokens("washing machine"), ["Front-load washers", None, "washing machines"]).exact
    # `washing` reaches `washers` through the root; `machine` is nowhere — a modifier-only hit
    compact = match(tokens("washing machine"), ["Compact Washers", "compact-washers"])
    assert compact.kind == "partial" and compact.qualifies  # 1 of 2 is half
    assert compact == Match(total=2, matched=1, head=False, extra=1, exact=False)
    none = match(tokens("washing machine"), ["Dishwashers", "dishwasher"])
    assert none.kind == "none"
    assert none == Match(total=2, matched=0, head=False, extra=1, exact=False)
    assert match([], ["Laptops"]).kind == "none" and match(q, [None, ""]).kind == "none"


def test_key_prefers_the_tightest_text_then_leaves_the_rest_to_the_caller():
    q = tokens("mattress")
    exact = match(q, ["Mattresses", "mattress"])
    toppers = match(q, ["mattress toppers", "mattress-toppers"])
    stores = match(q, ["Mattress Stores", "mattress-stores"])
    assert exact.key < toppers.key and toppers.key == stores.key  # CR's order decides these
    q = tokens("dryer")
    assert match(q, ["Gas Dryers"]).key == match(q, ["Hair dryers"]).key


def test_qualifies_needs_the_head_or_half_the_tokens():
    q = tokens("window air conditioner")
    assert match(q, ["Window Air Conditioners"]).qualifies
    assert match(q, ["Portable Air Conditioners"]).qualifies  # 2 of 3, head
    assert not match(q, ["Air Fryers"]).qualifies  # 1 of 3, no head
    assert not match(q, ["Window Fans"]).qualifies
    assert match(tokens("pressure cookers"), ["Pressure Washers"]).qualifies  # 1 of 2 is half
    otc = match(tokens("over-the-range microwaves"), ["Over-the-Counter Hearing Aids"])
    assert not otc.qualifies  # "the" is a stopword; "over" alone is one of three
    assert not match(tokens("instant pot"), ["multi-cookers"]).qualifies  # a brand, not a category


def test_prefix_slack_is_the_documented_constant():
    assert lexical.PREFIX_SLACK == 2
    assert token_matches("robot", "robotic") and not token_matches("robot", "robotics2")
