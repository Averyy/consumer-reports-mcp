"""Re-measure every response shape on the REAL 172-product payload (PLAN P11.1, SPEC §7 sizing).

Local only: reads the gitignored scratch/ captures, fills scores deterministically (the same
`fill_scores_in` the tests use — shape-equivalent to a member response), runs the real tool
code over a tmp runtime, and counts tokens with tiktoken cl100k_base.

    uv run --with tiktoken --no-sync scripts/measure_sizes.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from consumer_reports_mcp.config import Settings  # noqa: E402
from consumer_reports_mcp.credentials import CredentialStore  # noqa: E402
from consumer_reports_mcp.runtime import build_runtime  # noqa: E402
from consumer_reports_mcp.tools_products import (  # noqa: E402
    cr_categories,
    cr_filters,
    cr_product,
    cr_ratings,
)
from tests.conftest import FakeResponse, FakeWaferSession, make_category_page  # noqa: E402

# SPEC §7 budgets for the DEFAULT shapes. `full` nested defaults to 5 per group (config.py
# FULL_LIMIT_NESTED) because nested-10 measured 31k even lean; the 10 figure is informational.
BUDGETS = {"standard nested 10": 4000, "cr_filters": 4000, "full nested 5": 26000}


def tokens(obj) -> int:
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(json.dumps(obj, separators=(",", ":"))))


def load_real_fixture() -> dict:
    from scripts.build_fixtures import brace_match

    html = (ROOT / "scratch" / "pages" / "c37162.html").read_text(
        encoding="utf-8", errors="replace"
    )
    f = html.find("window.filterInstanceDATA")
    st = html.find("{", f)
    fid = json.loads(html[st : brace_match(html, st)])
    a = html.find("console.log('[ ratings-wrapper ]',")
    st = html.find("{", a)
    rw = json.loads(html[st : brace_match(html, st)])
    cat = dict(rw["cat"])
    attrs = cat.pop("categoryAttributes")
    return {
        "filter_instance": fid,
        "category_attributes": attrs,
        "cat": cat,
        "siblings": rw["productFilterPayload"]["debug"]["categories"],
        "subcats": rw["subcats"],
        "scat": rw["scat"],
        "final_url": "https://www.consumerreports.org/appliances/refrigerators/french-door-refrigerator/c37162/",
        "requested_id": None,
        "http_status": 200,
        "title": "Refrigerator Ratings & Reviews - Consumer Reports",
    }


async def main() -> int:
    fx = load_real_fixture()
    tmp = Path(tempfile.mkdtemp())
    sess = FakeWaferSession()
    settings = Settings(env={"CR_CACHE_DIR": str(tmp / "cache")}, home=tmp)
    store = CredentialStore(tmp / "session.json", env={})
    store.save({"hash": "h" * 36})
    rt = build_runtime(settings, env={}, session_factory=lambda **kw: sess, credentials=store)
    rt.cache.upsert_category_index(
        [
            {
                "id": 37162,
                "path": "/appliances/refrigerators/french-door-refrigerator/c37162/",
                "display_name": "French-Door Refrigerators",
            }
        ],
        "az",
        rt.clock(),
    )
    rt.cache.record_discovery_run("az", rt.clock(), 1)
    rt.cache.record_discovery_run("sitemap", rt.clock(), 1)
    sess.route(
        fx["final_url"],
        FakeResponse(
            url=fx["final_url"], content=make_category_page(fx, subscriber="true", fill_scores=True)
        ),
    )
    rows: list[tuple[str, int]] = []
    for detail in ("summary", "standard", "full"):
        for label, kw in (
            ("flat 25", {"group_mode": "flat", "limit": 25}),
            ("nested 5", {"limit": 5}),
            ("nested 10", {"limit": 10}),
            ("nested 25", {"limit": 25}),
        ):
            if detail == "full" and kw["limit"] > 10:
                kw = dict(kw, limit=10)
                label += " (capped 10)"
            out = await cr_ratings(rt, 37162, detail=detail, **kw)
            assert out.error is None, out.error
            tag = (
                " [default]"
                if (detail, label) in (("standard", "nested 10"), ("full", "nested 5"))
                else ""
            )
            rows.append((f"{detail} {label}{tag}", tokens(out.model_dump(mode="json"))))
    filters = await cr_filters(rt, 37162)
    rows.append(("cr_filters", tokens(filters.model_dump(mode="json"))))
    pid = int(next(iter(fx["filter_instance"]["data"])))
    prod = await cr_product(rt, pid)
    rows.append(("cr_product (no descriptions)", tokens(prod.model_dump(mode="json"))))
    prod_d = await cr_product(rt, pid, include_descriptions=True)
    rows.append(("cr_product (with descriptions)", tokens(prod_d.model_dump(mode="json"))))
    cats = await cr_categories(rt, family=28978)
    rows.append(("cr_categories family=28978 (enriched)", tokens(cats.model_dump(mode="json"))))
    lean = await cr_categories(rt)
    rows.append(
        (
            "cr_categories lean (1 row here; ×346 for the catalogue)",
            tokens(lean.model_dump(mode="json")),
        )
    )

    width = max(len(r[0]) for r in rows)
    print(f"{'shape'.ljust(width)}  tokens")
    for name, n in rows:
        print(f"{name.ljust(width)}  {n:>7,}")
    over = []
    for name, n in rows:
        for key, budget in BUDGETS.items():
            if name.startswith(key) and n > budget:
                over.append((name, n, budget))
    if over:
        print("\nOVER BUDGET:")
        for name, n, budget in over:
            print(f"  {name}: {n:,} > {budget:,} — move the default in config.py")
        return 1
    print("\nall within SPEC §7 budgets")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
