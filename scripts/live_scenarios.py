"""Drive every tool against Consumer Reports the way a member does — see VALIDATION.md.

    uv run --no-sync scripts/live_scenarios.py [--refresh] [--anonymous]

Uses the same settings, `session.json` and cache as the server. A warm cache answers most checks
without a request — that proves the envelopes, not CR's pages — so `--refresh` sends the FIRST
call on every category, car and survey to CR and requires it to come back `from_cache: false`.
`--anonymous` runs with no cookie against a cache of its own under `<cache_dir>/anonymous/`: a
member row answers ANY caller (SPEC §8), so the server's cache would hand this run the member's
scores. Prints one line per check and exits 1 on any failure. Opens no browser: the sign-in
check only confirms `cr_sign_in` is refused for a live cookie. Roughly 30 requests on a refresh,
spaced by the configured politeness interval.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from consumer_reports_mcp.auth_tools import cr_auth_status, cr_sign_in
from consumer_reports_mcp.cars.tools import cr_car, cr_car_search, cr_cars
from consumer_reports_mcp.config import Settings
from consumer_reports_mcp.credentials import CredentialStore, default_store
from consumer_reports_mcp.reliability import cr_reliability
from consumer_reports_mcp.runtime import build_runtime, configure_logging
from consumer_reports_mcp.tools_products import (
    cr_categories,
    cr_filters,
    cr_product,
    cr_ratings,
    cr_search,
)

FRENCH_DOOR = "c37162"
DISHWASHERS = "c28687"  # sitemap-only id
FRONT_LOAD_WASHERS = "c28739"  # args.cats[] mixes ids and slugs
TVS = "c28700"  # 303 products
MATTRESSES = "c28705"  # 14 MB page
DISPLAY_GROUP = "c200369"  # a display-group id, answered with its owner
TOP_LOAD_WASHERS = {"c32002", "c37107"}  # index rows whose names say "Washers", not "washing"

# what the anonymous cache is seeded WITH: the discovery index (the sitemap walk behind it is
# ~200 requests) — and never a fetched row, of any tier
PAYLOAD_TABLES = ("category_raw", "product_index", "reliability_raw", "car_raw")


def anonymous_settings(base: Settings) -> tuple[Settings, Path | None]:
    """A cache of the anonymous run's own, and where it was seeded from (None: not this time).

    SPEC §8 rule 1 — a member row answers any caller — is right for the server (a member who
    logs out keeps the scores they fetched) and makes a shared cache useless here: every products
    check would be answered with the member's row, labelled `member`. First use copies the
    server cache's index tables and drops every row; later runs find their own anonymous rows
    there, as the member run finds member rows in the server cache."""
    settings = Settings(home=base.home, env={"CR_CACHE_DIR": str(base.cache_dir / "anonymous")})
    if settings.db_path.exists() or not base.db_path.exists():
        return settings, None
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(base.db_path)
    dst = sqlite3.connect(settings.db_path)
    try:
        src.backup(dst)
        with dst:
            for table in PAYLOAD_TABLES:
                dst.execute(f"DELETE FROM {table}")
        dst.execute("VACUUM")
    finally:
        src.close()
        dst.close()
    return settings, base.db_path


class Failure(AssertionError):
    pass


def expect(cond: Any, what: str) -> None:
    if not cond:
        raise Failure(what)


def d(model: Any) -> dict:
    return model.model_dump(mode="json")


def products_of(data: dict | None) -> list[dict]:
    if not data:
        return []
    out: list[dict] = []
    if isinstance(data.get("products"), list):
        out += data["products"]
    if isinstance(data.get("product"), dict):  # cr_product: one product under `product`
        out.append(data["product"])
    for g in data.get("groups") or []:
        out += g.get("products") or []
    return out


class Run:
    def __init__(self, rt: Any, member: bool, *, refresh: bool = False) -> None:
        self.rt = rt
        self.member = member
        self.refresh = refresh
        self._touched: set[str] = set()
        self.results: list[tuple[str, bool, str]] = []

    def fresh(self, key: str) -> bool:
        """Whether THIS call carries `refresh=True`: on a `--refresh` run, the first call per
        category, car or survey. A second inside the 300 s cooldown is answered
        `refresh_skipped`, which `honest` treats as the failure it would be."""
        if not self.refresh or key in self._touched:
            return False
        self._touched.add(key)
        return True

    async def check(self, name: str, fn: Callable[[], Awaitable[str | None]]) -> None:
        t0 = time.monotonic()
        try:
            note = await fn() or ""
            ok = True
        except Failure as exc:
            note, ok = str(exc), False
        except Exception as exc:  # a crash is a failure with the type named
            note, ok = f"CRASH {type(exc).__name__}: {exc}", False
        self.results.append((name, ok, note))
        print(f"{'PASS' if ok else 'FAIL'}  {name:<48} {time.monotonic() - t0:5.1f}s  {note}")

    # --- the paywall-honesty rules every products envelope must satisfy -------------------
    def honest(self, env: Any, *, rows_expected: bool = True, live: bool = False) -> dict:
        e = d(env)
        expect(e["error"] is None, f"error: {e['error']}")
        for w in e["warnings"]:
            expect(not w.startswith("session_expiring"), f"unexpected {w}")
            expect(not w.startswith("refresh_"), f"the refresh did not reach CR: {w}")
        if live:  # a `--refresh` first call: CR's page, not a row, must have answered
            prov = e.get("provenance") or {}
            expect(prov.get("from_cache") is False, f"served from the cache on a refresh: {prov}")
        if not rows_expected:
            return e
        tier = "member" if self.member else "anonymous"
        expect(e["auth_state"] == tier, f"auth_state {e['auth_state']!r} != {tier!r}")
        sa = e["scores_available"]["overall_score"]
        expect(
            sa == ("available" if self.member else "unavailable"),
            f"scores_available.overall_score {sa!r}",
        )
        products = products_of(e["data"])
        scored = [p for p in products if p.get("overall_score") is not None]
        if self.member:
            expect(scored, "no product carries a score on a member row")
            expect(
                e["data"].get("notice") in (None, ""),
                f"notice on member data: {e['data'].get('notice')}",
            )
        else:
            expect(not scored, "a score leaked into an anonymous row")
            notice = e["data"].get("notice") or ""
            expect(notice, "anonymous data without a notice")
            expect("cr_sign_in" not in notice, "the anonymous notice prompts for sign-in")
        return e


async def main(argv: list[str]) -> int:
    anonymous = "--anonymous" in argv
    refresh = "--refresh" in argv
    configure_logging()
    settings = Settings()
    store = default_store(settings.config_dir, env={})
    seeded_from: Path | None = None
    if anonymous:
        store = CredentialStore(settings.config_dir / "no-such-session.json", env={})
        settings, seeded_from = anonymous_settings(settings)
    rt = build_runtime(settings, env={}, credentials=store)
    member = not anonymous and store.has_durable
    seeded = f"  (seeded from {seeded_from})" if seeded_from else ""
    print(
        f"mode: {'member' if member else 'anonymous'}  refresh: {'yes' if refresh else 'no'}  "
        f"cache: {settings.cache_dir}{seeded}"
    )
    run = Run(rt, member, refresh=refresh)
    try:
        await rt.discovery.ensure_az_index()
        await scenarios(run)
    finally:
        await rt.transport.aclose()
    failed = [r for r in run.results if not r[1]]
    print(f"\n{len(run.results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


async def scenarios(run: Run) -> None:
    rt = run.rt
    state: dict[str, Any] = {}

    # 1. French-door fridge under $2,500
    async def s1_search() -> str:
        out = d(await cr_search(rt, "french door refrigerator"))
        expect(out["error"] is None, out["error"])
        hits = out["data"]["categories"]
        expect(hits and hits[0]["id"] == FRENCH_DOOR, f"first hit {hits[:1]}")
        expect(hits[0]["match"] in ("exact", "full"), f"match {hits[0]['match']}")
        return f"{len(hits)} category hits, first {hits[0]['slug']} ({hits[0]['match']})"

    await run.check("search: french door refrigerator", s1_search)

    async def s1_ratings() -> str:
        live = run.fresh(FRENCH_DOOR)
        env = await cr_ratings(rt, FRENCH_DOOR, price_max=2500, recommended=True, refresh=live)
        e = run.honest(env, live=live)
        products = products_of(e["data"])
        expect(products, "no products")
        for p in products:
            expect(p["recommended"] is True, f"{p['id']} not recommended")
            price = p.get("price")
            expect(price is None or price <= 2500, f"{p['id']} priced {price}")
        state["fridge"] = products[0]["id"]
        return f"{len(products)} recommended under $2,500; auth_state {e['auth_state']}"

    await run.check("ratings: c37162 price_max recommended", s1_ratings)

    async def s1_product() -> str:
        fridge = state.get("fridge")
        expect(fridge, "no product id: the ratings step did not serve one")
        env = await cr_product(rt, fridge, include_descriptions=True)
        e = run.honest(env)
        p = e["data"]["product"]
        expect("dont_buy" in p, "dont_buy missing")
        expect(p.get("rank"), "rank missing")
        expect(len(p.get("attributes") or []) > 10, "few attributes")
        expect(any(a.get("description") for a in p["attributes"]), "no descriptions")
        return f"{p['brand']} {p['model']}: {len(p['attributes'])} attributes, rank {p['rank']}"

    await run.check("product: drilldown with descriptions", s1_product)

    async def s1_filters() -> str:
        env = await cr_filters(rt, FRENCH_DOOR)
        e = d(env)
        expect(e["error"] is None, e["error"])
        text = str(e)
        expect(len(text) < 40_000, f"{len(text)} chars — not range-collapsed?")
        ranged = [f for f in e["data"]["filters"] if isinstance(f.get("range"), dict)]
        expect(ranged, "no numeric filter collapsed to a range")
        return f"{len(e['data']['filters'])} filters, {len(ranged)} ranged, {len(text)} chars"

    await run.check("filters: c37162 collapsed", s1_filters)

    # 2. Dishwashers — sitemap-only id, plus brand reliability
    async def s2_ratings() -> str:
        live = run.fresh(DISHWASHERS)
        env = await cr_ratings(rt, DISHWASHERS, refresh=live)
        e = run.honest(env, live=live)
        return f"{len(products_of(e['data']))} rows served for a sitemap-only id"

    await run.check("ratings: c28687 (sitemap-only id)", s2_ratings)

    async def s2_reliability() -> str:
        live = run.fresh(f"reliability:{DISHWASHERS}")
        env = await cr_reliability(rt, DISHWASHERS, refresh=live)
        e = d(env)
        expect(e["error"] is None, e["error"])
        expect(e["auth_state"] == "anonymous", "reliability is one tier")
        expect("session" not in e, "reliability must omit session")
        if live:
            expect(e["provenance"]["from_cache"] is False, "survey served from the cache")
        expect(e["data"]["brands"], "no brands")
        return f"{len(e['data']['brands'])} brands surveyed"

    await run.check("reliability: c28687 brand surveys", s2_reliability)

    # 3. Quiet front-load washer — the slug-in-cats[] category, attribute projection
    async def s3_search() -> str:
        out = d(await cr_search(rt, "washing machines"))
        ids = [h["id"] for h in out["data"]["categories"]]
        expect(FRONT_LOAD_WASHERS in ids, f"front-load washers not among {ids[:5]}")
        # the index rows named "…Washers" are reached by the root rule, not by CR's label
        expect(TOP_LOAD_WASHERS <= set(ids), f"top-load washers missing from {ids}")
        return f"resolved via CR's label; hits {ids[:4]} (+{len(ids) - 4} more)"

    await run.check("search: washing machines (CR label)", s3_search)

    async def s3_ratings() -> str:
        live = run.fresh(FRONT_LOAD_WASHERS)
        env = await cr_ratings(rt, FRONT_LOAD_WASHERS, attributes=["Noise"], limit=5, refresh=live)
        e = run.honest(env, live=live)
        products = products_of(e["data"])
        expect(products, "no products")
        for p in products:
            proj = p.get("projected_attributes")
            expect(proj and len(proj) == 1, f"{p['id']}: projected {proj}")
        return f"{len(products)} rows, attribute projected on each"

    await run.check("ratings: c28739 attributes=[Noise]", s3_ratings)

    # 4. Every TV — paging past the 200 cap
    async def s4_paging() -> str:
        live = run.fresh(TVS)
        a = run.honest(
            await cr_ratings(rt, TVS, group_mode="flat", limit=200, refresh=live), live=live
        )
        b = run.honest(await cr_ratings(rt, TVS, group_mode="flat", limit=200, offset=200))
        ids_a = [p["id"] for p in products_of(a["data"])]
        ids_b = [p["id"] for p in products_of(b["data"])]
        expect(len(ids_a) == 200, f"page 1 has {len(ids_a)}")
        expect(ids_b and not set(ids_a) & set(ids_b), "pages overlap or page 2 empty")
        total = a["data"].get("total")
        expect(total == len(ids_a) + len(ids_b) or total is None, f"total {total}")
        return f"{len(ids_a)} + {len(ids_b)} = {total}"

    await run.check("ratings: c28700 flat paging (303)", s4_paging)

    # 5. Mattresses — the 14 MB page
    async def s5() -> str:
        live = run.fresh(MATTRESSES)
        e = run.honest(await cr_ratings(rt, MATTRESSES, limit=3, refresh=live), live=live)
        return f"served; groups {len(e['data'].get('groups') or [])}"

    await run.check("ratings: c28705 (14 MB page)", s5)

    # 6. Cars
    async def s6_search() -> str:
        out = d(await cr_car_search(rt, "Toyota RAV4"))
        expect(out["error"] is None, out["error"])
        cars = out["data"]["cars"]
        expect(cars, "no hits")
        expect("auth_state" not in out, "cars must not carry auth_state")
        newest = max(cars, key=lambda c: c.get("year") or 0)
        state["rav4"] = newest["model_year_id"]
        return f"{len(cars)} hits, newest {newest['year']} id {newest['model_year_id']}"

    await run.check("car_search: Toyota RAV4", s6_search)

    async def s6_car() -> str:
        rav4 = state.get("rav4")
        expect(rav4, "no model-year id: the search step did not serve one")
        live = run.fresh(f"car:{rav4}")
        out = d(await cr_car(rt, rav4, refresh=live))
        expect(out["error"] is None, out["error"])
        for w in out["warnings"]:
            expect(not w.startswith("refresh_"), f"the refresh did not reach CR: {w}")
        if live:
            expect(out["provenance"]["from_cache"] is False, "car served from the cache")
        sa = out["scores_available"]
        expect(sa and all(v in ("available", "absent") for v in sa.values()), f"scores {sa}")
        expect(out["session"] in ("active", "unverified", "none"), out["session"])
        return f"scores_available {sum(v == 'available' for v in sa.values())}/{len(sa)} available"

    await run.check("car: RAV4 model-year", s6_car)

    async def s6_cars_standard() -> str:
        out = d(
            await cr_cars(
                rt, make="toyota", year=2025, detail="standard", limit=3, refresh=run.refresh
            )
        )
        expect(out["error"] is None, out["error"])
        rows = out["data"]["cars"]
        expect(rows and len(rows) <= 3, f"{len(rows)} rows")
        expect(all(isinstance(r.get("states"), list) for r in rows), "states not a list")
        return f"{len(rows)} rows with road-test keys; total {out['data'].get('total')}"

    await run.check("cars: toyota 2025 standard", s6_cars_standard)

    async def s6_cars_new_suvs() -> str:
        out = d(await cr_cars(rt, car_type="suvs", state="new", limit=5))
        expect(out["error"] is None, out["error"])
        rows = out["data"]["cars"]
        expect(rows, "no rows")
        # `states` carries CR's own names (`modelYearStateName`: "New"/"Used"); the parameter
        # vocabulary is lowercase — compare case-insensitively
        expect(
            all([x.lower() for x in r["states"]] == ["new"] for r in rows),
            f"states {[r['states'] for r in rows]}",
        )
        return f"{len(rows)} new SUVs"

    await run.check("cars: new suvs summary", s6_cars_new_suvs)

    # 7. Session
    async def s7_status() -> str:
        out = d(await cr_auth_status(rt))
        data = out["data"]
        if run.member:
            expect(out["session"] == "active", f"session {out['session']} after member fetches")
            expect(data["source"] == "file" and data["expiry_basis"] == "measured", str(data))
            expect(
                data["days_left_max"] and data["days_left_max"] > 300, str(data["days_left_max"])
            )
        else:
            expect(out["session"] == "none", out["session"])
        return f"session {out['session']}, {data['expiry_basis']}, {data['days_left_max']} days"

    await run.check("auth_status", s7_status)

    if run.member:

        async def s7_sign_in() -> str:
            out = d(await cr_sign_in(rt))
            expect(out["data"]["status"] == "refused", f"status {out['data']['status']}")
            expect(out["data"]["reason"] == "session_active", out["data"]["reason"])
            return "refused: session_active (no window opened)"

        await run.check("sign_in: refused for a live cookie", s7_sign_in)

    # 8. Errors stay envelopes
    async def s8_unknown_category() -> str:
        out = d(await cr_ratings(rt, "c999999999"))
        expect(out["error"] and out["error"]["code"] == "unknown_category", out["error"])
        expect(out["auth_state"] is None and out["provenance"] is None, "tier claimed for no row")
        return f"reason {out['error'].get('reason')}"

    await run.check("error: unknown_category", s8_unknown_category)

    async def s8_group_owner() -> str:
        out = d(await cr_ratings(rt, DISPLAY_GROUP))
        expect(out["error"] and out["error"]["code"] == "unknown_category", out["error"])
        cands = out["error"].get("candidates") or []
        expect(cands and cands[0].get("group_id") == 200369, f"candidates {cands}")
        return f"owner {cands[0]['id']} {cands[0]['name']}, group {cands[0]['group']}"

    await run.check("error: display-group id names its owner", s8_group_owner)

    async def s8_unknown_product() -> str:
        out = d(await cr_product(rt, 1))
        expect(out["error"] and out["error"]["code"] == "unknown_product", out["error"])
        return "unknown_product"

    await run.check("error: unknown_product", s8_unknown_product)

    async def s8_year() -> str:
        n = rt.transport.request_count
        out = d(await cr_cars(rt, make="honda", year=1990))
        expect(out["error"] and out["error"]["code"] == "invalid_filter_value", out["error"])
        expect(out["error"]["candidates"], "no legal range in candidates")
        expect(rt.transport.request_count == n, "a request was spent on a rejected year")
        return f"candidates {out['error']['candidates']}"

    await run.check("error: cars year out of range, no request", s8_year)

    async def s8_short_query() -> str:
        out = d(await cr_search(rt, "x"))
        expect(out["error"] is None, out["error"])
        expect(out["data"]["categories"] == [] and out["data"]["products"] == [], "hits")
        return "empty answer, no error"

    await run.check("search: too-short query is empty, not an error", s8_short_query)

    # 9. Discovery
    async def s9() -> str:
        out = d(await cr_categories(rt))
        expect(out["error"] is None, out["error"])
        n = out["data"]["total"]
        expect(n >= 300, f"only {n} categories")
        expect("sitemap_pass_pending" not in out["warnings"], "sitemap pass never recorded")
        return f"{n} categories"

    await run.check("categories: both sources", s9)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
