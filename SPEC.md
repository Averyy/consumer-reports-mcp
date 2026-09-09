# consumer-reports-mcp — product spec

An MCP server that exposes a Consumer Reports **member's own** subscription as structured
tools, so an agent can consult CR ratings the way it consults any other data source.

Status: **Built, published and public (2026-09-06).** Eleven tools on two architectures (§7);
`consumer-reports-mcp` on PyPI since 0.2.4 on 2026-09-06, the repository public the same day,
CI green on Linux, macOS and Windows (§9). The data source, the paywall boundary, the auth
mechanism and the auth-state marker were all confirmed against a live member session before
anything was written (`RECON.md`), and this document is the design of record for what shipped:
where it states a rule, the rule is the one implemented.

A second round of anonymous structural checks (`RECON.md` §9h, 2026-09-02) closed several
unknowns and corrected four things this spec had wrong: the `type` filter is a no-op, a
subcategory URL serves its *parent's* payload under a different id, `attributeTypeName` means
different things on the two objects that carry it, and category score ranges ship on the category
page rather than the reliability page.

**All three spikes have run and are complete** (`RECON.md` §10, 2026-09-03), including Spike A's
member steps against a live session. They corrected the auth-detection
algorithm — CR rejects a bad credential with a redirect to a login page carrying no payload and no
marker, not with the anonymous page this spec assumed — located the `categoryAttributes` anchor,
and cut `cr_filters` from 36,007 tokens to 3,502. Everything else is decided; §10 records the
order it was built in.

---

## 1. Problem

CR sells a membership, not access. There is no member API, no data export, and no MCP server.
The data is behind a React front end, so the only way an agent consumes it today is by reading
rendered pages — slow, lossy, and it drops the per-attribute scores that make CR worth paying for.

Meanwhile CR's own front end ships the entire category dataset as JSON embedded in the page
itself (§5). A member's ordinary browser session already unlocks the scores in it. This closes
that gap.

## 2. Goals

- Ask CR a question from an agent and get **structured** ratings back: Overall Score,
  per-attribute test scores, predicted reliability, owner satisfaction, price, specs.
- Cache everything locally, so repeat questions cost zero requests and the data survives
  a lapsed membership.
- Serve **any** use for CR's ratings, not one workflow: shopping shortlists, spec lookups, brand
  reliability questions, category comparisons. Every answer carries its source URL and retrieval
  date, so a caller can cite it without hand-typing numbers.

## 3. Non-goals

- **Not a hosted service.** The code is open source and public (§13; public since 2026-09-06,
  §12), but each user runs it locally against their own membership.
  No shared server, no redistribution of any cache.
- **Paid content stays paid.** The server reads what the user's own session is entitled to and
  nothing else. It never solves a CAPTCHA, never automates login, and never stores a password
  (§6). For products the boundary is enforced by CR — the gated values are simply `null` in an
  anonymous payload (§5). For cars CR does not enforce it at the API at all, so what comes back
  anonymously is what CR serves anonymously, and the server passes it on (§5). The word
  "circumvention" is avoided deliberately: where there is no control, nothing is being
  circumvented.
- No bulk mirroring of CR's catalogue. Category-at-a-time, on demand.
- No opinion about what callers do with the data. This is a general-purpose ratings server, not
  the back end of one workflow — it exposes tools and stops there.

## 4. Prior art

Searched 2026-09-01. Scope, so the negative result can be judged:

- **GitHub repos**, 8 query variants — `consumer reports mcp`, `consumerreports mcp`,
  `consumer-reports mcp server`, `consumerreports-mcp`, plus adjacent `wirecutter mcp`, `rtings mcp`,
  `product reviews mcp server`, `ratings mcp server`. All zero.
- **GitHub code**, 3 variants targeting the CR product API in a Python/MCP context. Zero.
- **npm registry API** — nothing for `consumerreports`.
- **PyPI** — `consumerreports-mcp`, `consumer-reports-mcp`, `consumerreports`, `mcp-consumer-reports` all 404
  at the time (this project has since taken `consumer-reports-mcp`, §9).
- **Official MCP registry** — `consumer` returns only Gencove consumer genomics; `ratings` zero.

No *published* product-ratings MCP server exists for any review publisher.

### CR has built one — it just isn't shipped

**Retracted:** an earlier draft of this spec said no CR MCP server exists. It does.

CR demoed one in September 2025 at a Skyfire/Visa agentic-commerce event
([writeup](https://innovation.consumerreports.org/enhancing-trust-in-agentic-commerce/)).
In the demo a third-party shopping agent connects to CR's MCP server, discovers CR holds
ratings for the models under consideration, **executes a micro-license — paying CR per
rating** — and quotes the score in its answer. Skyfire carries identity and payment.

The searches above were not wrong; the thing was never released. CR marks it
"for demonstration purposes only, and not publicly available," and admits
"some questions remain about how CR's MCP server should be discovered and queried in
practice." They joined the Agentic AI Foundation — which stewards MCP — in March 2026,
so the work is live and a year further along than the post.

What this means for us:

- **The concept is validated by CR itself.** Their prototype's job is the one in §7:
  hand an agent structured ratings for a candidate set. We are not misreading the shape.
- **Their model is not ours.** CR's server sells pay-per-rating micro-licences to
  *commercial* agents. It is not member-subscription access, so even shipped it likely
  will not serve a member consulting their own subscription.
- **Their prototype is a year unreleased.** Nothing to wait for, nothing to coordinate with.

| What | Verdict |
|---|---|
| **CR's own MCP server** | **Exists as an unreleased prototype** — see below |
| Public/third-party CR MCP server | Does not exist. Nothing on GitHub, npm, PyPI or the MCP registry |
| CR API client library | Does not exist, official or unofficial |
| `ahmadkammonah/ConsumerReportsUsedCarScrapper` | HTML scraping, used cars only |
| `scottbecker/consumerreportscrawl` | HTML crawler, 2016, dead |
| RapidAPI "Consumer Reports" (apidojo) | Third-party scraper reseller, not CR, ToS-violating, brittle |
| CR Data Intelligence | Real licensed data, but B2B enterprise contracts for manufacturers |

Nobody has built against the JSON API. No base to fork; this is greenfield.

## 5. Data source

### The category page is the API — CONFIRMED 2026-09-01

A plain GET of a category URL such as
`/appliances/refrigerators/french-door-refrigerator/c37162/` embeds
`window.filterInstanceDATA`: ~1.1 MB of JSON holding **the entire category** — every
product, every rating attribute, and the complete filter taxonomy. Verified by anonymous
fetch: 172 French-Door models in one request.

This displaces the products API as the primary surface. One authenticated GET per
category, no api-key, no pagination, no endpoint reverse-engineering.

Payload shape:

```
window.filterInstanceDATA = {
  filters: [ ... ],   # the full filter taxonomy, see §7
  attrs:   [ ... ],   # 24 attribute definitions for the category
  data:    { <productId>: { ... } },
  args:    { ... },
}
```

Per product: `id`, `brandId`, `brandName`, `modelName`, `price`, `_price`,
`overallDisplayScore`, `_overallSortIndex`, `expertRatings` (`isRecommended`,
`isDontBuy`), `surveys` (owner satisfaction, brand reliability), `attrs[]` (each
`{attributeId, attributeTypeName, name, value}`), and `_shoppingParsed` (retailer
price spread).

For French-Door refrigerators the 24 attributes are 7 scored ratings — Thermostat
performance, Temperature uniformity, Energy efficiency, Noise, Ease of use, Crisper
performance, Icemaker performance — plus specs (exterior H/W/D, usable capacity,
counter-depth, annual usage cost, WiFi, ice/water dispenser) and CR's suggested settings.

### The paywall is server-side, and partial

Exactly two things are gated: **the Overall Score and the per-attribute test scores.**
Everything else CR publishes — predicted reliability, owner satisfaction, the CR Recommended
flag, prices, retailer spread and the full spec sheet — is available anonymously.
Measurements: `RECON.md` §2.

Two consequences for the design:

- **`scores_available` cannot be a boolean** (§7). A boolean would tell an agent "no scores"
  while the response carries three kinds of real CR score.
- Paywalled attributes arrive as entries with `value: null`, never absent, which is what makes
  the "null, never missing" contract in §7 implementable rather than aspirational.

### Ranking: exact within a display group, invalid across groups

Products carry `_groupName` / `_groupId`, and **CR renders each group as a separate ratings
table**. Within a group, `_overallSortIndex` is a faithful score rank — **100% agreement in all
17 groups across 6 categories tested** (`RECON.md` §9g), including the two that looked worst
whole-category (Exterior Paints 69.5%, All-In-One Printers 83.7%).

The groups are exactly the `categories` filter's options; option counts match group counts in
every category tested.

**Two consequences, and the second is the important one:**

**1. Anonymous ranking within a group is exact, not approximate.** `_overallSortIndex` is
identical anonymous-vs-member (`RECON.md` §9g), so a caller with no membership gets CR's true
score ordering within each group. Not an estimate — the same ordering CR renders.

**2. `overallDisplayScore` is NOT comparable across groups, for members either.** A whole-category
sort by score mixes separately-scored populations. CR never displays it that way: on `c37162`
their own member page shows a 71 above a 79, because the 71 heads the 30-inch table and the 79
heads the 34-inch table. An agent told "the best French-Door refrigerator scores 79" when the user
wants a 30-inch model has been given a number from the wrong population.

So ranking is **group-scoped, with no opt-out**:

- `cr_ratings` returns `group` on every product and ranks **within** group. `rank` is a
  within-group position, and it is exact in both tiers.
- **The sort key is `_overallSortIndex`, in both tiers, always.** It is CR's own within-group
  ordering, identical anonymous-vs-member, and it orders tied scores the way CR does.
  `overallDisplayScore` is display data and is **never** a sort key — sorting on it would make
  members and anonymous callers disagree on `rank` for the 71 tied products measured on
  `c37162` (`RECON.md` §9g), breaking the "identical in both tiers" property that makes
  anonymous ranking worth anything.
- **A cross-group score sort is supported and structurally labelled, not refused.** An earlier
  draft refused it outright. That was wrong, and inconsistent with every other decision in this
  spec: the whole design answers "this data is misleading" with *structure* — `null` scores,
  `scores_available`, `dont_buy` in every shape — never by withholding. "Which is the best
  refrigerator" is a question people genuinely ask, the `overallDisplayScore` values are real CR
  numbers, and refusing it teaches an agent that the tool is broken rather than that the
  comparison is qualified.

  So `sort="overallScore"` with `group_mode="flat"` on a multi-group category returns the
  ordering, and the envelope carries `sort.scope: "cross_group"` — a machine-readable field, the
  same guard used everywhere else, not a prose caveat. `data.notice` additionally states that the
  groups are separately-rated populations. Every product still carries its `group` and its
  within-group `rank`, so the honest comparison remains available in the same response.
- A single-group category (Banks, Smartphones, Dutch Ovens, Bathroom Scales, Infant Car Seats)
  has no such problem: the whole category is one comparable population.

**Two earlier claims in this spec were wrong and are withdrawn.** That `sort="overallScore"`
"works fully anonymously" (it ignores group scope), and that the ordering is merely
`"cr_default_order"` with unpredictable fidelity (it is exactly score-ordered within group; the
apparent unpredictability was measuring across groups). See `RECON.md` §9g for both retractions.

### Deriving the Overall Score — rejected

Tested, not assumed: the best fit over all anonymous signals reaches R² 0.836 but with a
**13.1-point maximum error on a 36-point range** (`RECON.md` §4). Scores cannot be derived.

Two reasons this stays out of scope even if the fit improved:

1. An agent must never be handed something that reads like a CR score and is not one.
   `overall_score` is `null` or it is CR's actual value; there is no third state.
2. It would be manufacturing the number CR sells, out of the fields CR gives away. Nothing is
   accessed that should not be, which is exactly why the objection is reason 1 rather than a
   security one: the output would be a plausible fake, and a plausible fake is worse than a
   `null`.

### Anonymous is a real tier, not a degraded one

Reliability, owner satisfaction, CR Recommended, price, retailer spread and the full spec sheet
are all public. A user with no membership can legitimately shortlist on them. Only the Overall
Score and the per-attribute test scores are missing.

### The envelope is universal

Identical `{filters, data, args, attrs}` envelope and the same seven filter ids across 11
categories spanning appliances, electronics, home-garden and two service categories
(`RECON.md` §1). One parser, one code path, all of CR.

Two facts that constrain the tools: **attribute counts vary 2–24**, so `detail="standard"`
cannot assume three scored attributes exist (§7); and **categories exceed the `limit` cap of
200** — Televisions has 303 products and Mattresses 293 (`RECON.md` §12c). Both were invisible to
the sampling that produced the earlier "largest is 172" figure, because neither is in the A-Z
index. `offset` is load-bearing rather than precautionary, and `truncated` must be explicit.

**Mattresses is a 14.4 MB page** — larger than the 11 MB French-Door page every size estimate in
§7 and §8 is based on. The 11 MB figure is typical, not a ceiling.

### Category discovery — the A-Z index is not enough

**The A-Z index is incomplete, and an earlier draft of this spec treated it as the catalogue.**
The full 195-sitemap crawl has now been run (`RECON.md` §12a):

| Source | Categories |
|---|---|
| A-Z index | 236 |
| **Sitemap crawl** | **346** |
| Missing from the A-Z index | **110** |
| Missing from the sitemaps | **0** |

The index omits **47% more categories than it lists** — including **Televisions (`c28700`, 303
products)**, **Mattresses (`c28705`, 293)**, Bluetooth Speakers (178), Humidifiers (121), Garbage
Disposals, High Chairs, Homeowners insurance and Dash Cams. Every one serves a completely normal
payload.

That failure lands in the project's worst direction: an agent asking for TV ratings would get
`unknown_category` — "CR does not cover this" — for one of the most-rated categories CR publishes.

**So discovery is two sources, and the sitemaps are the authority:**

```
/sitemap.xml                    -> 6 sitemaps (products, cars, video-hub, cda, cq, espanol)
  /sitemaps/products.xml        -> 195 per-supercategory sitemap URLs
    /products/sitemap/{scid}    -> real XML; category URLs with /cNNNN/
  /sitemaps/cars.xml            -> 51 sitemaps: 50 makes plus a `types` index
```

- **The A-Z index stays** as the cheap warm path: one fetch, and it is the only source carrying
  **display names** (`RECON.md` §8), which `cr_search` needs on a cold cache.
- **The sitemap crawl is what makes coverage correct**: 195 small XML fetches, run once and cached
  on the same long TTL. It is real XML — no HTML scraping, and none of the nested-`<span>` trap
  the A-Z index has. **The union is a strict superset of the A-Z index** (zero A-Z-only ids), so
  trusting it loses nothing.
- **An id from either source is valid.** `unknown_category` is only correct after *both* have been
  consulted; until the sitemap pass has run, an unrecognised id is a cache miss, not a refusal.
- **A sitemap id is a candidate, not a guarantee.** One of ten sampled missing ids returned a
  `404` with no payload. A sitemap-sourced id that 404s is a **retired category**, not schema
  drift: it must not raise `payload_missing`, and it is marked dead in `category_index` rather
  than retried on every call. The tool response is **`unknown_category` with
  `reason: "retired"`** — distinct from `reason: "not_in_index"`, because "CR used to rate this
  and stopped" and "no such category" are different answers and an agent may want to say so.
  A third reason, **`"discovery_incomplete"`**, is returned when only one discovery source has
  run (the sitemap pass is pending, was aborted after consecutive challenges, or failed): the
  answer is not final, and the envelope also carries `warnings: ["sitemap_pass_pending"]`. A
  sitemap pass is recorded as authoritative only when at most 10% of its sitemaps failed.
  `products.xml` is fetched content, so the pass walks **at most 1,000** of the `<loc>`s it
  names (195 measured) and records at most 50 error strings; every failure is still counted for
  the 10% rule. A `<loc>` outside the host allowlist (§8 *Concurrency*) is refused by the
  transport unfetched and counted as a failure.

**When the sitemap pass runs, and what happens meanwhile.** 195 fetches at the 2-second politeness
interval is roughly 6.5 minutes — far too long to block the first tool call, and too valuable to
defer until something fails. So it runs **as a background task at startup**, and:

- The **A-Z index is fetched first and synchronously** (one request), so the common categories
  resolve immediately.
- A lookup that misses the index **awaits the sitemap pass** rather than returning
  `unknown_category` — the wrong answer is worse than a slow one, and this is precisely the
  Televisions case (§5).
- **That await is bounded, at `discovery.SITEMAP_AWAIT_TIMEOUT_S` (25 s).** "A slow answer beats
  a wrong one" holds only while the caller is still listening: the pass is 195 fetches, about
  6.5 minutes, and Claude Desktop kills a local tool call at 60 s (§9). Unbounded, a cold-start
  lookup of any sitemap-only id was a *dead* call rather than a slow one — and the ids it hit
  are the ones people ask for first: Televisions `c28700`, Mattresses `c28705`, Dishwashers
  `c28687`. Bounded, the caller gets the retryable `discovery_incomplete` answer inside its
  deadline, and because giving up on the wait never cancels the pass (`shield` keeps it running;
  `wait_for` cancels only the wrapper), the next call resolves normally. Pinned by
  `test_a_miss_does_not_wait_longer_than_the_client_will`.
- While it is still running, responses carry `warnings: ["sitemap_pass_pending"]`, so a caller can
  tell "not found yet" from "not found".
- **A pass that ends unrecorded is retried, not abandoned.** Offline at startup, `products.xml`
  failing twice, three consecutive challenges: the pass ends without a `discovery_runs` row and
  `sitemap_done()` stays false. The next lookup that misses the index re-runs it — after a
  `REFRESH_COOLDOWN_S` (300 s) cooldown since the previous attempt, only once the A-Z source is
  authoritative (unfetched, the answer is `fetch_failed` regardless), and only as a RETRY: the
  lifespan launches the pass, a miss awaits or retries it, so a miss with no pass ever launched
  is a request-free `discovery_incomplete`. `cr_categories(refresh=True)` retries an unrecorded
  pass the same way (never a recorded one — `force` is not a refresh). An earlier shape
  returned the finished task object forever, so `c28700` answered `discovery_incomplete` until
  restart however long ago the network had come back.
- It runs once per cache lifetime, not once per process: the 90-day TTL covers it, and a warm
  cache skips it entirely.
- **Six of the 346 live under `/cars/` paths** — tires, tire stores, dash cams, electric scooters.
  They are *products* on the products surface, not the cars API. Path prefix is not a reliable
  signal for routing between the two surfaces; the id space is.

The A-Z index still gives the franchise breakdown:

| Franchise | Categories |
|---|---|
| home-garden | 84 |
| appliances | 68 |
| health | 34 |
| electronics-computers | 22 |
| money | 15 |
| babies-kids | 13 |

**The fetcher must follow redirects**, and **the identity of a payload is `args.cid`, not the id
you asked for.** Some A-Z index URLs carry an extra path segment and redirect to a shorter
canonical path; a non-following GET returns a page with no payload, which looks exactly like
schema drift (`RECON.md` §9f).

Worse, a *subcategory* URL redirects to the parent's path **while keeping its own id** and serves
the **parent's** payload: `GET …/30-inch-and-narrower-widths/c200367/` lands on
`…/french-door-refrigerator/c200367/` and returns `args.cid = 37162` with all 172 French-Door
products, not the 8 in that width group (`RECON.md` §9h). Three ids can therefore disagree in one
request — the one asked for, the one in the final URL, and the one in the payload.

**The cache keys on `args.cid`.** Keying on the requested id would file French-Door's 172 products
under `c200367`; keying on the final URL would do the same. The requested id is recorded as an
alias in `category_index` so the lookup still resolves, but the row belongs to the payload's own
category.

One fetch, cached with a long TTL. No crawling.

**Every category fetch then enriches the index for free.** `args` carries the supercategory
(`scid`/`scat`), the sibling categories (`cats[]` — the *product types*) and the display groups
(`subcats[]`), and the page carries each sibling's score range and rated count (`RECON.md` §9h).
So one refrigerator fetch fills in the whole refrigerator family, which is what makes
`cr_categories(family=)` answerable without extra requests (§7).

**`cats[]` is not a list of category ids.** It mixes them with product-type entries whose `id`
is a slug — `washer-dryer-pairs` on Front-Load Washers `c28739` and Electric Dryers `c30562`
(measured 2026-09-06). An `int()` on one of those raised straight out of the cache write and the
tool, so both categories answered nothing at all. A slug keys no category, so ingest skips it
rather than guessing, and every other place CR content reaches `int()` in the cache layer goes
through the same guard: CR's payload is data, and no field in it is trusted to be the type its
name suggests.

The pseudo-categories are a trap rather than a feature: `200369` is both the "31 - 33 Inch Widths"
filter option and its own URL, but fetching that URL returns the **parent's** payload under a
different id — see the redirect rule above.

**Cars are absent from both product indexes, and they are a second architecture** — see below.
In scope, with their own discovery path (`/sitemaps/cars.xml`, or `v1/cr/keys`).

### Cars — a JSON API, and the one place the paywall is not in the payload

Cars share nothing with the products surface: no `filterInstanceDATA`, no `data-subscriber`, no
A-Z index (`RECON.md` §11). They are a clean JSON API on `cars-api.consumerreports.org`,
authenticated by a static **`x-api-key`** that CR publishes in every car page's own environment
block — read at runtime, never pinned — and **no cookie at all**.

| Endpoint | Purpose |
|---|---|
| **`v2/cr/cars`** | **The listing endpoint.** Filter by `slugMakeName`, `carTypeSlugName`, `modelYearStateId` — they combine, and **pagination works**. Carries identity, `safetyVerdictRatingScore`, `crPopularScore`, `fuelEconomySpecs`, `incentive`, `testStateName` and a generation summary, in one request |
| **`v2/cr/modelYears/{modelYearId}`** | **The road-test payload** — one model-year, ~57 KB, 1,085 fields. The only source of `overallTestScore`, `roadTestScore` and the per-test `ratings[]` |
| `v1/cr/keys` | The bare identity index: **50 makes, 677 models, 7,095 model-years** unfiltered (3.1 MB). Superseded by `v2/cr/cars` for listing; still the cheapest whole-catalogue enumeration |
| `v2/cr/carTypes` | 7 car types and their categories, with counts and price bands |
| `v1/cr/makes` | Every make |
| `v1/cr/glossary/1` | The attribute dictionary — the cars analogue of `categoryAttributes` |
| `v1/crReliabilitySurvey/modelYearId/{id}` | Survey detail |

**`v2/cr/modelYears` (the bare listing) is not used.** Its paging is broken — `page=` is echoed
back while the same ids return — and it carries no ratings. `v2/cr/cars` supersedes it
(`RECON.md` §13c-ii).

It is in several ways *better* than the products surface: `overallScoreSortIndex` is the direct
analogue of `_overallSortIndex`, and `ratingsCategory` ships `overallRank` plus
`overallTestScoreMax`/`Min` — so rank-within-category and the category's score range are explicit
rather than derived.

**Scale: 50 makes, 677 models, 7,095 model-years** — 6,698 Used and 397 New (`RECON.md` §13a).
The car catalogue is overwhelmingly historical, which makes used-car coverage the larger half of
the surface rather than an afterthought.

**The taxonomy repeats categories across car types, with different counts.** `v2/cr/carTypes`
holds 7 types and 58 category slots but only **39 distinct categories**; 15 appear under more than
one type, and five report different tested/untested counts depending on the parent
(`RECON.md` §13b). So **a cars category id alone is not a complete address** — the pair
`(carTypeId, categoryId)` is. This is the cars analogue of `_groupName` scoping a product ratings
table, and it means `cr_cars` takes both.

**Road-test scores are one request per car, and that shapes `cr_cars`:**

| Attempt | Result |
|---|---|
| `v2/cr/modelYears/{id}` | the ratings payload — full `testRatings` |
| `v2/cr/modelYears/{a},{b}` | **`400`** — no batching |
| `?modelYearId=a,b` | `200` but the **filter is silently ignored** |
| `?page=2` / `?page=3` | echoed back, **but the same ids return** — paging is broken |
| `?modelYearCarTypeId=` / `?categoryId=` | **ignored** |
| `?modelYearStateId=` | works (1 = Used, 2 = New), and switches to a lean id-only shape |
| the bare listing's entries | carry `cars[]` but **no `testRatings`** |

Twelve further probes found no bulk path: `?fields=`, `?include=`, `?expand=`, both `cr/compare`
routes, `cr/testRatings`, a `modelYearIds` list and a comma path all fail or are silently ignored
(`RECON.md` §13c-iii). So a listing of N cars **with road-test scores** costs N requests.

That is why `cr_cars` has a **`detail`** parameter rather than a score gate:

- **`detail="summary"` (default)** — one request to `v2/cr/cars`. Identity, `safetyVerdictRatingScore`,
  `crPopularScore`, fuel economy, incentives, whether CR has finished testing it. No per-car fetch,
  so `limit` can be generous.
- **`detail="standard"`** — additionally fetches `v2/cr/modelYears/{id}` for **the rows actually
  returned**, adding `overallTestScore`, `roadTestScore`, predicted reliability and owner
  satisfaction. Costs one request per row, so `limit` defaults to **10** and is capped at **15**.
  Measured 21.6 s at `limit=10` (~2.16 s per row at the 2 s politeness interval); Claude Desktop
  kills a tool call at 60 s (§6 *cr_sign_in*), and the earlier cap of 25 projected to ~51 s with
  nothing left for one slow response. 15 rows is ~35 s behind the listing request. `offset` pages.

The expensive thing is now visibly the caller's choice, which is better than a rule that withholds
it. **Cache every model-year individually** — with no bulk call, per-id caching is the whole cars
performance story rather than the "buys nothing" it would be for products (§8).

**Every `cr_cars` filter maps to a real API parameter** (`RECON.md` §13c-ii), so narrowing happens
server-side rather than by fetching broadly and discarding. **An unfiltered `cr_cars` is refused
before the request** — `invalid_filter_value` on `car_type`, naming the three CR accepts — because
`v2/cr/cars` itself answers an unfiltered call with a `400` requiring one of `slugMakeName`,
`modelYearStateId` or `carTypeSlugName` (`RECON.md` §13c-ii); `year` and `category` alone do not
satisfy it. Refusing locally is CR's answer for zero requests, and it is why a listing with zero
rows always has a filter to name in `no_results:<params>` (§7): an `offset` past the end is a
different fact — an empty *page* of rows the filters did match — and is answered as an empty
page with `total` intact and no warning, never as `no_results` and never as an error.

| Parameter | API filter |
|---|---|
| `car_type` | `carTypeSlugName` |
| `category` | `categoryId` — **requires `car_type`**, since a cars category is scoped by its parent (§5) |
| `make` | `slugMakeName` |
| `year` | `modelYear` — **validated against the catalogue's own span first** (`car_index` `MIN(year)`–`MAX(year)`, 2000–2028 measured 2026-09-06): CR answers a `modelYear` outside that span with a `400` (`RECON.md` §13c-ii — CR's accepted span is exactly the catalogue's on both ends, 1999 and 2029 both 400), which used to reach the caller as `fetch_failed(bad_request)` for an ordinary question about a 1990 Honda. Below the span, or more than one year above it, is `invalid_filter_value` with `filter: "year"` and the range in `candidates`, and costs no request. **Exactly max+1 is sent to CR**: the index is a snapshot (90-day TTL) and a new model year lands at max+1, so refusing it locally would report a false absence for cars CR lists until the index refreshes; CR either lists them or answers the `400`, which is translated to the same error. With the index not yet fetched every value passes through the same way, so the answer never depends on whether `cr_car_search` ran first |
| `state` | `modelYearStateId` — **defaults to unfiltered**, both New and Used |

`ratingsCategoryId` is silently ignored by CR; `categoryId` is the name that works.

**`size` on `v2/cr/cars` counts models, not model-years**, so `limit` is applied by the server
after the fetch and never passed through. `limit` maxes at **100 in `summary`** (no per-row cost)
and **15 in `standard`**, where each row is its own request. **Measured on the built code
(2026-09-03): CR caps `size` at 50, `totalElements` counts models too, and the model-year
population is `counts[name == "cars"]` — that is what `total`/`truncated` report.** The
implementation sizes each page from the rows it needs (≈8 years per model) and pages on.

**`offset` pages over CR's own order, and the only reordering is years descending within a
model.** Each call fetches a *prefix* of the population sized from `offset + limit`, so the order
it slices must be prefix-stable — a longer prefix must begin with the shorter one. A sort of that
prefix across models is not: an earlier draft sorted by `(make, model, year)` in Python and
sliced, and a native order differing from Python's string collation in one place (`McLaren`
before `MINI`) returned rows at more than one offset and others at none (measured on 96 rows at
`limit=25`: 25 model-years returned twice, 32 never returned). A page holds whole models
(`size` counts them), so a model's years always arrive together, and reordering *inside* that
run keeps every prefix a prefix of the same sequence whatever `size` a later call derives. **The
guarantee: paging
`offset` by `limit` until `truncated` is false reaches every model-year exactly once.** Cross-
model order is CR's, which is not documented as stable across releases; it is stable within a
session, which is what paging needs. **`total` is the `counts` population; when CR sends no
`counts` and pages remain it is `null`, never the fetched prefix** — a prefix is not a count of
anything, and `truncated` is then `true`.

**Never confirm a filter from `queryParameters`.** `modelYear` filters correctly yet is absent
from that echo, while an undocumented `carsPerModel` appears in it. The echo is not a contract.

**The api-key needs no browser.** `DRS_CARS_API_API_KEY` is extractable from any car page's
server-rendered HTML with a regex over a plain wafer GET (`RECON.md` §13d), so "read it at runtime,
never pin it" is implementable on the ordinary fetch path.

**Cars booleans are `"Y"`/`"N"` strings.** `expertRatings.isRecommended` ships as `"Y"` or `"N"`
(`RECON.md` §11c) — not a boolean, and **not** the products surface's `"Yes"`/`"No"`. It gets its
own mapping; reusing the products rule silently yields `null` for every car.

**`cr_cars` covers New and Used, and `state` defaults to both.** 6,698 of the 7,095 model-years
are Used and only 397 are New (`RECON.md` §13a) — CR's car catalogue is overwhelmingly historical,
and used-car ratings are among the most valuable things it publishes. A default of New would
silently discard 94% of the surface, so the default is **no state filter at all**; `state="new"`
or `"used"` narrows it. This is the one cars default where getting it wrong is invisible: the
results look reasonable, they are just missing almost everything.

**`scores_available` on a cars envelope only ever reports `available` or `absent`.** `unavailable`
means "cannot be determined from this payload — sign in to find out" (§7), and on cars there is
nothing to find out: the API never withholds, so an all-null field means CR published no value.

A key may also be **`null`, meaning "this response did not look"** — a state about the *request*,
not about CR's data. `cr_cars(detail="summary")` issues no per-car ratings request, so it reports
the road-test keys as null; reporting `absent` there would claim CR published no value for a car
nobody asked about. The same applies under `detail="standard"` when every per-car fetch failed —
and to **every key on a page with zero rows**: `any()` over nothing is False, so a `no_results`
page (or an `offset` past the end) used to report `safety_verdict: absent`, "CR published no
value", derived from rows the caller's own filters had emptied.
Null is never a data absence, and the two enum values keep their meanings wherever they appear.
An earlier draft added a fourth value, `members_only`, for a refused listing; there are no refused
listings.

**`403` means "no such route" on this host, not "forbidden."** Twelve of sixteen probed paths
returned `403` with a 43-byte body (API-Gateway behaviour). A cars `403` must never be reported as
an auth problem — that is the misreading this whole spec exists to prevent, wearing a status code.
wafer *returns* that response rather than raising: the body is JSON, which is never
challenge-detected, so its bare-403 branch rotates identity and then hands the response back —
measured on wafer 0.5.0 against a local server, **3 requests and ~2 s of rotation delay under the
anonymous policy, 1 request with a cookie** (`max_rotations=0`). The classification is the same
under both; the anonymous policy just pays for it, and counts each toward wafer's
`max_failures=3` (the third consecutive failure resets the session identity — the check is
`count >= max_failures`, read in wafer 0.5.0 — harmless anonymously, there is no jar to lose). A `Challenged` from this host is therefore a real WAF page, never a route.

#### IMPORTANT: the cars paywall is enforced in the UI, not the payload

**The cars API is fully available anonymously. There is no gating in the response at all.**
Verified twice and deliberately over-sampled: **15 fetches, SHA-256 byte-identical, zero
differences** — the ratings endpoint across six model-years spanning New, Used and a 2003 model,
the reliability-survey endpoint, the taxonomy, the glossary and the makes list, each fetched with a
public `x-api-key` alone and then again carrying a valid member `hash` (`RECON.md` §11d). One
payload flattened to 1,085 leaf fields with no member-only keys and zero nulls. The
gated-on-the-website numbers — `overallTestScore`, `roadTestScore`, `overallRank`, the category
score range, predicted reliability, `isRecommended` — all come back **populated** without any
session.

**A consequence worth naming: on cars, `null` means "CR has no value", never "you cannot see it".**
Untested model-years and used cars legitimately carry nulls, and since the API never withholds,
that is the only thing a null can mean. It is the exact inverse of the products surface, so a
cars `scores_available` derivation reports `absent` and **never** `unavailable`.

**CR does gate cars, in the presentation layer.** The anonymous car page reports
`window.isSubscriber = false`, renders the Overall Score *label* with no value, carries 39
"paywall" markers, and contains none of `overallTestScore`, `predictedReliability`,
`isRecommended` or the crash tests. The member page renders them.

So the cars API returns exactly what CR's own UI withholds. That is categorically different from
products, where the paywall lives *in the payload* and an anonymous fetch genuinely cannot obtain
a gated number.

**There is no control to defeat.** The endpoint requires no authentication, CR publishes the
api-key in its own page source, and what comes back is what CR serves to anyone who asks — the
same status as every free field on the products side.

**So cars are served in full, to everyone.** An earlier draft gated them on `window.isSubscriber`
and refused anonymous ratings requests, reasoning that CR renders those numbers only for members
and therefore intends them as paid content. That is withdrawn. It imported a products-shaped
assumption onto a surface where it does not hold, and it made the server return **less than a
plain HTTP request would** — inventing a restriction CR does not impose, on data CR publishes,
against the user's own interest. §5's own principle for products is that the anonymous tier is a
real tier and not a degraded one; cars get the same treatment.

**The design that follows:**

- **Cars have exactly one tier, so the cars tools carry no auth machinery.** No `data_tier`, no
  `scores_available: "unavailable"`, no `members_only`. A cars response is the same for every
  caller, which makes cars structurally closer to `cr_reliability` (§7) than to `cr_ratings`.
- **`window.isSubscriber` is still read** when a car page is fetched for the api-key, because it
  is a free confirmation of session health for the *products* side (§7). It gates nothing.
- **A cars `null` means "CR has no value", never "you cannot see it"** — the API never withholds,
  so `scores_available` on cars reports `available` or `absent` and never `unavailable`.
- **`car_raw` is tier-free**, like `reliability_raw`: one row per `model_year_id`, no `auth_tier`
  column, cached and served to anyone.
- **Cars are cache-first under the same contract as products (§7), stated once as a convention
  in `cars/repository.py`.** Every cars fetch runs through `cars.api.guarded`: one flight per
  key — `("car_page",)`, `("car_index",)`, `("car_taxonomy",)`, `("car", id)` — so N concurrent
  cold calls fetch the car page, the 3.1 MB `v1/cr/keys` and the taxonomy once, not N times; and
  a `Challenged` is negatively cached under that key for an hour like a challenged category. The
  orchestrator catches exactly `(FetchFailed, Challenged)` — never a bare `Exception`, never one
  of the two — and serves the cached row, index or taxonomy with `error: null`, naming the
  failure (`refresh_failed:<code>` on a served model-year, `index_refresh_failed:<reason>` on the
  index); the exception reaches the tool only when there is nothing to serve. An earlier build had
  three conventions in one package, and a 429 on a stale model-year was a tool error with the row
  in hand.
- **A cars index or taxonomy that parses to zero rows is `fetch_failed(empty_response)`,
  retryable — never written, never recorded.** `v1/cr/keys` answering `{"response": []}` used to
  be recorded as a fresh 90-day run: every search then answered `from_cache: true` with no hits,
  and an empty taxonomy made every `car_type` an `invalid_filter_value` with an empty legal list,
  for three months. The products path's `ensure_az_index` refuses zero rows the same way.
- **`states` is a list on every cars tool** — `cr_car_search`, `cr_cars` rows and `cr_car` alike.
  An earlier `cr_car.state` was a string for one state, a list for several and null for none,
  while `cr_cars` rows carried a string: one field, three shapes.

### Secondary endpoints (still unverified)

Harvested from `CR.global` / `initStore` config blocks. Useful for cross-category search;
not needed for category retrieval.

| Service | Host | Known path |
|---|---|---|
| Products | `products-api.consumerreports.org` | `/api/products/v1/` |
| Shopping models | same | `/api/products/v1/cr/shopping-models` |
| **Swagger descriptor** | same | referenced as `CRO_SWAGGER_PRODUCTS_API_DOMAIN`, path unknown |
| **Mobile app API** | `api.consumerreports.org` | unknown — labelled `CRO_APP_API_DOMAIN` |
| Cars | `cars-api.consumerreports.org` | `/api/cars/v1`, `/api/cars/v2` |
| Member profile (MPII) | `member-service-api.consumerreports.org` | `/v1` |
| Membership entitlements | `mfa.consumerreports.org` | unknown |
| Search typeahead | `www.consumerreports.org` | `/api/search/typeahead` |
| Semantic search | same | `/api/search/integration/typeahead` |
| AskCR | same | `/askcr` |

These carry a static `x-api-key` identifying the *app*, plus a member session carrying
the *entitlement*. The api-key is public — it ships in every page — so it is not a secret,
and it is read from the live page at runtime rather than pinned from a stale copy.

## 6. Auth design

This ships as a public open-source server, so auth must work for any user on any platform —
no assumption of macOS, Keychain, or a particular browser. The user logs in to CR normally,
in their own browser, and hands the server the resulting **session cookie**. No api-key, no
bearer token, no password, no login flow to automate.

### What recon settled

Measured against a live member session (`RECON.md` §5). The design-relevant conclusions:

- **`hash` is the durable credential.** 365-day expiry, and on its own it fully restores the
  session — first request returns `data-subscriber="true"` and 172/172 scores, re-minting every
  other member cookie including a fresh `userLicenses`.
- **`userLicenses` is sufficient but NOT necessary**, and it is short-lived: rotated on each
  re-mint, and dead ~24 h after the `t` stamp it carries. An earlier draft called it "necessary
  and sufficient"; the necessity half was untested and is false (`RECON.md` §5).
- **A LAPSED `userLicenses` vetoes the `hash` re-mint** (`RECON.md` §5, measured 2026-09-08).
  Past its ~24 h window it is not inert: CR stops following the durable cookie through
  `secure.` and serves the ordinary anonymous page, so the session reads `session_expired` on a
  credential with a year left. This is the one rule in this section that was learned by
  shipping its opposite — see *the store never holds both* below.
- **Re-minting works over plain HTTP, with no JavaScript** (`RECON.md` §5). A raw GET holding
  only `hash` recovers full member access on its first request. This is the wafer case exactly,
  so the 365-day durability reaches our client rather than being a browser-only property.
- **So the capture stores `hash`, and `userLicenses` only when it stands alone.** `hash` is
  what makes a session survive; a stored `userLicenses` bought one saved redirect and cost the
  session a day later. Rotations still accumulate in wafer's jar for the life of the process —
  that renewal is the mechanism and keeping it is not optional — they simply stop being written
  to disk. See *the store never holds both*.
- **`userToken`'s 1-day expiry is not a constraint** — it is regenerated from `hash` during
  ordinary browsing, which is why members are not asked to log in daily.
- **The cookies we need are not HttpOnly.** An earlier draft argued "Copy as cURL" was
  *required* on HttpOnly grounds; that generalised wrongly from `JSESSIONID` on the login host.
  **The console one-liner is now the documented gesture** and cURL the fallback — see
  *`consumer-reports-mcp auth`* below, where the reasoning that once favoured cURL is retracted.
- **hCaptcha enforcement is unresolved and permanently moot**, since no login is ever automated.

**Browser cookie import (`rookiepy`) is rejected on trust, not capability.** An earlier draft
claimed it was tested and non-functional; that test is unreliable — the Chrome failure is
consistent with a declined macOS Keychain prompt rather than a defect. The argument that stands
regardless: reading a user's entire browser profile and keychain to obtain one 197-byte cookie
is a large trust ask, in a tool whose whole security story is "we only ever hold a cookie you
handed us."

### The three tiers

Listed in fallback order — least configured first. **Precedence runs the other way**: when both
are present the env var wins over the stored file.

1. **Anonymous — the default, zero configuration.** Works out of the box with no account.
   Returns the full catalogue, prices, specs and filters (§5). Scores are null. Never an error.
2. **Stored session** — `~/.config/consumer-reports-mcp/session.json`, mode `0600`, written by
   `cr_sign_in` from inside the conversation or by `consumer-reports-mcp auth`. The normal
   desktop path.
3. **`CR_SESSION_COOKIE` environment variable** — a `Cookie:`-header-format string
   (`name=value; name=value`). **`hash` is the only cookie it must contain.** The path for
   containers, CI and headless hosts; takes precedence over the stored file when both exist.

**`hash` does not rotate** — measured: after a re-mint it keeps the same value and the same
expiry, while `userLicenses` is reissued fresh (`RECON.md` §5). This is what makes env-var mode
viable: a static string holding `hash` keeps working for the credential's full year, because
nothing the server would need to write back ever changes. Rotations of `userLicenses` are held
in memory for the process lifetime and simply re-minted on the next cold start.

**The store never holds both cookies — `durable_only`.** `userLicenses` is seeded and persisted
**only when no `hash` is stored**; beside a `hash` it is dropped on every path — loaded, pasted,
saved, or written back from the jar. The rule lives in one function in `credentials.py`
(BOUNDARY 4) and is applied by all four, because a store that refused to *send* it while still
*writing* it back would put the lapsed token in front of the next cold start exactly as before.

The reasoning is arithmetic, not caution. A stored `userLicenses` is written back mid-process
and read only by the NEXT process, so it is older than the session that sends it *by
construction*; it lapses at ~24 h; and past that it vetoes the re-mint (`RECON.md` §5). Against
that it saves exactly one redirect on the first request of a cold start and nothing after
(`RECON.md` §10a A3). One hop, weighed against losing the session every day the server is
restarted more than a day after it last ran — which for a desktop MCP server is most days.

Alone, `userLicenses` is the entire credential (a paste or an env var may carry only that) and
is kept and rotated as before. A `session.json` written before this rule self-heals when read:
the dead token is dropped in memory, and the file is rewritten without it at the next sign-in —
`load()` never writes.

**The store of record is `session.json`; wafer's jar is a working copy.** On startup the jar is
seeded from `session.json` (or `CR_SESSION_COOKIE`); rotations accumulate in the jar during the
run and are flushed back to `session.json` only for cookies that came from there — never for the
env-var path, which is read-only by nature. Re-running `auth` overwrites `session.json` and
discards the jar, so a fresh paste always wins over stale rotations.

### Driving this through wafer — measured in Spike A

`RECON.md` §5 was measured through Playwright, which proves what **CR** does but not what **our
client** does. **Spike A has since exercised this path end to end through wafer 0.4.9**
(`RECON.md` §10a, §10i): `hash` alone re-mints a full member session, a *superseded but not yet
lapsed* `userLicenses` alongside it still authenticates — a **lapsed** one does not, and that
distinction cost a session a day for two captures before it was measured (`RECON.md` §5) — and
nothing member-identifying reaches the cache. What follows
is therefore measured, not inferred — but the reasoning is kept because each rule exists to
prevent a specific failure that is still reachable.

**1. A rotation empties the cookie jar, and that manufactures a false `session_expired`.**
wafer's first rotation rung "rebuilds the wreq client (new TLS session, empty cookie jar)", and
after `max_failures` consecutive failures on a domain it "silently resets the session identity
(… cleared cookies for that domain, new cookie jar) and continues retrying. **It does not
raise.**" So a single transient 403 or empty 200 on a member fetch produces:

> retry without `hash` → CR serves a normal `200` anonymous page → `data-subscriber="false"` with
> a cookie configured → the server reports `session_expired`, tells the user to re-paste a cookie
> that is in fact perfectly valid, and appends an anonymous row to the cache.

That is this project's named worst failure arriving from the WAF side rather than the paywall
side. Two mitigations, both required:

- **The rotation policy is chosen once, when the session is constructed.** `max_rotations` and
  `max_failures` are constructor kwargs — wafer's per-request kwargs are `headers`, `params`,
  `timeout`, `attempt_timeout`, `max_response_size` and the body forms, and nothing else — so
  "member fetches rotate differently from anonymous ones" is not a thing a single session can
  express. Two sessions is not the alternative: `rate_limit` is enforced *per session*, so a
  second one would silently double the request rate against CR, which is the opposite of what
  any of this is for.

  So the decision is made at startup, from one fact: **is a cookie configured?**

  | Process state | Session constructed with |
  |---|---|
  | A cookie is configured | `max_rotations=0, max_failures=None` |
  | No cookie | wafer's defaults — the full rotation ladder |

  When a cookie is configured, **every** request through the session carries `hash` — the A-Z
  index, reliability pages and category pages alike — so a rotation on any of them empties the
  jar and poisons the next member fetch. There is nothing to protect by leaving rotation on for
  some of them. In no-rotation mode wafer *returns* 403/429/challenge/empty-200 instead of
  raising and rotating, so those statuses are classified here — `challenged` (§7) — with the
  credential still in the jar.
- **Assert the credential before classifying auth state.** Before any fetch is allowed to mean
  `session_expired`, check `session.get_cookie("hash", url) is not None`. A jar that lost the
  credential is `fetch_failed` with `reason: "identity_rotated"` (§7) — a transport failure,
  **never** an auth state, and its response is never cached. "The cookie is gone" and "the cookie
  was rejected" are different facts and only one of them is the user's problem. It is a
  `fetch_failed` *reason*, not its own code, and **the server does not retry it** — retries live
  in wafer and nowhere else (§7).

  **It is a handled path, and it is reachable today.** An earlier draft called it unreachable
  under the no-rotation construction. That is wrong: wafer's `_rebuild_client` discards the
  in-memory jar, and rotation is not its only caller — the AIA certificate-chain completion path
  calls it too, independent of `max_rotations`.

  A later draft said the path was unreachable "by version, not by design", on the grounds that AIA
  shipped only in an unreleased checkout. **That is now false.** `wafer-py 0.5.0` is on PyPI, its
  entire `v0.4.9..v0.5.0` history is the AIA feature, and the pin (§9) resolves to 0.5.0 on
  a fresh install — verified, along with `wafer._aia` being importable there. So this is an
  ordinary branch that can fire in production, not a defensive assertion, and it needs a test from
  the start.

  Two consequences for the fetch path. **Re-seed before the request, classify after it.** If
  `get_cookie("hash", …)` is absent *before* a request, inject it from the credential store — a
  precondition, not a retry, and cheap. If it is absent *after* a response that came back
  logged-out, that is `fetch_failed(identity_rotated)` and the row is not cached.

**2. An injected cookie needs a `Domain`, and the paste does not carry one.** `add_cookie` takes
a `Set-Cookie`-format string, but a "Copy as cURL" paste yields only `name=value` — expiry,
path and domain live in `Set-Cookie` responses, which is the same reason there is no expiry
countdown. So *we* choose the attributes, and the choice matters: wafer notes that a cookie
placed by something other than the session's own traffic "has no recorded host-only bit, so it
resolves on its exact host but is not offered to a subdomain", while the `hash` re-mint involves
a cross-origin redirect (`RECON.md` §5). The server injects
`hash=…; Domain=.consumerreports.org; Path=/; Secure` and Spike A proves the redirect chain
carries it.

**3. Rotations are read from the jar, not from the response.** `resp.cookies` is "cookies set by
THIS response" only, and the re-minted `userLicenses` most likely arrives on a redirect hop, not
the final one. Write-back therefore reads `session.get_cookie(name, https_url)` for the named
cookies after the request completes. (The `https` URL matters — `Secure` cookies are not returned
for an `http` URL.) This is also why `cache_dir` is not the mechanism: wafer persists only
*solver* cookies to disk, and normal `Set-Cookie` state "stays in-memory and is lost on session
rebuild" — consistent with `session.json` being the store of record, and confirming the flush
must be explicit.

**4. `max_response_size` is set.** Category pages are ~11 MB and reliability pages ~2.4 MB. A cap
of 64 MB costs nothing in normal operation and stops a drifted or hostile response from being
buffered without bound.

### `consumer-reports-mcp auth` — the onboarding

**The short path is the default, and it is 36 characters.** The user logs in to CR in their own
browser, opens the DevTools **Console**, pastes one line, and pastes the result into the command:

```js
document.cookie.match(/(?:^|;\s*)hash=([^;]*)/)[1]   // prints `hash`; copy it by hand
```

**`copy()` was the original gesture and is not used, because it fails silently.** DevTools' `copy()`
returns `undefined` whether or not it wrote to the clipboard, and it writes nothing when the console
is undocked or the page is unfocused — so the user gets an identical, reassuring `undefined` and an
empty clipboard, and the first sign of trouble is `auth` rejecting a paste they believe they made.
Printing the value fails visibly instead: either 36 characters appear or a `TypeError` says the
cookie is not there. The cost is that the token is on screen and in console history, on the user's
own machine, which is the lesser problem.

**An earlier draft made "Copy as cURL" the primary gesture, on reasoning its own later
measurements retracted.** That draft argued cURL was preferable because "a user told to copy *the
session cookie* will often grab only one; the whole header is what we actually need." The whole
header is **not** what we need: `RECON.md` §5 and §10i measured that **`hash` alone fully restores
a member session**, and §10i measured that it does so through wafer. Grabbing exactly one cookie
is now the correct outcome, provided it is the right one — and the one-liner selects it by name,
so it cannot be the wrong one.

The difference in user effort is not marginal:

| | Copy as cURL | The one-liner |
|---|---|---|
| Steps | open DevTools, Network tab, reload, find a request, right-click, Copy → Copy as cURL | open DevTools, Console, paste, Enter |
| What is pasted into the terminal | several KB, one line, nested quotes | **36 characters** |
| Failure modes | terminals mangling very long lines; shells reinterpreting quotes | none of note |

**Both are accepted**, because cURL is familiar and some users will reach for it anyway, and
because a `Cookie:` header is unambiguous. `--status` reports where the credential came from —
the env var or the stored file — not which paste form produced it.

> **One friction point to document rather than hide.** Chrome and Firefox refuse the first console
> paste as self-XSS protection and require the user to type `allow pasting` once. The command's
> help text says so up front — a user who hits that silently will conclude the tool is broken.

**Input is read from stdin, never from argv.** A pasted credential on a command line lands in the
shell history and in the process table, and a pasted cURL fights the shell's quoting. `auth` reads
until the paste is usable or EOF, so both `consumer-reports-mcp auth` (paste, then Enter for a
bare token; a cURL paste also returns as soon as its cookie header is read, and Ctrl-D is needed
only when the paste carries no `hash` or its last line is a `\` continuation) and
`pbpaste | consumer-reports-mcp auth` work.

The command then:

1. Accepts **a bare `hash` value**, a `name=value; …` cookie string, or a full cURL paste; keeps
   **`hash` and, if present, `userLicenses`**, discarding the rest. A bare 36-character token is
   treated as `hash`.
2. **Validates it immediately** against one real category fetch — **Robotic Vacuums (`c35183`,
   8 products), the smallest category measured**, not an 11 MB reference category — reporting
   `member session active` or `that cookie did not authenticate` with the reason. The fetched
   payload is written to the cache like any other, so the validation request is not wasted.

   **The probe category is exempt from the validate-the-id-first rule** (§7). That rule exists so
   a bad id does not cost four retried 5xx responses, but it checks against the index — and on a
   cold machine there is no index yet, so applying it here would force a 6.5-minute discovery pass
   before a user could log in. `c35183` is a constant this project ships, not user input.

   Paste-and-know, not paste-and-hope, and it exercises the same detection routine the server
   uses at runtime.

   **On failure nothing is written to `session.json`.** The spec previously left this open. A
   credential that just failed to authenticate is not worth persisting, and writing it would put
   the next process start into `session: expired` — turning a bad paste into a state the user has
   to clear before anything works. The anonymous payload the failed fetch returned *is* still
   cached: it is valid data regardless of who asked for it.
   **It also warns when `hash` is absent from the paste.** Validation tests sufficiency, not
   durability: `userLicenses` alone passes the fetch (`RECON.md` §5) while lacking the only
   durable credential, so without this warning a user would be told "session active" and lose
   it silently within days. This is the one case where a cURL paste is *worse* than the one-liner
   — it can succeed while containing no `hash` at all, which the one-liner cannot do.

   **`hash` alone costs one extra redirect on the first request and nothing after.** The re-mint
   is 2 hops on request one and 0 thereafter (`RECON.md` §10i A2/A3), so omitting `userLicenses`
   is not a performance concern worth asking the user to paste kilobytes for.
3. Writes the cookies to the stored-session file at `0600`, and thereafter **persists
   rotations** back to it, read from the jar per the wafer notes above. The flush happens after
   any fetch where `get_cookie("userLicenses")` differs from the stored value — not on every
   request, and never for the env-var path, which is read-only by nature.

`session.json` is deliberately minimal: the cookies, when they were captured, and — when the
browser sign-in read it off the jar — the durable cookie's own expiry:

```jsonc
{ "schema_version": 2,
  "cookies": { "hash": "…", "userLicenses": "…" },
  "captured_at": "2026-09-02T11:04:00Z",
  "expires_at": "2027-09-02T11:03:58Z" }   // null for a paste: it carries no attributes
```

Schema 1 had no `expires_at`; a schema-1 file loads unchanged as "expiry not measured", so
nothing stored before needs rewriting, and `load()` never writes.

Two more subcommands, both one-liners that the design needs and an earlier draft omitted:

- **`auth --status`** — reports the tier, the age of the capture, and whether `hash` is present,
  without a network call. The honest answer to "is my session still good?" without spending a
  fetch to find out.
- **`auth --forget`** — deletes `session.json`. There was previously no way to remove a stored
  credential short of `rm`, which is a poor answer in a tool whose security story is "we only
  ever hold a cookie you handed us".

**The countdown is measured when it can be, assumed when it cannot, and says which.** An
earlier draft promised `session_expires_in_days` as a fact, and for a PASTE that is
unimplementable: a `Cookie:` request header carries only `name=value` pairs — the real expiry
lives in `Set-Cookie` responses and the browser's jar, neither of which is in a paste. What is
knowable there is `captured_at`, and `hash` from a "remember me" login runs 365 days from the
mint without sliding, so for a paste `--status` reports `remaining_days_max = 365 - age`, labels
it `expiry_basis: "assumed"` and names it a maximum: capture can post-date CR's mint by any
amount, so the real deadline is that day **or earlier**, never later.

The browser sign-in is different: `context.cookies()` hands over the cookie's `expires`, and an
earlier version of this project read it and threw it away — it returned only the value, stored
only `captured_at`, and counted down from the constant. **A cookie captured 2026-09-05 stopped
working within about a day while the status said `remaining_days_max: 364`**, and the system
could not say whether that cookie had ever been durable, because the one fact that would have
answered — the expiry the browser had literally handed it — was never recorded. So now the
capture is the value AND the expiry (`browser_auth.Capture`), the expiry is stored as
`expires_at`, and `remaining_days_max` counts from it with `expiry_basis: "measured"`. It is
still a maximum — CR can revoke a cookie before its expiry — but it is CR's own date, not ours.
And a `hash` the browser reports with NO expiry (`expires: -1`, a session cookie) is not a
capture at all: that is "remember me" not having taken, CR issues a session that dies in days
(`RECON.md` §5), and stored under the assumed bound it reads as a year of life right up to the
day it dies — the exact failure above. It ends in `NotDurable` / `reason: "not_durable"`,
nothing stored, after a short grace for the durable cookie to land on a later redirect hop.

Inside 30 days the status warns, and says renewing means a fresh sign-in rather than another
request, because using the cookie buys no time. Reporting a bound the user can act on beats
reporting nothing about a credential that otherwise dies silently a year in — and reporting
which kind of bound it is beats reporting a number that could be off by a year.

What replaces it is honest and sufficient:

- **`hash` carries a 365-day expiry** and restores the session unaided over plain HTTP, so a
  captured session is expected to last about a year. That is CR's cookie policy plus a
  confirmed renewal mechanism, not a guarantee we can verify per-paste.
- **Expiry is detected per fetch**, not predicted: `data-subscriber="false"` with a cookie
  configured is `session_expired` (§7) — a distinct, actionable state meaning "sign in again"
  (`cr_sign_in`, or `consumer-reports-mcp auth`), never silently reported as `anonymous`.

The silent-expiry risk this was meant to address is therefore handled by detection rather than
prediction, which is the part that was ever load-bearing.

### Why not just take a username and password?

The obvious question, and the answer is not squeamishness — it is that the credential-login path
is worse on every axis that matters, while a better "just log in" option exists.

**What CR's login actually requires** (measured 2026-09-03 by reading
`secure.consumerreports.org/ec/login`; no login was attempted): fields `username`, `password`,
`setAutoLogin`, `rurl`, and three hidden captcha fields — `captchaToken`, `captchaEKey`,
`captchaSiteKey`. **hCaptcha is wired into the form.** No SSO, no MFA.

**Taking the password directly is rejected:**

- **hCaptcha is on the form.** Driving it programmatically means solving a CAPTCHA — ruled out by
  §3, and in practice it means paying a third-party solving service, which is both a ToS problem
  and a dependency no one should have to trust to read appliance ratings.
- **A password is a categorically larger trust ask than a session token.** `hash` reads ratings.
  A password is account takeover: billing, address, order history, and whatever else the user
  reuses it on. This project's entire security story is "we only ever hold a cookie you handed
  us", and that story does not survive a password field.
- **It would have to be stored or re-prompted, and both are bad.** An MCP server starts
  unattended, so "prompt each time" does not work; storing a password is strictly worse than
  storing the token it would fetch.
- **Automated POSTs to a login endpoint is the single most flagged pattern there is.** The
  realistic downside is not that it fails — it is that it locks the user's account.

### `auth --browser` — the assisted path, IN SCOPE for v1

**The good version of "just log in" is browser-assisted, and it ships as an optional extra.** Open
a real browser at CR's real login page; the **user** types their own password into CR's own form
and clears the captcha themselves if it appears; the server harvests the resulting cookie and
keeps only `hash`. No password ever reaches this codebase, the captcha is solved by a human
because a human is present, and the traffic is a genuine login because it *is* one.

```bash
uv run consumer-reports-mcp auth --browser
```

**Installed separately, because a browser is a heavy dependency to impose on a paste.**
`uv sync --extra browser` adds Playwright. Absent, `--browser` exits naming that command and
pointing at the console one-liner — never a traceback. (From PyPI the equivalent is
`uvx --from "consumer-reports-mcp[browser]" consumer-reports-mcp`, §9.)

**The flow, and every step of it is a constraint:**

1. **Launch the installed Chrome with `channel="chrome"`, `headless=False` — and without
   advertising the automation:** `args=["--disable-blink-features=AutomationControlled"]` and
   `ignore_default_args=["--enable-automation"]`. `channel` uses the browser already on the
   machine instead of downloading ~150 MB of Chromium. Headed is not a preference: the user has
   to type and may have to clear a captcha. And the window must read `navigator.webdriver ===
   false`: CR's form runs an invisible hCaptcha on every submit that keys on that bit — measured
   (`RECON.md` §5 *Login*), `true` opens a puzzle and issues no token, `false` issues one silently
   in under a second — and a puzzle solved by a human in a flagged window still came back "We
   still don't recognize that sign in" for credentials that work in plain Chrome. The blink flag does
   the work (Playwright 1.62 passes no `--enable-automation`); the ignore covers the older
   releases the `>=1.40` floor admits. If no Chrome-family browser is found, say so and name the
   paste path rather than downloading one silently.
2. **A fresh, throwaway context — never the user's real profile.** `browser.new_context(
   no_viewport=True)` with no `storage_state`: the page sees the real window and screen rather
   than Playwright's emulated 1280×720 (a `screen` equal to the viewport with no menu bar — a
   shape no real desktop has). This is the same trust line that rejected `rookiepy` (§6): reading a user's
   whole browser profile to obtain one cookie is a large ask, and it is not made acceptable by
   Playwright doing the reading. The user logs in inside a clean window; the context is discarded
   when the command exits. It follows that being already logged in elsewhere does not help, and
   that is the correct trade.
3. **Navigate to the login page and keep `setAutoLogin` checked — on every poll, not once.**
   That box mints the durable 365-day `hash` (`RECON.md` §5); without it the captured session
   dies in days and looks exactly like the tool breaking a week later. Ticking a checkbox on the
   user's behalf is not touching their credential, and the alternative — an instruction they can
   miss — fails silently and much later. The tick is re-asserted each poll (`page.check` on an
   already-checked box returns without clicking, scrolling or focusing, so it is invisible while
   the user types; a 250 ms budget keeps a page with no form from stalling the poll). Measured
   2026-09-07 on the real page: CR renders the box `checked="checked"` server-side on both the
   plain page and the `?error` page a failed submit lands on, the form is a plain
   `POST /ec/login`, and the login script never touches it — so a re-render does not lose the
   tick, and the per-poll guard covers what a one-time tick did not: the user un-ticking it, or
   CR changing its default.
4. **Then wait, and do nothing else.** Poll `context.cookies()` for a `hash` cookie, up to a
   generous timeout (default 5 minutes, `--timeout`). **The server never reads, fills, or inspects
   the username or password fields**, and it never submits the form. If the user closes the window
   or the timeout elapses, exit non-zero saying nothing was captured. `context.cookies()` is
   every cookie in the window, so the capture is **CR's `hash` and only that**: the cookie's
   domain must be `consumerreports.org` or a subdomain of it, and the value must be the
   36-character token shape the paste path accepts. Anything else keeps the poll going — a stray
   `hash` from another site used to be returned as the capture, and the flow then failed
   permanently while blaming remember-me. **And it must be durable**: the capture is the value
   together with the cookie's `expires` (`Capture(value, expires_at)`), and a `hash` reported
   with none (`-1`, a session cookie) — or with an expiry under `MIN_DURABLE_S` (two days: the
   2026-09-05 capture lived 24 h, and a credential that dies tomorrow must not sit behind
   "member session active") — is "remember me" not having taken. After a short grace for the
   durable one to land on a later hop, the poll ends in `NotDurable` naming the measured life,
   and nothing is captured. See *The countdown is measured when it can be* above for why
   silently accepting it is the worst outcome this flow has. **Every `hash` the window
   holds is logged once with its attributes — domain, path, flags, expiry — and never its
   value**: that line is the fact the 2026-09-05 capture never recorded. Among several
   durable ones the capture is the one on the apex domain (what the jar seeds) with the
   farthest expiry, not whichever the browser listed first.
5. **Harvest `hash` only, then validate and store exactly as the paste path does** — same
   `c35183` validation fetch, same `0600` write, same warning if `hash` is somehow absent, plus
   the measured `expires_at`, which is the one thing this path stores that the paste cannot.
   From step 5 on there is one code path, not two.
6. **Close the browser and discard the context** whether it succeeded or failed — on every exit
   path (success, timeout, the user closing the window, `cancel()`, task cancellation,
   interpreter shutdown), and **bounded**. `context.close()`/`browser.close()` wait for a reply
   from Playwright's driver and have no timeout of their own; at loop shutdown `asyncio.run`
   cancels every task in one sweep, the driver's pipe reader included, so that reply can never
   be read and an unbounded close deadlocks the interpreter with the window still open
   (measured: a harness parked in `kevent` for 26 minutes). **That wedged-but-alive process is
   the only source of an orphaned browser.** A *killed* process leaves none: Playwright's
   driver `SIGKILL`s the browser's process group from its own exit hook the moment its stdin
   closes, and `SIGTERM` or `SIGKILL` of the Python process mid-capture was measured (real
   Chrome, Playwright 1.62) to leave nothing behind. So the bounded teardown is the fix, and the
   pid kill is scoped to the one case it covers — Python alive, driver unresponsive. The
   teardown shares one `CLOSE_TIMEOUT_S` budget for context and browser, then
   `DRIVER_STOP_TIMEOUT_S` for the driver stop — and a step that overruns is **abandoned in its
   own task, never cancel-and-waited**: a Playwright call absorbs the first cancellation
   (`_inner_send` catches it, sends `__abort__` and awaits the abort's own reply), so
   `asyncio.wait_for` — which cancels once and then waits — hangs exactly as before (measured:
   the timeout fired, `cancelling()` read 2, and only a third cancel woke the task). The
   browser's pid is read at launch through a browser-level CDP session
   (`SystemInfo.getProcessInfo`, ~6 ms), and whatever the graceful path does not confirm closed
   is `SIGTERM`ed by pid — Playwright launches Chrome in its own process group and it takes its
   helpers with it (measured on macOS) — from the capture's `finally` and, for a loop torn down
   under the capture, from an `atexit` reaper. A pid is only ever signalled while the process
   table still shows a Playwright-profiled browser on it (the recycled-pid guard):
   `/proc/<pid>/cmdline` on Linux — and an EMPTY `cmdline` is no answer: the kernel empties it
   for a zombie, a kernel thread and a child still inside `execve`, so it falls through to
   `ps -ww`, which tells the three apart (`<defunct>`, `[kthread]`, the real argv once exec
   lands); reporting `unreadable` there lost the first Linux CI run a race against a child the
   test had just spawned — `ps -ww` where there is no procfs (macOS), `Get-CimInstance
   Win32_Process` on Windows, because `tasklist` has no command-line column and cannot see the
   profile marker. The query answers `(text, status)` — `found`, `gone`, `unreadable` or
   `query_failed:<why>` — and only `found` can match: the first CI run (2026-09-06) found
   that a plain `ps` on Linux cuts a piped line at 80 columns, before the marker, so the guard
   had never matched a live Chrome there, and that on Windows the real Chrome came back as a
   bare None that could have meant any of three things. The Windows script is a base64
   `-EncodedCommand`, writes to `[Console]::Out`, maps each outcome to an exit code under
   `$ErrorActionPreference = 'Stop'`, and runs under `COMMAND_LINE_TIMEOUT_S` (15 s — a cold
   WMI provider host outlasted the old 5 s). The query is decoded with `errors="replace"`
   (both markers are ASCII; Windows PowerShell 5.1 writes the OEM code page while Python
   decodes the ANSI one, and the profile path carries the user's name — a strict decode raised
   `UnicodeDecodeError`, which is not an `OSError`, out of the guard) and, on Windows, with
   `CREATE_NO_WINDOW` (a console program spawned from a console-less process opens its own
   console window). On Windows `os.kill` is `TerminateProcess` of the browser process alone —
   there is no process group; Playwright spawns the browser `detached` only off Windows — and
   the driver's own exit hook there is `taskkill /pid <pid> /T /F` (read in the 1.62 driver),
   so a killed Python leaves nothing on Windows either. The Windows branch is pinned by
   `tests/test_browser_auth.py` and MEASURED by the CI workflow (`.github/workflows/ci.yml`):
   the guard test runs the real `Get-CimInstance` query on a Windows runner, and
   `tests/live/test_browser_hardware.py` (`CR_BROWSER_LIVE=1`, no CR traffic) kills a real
   Chrome by pid and a Python mid-capture on Windows, macOS and Linux runners. Note that `src/`
   installs **no `SIGTERM` handler**: a Desktop `SIGTERM` ends the process without running the
   capture's `finally` or `atexit`, and is harmless only because of the driver's own hook. The Playwright import itself (0.28 s cold;
   the Desktop bundle ships no `.pyc`) runs on a worker thread, never on the loop.

**It never touches the data path.** Playwright obtains a cookie and exits; every subsequent fetch
is wafer (§9). A browser is not a fallback for a blocked request — that remains "back off and
surface `challenged`" (§11).

**The launch is a ladder, and nothing on it downloads anything.** `channel="chrome"`, then
`channel="msedge"`, then `google-chrome` / `chromium` / `chromium-browser` / `brave-browser`
resolved on `PATH`. Channels resolve the browser from its install location rather than `PATH`,
which matters under a Desktop-launched process whose `PATH` is minimal. `BrowserNotFound` fires
only when every rung fails, and says which were tried. A closed window is `WindowClosed`, a
subclass of `CaptureTimeout` so every existing caller still catches it, distinguishable for the
one that wants to say so.

### `cr_sign_in` — the in-conversation path, and the Desktop constraints that shape it

**This is the primary onboarding.** A Claude Desktop user may never open a terminal, so "run
`consumer-reports-mcp auth`" is not an instruction they can follow. `cr_sign_in` does what
`auth --browser` does, from inside the conversation: the installed browser opens on CR's own
sign-in page in a throwaway context, the user signs in there, the server keeps `hash`, validates
it against the same `c35183` probe through the same routine (`auth_tools.validate_cookies`, which
the CLI now calls too), stores it `0600` — and then **adopts it without a restart**.
`auth --browser` and the paste remain, demoted: the CLI for people who have a terminal, the paste
for the machine where no browser window can open.

Three facts about the host dictate the shape, and none of them is negotiable:

1. **Claude Desktop kills a local tool call at 60 seconds, and progress notifications do not
   extend it.** A human sign-in takes as long as it takes — minutes, a captcha, a password
   manager — so a tool that blocks until the cookie appears is killed mid-flow on the one client
   this exists for. Claude Code has no such limit, which is exactly why the problem is easy to
   miss when developing against it. (The same cap is why `cr_cars(detail="standard")` is now
   capped at 15 rows, §5.)
2. **Elicitation is unavailable.** Desktop declares no elicitation capability to a local stdio
   server, so the protocol's own "ask the user for input" cannot carry the flow — and it should
   not carry a password anyway (*Why not just take a username and password?*).
3. **The transport fixes its auth policy at construction** (rule 1 above: `max_rotations=0,
   max_failures=None` iff a cookie is configured, chosen once, and `rate_limit` forbids a second
   session). A cookie saved mid-process therefore changed nothing until the next start.

So the design is **non-blocking start, status poll, live adoption**:

- **`cr_sign_in(force=false)`** starts a background task and returns within ~2 s wearing the
  same outer envelope as every other tool (§7): `{session, warnings, error, data}` with
  `data: {status, reason, instructions, browser, expires_in_s}`. `error` is always null — a
  refusal or failure is a `data.status` with a machine-readable `data.reason`, not an
  error-taxonomy entry. The task runs `capture_hash()` → `validate_cookies()` → `store.save()`
  → `transport.adopt()`. It waits up to 1.5 s for the browser to report so that a launch
  failure is answered by the call itself (`status: "failed", reason: "browser_not_found"`)
  rather than by a later poll; a slow launch is reported as `waiting` with `browser: null` and
  the poll fills it in. The answer is whatever phase the task is in when the wait ends — a
  task that runs ahead of it (a capture and validation that never suspend on real I/O finish
  in one scheduler tick) is answered `validating` or `active`, never a `waiting` that
  describes a window already closed beside `session: active`.
- **`cr_auth_status(wait_s=0)`** returns `{session, warnings, error, data}` with
  `data: {source, captured_at, expires_at, expiry_basis, days_left_max, sign_in, reason,
  browser}` — `expiry_basis` is `measured` (the browser read CR's own expiry, in
  `expires_at`) or `assumed` (a paste: 365 days from capture) — and `days_left_max` counts
  from whichever applies. With `wait_s > 0` it
  long-polls for a phase change, capped at **45 s** — under the 60 s limit with margin for the
  round trip — and returns immediately when nothing is in flight. `sign_in` is
  `idle | verifying | waiting | validating | active | refused | failed`.
- **Idempotent while one is in flight**: a second call answers `in_progress` and opens nothing,
  `force` included.
- **The guard verifies; it does not refuse blind.** `force` means one thing: *replace a cookie
  verified live*. Without it, what happens depends on what is actually known:
  - **`none` or `expired`** (nothing stored, or CR rejected it): proceed — the renewal case.
  - **Past the cookie's own bound** (`days_left_max <= 0` — CR's measured expiry when the
    browser recorded one, else the assumed 365-day upper bound, §6 *renewal*): proceed, whatever
    `session` says — the bound says it cannot be live, so there is nothing to verify and nothing
    worth refusing for.
  - **`active`** (a marker-bearing fetch in THIS process saw it live): refuse, `reason:
    "session_active"`. The message may say the cookie works, because that is known.
  - **`unverified`** (stored, no marker-bearing fetch yet): **resolve it** — the task starts in
    a `verifying` phase that runs the same `validate_cookies` probe against the STORED cookie
    (one request, so the call still answers inside its launch wait when the probe is quick, and
    says `status: "verifying"` when it is not). The verdict is reported to
    `health.on_probe_verdict()` (the probe IS a marker-bearing fetch): `member` → `active` and
    the poll reports `sign_in: "refused", reason: "session_active"`; `session_expired` /
    `credential_rejected` → `expired` and the window opens;
    anything else (`could_not_check:<reason>`, a drift code) is no verdict on the cookie, so no
    window opens on a guess and the poll reports `failed` with that reason.

  Why not refuse on `unverified`, as an earlier draft did: health only ever leaves
  `unverified` on a marker-bearing fetch (a category page, the car page), and in a fresh
  Desktop process nothing may ever make one — `cr_categories`, `cr_search`, reliability and the
  cars API never move it. A guard that refuses `unverified` therefore refuses a DEAD cookie
  forever, with a message asserting it works, and the user recovers only by discovering
  `force`. That makes the guard advisory rather than protective, and renewal was the whole point
  of the feature. The earlier-still draft that fired on `active` alone had the opposite defect
  (a signed-in user got a window on every Desktop start); the probe is what resolves both.
- **The other refusals, each with a `reason` the agent can act on:** `env_override`
  (`CR_SESSION_COOKIE` is set, so a stored cookie would be ignored on the next start — the
  message names where to clear it: the extension's settings in Desktop, the MCP server entry's env
  block, or the shell; a blank value, the Desktop field left empty, is not an override, and
  neither is an unexpanded `${…}` template a host might pass through for that empty field),
  `browser_extra_missing` (names the install command and the paste path; checked BEFORE any
  probe is spent), `offline`.
- **Failures the poll reports:** `browser_not_found`, `window_closed`, `capture_timeout`,
  `not_durable` (CR issued a session-only `hash` — no expiry, remember-me did not take —
  nothing captured, the text says to leave "Remember Me" ticked),
  `session_expired` / `credential_rejected` (the cookie did not authenticate — nothing stored),
  `could_not_check:<reason>` (a transport failure is not a verdict — nothing stored, as in the
  CLI; from the pre-flight check it also means no window was opened), `save_failed:<type>`,
  `internal_error:<type>` (an unexpected exception anywhere in the task — a catch-all, so a live
  phase can never outlive the task), `cancelled` (server shutdown; the task reads
  `cancelling()` before `save()`, so a cancellation swallowed on the way cannot turn
  `cancelled` into a stored, `active` session behind the caller's back).

**Live adoption is `Transport.adopt()`, and it is the risky piece.** Under the transport lock it
re-reads the store (the store, not an argument, so it cannot disagree with what the next session
will seed), flips `cookie_configured`, clears the rejection latch (a rejection was about the old
cookie), moves `SessionState` to the cold-start state for the new configuration via
`reconfigure()` (`unverified` with a cookie, `none` without — every other `SessionState` event
is a no-op while unconfigured, by design), and drops the session so the next request builds one
under the new
policy. Cache tier qualification already reads `health.effective_tier` per call (§8), so the
anonymous row stops qualifying and the next call refetches under the cookie — verified by test,
not assumed. Two safety rules:

- **A request that straddles an adopt is discarded**, as `fetch_failed(policy_changed)` (§7):
  retryable, never cached, never classified. It was made under the old credential (or none);
  classifying it under the new one would let a rejection of the *old* cookie latch
  `rejected`/`expired` onto the *new* one, or read a logged-out page against a jar the new policy
  has not seeded. The transport keeps a generation counter for exactly this. The cars API has no
  credential logic and is unaffected. **And it is retried exactly once by every caller that
  owns a whole tool call** — `get_category`, `get_reliability`, `CarsApi.page_info`, and the
  sitemap pass (`transport.retry_once`) — because it is *our own* state change, not a network
  condition, and the likely real sequence is mundane: the sign-in starts, the user takes a
  minute, the agent runs an 11 MB ratings fetch, validation completes mid-fetch. Without the
  retry that surfaced as `fetch_failed`. Worse, `products.xml` failing this way in the first
  seconds of startup left `sitemap_done()` false for the whole process — `c28700` answered
  `unknown_category / discovery_incomplete` until restart — so `products.xml` is retried once on
  any retryable failure, and a single per-supercategory sitemap lost to `policy_changed` is
  retried rather than skipped (skipped, its categories would be absent from an index recorded
  as authoritative: a confident `not_in_index` for a category that exists). A second
  `policy_changed` in a row still surfaces; nothing loops.
- **In-flight requests keep their reference to the old session and finish on it.** wafer 0.5.0's
  `AsyncSession` has no `close()`; its `__aexit__` releases an owned challenge solver and nothing
  else, so retiring the old session interrupts nobody.

After a successful sign-in the flow marks the session `active` rather than `unverified`: the probe
fetch saw `data-subscriber="true"` with this exact credential moments ago, in this process, which
is the marker-bearing fetch the rule asks for.

**The security bounding.** `cr_sign_in` is annotated `readOnlyHint=false`,
`idempotentHint=false`, `openWorldHint=true` — deliberately not the shared read-only annotations
object — so both clients prompt before running it; and its description carries the consent rule
("only when the user explicitly asks"), because the description is the one thing an agent is
guaranteed to read. It never returns or logs the cookie: the token travels `capture_hash()` →
validation → `save()` and appears in no envelope, no `reason`, no log line. An unwanted
invocation can open a window on the user's screen and, if the user then signs in, store a cookie
the user chose to mint — it cannot read a password, cannot act without the user typing, and
cannot override `CR_SESSION_COOKIE`. The residual risk is a client's "Always allow": after that
the prompt is gone and only the description stands between an agent and an unasked-for window.
That is why the description says what it says.

**Renewal is the part that matters in month eleven.** A "remember me" `hash` is fixed at 365
days from the mint and using it never extends it, so every session dies, on schedule, a year in.
Two signals:

- **`warnings: ["session_expiring:<days>"]`** on every tool that carries `session`, once
  `remaining_days_max` is inside 30 days. That number counts from CR's own expiry when the
  browser sign-in recorded one (`expiry_basis: "measured"`), and from 365 − capture age only
  when nothing was measured (`"assumed"`: a paste, or a session stored before expiries were
  recorded) — an upper bound that can overstate the life left by up to a year, which is how a
  2026-09-05 capture died within a day while this warning stayed silent at 364. `<days>` is
  that number, truncated and clamped at 0. Not emitted once the session is already `expired` —
  the envelope says `session_expired` then, and "expiring" on top of it is noise. The env-var
  path has no capture date, so no bound and no warning. `--status` and `cr_auth_status` report
  the same number, as `days_left_max`, together with `expires_at` and `expiry_basis` so a user
  can tell a measurement from an assumption.
- **The `session_expired` notice names the fix**: `cr_sign_in` first, then
  `consumer-reports-mcp auth`, then "update `CR_SESSION_COOKIE` if it came from there".

Renewal is then a plain `cr_sign_in`: a dead cookie — one CR has rejected in this process, one
the pre-flight check finds rejected, or one past its own 365-day bound — proceeds without
`force`. A fresh sign-in with "remember me" mints a new 365-day cookie, the flow validates,
stores and adopts it, and nothing restarts. `force=true` is only for replacing a cookie that is
verified live — renewing early, before the last 30 days. The Desktop bundle that makes all of
this installable is §9 *Claude Desktop bundle*; the pattern, written up for reuse in another
server, is `MCP-AUTH-PATTERN.md`.

### Not done, deliberately

- **No password is ever requested, stored or transmitted.** Not in an env file, not in the
  repo, not in a keyring. The server has no code path that submits a login form.
- **No CAPTCHA solving**, and no automated login. A human logs in; the server only reads the
  result.
- Cookies are never logged, never written to the cache, and never echoed back in tool output.
- **No response body ever reaches tool output, a warning, or a log line.** Errors carry the
  status, the `reason` and the page `<title>` — never the body. wafer's own guidance is that a
  failure response "may be a full WAF challenge page with embedded tokens/sensor data — do not
  log its body or headers unscrubbed", and a CR *member* page additionally embeds profile data
  (`userInfo` is a 320-byte profile cookie). This is the rule that keeps a debugging session
  from becoming a credential leak.
- **The cache holds response data only — now measured, not asserted.** Spike A6 (`RECON.md`
  §10i) diffed a member fetch against an anonymous one: the `args` block is identical with no key
  differing in value, and neither the credential values, nor `userInfo`, nor any email-shaped
  string appears in the `filter_instance` or `.cat` slices that get written to `cr.db`.

If measured session lifetime turns out to be short enough to make re-pasting a burden, an
optional Playwright-based `login` extra is the additive follow-on — it produces the same
cookie through the same interface, so it changes nothing downstream. **This shipped as
`auth --browser`** (§6) — an optional extra, in scope for v1.

## 7. Tool surface

Designed around questions, not endpoints.

**Filtering is local, not proxied.** One fetch returns the whole category, so filters are
operations over cached data rather than request parameters. Zero extra requests, and
combinations CR's own UI does not offer are free.

**One tool set, 346 categories.** Category is a parameter, never a tool name — a tool per
category would mean over a thousand tools.

| Tool | Returns |
|---|---|
| `cr_categories(franchise?, family?, refresh?)` | Categories with ids, slugs, franchise, **product family, score range and display groups** — so the agent stops guessing them, and can see a whole family at once |
| `cr_filters(category)` | The filter taxonomy for a category — what is filterable, and the legal values |
| `cr_ratings(category, group?, brands?, price_min?, price_max?, recommended?, features?, sort?, order?, group_mode="nested", attributes?, limit?, offset=0, detail="standard", refresh?)` | Models ranked **within display group**: Overall Score, price, CR Recommended, attributes per `detail` |
| `cr_product(id, include_descriptions=false, refresh?)` | Full record: all scored attributes, specs, owner satisfaction, retailer prices |
| `cr_search(query, refresh?)` | Model name or number → candidate products **across cached categories** — see below |
| `cr_reliability(category, detail="standard", include_methodology=false, refresh?)` | **Brand-ranked** predicted reliability and owner satisfaction — fully anonymous |
| `cr_car_search(query, refresh?)` | Make / model / year → `modelYearId`, from the cars index (§5). Anonymous |
| `cr_cars(car_type?, category?, make?, year?, state?, detail="summary", limit=25, offset=0, refresh?)` | Model-years in a car type or by make. `summary` is one request and carries safety verdict, popular score, fuel economy and incentives; `standard` adds road-test scores at one request per row (`limit` then defaults to 10, max 15) |
| `cr_car(model_year_id, detail="standard", refresh?)` | One model-year in full: Overall Score, road-test score, per-test ratings, predicted reliability, owner satisfaction, crash tests, specs |
| `cr_sign_in(force=false)` | Connect or renew the membership from inside the conversation: checks a stored-but-unverified session against CR first and opens the installed browser on CR's own sign-in page only if there is no live session; returns within seconds and the outcome arrives via `cr_auth_status`. `force` replaces a session verified live. Only on the user's explicit request (§6 *cr_sign_in*) |
| `cr_auth_status(wait_s=0)` | Session health, where the credential came from, an upper bound on the days it has left, and the phase of any sign-in in flight; `wait_s` long-polls up to 45 s. Never the cookie |

**Nine data tools, two architectures, plus two auth tools.** The first six are the products
surface, keyed on a category id and reading the embedded page payload. The next three are cars,
keyed on a `modelYearId` and reading the cars JSON API. They are deliberately separate tool
families rather than a `type="car"` parameter: the id spaces, the auth marker, the paywall
mechanics and the response shapes have nothing in common, and collapsing them would hide exactly
the distinction that matters. The last two are not data tools at all: they exist because a
Claude Desktop user has no terminal to run `auth` in (§6 *cr_sign_in*).

Full parameter semantics in **Tool contracts** below; every signature there is the one to code
against.

### `cr_ratings` output: nested by group, flat when filtered

The shape follows the group scoping in §5 rather than restating it as a warning.

**Multi-group category, no `group` argument → nested**, with `limit` applying *within* each group:

```jsonc
{
  "groups": [
    { "group": "30 Inch and Narrower Widths", "size": 8, "total": 8, "truncated": false,
      "products": [ { "rank": 1, "overall_score": 71, … }, … ] },
    { "group": "31 - 33 Inch Widths", "size": 12, "total": 12, "truncated": false,
      "products": [ { "rank": 1, "overall_score": 75, … }, … ] },
    { "group": "34 Inch and Wider Widths", "size": 152, "total": 152, "truncated": true,
      "products": [ { "rank": 1, "overall_score": 79, … }, … ] }
  ],
  "sort": { "key": "overallScore", "order": "desc", "scope": "within_group" }
}
```

**Filtered to one group (or a single-group category) → flat**, since everything returned is then
score-comparable:

```jsonc
{ "products": [ { "rank": 1, "overall_score": 75, … }, … ], "size": 12, "total": 12,
  "sort": { "key": "overallScore", "order": "desc", "scope": "within_group" } }
```

Why nested is the default: an agent handed a flat list will compare the numbers in it, whatever a
`group` field says. Nesting makes the invalid comparison structurally awkward instead of merely
discouraged — the same reasoning that makes scores `null` rather than absent. It also answers the
common question better: "top 5 in each size class" is more useful than 25 rows dominated by
whichever group happens to be largest (152 of 172 here).

`limit` applies **per group** in nested mode and to the whole list when flat. `offset` likewise.
Each group carries `size` (products in CR's group, unfiltered), `total` (products matching this
call) and `truncated`. Two counts because `rank` is a position in CR's table, not in the filtered
result (§7 contracts) — with only one count, `rank: 5` of `total: 2` reads as a bug.

**`group_mode`** is `"nested"` (default) or `"flat"`. Flat returns one list with `group` and
`rank` still on every product — useful for `sort="price"`, for a brand filter that spans groups,
and for any caller that wants a single array. What flat does *not* buy is a cross-group score
ordering by default — flat sorts on CR's page order `(group, rank)` unless a caller explicitly
asks for `sort="overallScore"`, which is honoured and labelled `sort.scope: "cross_group"` (§5).

**The old `ranking` / `ranking_caveat` pair is gone**, along with the `rank_scope` field that
briefly replaced it — the envelope reports `sort` instead, since that is the thing that actually
varies. Reasoning in *Response envelope* below.

### Product shape

`summary` is the base; `standard` adds the scores; `full` adds the rest. `group`, `rank`,
`dont_buy` and `smart_buy` are present at **every** level (§7 contracts).

```jsonc
{
  "id": 401234, "brand": "Brand A", "model": "MODEL-001",
  "group": "31 - 33 Inch Widths",      // _groupName — the unit scores are comparable within
  "rank": 3,                            // position in CR's unfiltered group table; null if unranked
  "price": 2799,
  "overall_score": 71,                  // null anonymously; never absent
  "recommended": false,
  "dont_buy": false,
  "smart_buy": false,
  // standard/full add:
  "ratings": [ { "id": 11197, "name": "Thermostat performance", "value": 4 } ],
  "owner_satisfaction": 4,              // public — see below
  "predicted_reliability": 3,           // public — see below
  // full adds:
  "retailers": 7, "retailer_prices": [2599, 2999, 2599.99],
  "attributes": [ … ]                   // every attribute, normalized per §7
}
```

**`owner_satisfaction` and `predicted_reliability` sit in `standard`, not `full`.** They are two
small integers, they are public (§5), and they are the *only* real CR scores an anonymous caller
gets per product — putting them behind `full` while `standard` carries an all-null `ratings`
array optimises the default shape for members and leaves the anonymous default carrying nothing
but nulls. An earlier draft had them in `full`.

**There is no `availability` field.** `modelAvailabilityName` lives in the `initStore` payload,
and §7 keeps that envelope isolated to `cr_reliability` — surfacing it here would make
`cr_product` a second consumer and let one call trigger a 2 MB fetch to populate one string. An
earlier draft listed it under `full`, contradicting the isolation rule two sections later.

`rank` is within-group in both tiers and exact in both (§5). It is **not** a category-wide
position, and there is deliberately no category-wide rank field — that number would imply a
cross-group comparison the data does not support.

### Response sizing — measured, not guessed

Measured on the 172-product French-Door category:

**Re-measured 2026-09-01 against a populated member payload.** The original numbers were taken
on anonymous data where every score is `null`, and were wrong by 45–75%.

> **These figures pre-date `group`, `rank`, `dont_buy` and `smart_buy` being added to every
> shape, and `owner_satisfaction`/`predicted_reliability` moving into `standard`.** Expect
> roughly +15–20% on `summary` and proportionally less on `standard`/`full`. Re-measure once the
> parser exists rather than guessing again.

| Shape | `limit=10` | `limit=25` (flat default) | all 172 |
|---|---|---|---|
| `detail="summary"` | 408 | 833 | 5,138 |
| **`detail="standard"`** | 881 | **2,014** | 13,260 |
| `detail="full"` | 1,614 | 3,847 | 25,897 |

| Single shape | Tokens |
|---|---|
| Raw category payload | **357,057** — never returned |
| `cr_product`, lossless **with** attribute descriptions | 2,507 |
| `cr_product`, descriptions omitted | **1,126** |

**Measured by Spike C** (`RECON.md` §10h), on the anonymous payload with deterministic
score-fill, counted with `tiktoken`:

| detail | flat 25 | nested 5 | nested 10 | nested 25 |
|---|---|---|---|---|
| `summary` | 1,487 | 925 | 1,672 | 2,674 |
| **`standard`** | **3,012** | 1,840 | **3,380** | 5,419 |
| `full` | 22,136 | 13,371 | 24,837 | 39,878 |

Three decisions come out of it:

- **`limit` defaults to 25 flat and 10 per group nested** — confirmed, not estimated. Nested
  `standard`/10 measured ~3,380 tokens here, close to the flat default rather than a multiple of
  it, and a 4-group category measured 3,559, so group count matters less than feared. (On the
  built code it is 3,950 against a 4,000 budget — the conclusion holds, the headroom is thinner
  than this table suggests. Every figure in this section is Spike C's; re-run
  `scripts/measure_sizes.py` before planning against any of them.)
- **The old "~2,014 tokens" figure was 50% low.** Flat `standard`/25 is **3,012**. The earlier
  note predicted +15–20% for the added fields; moving `owner_satisfaction` and
  `predicted_reliability` into `standard` cost more than that. The table above supersedes it.
- **`full` is capped at `limit=10`**, flat or nested. At 25 it is 22k flat and 39.9k nested —
  a shape that eats a context window for one call. A caller wanting every attribute on more than
  ten products is drilling down and should page or use `attributes`. **Re-measured on the built
  code (`scripts/measure_sizes.py`, 2026-09-03): nested-10 `full` is 31k tokens even with lean
  attribute records, so the nested DEFAULT for `full` is 5 per group; the cap stays 10.**

**Attribute descriptions are category constants and do not belong on product records.** They
more than double `cr_product` (2,507 → 1,126 without), and the same text repeats for every
product in the category. They ship once via `cr_filters`, which already returns the `features`
definitions carrying `description` and `unitName`. `cr_product` takes
`include_descriptions=false` by default.

`detail` is an enum: `summary` (score, price, recommended flag), `standard` (plus the top 3
rated attributes **in `categoryAttributes` `sortOrder` order**, plus the two survey scores — see
§7 contracts), `full` (every rated attribute). `limit` defaults to **25 flat, 10 per group
nested**, max 200 (10 under `full`). The raw payload is never a return value — that is what `cr_product` is for.

`cr_filters` earns its place because the taxonomy is per-category and not guessable — the
refrigerator category filters on width bands and counter-depth, a microwave category will
not. Without it the agent invents filter names.

**But as CR ships it, `cr_filters` is 36,007 tokens — and 94% of that is one filter.** Spike C
(`RECON.md` §10h) found the `features` options embed every distinct value in the category: "Total
usable capacity" alone is 9,431 tokens. Capping `price`'s 254 labels — the fix an earlier draft
assumed — saves 1,494 and misses the cause entirely.

**So numeric `data[]` collapses to a range.** For any `numeric-*` attribute the tool returns
`range: {min, max}` instead of the value list; non-numeric attributes keep their distinct values,
capped at 12 with `values_truncated: true`. Descriptions and units stay — this tool is their only
home (§7).

| Variant | Tokens |
|---|---|
| As CR ships it | 36,007 |
| **Range-collapsed, descriptions kept** | **3,502** |
| Range-collapsed, descriptions dropped | 2,891 |

A 10× reduction, and nothing an agent needs is lost: for a numeric filter the bounds are exactly
what you need to construct a query, and the individual values were never actionable.

### Family navigation and category score ranges — both free from the category page

An earlier draft deferred score ranges to v1.1, on the belief that they lived in the 2 MB
reliability payload and would therefore be `null` for any category whose reliability page was not
cached — an ambiguous null in the one tool that should be trivial. **That premise was wrong.**
`modelMinOverallDisplayScore`, `modelMaxOverallDisplayScore` and `ratedModelsCount` ship on the
**category page**, anonymously, for the category *and all of its siblings* — 38 occurrences on
`c37162`, values identical to the reliability payload's (`RECON.md` §9h). No reliability fetch is
involved, and one ordinary category fetch populates its entire family.

So both ship in v1, and they share a mechanism:

**Ingest enriches `category_index` for the whole family.** Every category fetch already parses the
region that carries the nine sibling blocks (§8). From it, ingest writes per sibling: the
supercategory (`args.scid` / `scat`), the score range, `ratedModelsCount`, and the display groups
from `subcats[]`. Fetching one refrigerator category therefore fills in seven more rows for free,
and the index self-enriches through ordinary use.

**`cr_categories(franchise?, family?, refresh?)`** returns:

```jsonc
{ "id": "c37162", "slug": "french-door-refrigerator", "name": "French-Door Refrigerators",
  "franchise": "appliances",
  "family": { "id": 28978, "name": "Refrigerators" },
  "score_range": { "min": 43, "max": 79 },
  "score_range_status": "known",        // known | none_published | not_fetched
  "rated_count": 172,
  "groups": [ { "id": 200367, "name": "30 Inch and Narrower Widths" }, … ] }
```

**`score_range_status` is why this is not an ambiguous null**, and it is the same three-state
pattern as `scores_available`: `known` (CR publishes a range), `none_published` (the family has
been fetched and CR publishes none — real, e.g. Compact Refrigerators, `RECON.md` §9h), and
`not_fetched` (nothing in this family has been fetched yet, so we do not know). Three causes,
three values, never one null meaning all three.

**Under `not_fetched`, `family` and `groups` are `null` — not `{}` and not `[]`.** An empty
`groups` array is a *positive claim* that CR ships no display groups for this category, which is
exactly what a single-group category looks like (`RECON.md` §9h: single-group categories ship an
empty `categories` filter). `[]` for "we have not looked" would therefore assert the one thing
`score_range_status` exists to avoid asserting. `[]` appears only after a fetch.

**`cr_categories` needs a scope, because the enriched form is not small.** Measured over the 236
A-Z rows: **19,569 tokens** enriched with family, groups and score ranges, against 6,590 for
id/slug/name/franchise alone (`RECON.md` §10h). **Scale both by ~1.47x for the true 346** — about
28,700 and 9,700 — so even the lean shape needs paging or a default scope. So the enriched fields ship only when the call is scoped — `franchise` or
`family` — and an unscoped call returns the lean shape with `score_range_status` still present on
every row, so an agent can see what it would gain by narrowing.

**`family` answers the product-type question.** `cr_categories(family=28978)` returns the
refrigerator types — Top-Freezer, Bottom-Freezer, French-Door, Side-by-Side, Built-In, Mini
Fridges, Outdoor — with their score ranges, so an agent researching refrigerators can see the
shape of the whole family and pick a type, without ever being offered a microwave. This is the
capability CR's `type` filter looked like it provided and does not.

Note that score range and survey data are independent: Mini Fridges and Outdoor Refrigerators
carry ranges with `HasReliabilityData: false` (`RECON.md` §9h), so neither implies the other.

**A `family` that is not a supercategory is an error, not an empty list.** A family is learned by
ingesting any category in it, so the set of known families grows with what has been fetched — and
the likeliest mistake is passing a *member* category id (`c37162`) where its parent (`28978`) is
wanted. Returning `[]` would answer that with "this family holds nothing", which is the shape of
this project's core failure mode. Instead `cr_categories` returns `invalid_filter_value` with
`filter: "family"` and the legal ids and names in the message and in `candidates`. On a cache with
nothing fetched yet the list is empty, so the message says so and points at fetching one category
first, rather than implying CR has no families.

### `cr_reliability` — the sixth tool, and the anonymous tier's best feature

Sourced from the category's `reliabilityURL`, which carries `window.initStore` rather than
`filterInstanceDATA` (`RECON.md` §9). It answers a question the other five cannot: **"which
brands are reliable?"** rather than "which model should I buy?"

Why it earns a slot despite the tool surface having been signed off at five:

- **It is entirely anonymous.** All 17 brands, both survey types, three scales each, with no
  session. Given anonymous is now the default mode (§6), this is the single most valuable thing
  a non-member can get from CR — brand reliability is arguably CR's most-cited output.
- **It is not derivable from the main payload.** There, a product carries its *brand's* score;
  here the brand ranking exists in its own right, with methodology text.
- **One fetch serves nine sibling categories**, so the cost amortises across a product family.
- **`HasReliabilityData` tells us when not to fetch**, so categories without survey data cost
  nothing.

The cost is honest and bounded: **a second parser and a second schema-drift surface.** That is
a real tax on the "one parser, one code path" property (§5), so it is contained deliberately —
`cr_reliability` is the *only* consumer of the `initStore` envelope, it never touches the
category path, and a drift there degrades one tool rather than all six.

`data.models[]` on that page also carries five fields absent from `filterInstanceDATA`
(`RECON.md` §9), including `modelAvailabilityName`, which marks discontinued models. **None of
them are fetched for**, but `modelAvailabilityName` **is merged when a reliability row is already
cached**.

An earlier draft refused the merge outright, on the grounds that it would make `cr_product` a
second consumer of the `initStore` envelope and could trigger a 2 MB fetch to populate one field.
The second half is the real objection and it is conditional: any category where `cr_reliability`
has been called already has that payload on disk, and reading a field out of it then costs
nothing. A blanket rule justified by a cost that usually does not apply is the same mistake the
cars gate made.

So the rule is scoped to the cost: **merge when the row is cached, never fetch to populate it.**
When it is absent, `availability` is `null` with `warnings: ["availability_not_cached"]` — not
omitted, because "CR did not say" and "we did not look" are different facts (§7). The isolation
rule survives intact in the form that mattered: **no `cr_product` call ever triggers a reliability
fetch.**

Discontinued-model detection is worth this. A shortlist that recommends a model CR has marked
unavailable is a bad answer from a shopping tool, and the flag is free whenever the sibling
reliability payload has been pulled for any reason.

#### Contract

```jsonc
{
  "category": "c37162",
  "brands": [                                    // ordered by sortOrder as CR ships it
    { "brand_id": 158113, "brand_name": "…",
      "predicted_reliability": 4,                // 1–5, null if CR has none for this brand
      "owner_satisfaction": 5 }                  // 1–5, null if CR has none
  ],
  "methodology": { "reliability": "…", "owner_satisfaction": "…" },  // only if include_methodology
  "has_reliability_data": true,
  "has_owner_satisfaction_data": true
}
```

- **Scales.** Only the 1–5 `surveyScore` ships by default. CR also carries `survey10PtScore` and
  `survey100PtScore`; three scales for one fact is noise, so the /100 value is available via
  `detail="full"` and the /10 is dropped.
- **Null, never absent** — the same rule as everywhere else. `RECON.md` §9 shows brands with a
  reliability score but no owner-satisfaction score, so per-brand gaps are real and must not be
  silently omitted rows.
- **`include_methodology=false` by default.** CR's `blurb`/`footnote`/`infoText` are multi-sentence
  prose repeated per survey type; useful once, wasteful per call.
- **No-data is structural, not an empty list.** When **neither** survey has data the tool returns
  `has_reliability_data: false` with `brands: []` — an empty array alone would read as "no brands
  are reliable", which is this project's core failure mode in miniature. When only one survey is
  missing the brands still ship, ranked by the survey that has data, with the absent side `null`
  per brand: dropping them would lose the half CR did publish.
- **CR names the categories it surveys, and we must read it.** `args.cats[]`'s entry for the id
  carries `reliabilityURL` as a URL string where a survey exists and the **boolean `false`**
  where none does — bimodal over every category measured, 27 string / 25 `false` (`RECON.md`
  §9a). So `cache.reliability_url_status` answers `known` / `none_published` / `not_fetched`,
  and `none_published` takes the no-data envelope above **without a request**, with `cr_url:
  null` (there is no page to name) and `fetched_at` from the category payload that said so.
  Guessing is reserved for `not_fetched`, the only state where nothing is known.

  This is a fix, not a refinement. Both states collapsed into one `None`, so `cr_reliability`
  guessed `…/reliability/c33041/` for upright freezers, spent a request, took CR's correct 404,
  and returned `fetch_failed`/`url_unresolved` advising the caller to *"run `cr_ratings` on the
  category so the real URL is cached"* — which they had done, and which cached the very payload
  that says there is no URL. The advice could never work, and the error contradicted this
  section's own rule that a category CR runs no survey on is not a failure.

#### Auth semantics — this tool is anonymous-only

**`data-subscriber` does not appear on reliability pages at all** — zero occurrences of either
value (measured). So the generic rule "no marker ⇒ `marker_missing`" would fire a false
schema-drift alarm on *every* call. `cr_reliability` therefore:

- reports **`auth_state: "anonymous"`** always, regardless of session, because the data is
  identical either way, and **omits `session` and `data_tier` entirely** — the derivation in §7
  would otherwise yield `session_expired` on a tool whose data no session has ever affected;
- sets `scores_available: {predicted_reliability: …, owner_satisfaction: …}` derived per §10,
  and omits the product-score keys entirely — they are not fields this tool can return.
  **`unavailable` is never emitted here**: nothing on this page is gated, so all-null means
  `absent`, which `HasReliabilityData` independently confirms. The generic anonymous rule would
  otherwise tell a caller to sign in to see data no membership has ever unlocked;
- omits `sort` — brands are returned in CR's own `sortOrder`, there is no caller-chosen ordering
  and there are no display groups here, so the field would have nothing to report;
- uses **`reliability_payload_missing`** as its drift alarm rather than `payload_missing`, so a
  drift in `initStore` is distinguishable from a drift in `filterInstanceDATA`.

#### Fetching and caching

The `reliabilityURL` is read from `args.cats` in a cached category payload when one exists, and
otherwise **constructed** from the known pattern — `{category_path}/reliability/c{id}/` — so
`cr_reliability` never forces an 11 MB category fetch just to learn a URL.

**The constructed form needs the canonical path, which is not always the A-Z index path.** Some
index URLs redirect to a shorter canonical path with the same `cNNNN` (`RECON.md` §9f), so a URL
built from the index entry can 404 or redirect. Redirects are followed (§5), and a 404 on a
*constructed* URL is never reported as `reliability_payload_missing` — a guessed URL failing is
not schema drift.

**It falls back to a cached category payload, and stops there.** If `args.cats` is available from
a category row already in the cache, the real `reliabilityURL` is read from it. If no such row
exists, the tool returns `fetch_failed` with `reason: "url_unresolved"` — it does **not** trigger
an 11 MB category fetch to resolve a URL, which is the cost this whole construction exists to
avoid. The caller can run `cr_ratings` on the category first, and the next call resolves.

Cached in its own table, since it is a different envelope:

```sql
reliability_raw(category_id, fetched_at, payload_json)   -- append-only, anonymous only
```

No `auth_tier` column: the data does not vary by session, so tiering it would create two
identical rows. **One fetch is fanned out to every sibling the payload names** — `data.categories[]`
carries each sibling's own brand arrays, so a row is written per sibling id and a later call for
any of them hits cache instead of refetching 2 MB.

**Take the sibling list from the payload being parsed, never from a count.** The reliability
payload lists **9** categories on `c37162` where the category page's `args.cats` lists **7** — it
adds Compact Refrigerators and Refrigerator drawers (`RECON.md` §9h). An earlier draft said "all
nine" as though nine were a property of the family; it is a property of that one payload.

### `cr_search` searches categories first, then cached products

The page-as-API model has **no cross-category product search surface**, and fetching all 346
categories to search them is the bulk mirroring §3 rules out.

**CR's typeahead confirms that, and earns a place anyway.** An earlier draft dismissed it as
"unverified and belonging to the API family this project does not build against" — a principle
that died when `cars-api` came into scope, so it was tested (`RECON.md` §14).
`/api/search/typeahead?query=` works anonymously, answers in 0.5–2 KB, and returns CR's own
category matches with their `/cNNNN/` ratings links. It returns **zero** results for a model
number, which settles the product question: there is no cross-category product search to be had.

But as a *category* resolver it beats matching our own index locally, because it is CR's matching
rather than substring comparison — `televisions` resolves to `c28700`, the category the A-Z index
omits entirely, and `mattress` returns mattresses, mattress toppers and mattress stores ranked.
So `cr_search` uses it as its **first** category source, falling back to the local index when it
is unreachable. It is an enhancement on a path that already works offline, never a dependency.

But a cache-only search returns nothing on a fresh install, which an agent reads as "CR has no
such product" — the same class of error as the auth-state confusion. So `cr_search` searches two
things, in order:

1. **Category names and slugs.** The A-Z index carries a display name for each of its 236 entries
   (`RECON.md` §8), and every category from either source carries a slug. **Both are matched, and
   the slug match is load-bearing**: the 110 sitemap-only categories have no display name at all
   (the sitemaps carry URLs, not labels), so a name-only search would fail to find *Televisions*
   or *Mattresses* — the exact "CR does not cover this" failure §5 calls the worst direction.
   Matching `televisions` against the slug in `/electronics-computers/tvs/c28700/` finds it with
   no display name in hand. One cached fetch answers "which category covers this?"
   immediately. `"dishwasher"` routes to the category on a cold cache.
2. **Products in already-cached categories**, for model names and numbers like `B36CD10ENS`.

Product matching is **case-insensitive substring** on `brandName` + `modelName`, with no fuzzy
matching — model numbers like `B36CD10ENS` make edit-distance matching actively harmful, since
one character is a different product. A query under two characters matches no product: a
single character is not a model identifier, and `"x"` substring-matched 25 rows through `XE`,
`FLEX` and `X0LW`. **Names are matched and served DECODED.** CR ships display strings inside
its JSON entity-encoded — `Nitro V 16&quot;`, `Black &amp; Decker` (157 of 3,348 cached
product names carried one) — and every name, label, unit and description passes through
`extract.clean_text` once, at ingest and again on read for rows written before it existed, so
`16"` finds the laptop and no tool answers a raw `&quot;`. Results are labelled by kind (`category` vs `product`) and
capped at 25 per kind. A product miss returns `searched_categories: [...]` naming what was
actually searched, so an empty result can never be mistaken for "CR does not rate this". Live
cross-category *product* search stays post-v1.

**Category hits are RANKED across both sources, by one lexical rule; source order is not the
ranking.** In source order — every typeahead hit, then the index — a fuzzy remote suggestion
outranked an exact local match: measured 2026-09-06, `'over-the-range microwaves'` answered
*Countertop Microwave Ovens* first and the exact category second, and `'pressure cookers'`
answered *Pressure Washers* first, with *multi-cookers* (what CR calls a pressure cooker) fourth.
An assistant taking `categories[0]` would describe a pressure washer. The rule lives in
`lexical.py` and is plain token logic, no distance metric: hyphens split like spaces, function
words (`the`, `and`, `of`…) drop out, and a query token matches a hay token when the two agree
after a plural strip (`microwaves` ≈ `microwave`, `mattresses` ≈ `mattress`), when both are
derived forms sharing a root once the gerund and agent-noun suffixes come off (`washing` ≈
`washers`, `cooking` ≈ `cookers`, never the bare word — `blends` is not `blender`; measured
2026-09-07, `washing machines` reached Front-load washers through CR's label and missed both
top-load categories, whose names say *Washers*), or the hay token
extends it by at most two characters (`tv` → `tvs`, `robot` → `robotic`) — never by substring,
which would let `the` in `over-the-range` reach *thermostats*. A hit's texts are its display
name, its slug and, for a typeahead hit, CR's own label (`"washing machines"` on Front-load
washers, `"televisions"` on TVs — CR's synonym for the category, which counts as one of its
names). The order is:

1. an **exact** text — its stemmed tokens are the query's, order aside;
2. a hit carrying the query's **head** token, its last word: `cookers` in `pressure cookers` is
   the thing asked for and `pressure` only a modifier, so *rice cookers* and *multi-cookers*
   outrank *Pressure Washers*, which shares only the modifier;
3. **more query tokens matched** — *Over-the-Range Microwave Ovens* (all three) over
   *Countertop Microwave Ovens* (one);
4. the **tightest text** — fewest unmatched words of its own, *Mattresses* over *Mattress
   Toppers*;
5. **CR's typeahead order**, then the index's alphabetical order after every typeahead hit.
   Everything the tokens leave tied is CR's call: it knows popularity and this server does not.
   `'dryer'` returns CR's five dryer categories in CR's order, every one an equal `full` match.

Typeahead hits are **reordered, never dropped**, and their `source` stays `typeahead`; the
index remains the fallback and never becomes authoritative. Each hit carries **`match`**:
`exact`, `full` (every query token matched), `partial`, or `none` — CR suggested it for a reason
the tokens do not show (*Compact Washers* for `washing machine`). It is the structural version
of the ranking: `categories[0]` with `match: "partial"` is the best of a set of weak fits, not
the answer, and an agent reading `'pressure cookers'` sees five partials and no category.

The **local index search** (`Cache.search_categories`) uses the same rule and is no longer a
whole-query substring test — that is why `'over-the-range microwaves'` found nothing locally
beside an almost-exact category, and why under 3 characters (no typeahead call) `'tv'` ranked
*TVs* third behind *Phone TV Internet Bundles* alphabetically. A row qualifies when the head
token matches or at least half the query's tokens do (`window air conditioner` reaches *Portable
Air Conditioners*, not *Air Fryers*), ranked by the rule above and capped at 25. Slugs stay in
the haystack for the reason given under 1.

A **brand name is an honest miss**: `'instant pot'` returns no category — CR's typeahead offers
only unlinked editorial rows and no token reaches *multi-cookers*. No synonym table is
maintained; `cr_categories` lists the catalogue, and an empty `categories` says "no category
name or slug resembles this", never "CR does not rate it".

### Filter taxonomy, as CR ships it

Observed on French-Door Refrigerators (`c37162`):

Seven filter ids, stable across all 11 categories tested; the options inside them are
category-specific. Full shapes in `RECON.md` §1. An earlier draft of this spec had the types
wrong and invented a filter that does not exist — two corrections matter for the client:

- **There is no `isRecommended` filter.** CR Recommended is an option *inside* the `custom`
  group, carrying `prop: "expertRatings.isRecommended"`, not a top-level toggle.
- **`price` is `input-text`, not a range.** Its `data[]` is a list of discrete price labels, so
  a client posting `{min, max}` against the shipped taxonomy is guessing. Filtering is local
  anyway, so the server applies min/max over `product.price` itself and uses `data[]` only to
  advertise the observed bounds.

`cr_filters` returns the taxonomy; the client never hardcodes it.

**Each returned filter names the parameter that consumes it.** The tool surface deliberately does
*not* mirror CR's filter shape (see Tool contracts), which leaves an agent reading one shape and
writing another — a real cost, and the honest objection to flattening. It is paid off by making
the mapping data rather than inference: every filter in `cr_filters` output carries
`parameter: "brands" | "group" | "price_min/price_max" | "features" | "recommended" | null`,
where `null` means "not exposed" (`type`, and `sort`, which is a top-level argument).

The alternative — mirroring CR's `{type, categories, brands, price, features, custom, sort}`
verbatim — was considered and rejected on the measurements: of those seven, `type` is a no-op
(`RECON.md` §9h), `sort` is not a filter, `custom` holds a single boolean, and `price` ships as
254 discrete labels rather than a range. Reproducing that faithfully would hand an agent a
taxonomy that is three-sevenths degenerate, and cost a nested `anyOf` per filter to express. The
flat parameters say what an agent wants; `cr_filters` says what CR has; the `parameter` field is
the bridge.

### Attribute normalization — structure only, nothing dropped

CR ships **at least seven** dataTypes. `attrs` on `c37162` shows six — `numeric-rating-score`
(the paywalled 1–5 scores), `numeric-general`, `numeric-price`, `boolean`, `text`, `custom` — and
the full `categoryAttributes` dictionary adds **`numeric-overall-score`** (`RECON.md` §1, §9h).
The count is not the point: the set is open, which is why the rule below is "carry any declared
type through untouched" rather than a closed table. The definition block also carries `unitName`,
a plain-English `description`, and `data[]` (the distinct values present in this category).

**`custom` is treated exactly like `text`: `kind: "custom"`, never coerced.** An earlier draft
counted five types and gave `custom` no rule at all, which under "coerce by declared dataType"
leaves an implementer with a declared type the coercion table does not mention — and the one
forbidden fallback is guessing from value shape. Unknown future types get the same treatment:
carried through with their declared name, value untouched.

Each attribute normalizes to
`{id, name, kind, value, raw_value, unit, description}`. Four traps, all real:

- **`unitName` exists — never infer a unit.** "Exterior width: 36" ships with
  `unitName: "in."` in the definition block. Join value to definition by `attributeId`.
- **`boolean` values are the strings `"Yes"`/`"No"`, and `text` attributes also contain
  `"Yes"`/`"No"`.** Coerce by declared `dataType`, never by inspecting the value.
- **`text` holds numeric-looking values** — `"39"`, `"0"`, `"-1"`, `"-2"` (dial positions
  and suggested settings). Coercing text to number turns a dial position into arithmetic.
  Never coerce `text`.
- **`attributeId` is the stable key, `name` is not.** Names vary by category and change
  over time. Keep the ID or lose the join.
- **`attributeTypeName` is a homonym — the same key means different things on the two objects.**
  On a `categoryAttributes` *definition* it is `PRICING` / `SPEC` / `TEST_RESULT`, a kind of
  attribute; the declared dataType there lives in **`attributeDataTypeName`**. On a product
  `attrs[]` *entry* it is the dataType itself (`numeric-rating-score`, `text`, …). Join the two
  and read the wrong one and every attribute comes back typed `"SPEC"`, which is in no coercion
  table (`RECON.md` §9h). Read `attributeDataTypeName` from definitions, `attributeTypeName` from
  entries — never the same field name from both.

Keeping `raw_value`, `id` and `description` makes the normalization lossless, so the only
downside of normalizing — losing fidelity — does not apply.

**The category page carries a complete attribute dictionary — use it, not `attrs`.**

`filterInstanceDATA.attrs` is *not* the full definition set. The same page also embeds nine
`categoryAttributes` blocks (the category plus its siblings), and across **all 8 categories
tested they cover 100% of the attributes products actually carry**, where `attrs` misses some in
6 of the 8 (`RECON.md` §1). On `c37162`: 37/37 versus 23/37.

Each entry carries `unitName`, `description`, `displayName`, `attributeGroup` (`"Specs"` /
`"Features"`), `sortOrder`, `attributeDataTypeName` and `isFilterSuppressed` — strictly more than
`attrs` provides, from bytes already fetched.

So the join is: **`categoryAttributes.attributeDataTypeName` first,
`filterInstanceDATA.attrs` second, the entry's own `attributeTypeName` last** — note the *field
names differ by source*, which is the homonym trap above: the definition's `attributeTypeName` is
`SPEC`/`PRICING`/`TEST_RESULT`, not a dataType. No second request, no second envelope, no
opportunistic caching. An
earlier draft of this spec routed this through the 2 MB reliability page; that was unnecessary —
the dictionary was on the page all along, ~2.5 MB further into the same HTML.

**`attributeGroup` is worth surfacing.** It is CR's own division of an attribute list into specs
versus features, so `cr_product` and `cr_filters` can group output the way CR does rather than
inventing a taxonomy.

**The fallback still ships, and stays rare.** Zero attributes went unmatched across the 8
categories, but a category that adds an attribute mid-release would otherwise produce an entry
with no declared type — and the coercion rule must never fall back to guessing from value shape.

**When an entry matches no definition at all, the coercion rule must still work.** Against the
full `categoryAttributes` dictionary this does not currently happen — zero unmatched across the 8
categories tested. The 14-of-37 gap on `c37162` is what `attrs` *alone* leaves (23 of 37 join,
`RECON.md` §1), and it is the shape of the problem a mid-release attribute would reproduce. For
any such entry:

- **`kind` comes from the entry's own `attributeTypeName`**, which carries the same vocabulary
  as the definition block's `dataType` (`boolean`, `text`, `numeric-general`, …). This is what
  makes "coerce by declared `dataType`, never by value shape" implementable at all for these —
  without it, an unjoined entry would have no declared type and the rule would quietly fall back
  to exactly the value-shape guessing it forbids.
- **`unit` and `description` are `null`.** They exist only in the definition block. `null` here
  means "CR ships no unit for this attribute", which is also true of the 29 of 37 entries that
  *do* join but have no `unitName` — so callers need no special case.
- All 14 observed unjoined entries are `boolean` or `text` feature flags, where a missing unit
  is expected rather than lossy.

Never synthesize a definition for an unjoined entry, and never drop it — it is real data.

### No-session behaviour

Anonymous is a first-class mode, not a failure. A request with no session returns the
catalogue, prices, specs, retailer price spread and filters, with every score `null`.

The envelope always carries `auth_state` and `scores_available`, and individual scores are
always `null` rather than absent. This is structural, not a note in prose: an agent must
never be able to read "no member session" as "CR did not rate this model". Where a request
cannot be usefully served anonymously — a question that is only about scores — the response
says so at the top level rather than returning an empty-looking result.

Every response carries `fetched_at` and `cr_url` so a claim can be traced to its source
and its age.

### `auth_state` — the detection algorithm

**Two orthogonal facts, two fields.** An earlier draft carried only `auth_state`, which
conflated *the tier of the data being served* with *the health of the credential right now*.
In a cache-first design those come apart constantly: a cached member row served after the
session has lapsed is genuinely member data and a genuinely dead session, and one enum cannot
say both. The observable symptom was an agent seeing `member`, `session_expired`, `member`
across three consecutive calls with one unchanged cookie, and having no way to tell which
answer was about the data and which was about the login.

| Field | Values | What it describes |
|---|---|---|
| **`data_tier`** | `anonymous` \| `member` | The tier the **served row** was fetched at, recorded from `data-subscriber` at fetch time. A property of the data. |
| **`session`** | `none` \| `active` \| `expired` \| `unverified` | The credential's health, process-level, from the most recent network fetch. A property of the configuration. |

**Only pages that carry an auth marker update `session`** — product category pages
(`data-subscriber`) and car pages (`window.isSubscriber`). The A-Z index, the sitemaps, the
reliability pages and every `cars-api` endpoint carry no marker at all (`RECON.md` §9d, §11a), so
they can neither confirm nor refute a session and must leave the value untouched: a reliability
fetch must never downgrade a healthy `active` to `expired`.

**The two markers are read on their own surfaces and are never mixed.** A car page has no
`data-subscriber` and a product page has no `window.isSubscriber`; treating a missing marker as a
negative would report every fetch of the other surface as logged-out. `marker_missing` fires only
when the marker expected *for that surface* is absent.

**The `/ec/login` rejection check applies to every `www.consumerreports.org` fetch, but to no
`cars-api` fetch.** A rejected `hash` is rejected by the *host*, so in a cookie-configured process
it redirects the A-Z index, the sitemaps, a reliability page and a car page just as surely as a
category page — the check therefore lives in the transport layer and runs on every `www.` request,
not only on the two surfaces that carry markers. `cars-api` responses never redirect there — on
that host a `403` means "no such route" (§5) — so the check must not be applied to API calls, and
a cars `403` must never be read as a dead credential. Note the asymmetry with the marker step,
which stays surface-specific: rejection is detectable everywhere, confirmation only where a marker
is rendered.

`session` values: `none` = no cookie configured. `active` = a fetch this process saw
`data-subscriber="true"`. `expired` = a fetch this process saw `data-subscriber="false"` while a
cookie was configured. `unverified` = a cookie is configured but no network fetch has happened
yet this process, so nothing is known — the honest cold-start answer in a cache-first server,
and the reason `session` is not a boolean.

**`session` moves only through `SessionState` events; no caller chooses a state.** The four
writers report what they observed and `credentials.py` alone decides: the transport reports
`on_rejected()` when a `www.` response's final URL is the login route; the repository (a
category page's `data-subscriber`) and the cars client (the car page's `window.isSubscriber`)
report `on_marker(is_member, credential_present=)`, where `credential_present` is the
transport's assertion that `hash` was in the jar AFTER the response; the sign-in flow reports
`on_probe_verdict(verdict)` with `validate_cookies`' verdict; `adopt()` reports
`reconfigure(configured)`. So §6 rule 1 — never `expired` without that jar assertion — is
encoded once, in `on_marker`, which also moves nothing to `active` without it (a logged-in
page the configured credential did not produce is not that credential's health), nothing on a
missing marker (`marker_missing` is drift, not a state), and nothing while unconfigured. An
earlier shape had `mark_active()`/`mark_expired()` called from four places in three layers,
and the repository re-latching a rejection the transport had already latched; the rule lived
in three copies and nothing structural kept them equal. The rejection latch itself
(`credentials.rejected`: no re-seed, no rotation write-back) is the jar's business and stays
on the store, written by the transport only.

**`auth_state` remains, derived from the pair**, because it is the field an agent reads first
and the three-value enum is legible:

```
"member"           if data_tier == "member"
"session_expired"  if data_tier == "anonymous" and session == "expired"
"anonymous"        otherwise
```

The dangerous case is the second, and it is the reason "is a cookie configured" is not a
sufficient test:

| Value | Meaning | Scores |
|---|---|---|
| `anonymous` | The SERVED ROW was fetched without a confirmed member session — no cookie, an unverified one, or a cached anonymous row served under a live session after a refetch failed. A statement about the row, never about the credential; `session` carries that. Expected, not an error. | gated fields `null` |
| `member` | The served row was fetched with a confirmed logged-in session. | populated |
| `session_expired` | Cookie **was** configured but the page came back logged-out. | gated fields `null` |

**There is no `unknown`.** An earlier draft used it for cache hits, which would have made
`unknown` the steady-state answer in a cache-first design — the enum would report nothing
useful precisely when it is asked most often. Every cached row records the tier that was
positively detected via `data-subscriber` at fetch time, so a cache hit reports **that recorded
tier** plus `from_cache: true`. The pair is strictly more informative than `unknown`.

**With no served row there is no `auth_state` to derive, and the envelope says so: `null`.** An
error envelope that served nothing — an unknown category, an unknown product in no cached
category, a parameter rejected before the fetch, a failed fetch with nothing cached — has no
`data_tier` (its `provenance` is null for the same reason), so the derivation above has no
input. An earlier draft filled the value in from the session alone: `session_expired` when the
credential was known dead, `anonymous` otherwise. That is a second derivation, re-encoded on
one surface, and it answered `auth_state: "anonymous"` beside `session: "active"` on every
no-row error — the tier of a row that does not exist, next to a verified-live credential.
`null` is the convention `scores_available`, `provenance` and `sort` already follow on that
envelope: a field that describes the served row is null when nothing was served, and
`session` alone carries the credential's health (`session_expired` is not lost — it is
`session: "expired"` on the same envelope). An error envelope that *did* serve a row —
`unknown_product` against a cached category, a filter rejected after the fetch — reports that
row's tier **and its provenance**, as a success does. The two travel together or not at all:
a row real enough to name a tier is real enough to say where it came from, how old it is and
whether it was cached, so `auth_state: "member"` beside `provenance: null` — member data from
nowhere, of no age — is not a state the envelope can be in. It shipped once: `cr_ratings`' error
path took the served row's tier and hard-coded `provenance: null`, so every `sort`/`order`/
`group` error after the fetch made that claim (measured 2026-09-06). Now `RowEnvelope`, the base
of the three products envelopes, refuses the split at construction in both directions, and the
error path is built from the served row itself — a caller who mistyped `sort` still learns the
category was served from the cache, when, and from which URL. (The row's own warnings ride
along too — `attribute_dictionary_missing` is exactly what an `unknown_filter` on `features`
is about.) `cr_reliability`'s `reliability_payload_missing` over a cached row does the same with
`ReliabilityProvenance`: its `cr_url` is the URL that landed elsewhere, which is the diagnostic.
The key is still required and the enum still in the `outputSchema` (`anyOf` with `null`);
`cr_reliability` still pins the literal `"anonymous"`, and cars still omit the key (below) —
three surfaces, one rule: **`auth_state` is never invented from the session**. `null` means "no
row to describe", omitted means "not a concept on this surface". `tests/test_boundaries.py`
walks every `E.*Envelope(auth_state=)` construction and accepts only `E.auth_state(...)`,
`None`, or reliability's literal — and checks the `provenance=` beside it is null exactly when
`auth_state=` is.

**`session_expired` is an `auth_state`, not an error.** It also appears in the error taxonomy
below, and the two must not both fire. The rule: a fetch that *succeeds* but comes back logged
out is `auth_state: "session_expired"` with **`error: null`** — the response carries a complete,
valid anonymous catalogue, so calling it an error would discard real data and misreport a
degraded-but-working state as a failure. `error: "session_expired"` is reserved for a request
that could not be served at all, which in practice means only `consumer-reports-mcp auth`
validation.

**Serving a retained scored row** (§8 rule 2) reports the tier recorded on the row being served
in `data_tier`, with `fetched_at` set to **when that row was fetched**, not now. Otherwise a
retained-but-scored response would date itself to the present and defeat the provenance the
append-only cache exists to provide. The retention is reported by `superseded_at`, not by
`stale` — see the envelope below, where those two facts are separated.

CR does not signal a rejected cookie — an expired session returns the anonymous page with a
normal `200` (§5). So detection must key on a **positive logged-in marker in the page**, never
on "cookie is set" and never on "scores are non-null":

- Inferring from "cookie is set" conflates a valid session with an expired one.
- Inferring from "all scores are null" conflates an expired session with a category CR has
  genuinely not rated — the exact confusion this whole section exists to prevent.

**A rejected credential never reaches the marker at all — check the URL first.** Spike A
(`RECON.md` §10b) measured what CR actually does with a bad `hash`, and it is not what this spec
assumed. The request is redirected — `…/c35183/` → `/ec/login?loginMethod=auto` →
`/ec/login?error` — and returns a **51 KB login page with a `200`, no `filterInstanceDATA`, and
zero occurrences of `data-subscriber` in either value**. CR validates the credential, rejects it,
and clears the cookie.

The spec previously assumed a rejected cookie yields the ordinary anonymous page. Under the rules
as written, an ordinary expired credential would therefore have produced `payload_missing` — the
loud schema-drift alarm — and been negatively cached for an hour. So detection runs in two steps,
and the first one happens **before any parsing**:

**Read `resp.url`, never `resp.history`.** A *successful* re-mint also redirects through
`secure.consumerreports.org/ec/login` — Spike A2 measured two hops on a perfectly healthy member
fetch (`RECON.md` §10i). The login host in the chain is normal; the login page as the
*destination* is the rejection. Checking the history would report every member fetch as a dead
credential.

**And match it as a route, not a substring.** `"/ec/login" in resp.url` also matched
`www.consumerreports.org/anything/ec/login-tips/`, and one such page latched `rejected`, marked
the session expired and evicted the cookie for the rest of the process — a member session
silently downgraded to anonymous. The check is `is_login_url`: the login host
(`secure.consumerreports.org`) **and** the `/ec/login` path segment, parsed with `urlsplit`. A
login-shaped page anywhere else is drift (`payload_missing`), which is at least visible.

```
# step 1 — the FINAL url only, no body inspection, parsed as a route
"credential_rejected"  if is_login_url(resp.url)   # host == secure.… and path == /ec/login

# step 2 — only for a page that is actually a category page
"member"               if b'data-subscriber="true"'  in body
"session_expired"      if b'data-subscriber="false"' in body and a cookie was configured
"anonymous"            if b'data-subscriber="false"' in body and no cookie was configured
```

`credential_rejected` moves `session` to `"expired"` — it is the same fact arriving by a
different route, and it is **never** `payload_missing` or `marker_missing`. The response carries
no catalogue, so unlike a marker-detected expiry it cannot be served as anonymous data; it falls
back to the cache like any other failed fetch. `auth_state` then follows the served row, as
always: a cached anonymous row answers with `auth_state: "session_expired"` (anonymous data, dead
session — the derivation above), and with nothing cached the envelope is `error.code:
"credential_rejected"` beside `auth_state: null` and `session: "expired"`, because there is no
row to describe.

**Only a *malformed* credential is measured.** A genuinely lapsed one may still return the
anonymous page (`RECON.md` §5 removed cookies rather than corrupting them, and got the anonymous
page). Both paths must therefore be handled: the URL check catches rejection, the marker check
catches a silently downgraded session.

**A stale credential must not break anonymous browsing.** Because a bad `hash` redirects every
request to the login page, a stale `session.json` would otherwise take out *all* fetches rather
than degrading to the anonymous tier. On `credential_rejected` the client **drops the credential
from the jar for the remainder of the process** and retries once without it, which returns the
ordinary anonymous page. The stored file is left alone — repairing it is the user's call via
`auth` — but a dead cookie costs one extra request, not the whole server.

The marker itself, `data-subscriber`, is emitted into the raw HTML by the server, so a plain
wafer GET sees it with no JavaScript (`RECON.md` §5).

**Do not use "Sign Out" as the marker.** It is the obvious candidate and it is wrong: the
string appears **twice in the anonymous page**, inside hidden account-nav markup. Keying on it
would report every anonymous fetch as an active member session — silently, and in the exact
direction that does the most damage.

If neither `data-subscriber` value is found on a **category page**, the result is
`marker_missing` (§7), never a guess. A **car page** carries no `data-subscriber` at all and is
fetched only for the api-key, so a missing `window.isSubscriber` there is recorded as `None` and
never raises — cars need no session, so there is nothing for it to gate. **If *both* values appear on one page, that is also `marker_missing`** — a page asserting
logged-in and logged-out at once is a page whose markup we no longer understand, and picking the
more convenient value is exactly the "fails toward member" mistake this section exists to prevent.
It is not currently observed; it is specified so the parser has no third branch to invent. It is deliberately distinct from `payload_missing`: the payload and the auth marker are
separate pieces of CR's markup that can drift independently, and collapsing them into one alarm
would hide which half broke.

`session_expired` must never be silently reported as `anonymous`. It is a distinct, actionable
state meaning "sign in again" — the notice names `cr_sign_in`, then `consumer-reports-mcp auth`,
then the env var (§6 *renewal*) — and it is surfaced in the envelope.

### Response envelope

Every tool returns:

```jsonc
{
  "auth_state": "member",              // derived enum, above
  "session": "active",                 // credential health — the actionable one
  "scores_available": {                // NOT booleans — see below
    "overall_score": "available",
    "attribute_ratings": "available",
    "owner_satisfaction": "available", // all derived per §10 — never hardcoded
    "predicted_reliability": "available",
    "recommended_flag": "available"
  },
  "provenance": {                      // facts about the row that answered
    "data_tier": "member",
    "fetched_at": "2026-09-01T14:02:11Z",
    "cr_url": "https://www.consumerreports.org/.../c37162/",
    "from_cache": true,
    "stale": false,                    // past TTL — and nothing else
    "superseded_at": null              // a scored row was retained over a newer unscored one
  },
  "sort": { "key": "overallScore", "order": "desc", "scope": "within_group" },
  "warnings": [],                      // always present, empty on a clean success
  "error": null,
  "data": { }                          // per-tool
}
```

**Eight top-level keys, down from thirteen.** An earlier draft carried `data_tier`, `fetched_at`,
`from_cache`, `stale`, `superseded_at` and `cr_url` flat alongside the payload. Each is defensible
alone; together they buried `data` and `error` under bookkeeping that is answering one question —
*where did this row come from and how much should I trust its age* — so they are one object,
`provenance`. `data_tier` belongs there rather than beside `session`: it is a property of the
**row that answered**, not of the current configuration, which is exactly the distinction the
two-field split exists to make.

**`rank_scope` is gone.** It only ever had one value, and a field that cannot vary is noise on
every response. `rank` is **always** a within-group position — stated once, here, rather than
re-asserted in every payload.

**`sort` replaces it, and it does vary.** It echoes what the caller actually got:
`{key, order, scope}`, where `scope` is `"within_group"` or `"cross_group"`. That is the field an
agent must read before comparing two scores, and unlike `rank_scope` it carries real information.
It ships on `cr_ratings` only — nothing else returns an ordering.

**`scores_available` is an object, and its values are a three-state enum — not booleans.** The
object part is because the paywall is partial: owner satisfaction, predicted reliability and the
Recommended flag are present anonymously (§5), so a single boolean would tell an agent "no
scores" while the response carries three kinds of real CR score.

The three states exist because a boolean *per field* still collapses the one distinction this
project exists to preserve. Survey scores are absent for **members** across health, money,
babies-kids and home-garden (`RECON.md` §9f) — so on Banks a member gets no owner satisfaction
because CR does not collect it, while on French-Door an anonymous caller gets no overall score
because it is gated. Under booleans both are `false`:

| Value | Condition | Means |
|---|---|---|
| `available` | any product in the payload has a non-null value | CR has this data and you can see it |
| `absent` | all null **and** `data_tier == "member"` | CR does not publish this for this category |
| `unavailable` | all null **and** `data_tier == "anonymous"` | Cannot be determined from this payload — gated, or CR has none. Sign in to distinguish |

`unavailable` is deliberately not called "gated": anonymously the two causes are
indistinguishable from the payload, and claiming otherwise would hardcode the paywall boundary
§10 forbids hardcoding. Saying "cannot be determined, sign in to find out" is the true statement.

**A note on `recommended_flag`.** `isRecommended` is a boolean CR ships on every product and
never nulls — Banks has 0 recommended of 69 (`RECON.md` §9f) and still carries the field. Under
"any non-null" that is `available`, which is correct: CR *does* publish the flag, and zero
products carrying it is data, not absence. Anyone reading "non-null" as "truthy" would report
`absent` there instead, so Banks is a required test case.

### Warnings vocabulary

`warnings[]` is machine-readable, so it is a closed vocabulary like `error.code` — and unlike
`error.code` it used to be enforced by nothing: a bare `list[str]` with every token spelled at
its emission site across a dozen modules, and this document's prose as the only enumeration.
The registry now lives in code (`envelope.WARNING_TOKENS` / `WARNING_PREFIXES`) and **every
envelope validates its `warnings` against it at construction** — an unregistered token is a bug
in this process and fails the tool call loudly, the way an unlisted `code` already does, rather
than reaching an agent as a word it has never been told about. Adding a warning means adding it
here, in the registry and in `CLAUDE.md`; `tests/test_envelope.py` pins the three to each other.

A token is either bare, or `prefix:<detail>` with a non-empty detail. `<detail>` is never free
text: an id, a count, a reason code or the percent-encoded parameter list of `no_results`.
The registry catches a *programming* error loudly; it must never turn a legitimate runtime
state into one. So every prefixed emission derives its detail from something that cannot be
empty — an `int`, a literal reason code, a field name — and the one renderer whose detail is
built from caller input, `envelope.no_results`, **refuses an empty parameter set** (raises,
naming the rule) rather than rendering the bare `no_results:`. Both emission sites gate on the
tool's active filters (`FilterSpec.active` on products; `cr_cars`' own filter dict), because
"zero rows with no filter" is not what the token means — and on `cr_cars` it is not a reachable
state either: an unfiltered listing is refused before the request (§5).

| Token | Meaning | Emitted by |
|---|---|---|
| `sitemap_pass_pending` | the sitemap discovery source is not yet authoritative (running, not started, or failed), so an `unknown_category` is not final | every products tool |
| `empty_category` | CR published no products in this category (`total: 0` is CR's, not a filter's) | `cr_ratings`, `cr_filters`, `cr_product` |
| `no_results:<k=v,…>` | the caller's filters excluded every product / model-year of a populated set; `<k>` are the TOOL's own parameter names, values percent-encoded. Mutually exclusive with `empty_category` | `cr_ratings`, `cr_cars` |
| `attribute_dictionary_missing` | `categoryAttributes` was absent from the page; the join degraded through its fallback chain and units/descriptions are lost | `cr_ratings`, `cr_filters`, `cr_product` |
| `ungrouped_products:<n>` | `<n>` products CR ships without a `_groupId`: `group: null`, `rank: null`, one trailing nested block | `cr_ratings`, `cr_filters`, `cr_product` |
| `coercion_failed:<attributeId>` | a value would not coerce to its declared `dataType`; `value: null`, `raw_value` kept. On cars the suffix is a FIELD name (`coercion_failed:isRecommended`) | products shapes, `cr_car`, `cr_cars(detail="standard")` |
| `refresh_failed:<code>` | a cached row answered because the refetch failed with error-taxonomy `<code>` (`fetch_failed`, `challenged`, a drift code) — or `no_url`, "no URL is known for this id" | `cr_ratings`, `cr_filters`, `cr_product`, `cr_reliability`, `cr_car`, `cr_cars` |
| `refresh_skipped` | `refresh=true` inside the 300 s cooldown was answered from a qualifying row younger than that (§8 *Retention*) | the products tools, `cr_reliability` |
| `availability_not_cached` | no reliability row for the category is cached, so `availability` is `null` — it is never fetched to populate | `cr_product` |
| `availability_stale` | `availability` was merged from a reliability row past its TTL | `cr_product` |
| `survey_flag_mismatch:<survey>` | CR's `HasReliabilityData` / `HasOwnerSatisfactionData` flag disagrees with whether that survey's rows are present (`reliability`, `owner_satisfaction`) | `cr_reliability` |
| `typeahead_unavailable` | CR's typeahead failed or was challenged; only the local index was searched for categories | `cr_search` |
| `ratings_unavailable:<modelYearId>:<reason>` | one row's per-car ratings fetch failed under `detail="standard"`; the row is listed without its road-test keys | `cr_cars` |
| `index_refresh_failed:<reason>` | the cars index refetch failed and the cached index answered | `cr_car_search` |
| `session_expiring:<days>` | the stored cookie's remaining life — CR's own expiry when measured, the assumed 365-day bound otherwise — is inside 30 days (§6 *renewal*) | every tool that carries `session` |

### Envelope fields by tool

Not every field is meaningful on every tool, and inventing a per-tool answer at implementation
time is how eleven tools end up with eleven envelopes. `—` means the key is **omitted**, not null.

**Products:**

| Field | `cr_ratings` | `cr_product` | `cr_filters` | `cr_reliability` | `cr_categories` | `cr_search` |
|---|---|---|---|---|---|---|
| `auth_state` | ✓ (`null` with no served row, §7) | ✓ (same) | ✓ (same) | always `anonymous` (§7) | — | — |
| `session` | ✓ | ✓ | ✓ | — | ✓ | ✓ |
| `scores_available` | ✓ | ✓ | ✓ | survey keys only (§7) | — | — |
| `sort` | ✓ | — | — | — | — | — |
| `provenance` | ✓ (null exactly when `auth_state` is, §7) | ✓ (same) | ✓ (same) | ✓ (no `superseded_at`) | index fetch | index fetch |
| `warnings` / `error` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

**Cars:**

| Field | `cr_car_search` | `cr_cars` | `cr_car` |
|---|---|---|---|
| `auth_state` / `data_tier` | — | — | — |
| `session` | ✓ | ✓ | ✓ |
| `scores_available` | — | ✓ (`available`/`absent`, or null = not looked) | ✓ (same) |
| `sort` | — | — | — |
| `provenance` | ✓ | ✓ | ✓ |
| `warnings` / `error` | ✓ | ✓ | ✓ |

**Cars omit `auth_state` and `data_tier` entirely**, for the reason `cr_reliability` pins its
`auth_state` to `"anonymous"` (§7) — the two differ in which key they drop, not in why: the data
does not vary by session, so reporting a tier would imply a distinction that does
not exist. `session` stays, because "your cookie is dead" is worth knowing on any call.

Cars carry no `sort`, and the reason is a real constraint rather than a simplification.
`overallScoreSortIndex` lives inside the **per-id ratings payload** (`RECON.md` §11c), so ordering
a listing by score would mean fetching every model-year in the population — N requests before the
first row (§5) — and the bare listing endpoint's own paging is broken (`RECON.md` §13c).
**`cr_cars` therefore returns `v2/cr/cars`' own order — years descending within a model, nothing
else moved — and `offset` pages over it so that every model-year is reached exactly once (§5).**
Score order is available where it is affordable — within a `cr_car` result, and via
`ratingsCategory.overallRank`, which each ratings payload carries for its own car. Under
`detail="standard"` the returned page *has* its scores, so a caller can order what they were
given; what is not offered is score-ordering a population that has not been fetched.

`cr_categories` and `cr_search` span the A-Z index and, for `cr_search`, many cached categories
of mixed tier — so a single `fetched_at`, `cr_url` or `scores_available` would be a fiction.
Their `fetched_at`/`cr_url` describe the **index**, and `cr_search` labels per-result provenance
inside `data` instead. They still carry `session`, because "your cookie is dead" is worth
knowing on any call.

**Auth:**

| Field | `cr_sign_in` | `cr_auth_status` |
|---|---|---|
| `auth_state` / `scores_available` / `sort` / `provenance` | — | — |
| `session` | ✓ | ✓ |
| `warnings` / `error` | ✓ (`error` always null) | ✓ (`error` always null) |
| `data` | `{status, reason, instructions, browser, expires_in_s}` | `{source, captured_at, expires_at, expiry_basis, days_left_max, sign_in, reason, browser}` |

**The two auth tools wear the same outer envelope as every other tool, around a status object.**
There is no row to describe, so `provenance` and `scores_available` are omitted like the other
per-tool omissions above; and a refusal or failure is a `data.status` with a machine-readable
`data.reason`, not an error-taxonomy entry, so `error` is always null — but the key is there,
because "every tool returns `{…, warnings, error, data}`" has to be true of every tool for an
agent to rely on it. An earlier draft made these two flat (`{status, reason, …, session}`),
which contradicted this table and made the next paragraph structurally false for `cr_sign_in`.
Both are typed models with enums like everything else, and no field at any depth could ever
hold a cookie value.

**`warnings` carries `session_expiring:<days>` on every tool that carries `session`** —
products, cars, `cr_auth_status`, `cr_sign_in` — once the stored cookie's remaining life (measured
or assumed, §6 *renewal*) is inside 30 days. `cr_reliability` drops `session` and so drops this too.

### Tool descriptions are part of the contract

The tool description is the one piece of this spec an agent is **guaranteed** to read — it is in
the tool list, before any call is made. An earlier draft specified the envelope in detail and left
the descriptions to be written at implementation time, which put the project's load-bearing
sentence in the least-read place. They are spec text:

| Tool | Description text |
|---|---|
| `cr_ratings` | Consumer Reports ratings for a product category, ranked within CR's own display groups. **A `null` score means "not visible in this session", never "CR did not rate this model" — check `auth_state`.** Scores are never estimated. |
| `cr_product` | Full Consumer Reports record for one product: every scored attribute, specs, owner satisfaction, retailer prices. Same `null` rule as `cr_ratings`. |
| `cr_filters` | What is filterable in a category and the legal values, including attribute descriptions and units. Call this before guessing filter names. |
| `cr_reliability` | Consumer Reports **brand-level** predicted reliability and owner satisfaction for a category. Brand-level, not model-level — these are survey results per brand, not a rating of any single product. Available without a membership. |
| `cr_categories` | The 346 Consumer Reports product categories with ids and slugs. Cars have their own tools. |
| `cr_search` | Find which category covers a product, or a model by name/number among already-cached categories. Category hits are ranked and carry `match`: a `partial` first hit is the best of weak fits, not the answer. An empty result means "not in what has been fetched", never "CR does not rate it" — the response names what was searched. |
| `cr_car_search` | Find a Consumer Reports car by make, model and year. Returns identifiers, not ratings — pass one to `cr_car`. |
| `cr_cars` | List Consumer Reports car model-years by type, make or new/used. Returns safety verdict, popular score, fuel economy and incentives in one request; pass `detail="standard"` to add road-test scores, which cost one request per car. |
| `cr_car` | Full Consumer Reports record for one car model-year: Overall Score, road-test score, per-test ratings, predicted reliability, owner satisfaction, crash tests and specs. |
| `cr_sign_in` | Connect the user's Consumer Reports membership: opens a browser window on their screen at CR's own sign-in page, where they type their password into CR's form; the server keeps only the resulting session cookie and never sees the password. **Call this ONLY when the user explicitly asks to connect or renew their membership — never to fill in null scores on your own.** Returns within seconds; poll `cr_auth_status` for the outcome. |
| `cr_auth_status` | Whether a Consumer Reports member session is configured and healthy, how many days the stored cookie has left at most, and the state of any sign-in in progress. Never returns the cookie. Pass `wait_s` (up to 45) to wait for a running sign-in to finish. |

**The `cr_sign_in` description carries the consent rule, because the description is the one place
an agent is guaranteed to read.** A tool that opens a window on the user's screen must not be
called on the agent's own initiative; the annotations (below) make the client prompt, and the
description makes the agent not ask in the first place unless the user did.

### Two structural guards, because prose can be skipped

`auth_state` plus `scores_available` is a sound design that still asks an agent to correlate two
envelope fields with a `null` forty lines below. Two cheap additions make the contract
machine-visible rather than documentary:

**1. Declare `outputSchema` on every tool and return `structuredContent`.** **This requires typed
envelope classes, not `dict` returns** — under `mcp==2.1.1` a tool annotated `-> dict` produces an
untyped object schema, which is exactly the guard failing open. The envelope and every `data`
shape are declared as models so the schema carries the real enums. MCP clients can then
see `auth_state`, `data_tier` and `scores_available` as typed enums rather than as text the model
may or may not attend to. This is the only place in the design where the null-score contract
becomes checkable by something other than the model's attention, and it costs nothing at runtime.

**2. Emit `data.notice` when, and only when, the gated fields are entirely null.** One sentence,
adjacent to the data being read rather than in a header above it:

> `"overall_score and ratings are null for every product in this response because auth_state is
> anonymous. This is a session limitation, not an absence of CR ratings."`

**The condition is `scores_available.overall_score == "unavailable"`, not "the scores are
null".** Keying on nullness alone would fire on a member fetch of a category CR simply has not
scored — announcing a session limitation that does not exist — and would hardcode the paywall
boundary §10 forbids hardcoding. Reusing the derived enum means the notice fires exactly when
the cause is genuinely indeterminate, and the sentence names the actual `auth_state` rather than
asserting `anonymous`.

Emitted only in that condition, so it never becomes background noise the model learns to
skip. Where a request is *only* answerable with scores, the same field says so at the top level
rather than returning a plausible-looking empty result.

### Tool contracts

Concrete enough to code against — these were the gaps that would otherwise be invented at
implementation time:

- **`category`** accepts the `cNNNN` id, a bare integer or numeric string (`37162`, `"37162"`),
  or the slug; ids are canonical internally. A model that has seen `cid` in one response and
  types `37162` in the next should not get an error for it.
  `cr_categories` returns both. **The slug is the last path segment before `/cNNNN/`** —
  `french-door-refrigerator`, not the full path and not the display name. Slugs are not
  guaranteed unique across 346 categories, so a slug matching more than one entry returns
  `ambiguous_category` listing the candidate ids rather than silently picking the first.
  **The slug is taken from the canonical URL, not the A-Z index URL** — the two differ for
  redirecting categories (`RECON.md` §9f), and the canonical one is what the cache records in
  `final_url`. Both are accepted on input; `category_index` stores the canonical.

- **Filters are flat top-level parameters, not a nested object mirroring CR's taxonomy.** An
  earlier draft took `filters={type, categories, brands, price, features, custom}` — CR's own
  internal filter ids, with `custom: {"isRecommended": true}` whose option id was a guess
  (`RECON.md` §1 records the option *shape*, never that key's value). That is an endpoint
  wearing a question's clothes, and it costs an agent three levels of nesting to ask "Bosch,
  under $3000, CR Recommended". `AND` across parameters throughout:

  | Parameter | Accepted value | CR filter it consumes |
  |---|---|---|
  | `group` | display-group name **or** `categories` option id | `categories` — **these are the display groups**; filtering to one makes the result set score-comparable (§5) |
  | `brands` | array of brand names or brand ids, `OR` within the array | `brands` |
  | `price_min` / `price_max` | number, either bound optional | `price` |
  | `recommended` | `true` / `false` | the `isRecommended` option inside `custom` |
  | | *(omitted = no filter; `false` filters **to** non-recommended models, it does not disable the filter)* | |
  | `features` | object keyed by `attributeId` **or** attribute name → scalar, `[min, max]` for numeric, `true`/`false` for boolean | `features` |

  **CR's `type` filter is dropped because it does nothing** — measured, not assumed. It carries
  **exactly one option, the category you are already on**, in all five categories tested across
  three franchises (`RECON.md` §9h). It is a label rendered as a control, not a filter;
  implementing it would add a parameter whose only legal value is a no-op.

  **Product-type scoping is real, and it lives elsewhere** — see *Family navigation* below. The
  concern it answers ("don't show me microwaves while I'm researching refrigerators") is
  genuine, and it is served by `args.scid` / `cats[]` / `subcats[]`, which ship on every category
  fetch and which an earlier draft used only as "a second discovery path".

  **`price_min`/`price_max` deliberately diverge from CR's shape.** CR ships `price` as
  `input-text` with a list of discrete price labels (256 on `c37162`), but filtering is local
  (§7), so the server ranges over `product.price` directly and uses CR's `data[]` only to
  advertise observed bounds. Making an agent pick from 256 labels would be hostile for no gain.
  Categories with no price at all — Banks is `0/69` in both tiers (`RECON.md` §9f) — return
  `filter_on_unavailable_attribute` rather than an empty list.

  **`brands` and `features` accept names as well as ids.** Ids remain canonical internally and
  in `cr_filters` output (names drift; ids do not — the `attributeId` rule), but requiring an
  agent to call `cr_filters` before every query to translate "Bosch" into `158113` is a round
  trip that buys nothing. Name matching is case-insensitive and exact-after-trim, never fuzzy.
  **Both `name` and `displayName` are matched** — definitions carry both (`RECON.md` §1) and an
  agent reading a rendered label has no way to know which one it saw. A string matching two
  different definitions returns `ambiguous_filter_name` with both ids; a string matching the
  `name` of one and the `displayName` of the same one is not ambiguous.

  **Booleans take `true`/`false`, not `"Yes"`/`"No"`.** The stored values are the strings
  `"Yes"`/`"No"` (§7 normalization), so the engine maps the caller's boolean onto them. A caller
  passing `"Yes"` is accepted too; passing `true` and silently matching nothing would be a
  zero-results bug with no error.

  **Filtering on an attribute whose values are all null is an error, not an empty set.** A range
  filter over a `numeric-rating-score` attribute matches nothing anonymously, and "no products
  match" would be indistinguishable from "those scores are paywalled" — this project's named
  worst failure reproduced at the filter level. It returns `filter_on_unavailable_attribute`
  naming the attribute, with `reason` taking the **same values `scores_available` uses**:
  `"unavailable"` when `data_tier` is `anonymous`, `"absent"` when `member`. Not `"gated"` — the
  envelope deliberately avoids that word because anonymously the two causes are
  indistinguishable, and a category with no prices at all (Banks, `RECON.md` §9f) would be
  labelled gated when nothing about price is gated for anyone. **The test is on the served payload, not
  on the session** — under never-downgrade an anonymous caller can be served a retained member
  row, where the filter works fine and must not error. The earlier name `gated_filter` keyed on
  session state and was wrong in both directions.

  An unknown filter name, option id or value type is an error (below), never silently dropped.

- **`sort`** is `overallScore` (default) or `price`, plus `order` (`desc` default). The envelope
  echoes `{key, order, scope}` back.

  **Within a group, `overallScore` sorts on `_overallSortIndex` ascending, in both tiers,
  always.** It is CR's
  own within-group ordering, identical anonymous-vs-member (`RECON.md` §9g), and it is the only
  key that gives members and anonymous callers the same `rank`. Sorting members on
  `overallDisplayScore` instead would reorder the 71 tied products on `c37162` arbitrarily
  against CR's own presentation, and break the tier-independence that makes anonymous ranking
  worth having. `overallDisplayScore` is returned, never sorted on.

  **`price` puts nulls last in both directions.** Not first in `asc` — a category where CR
  publishes no price (Banks) would otherwise open with 69 blanks.

  **Across groups** — flat mode with an explicit `sort="overallScore"` — there is no shared
  index, so the ordering falls back to `overallDisplayScore` and is therefore **member-only and
  score-dependent**: anonymously every value is `null` and the order degenerates to CR's page
  order. That asymmetry is exactly why `sort.scope` is a field: `"cross_group"` tells the agent
  both that the populations differ *and* that this ordering is not the tier-independent one.

- **`group_mode`** is `"nested"` (default) or `"flat"`. Nested is the default because an agent
  handed a flat list will compare the numbers in it whatever a `group` field says (§7).

  **Flat mode's default sort is CR's own page order — `(group, rank)`.** That is what CR's page
  shows, it is not a cross-group score comparison, and it means asking for one array does not
  silently opt you into one. `sort.scope` reports `"within_group"`.

  An **explicit** `sort="overallScore"` with `group_mode="flat"` on a multi-group category is
  honoured, ordered on `overallDisplayScore` across the whole set, with `sort.scope:
  "cross_group"` and a `data.notice`. Nothing is refused; the caller is told what they got.

  **"Explicit" requires `sort` to default to `null`, not to `"overallScore"`.** An MCP call cannot
  otherwise distinguish a caller who passed the default from one who omitted the parameter, and
  that distinction is the whole trigger. So the declared default is `null`, meaning "no preference"
  — resolved to `_overallSortIndex` within a group and to `(group, rank)` in flat mode. The
  documented default is still "score order"; it is the *schema* default that must be nullable.

- **`rank` is the product's position in CR's *unfiltered* group table.** Dense positions from
  `_overallSortIndex` (ties share a position; 1, 2, 3 — never competition ranking, which would
  need the score and would therefore differ by tier). It does **not** renumber under `brands`,
  `price_min`, `features` or `sort="price"`: filtering to Bosch shows Bosch's fifth-best model as
  `rank: 5`, not `rank: 1`, because the number an agent quotes must mean "fifth best in this
  group per CR" and not "fifth best among what I happened to ask for". Each group therefore
  carries **two counts**: `size` (products in CR's group, unfiltered) and `total` (products
  matching this call). `rank` is `null` only where `_overallSortIndex` is null.

- **`attributes`** (optional) projects named attributes into every returned product regardless of
  `detail`, keyed by `attributeId` or name. Specs are otherwise drilldown-only, so an agent
  shortlisting 30-inch counter-depth refrigerators can *filter* on width but cannot *see* it
  without one `cr_product` call per row. Costs a few tokens per product and removes the most
  common N+1 in the whole surface.

- **`detail="standard"` = the first three `numeric-rating-score` attributes in
  `categoryAttributes` **`sortOrder`** order**, with entries lacking `sortOrder` sorted last and
  ties broken by `attributeId`. The tiebreak is required, not tidiness: `sortOrder` is absent on
  some entries in every tier, so an unguarded sort is either unstable or raises on `None`.

  **This is measured, and the obvious alternative is a bug.** `categoryAttributes` ships in a
  *different array order* for a member than for an anonymous caller — same ids, same content per
  entry, shuffled sequence (`RECON.md` §10j). Selecting by `sortOrder` returns the same three
  attributes in both tiers; selecting by **declaration order returns three entirely different
  attributes**. An earlier draft specified declaration order and justified it as "a fixed,
  category-stable order … so anonymous and member responses have the same shape" — the rule
  delivered the exact opposite of its own stated goal. It also read from `attrs[]`, which misses
  attributes in 6 of 8 categories (`RECON.md` §1).

- **`limit`** defaults to **25 in flat mode and 10 per group in nested mode**, max 200 either
  way — except `detail="full"`, capped at 10 (§7 sizing) — and validated **before** the fetch, so
  a bad `limit` costs a rejection rather than an 11 MB download, paired with **`offset`** (default 0). **In nested mode both apply per group**, and each group carries its own `size`, `total` and `truncated`; flat
  mode applies them to the whole list. `truncated: true` whenever `offset + limit < total`, so
  results never silently truncate. `offset >= total` is an empty list with the real `total`, not
  an error. `offset` exists because the largest category measured is 172 against a cap of 200
  and **categories do exceed it** — Televisions 303, Mattresses 293 (`RECON.md` §12c). Without
  `offset`, products beyond 200 in those categories would be unreachable by any call.

- **`refresh`** (default `false`) forces a network fetch past a cache hit. Accepted by every
  data tool (the two auth tools take none — there is no row behind them). For `cr_categories`
  and `cr_search` it refreshes **only the A-Z index** — neither ever
  fetches a category payload, and without `refresh` the index is otherwise pinned to its
  90-day TTL with no way to update it. `cr_product(id, refresh=true)` refetches **the one
  category the selection rule chose** (§8), not every category containing that product.
  **`refresh` has a cooldown.** The data tools are annotated read-only, so a client never
  prompts for `refresh=true` — and "call it fifty times with refresh" would cost fifty 11 MB
  fetches and fifty appended rows. When a row the caller could be served is younger than
  `REFRESH_COOLDOWN_S` (300 s), that row is served with `warnings: ["refresh_skipped"]` and
  `from_cache: true` instead of a fetch. The cooldown counts only rows at the caller's effective
  tier (§8) — a fresh anonymous row does not stop a newly signed-in member from fetching — and
  the `auth` probe is exempt: it is a verdict on a credential and reads the fetch's own
  classification, so it must always go to the network.

- **`cr_product(id)`** requires the containing category to be cached or fetchable; there is no
  product-level endpoint in this model. An unknown id returns `unknown_product` with a
  suggestion to run `cr_ratings` or `cr_search` for the category first. Takes
  `include_descriptions=false` by default — descriptions are category constants served by
  `cr_filters`, and carrying them per product more than doubles the response. Because
  `product_index` is many-to-many and outlives a product's removal from a category, the
  selection rule reads "the newest scored row **that still contains this product**", falling
  back to the newest row of any tier that does; an index entry with no surviving payload is
  pruned rather than returned as `unknown_product`.

- **`group` ships in EVERY product shape, at every `detail` level.** It is `_groupName` — CR's
  display segment, the unit within which scores and ranks are meaningful (§5). Omitting it lets a
  caller compare a 71 against a 79 that were never on the same scale, which is the same class of
  error as reading a stripped score as an absent rating. `rank` is always within-group and is
  meaningless without it. **Nesting is decided by distinct `_groupId` count**, and `group` is
  **never null**: a single-group category names itself — Banks ships `_groupId` = `37154` (its own
  `cid`) and `_groupName` = `"Banks"` on all 69 products (`RECON.md` §9h). One group means flat.
  Single-group categories also ship an **empty** `categories` filter, not a one-option one, so
  `cr_filters` correctly advertises no `group` values for them. **Nesting keys on distinct
  `_groupId` present in `data`, never on `len(subcats)`** — `c37162` ships four `subcats` but only
  three groups have products (`RECON.md` §10g), so counting the taxonomy would nest an empty
  fourth group. **A product CR ships without a `_groupId`** (never observed; defended against)
  is the one exception to "never null": `group: null` and `rank: null` — there is no table for
  it to be ranked in, and ranking the groupless among themselves handed such a product
  `rank: 1` — it sorts after every group, nests into one trailing block with `group_id: null`,
  and the envelope warns `ungrouped_products:<n>`. It never makes a single-group category
  multi-group (an explicit flat `overallScore` sort stays `within_group`), and it is never
  dropped: structure is the guard, not withholding.

- **`dont_buy` ships in EVERY product shape, at every `detail` level, including `summary`.**
  It is `expertRatings.isDontBuy` — CR's safety/defect warning. It is rare (0 across the 7
  categories measured), and that is precisely why it must never be a drilldown-only field: a
  model CR warns against appearing in a ranked list with no flag is the worst output this
  server could produce. Rarity makes it cheap to carry, not safe to omit.

- **`smart_buy` ships in EVERY product shape**, alongside `recommended`, sourced from
  `_shoppingParsed.isSmartBuy`. It is a CR designation distinct from CR Recommended, available
  anonymously, and genuinely sparse — 0 of 172 French-Door refrigerators but 9 of 66 gas ranges
  — so it is meaningful when present. (An earlier draft also listed it under `full` only; it is
  one boolean and it ships everywhere.)

### Error taxonomy

Errors are **structured values in the envelope**, not MCP protocol errors — an agent must be
able to distinguish "CR has no data" from "the fetch failed", and a protocol error erases that
distinction. `error` is null on success, and otherwise carries `code` plus whatever of these
applies — every field optional, so an agent reads the ones it understands and ignores the rest:

```jsonc
{
  "code": "invalid_filter_value",   // the closed enum below
  "message": "…",                   // human-readable, and the only free-text field
  "reason": "no_such_route",        // the transport/absence sub-reason, itself enumerated
  "retryable": false,               // whether trying the same call again could work
  "http_status": 429,               // when a status caused it
  "title": "…",                     // the <title> of an unexpected page; never its body
  "candidates": [{"id": 28978, "name": "Refrigerators"}],  // legal values, when they are knowable —
                                    // ALWAYS a list of objects whose keys fit the parameter, in
                                    // one of three shapes (below); never a bare scalar, and
                                    // never only in the prose
  "filter": "attributes",           // WHICH parameter was wrong — the caller's name for it
  "attribute": "…"                  // the attribute a filter error is about
}
```

**`candidates` carries the legal set wherever the site knows it, in one of three shapes, and
the prose beside it is never the only machine-readable source.** Measured 2026-09-06: of the
`invalid_filter_value` sites only `year` populated it — `car_type` listed seven legal slugs in
the message with `candidates: null`, and `group` put the legal names *and their ids* in the
prose (`'24-Inch' (33347)`) while the structured field designed for exactly that stood empty.
That contradicts the project's own rule that these are structural fields, not prose. The shapes,
each with one constructor so the key is the same on both surfaces:

| Shape | Constructor | Parameters |
|---|---|---|
| `[{"value": v}, …]` — a closed vocabulary, keyed by the word the caller passes | `envelope.legal_values` | `detail` (all four tools that take it), `sort`, `order`, `group_mode`, `state` |
| `[{"min": lo, "max": hi}]` — a numeric span, the one entry IS the legal set; an open bound is `null` | `envelope.legal_range` | `year` (both the local check and CR's translated `400`, the span known), `limit` (`1`–the cap for that `detail`, both surfaces), `offset` (`0`–`null`) |
| `[{id, name}, …]`-style rows — CR's vocabulary, with the key the caller passes and the name they will recognise | `envelope.candidates` over the rows | `group` `{id, name}`; a cars `category` under a type `{id, name}`; `car_type` `{id, slug, name}`; `make` `{slug, name}`; `family` `{id, name}` (both refusals, once a family is known); `ambiguous_*` ids `{id}`; the display-group hint `{id, slug, name, group_id, group}` |

Two vocabularies are offered as **near matches, not the full set**: `brands` and an unknown
attribute (`features=`/`attributes=`, `unknown_filter`), like `make` on cars — names that
contain the caller's token or are contained by it, `{id, name}`. The complete legal set runs
to dozens on a real category, and `candidates` is capped at 12 without a count of what was
left out (below), so a truncated full list would read as the legal set when it is a twelfth
of it; `cr_filters` is the structured home of the complete list, and the message says so. No
near match is `candidates: null`, never `[]`.

Sites that legitimately offer nothing: `query` (its legal set is a length, not values — the
message carries the limit); the boolean and numeric `features` value-shape errors (`true`/
`false`, a number or `[min, max]` — a type, not a vocabulary); `brands` given a non-list and
`features` given a non-object (the same); a cars `category` that is not an integer, or one
passed without `car_type` (the fix is a companion parameter, not a value); the unfiltered
`cr_cars` refusal (`car_type` is the parameter to *add*); `make` and `year` with no index yet
(nothing known to offer; the message points at `cr_car_search`); `unknown_category` for an
ordinary miss (346 categories; `cr_search` is the tool); `unknown_product`; every transport
and drift code.

**`reason`, `retryable` and `http_status` are projected from a transport failure by one
function, `transport.failure_fields`, and its code by `transport.failure_code`.** Every site
that catches a `FetchFailed` or `Challenged` unpacks that dict — into a `ToolError`, a
fallback's `detail`, a negative-cache entry — rather than spelling the three fields itself.
The two types answer the same three questions, with an asymmetry that is a fact about CR: a
`Challenged`'s `reason` is its challenge type, its `http_status` is always the status the
challenge arrived on (a `FetchFailed` carries one only when a status caused it), and it is
never retryable — surfaced, and negatively cached for an hour, so a retry would replay it. The
exception's `url` and `detail` (free text) never escape. Two tests hold the line: the
projection must read every attribute the exceptions store, and no other module may read a
projected attribute off a caught failure (`.reason` alone is a decision attribute).

**`filter` names the parameter the caller passed, not the one the code happens to resolve
through.** The same attribute resolver serves `features=` and `attributes=`, so it takes the
caller's parameter name — a bad `attributes=[999999]` reports `filter: "attributes"`, and pointing
at `features` would send the caller to edit an argument they never supplied.

**A value quoted into `message` — the caller's token, or CR's name for what it was compared
against — goes through one function, `envelope.quoted`, and is bounded.** `message` is the
one free-text field, and every site that echoed a token used `!r` unbounded: a
200,000-character `category` came back as a 200,000-character `unknown_category` message, the
same hole `no_results` closed for `warnings` (above) and the one place left where a call could
inflate a response without limit. `repr` stays the base, because it is the right one: it
escapes every non-printable (a newline, an ANSI escape, a right-to-left override), and its
quoting is unambiguous — a value holding `'` renders in `"`, one holding both escapes the quote
— so a value can never read as the end of the quoted token and the start of more prose to the
agent parsing the message. What `repr` does not do is bound the length, and past 4,300 digits
it refuses an int outright (`ValueError`, which used to escape the tool). So `quoted` clips
the VALUE at 60 characters before quoting (no escape sequence is cut in half), bounds the
rendering too (escapes expand), describes an int it cannot render, and marks a cut OUTSIDE
the quotes with the true length — `'AAAA'… (200,000 chars)` — so an agent reads "the server
saw a 200,000-character value beginning with this", never a shorter value, and a value that
itself ends in `…` is not mistaken for a cut one. 60 is `no_results`' per-value bound: the
longest CR category name, slug or brand measured is under 40, so a legitimate token is always
shown whole. Lists (`quoted_list`) are capped at 12 — `cr_filters`' value-list cap, the whole
of every legal set measured — with the count of distinct values left out; `candidates` and
`attribute` are bounded the same way (`envelope.candidates`, `envelope.clipped`), since CR's
names land there too and CR's content is attacker-influenceable in this threat model. The one
caller string a tool echoes as typed, `cr_search`/`cr_car_search`'s `query`, is refused past
`SEARCH_QUERY_MAX_CHARS` (200) before any request — never clipped, since a search over a
prefix reported as the whole would misstate what was searched, and never quoted, since its
length is the whole complaint. `tests/test_boundaries.py` scans the tool-facing modules: no
f-string may use `!r`, and every interpolation in an error message is a quoting call or an
expression listed with its reason.

| Code | Cause | Retryable |
|---|---|---|
| `unknown_category` | Category id, slug or display name not in the index — **checked before any fetch**, because CR answers a bad id with a `500`, not a `404` (`RECON.md` §9h), and wafer retries 5xx. Retired slugs still resolve via `category_slug_alias` (§8). **A display-group id passed as a category** (`cr_ratings` prints `group_id` on every nested block, so it is the likeliest bare `cNNNN` in no index) is still `unknown_category` (`not_in_index`, or `discovery_incomplete` while only one source has run — a group id can also be a subcategory URL that aliases to the parent, so the miss is not final then), but the message names the owning category and the `group=` to pass, and `candidates` carries `{id, slug, name, group_id, group}` — known only for categories already fetched, which is exactly when an agent can have seen the id. The id is the one `resolve_category` parsed (`_CID`, carried on the `unknown` resolution) — the token is never re-parsed, so a digit-shaped string the grammar rejects (`'²'`, a 20-digit id) is an ordinary `unknown_category`, never an exception out of the tool | No |
| `ambiguous_category` | A slug matching more than one category; lists candidate ids | No |
| `unknown_filter` / `invalid_filter_value` | Filter name/id or option not in this category | No |
| `ambiguous_filter_name` | A brand or attribute name matching two definitions; lists both ids | No |
| `unknown_product` | Product id not in any cached category — or not a key any row can carry (`cache.row_id`: a positive integer SQLite can bind; a non-integer, non-ASCII digits or a value past 2^63−1 is answered here without a lookup or a request, never an exception out of the tool) | No |
| `session_expired` | **`auth` validation only** — the pasted cookies did not authenticate. A *tool* fetch that comes back logged out is `auth_state`, `error: null` (above) | No — re-supply the cookie |
| `fetch_failed` | Any transport failure — offline, DNS, connection refused, `WaferTimeout`, 5xx, or a cache miss with no network. Carries `reason` and `retryable` | Per `reason` |
| `challenged` | wafer `ChallengeDetected` / `RequestBlocked` / `RateLimited`, or a 403/429 under `max_rotations=0`. `reason` is `rate_limited` (429), `blocked` (a 403/429 on a page host), or `token_mint_failed` (wafer could not mint a challenge token) | **No** — surface it |
| `payload_missing` | `window.filterInstanceDATA` absent or unparseable | No — schema drift |
| `marker_missing` | neither `data-subscriber` value present on a *category* page | No — schema drift |
| `reliability_payload_missing` | `window.initStore` absent or unparseable | No — schema drift |
| `credential_rejected` | CR redirected the fetch to `/ec/login` — the configured cookie was rejected (`RECON.md` §10b). Surfaced as `session: "expired"`; `auth_state` follows the served row — `session_expired` when a cached anonymous row answers, `null` when nothing is cached and this code is the error — and never as a drift alarm | No — re-supply the cookie |
| `filter_on_unavailable_attribute` | Filtered on an attribute whose values are all null in the served payload; carries `reason: "unavailable"` \| `"absent"` (§7) | No — sign in, or CR has none |
| `unknown_car` | `cr_car` on a model-year id the cars API does not know: a `403`/`404` (no such route), **or a `200` whose `response` is `{}` with `responseSummary.responseCount == 0`** — measured 2026-09-04, and the usual answer for an unknown id. No other status qualifies; a `429`/`400` is `challenged`/`fetch_failed`. An id that is not a key any row can carry (`cache.row_id`, as for `unknown_product`) is answered here without a request | No |

**`EmptyResponse` maps to `fetch_failed` with `reason: "empty_response"`.** Under the
no-rotation construction (§6) an empty 200 is *returned* rather than raised, so it reaches the
parser and would otherwise surface as `payload_missing` — a schema-drift alarm for what is
actually a transport hiccup. In a no-cookie process wafer raises it instead; both paths land on
the same code.

**`cache_miss_offline` is gone, folded into `fetch_failed`.** wafer raises `ConnectionFailed`
for an offline host and for CR being down alike, so the two were not distinguishable at the point
the error is constructed — the split invited an implementer to guess. One code, with a `reason`
and an explicit `retryable`:

| `reason` | Derived from | `retryable` |
|---|---|---|
| `connection_failed` | `ConnectionFailed` | yes |
| `timeout` | `WaferTimeout` | yes |
| `server_error` | a returned 5xx | yes |
| `empty_response` | `EmptyResponse`, or an empty body returned under no-rotation; also a cars-api `200` that is not JSON or carries no `response` object, and a `v1/cr/keys` / `v2/cr/carTypes` payload that parses to zero rows (§5) — never written, never recorded | yes |
| `too_large` | `ResponseTooLarge` | no |
| `redirect_loop` | `TooManyRedirects` — a cycle repeats, so it is not retryable | no |
| `no_such_route` | a `403` (or `404`) from `cars-api`, which answers unknown routes that way (§5), **or an empty `200` from `v2/cr/modelYears/{id}`** — raised before the cache write, so an unknown id is never stored as an all-null car. **Never an auth failure** | no |
| `bad_request` | a `400` from `cars-api` — our request was malformed (measured: `size` above 50). Never a data absence. **One exception is translated before it reaches a tool result:** a `400` from the listing while `modelYear` was sent is CR rejecting `year` as a value (§5) and is answered as `invalid_filter_value` on `year`, since the other listing parameters are resolved against fetched vocabularies and cannot be malformed. The translation is made in `list_cars`, beside the request — the repository decides which statuses are tool-level answers, as the products repository does — so `cr_cars` catches it as an ordinary `CarsQueryError` | no |
| `identity_rotated` | `hash` absent from the jar after a logged-out response | no |
| `policy_changed` | a `www.` response that arrived after `Transport.adopt()` moved the auth policy mid-flight (§6 *cr_sign_in*). It was made under the old credential or none, so it is discarded rather than classified under the new one — never cached, never an auth state. Our own state change, so every caller that owns a tool call (`get_category`, `get_reliability`, `CarsApi.page_info`, the sitemap pass) retries it exactly once; it reaches a tool result only when the retry fails the same way | yes |
| `url_unresolved` | no URL could be resolved for the target: a constructed reliability URL that 404s with nothing cached, or a category id with no known URL. Also raised (retryable) when the car page yields no api-key, and (not retryable) when a URL fails the transport's host allowlist — not `https`, or a host other than `www.`, `secure.` and `cars-api.consumerreports.org` (§8 *Concurrency*) | no, except the api-key case |

**`reason` is derived from the exception type, never by parsing `ConnectionFailed.reason`.** That
attribute is a free-text string whose content wafer documents for exactly one case (a host that
resolved only to `0.0.0.0`/`::`); treating it as an enum of `offline`/`dns`/`refused` would be
inventing an API. Carry its text through for diagnostics and classify on the type.

**A challenge can arrive with a `200`.** Classification must check `resp.challenge_type is not
None` and not only the status code — some WAFs serve a challenge body under a success status.

**`no_survey_data` is gone.** §7 already specifies the non-error answer — `has_reliability_data:
false` with `brands: []` — and having both meant the same condition could be a success *and* an
error, with `data` populated alongside a non-null `error` in violation of the rule below. A
category CR runs no survey on is not a failure.

**`expired` and `rejected` are different facts, and the server can tell them apart.** `session`
carries five values, not four: `expired` is a claim about the CLOCK and is made **only** with
local proof (`now > expires_at`, no fetch required); `rejected` is CR declining to honour a
cookie that has not run out. `cr_auth_status.data.session_reason` names which observation
produced it — `cookie_past_expiry`, `login_redirect` (CR sent the request to `/ec/login`,
`RECON.md` §10b) or `served_anonymous` (an ordinary anonymous page with the cookie still in the
jar, `RECON.md` §5). The clock wins when both apply: a cookie past its expiry is expired
whatever CR answered. Not knowing the expiry is not proof of one, so a state with no store to
ask reports `rejected` — what it actually observed.

Collapsed into one word, this told a member with **363.7 days left** that their cookie had
expired, with `reason: null`, while the real cause was a lapsed `userLicenses` of our own
(§6 *the store never holds both*). The natural next sentence — "your cookie expired, re-capture
it" — was false, and `days_left_max` sitting beside it read as a contradiction rather than as
the useful fact that the cookie was fine. The notice said "has expired **or** was rejected",
naming both because it could not tell; it now says which (`EXPIRED_FIX_WHAT`).

`auth_state` deliberately keeps ONE token, `session_expired`, for both. It answers "why are
these scores null", and the answer and the remedy are identical either way; splitting it would
churn every products envelope for a distinction that belongs in `session` and `session_reason`,
where it is a diagnostic rather than a cause of nulls.

**`session_expired` names the path it can be fixed on**: `cr_sign_in` first, then
`consumer-reports-mcp auth`, then "update `CR_SESSION_COOKIE` if the cookie came from there"
(§6 *renewal*; the text is `envelope.EXPIRED_FIX_TEMPLATE`, appended to the `data.notice`).
Telling a container user to run a CLI that writes a file the server will not read is a dead end,
and telling a Desktop user to open a terminal is another.

`payload_missing` and `marker_missing` are the schema-drift alarms: they mean CR moved
something, and they must be loud rather than degrading into an empty result set.

### Partial and empty payloads

A present-but-thin payload is not drift, and conflating them makes both unactionable:

- **All four top-level keys present, `data` empty** → success, `total: 0`, and
  `warnings: ["empty_category"]`. CR does occasionally publish a category it has not populated.
- **Filters excluded every product, but the category is populated** → success, `total: 0`,
  `size` still the unfiltered count, and `warnings: ["no_results:<k=v,…>"]`. This is a
  different fact from `empty_category` — "your filter was too narrow" versus "CR rates nothing
  here" — and the two are **mutually exclusive**: an agent handed a bare `[]` cannot tell them
  apart, and widening a filter is the right move for only one of them. Because the distinction
  is the point, filtering an EMPTY category never raises `filter_on_unavailable_attribute`:
  `all()` over zero products is vacuously true, so that check is gated on the category having
  products, or an empty category would answer a price filter with "sign in to find out whether
  Consumer Reports publishes it" — a claim about the session about data that does not exist.
  - `<params>` names the **tool's own parameters**, on `cr_ratings` and `cr_cars` alike — a
    caller that passed `state="used"` cannot act on CR's internal `modelYearStateId=1`.
  - Pairs join on `,`, a list on `|`, a features entry as `name:value`; every value is
    **percent-encoded**, so no value can carry a delimiter, `brands=["a|b"]` stays distinct
    from `brands=["a","b"]`, and a caller cannot forge a second vocabulary token inside a
    value. Values are deduped and bounded (§7): they are caller input, and unbounded they were
    the one place a single call could inflate a response without limit.
  - Only listing tools emit it. The search tools (`cr_search`, `cr_car_search`) answer the
    empty case structurally inside `data` instead, naming what was searched.
- **Any top-level key missing, or the JSON unparseable** → `payload_missing`, **never cached**,
  carrying the HTTP status and the page `<title>`. Without those two, a 200 maintenance page or a
  consent interstitial fires a schema-drift alarm indistinguishable from a real one.
- **`categoryAttributes` missing while `filterInstanceDATA` parses** → success with
  `warnings: ["attribute_dictionary_missing"]`. The join degrades through its documented fallback
  chain (§7) and every response silently loses units and descriptions, so it warns rather than
  failing — but it must not be silent.
- **A value that will not coerce to its declared `dataType`** (`"36 - 38"` against
  `numeric-general`, `"N/A"` against a rating) → keep `raw_value`, set `value: null`, add
  `warnings: ["coercion_failed:<attributeId>"]`. Never guess from value shape (§7), never drop
  the entry.

**Drift failures are negatively cached for one hour, in memory** — `payload_missing`,
`marker_missing`, `reliability_payload_missing` and `challenged`. A category returning
`payload_missing` otherwise costs an 11 MB fetch on every single call while CR is broken. The
negative entry records the code and is never written to SQLite — only real payloads persist.
**`fetch_failed` is never negatively cached**: an offline laptop that comes back online should
work on the next call, not in an hour. The cars path uses the same cache under its own keys —
the car page, the index, the taxonomy and each model-year (§5 *Cars*) — so a challenged cars-api
is not re-hit on every call either.

**A cached row is served with `error: null` whenever one exists** — including a row the tier
rule (§8) would not have accepted as a *hit*. Tier qualification decides whether to go to the
network; it never decides whether to discard data on the way back. So a member whose fetch fails
still gets the anonymous row, labelled honestly, rather than an error. This holds even past the
TTL and even when the refetch failed: the original `fetched_at`, and `refresh_failed` in `warnings[]`.
`error` is non-null only when there is nothing to return at all, and `data` is never populated
alongside a non-null `error`. Old data plus a warning beats an error that discards data the
caller could have used.

**When both drift alarms would fire**, `payload_missing` wins — it is the more fundamental
failure, and reporting a missing auth marker on a page whose payload is already gone would be
misleading about what broke.

**Cache-first means expiry can go unnoticed.** With a 30-day TTL and no live fetch, a session
can lapse without any tool noticing, so "`session_expired` is never silently reported" holds
only for calls that actually touch the network. This is accepted — the cached data was
correctly labelled `member` when fetched and stays true — but it means a probe fetch is the only
way to check session health on demand: `consumer-reports-mcp auth`, or `cr_sign_in` on an
`unverified` cookie, which runs the same probe before deciding whether to open a window (§6).
`cr_auth_status` reports what this process has recorded, without a request.

**Retries happen in wafer and nowhere else.** By the time an error reaches a tool, wafer has
already exhausted its budget, so the server never re-issues the request itself — layered
retries under a WAF is exactly how a polite client becomes an impolite one, against a real
membership. `challenged` in particular is surfaced to the caller, never retried around.

## 8. Storage

SQLite at `~/.cache/consumer-reports-mcp/cr.db`, WAL mode.

```sql
-- PRAGMA user_version gates migration; every statement is CREATE ... IF NOT EXISTS, so a
-- bump re-runs the whole script and adds what is new. Currently 2 (2 added category_slug_alias).
category_raw(category_id, auth_tier, fetched_at, scored, schema_version, payload_json,
             final_url, requested_id)              -- append-only, source of truth
product_index(product_id, category_id, brand_name, model_name)  -- MANY-TO-MANY, see below
category_index(category_id, slug, display_name, franchise, canonical_url,
               source,            -- 'az' | 'sitemap' | 'payload'
               family_id, family_name, score_min, score_max, score_range_status,
               rated_count, has_reliability_data, groups_json, reliability_url,
               dead_at, first_seen, last_seen)
category_alias(alias_id, category_id)       -- a subcategory id -> the payload's args.cid (§5)
category_slug_alias(slug, category_id)      -- every slug a category has been published under.
                                            -- Discovery prefers the sitemap's shorter canonical
                                            -- URL, and the slug is derived from that URL, so the
                                            -- background pass rewrites slugs the synchronous A-Z
                                            -- pass already handed out. Consulted only after
                                            -- category_index misses, so a live slug always wins.
discovery_runs(source, fetched_at, count)   -- so a pass is not repeated inside its TTL
reliability_raw(category_id, fetched_at, payload_json, final_url)  -- append-only, anonymous only

-- cars: a separate id space and a separate fetch unit (§5)
car_index(model_year_id, make_id, make, slug_make, model_id, model, slug_model, year,
          states_json, car_types_json, primary_car_type_id)
car_taxonomy(fetched_at, payload_json)                           -- v2/cr/carTypes, 90-day TTL
car_raw(model_year_id, fetched_at, schema_version, payload_json)  -- append-only, one tier
```

**`car_raw` is keyed on `model_year_id` and has no `auth_tier` column**, because the cars API has
no tier to distinguish (`RECON.md` §11d): every caller gets the same bytes. **Per-id caching is
the whole cars performance story**, because there is no bulk ratings call — the opposite of
products, where §8 argues per-item caching buys nothing.

**`car_raw` rows serve every caller**, because cars have one tier — there is no membership state
to preserve or degrade, and no never-downgrade rule to apply. `provenance.fetched_at` dates the
row as it does everywhere else. (An earlier draft argued this paragraph at length, when cars were
gated and a cached row raised the question of who could read it back. That question no longer
exists.)

**Cars TTLs.** `car_raw` follows the 30-day category TTL — road-test results move as slowly as
product ratings. `car_index` and the car-type taxonomy follow the 90-day index TTL: makes, models
and body-style categories change on a model-year cadence, not weekly.

**`category_index` carries everything `cr_categories` returns**, because the tool answers from the
index alone — it must never fetch a category to describe one. An earlier draft listed only
`(category_id, slug, display_name, franchise)`, which cannot serve the family, score range, rated
count or groups §7 promises. Notes on the less obvious columns:

- **`source`** — `az` or `sitemap`, recording which pass found it. Without it, "is this id unknown
  or merely not-yet-discovered?" cannot be answered and `unknown_category` would fire for real
  categories before the sitemap pass finishes (§5).
- **`display_name` is nullable.** The sitemaps carry URLs, not labels, so the 110 sitemap-only
  categories have no name until something fetches them. This is why `cr_search` matches slugs (§7).
- **`score_range_status`** — `known` / `none_published` / `not_fetched`, never a bare null (§7).
- **`groups_json`** is `null` until the category itself is fetched. `[]` would assert "single
  group", which is a real and different claim (§7).
- **`dead_at`** marks a sitemap id that 404d, so it is not retried on every call (§5).
  `first_seen` / `last_seen` date the row against the discovery pass that produced it.
- **`source` includes `payload`** — a sibling learned from a category fetch's family block (§7)
  is neither an A-Z nor a sitemap discovery, and conflating it with either would misreport what
  has actually been crawled.
- **`car_index` stores `states_json` and `car_types_json`**, not single values: a model-year can
  be both New and Used, and a car category is scoped by its parent type, so a model-year belongs
  to several `(carTypeId, categoryId)` pairs (§5).
- **`category_alias`** maps a requested subcategory id to the `args.cid` its payload actually
  carries — `c200367` → `c37162` (§5). Without it a caller who asks by the id CR gave them gets
  `unknown_category` for a category that is cached.

### `payload_json` is an ingest envelope, not `filterInstanceDATA`

**The attribute dictionary is not inside `filterInstanceDATA`.** `categoryAttributes` sits
~2.5 MB further into the same HTML, outside the payload (`RECON.md` §1). "Store the extracted
JSON, discard the HTML" and "join definitions from `categoryAttributes`" are both right and,
taken literally together, produce a cache that cannot serve a single unit or description — every
cached response would fall through to `attributeTypeName` for every entry, silently. So what is
stored is everything the parser needs, extracted at ingest:

```jsonc
{
  "schema_version": 1,
  "filter_instance": { … },        // window.filterInstanceDATA
  "category_attributes": [ … ],    // the category's own block, per §7
  "family": [                      // one entry per sibling, including this category
    { "id": 37162, "name": "French-Door Refrigerators", "slug": "french-door-refrigerator",
      "score_min": 43, "score_max": 79, "rated_count": 172,
      "has_reliability_data": true,
      "groups": [ { "id": 200367, "name": "30 Inch and Narrower Widths" }, … ] }
  ],
  "supercategory": { "id": 28978, "name": "Refrigerators" },
  "data_subscriber": "true",       // the raw marker, as read from the HTML
  "final_url": "https://…/c37162/",// after redirects — the A-Z URL may differ (§5)
  "requested_id": 200367,          // when it differs from args.cid (§5); null otherwise
  "http_status": 200
}
```

**Sibling `groups` are `null`, not `[]`.** A category page carries `.subcats` for *itself* only,
so the family entries for its siblings have no group information — and `[]` would claim they are
single-group categories, which is a positive assertion the payload does not support. They fill in
when that sibling is itself fetched.

**`family` is stored in the envelope, not only projected into `category_index`.** Ingest writes
those sibling rows into the index for `cr_categories` to serve (§7), and it would be tempting to
treat the index as their only home. But the index is *derived*, and the payload is the source of
truth precisely so "a parser fix needs no re-fetch" holds. If family parsing has a bug and the
only copy is the projection, fixing it costs a re-fetch of every category — the outcome
append-only storage exists to prevent. It is a few hundred bytes against a 1.2 MB envelope.

`schema_version` lives inside `payload_json` and is mirrored to a column purely so it is
queryable without parsing 1.2 MB — one value, written once. It exists because the "a parser fix
needs no re-fetch" property only holds
while the stored shape is self-describing. When an extraction anchor moves, old rows must be
readable as what they were, not reinterpreted as what the new parser expects.

**Where `categoryAttributes` lives — solved by Spike B** (`RECON.md` §10f). The blocks are the
second argument of a **debug statement left in production**, sitting in the same `<script>` as
`filterInstanceDATA`, immediately after it:

```js
console.log('[ ratings-wrapper ]', { … })    // ~6.9 MB object, ~1.47 MB into the page
```

Brace-match that object and the structure is direct — no searching, no dedupe, no filename key:

| Path | Use |
|---|---|
| **`.cat`** | **This category's own block.** `.cat._id == args.cid` on 6 of 6 categories tested |
| `.productFilterPayload.debug.categories[]` | The siblings, keyed by **`_id`** — the family (§7) |
| `.subcats` | The display groups |
| `.scat` | The supercategory |

An earlier draft had this as "nine blocks, dedupe the repeat, key off `profileImage.fileName`".
All three are unnecessary: `.cat` names the right block outright. Sibling counts vary by family
(8, 8, 7, 4, 2, 1 across the corpus), so the list is read from the payload, never assumed.

> **This is the most fragile thing in the project, and it should be treated that way.** A
> `console.log` is a build artifact: a minifier or a cleanup commit removes it with no other
> change to the page, and nothing else on the page carries `unitName`, `description`,
> `attributeGroup` or `sortOrder`. The `attrs` → `attributeTypeName` fallback chain (§7) is what
> keeps the server answering the day it disappears, so that chain is load-bearing rather than
> defensive decoration, and `attribute_dictionary_missing` must be a warning an operator sees.

### Row selection

**`product_index` stays many-to-many, but its original justification was wrong.** An earlier
draft argued that because `200369` ("31–33 Inch Widths") is both a filter value inside `c37162`
and its own browsable category, the same product appears in two payloads. It does not: that URL
serves the **parent's** payload under `args.cid = 37162` (§5), so there is only ever one payload,
and sibling categories are disjoint — French-Door ∩ Top-Freezer = **0 products** (`RECON.md` §9h).

It stays many-to-many anyway, as cheap insurance rather than a load-bearing claim:
`PRIMARY KEY (product_id, category_id)` costs nothing, and 346 categories have not been checked
for overlap. What changed is that no code should *rely* on multi-membership being common — the
`cr_product` selection rule (§7) is now about picking the freshest scored row, not about
disambiguating copies.

**`scored` is computed at ingest and stored, not re-derived per query.** It is true when the
payload has any non-null `overallDisplayScore` or any non-null `numeric-rating-score` value —
**the same derivation `scores_available` uses** (§10). Two reasons it is a column rather than a
predicate written at each call site: "contains non-null gated values" appears in three selection
rules, and left as prose it would be implemented three different ways; and computing it per query
means re-parsing a 1.2 MB payload to answer "is this row worth serving".

Note the test is on the *data*, not the tier — "member rows have scores" is the one-category
measurement §10 forbids hardcoding, and a member row for a category CR has not rated carries no
scores either.

**A cache hit requires a row at a tier at least as good as the current configuration.** This is
the half of tier-keying that an earlier draft left undone. Keying rows by tier stops an anonymous
row from being *reported* as member data, but the selection rule still read "if none has scores,
serve the newest row of any tier" — so after configuring a cookie, an anonymous row remained a
perfectly valid hit for the rest of its 30-day TTL, and the server would honestly report
`anonymous` for a month while a working membership sat unused. Precisely:

Rows are qualified against the **effective tier**, which is what the caller can currently
*obtain* — not what they have configured:

| `session` (§7) | Effective tier |
|---|---|
| `active`, `unverified` | `member` |
| `none`, `expired` | `anonymous` |

1. A **member** row satisfies any caller.
2. An **anonymous** row satisfies a caller whose effective tier is `anonymous`.
3. Otherwise it is a miss: fetch, then apply never-downgrade on the write.

**The `expired` case is why this keys on the session and not on the configuration.** An earlier
draft read "an anonymous row satisfies a caller only when no cookie is configured", which looks
equivalent and is not: with a *lapsed* cookie configured, no row would ever qualify, so every
single call would miss, fetch 11 MB, and — `category_raw` being append-only — append another
1.2 MB row that also fails to qualify. A dead cookie would have been strictly worse than no
cookie at all, forever. Keying on the effective tier costs exactly one probe fetch per process
to discover the session is dead, after which cached anonymous rows serve normally with
`auth_state: "session_expired"` telling the caller why.

Among the rows that qualify, **serve the newest `scored` row**; if none is scored, the newest
qualifying row. Scored data outranks fresher unscored data, which is what Goal 2 (surviving a
lapsed membership) requires — a member who logs out should not lose the scores they already
fetched. The chosen `final_url` and `fetched_at` identify which row answered, so the result is
traceable rather than arbitrary.

`cr_product(id)` uses the same rule, with the extra clause that the row must still contain the
product (§7).

### `stale` and `superseded_at` are two different facts

An earlier draft used `stale` for both "past its TTL" and "a scored row was retained over a newer
unscored fetch" — which can be true of a perfectly fresh row, so the flag meant nothing on its
own:

- **`stale: true`** — the served row is past `CR_CACHE_TTL_DAYS`. Nothing else.
- **`superseded_at: <timestamp>`** — a newer lower-tier fetch exists and was not served, because
  this row has scores and that one does not. `null` when there is none.

**`refresh=true` appends, it does not promote.** A refresh while anonymous writes a new anonymous
row and the selection rule still prefers the older scored row, now carrying `superseded_at` and
its original `fetched_at`. Refresh means "go look again", never "discard what you have" — the
alternative silently destroys scores on a single anonymous call.

**"Refetch?" is decided from the last CHECK, never from the served row's age.** The two facts
above make a third one necessary. Under never-downgrade the served row is the retained scored
one, and once it passes the TTL it is `stale` on every call for the rest of its life — so a
cache-hit test written as `not sel.stale` fetched 11 MB and appended an unscored row on *every*
call, forever, for exactly the lapsed-membership case the retention rule exists for (reproduced:
three calls, three fetches, four rows). The selection therefore also carries **`checked_at`** —
the `fetched_at` of the newest row the caller *could* have been served, at their effective tier,
whichever row was actually chosen — and the repository refetches only when *that* is past the
TTL. One refetch after the TTL, then the retained scored row is served with `superseded_at` set
and no further network call until the newer row itself ages out. `stale` keeps its one meaning:
the served row is past the TTL. For `cr_product` the last check is on the **category**, whether
or not the newest row still contains the product — a product CR dropped is only ever in old
rows, and must not refetch the category on every call either.

### Retention

Default TTL 30 days; `refresh=true` forces a re-fetch. Ratings move slowly. The A-Z index has
its own 90-day TTL.

**`category_raw` is append-only** on `(category_id, auth_tier, fetched_at)`. This is not
bookkeeping for its own sake — it makes never-downgrade a *query* ("newest scored row") rather
than update logic with an edge case, and it gives Goal 2's dated provenance record for free.

**Pruning never deletes the newest `scored` row for a category.** Pruning purely by age would
delete exactly the row never-downgrade exists to keep: after a membership lapses, the scored row
is the *oldest* one, and an age-based sweep would silently undo the whole rule. Prune by age
within `(category_id, auth_tier)`, always retaining the newest scored row per category.

**`Cache.prune()` runs once per process, at startup, from the server lifespan** — rows older
than 3×TTL go, never the newest scored row per category, and a prune failure is logged and
never keeps the server from serving. An earlier draft deferred the call site ("nothing yet says
how often a sweep should run"), which left the append-only cache with no bound at all.

**Three more bounds, because `refresh` is a read-only tool parameter.** Clients never prompt for
it, so a prompt-injected "call it fifty times with refresh" used to append ~14 MB per call:

- **The refresh cooldown** (§7 *Tool contracts*): a qualifying row younger than 300 s answers a
  `refresh` with `warnings: ["refresh_skipped"]` and no fetch.
- **Identical bytes inside the TTL append nothing.** `write_category` compares the new
  `payload_json` with the newest row at the same tier; if they agree and that row is inside the
  TTL, the INSERT is skipped and that row is served — `from_cache: false`, because the network
  was hit, and `fetched_at` unchanged, because it is when CR first served these bytes. Only
  inside the TTL: a row is also the record of a check (`checked_at` above), so past the TTL the
  same bytes must land as a new row or the selection stays stale and refetches on every call.
  Whether CR's payload is byte-stable between fetches has not been measured; when it is not,
  the rule is inert and the cooldown and the prune still bound the cache.
- **`write_reliability` applies the same rule per sibling id** — the fan-out writes one row per
  category the payload names, and a sibling whose newest row already carries these bytes inside
  the TTL is skipped.

**No normalised per-product table.** The unit of fetch is the *category* — a single product can
never be fetched — so per-item caching buys nothing on the fetch path and would be a second
schema to keep in sync. `product_index` exists only so `cr_product(id)` and `cr_search` can find
which category holds a product; the payload stays the source of truth, which also means a parser
fix needs no re-fetch. An index entry whose payload no longer contains the product is pruned on
read rather than returned as a dangling id (§7).

**The page itself is never stored.** ~11 MB of HTML yields ~1.2 MB of extracted JSON — a **10×
overhead**. That ratio is also why caching is the architecture rather than an optimisation:
every miss costs 11 MB over the wire.

### Auth tier is part of the cache key — non-negotiable

Keying on category alone corrupts data in two directions, both silent:

- Fetch anonymously, then configure a session: for up to 30 days every call serves cached
  nulls while reporting an active member session.
- A session expires mid-TTL: CR serves the anonymous page with a normal `200` and no error
  (§5 — the paywall strips scores server-side rather than refusing), and the refetch
  **overwrites scored rows with null-score rows.**

The second also destroys Goal 2 ("the data survives a lapsed membership") — an unkeyed TTL
actively deletes the thing the cache exists to preserve.

Two rules, both structural:

1. **Key includes the auth tier.** Anonymous and member responses never share a row.
2. **Never downgrade.** A scored row is never overwritten by an unscored one for the same
   product. If a refetch comes back stripped, the scored row is retained and the response
   carries `superseded_at` rather than being replaced — a lapsed membership degrades to "here is
   what CR said on this date", never to nulls.

Rule 2 is what makes the cache a dated record rather than a mirror — "here is what CR said on
this date" is a citable claim in a way that a mirror is not. The cache is the point, not an
optimisation: it keeps request volume near zero.

### Concurrency

`MCPServer` can service overlapping tool calls, so this is not theoretical:

- **One `AsyncSession`, shared.** wafer's `AsyncSession` is safe across tasks; `SyncSession` is
  explicitly not thread-safe. Tools are `async`.
- **One SQLite connection per call**, WAL mode. Connections are not shared across threads.
  **Every multi-statement transaction opens `BEGIN IMMEDIATE`, never a deferred `BEGIN`**: in
  WAL mode a transaction that reads first takes a snapshot, and once another connection has
  committed, its first write fails with SQLITE_BUSY_SNAPSHOT at once — the busy handler is not
  consulted, because waiting cannot make that snapshot current (measured: "database is locked"
  after 0.000 s with `busy_timeout=30000`; the IMMEDIATE form waited 0.23 s and committed).
  Cross-process only — a second MCP host on one `cr.db`, or `consumer-reports-mcp auth` beside
  the server — but `upsert_category_index` runs inside the sitemap pass outside its `try`, and
  `prune` runs at every startup.
- **Single-flight on `(category_id, tier)`.** Two calls for the same uncached category would
  otherwise fetch 11 MB twice, concurrently, against a real membership — the opposite of the
  politeness the rate limiter exists to provide. The second waits on the first and reads its row.
  **The flight is its OWN task; every caller — the first included — awaits it through `shield`,
  and it is cancelled only when its last caller leaves.** So a client cancelling the first call
  (`mcp` 2.1.1 interrupts the tool task on `notifications/cancelled`) does not cancel the
  second: the earlier shape awaited the factory in the first caller's task and stored its
  `CancelledError` on the shared future, which `shield` re-raised in every waiter — a task that
  sees `CancelledError` is marked cancelled whether or not anyone cancelled it, so
  `cr_filters("tvs")` died with `cancelled()` true and `cancelling() == 0`, and a cancelled
  `cr_car` killed the `cr_cars(detail="standard")` listing sharing its `("car", id)` flight
  (its `except (FetchFailed, Challenged)` never sees a `CancelledError`). A lone caller's
  cancellation still stops the download, and a newcomer never joins a dying flight.
- **The politeness interval is the transport's own gate, and wafer's `rate_limit` is 0.** wafer's
  `RateLimiter` is wait → send → record with no lock, reading one shared timestamp: five
  concurrent callers against it at `min_interval=2.0` sent at offsets `[0, 0, 0, 0, 0]`, not
  `0, 2, 4, 6, 8`. The real sequence is the 195-request sitemap pass running while an agent
  issues `cr_ratings` and `cr_reliability` in parallel — N× the configured rate against CR under
  the user's own cookie. The one-session rule below is about *sessions* and never addressed
  concurrency within one. So `Transport` holds an `asyncio.Lock` per host across the **wait
  only** — released before the request goes out, or 60-second downloads would queue behind each
  other — and stamps the last send as it releases, so concurrent callers leave `interval` apart.
  wafer's limiter is disabled rather than doubled, so the two never stack; wafer's own retry
  backoff is separate and unaffected. `CR_MIN_REQUEST_INTERVAL_S=0` disables the gate.
- **Every fetch passes one host allowlist.** URLs come from fetched content — every `<loc>` in
  `products.xml`, `reliabilityURL` in a payload, a cached `final_url` refetched later — and the
  jar seeds `hash` for `Domain=.consumerreports.org`. Without the check, anyone shaping what CR
  serves could point the process at `169.254.169.254`, localhost or an internal host, and any CR
  subdomain would receive the user's 365-day credential; the `<title>` of whatever answered came
  back through `error.title`. `Transport.fetch` therefore requires `https` and a hostname in
  exactly `{www., secure., cars-api.}consumerreports.org` — an exact set, not a suffix — and
  raises `fetch_failed(url_unresolved, retryable: false)` before building a session otherwise.
- **Two server processes can share `cr.db` and `session.json`.** WAL handles the database.
  `session.json` write-back is last-writer-wins, which is benign only because `hash` does not
  rotate (§6) — the only thing that can be lost is a `userLicenses` value that the next request
  re-mints anyway. Worth stating, because it stops being benign the moment anything durable is
  written there.

## 9. Stack

- Python ≥3.12, `uv`, per house convention.
- **`wafer-py>=0.4.9,<0.6`** for **all** HTTP — TLS fingerprinting, challenge detection, cookie
  handling and rate limiting. Every API this project uses — `add_cookie`, `get_cookie`,
  `cookie_scope_summary`, `max_response_size`, `attempt_timeout`, `resp.history`,
  `resp.challenge_type`, `resp.rotations`, `RequestBlocked` and the rest of the exception
  hierarchy — is verified present at both `v0.4.9` and the current `0.5.0`, which is what the pin
  actually resolves to, and the test suite passes against both released versions. **The ceiling
  is deliberate.** What §6 and §8 encode about wafer is behaviour, not signatures — a rotation
  rebuilds with an empty jar and does not raise, `max_rotations`/`max_failures` are
  constructor-only, a JSON-bodied 403 is *returned* under both policies, `__aexit__` is the only
  teardown, the limiter is lockless — all measured on 0.4.9/0.5.0. wafer is pre-1.0 with no
  changelog and its minors are where the surface has moved (0.3.0, 0.4.2 and 0.5.0 each added
  something this project depends on), and a PyPI install carries no lockfile, so an open-ended
  pin would let a 0.6 change those rules silently. `tests/test_transport.py` pins the range to
  `pyproject.toml`, `CLAUDE.md` and this paragraph, and the installed version to the range;
  widening it means re-measuring first.

  **Two things follow from 0.5.0 being live.** The AIA certificate-chain feature it adds calls
  `_rebuild_client`, which empties the cookie jar — so the `identity_rotated` path in §6 is
  reachable in production rather than theoretical. And **`llms.txt` documents a working checkout,
  not a release**, so any *further* wafer API this project adopts must be checked against the
  published version rather than the local tree. The page GETs are plain, but they are large, repeated and behind a WAF, which
  is exactly what wafer is for. One shared **`AsyncSession`** (§8 concurrency), constructed with
  `max_response_size` set, and **`max_rotations=0, max_failures=None` whenever a cookie is
  configured** (§6 — the policy is fixed at construction; wafer has no per-request form). Always pair `timeout=` with `attempt_timeout=`: `timeout` is a total budget across
  retries and rotations, so unpaired, one hung request eats the whole budget and retries never
  fire.

  **One documented exception: validating a freshly captured cookie.** `auth_tools.validate_cookies`
  builds a second, short-lived session for a *single* probe request, then closes it — the same
  thing `auth` has always done from the CLI, now also reachable from `cr_sign_in` while the main
  session is live. The rule exists so two long-lived sessions cannot each apply `rate_limit` and
  double the sustained request rate; one probe on a session that is released immediately does not
  do that. The alternative — adopt the cookie first, probe through the main session, then revert
  on failure — would put a rollback path inside `Transport.adopt()` and would still need a
  separate branch for the CLI, which has no main session to borrow. Validating before adopting is
  also the safer order: nothing is stored or adopted until a real member marker is seen.
- **`mcp>=2,<3`** for the server — **`MCPServer`, imported from `mcp.server.mcpserver`**, with
  `outputSchema` declared on every tool (§7). Earlier drafts of this spec said "FastMCP"; that
  class was renamed in the 2.x SDK and `mcp.server.fastmcp` **no longer exists** (verified against
  `mcp==2.1.1`, the current release), so the old name is an import error rather than a style
  choice. Pinning `mcp<2` to keep it is a one-line change if 2.x proves troublesome, but a
  greenfield public project should not start on a removed import. Logs go to **stderr** — stdout
  is the protocol channel, and no response body is ever logged (§6).
- **Playwright as an optional `[browser]` extra only** — `auth --browser` and `cr_sign_in` (§6).
  Not a core dependency, never on the data path, and not a fallback for a blocked fetch. No
  `rookiepy` — rejected on trust grounds (§6), and neither path reads the user's profile. The
  Desktop bundle below installs the extra, so `cr_sign_in` works there out of the box.

### Entry points and installation

**One console script, with the auth capture as a subcommand** — the capture is a terminal gesture
and the server is not, but a second entry point would be a second thing to install and to name.
`pyproject.toml` declares `consumer-reports-mcp`; bare, it serves:

| Command | Does |
|---|---|
| `consumer-reports-mcp` | Serves MCP over **stdio**. The MCP client's command. |
| `consumer-reports-mcp auth [--browser\|--status\|--forget\|--paste-file PATH\|--timeout N]` | The session capture and its inspection (§6). `--paste-file` reads the paste from a file instead of stdin; `--timeout` bounds the `--browser` wait (default 300 s). |

**Published on PyPI as `consumer-reports-mcp`** — first release 0.2.4 on 2026-09-06, through the
trusted-publishing workflow in `.github/workflows/release.yml`. The `[browser]` extra resolves
from the index (verified: `uvx --from "consumer-reports-mcp[browser]==0.2.4" consumer-reports-mcp
--help` in a clean home installs Playwright 1.62.0 and serves). Publication was deferred until the
server had been built and used in anger; nothing in the design depended on which way it went, and
the clone-based install block collapsed to one line with no other change:

```bash
claude mcp add consumer-reports -- uvx consumer-reports-mcp
# with the in-conversation sign-in (Playwright), the extra rides on the package spec:
claude mcp add consumer-reports -- uvx --from "consumer-reports-mcp[browser]" consumer-reports-mcp
uvx consumer-reports-mcp auth              # capture a session (optional — anonymous works)
```

Development runs from a clone. **The registration command takes the path from the shell, never
from this document** — an earlier draft hardcoded one machine's home directory:

```bash
git clone https://github.com/Averyy/consumer-reports-mcp && cd consumer-reports-mcp && uv sync
claude mcp add consumer-reports -- uv --directory "$PWD" run consumer-reports-mcp
```

**Two workflows, both green on Linux, macOS and Windows runners (2026-09-06).**
`.github/workflows/ci.yml` runs on every push: `ruff check`, `ruff format --check`, the offline
suite, the bundle build, and — in a second job on a real installed Chrome, no CR traffic — the
browser-teardown measurements §6 relies on (`tests/live/test_browser_hardware.py`, headed under
Xvfb on Linux). Three platforms because two claims in §6 can only be measured on the OS they are
about, and the first run found two of them wrong (the Linux `ps` truncation and the Windows
`None`). `.github/workflows/release.yml` publishes on a `vX.Y.Z` tag through **trusted
publishing** — GitHub mints a short-lived OIDC token and PyPI verifies it, so no API token is
stored anywhere; the job with `id-token: write` is separate from the one that builds, and the
build refuses a tag that disagrees with `pyproject.toml` (PyPI files are immutable, so a wrong
version can only be yanked), builds with `--no-sources`, and runs `scripts/check_artifacts.py`
so an sdist or wheel carrying a capture or a credential never reaches the index.

### Claude Desktop bundle (`.mcpb`)

Desktop users install a bundle, not a clone. `bundle/` holds the template and
`scripts/build_bundle.py` produces `dist/consumer-reports-mcp-<version>.mcpb`. The recipe, and
the reason for each line of it:

- **`server.type: "uv"`, manifest 0.4.** Desktop downloads its own `uv` (pinned 0.9.7), requires
  a `pyproject.toml` in the bundle, runs plain `uv sync` there, and starts `mcp_config`:
  `uv run --directory ${__dirname} consumer-reports-mcp`. Nothing else is installed on the
  machine and no Python needs to exist beforehand.
- **`uv sync` does not install the bundle project's own extras, but honours extras named in a
  dependency specification.** So the bundle's `pyproject.toml` depends on
  `consumer-reports-mcp[browser]` and declares no extras of its own: Desktop users get Playwright
  and `cr_sign_in` works; PyPI users installing the server itself stay lean.
- **The server is a pinned PyPI dependency, and the pin is generated.** `bundle/pyproject.toml`
  depends on `consumer-reports-mcp[browser]==0`; the `==0` is a placeholder like the wrapper's
  `version = "0"`, and `scripts/build_bundle.py` stamps the root version over both — refusing a
  wrapper that lacks either placeholder — so a bundle always installs the release it was built
  for. Desktop's `uv sync` resolves the pin from PyPI at install time and the build has no
  network, so the bundle is built from the released tag *after* the publish: built before it,
  it packs fine and fails at the user's `uv sync`. Before publication the build vendored the
  source tree and `[tool.uv.sources]` pointed uv at it; that path is gone, and the test suite
  forbids a `vendor/` entry in the artifact so it cannot come back unnoticed.
- **The manifest's `version` is generated from `pyproject.toml`, never typed.** The template
  carries no version — a template with one is refused — and the `tools` list is generated from
  `server.DESCRIPTIONS` for the same reason. This is the `__init__.py` version-drift bug, kept
  fixed. The wrapper project's own version is stamped the same way.
- **`user_config.session_cookie`** (`sensitive`, not required) maps to `CR_SESSION_COOKIE`, for
  the machine where no browser window can open. Left blank it is not an override (`load()`'s
  non-blank rule), and neither is the manifest's own `${user_config.session_cookie}` template
  arriving unexpanded for a blank field (`credentials.env_value_set`) — otherwise `cr_sign_in`
  would refuse `env_override` on every start with nothing to clear; filled in, it wins over the
  stored file and `cr_sign_in` refuses and says so.
- **`compatibility.platforms: ["darwin", "win32", "linux"]`** — Claude Desktop's Linux beta
  shipped 2026-06-30 (Ubuntu 22.04+ / Debian 12+, x64 and arm64, via apt), and the `uv` server
  type is cross-platform; nothing in the bundle names a platform (`uv run --directory
  ${__dirname} consumer-reports-mcp`, and the browser ladder ends in the Linux executables). The
  server and its bundle build run on a Linux runner in CI; an `.mcpb` install into the Linux
  Desktop itself has not been performed.
- **`src/server.py`** is a four-line shim that satisfies the `uv` type's `entry_point` and calls
  `cli.main()`; the console script in `mcp_config` is what actually runs.

No network is needed to build, and the test suite checks the generated manifest against
`pyproject.toml` without unpacking anything onto the machine.

### Config surface

| Variable | Default | Purpose |
|---|---|---|
| `CR_SESSION_COOKIE` | unset | Session cookie; takes precedence over the stored file (§6) |
| `CR_CACHE_DIR` | `~/.cache/consumer-reports-mcp` | Cache location |
| `CR_CACHE_TTL_DAYS` | `30` | Payload TTL — category pages *and* per-car ratings |
| `CR_INDEX_TTL_DAYS` | `90` | Index TTL — the A-Z index, the sitemap pass, the cars index and the car taxonomy. All change rarely |
| `CR_MIN_REQUEST_INTERVAL_S` | `2.0` | Minimum seconds between requests to a CR host, enforced by the transport's own per-host gate (§8 *Concurrency*); wafer's `rate_limit` is passed `0` so the two never stack. `0` disables |
| `CR_TIMEOUT_S` | `120` | Total budget for one fetch, across every retry |
| `CR_ATTEMPT_TIMEOUT_S` | `60` | Per-attempt cap |
| `CR_OFFLINE` | unset | `1`: never touch the network — every fetch is `fetch_failed(connection_failed)` and the cache serves what it has |

**The timeout pair is sized for an 11 MB body, and that is the whole point of stating numbers.**
An `attempt_timeout` chosen for a normal API call kills a category download mid-body, and wafer
treats that as a retryable failure — so it replays the *whole* 11 MB request up to `max_retries`
times, turning one slow fetch into four. That is precisely the impoliteness the rate limiter
exists to prevent, arriving through the timeout setting instead. Spike A measured an 11 MB fetch
at ~2 s (`RECON.md` §10d), so these defaults have ample headroom.

**The rate-limit variable is seconds-between-requests, not requests-per-second.** An earlier
draft called it `CR_RATE_LIMIT_RPS` with a default of `0.5`, intending one request every two
seconds. wafer's `rate_limit` is "min seconds between requests to same hostname", so `0.5` passed
through unchanged would have meant **two requests per second — four times more aggressive than
intended**, against a real membership, in the one setting whose entire purpose is politeness. The
name now matches the unit so the value cannot be misread again. The value no longer reaches
wafer at all: the interval is enforced by the transport's own gate, which — unlike wafer's
limiter — holds across concurrent callers (§8 *Concurrency*).

## 10. Build order

**Everything in this section has shipped** (first PyPI release 2026-09-06, §9). It is kept as
the record of the order and the reasoning behind it, which is why it still reads as a plan.

Recon on **CR** was done first (`RECON.md`). Recon on **our client** was not, so three spikes
came before any module. An earlier draft said "everything ships together, nothing is deferred";
that was true of the tool surface and false of the unknowns, and it would have had the auth
module written against inferred wafer behaviour.

### Spikes — run 2026-09-03

**All three are complete**, including Spike A's member steps.
Results in `RECON.md` §10, scripts in the gitignored `scratch/spikes/`. What they changed is
already folded into §5–§9 above; what is still unmeasured is in `RECON.md` §15.

#### Spike A — the wafer auth path — DONE

**Complete, including the member steps** (`RECON.md` §10a–§10d, §10i–§10j).

**Without a membership:** Cookie scoping (`Domain=.consumerreports.org` reaches
`secure.`, host-only does not); injected cookies are genuinely transmitted; an 11 MB fetch takes
~2 s so the timeout pair has headroom; and — the finding that changed the design — **CR rejects an
invalid `hash` with a redirect to `/ec/login`, no payload and no marker** (`RECON.md` §10b), which
rewrote the detection algorithm in §7.

**Done, against a live member session** (`RECON.md` §10i–§10j). `hash` alone re-mints a full
member session through wafer — 8/8 scores, `userLicenses` rotated — so §6's design holds end to
end on our own client. `hash` plus a *stale* `userLicenses` also authenticates, which closes the
open question and makes storing both cookies safe. The member-vs-anonymous diff is clean: `args`
is identical and no credential or profile string appears in anything written to the cache.

Two traps came out of it, both now in §7: the login host appears in the redirect chain of a
*successful* re-mint, so only the final URL distinguishes rejection; and `categoryAttributes`
arrives in a different array order per tier, which is what makes `sortOrder` selection mandatory.

The original brief, kept for reproduction:

**Spike A — the wafer auth path.** Half a day, and it blocks the whole auth module (§6).
Construct an `AsyncSession`, inject `hash` as `Domain=.consumerreports.org; Path=/; Secure`, GET
Robotic Vacuums (`c35183`, 8 products), assert `data-subscriber="true"`, read `resp.history` for
the re-mint redirect chain and `session.get_cookie` for the new `userLicenses`. Then **force a
rotation** and confirm the credential vanishes from the jar — that failure mode is the reason for
`max_rotations=0` and the `get_cookie` assertion, and it should be observed rather than trusted.
Then test **`hash` + a stale `userLicenses`**, the cold-start state of `session.json` days after
a paste, which `RECON.md` §5 never covered: if CR rejects a stale token instead of re-minting,
keeping both cookies is worse than keeping `hash` alone and step 1 of the `auth` command changes.
This is also the first member-session traffic wafer has ever sent CR — all volume testing was
anonymous (`RECON.md` §11).

Four more things ride along, because the session is already warm:

- **Diff the member page against the anonymous one beyond the product keys** — the `args` block
  and the surrounding markup — before a single byte is written to `cr.db` (§6). Include a
  reliability page: `reliability_raw` is labelled anonymous-only, but a cookie-configured process
  fetches it with `hash` in the jar, so "the data does not vary by session" needs checking rather
  than assuming.
- **Time the 11 MB fetch** on a warm and a cold connection, so the timeout values in §9 are
  measured rather than guessed.
- **Force the rotation with a throwaway session**, not the one under test — rotation policy is
  fixed at construction, so observing it needs a second session built with defaults.

#### Spike B — extraction anchors — DONE

`filterInstanceDATA` brace-matching works 6/6 with a `;\n` terminator; `categoryAttributes` is
reached via `.cat` inside a `console.log('[ ratings-wrapper ]', …)` debug object; the A-Z index
carries no `data-subscriber`; `subcats` can list a group with no products. Full results in
`RECON.md` §10e–§10g; §8 now specifies the anchor.

#### Spike C — response sizing — DONE

Nested default of 10 confirmed at ~3,380 tokens; the old flat figure was 50% low; `full` capped at
10; and `cr_filters` cut from 36,007 tokens to 3,502 by collapsing numeric value lists to ranges.
`RECON.md` §10h; §7 carries the decisions.

### Then, in dependency order

**Foundation**
- Project skeleton: `pyproject.toml`, `uv` setup, `LICENSE`, `.gitignore` (exclude
  `.playwright-mcp/`), git init.
- wafer session module — one `AsyncSession`, rotation policy fixed at construction from cookie
  presence (§6), cookie jar seeded and flushed, `timeout` always paired with `attempt_timeout`.
- SQLite cache per §8: append-only `category_raw` with `scored`, many-to-many `product_index`,
  `category_index`, `reliability_raw`; effective-tier row selection and single-flight.
- **Mixed-tier cache fixtures and the auth-state matrix land here**, not in a later test phase —
  the tier rules are the storage module's actual contract, and a rule this easy to get subtly
  wrong (two of the three cache bugs found in review were in exactly this logic) should not be
  written untested.
- **Discovery, two passes** (§5). A-Z index ingest first — one fetch, and the only source of
  display names; **the anchor text is wrapped in a nested `<span>`** and hrefs mix absolute and
  root-relative, so strip inner tags within each `<a>` rather than matching `>([^<]+)</a>`, which
  finds 2 of 236 (`RECON.md` §9h). Then the **sitemap pass** — `/sitemaps/products.xml` and its
  195 per-supercategory XML files — which is what makes coverage correct; the A-Z index omits
  Televisions, Mattresses and Tablets (`RECON.md` §12a). Record `source` on every row.

**Parsing**
- `filterInstanceDATA` extraction, `auth_state` detection via `data-subscriber`, attribute
  normalization including the undefined-entry fallback (§7).
- The `initStore` parser for reliability pages — a second, isolated code path (§7).
- `scores_available` **derived from the payload**, never hardcoded — see below.
- **Parser fixtures land here**, covering both envelopes, the null-score contract and the
  three-state derivation. Same reasoning as above: this is the code the project's core promise
  rests on.

**Auth** — `consumer-reports-mcp auth` (paste) and `--browser` (P9.4): accept a bare `hash`, a
cookie string or a cURL paste, keep `hash` + `userLicenses`,
validate against a live fetch, write `0600`. It comes *after* Parsing, not with Foundation:
validation runs the same `data-subscriber` detection the server uses, so it depends on it.

**Tools** — `cr_ratings` flat, then nested, then the local filter engine; `cr_filters`;
`cr_product`; `cr_categories` and `cr_search` on the index; `cr_reliability` next, since it is
the isolated second envelope and nothing depends on it.

**Cars** — last, and deliberately so: a second id space, a second cache table and a per-row
request cost (§5). Order: `v1/cr/keys` ingest into `car_index`; `cr_car_search`; `cr_car` (single
model-year); `cr_cars` (listing on `v2/cr/cars`), then `detail="standard"` and its per-row ratings
fetch.
**The request-cost behaviour gets tests before the tools ship**: `summary` must issue exactly one
request, and `standard` exactly one per returned row — the N+1 is the only real hazard on this
surface. **Each tool ships with its description
and `outputSchema` in the same commit** — the SDK declares the schema at tool definition, so
deferring it to a later pass means writing every tool twice.

**Sizing pass** — re-measure every response shape against Spike C's numbers and set the final
`limit` defaults.

All fixtures are captured from anonymous fetches only, never contain member scores, and are
content-minimal (§12, CLAUDE.md).

**Done when** an agent with no prior knowledge of CR can, through these tools alone, find a
category, shortlist within it, drill into a model, and cite the result with a URL and a date —
anonymously, and again with a session.

### Derive availability, do not hardcode it

`RECON.md` §2 and §9f establish the paywall boundary across **five franchises** — appliances,
health, money, babies-kids, home-garden: `overallDisplayScore` and `numeric-rating-score` values
gated, everything else public. Only electronics-computers lacks a paired diff.

**The wider sample strengthens the case for deriving rather than weakening it.** The same
measurements showed owner satisfaction and predicted reliability are absent **for members** in
four of those five franchises — not paywalled, simply not collected for those product types. Any
hardcoded "owner_satisfaction is available" would be wrong for most of the catalogue.

The parser must therefore compute `scores_available` **by observing the payload it actually
has**, rather than encoding that finding as a constant.

**The rule, precisely** — ambiguity here produces two different implementations:

> A field is `available` if **any** product in the payload has a non-null value for it.
> Evaluated over the **whole category payload**, never the returned slice, so `limit` and
> the filter parameters cannot change what the envelope claims about CR's data.

`any` rather than `all` because partial population is normal and not a paywall: owner
satisfaction is present on 158 of 172 products even for a member (`RECON.md` §2). Under `all`,
14 unrated products would flip the whole category to "unavailable" and hide 158 real scores.
Per-product absence is already carried by that product's own `null`.

Two reasons for deriving at all:

1. It makes the sample size irrelevant. A category that gates a different set is described
   correctly with no code change, and no amount of extra sampling would ever fully guarantee
   what a hardcoded list assumes.
2. It fails in the safe direction. A hardcoded list that is wrong reports scores as available
   when they are null — the exact misreading §7 exists to prevent.

The measured boundary stays in `RECON.md` as documentation of what CR does today. It is not an
input to the code.

## 11. Risks

| Risk | Mitigation |
|---|---|
| Page payload moves or is renamed on a site release | `payload_missing` is a loud error, never an empty result (§7). Raw JSON is cached, so a parser fix needs no re-fetch |
| Session expires silently — CR serves the anonymous page with a `200` | Positive logged-in marker for detection and `session_expired` as a distinct state (§6, §7). `--status` reports an upper bound on the life left and warns inside 30 days — see §6 |
| Sustained member-session traffic looks different to a WAF than the recon did | Still untested at volume; Spike A sent ~a dozen member requests without incident (`RECON.md` §10i). Cache-first keeps volume near zero; rate limiter on by default |
| **A wafer identity rotation silently drops the member cookie and CR answers anonymously** | The named worst failure, arriving from the WAF side. the session is constructed `max_rotations=0` whenever a cookie is configured, and `hash` is asserted present in the jar before any `session_expired` classification — otherwise `fetch_failed(identity_rotated)`, uncached (§6) |
| A member-identifying field rides along in a cached payload | **Measured clean** — Spike A6 diffed `args` and the stored slices member-vs-anonymous: identical, no credential or profile string (`RECON.md` §10i) |
| WAF objects to a non-browser client | wafer's fingerprinting is the first answer. Playwright is **not** the fallback — §13 keeps it off the data path; the fallback is to back off and surface `challenged` (§7) |
| Account flagged for automated traffic | Cache-first, category-at-a-time, wafer rate limiter on by default |
| Schema drift breaks parsers | Cache raw JSON alongside parsed records, so a parser fix needs no re-fetch |
| Membership lapses | Cache retains everything already fetched |
| CR ships its own MCP server and this becomes redundant | Accepted. Theirs is commercial pay-per-rating, not member access, and a year unreleased |

## 12. Legal, trust and safety

This is written as a public repository that reads a paid subscription's gated content using a
credential the user pastes in. None of that is unusual for a personal tool, and all of it is worth
stating plainly rather than leaving implicit. (The repository is **public since 2026-09-06**.
A flip exposes every commit, so the rules in this section had to hold for the whole history, not
just the tree — they did not: 20 commits still carried the scores `2da9a38` redacted. The history
was squashed to one commit before the flip rather than force-pushed over, since unreachable
objects stay fetchable by SHA.)

**Affiliation.** Not affiliated with, endorsed by, or connected to Consumer Reports. "Consumer
Reports" is a trademark of Consumer Reports, Inc., used here only to name the service the tool
reads. The README carries this too.

**Terms of use are the user's to accept.** CR's Terms restrict automated access, as most
publishers' do. This server is the least objectionable shape that request can take — local,
personal, cache-first, one category at a time, rate-limited, reading only what the user's own
session is already entitled to — but it is the *user's* account and the user's agreement. The
README says so, links CR's **User Agreement** (the document actually exists under that name;
`/terms-of-use/` is a 404), and does not pretend the question does not exist. Nothing here
is offered as legal advice.

**Fixtures are content-minimal, not just score-free.** `CLAUDE.md` forbids committing member-only
scores; that is necessary and not sufficient. A committed anonymous `filterInstanceDATA` is still
1.1 MB of CR's compiled catalogue — model names, prices, specs, Recommended flags, owner
satisfaction. Test fixtures are **structurally faithful and content-minimal**: a handful of
products with synthetic brand and model strings, real attribute *definitions* (the join needs
them), real filter *shapes*, real `_groupName` values. Full payloads stay local and gitignored.

**The stored session is a year-long credential.** Anyone who can read `session.json` has the
user's membership, with no password and no second factor. Consequences, all in the README:
`0600` is enforced where the platform supports it and **means nothing on Windows**, which matters
because §6 promises this works on any platform; a shared machine is a bad place for it;
`auth --forget` removes it (§6); and the env-var path exists partly so a container never writes
it to disk at all.

**Cars are served in full, and that needs stating plainly rather than hedging.** CR's cars API
requires no authentication and CR publishes the api-key in its own page source, so what it returns
is what CR serves to anyone who asks (§5, `RECON.md` §11d). Nothing is circumvented, because there
is nothing in the way. An earlier draft had the server withhold those scores anyway on an
inferred-intent argument; that was withdrawn, because it invented a restriction CR does not impose
and returned less than a plain HTTP request would.

**No response body ever leaves the process** — not to tool output, not to a log, not into the
cache as debugging residue. §6 carries the rule and the reasons.

**`RECON.md` §5 has been reduced to its conclusions — DONE 2026-09-03.** It previously named the
cookies, identified the single 36-byte value that restores a year of access over plain HTTP, and
tabled the ablation that proved it. That detail was load-bearing while the auth design was being
settled; it is not load-bearing now that the design is fixed, and in a public repo it is a
description of CR's session mechanics for no working benefit.

What ships: `hash` is the durable credential, it re-mints over plain HTTP, the client must
persist rotations, `data-subscriber` is the marker, and "Sign Out" must never be used for
detection. Every rule the design depends on, with enough reasoning to check it.

What does not: the cookie-by-cookie ablation table, the per-cookie property table (bytes,
HttpOnly, expiry) and the re-mint transcript. Those moved to **`notes/auth-recon.md`**, which
`.gitignore` excludes. Nothing anywhere — public or local — has ever contained a cookie *value*.

## 13. Decisions taken

- **Name: `consumer-reports-mcp`**, matching the directory on disk. Free on both PyPI and npm
  when chosen; registered on PyPI by this project on 2026-09-06 (§9).
  `cr-mcp` was taken on npm by an unrelated *code review* MCP, and "CR" reads as code-review
  in MCP circles. Earlier drafts said `consumerreports-mcp` — the hyphenated form wins, and
  package name, cache dir, config dir and both §9 entry points all use it.
- **Auth is cookie-only in v1 (§6).** The user logs in normally and hands over a session token —
  a 36-character console one-liner, a cURL paste, or `auth --browser`, which drives a real browser
  so CR's own login page collects the password. The server never asks for a password, never
  automates login and never solves a CAPTCHA.
  Chosen because every other option — Playwright, keyring, browser import — reduces to
  producing the same cookie, so this path must exist regardless, and building it first makes
  the others purely additive.
- **`rookiepy` browser import dropped.** Tested against a real macOS profile 2026-09-01:
  Chrome `decrypt_encrypted_value failed`, Safari crashes in its binary-cookie parser. Not a
  capability problem — §6 retracts that test, since the failure is consistent with a declined
  Keychain prompt. It is dropped on trust grounds:
  reading a whole browser profile and keychain to obtain cookies the user can paste is a large
  ask for a small convenience.
- **Browser-assisted login is IN SCOPE for v1** as `auth --browser`, an optional `[browser]`
  extra (§6). Earlier drafts deferred it; it is promoted because the goal is *easier auth*, not a
  smaller dependency list, and the paste — however short — still routes a non-developer through
  DevTools. The user types their own password into CR's own page; the server harvests only the
  cookie. `channel="chrome"` drives the installed browser rather than downloading one, a fresh
  throwaway context is used rather than the user's profile, `setAutoLogin` is pre-checked so the
  durable `hash` is minted, and it never touches the data path, which stays wafer.
- **Taking a username and password directly is rejected outright**, not deferred (§6). hCaptcha is
  on the form, a password is account takeover rather than read access, an unattended server cannot
  re-prompt, and automated login POSTs are what get accounts locked.
- **The in-conversation sign-in (`cr_sign_in` / `cr_auth_status`) is the primary onboarding, and
  it is non-blocking by construction** (§6 *cr_sign_in*). Claude Desktop kills a tool call at 60 s
  and offers no elicitation to a local server, so the tool starts a background capture and a
  status tool polls it; the captured cookie is adopted live by `Transport.adopt()` rather than on
  the next restart. A request that straddles an adopt is discarded as `policy_changed`, never
  classified under the new credential — and retried once by the caller that owns the tool call,
  so the server's own state change never surfaces as a tool error. **The guard verifies an
  `unverified` stored cookie against CR before deciding**; it refuses only a cookie verified live
  in this process, so a dead cookie renews without `force` (an earlier draft refused blind and
  broke renewal — the case the feature exists for). Both auth tools wear the ordinary outer
  envelope (§7). Ships in a `.mcpb` bundle whose manifest version is generated, never typed (§9).
- **Tool surface: eleven tools** — six products, three cars, two auth (§7). `cr_ratings` returns a
  `detail`-controlled summary with
  `cr_product` for the drilldown; no AskCR passthrough. Signed off at five, with
  **`cr_reliability` added after recon showed the reliability pages carry brand-ranked survey
  data that is both anonymous and not derivable from the category payload** (§7). It is the
  only tool on the second envelope, deliberately isolated.
- **No AskCR passthrough.** The structured data is good enough that a prose-answer tool
  earns nothing. Nine structured data tools, no chatbot proxy.
- **`cr_compare` dropped.** With the whole category cached locally it was `cr_product`
  called N times wearing a costume. `cr_filters` added in its place — the taxonomy is
  per-category and not guessable.
- **Public and open source.** Auth must work for any user on any platform — no macOS,
  Keychain or single-browser assumptions (§6).
- **All of CR, cars included.** 346 product categories across 7 franchise values share one envelope;
  cars are a second architecture on their own JSON API, covered by their own tools (§5, §7).
  An earlier draft scoped cars out — that was a scoping decision, not a technical limit, and it is
  reversed. The cars API is in several respects easier than the products surface — it has one
  tier, so the cars tools carry no auth machinery at all (§5). Its only real constraint is request
  cost: road-test scores are one request per car, which `cr_cars`' `detail` parameter makes the
  caller's visible choice.
- **General-purpose, and it writes nothing outside its own cache.** The server exposes tools;
  what a caller does with the results is not its concern, and no caller-specific convention
  appears anywhere in this spec.

## 14. Open questions

1. ~~Is the Swagger descriptor reachable?~~ **Moot.** The category page carries the whole
   dataset; no API schema needed.
2. ~~Web API or mobile app API?~~ **Neither.** The category page is the surface (§5).
3. ~~Does the API gate ratings on member auth?~~ **Answered: yes, server-side, and partially.**
   An anonymous fetch returns the catalogue with `overallDisplayScore` and the
   `numeric-rating-score` values stripped — and *only* those. Owner satisfaction, predicted
   reliability, CR Recommended, prices, retailer spread and specs all come back populated
   (`RECON.md` §2). An earlier phrasing here said "every score stripped", which is the
   misreading `scores_available` exists to prevent, restated as a closed question.
4. ~~Tool surface sign-off.~~ **Closed** — see §13.
5. ~~Does an authenticated fetch return the same envelope, and what marks the page as logged
   in?~~ **Answered 2026-09-01.** Identical envelope, values populated; marker is
   `data-subscriber` (§5, §6, §7). This closed every unknown about **CR's** behaviour; the
   remaining unknowns are about **our client's**, and are the three spikes in §10.
6. ~~Is hCaptcha enforced on every login POST?~~ **Permanently moot** — no login is ever
   automated. Recorded in §6 for the record only.
7. ~~How long does a session last?~~ **Answered, with a caveat.** `hash` from a "remember me"
   login is the durable credential (365 days) and alone restores full member access over plain
   HTTP, re-minting the short-lived `userLicenses`; `userToken`'s 1-day expiry is irrelevant
   (§6, `RECON.md` §5). The caveat: a capture of 2026-09-05 died within a day, and the version
   that captured it had discarded the cookie's expiry, so whether it was ever a 365-day cookie
   is unknowable. The browser sign-in now records the expiry and refuses a session-only one.
8. ~~Can the Overall Score be derived from anonymous fields?~~ **No — tested and rejected**
   (§5). Max error 13.1 points on a 36-point range.
9. **Does a captured session actually survive its expected ~year in practice?** Empirical, not
   structural: the renewal mechanism is confirmed, but only elapsed time proves CR does not
   invalidate sessions for other reasons. Answered by using the server and noting when a rejection
   first appears. **No longer gates anything** — the browser extra was previously deferred behind
   this question and is now in scope on its own merits (§6, §13).
10. Do CR Digital Archive back issues download as PDFs? If so they are a downstream archiving
    concern, not a tool surface. Unrelated to v1.
