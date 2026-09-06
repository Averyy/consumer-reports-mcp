"""P7.3 — the typed envelopes and the two structural guards (SPEC §7)."""

from __future__ import annotations

import pytest

from consumer_reports_mcp import envelope as E


def _ratings(**over):
    base = dict(
        auth_state="anonymous",
        session="none",
        scores_available=E.ScoresAvailable(
            overall_score="unavailable",
            attribute_ratings="unavailable",
            owner_satisfaction="available",
            predicted_reliability="available",
            recommended_flag="available",
        ),
        provenance=E.Provenance(
            data_tier="anonymous",
            fetched_at="2026-09-03T12:00:00Z",
            cr_url="https://x/",
            from_cache=True,
            stale=False,
            superseded_at=None,
        ),
        sort=E.SortInfo(key="overallScore", order="desc", scope="within_group"),
        warnings=[],
        error=None,
        data=None,
    )
    base.update(over)
    return E.RatingsEnvelope(**base)


def test_eight_keys_on_ratings():
    env = _ratings()
    assert list(env.model_dump()) == [
        "auth_state",
        "session",
        "scores_available",
        "provenance",
        "sort",
        "warnings",
        "error",
        "data",
    ]


@pytest.mark.parametrize(
    ("tier", "session", "expected"),
    [
        ("member", "expired", "member"),
        ("member", "active", "member"),
        ("anonymous", "expired", "session_expired"),
        ("anonymous", "unverified", "anonymous"),
        ("anonymous", "none", "anonymous"),
        ("anonymous", "active", "anonymous"),
    ],
)
def test_auth_state_derivation_table(tier, session, expected):
    assert E.auth_state(tier, session) == expected


def test_notice_only_when_unavailable():
    scores = {
        "overall_score": "absent",
        "attribute_ratings": "absent",
        "owner_satisfaction": "absent",
        "predicted_reliability": "absent",
        "recommended_flag": "available",
    }
    assert E.maybe_notice(scores, "member") is None  # member all-null → no notice
    scores["overall_score"] = "unavailable"
    text = E.maybe_notice(scores, "session_expired")
    assert text and "session_expired" in text and "not an absence of CR ratings" in text
    assert E.maybe_notice(None, "anonymous") is None


def test_reliability_envelope_has_no_session_or_sort_field():
    fields = set(E.ReliabilityEnvelope.model_fields)
    assert "session" not in fields and "sort" not in fields
    assert fields == {"auth_state", "scores_available", "provenance", "warnings", "error", "data"}
    assert "superseded_at" not in E.ReliabilityProvenance.model_fields
    assert E.ReliabilityEnvelope.model_json_schema()["properties"]["auth_state"] == {
        "const": "anonymous",
        "title": "Auth State",
        "type": "string",
    }


def _enum_of(prop: dict) -> list:
    """The enum of a property that may be nullable (`anyOf: [{enum}, {type: null}]`)."""
    return prop["enum"] if "enum" in prop else prop["anyOf"][0]["enum"]


def test_output_schema_has_enums():
    schema = E.RatingsEnvelope.model_json_schema()
    assert _enum_of(schema["properties"]["auth_state"]) == [
        "anonymous",
        "member",
        "session_expired",
    ]
    assert schema["properties"]["session"]["enum"] == ["none", "unverified", "active", "expired"]
    sa = schema["$defs"]["ScoresAvailable"]["properties"]["overall_score"]
    assert sa["enum"] == ["available", "absent", "unavailable"]
    assert schema["$defs"]["Provenance"]["properties"]["data_tier"]["enum"] == [
        "anonymous",
        "member",
    ]
    assert schema["$defs"]["SortInfo"]["properties"]["scope"]["enum"] == [
        "within_group",
        "cross_group",
    ]


def test_auth_state_is_null_only_where_no_row_was_served():
    """SPEC §7: `auth_state` is derived from the SERVED row. The three products envelopes accept
    `null` — an error envelope with nothing served, where `provenance` is null too — and keep
    the enum in the schema; `cr_reliability` pins the literal and cannot be null; cars have no
    such field at all. Three surfaces, one rule: no row, no invented tier."""
    err = E.ToolError(code="unknown_category", message="nope", reason="not_in_index")
    env = _ratings(auth_state=None, error=err, scores_available=None, provenance=None, sort=None)
    assert env.model_dump(mode="json")["auth_state"] is None
    for model in (E.RatingsEnvelope, E.ProductEnvelope, E.FiltersEnvelope):
        prop = model.model_json_schema()["properties"]["auth_state"]
        assert _enum_of(prop) == ["anonymous", "member", "session_expired"], model
        assert {"type": "null"} in prop["anyOf"], model
        assert "auth_state" in model.model_json_schema()["required"], model  # null, never absent
    with pytest.raises(ValueError):
        E.ReliabilityEnvelope(
            auth_state=None,
            scores_available=None,
            provenance=None,
            warnings=[],
            error=err,
            data=None,
        )
    for model in (E.CarSearchEnvelope, E.CarsEnvelope, E.CarEnvelope):
        assert "auth_state" not in model.model_fields


def test_error_and_data_mutually_exclusive():
    err = E.ToolError(code="unknown_category", message="nope", reason="not_in_index")
    env = _ratings(
        auth_state=None, error=err, data=None, scores_available=None, provenance=None, sort=None
    )
    assert env.error is not None and env.data is None
    # the envelope is constructed by the tools; the invariant is that a non-null error never
    # travels with data — pin it on the model dump used by the server
    dumped = env.model_dump(mode="json")
    assert dumped["error"]["code"] == "unknown_category" and dumped["data"] is None


def test_product_shapes_serialise_by_their_own_class():
    summary = E.ProductSummary(
        id=1,
        brand="Brand A",
        model="M",
        group="G",
        rank=1,
        price=None,
        overall_score=None,
        recommended=False,
        dont_buy=False,
        smart_buy=False,
    )
    standard = E.ProductStandard(
        **summary.model_dump(),
        ratings=[E.Rating(id=1, name="x", value=None)],
        owner_satisfaction=4,
        predicted_reliability=None,
    )
    block = E.FlatRatings(
        category=E.CategoryRef(id=1, slug="s", name="n"),
        detail="standard",
        group_mode="flat",
        products=[summary, standard],
        size=2,
        total=2,
        truncated=False,
        notice=None,
    )
    dumped = block.model_dump(mode="json")
    assert "ratings" not in dumped["products"][0]  # summary: omitted, not null
    assert dumped["products"][1]["ratings"][0]["value"] is None  # standard: null, never absent
    assert dumped["products"][1]["predicted_reliability"] is None
    assert dumped["products"][0]["overall_score"] is None and "dont_buy" in dumped["products"][0]
    assert "projected_attributes" not in dumped["products"][0]  # omitted unless asked for
    attr = E.Attribute(
        id=1,
        name="w",
        kind="numeric-general",
        value=36,
        raw_value=36,
        unit=None,
        description=None,
        group=None,
    ).model_dump()
    assert attr == {"id": 1, "name": "w", "kind": "numeric-general", "value": 36}
    gated = E.Attribute(
        id=2,
        name="r",
        kind="numeric-rating-score",
        value=None,
        raw_value=None,
        unit=None,
        description="d",
        group="Ratings",
    ).model_dump()
    assert gated["value"] is None and "raw_value" not in gated and gated["description"] == "d"


def test_cars_envelopes_omit_auth_state_and_data_tier():
    for model in (E.CarSearchEnvelope, E.CarsEnvelope, E.CarEnvelope):
        fields = set(model.model_fields)
        assert "auth_state" not in fields and "session" in fields
    assert "data_tier" not in E.CarProvenance.model_fields
    schema = E.CarsEnvelope.model_json_schema()
    enum = schema["$defs"]["CarScoresAvailable"]["properties"]["overall_score"]["anyOf"][0]["enum"]
    assert enum == ["available", "absent"]


def test_categories_and_search_envelopes_carry_session_and_index_provenance():
    for model in (E.CategoriesEnvelope, E.SearchEnvelope):
        fields = set(model.model_fields)
        assert fields == {"session", "provenance", "warnings", "error", "data"}


def test_error_model_from_query_error():
    from consumer_reports_mcp.query import QueryError

    qe = QueryError("ambiguous_filter_name", "twin", filter="features", candidates=[1, 2])
    m = E.error_model(qe)
    assert m.code == "ambiguous_filter_name" and m.candidates == [{"id": 1}, {"id": 2}]
    assert m.filter == "features"


# --------------------------------------------------------------------------- no_results


def test_no_results_encodes_every_delimiter_it_uses():
    """`,` `=` `:` `|` and newlines are the grammar's own separators, so a value carrying one
    must not be able to split a pair or forge a second vocabulary token."""
    warning = E.no_results({"brands": ["a,b=c:d|e\nf"]})
    body = warning.removeprefix("no_results:")
    assert len(body.split(",")) == 1
    assert body.count("=") == 1
    assert "\n" not in warning


def test_no_results_distinguishes_a_literal_separator_from_a_list():
    assert E.no_results({"brands": ["a|b"]}) != E.no_results({"brands": ["a", "b"]})


def test_no_results_truncates_a_long_value_and_the_whole_token():
    warning = E.no_results({"features": {"k": "A" * 200_000}})
    assert len(warning) <= E.NO_RESULTS_MAX_TOTAL + 1


def test_no_results_caps_and_dedups_list_values():
    many = E.no_results({"brands": list(range(100))})
    assert many.count("|") == E.NO_RESULTS_MAX_ITEMS  # capped values plus the truncation mark
    assert E.no_results({"brands": [1, 1, 1]}) == "no_results:brands=1"


def test_no_results_renders_numbers_and_booleans_canonically():
    """A client's `500` arrives as `500.0` through the float-typed schema; `recommended` and a
    feature boolean must not render two different ways in one token."""
    assert E.no_results({"price_min": 500.0}) == "no_results:price_min=500"
    assert E.no_results({"price_min": 12.5}) == "no_results:price_min=12.5"
    assert E.no_results({"recommended": True}) == "no_results:recommended=true"
    assert E.no_results({"features": {"k": True}}) == "no_results:features=k:true"


def test_no_results_pairs_are_sorted_so_the_token_is_stable():
    assert E.no_results({"price_min": 1, "brands": [2]}).startswith("no_results:brands=")


# --------------------------------------------------------------------------- warnings registry


def _categories(warnings: list[str]) -> E.CategoriesEnvelope:
    return E.CategoriesEnvelope(
        session="none", provenance=None, warnings=warnings, error=None, data=None
    )


def test_every_envelope_rejects_an_unregistered_warning():
    """`warnings[]` used to be a bare `list[str]`: a typo at any of a dozen emission sites, or a
    token nobody documented, shipped as "machine-readable". The registry is checked where it
    matters — at envelope construction — so an unknown token fails the tool call loudly, the
    way an unlisted `ToolError.code` already does."""
    for bad in ("sitemap_pending", "no_results:", "empty_category:1", "refresh-failed:x", ""):
        with pytest.raises(ValueError, match="unregistered warning"):
            _categories([bad])
    ok = _categories(["sitemap_pass_pending", "no_results:brands=A", "session_expiring:0"])
    assert ok.warnings == ["sitemap_pass_pending", "no_results:brands=A", "session_expiring:0"]
    # every envelope class wears the same checked type — not only the one built above
    for cls in (
        E.RatingsEnvelope,
        E.ProductEnvelope,
        E.FiltersEnvelope,
        E.ReliabilityEnvelope,
        E.CategoriesEnvelope,
        E.SearchEnvelope,
        E.CarSearchEnvelope,
        E.CarsEnvelope,
        E.CarEnvelope,
        E.SignInEnvelope,
        E.AuthStatusEnvelope,
    ):
        field = cls.model_fields["warnings"]
        assert any(type(m).__name__ == "AfterValidator" for m in field.metadata), cls.__name__


def test_warning_registry_keeps_the_output_schema_a_plain_string_array():
    schema = E.RatingsEnvelope.model_json_schema()["properties"]["warnings"]
    assert schema["type"] == "array" and schema["items"] == {"type": "string"}


def test_is_known_warning_grammar():
    assert E.is_known_warning("empty_category")
    assert E.is_known_warning("coercion_failed:isRecommended")
    assert E.is_known_warning("ratings_unavailable:123:timeout")
    assert not E.is_known_warning("coercion_failed")  # a prefix needs its detail
    assert not E.is_known_warning("empty_category:x")  # a bare token takes none
    assert not E.is_known_warning(None)  # type: ignore[arg-type]
    assert not (E.WARNING_TOKENS & E.WARNING_PREFIXES)


def test_warning_registry_matches_the_documented_vocabulary():
    """The registry is the code's enumeration; SPEC §7 and CLAUDE.md are the prose ones. Each
    token in code is documented in both, and each token the SPEC's vocabulary table lists is
    registered — so the three cannot drift apart silently in either direction."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = (root / "SPEC.md").read_text(encoding="utf-8")
    claude = (root / "CLAUDE.md").read_text(encoding="utf-8")
    registered = E.WARNING_TOKENS | E.WARNING_PREFIXES
    for token in registered:
        assert token in spec, f"{token} is not in SPEC.md"
        assert token in claude, f"{token} is not in CLAUDE.md"
    # the SPEC's vocabulary table: every `token` in its first column is a registered token or
    # prefix (a prefixed entry is written `prefix:<detail>`)
    table = spec.split("### Warnings vocabulary", 1)[1].split("\n###", 1)[0]
    listed = {
        m.group(1).partition(":")[0]
        for m in re.finditer(r"^\| `([a-z_]+(?::<[^`]*)?)`", table, re.M)
    }
    assert listed, "the SPEC vocabulary table was not found"
    assert listed == registered, listed ^ registered


def test_no_results_refuses_an_empty_parameter_set():
    """With nothing to name the renderer produced the bare `no_results:`, which the registry
    rejects at envelope construction — so a zero-row page with no filter became a tool crash.
    The renderer refuses it at the point of construction, naming the rule, and the registry
    keeps rejecting the bare token: the guard is not blunted, the caller is told earlier."""
    with pytest.raises(ValueError, match="at least one filter"):
        E.no_results({})
    assert not E.is_known_warning("no_results:")
    assert E.is_known_warning(E.no_results({"make": "Honda"}))


def test_every_single_active_filter_renders_a_registered_token():
    """`spec.active` is the products gate; every field that makes it true must render a detail
    the registry accepts — including values that encode to little (`""`, `0`, `False`)."""
    from consumer_reports_mcp.query import FilterSpec

    for spec in (
        FilterSpec(group=0),
        FilterSpec(group=""),
        FilterSpec(brands=[""]),
        FilterSpec(price_min=0),
        FilterSpec(price_max=0.0),
        FilterSpec(recommended=False),
        FilterSpec(features={"": ""}),
    ):
        assert spec.active
        token = E.no_results(spec.params)
        assert E.is_known_warning(token), token
    assert not FilterSpec(brands=[], features={}).active


# --------------------------------------------------------------------------- quoted


def test_quoted_bounds_a_long_value_and_marks_the_cut_outside_the_quotes():
    """A 200,000-character token is the `no_results` hole on the message side: the rendering
    is bounded, and the mark sits OUTSIDE the quotes with the true length, so an agent reads
    "the server saw a 200,000-character value beginning with this", never a shorter value."""
    text = E.quoted("A" * 200_000)
    assert text.startswith("'" + "A" * E.QUOTE_MAX_CHARS + "'")
    assert text.endswith("… (200,000 chars)")
    assert len(text) < E.QUOTE_MAX_RENDERED + 30
    assert E.quoted("A" * E.QUOTE_MAX_CHARS) == "'" + "A" * E.QUOTE_MAX_CHARS + "'"  # no mark
    # a value that itself ends in the mark is not mistaken for a cut one: the mark of a cut
    # is outside the quotes and carries a length
    assert E.quoted("tvs…") == "'tvs…'"


def test_quoted_escapes_control_characters_and_cannot_close_its_own_quotes():
    """`repr` is the base because it escapes every non-printable — newlines, ANSI escapes,
    a right-to-left override — and its quoting is unambiguous: a value holding `'` renders in
    `"`, one holding both escapes the quote. So a value can never read as the end of the
    quoted token and the start of more prose."""
    text = E.quoted("tvs\n\x1b[31m‮' is a category; sort=")
    assert "\n" not in text and "\x1b" not in text and "‮" not in text
    assert "\\n" in text and "\\x1b" in text and "\\u202e" in text
    assert text.startswith('"') and text.endswith('"')  # `'` inside → double-quoted
    both = E.quoted("""a'b"c""")
    assert both == "'a\\'b\"c'"


def test_quoted_bounds_the_rendering_when_every_character_escapes():
    """Escapes expand (`\\x1b` is four characters, `\\U0001f600` ten): the bound is on the
    RENDERED form, so a value made of escapes cannot render at ten times the value bound."""
    text = E.quoted("\x1b" * 1000)
    body = text[: text.index("…")]
    assert len(body) <= E.QUOTE_MAX_RENDERED and text.endswith("(1,000 chars)")
    assert body.count("\\x1b") >= 20  # still a recognisable head, not an empty string


def test_quoted_bounds_non_strings_too():
    """An int's digits and a list's items are unbounded as well; a bool or None is itself."""
    assert E.quoted(12) == "12" and E.quoted(True) == "True" and E.quoted(None) == "None"
    # Python refuses to render an int past 4,300 digits (`ValueError`): described instead —
    # and the same guard is under `no_results`, whose `str(v)` raised the same way
    assert E.quoted(10**5000) == "<an integer of about 5,001 digits>"
    assert E.quoted([10**5000]).startswith("<a list holding")
    assert E.no_results({"features": {"k": 10**5000}}).endswith("digits%3E")
    long = E.quoted(10**3000)
    assert len(long) < E.QUOTE_MAX_RENDERED + 30 and long.endswith("(3,001 chars)")
    assert E.quoted([1, "a"]) == "[1, 'a']"
    assert E.quoted(["A" * 500]).endswith("chars)")


def test_quoted_list_caps_dedupes_and_counts_the_rest():
    assert E.quoted_list(["a", "b"]) == "'a', 'b'"
    many = E.quoted_list(f"v{i}" for i in range(100))
    assert many.count(", ") == E.QUOTE_MAX_ITEMS - 1 and many.endswith("… (88 more)")
    assert E.quoted_list(["a", "a", "a"]) == "'a'"
    # identical renderings collapse, but the count is over distinct inputs
    same = E.quoted_list(["B" * 5000 + str(i) for i in range(200)])
    assert same.count("chars)") == 3 and same.endswith("… (197 more)")
    assert E.quoted_list([]) == ""
    rows = [{"id": 1, "name": "x"}]
    rendered = E.quoted_list(rows, lambda r: f"{r['id']} ({E.quoted(r['name'])})")
    assert rendered == "1 ('x')"


def test_candidates_are_bounded_like_the_message_beside_them():
    """`error.candidates` is structured, so JSON escaping covers control characters and only
    the length needs bounding: at most QUOTE_MAX_ITEMS entries, every string clipped, and
    None (never `[]`, which would claim "no legal values") when there is nothing."""
    rows = [{"id": i, "name": "N" * 1000, "slug": "s"} for i in range(30)]
    out = E.candidates(rows)
    assert len(out) == E.QUOTE_MAX_ITEMS
    assert out[0]["name"] == "N" * E.QUOTE_MAX_CHARS + "…" and out[0]["slug"] == "s"
    assert out[0]["id"] == 0  # non-strings untouched
    assert E.candidates(["x" * 100, 7]) == ["x" * E.QUOTE_MAX_CHARS + "…", 7]
    assert E.candidates([]) is None


def test_bad_query_refuses_an_empty_or_over_long_query_without_echoing_it():
    from consumer_reports_mcp.config import SEARCH_QUERY_MAX_CHARS

    assert E.bad_query("").code == "invalid_filter_value" and E.bad_query("").filter == "query"
    assert E.bad_query("x" * SEARCH_QUERY_MAX_CHARS) is None
    err = E.bad_query("x" * 200_000)
    assert err.filter == "query" and "200,000" in err.message and len(err.message) < 100
    assert "xxx" not in err.message


def test_auth_state_and_provenance_are_null_together():
    """SPEC §7: both describe the SERVED row, so an envelope with a tier and no provenance —
    "member data, from nowhere, of no age" — is refused at construction, as is a provenance
    with no tier. A filter rejected after the fetch shipped the first shape on every
    `sort`/`order`/`group` error; the model now makes it unconstructible on all three
    products envelopes, whatever site builds them."""
    prov = E.Provenance(
        data_tier="member",
        fetched_at="2026-09-03T12:00:00Z",
        cr_url="https://x/",
        from_cache=True,
        stale=False,
        superseded_at=None,
    )
    err = E.ToolError(code="invalid_filter_value", message="nope", filter="sort")
    for model, extra in (
        (E.RatingsEnvelope, {"sort": None}),
        (E.ProductEnvelope, {}),
        (E.FiltersEnvelope, {}),
    ):
        base = dict(session="active", scores_available=None, warnings=[], error=err, data=None)
        with pytest.raises(ValueError, match="provenance=null"):
            model(auth_state="member", provenance=None, **base, **extra)
        with pytest.raises(ValueError, match="provenance=set"):
            model(auth_state=None, provenance=prov, **base, **extra)
        assert model(auth_state="member", provenance=prov, **base, **extra).auth_state == "member"
        assert model(auth_state=None, provenance=None, **base, **extra).provenance is None
    assert issubclass(E.RatingsEnvelope, E.RowEnvelope)
    for model in (E.ReliabilityEnvelope, E.CarsEnvelope, E.CarEnvelope, E.CategoriesEnvelope):
        assert not issubclass(model, E.RowEnvelope)  # no derived `auth_state` to pair


def test_legal_values_and_legal_range_are_the_two_candidate_shapes():
    """`candidates` has three shapes (SPEC §7 *Error taxonomy*): `{value}` for a closed
    vocabulary, `{min, max}` for a numeric span, and `{id, name}`-style rows for a CR
    vocabulary. The first two have one constructor each, so every enum parameter on both
    surfaces answers the same key — `candidates[*].value` — and never a bare scalar the
    caller has to guess the meaning of. `legal_values` is bounded like `candidates`; an empty
    vocabulary is None, never `[]`."""
    assert E.legal_values(("summary", "standard")) == [{"value": "summary"}, {"value": "standard"}]
    assert E.legal_values({"new": 2, "used": 1}) == [{"value": "new"}, {"value": "used"}]
    assert E.legal_values(()) is None
    assert len(E.legal_values(range(100))) == E.QUOTE_MAX_ITEMS
    assert E.legal_values(["x" * 100])[0]["value"] == "x" * E.QUOTE_MAX_CHARS + "…"
    assert E.legal_range(1, 25) == [{"min": 1, "max": 25}]
    assert E.legal_range(0, None) == [{"min": 0, "max": None}]  # an open bound is null
    err = E.ToolError(code="invalid_filter_value", message="m", candidates=E.legal_range(1, 2))
    assert err.model_dump()["candidates"] == [{"min": 1, "max": 2}]
