"""Build content-minimal, score-free test fixtures from the gitignored scratch/ captures.

Keeps real STRUCTURE (filter shapes, every attribute definition, `args`, family blocks, display
group names) and replaces CONTENT (products, brands, model names, prices, prose). The outputs are
committed under tests/fixtures/; this script only runs on a machine that holds scratch/.

    uv run scripts/build_fixtures.py

SPEC §12 / CLAUDE.md: never commit member scores, never commit a full anonymous payload.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ROOT / "scratch"
OUT = ROOT / "tests" / "fixtures"

SYNTH_API_KEY = "SYNTHETICKEY0000000000000000000000000ABCD"[:40]
assert len(SYNTH_API_KEY) == 40

BRANDS = [("Brand A", 900001), ("Brand B", 900002), ("Brand C", 900003), ("Brand D", 900004)]


# --------------------------------------------------------------------------- helpers


def brace_match(s: str, start: int) -> int:
    """Index one past the `}` closing the object opened at s[start]. String/escape aware."""
    depth = 0
    i = start
    in_str = False
    esc = False
    quote = ""
    while i < len(s):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == quote:
                in_str = False
        else:
            if c in "\"'":
                in_str = True
                quote = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i + 1
        i += 1
    raise ValueError("unbalanced braces")


def load_page(name: str) -> tuple[dict, dict, str]:
    html = (SCRATCH / "pages" / name).read_text(encoding="utf-8", errors="replace")
    f = html.find("window.filterInstanceDATA")
    st = html.find("{", f)
    fid = json.loads(html[st : brace_match(html, st)])
    a = html.find("console.log('[ ratings-wrapper ]',")
    st = html.find("{", a)
    rw = json.loads(html[st : brace_match(html, st)])
    title = re.search(r"<title>(.*?)</title>", html, re.S).group(1)
    return fid, rw, title


def dump(name: str, obj: Any) -> None:
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"  wrote {name:40} {path.stat().st_size / 1024:6.1f} KB")


def write_text(name: str, text: str) -> None:
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"  wrote {name:40} {path.stat().st_size / 1024:6.1f} KB")


SCALAR_BLOCK_KEYS = (
    "_id",
    "productGroupName",
    "productGroupSlugName",
    "parentProductGroupId",
    "productGroupTypeId",
    "productGroupTypeName",
    "productGroupClassificationId",
    "productGroupClassificationName",
    "sortOrder",
    "targetPath",
    "breadcrumbName",
    "shortName",
    "singularName",
    "pluralName",
    "hasChildren",
    "modelCounts",
    "modelMaxOverallDisplayScore",
    "modelMinOverallDisplayScore",
    "HasReliabilityData",
    "HasOwnerSatisfactionData",
)


def block_scalars(block: dict) -> dict:
    """The parts of a ratings-wrapper category block the parser reads — no prose, no attrs."""
    out = {k: block.get(k) for k in SCALAR_BLOCK_KEYS if k in block}
    out["productGroupIntroText"] = "Synthetic intro text."
    return out


def trim_definition_data(defs: list[dict], keep: int = 3) -> list[dict]:
    out = []
    for d in defs:
        d = copy.deepcopy(d)
        if isinstance(d.get("data"), list):
            d["data"] = d["data"][:keep]
        if isinstance(d.get("attrs"), list):  # per-product value list inside a definition
            d["attrs"] = []  # per-product echoes, never read by a parser
        out.append(d)
    return out


def trim_filters(
    filters: list[dict], *, brands: list[tuple[str, int]], prices: list[float]
) -> list[dict]:
    out = []
    for f in filters:
        f = copy.deepcopy(f)
        if f["id"] == "price":
            f["data"] = [{"label": p} for p in sorted({p for p in prices if p is not None})[:5]]
        elif f["id"] == "brands":
            f["data"] = [{"id": bid, "label": name, "sortOrder": 0} for name, bid in brands][:4]
        elif f["id"] == "features":
            f["data"] = trim_definition_data(f["data"], keep=3)
        out.append(f)
    return out


def trim_args(args: dict) -> dict:
    a = copy.deepcopy(args)
    for k in ("analytics", "env", "opts"):
        a.pop(k, None)
    return a


# --------------------------------------------------------------------------- products


def synth_attrs(
    template: list[dict], pid: int, *, text_override: dict[int, str] | None = None
) -> list[dict]:
    """Rebuild a product's attrs[] with the real ids/names/types and synthetic values."""
    out = []
    text_pool = ["39", "0", "-1", "Yes", "No", "External"]
    for a in template:
        aid = a["attributeId"]
        t = a["attributeTypeName"]
        v: Any
        if t == "numeric-rating-score":
            v = None  # gated — always null in a fixture
        elif t == "numeric-price":
            v = None if pid % 3 == 0 else 20 + (pid + aid) % 60
        elif t == "numeric-general":
            v = round(10 + (pid + aid) % 50 + 0.5 * ((pid + aid) % 2), 1)
        elif t == "boolean":
            v = "Yes" if (pid + aid) % 2 == 0 else "No"
        elif t == "text":
            v = text_pool[(pid + aid) % len(text_pool)]
        else:
            v = a.get("value")
        if text_override and aid in text_override:
            v = text_override[aid]
        out.append({"attributeId": aid, "attributeTypeName": t, "name": a["name"], "value": v})
    return out


def surveys(owner: int | None, reliability: int | None) -> dict:
    def block(label: str, type_id: int, score: int | None) -> dict | None:
        if score is None:
            return None
        return {
            "surveyTypeId": type_id,
            "surveyTypeLabel": label,
            "scoreTypeRemarkId": 1,
            "scoreTypeRemark": "Valid Score",
            "infoText": "Synthetic survey info text.",
            "surveyScore": score,
            "survey10PtScore": score * 2,
            "survey100PtScore": score * 20,
        }

    return {
        "ownerSatisfaction": block("Owner satisfaction", 2, owner),
        "reliability": block("Predicted reliability", 1, reliability),
        "comfort": None,
    }


def product(
    *,
    index: int,
    pid: int,
    brand: tuple[str, int],
    group: tuple[int, str],
    sort_index: int,
    price: float | None,
    attrs: list[dict],
    recommended: bool = False,
    dont_buy: bool = False,
    smart_buy: bool = False,
    owner: int | None = None,
    reliability: int | None = None,
    hierarchy: dict | None = None,
    retailer_prices: list[float] | None = None,
) -> dict:
    return {
        "index": index,
        "id": pid,
        "productGroupHierarchy": hierarchy or {},
        "price": price,
        "_price": (price - 100) if isinstance(price, (int, float)) and price > 100 else price,
        "overallDisplayScore": None,
        "_overallSortIndex": sort_index,
        "brandId": brand[1],
        "brandName": brand[0],
        "modelName": f"MODEL-{index + 1:03d}",
        "expertRatings": {"isRecommended": recommended, "isDontBuy": dont_buy},
        "surveys": surveys(owner, reliability),
        "attrs": attrs,
        "_groupId": group[0],
        "_groupName": group[1],
        "_shoppingParsed": {
            "count": len(retailer_prices or []),
            "prices": retailer_prices or [],
            "isSmartBuy": smart_buy,
        },
    }


def hierarchy(scid: int, scname: str, cid: int, cname: str, group: tuple[int, str]) -> dict:
    return {
        "superCategoryId": scid,
        "superCategoryName": scname,
        "categoryId": cid,
        "categoryName": cname,
        "subCategoryId": group[0],
        "subCategoryName": group[1],
    }


def category_fixture(
    *,
    fid: dict,
    rw: dict,
    title: str,
    products: list[dict],
    final_url: str,
    requested_id: int | None,
    brands: list[tuple[str, int]],
) -> dict:
    cat = rw["cat"]
    siblings = rw.get("productFilterPayload", {}).get("debug", {}).get("categories") or []
    prices = [p["price"] for p in products]
    fi = {
        "filters": trim_filters(fid["filters"], brands=brands, prices=prices),
        "data": {str(p["id"]): p for p in products},
        "args": trim_args(fid["args"]),
        "attrs": trim_definition_data(fid["attrs"], keep=3),
    }
    return {
        "_note": "content-minimal fixture built by scripts/build_fixtures.py — synthetic products",
        "filter_instance": fi,
        "category_attributes": cat.get("categoryAttributes") or [],
        "cat": block_scalars(cat),
        "siblings": [block_scalars(s) for s in siblings],
        "subcats": [block_scalars(s) for s in (rw.get("subcats") or [])],
        "scat": block_scalars(rw.get("scat") or {}),
        "final_url": final_url,
        "requested_id": requested_id,
        "http_status": 200,
        "title": title,
    }


def build_c37162() -> None:
    fid, rw, title = load_page("c37162.html")
    template = next(iter(fid["data"].values()))["attrs"]
    groups = {
        200367: "30 Inch and Narrower Widths",
        200369: "31 - 33 Inch Widths",
        200371: "34 Inch and Wider Widths",
    }
    A, B, C, D = BRANDS
    scid, cid = fid["args"]["scid"], fid["args"]["cid"]
    h = lambda g: hierarchy(scid, "Refrigerators", cid, "French-Door Refrigerators", (g, groups[g]))  # noqa: E731

    text_ids = [a["attributeId"] for a in template if a["attributeTypeName"] == "text"]
    numgen_ids = [a["attributeId"] for a in template if a["attributeTypeName"] == "numeric-general"]

    # 8 synthetic products across the 3 real groups. Within a group `_overallSortIndex` is
    # monotone with one tie (group 200371). Brand B's only product is rank 3 in its group.
    rows = [
        dict(
            index=0,
            pid=500001,
            brand=A,
            group=(200367, groups[200367]),
            sort_index=3,
            price=1899,
            recommended=True,
            owner=4,
            reliability=4,
            retailer_prices=[1899, 1949.99],
        ),
        dict(
            index=1,
            pid=500002,
            brand=D,
            group=(200367, groups[200367]),
            sort_index=9,
            price=None,
            owner=None,
            reliability=None,
        ),
        dict(
            index=2,
            pid=500003,
            brand=A,
            group=(200369, groups[200369]),
            sort_index=2,
            price=2299,
            owner=5,
            reliability=3,
            retailer_prices=[2299, 2399, 2299.99],
        ),
        dict(
            index=3,
            pid=500004,
            brand=C,
            group=(200369, groups[200369]),
            sort_index=5,
            price=2499,
            dont_buy=True,
            owner=3,
            reliability=2,
            retailer_prices=[2499],
        ),
        dict(
            index=4,
            pid=500005,
            brand=B,
            group=(200369, groups[200369]),
            sort_index=8,
            price=1599,
            owner=4,
            reliability=4,
        ),
        dict(
            index=5,
            pid=500006,
            brand=A,
            group=(200371, groups[200371]),
            sort_index=1,
            price=3299,
            recommended=True,
            owner=4,
            reliability=5,
            retailer_prices=[3299, 3199, 3399],
        ),
        dict(
            index=6,
            pid=500007,
            brand=C,
            group=(200371, groups[200371]),
            sort_index=6,
            price=2899,
            smart_buy=True,
            owner=3,
            reliability=3,
            retailer_prices=[2899, 2799],
        ),
        dict(
            index=7,
            pid=500008,
            brand=D,
            group=(200371, groups[200371]),
            sort_index=6,
            price=2099,
            owner=None,
            reliability=None,
        ),
    ]
    products = []
    for r in rows:
        pid = r["pid"]
        override: dict[int, str] = {}
        if r["index"] == 0 and text_ids:
            override[text_ids[0]] = "-1"  # dial position: text, never coerced
        attrs = synth_attrs(template, pid, text_override=override)
        if r["index"] == 2 and numgen_ids:
            for a in attrs:  # a value that will not coerce to numeric-general
                if a["attributeId"] == numgen_ids[0]:
                    a["value"] = "36 - 38"
        if r["index"] == 4:
            # an entry with no definition ANYWHERE: unit must stay None, kind from the entry
            attrs.append(
                {
                    "attributeId": 999002,
                    "attributeTypeName": "numeric-general",
                    "name": "Exterior width",
                    "value": 36,
                }
            )
        if r["index"] == 5:
            attrs.append(
                {
                    "attributeId": 999001,
                    "attributeTypeName": "boolean",
                    "name": "Mystery flag",
                    "value": "Yes",
                }
            )
        r = dict(r)
        r["hierarchy"] = h(r["group"][0])
        products.append(product(attrs=attrs, **r))
    fx = category_fixture(
        fid=fid,
        rw=rw,
        title=title,
        products=products,
        final_url="https://www.consumerreports.org/appliances/refrigerators/french-door-refrigerator/c37162/",
        requested_id=None,
        brands=BRANDS,
    )
    dump("category_c37162.json", fx)


def build_banks() -> None:
    fid, rw, title = load_page("c37154_banks.html")
    template = next(iter(fid["data"].values()))["attrs"]
    group = (37154, "Banks")
    scid, cid = fid["args"]["scid"], fid["args"]["cid"]
    brands = [("Bank A", 910001), ("Bank B", 910002), ("Bank C", 910003), ("Bank D", 910004)]
    products = []
    for i, (brand, idx) in enumerate(zip(brands, [1, 2, 3, 4], strict=True)):
        attrs = synth_attrs(template, 510001 + i)
        for a in attrs:
            if a["attributeTypeName"] == "text":
                a["value"] = "Size Tier " + "ABCD"[i]
        products.append(
            product(
                index=i,
                pid=510001 + i,
                brand=brand,
                group=group,
                sort_index=idx,
                price=None,
                attrs=attrs,
                hierarchy=hierarchy(scid, "Banks & Credit Unions", cid, "Banks", group),
            )
        )
    fx = category_fixture(
        fid=fid,
        rw=rw,
        title=title,
        products=products,
        final_url="https://www.consumerreports.org/money/banks-credit-unions/banks/c37154/",
        requested_id=None,
        brands=brands,
    )
    dump("category_c37154_banks.json", fx)


def build_c200228() -> None:
    fid, rw, title = load_page("c200228_redirect.html")
    template = next(iter(fid["data"].values()))["attrs"]
    scid, cid = fid["args"]["scid"], fid["args"]["cid"]
    # 4 subcats in the taxonomy, products in only 3 (Coconut Milk 200361 has none): RECON §10g.
    groups = {200358: "Almond Milk", 200360: "Oat Milk", 33644: "Soy Milk"}
    A, B, C, _ = BRANDS
    rows = [
        dict(
            index=0,
            pid=520001,
            brand=A,
            group=(200358, groups[200358]),
            sort_index=1,
            price=3.49,
            recommended=True,
        ),
        dict(
            index=1, pid=520002, brand=B, group=(200358, groups[200358]), sort_index=4, price=2.99
        ),
        dict(
            index=2, pid=520003, brand=A, group=(200360, groups[200360]), sort_index=2, price=4.29
        ),
        dict(
            index=3, pid=520004, brand=C, group=(200360, groups[200360]), sort_index=7, price=3.99
        ),
        dict(index=4, pid=520005, brand=C, group=(33644, groups[33644]), sort_index=3, price=2.79),
    ]
    numgen_ids = [a["attributeId"] for a in template if a["attributeTypeName"] == "numeric-general"]
    products = []
    for r in rows:
        attrs = synth_attrs(template, r["pid"])
        if r["index"] == 1 and len(numgen_ids) > 1:
            for a in attrs:  # the real-world empty-string case seen on this category
                if a["attributeId"] == numgen_ids[1]:
                    a["value"] = ""
        r = dict(r)
        r["hierarchy"] = hierarchy(scid, "Milk & Milk Alternatives", cid, "Plant Milk", r["group"])
        products.append(product(attrs=attrs, **r))
    fx = category_fixture(
        fid=fid,
        rw=rw,
        title=title,
        products=products,
        final_url="https://www.consumerreports.org/health/milk-milk-alternatives/c200228/",
        requested_id=200358,  # asked for a subcategory id, served the parent's payload (SPEC §5)
        brands=[A, B, C],
    )
    dump("category_c200228.json", fx)


# --------------------------------------------------------------------------- reliability


def build_reliability() -> None:
    rel = json.loads((SCRATCH / "rel37162.json").read_text(encoding="utf-8"))
    d = rel["data"]

    def survey(type_id: int, gid: int, brands: list[tuple[str, int, int | None]]) -> dict:
        return {
            "productGroupSurveyValue": [
                {
                    "brandId": bid,
                    "brandName": name,
                    "productGroupId": gid,
                    "surveyScore": score,
                    "survey10PtScore": score * 2,
                    "survey100PtScore": score * 20 - 5,
                    "surveyTypeId": type_id,
                    "sortOrder": i + 1,
                }
                for i, (name, bid, score) in enumerate(brands)
                if score is not None
            ],
            "surveyTypeId": type_id,
            "productGroupId": gid,
            "blurb": "<p>Synthetic methodology blurb.</p>",
            "footnote": "<p>Synthetic footnote.</p>",
            "infoText": "Synthetic info text.",
        }

    def cat_block(
        src: dict,
        *,
        has_data: bool,
        brands: list[tuple[str, int, int | None]],
        owner: list[tuple[str, int, int | None]],
    ) -> dict:
        b = block_scalars(src)
        b["HasReliabilityData"] = has_data
        b["HasOwnerSatisfactionData"] = has_data
        if has_data:
            b["surveys"] = {
                "reliability": survey(1, src["_id"], brands),
                "ownerSatisfaction": survey(2, src["_id"], owner),
            }
        return b

    # the two surveys are DIFFERENT permutations in the real payload: reliability ranks A, B, C;
    # owner satisfaction ranks C, A — and Brand B has no owner-satisfaction score at all
    brands = [("Brand A", 900001, 4), ("Brand B", 900002, 3), ("Brand C", 900003, 5)]
    owner = [("Brand C", 900003, 4), ("Brand A", 900001, 5), ("Brand B", 900002, None)]
    category = cat_block(d["category"], has_data=True, brands=brands, owner=owner)
    by_id = {c["_id"]: c for c in d["categories"]}
    siblings = [
        cat_block(by_id[37162], has_data=True, brands=brands, owner=owner),  # self, as CR ships
        cat_block(
            by_id[28722],
            has_data=True,
            brands=[("Brand A", 900001, 3), ("Brand D", 900004, 4)],
            owner=[("Brand A", 900001, 4), ("Brand D", 900004, 2)],
        ),
        cat_block(by_id[29738], has_data=False, brands=[], owner=[]),
    ]
    models = [
        {
            "_id": 500001,
            "modelName": "MODEL-001",
            "brandId": 900001,
            "brandName": "Brand A",
            "productGroupId": 200367,
            "modelAvailabilityName": "Available",
            "testStateName": "Tested",
        },
        {
            "_id": 500003,
            "modelName": "MODEL-003",
            "brandId": 900001,
            "brandName": "Brand A",
            "productGroupId": 200369,
            "modelAvailabilityName": "Discontinued",
            "testStateName": "Tested",
        },
        {
            "_id": 500006,
            "modelName": "MODEL-006",
            "brandId": 900001,
            "brandName": "Brand A",
            "productGroupId": 200371,
            "modelAvailabilityName": "Available",
            "testStateName": "Tested",
        },
    ]
    fx = {
        "_note": "content-minimal initStore fixture built by scripts/build_fixtures.py",
        "init_store": {
            "data": {
                "supercategory": block_scalars(d["supercategory"]),
                "category": category,
                "categories": siblings,
                "models": models,
                "taxonomy": {},
                "overrideSuperCategories": [],
            },
            "env": {},
            "seo": {},
        },
        "final_url": "https://www.consumerreports.org/appliances/refrigerators/french-door-refrigerator/reliability/c37162/",
        "title": "Refrigerator Reliability - Consumer Reports",
    }
    dump("reliability_c37162.json", fx)


# --------------------------------------------------------------------------- discovery


def build_az_and_sitemaps() -> None:
    html = """<!DOCTYPE html>
<html><head><title>Products A-Z - Consumer Reports</title></head>
<body>
<nav class="account-nav" hidden><a href="/ec/logout">Sign Out</a></nav>
<a class="cda-gnav__nav-item-label" href="https://www.consumerreports.org/cars/suvs/">
    SUVs
</a>
<div class="products-a-z__results">
  <a href="/appliances/refrigerators/french-door-refrigerator/c37162/" class="products-a-z__results__item products-a-z__results__category-item">
      <span style="padding-left: 20px">French-Door Refrigerators</span>
  </a>
  <a href="https://www.consumerreports.org/appliances/refrigerators/top-freezer-refrigerator/c28722/" class="products-a-z__results__item">
      <span>Top-Freezer Refrigerators</span>
  </a>
  <a href="/money/banks-credit-unions/banks/c37154/" class="products-a-z__results__item"><span>Banks &amp; Credit Unions</span></a>
  <a href="/cars/dash-cams/c201102/" class="products-a-z__results__item"><span>Dash Cams</span></a>
  <a href="/health/milk-milk-alternatives/plant-milk/c200228/" class="products-a-z__results__item">
      <span><b>Plant</b> Milk</span>
  </a>
  <a href="/home-garden/vacuum-cleaners/robotic-vacuums/c35183/" class="products-a-z__results__item"><span>Robotic Vacuums</span></a>
</div>
<span class="account-nav__item">Sign Out</span>
</body></html>
"""
    write_text("azindex.html", html)
    write_text(
        "sitemap_products.xml",
        '<?xml version="1.0" encoding="UTF-8"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>https://www.consumerreports.org/products/sitemap/28958</loc><lastmod>2026-08-30</lastmod></sitemap>"
        "<sitemap><loc>https://www.consumerreports.org/products/sitemap/28981</loc><lastmod>2026-08-30</lastmod></sitemap>"
        "</sitemapindex>\n",
    )
    write_text(
        "sitemap_28958.xml",
        '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="https://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://www.consumerreports.org/electronics-computers/sound-bars/</loc><lastmod>2026-09-03</lastmod></url>"
        "<url><loc>https://www.consumerreports.org/electronics-computers/sound-bars/c28698/</loc><lastmod>2026-09-03</lastmod></url>"
        "<url><loc>https://www.consumerreports.org/electronics-computers/sound-bars/recommended/c28698/</loc><lastmod>2026-09-03</lastmod></url>"
        "<url><loc>https://www.consumerreports.org/electronics-computers/sound-bars/synthetic-model/m419907/</loc><lastmod>2026-09-03</lastmod></url>"
        "<url><loc>https://www.consumerreports.org/electronics-computers/tvs/c28700/</loc><lastmod>2026-09-03</lastmod></url>"
        "<url><loc>https://www.consumerreports.org/cars/dash-cams/c201102/</loc><lastmod>2026-09-03</lastmod></url>"
        "</urlset>\n",
    )


# --------------------------------------------------------------------------- cars


def build_cars() -> None:
    # --- keys.json: 2 makes x 2 models x 3 model-years, New/Used mix, one model-year in two types
    def my(myid: int, year: int, states: list[str], types: list[tuple[int, str, str]]) -> dict:
        return {
            "modelGenerationId": 9000 + myid % 100,
            "modelYear": year,
            "modelYearId": myid,
            "modelYearStates": [
                {"modelYearStateName": s, "modelYearStateId": 2 if s == "New" else 1}
                for s in states
            ],
            "modelYearCarTypes": [
                {
                    "carTypeDisplayName": name,
                    "carTypeSingularName": name.rstrip("s"),
                    "isPrimary": primary,
                    "carTypeOriginalName": name,
                    "carTypeId": tid,
                    "carTypePluralName": name,
                }
                for tid, name, primary in types
            ],
        }

    SED = (105, "Sedans & Hatchbacks", "Y")
    LUX = (116, "Luxury Cars & SUVs", "N")
    SUV = (103, "SUVs", "Y")
    keys = {
        "response": [
            {
                "makeId": 90001,
                "makeName": "Make A",
                "slugMakeName": "make-a",
                "models": [
                    {
                        "modelId": 91001,
                        "modelName": "Model X",
                        "slugModelName": "model-x",
                        "modelYears": [
                            my(
                                700001, 2026, ["New"], [SED, LUX]
                            ),  # one model-year in two car types
                            my(700002, 2025, ["New", "Used"], [SED]),
                            my(700003, 2019, ["Used"], [SED]),
                        ],
                    },
                    {
                        "modelId": 91002,
                        "modelName": "Model Y",
                        "slugModelName": "model-y",
                        "modelYears": [
                            my(700004, 2026, ["New"], [SUV]),
                            my(700005, 2024, ["Used"], [SUV]),
                            my(700006, 2018, ["Used"], [SUV]),
                        ],
                    },
                ],
            },
            {
                "makeId": 90002,
                "makeName": "Make B",
                "slugMakeName": "make-b",
                "models": [
                    {
                        "modelId": 92001,
                        "modelName": "Model P",
                        "slugModelName": "model-p",
                        "modelYears": [
                            my(700007, 2026, ["New"], [SED]),
                            my(700008, 2023, ["Used"], [SED]),
                            my(700009, 2015, ["Used"], [SED]),
                        ],
                    },
                    {
                        "modelId": 92002,
                        "modelName": "Model Q",
                        "slugModelName": "model-q",
                        "modelYears": [
                            my(700010, 2025, ["New", "Used"], [SUV, LUX]),
                            my(700011, 2021, ["Used"], [SUV]),
                            my(700012, 2016, ["Used"], [SUV]),
                        ],
                    },
                ],
            },
        ],
        "responseSummary": {"responseCount": 2, "requestQueryParameters": []},
    }
    dump("cars/keys.json", keys)

    # --- cartypes.json: 2 types, 3 categories, one category under both with different counts
    def category(cid: int, name: str, slug: str, tested: int, not_tested: int, order: int) -> dict:
        return {
            "categoryId": cid,
            "categoryName": name,
            "slugCategoryName": slug,
            "sortOrder": order,
            "testedCarCount": tested,
            "notTestedCarCount": not_tested,
            "inTestCarCount": 0,
            "categoryIntroText": "Synthetic intro.",
            "carTypeCategoryPriceMax": 60000,
            "carTypeCategoryPriceMin": 20000,
        }

    cartypes = {
        "content": [
            {
                "carTypeName": "Sedans & Hatchbacks",
                "slugCarTypeName": "sedans",
                "carTypeSortOrder": 4,
                "carTypeId": 105,
                "carTypePriceMax": 249000,
                "carTypePriceMin": 17390,
                "testedCarCount": 30,
                "notTestedCarCount": 10,
                "inTestCarCount": 1,
                "categories": [
                    category(11331, "Small sedans", "small-sedans", 12, 2, 1),
                    category(11359, "Electric sedans", "electric-sedans", 14, 9, 2),
                ],
            },
            {
                "carTypeName": "Hybrids/EVs",
                "slugCarTypeName": "hybrids-evs",
                "carTypeSortOrder": 6,
                "carTypeId": 107,
                "carTypePriceMax": 150000,
                "carTypePriceMin": 22000,
                "testedCarCount": 25,
                "notTestedCarCount": 12,
                "inTestCarCount": 0,
                "categories": [
                    category(
                        11359, "Electric sedans", "electric-sedans", 13, 8, 1
                    ),  # same id, different counts
                    category(13762, "Electric luxury SUVs", "electric-luxury-suvs", 9, 10, 2),
                ],
            },
        ]
    }
    dump("cars/cartypes.json", cartypes)

    # --- modelyear.json: the real structure with every score, rank, id and name made synthetic
    real = json.loads((SCRATCH / "cars" / "my23133-anon.json").read_text(encoding="utf-8"))
    NAME_MAP = {
        "Acura": "Make A",
        "acura": "make-a",
        "RDX": "Model X",
        "rdx": "model-x",
        "Honda Motor Company": "Parent Co",
    }
    NUMERIC_KEYS = re.compile(
        r"(score|rank|rating|value|percent|index|horsepower|displacement|cost|price|msrp|median|max|min|miles|gal|dollar|count|cashvalueupto)",
        re.I,
    )
    ID_MAP = {
        "modelYearId": 700001,
        "carId": 800001,
        "makeId": 90001,
        "modelId": 91001,
        "modelGenerationId": 9001,
    }
    SYNTH_NAMES = {
        "tireBrandName": "Tire Brand A",
        "tireModelName": "Tire Model A",
        "trimName": "Trim A",
        "headquarters": "Synthetic City",
    }

    def synth(obj: Any, key: str = "") -> Any:
        if isinstance(obj, dict):
            return {k: synth(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [synth(v, key) for v in obj]
        if key in ID_MAP:
            return ID_MAP[key]
        if key in SYNTH_NAMES:
            return SYNTH_NAMES[key]
        if key == "fileName":  # a CDN asset id identifies the real car whatever we rename it
            return "/synthetic-asset"
        if isinstance(obj, str):
            if obj in NAME_MAP:
                return NAME_MAP[obj]
            if len(obj) > 30 or "<p>" in obj or "<ul>" in obj:
                return "Synthetic text."
            for real_name, fake in NAME_MAP.items():
                if real_name in obj:
                    obj = obj.replace(real_name, fake)
            return obj
        if isinstance(obj, bool):
            return obj
        if isinstance(obj, int) and NUMERIC_KEYS.search(key):
            return (obj * 7 + 3) % 97 + 1
        if isinstance(obj, float) and NUMERIC_KEYS.search(key):
            return round(((obj * 7 + 3) % 97 + 1) / (10 if obj <= 5 else 1), 1)
        return obj

    fx = synth(real)
    # pin the headline values so tests can assert them, and keep isRecommended as CR's "Y"
    car = fx["response"]["modelYear"]["cars"][0]
    car["testRatings"]["overallTestScore"] = 61
    car["testRatings"]["roadTestScore"] = 70
    car["overallScoreSortIndex"] = 12
    car["ratingsCategory"]["overallRank"] = 3
    car["ratingsCategory"]["overallTestScoreMax"] = 80.0
    car["ratingsCategory"]["overallTestScoreMin"] = 45.0
    # join with cartypes.json / keys.json: (carTypeId, categoryId) is the address (SPEC §5)
    car["ratingsCategory"]["categoryId"] = 11359
    car["ratingsCategory"]["carTypeId"] = 105
    car["ratingsCategory"]["carTypeDisplayName"] = "Sedans & Hatchbacks"
    car["ratingsCategory"]["categoryPluralName"] = "Electric sedans"
    fx["response"]["modelYear"]["expertRatings"]["isRecommended"] = "Y"
    fx["response"]["modelYear"]["crPopularScore"] = 66
    fx["response"]["model"]["reliabilityRatings"]["predictedReliabilityScore"] = 52
    fx["response"]["model"]["reliabilityRatings"]["predictedReliabilityRating"] = 3.0
    fx["response"]["model"]["ownerSatisfactionRatings"]["predictedWouldBuyAgainPercent"] = 65
    fx["response"]["model"]["ownerSatisfactionRatings"]["predictedWouldBuyAgainRating"] = 4.0
    fx["response"]["modelYear"]["modelYear"] = 2026
    fx["response"]["modelYear"]["modelYearStates"] = [
        {"modelYearStateName": "New", "modelYearStateId": 2}
    ]
    dump("cars/modelyear.json", fx)

    # --- cars_listing.json: v2/cr/cars shape, flat entries, mixed New/Used across two makes
    def entry(
        myid: int,
        make: tuple[int, str, str],
        model: tuple[int, str, str],
        year: int,
        state: str,
        *,
        safety: float | None,
        popular: int | None,
        tested: bool,
    ) -> dict:
        return {
            "modelYearId": myid,
            "makeId": make[0],
            "makeName": make[1],
            "slugMakeName": make[2],
            "modelId": model[0],
            "modelName": model[1],
            "slugModelName": model[2],
            "modelYear": year,
            "modelYearStateId": 2 if state == "New" else 1,
            "modelYearStateName": state,
            "carId": 800000 + myid % 1000,
            "carVersionName": "Synthetic 4-door",
            "isDefaultCar": "Y",
            "powerTrainType": "Conventional",
            "testStateId": 3 if tested else 1,
            "testStateName": "Test Completed" if tested else "Not Tested",
            "modelGenerationId": 9000 + myid % 100,
            "modelGenerationStartYear": year - 2,
            "modelGenerationSummary": "Synthetic text.",
            "crPopularScore": popular,
            "safetyVerdictRatingScore": safety,
            "fuelEconomySpecs": {
                "annualFuelConsumptionGal": 480.0,
                "annualFuelCostDollar": 1700.0,
                "cruiseRangeMiles": 420,
            },
            "incentive": {
                "cashValueUpTo": 500,
                "effectiveDate": "2026-04-01T04:00:00.000Z",
                "expiryDate": "2027-03-31T04:00:00.000Z",
            },
            "firstPublishedDate": "2025-12-04T12:26:10.000Z",
            "lastPublishedDate": "2026-08-26T17:58:20.000Z",
            "ratingsCopiedFromCarId": None,
            "ratingsCopiedFromCarModelYear": None,
            "ratingsCopiedFromCarVersionName": None,
        }

    MA = (90001, "Make A", "make-a")
    MB = (90002, "Make B", "make-b")
    MX = (91001, "Model X", "model-x")
    MY_ = (91002, "Model Y", "model-y")
    MP = (92001, "Model P", "model-p")
    listing = {
        "content": [
            entry(700001, MA, MX, 2026, "New", safety=5.0, popular=71, tested=True),
            entry(700002, MA, MX, 2025, "Used", safety=5.0, popular=68, tested=True),
            entry(700003, MA, MX, 2019, "Used", safety=None, popular=None, tested=False),
            entry(700004, MA, MY_, 2026, "New", safety=4.0, popular=60, tested=True),
            entry(700007, MB, MP, 2026, "New", safety=None, popular=55, tested=False),
            entry(700008, MB, MP, 2023, "Used", safety=4.0, popular=58, tested=True),
        ],
        "queryParameters": [{"name": "slugMakeName", "value": ["make-a"]}],
        # as CR ships it: a LIST of {name, value}; `cars` is the model-year population while
        # `totalElements` (like `size`) counts MODELS
        "counts": [
            {"name": "makes", "value": 2},
            {"name": "cars", "value": 6},
            {"name": "models", "value": 3},
        ],
        "size": 50,
        "pageNumber": 1,
        "responseSize": 6,
        "totalElements": 3,
        "last": True,
        "totalPages": 1,
        "first": True,
    }
    dump("cars/cars_listing.json", listing)

    # --- car pages: env block with a synthetic 40-char key, and the isSubscriber marker
    def car_page(is_subscriber: bool) -> str:
        env = (
            '{"env":{"DRS_CARS_API_BASE":"https:\\/\\/cars-api.consumerreports.org\\/api\\/cars\\/",'
            '"DRS_CARS_API_VERSION":"v1",'
            '"DRS_CARS_API_V2_BASE":"https:\\/\\/cars-api.consumerreports.org\\/api\\/cars\\/v2",'
            f'"DRS_CARS_API_API_KEY":"{SYNTH_API_KEY}"}},'
            '"make":{"makeName":"Make A"},"model":{"modelName":"Model X"},"modelYear":2026,"currentCarId":800001}'
        )
        return (
            "<!DOCTYPE html><html><head><title>2026 Make A Model X Reviews - Consumer Reports</title></head><body>\n"
            '<nav class="account-nav" hidden><a href="/ec/logout">Sign Out</a></nav>\n'
            "<script>\n"
            f"        window.initStore = {env};\n"
            "        window.isAnonymous = null; // getUserInfo.ts will set it to true/false\n"
            f"        window.isSubscriber = {'true' if is_subscriber else 'false'};\n"
            "</script>\n"
            '<div class="paywall">Overall Score</div>\n'
            "</body></html>\n"
        )

    write_text("cars/carpage_anon.html", car_page(False))
    write_text("cars/carpage_member.html", car_page(True))


def main() -> int:
    if not (SCRATCH / "pages" / "c37162.html").exists():
        print("scratch/ captures not present — nothing to rebuild", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    build_c37162()
    build_banks()
    build_c200228()
    build_reliability()
    build_az_and_sitemaps()
    build_cars()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
