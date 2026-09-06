"""`lexical` — the one scoring rule behind `cr_search`'s category ranking (SPEC §7)."""

from __future__ import annotations

from consumer_reports_mcp import lexical
from consumer_reports_mcp.lexical import Match, match, stem, token_matches, tokens


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


def test_token_matches_by_stem_or_short_extension_never_substring():
    assert token_matches("microwaves", "microwave") and token_matches("microwave", "microwaves")
    assert token_matches("tv", "tvs") and token_matches("robot", "robotic")
    assert not token_matches("the", "thermostats")  # eight characters longer: not a prefix hit
    assert not token_matches("washing", "washers") and not token_matches("range", "orange")
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
    none = match(tokens("washing machine"), ["Compact Washers", "compact-washers"])
    assert none.kind == "none"
    assert none == Match(total=2, matched=0, head=False, extra=2, exact=False)
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
