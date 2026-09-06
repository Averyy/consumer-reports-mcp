"""P3 — the SQLite layer whose contract is the tier rules (SPEC §8, PLAN §4.3)."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta

import pytest

from consumer_reports_mcp import cache as cache_mod
from consumer_reports_mcp.cache import Cache
from tests.conftest import fixture_envelope

T0 = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
TTL = 30


def days(n: float) -> datetime:
    return T0 - timedelta(days=n)


@pytest.fixture
def cache(tmp_path) -> Cache:
    return Cache(tmp_path / "cr.db")


@pytest.fixture
def anon_env(c37162) -> dict:
    return fixture_envelope(c37162)


@pytest.fixture
def member_env(c37162) -> dict:
    return fixture_envelope(c37162, fill_scores=True)


# --------------------------------------------------------------------------- P3.1


def test_schema_creates_and_is_idempotent(tmp_path):
    a = Cache(tmp_path / "cr.db")
    v1 = a.user_version()
    b = Cache(tmp_path / "cr.db")
    assert b.user_version() == v1 == cache_mod.USER_VERSION
    assert (tmp_path / "cr.db").exists()


def test_category_raw_has_no_update_or_delete_api():
    src = inspect.getsource(cache_mod)
    for stmt in ("UPDATE category_raw", "DELETE FROM category_raw"):
        occurrences = [
            m.start() for m in __import__("re").finditer(__import__("re").escape(stmt), src)
        ]
        # only prune() may delete; nothing may update
        if stmt.startswith("UPDATE"):
            assert not occurrences, stmt
        else:
            assert occurrences
            prune_src = inspect.getsource(Cache.prune)
            assert stmt in prune_src
            assert len(occurrences) == prune_src.count(stmt)


# --------------------------------------------------------------------------- P3.2 writes


def test_anonymous_write_after_member_leaves_member_row_intact(cache, anon_env, member_env):
    r1 = cache.write_category(member_env, tier="member", scored=True, fetched_at=days(10))
    r2 = cache.write_category(anon_env, tier="anonymous", scored=False, fetched_at=days(1))
    assert r1 != r2
    sel = cache.select_category(37162, "anonymous", TTL, T0)
    assert sel.rowid == r1 and sel.tier == "member" and sel.scored
    assert sel.superseded_at == cache_mod.to_iso(days(1))
    assert cache.load_envelope(r1)["data_subscriber"] == "true"


def test_alias_recorded_and_row_filed_under_args_cid(cache, c200228):
    env = fixture_envelope(c200228)  # requested 200358, args.cid 200228
    rowid = cache.write_category(env, tier="anonymous", scored=False, fetched_at=days(1))
    assert cache.select_category(200228, "anonymous", TTL, T0).rowid == rowid
    assert cache.select_category(200358, "anonymous", TTL, T0) is None
    res = cache.resolve_category("c200358")
    assert res.kind == "ok" and res.category_id == 200228 and res.via_alias


def test_family_enriches_seven_siblings_from_one_write(cache, anon_env):
    cache.write_category(anon_env, tier="anonymous", scored=False, fetched_at=days(1))
    rows = cache.list_categories(family=28978)
    assert len(rows) == 8
    by_id = {r["category_id"]: r for r in rows}
    own = by_id[37162]
    assert own["display_name"] == "French-Door Refrigerators"
    assert own["slug"] == "french-door-refrigerator"
    assert own["canonical_url"].endswith("/french-door-refrigerator/c37162/")
    assert own["franchise"] == "appliances"
    assert (own["score_min"], own["score_max"], own["rated_count"]) == (43, 79, 172)
    assert own["family_id"] == 28978 and own["family_name"] == "Refrigerators"
    assert [g["id"] for g in own["groups"]] == [200367, 200369, 200371]
    sib = by_id[28722]
    assert sib["source"] == "payload" and sib["groups"] is None
    assert sib["canonical_url"].endswith("/top-freezer-refrigerator/c28722/")
    assert sib["slug"] == "top-freezer-refrigerator" and sib["franchise"] == "appliances"
    assert sib["has_reliability_data"] is True
    assert by_id[29738]["has_reliability_data"] is False


def test_a_slug_id_in_cats_does_not_kill_the_write(cache, anon_env):
    """CR's `args.cats[]` mixes category ids with PRODUCT-TYPE entries whose `id` is a slug.
    Measured 2026-09-06: Front-Load Washers `c28739` and Electric Dryers `c30562` both ship
    `id: "washer-dryer-pairs"`, and `int()` on it raised out of `write_category`, out of
    `_fetch_category` and out of the tool — so those two categories answered nothing at all,
    for every caller, on every call. A slug keys no category, so it is skipped; the integer
    siblings beside it must still enrich."""
    env = json.loads(json.dumps(anon_env))
    cats = env["filter_instance"]["args"]["cats"]
    cats.append(
        {"id": "washer-dryer-pairs", "typeURL": "/appliances/washer-dryer-pairs/", "name": "Pairs"}
    )
    cats.append({"id": None, "typeURL": "/appliances/nothing/"})

    cache.write_category(env, tier="anonymous", scored=False, fetched_at=days(1))

    rows = {r["category_id"]: r for r in cache.list_categories(family=28978)}
    assert 37162 in rows  # the write happened at all
    assert rows[28722]["canonical_url"].endswith("/top-freezer-refrigerator/c28722/")
    assert all(isinstance(cid, int) for cid in rows)


def test_a_slug_product_id_does_not_break_row_containment(cache, anon_env):
    """The same shape one layer down: `_row_contains` compared `int(p["id"])`, so a product
    CR ships with a non-numeric id raised inside `cr_product`'s cache lookup."""
    env = json.loads(json.dumps(anon_env))
    products = env["filter_instance"]["data"]
    if isinstance(products, dict):
        products = list(products.values())
    if products:
        products[0]["id"] = "not-a-number"
    rowid = cache.write_category(env, tier="anonymous", scored=False, fetched_at=days(1))
    assert cache._row_contains(rowid, 999999) is False


def test_score_range_status_three_values(cache, anon_env, c37162):
    cache.upsert_category_index(
        [{"id": 28722, "path": "/appliances/refrigerators/top-freezer-refrigerator/c28722/"}],
        "az",
        days(2),
    )
    assert cache.index_row(28722)["score_range_status"] == "not_fetched"
    cache.write_category(anon_env, tier="anonymous", scored=False, fetched_at=days(1))
    assert cache.index_row(28722)["score_range_status"] == "known"
    assert (cache.index_row(28722)["score_min"], cache.index_row(28722)["score_max"]) == (28, 82)
    assert cache.index_row(28722)["source"] == "az"  # source never changes
    assert cache.index_row(29738)["score_range_status"] == "none_published"
    assert cache.index_row(37162)["score_range_status"] == "known"
    # a wrapper-less fetch must not claim none_published for anyone
    bare = fixture_envelope(c37162, with_wrapper=False)
    cache.upsert_category_index(
        [{"id": 28719, "path": "/appliances/refrigerators/bottom-freezer-refrigerator/c28719/"}],
        "sitemap",
        days(2),
    )
    cache.write_category(bare, tier="anonymous", scored=False, fetched_at=T0)
    row = cache.index_row(28719)
    assert row["score_range_status"] == "known"  # kept from the earlier write
    assert (row["score_min"], row["score_max"]) == (56, 85)  # and so is the range itself
    # a wrapper-less write first-sighting a sibling leaves it honestly not_fetched
    bare2 = fixture_envelope("category_c37154_banks.json", with_wrapper=False)
    cache.write_category(bare2, tier="anonymous", scored=False, fetched_at=T0)
    sib = cache.index_row(200160)
    assert sib is not None and sib["score_range_status"] == "not_fetched"
    assert sib["score_min"] is None and sib["source"] == "payload"


def test_reliability_fanout_writes_row_per_named_sibling(cache, reliability_fixture):
    payload = reliability_fixture["init_store"]
    n = cache.write_reliability([37162, 28722, 29738], payload, days(1), "https://x/rel/")
    assert n == 3
    for cid in (37162, 28722, 29738):
        sel = cache.select_reliability(cid, TTL, T0)
        assert sel is not None and sel.stale is False
    assert cache.select_reliability(28721, TTL, T0) is None
    assert cache.load_reliability(cache.select_reliability(28722, TTL, T0).rowid) == payload


# --------------------------------------------------------------------------- P3.3 selection


def _seed(cache, rows, anon_env, member_env):
    ids = []
    for tier, scored, age in rows:
        env = member_env if tier == "member" else anon_env
        ids.append(cache.write_category(env, tier=tier, scored=scored, fetched_at=days(age)))
    return ids


@pytest.mark.parametrize(
    ("rows", "session_effective", "expect"),
    [
        ([("anonymous", False, 1)], "anonymous", ("hit", "anonymous", 1, None)),
        ([("anonymous", False, 1)], "member", ("miss", None, None, None)),  # unverified/active
        (
            [("member", True, 10), ("anonymous", False, 1)],
            "anonymous",
            ("hit", "member", 10, 1),
        ),
        ([("member", True, 10), ("anonymous", False, 1)], "member", ("hit", "member", 10, 1)),
        (
            [("member", False, 10), ("anonymous", False, 1)],
            "anonymous",
            ("hit", "anonymous", 1, None),
        ),
        ([("member", False, 10), ("anonymous", False, 1)], "member", ("hit", "member", 10, None)),
        ([("member", True, 40)], "member", ("stale", "member", 40, None)),
        (
            [("anonymous", True, 5), ("anonymous", False, 1)],
            "anonymous",
            ("hit", "anonymous", 5, 1),
        ),
    ],
)
def test_mixed_tier_matrix(cache, anon_env, member_env, rows, session_effective, expect):
    _seed(cache, rows, anon_env, member_env)
    sel = cache.select_category(37162, session_effective, TTL, T0)
    kind, tier, age, superseded_age = expect
    if kind == "miss":
        assert sel is None
        return
    assert sel is not None
    assert sel.tier == tier
    assert sel.fetched_at == cache_mod.to_iso(days(age))
    assert sel.stale is (kind == "stale")
    if superseded_age is None:
        assert sel.superseded_at is None
    else:
        assert sel.superseded_at == cache_mod.to_iso(days(superseded_age))


def test_expired_cookie_hits_anonymous_row(cache, anon_env, member_env):
    # session `expired` → effective anonymous → a dead cookie is never worse than none
    _seed(cache, [("anonymous", False, 1)], anon_env, member_env)
    assert cache.select_category(37162, "anonymous", TTL, T0) is not None


def test_unverified_misses_on_anonymous_only(cache, anon_env, member_env):
    _seed(cache, [("anonymous", False, 1)], anon_env, member_env)
    assert cache.select_category(37162, "member", TTL, T0) is None
    assert cache.any_row(37162, TTL, T0).qualified is False  # but it is still servable


def test_superseded_at_set_only_when_newer_unscored_exists(cache, anon_env, member_env):
    _seed(cache, [("anonymous", False, 5), ("member", True, 1)], anon_env, member_env)
    sel = cache.select_category(37162, "anonymous", TTL, T0)
    assert sel.tier == "member" and sel.superseded_at is None


def test_fetched_at_is_served_rows_not_now(cache, anon_env, member_env):
    _seed(cache, [("member", True, 10), ("anonymous", False, 1)], anon_env, member_env)
    sel = cache.select_category(37162, "anonymous", TTL, T0)
    assert sel.fetched_at == cache_mod.to_iso(days(10))


def test_prune_keeps_newest_scored_row_even_if_oldest(cache, anon_env, member_env):
    ids = _seed(
        cache,
        [("member", True, 200), ("anonymous", False, 150), ("anonymous", False, 1)],
        anon_env,
        member_env,
    )
    deleted = cache.prune(T0, TTL)  # cutoff = 90 days
    assert deleted == 1
    sel = cache.select_category(37162, "anonymous", TTL, T0)
    assert sel.rowid == ids[0] and sel.scored and sel.stale
    assert cache.load_envelope(ids[0])
    with pytest.raises(KeyError):
        cache.load_envelope(ids[1])


def test_product_row_must_contain_product_and_prunes_dangling_index(cache, c37162, member_env):
    # member row with all 8 products, then an anonymous row where MODEL-008 has been removed
    r_member = cache.write_category(member_env, tier="member", scored=True, fetched_at=days(20))
    trimmed = fixture_envelope(c37162)
    trimmed["filter_instance"] = dict(trimmed["filter_instance"])
    trimmed["filter_instance"]["data"] = {
        k: v for k, v in trimmed["filter_instance"]["data"].items() if k != "500008"
    }
    r_anon = cache.write_category(trimmed, tier="anonymous", scored=False, fetched_at=days(1))
    sel = cache.select_product_row(500008, "anonymous", TTL, T0)
    assert sel.rowid == r_member  # the only row that still contains it
    sel2 = cache.select_product_row(500001, "anonymous", TTL, T0)
    assert sel2.rowid == r_member and sel2.scored  # newest scored containing it
    assert sel2.superseded_at == cache_mod.to_iso(days(1))
    # an index entry whose payload no longer exists anywhere is pruned on read
    cache.prune(T0 + timedelta(days=400), TTL)  # removes the anonymous row, keeps the scored one
    assert cache.select_product_row(500001, "anonymous", TTL, T0).rowid == r_member
    cache2 = Cache(cache.db_path)
    with cache2._connect() as conn:
        conn.execute("INSERT INTO product_index VALUES (999999, 37162, 'x', 'y')")
    assert cache2.select_product_row(999999, "anonymous", TTL, T0) is None
    with cache2._connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM product_index WHERE product_id=999999").fetchone()[0]
            == 0
        )
    _ = r_anon


def test_product_row_containment_is_parsed_not_substring(cache, c37162):
    env = fixture_envelope(c37162)
    fi = dict(env["filter_instance"])
    fi["data"] = list(fi["data"].values())  # list-shaped data: same products, no '"id":{' needle
    env = dict(env, filter_instance=fi)
    rowid = cache.write_category(env, tier="anonymous", scored=False, fetched_at=days(1))
    sel = cache.select_product_row(500001, "anonymous", TTL, T0)
    assert sel is not None and sel.rowid == rowid
    with cache._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM product_index WHERE category_id=37162").fetchone()[0]
    assert n == 8  # nothing was pruned


def test_bad_effective_tier_is_rejected(cache, anon_env):
    cache.write_category(anon_env, tier="anonymous", scored=False, fetched_at=days(1))
    for bad in ("anon", "", None):
        with pytest.raises(ValueError):
            cache.select_category(37162, bad, TTL, T0)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            cache.select_product_row(500001, bad, TTL, T0)  # type: ignore[arg-type]


def test_lru_serves_the_round_trip_not_the_input(cache, anon_env):
    rowid = cache.write_category(anon_env, tier="anonymous", scored=False, fetched_at=days(1))
    loaded = cache.load_envelope(rowid)
    assert loaded == anon_env and loaded is not anon_env


def test_resolve_cached_payload_without_index_row_carries_its_url(cache, anon_env):
    cache.write_category(anon_env, tier="anonymous", scored=False, fetched_at=days(1))
    with cache._connect() as conn:
        conn.execute("DELETE FROM category_index")
    res = cache.resolve_category(37162)
    assert res.kind == "ok" and res.row["canonical_url"].endswith("/c37162/")
    assert res.row["slug"] == "french-door-refrigerator"


def test_product_row_non_qualifying_is_flagged(cache, anon_env):
    cache.write_category(anon_env, tier="anonymous", scored=False, fetched_at=days(1))
    sel = cache.select_product_row(500001, "member", TTL, T0)
    assert sel is not None and sel.qualified is False


# --------------------------------------------------------------------------- P3.4 misc


def test_resolve_accepts_cNNNN_int_str_slug_alias(cache, anon_env):
    cache.upsert_category_index(
        [
            {
                "id": 37162,
                "path": "/appliances/refrigerators/french-door-refrigerator/c37162/",
                "display_name": "French-Door Refrigerators",
            },
            {"id": 28700, "path": "/electronics-computers/tvs/c28700/"},
        ],
        "az",
        days(1),
    )
    for token in (
        "c37162",
        "C37162",
        37162,
        "37162",
        "french-door-refrigerator",
        "French Door Refrigerator",
        "french_door_refrigerator",
        "French-Door Refrigerators",
    ):
        res = cache.resolve_category(token)
        assert res.kind == "ok" and res.category_id == 37162, token
    assert cache.resolve_category("tvs").category_id == 28700
    assert cache.resolve_category("c99999").kind == "unknown"
    assert cache.resolve_category("mattresses").kind == "unknown"
    cache.write_category(
        fixture_envelope("category_c200228.json"),
        tier="anonymous",
        scored=False,
        fetched_at=days(1),
    )
    assert cache.resolve_category(200358).category_id == 200228


def test_superseded_slug_keeps_resolving(cache):
    """The A-Z pass publishes a slug synchronously; the sitemap pass then wins with a shorter
    canonical URL and a different slug. Without an alias, a slug the server already handed out
    starts answering `unknown_category / not_in_index` — "CR does not rate it"."""
    cache.upsert_category_index(
        [{"id": 200854, "path": "/money/car-travel/car-rental-companies/c200854/"}], "az", days(1)
    )
    assert cache.resolve_category("car-rental-companies").category_id == 200854

    cache.upsert_category_index(
        [{"id": 200854, "path": "/money/car-travel/c200854/"}], "sitemap", days(1)
    )
    row = cache.index_row(200854)
    assert row["slug"] == "car-travel"  # the sitemap's canonical path still wins
    assert cache.resolve_category("car-travel").category_id == 200854
    assert cache.resolve_category("car-rental-companies").category_id == 200854

    # a live slug always beats a retired one, and an alias never invents a category
    cache.upsert_category_index(
        [{"id": 400001, "path": "/money/car-rental-companies/c400001/"}], "az", days(1)
    )
    assert cache.resolve_category("car-rental-companies").category_id == 400001
    assert cache.resolve_category("no-such-slug").kind == "unknown"


def test_resolve_ambiguous_slug_lists_candidates(cache):
    cache.upsert_category_index(
        [
            {
                "id": 1001,
                "path": "/home-garden/paint/interior-paint/c1001/",
                "display_name": "Interior Paints",
            },
            {
                "id": 1002,
                "path": "/home-garden/coatings/interior-paint/c1002/",
                "display_name": "Interior Paint (Pro)",
            },
        ],
        "sitemap",
        days(1),
    )
    res = cache.resolve_category("interior-paint")
    assert res.kind == "ambiguous"
    assert [c["id"] for c in res.candidates] == [1001, 1002]
    assert all({"id", "slug", "name"} <= set(c) for c in res.candidates)


def test_search_categories_matches_slug_without_display_name(cache):
    cache.upsert_category_index(
        [
            {"id": 28700, "path": "/electronics-computers/tvs/c28700/"},  # sitemap-only, no name
            {"id": 28705, "path": "/home-garden/mattresses/c28705/"},
            {
                "id": 34706,
                "path": "/money/mattress-stores/c34706/",
                "display_name": "Mattress Stores",
            },
        ],
        "sitemap",
        days(1),
    )
    assert [r["category_id"] for r in cache.search_categories("tvs")] == [28700]
    hits = [r["category_id"] for r in cache.search_categories("mattress")]
    assert hits == [34706, 28705]  # no exact match → alphabetical ("mattress stores" first)
    assert [r["category_id"] for r in cache.search_categories("mattresses")] == [28705]
    assert cache.search_categories("television") == []  # substring, no synonyms locally


def test_search_products_labels_provenance(cache, anon_env):
    cache.write_category(anon_env, tier="anonymous", scored=False, fetched_at=days(1))
    hits = cache.search_products("model-001")
    assert len(hits) == 1
    h = hits[0]
    assert h["id"] == 500001 and h["category_id"] == 37162
    assert h["data_tier"] == "anonymous" and h["fetched_at"] == cache_mod.to_iso(days(1))
    assert h["category_slug"] == "french-door-refrigerator"
    assert [x["id"] for x in cache.search_products("Brand A")] == [500001, 500003, 500006]
    assert cache.search_products("MODEL-00") and not cache.search_products("MODEL-009")
    assert cache.cached_category_ids() == [
        {"id": 37162, "slug": "french-door-refrigerator", "name": "French-Door Refrigerators"}
    ]


def test_lru_parses_once(cache, anon_env, monkeypatch):
    rowid = cache.write_category(anon_env, tier="anonymous", scored=False, fetched_at=days(1))
    fresh = Cache(cache.db_path)  # cold LRU
    calls = []
    real = json.loads

    def counting(s, *a, **k):
        calls.append(len(s))
        return real(s, *a, **k)

    monkeypatch.setattr(cache_mod.json, "loads", counting)
    a = fresh.load_envelope(rowid)
    b = fresh.load_envelope(rowid)
    assert a is b and len(calls) == 1


def test_discovery_runs_and_mark_dead(cache):
    cache.record_discovery_run("az", days(1), 236)
    cache.record_discovery_run("sitemap", T0, 346)
    st = cache.discovery_status()
    assert st["az"]["count"] == 236 and st["sitemap"]["fetched_at"] == cache_mod.to_iso(T0)
    cache.upsert_category_index([{"id": 33007, "path": "/x/y/c33007/"}], "sitemap", T0)
    cache.mark_dead(33007, T0)
    row = cache.index_row(33007)
    assert row["dead_at"] == cache_mod.to_iso(T0)
    assert cache.resolve_category(33007).dead is True
    assert 33007 not in {r["category_id"] for r in cache.list_categories()}
    # a later discovery sighting resurrects it
    cache.upsert_category_index([{"id": 33007, "path": "/x/y/c33007/"}], "sitemap", T0)
    assert cache.index_row(33007)["dead_at"] is None


def test_index_prefers_canonical_url_and_az_name(cache):
    cache.upsert_category_index(
        [
            {
                "id": 200228,
                "path": "/health/milk-milk-alternatives/plant-milk/c200228/",
                "display_name": "Plant Milk",
            }
        ],
        "az",
        days(2),
    )
    cache.upsert_category_index(
        [{"id": 200228, "path": "/health/milk-milk-alternatives/c200228/"}], "sitemap", days(1)
    )
    row = cache.index_row(200228)
    assert row["canonical_url"].endswith("/health/milk-milk-alternatives/c200228/")
    assert row["slug"] == "milk-milk-alternatives" and row["display_name"] == "Plant Milk"
    assert row["source"] == "az" and row["franchise"] == "health"


def test_cars_tables(cache):
    rows = [
        {
            "model_year_id": 700001,
            "make_id": 1,
            "make": "Make A",
            "slug_make": "make-a",
            "model_id": 2,
            "model": "Model X",
            "slug_model": "model-x",
            "year": 2026,
            "states": ["New"],
            "car_types": [105, 116],
            "primary_car_type_id": 105,
        },
        {
            "model_year_id": 700002,
            "make_id": 1,
            "make": "Make A",
            "slug_make": "make-a",
            "model_id": 2,
            "model": "Model X",
            "slug_model": "model-x",
            "year": 2025,
            "states": ["New", "Used"],
            "car_types": [105],
            "primary_car_type_id": 105,
        },
    ]
    assert cache.write_car_index(rows, T0) == 2
    assert cache.car_index_status()["count"] == 2
    assert cache.car_index_row(700001)["car_types"] == [105, 116]
    hits = cache.search_cars("model x")
    assert [h["model_year_id"] for h in hits] == [700001, 700002]  # year desc
    assert cache.search_cars("make a 2025")[0]["model_year_id"] == 700002
    cache.write_car_taxonomy({"content": []}, days(1))
    tax, fetched, stale = cache.select_car_taxonomy(90, T0)
    assert tax == {"content": []} and not stale
    rid = cache.write_car_raw(700001, {"response": {"x": 1}}, days(40))
    sel = cache.select_car_raw(700001, TTL, T0)
    assert sel.rowid == rid and sel.stale is True
    assert cache.load_car_raw(rid) == {"response": {"x": 1}}
    assert cache.select_car_raw(700009, TTL, T0) is None


def test_reliability_url_from_cache(cache, anon_env):
    assert cache.reliability_url_from_cache(28722) is None
    cache.write_category(anon_env, tier="anonymous", scored=False, fetched_at=days(1))
    assert cache.index_row(28722)["reliability_url"].endswith("/reliability/c28722/")
    url = cache.reliability_url_from_cache(28722)
    assert (
        url
        == "https://www.consumerreports.org/appliances/refrigerators/top-freezer-refrigerator/reliability/c28722/"
    )
    assert cache.reliability_url_from_cache(37162).endswith("/reliability/c37162/")
    assert cache.reliability_url_from_cache(99999) is None


# --------------------------------------------------------------------------- checked_at / dedupe


def test_checked_at_is_the_newest_qualifying_row_not_the_served_one(cache, anon_env, member_env):
    _seed(cache, [("member", True, 40), ("anonymous", False, 1)], anon_env, member_env)
    sel = cache.select_category(37162, "anonymous", TTL, T0)
    assert sel.tier == "member" and sel.stale is True  # the SERVED row is past the TTL
    assert sel.checked_at == cache_mod.to_iso(days(1))  # but the category was checked yesterday
    # a member caller cannot use the anonymous row, so their last check IS the member row
    sel_m = cache.select_category(37162, "member", TTL, T0)
    assert sel_m.checked_at == cache_mod.to_iso(days(40))
    assert cache.any_row(37162, TTL, T0).checked_at == cache_mod.to_iso(days(1))


def test_product_checked_at_is_the_category_not_the_containing_rows(cache, c37162, member_env):
    r_member = cache.write_category(member_env, tier="member", scored=True, fetched_at=days(40))
    trimmed = fixture_envelope(c37162)
    trimmed["filter_instance"] = dict(trimmed["filter_instance"])
    trimmed["filter_instance"]["data"] = {
        k: v for k, v in trimmed["filter_instance"]["data"].items() if k != "500008"
    }
    cache.write_category(trimmed, tier="anonymous", scored=False, fetched_at=days(1))
    sel = cache.select_product_row(500008, "anonymous", TTL, T0)
    assert sel.rowid == r_member and sel.stale is True
    assert sel.checked_at == cache_mod.to_iso(days(1))  # the category was looked at yesterday
    # a non-qualifying pick reports the newest row of any tier as the last look
    cache2 = Cache(cache.db_path.parent / "b.db")
    cache2.write_category(trimmed, tier="anonymous", scored=False, fetched_at=days(3))
    sel2 = cache2.select_product_row(500001, "member", TTL, T0)
    assert sel2.qualified is False and sel2.checked_at == cache_mod.to_iso(days(3))


def test_write_category_dedupes_identical_payload_inside_the_ttl(cache, anon_env, member_env):
    a = cache.write_category(
        anon_env, tier="anonymous", scored=False, fetched_at=days(2), ttl_days=TTL
    )
    b = cache.write_category(
        anon_env, tier="anonymous", scored=False, fetched_at=days(1), ttl_days=TTL
    )
    assert a == b
    # a different tier is a different row even when the bytes agree
    m = cache.write_category(
        anon_env, tier="member", scored=False, fetched_at=days(1), ttl_days=TTL
    )
    assert m != a
    # different bytes at the same tier append
    c = cache.write_category(
        member_env, tier="anonymous", scored=True, fetched_at=days(1), ttl_days=TTL
    )
    assert c not in (a, m)
    # past the TTL the identical payload IS appended — the row is the record of the check
    d = cache.write_category(
        anon_env, tier="anonymous", scored=False, fetched_at=T0 + timedelta(days=40), ttl_days=TTL
    )
    assert d not in (a, m, c)
    # and without a TTL the store appends unconditionally (the repository owns the TTL)
    e = cache.write_category(
        anon_env, tier="anonymous", scored=False, fetched_at=T0 + timedelta(days=40)
    )
    assert e != d
    with cache._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM category_raw").fetchone()[0] == 5


def test_write_reliability_dedupes_identical_payload_inside_the_ttl(cache, reliability_fixture):
    payload = reliability_fixture["init_store"]
    assert cache.write_reliability([37162, 28722], payload, days(2), "u", ttl_days=TTL) == 2
    # only the sibling that has no such row yet is written
    assert cache.write_reliability([37162, 28722, 29738], payload, days(1), "u", ttl_days=TTL) == 1
    with cache._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reliability_raw").fetchone()[0] == 3
    assert cache.select_reliability(37162, TTL, T0).fetched_at == cache_mod.to_iso(days(2))
    # past the TTL it is appended again
    later = T0 + timedelta(days=40)
    assert cache.write_reliability([37162], payload, later, "u", ttl_days=TTL) == 1


# --------------------------------------------------------------------------- write locking


def _interleaving_connect(monkeypatch, outcomes: list[str], *, busy_ms: int = 200):
    """Route `Cache._connect` through a connection whose transaction's FIRST read is followed,
    before the next statement, by another connection's committed write — the cross-process
    interleaving (a second MCP host, `consumer-reports-mcp auth` beside the server) under
    which a deferred transaction's later write fails with SQLITE_BUSY_SNAPSHOT at once, busy
    handler or no busy handler. The other writer's fate lands in `outcomes`."""
    import sqlite3

    real_connect = sqlite3.connect

    class Interleaving(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._armed = False

        def execute(self, sql, *args):
            cur = super().execute(sql, *args)
            head = sql.lstrip().upper()
            if head.startswith("BEGIN"):
                self._armed = True
            elif head.startswith(("COMMIT", "ROLLBACK")):
                self._armed = False
            elif self._armed and head.startswith("SELECT"):
                self._armed = False
                path = super().execute("PRAGMA database_list").fetchone()[2]
                other = real_connect(path, isolation_level=None, timeout=busy_ms / 1000)
                try:
                    other.execute(f"PRAGMA busy_timeout={busy_ms}")
                    other.execute(
                        "INSERT INTO car_taxonomy (fetched_at, payload_json) "
                        "VALUES ('2026-01-01T00:00:00Z', '{}')"
                    )
                    outcomes.append("committed")
                except sqlite3.OperationalError:
                    outcomes.append("locked")
                finally:
                    other.close()
            return cur

    monkeypatch.setattr(
        cache_mod.sqlite3,
        "connect",
        lambda *a, **kw: real_connect(*a, factory=Interleaving, **kw),
    )


@pytest.mark.parametrize("op", ["prune", "upsert_category_index", "write_reliability"])
def test_read_then_write_transactions_hold_the_write_lock_from_the_start(
    cache, anon_env, member_env, monkeypatch, op
):
    """`BEGIN IMMEDIATE`, never a deferred `BEGIN`, for a transaction that reads before it
    writes: in WAL mode the upgrade after another connection has committed is
    SQLITE_BUSY_SNAPSHOT — immediate, and the busy handler is not consulted ("database is
    locked" after 0.000 s despite `busy_timeout=30000`). Cross-process only, and `prune` runs
    at every startup since batch 1 wired it into the lifespan."""
    _seed(cache, [("member", True, 200), ("anonymous", False, 150)], anon_env, member_env)
    outcomes: list[str] = []
    _interleaving_connect(monkeypatch, outcomes)
    if op == "prune":
        assert cache.prune(T0, TTL) == 1
    elif op == "upsert_category_index":
        rows = [{"id": 37162, "path": "/appliances/refrigerators/x/c37162/"}]
        assert cache.upsert_category_index(rows, "az", T0) == 1
    else:
        assert cache.write_reliability([37162], {"a": 1}, T0, None, ttl_days=TTL) == 1
    assert outcomes == ["locked"]  # the other writer waited on OUR lock, then gave up


def test_every_multi_statement_transaction_begins_immediate():
    source = inspect.getsource(cache_mod)
    assert 'execute("BEGIN")' not in source
    assert source.count('execute("BEGIN IMMEDIATE")') == 6


def test_row_id_is_the_key_grammar_for_product_and_model_year_tokens():
    """`cache.row_id` owns "what can be a product / model-year key" as `_CID` owns the category
    token: a positive integer SQLite can bind, from an int or ASCII digits. Everything else is
    None — never an exception, and never a value the bind would raise on."""
    from consumer_reports_mcp.cache import SQLITE_INT_MAX, row_id

    assert row_id(700001) == 700001 and row_id("700001") == 700001 and row_id(" 42 ") == 42
    assert row_id(SQLITE_INT_MAX) == SQLITE_INT_MAX
    for junk in (0, -1, True, False, None, 1.5, "²", "①", "c700001", "1" * 5000, "9" * 20):
        assert row_id(junk) is None, junk if not isinstance(junk, str) else junk[:10]
    assert row_id(SQLITE_INT_MAX + 1) is None and row_id(10**20) is None


def test_category_token_grammar_is_ascii_and_bounded():
    """`_CID` matches `c?` + 4–7 ASCII digits and nothing else: `\\d` would admit `'²'`, which
    `int()` rejects, and an unbounded run would overflow the bind."""
    from consumer_reports_mcp.cache import _CID

    assert _CID.match("c37162") and _CID.match("C37162") and _CID.match("37162")
    assert _CID.match("1234") and _CID.match("1234567")
    for junk in ("c12", "123", "12345678", "²", "c²", "①", "9" * 20, "1" * 5000, "c 37162"):
        assert _CID.match(junk) is None, junk[:10]
