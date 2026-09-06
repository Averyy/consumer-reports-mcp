# consumer-reports-mcp

MCP server exposing a Consumer Reports member's own subscription as structured ratings tools.
Open source (MIT), Python 3.12+. **Intended to be public; the GitHub repository is PRIVATE as
of 2026-09-06** (`gh repo view` → `visibility: PRIVATE`), so every
`github.com/Averyy/consumer-reports-mcp` URL in the README, `pyproject.toml` and the bundle
manifest 404s for anyone but the owner until it is flipped. Flipping it exposes the full history
and is the owner's decision alone — never run `gh repo edit --visibility` from an agent.

- `SPEC.md` — design of record. Read before proposing anything structural.
- `RECON.md` — measured facts about CR's payload, paywall and auth. Cite it; don't re-derive it.
- `MCP-AUTH-PATTERN.md` — the in-conversation sign-in as a reusable pattern (stands alone).

## Architecture

**The category page is the data source. There is no API to reverse-engineer.** A plain GET of
a category URL embeds `window.filterInstanceDATA` — the entire category as JSON. One request
per category, no api-key, no pagination.

- **IMPORTANT: the A-Z index is INCOMPLETE — it is not the catalogue.** Measured: **346
  categories exist, the index lists 236, and 110 are missing from it** (0 the other way, so the
  sitemap union is a strict superset). Omissions include **TVs `c28700` (303 products)**,
  **Mattresses `c28705` (293)**, Bluetooth Speakers (178), Humidifiers (121), High Chairs,
  Homeowners insurance. Discovery is TWO passes: the A-Z index
  (`/cro/a-to-z-index/products/index.htm`, one fetch, the only source of display names) **plus**
  the sitemaps — `/sitemaps/products.xml` then its 195 `/products/sitemap/{scid}` XML files.
  Record `source` per row; `unknown_category` is only correct once both have run.
- **A sitemap id can be stale** (1 of 10 sampled 404d). Treat a 404 on a sitemap-sourced id as a
  retired category — mark it dead (`dead_at`), never raise `payload_missing`. The tool answer
  is `unknown_category` with `reason: "retired"` vs `"not_in_index"`, plus a third value,
  **`"discovery_incomplete"`**, when only ONE discovery source has run (the sitemap pass is
  pending, aborted or failed) — "not found yet" is structurally distinct from "not found".
  Only a sitemap-sourced id that 404s is retired; an A-Z id that 404s is `payload_missing`.
- **The sitemap pass runs in the BACKGROUND at startup** (195 fetches ≈ 6.5 min). Fetch the A-Z
  index synchronously first; a lookup that misses it **awaits** the pass rather than answering
  `unknown_category`, and warns `sitemap_pass_pending` meanwhile. **A pass that ends UNRECORDED
  (offline at startup, `products.xml` failing twice, three consecutive challenges) is retried by
  the next miss** — after a `REFRESH_COOLDOWN_S` (300 s) cooldown, only once the A-Z source is
  authoritative, and only as a RETRY (the lifespan launches; a miss awaits or retries, so a miss
  with no pass ever launched is a request-free `discovery_incomplete`) — and by
  `cr_categories(refresh=True)`. Returning the finished task object forever made
  `discovery_incomplete` last until restart, however long ago the network had come back.
- **`cr_cars` `state` DEFAULTS TO BOTH New and Used.** 6,698 of 7,095 model-years are Used — a
  New-only default silently discards 94% of the catalogue and the results still look plausible.
- **`cr_search` resolves categories via CR's typeahead first** (`/api/search/typeahead?query=`,
  param is `query`, min 3 chars, no auth), falling back to the local index. It returns 0 results
  for a model number — there is no cross-category product search behind it.
- **Merge `modelAvailabilityName` only when a reliability row is already cached**; never fetch to
  populate it. Absent → `availability: null` + `availability_not_cached` warning.
- **`cr_search` MUST match slugs, not just display names.** The 110 sitemap-only categories have
  no display name (sitemaps carry URLs), so a name-only search cannot find Televisions.
- **6 of the 346 sit under `/cars/` paths** (tires, dash cams, tire stores, electric scooters) but
  are PRODUCTS, not the cars API. Route on the id space, never the URL prefix.
  **Follow redirects** — some index URLs redirect to a shorter canonical path with the same
  `cNNNN`; not following looks identical to schema drift.
- **Categories DO exceed the 200 `limit` cap** (TVs 303, mattresses 293), so `offset` is
  load-bearing. Mattresses is a **14.4 MB** page — the 11 MB French-Door figure is typical, not a
  ceiling.
- **Do not build against `products-api.consumerreports.org`.** The page payload supersedes it.
- **Cars ARE in scope, on a second architecture.** `cars-api.consumerreports.org`, static public
  `x-api-key` read from the car page at runtime, **no cookie**. Index `v1/cr/keys` (**7,095**
  model-years unfiltered; filter with `modelYearCarTypeId`); ratings
  `v2/cr/modelYears/{modelYearId}`; dictionary `v1/cr/glossary/1`.
- **The cars API is FULLY AVAILABLE ANONYMOUSLY** — 15 fetches SHA-256 byte-identical anon vs
  member, zero differences, across New/Used model-years and every endpoint. Scores come back
  populated with only the public key. **On cars a `null` therefore means "CR has no value", never
  "you cannot see it"** — the inverse of products, so cars `scores_available` reports `absent`,
  never `unavailable`.
- **IMPORTANT: the cars paywall is in the UI, NOT the payload.** CR gates cars only
  in the page (`window.isSubscriber=false`, 39 paywall markers, no score rendered). So serving car
  scores anonymously is not a *bypass* — nothing is being defeated, so **cars are served in full to
  everyone**. **The cars auth marker
  is `window.isSubscriber`** in the car page's server HTML, never `data-subscriber` (cars pages
  carry none). **Cars are served in full to everyone** — the API has one tier, so cars responses
  omit `auth_state`/`data_tier` entirely (for the reason `cr_reliability` pins `auth_state` to
  `anonymous` and drops `session` — same reason, different key) and `car_raw` has no `auth_tier`
  column. An earlier draft gated cars on `isSubscriber`; withdrawn — it returned less than a plain
  HTTP request would.
- **On `cars-api`, `403` means "no such route", not "forbidden"** — 12 of 16 probed paths returned
  403 with a 43-byte body. Never report a cars 403 as an auth problem. Only a 403/404 may become
  `unknown_car` (alongside the empty `200` below); a 429 is `challenged(rate_limited)` and a 400 is
  `fetch_failed(bad_request)`. The `/ec/login` rejection
  check is a `www.consumerreports.org` PAGE rule; `cars-api` never redirects there, so never apply
  it to API calls. **wafer RETURNS a cars-api 403 under both policies** — the body is JSON, never
  challenge-detected, so its bare-403 branch rotates and then hands the response back (measured
  on wafer 0.5.0: 3 requests / ~2 s under the anonymous policy, 1 with a cookie). `get_json`
  therefore sees the status and classifies it; a `Challenged` from `cars-api` is a real WAF page
  and stays `challenged`. Do not add a `Challenged`→`no_such_route` catch: it is dead for the
  real case and would misread a WAF challenge as a missing route.
- **IMPORTANT: an UNKNOWN model-year is a `200`, not a 403/404** — measured 2026-09-04:
  `v2/cr/modelYears/99999999` returns `{"response": {}, "responseSummary": {"responseCount": 0}}`
  in 81 bytes. So `unknown_car` cannot be detected from the status alone; `get_model_year` reads
  the payload and raises `no_such_route` **before the cache write**. Untranslated it becomes an
  all-null car reporting every score `absent` — "CR published no value" for a car that does not
  exist — cached for the full 30-day TTL.
- **Both `data-subscriber` values on one page is `marker_missing`**, never a pick — that is the
  "fails toward member" mistake.
- **The `/ec/login` check runs on EVERY `www.` fetch** (index, sitemaps, reliability, pages) —
  a rejected `hash` is rejected by the host. Never on `cars-api`. The *marker* step stays
  surface-specific: rejection is detectable everywhere, confirmation only where a marker renders.
- **The two auth markers are per-surface and never mixed**: `data-subscriber` on product pages,
  `window.isSubscriber` on car pages. Each surface lacks the other's marker, so a missing marker is
  only `marker_missing` for the marker that surface is supposed to carry. Only marker-bearing pages
  update `session` — never the index, sitemaps, reliability pages or `cars-api`.
- **Cars `isRecommended` is the string `"Y"`/`"N"`**, not a boolean and not products' `"Yes"`/`"No"`.
- **Cars `scores_available` never reports `unavailable`** — the API never withholds, so an
  all-null field means CR published no value, and the two enum values are `available`/`absent`.
  A key is **`null` when the response did not look**: `cr_cars(detail="summary")` fetches no per-car ratings, so it
  reports the road-test keys null rather than `absent`, which would be a claim about CR's data.
  **A page with ZERO rows reports every key null** for the same reason — `any()` over nothing is
  False, so a `no_results` page (or an `offset` past the end) used to say `safety_verdict:
  absent`, "CR published no value", derived from rows the caller's filters emptied.
- **Cars TTLs**: `car_raw` 30 days (like categories), `car_index` and taxonomy 90 days (like the
  index).
- **IMPORTANT: the cars path is cache-first under ONE convention, stated in
  `cars/repository.py`'s docstring.** Every cars fetch goes through `cars.api.guarded(rt, key,
  fetch, url=)`: single-flight on the key — `("car_page",)`, `("car_index",)`,
  `("car_taxonomy",)`, `("car", id)` — and a `Challenged` negatively cached under it for an hour,
  replayed without a request. The orchestrator (`get_model_year`, `ensure_car_index`,
  `ensure_taxonomy`) catches exactly `(FetchFailed, Challenged)` — never bare `Exception`, never
  one alone — serves what is cached with `error: null` naming the failure (`refresh_failed:<code>`
  on a served model-year, `index_refresh_failed:<reason>` on the index), and raises only when
  nothing is cached. `ensure_car_index` returns `(fetched_now, fetched_at, refresh_failed)`;
  `get_model_year` returns a `CarServed`. A 429 on a stale model-year used to be a tool error
  with the row in hand.
- **IMPORTANT: a cars index or taxonomy that parses to ZERO rows is
  `fetch_failed(empty_response)`, retryable — never written, never recorded.** `{"response": []}`
  from `v1/cr/keys` used to be recorded as a fresh 90-day run; every search then answered
  `from_cache: true` with no hits, and an empty taxonomy made every `car_type` invalid with an
  empty legal list, for three months. Same refusal as `Discovery.ensure_az_index`.
- **Cars scale: 50 makes, 677 models, 7,095 model-years** (6,698 Used / 397 New).
- **List from `v2/cr/cars`, not `v1/cr/keys` or `v2/cr/modelYears`.** It filters on
  `slugMakeName`/`carTypeSlugName`/`modelYearStateId` (they combine), **pagination works**, and it
  carries `safetyVerdictRatingScore`, `crPopularScore`, fuel economy and incentives in one request.
  `v2/cr/modelYears`' paging is broken and it has no ratings.
- **`size` on `v2/cr/cars` counts MODELS, not model-years** — `size=3` returned 18 model-years.
  Apply `limit` after the fetch; never pass it through. **Measured 2026-09-03: `size` is capped
  at 50** (`400: "default and maximum value of 'size' is 50"`), **`totalElements` also counts
  models**, and the model-year population is `counts[name == "cars"].value` (`counts` is a LIST
  of `{name, value}`). `page` is 1-based (`page=0` answers `first: false`).
- **IMPORTANT: `offset` pages over CR's OWN order; never sort the fetched prefix across models.**
  `list_cars` fetches a prefix sized from `offset + limit`, so the order it slices must be
  prefix-stable. A Python `(make, model, year)` sort of that prefix was not — a native order that
  differs from `str` collation in one place (`McLaren` before `MINI`) returned rows at two offsets
  and others at none. `stabilise_page` reorders ONLY years (descending) inside one model's
  contiguous run, which is always within a page because `size` counts models. The guarantee,
  pinned by `test_offsets_reach_every_model_year_exactly_once`: paging to `truncated: false`
  reaches every model-year exactly once.
- **`cr_cars.total` is `null` when CR sends no `counts` and pages remain** — never the fetched
  prefix (`truncated` is then `true`). It is `len(entries)` only once the last page is in hand.
- **Cars `states` is ALWAYS a list**, on `cr_car_search`, `cr_cars` rows and `cr_car`. There is
  no `state` field: it was a string for one state, a list for several and null for none.
- **There is NO bulk ROAD-TEST call** (12 probes). `v2/cr/modelYears/{id}` is one-per-car, so
  `cr_cars(detail="standard")` costs one request per returned row — hence `limit` 10, **max 15**.
  Measured 21.6 s at `limit=10` (~2.16 s/row); **Claude Desktop kills a tool call at 60 s**, so
  the old cap of 25 projected to ~51 s with nothing left for a slow response. Page with `offset`.
  Cache per `model_year_id`.
- **A cars category is scoped by its car type.** 7 types hold 58 slots but 39 distinct categories;
  5 report different counts under different parents. `(carTypeId, categoryId)` is the address, and
  `cr_cars(category=)` therefore **requires `car_type`**.
- **`v2/cr/cars` filters: `carTypeSlugName`, `categoryId`, `slugMakeName`, `modelYear`,
  `modelYearStateId`** — all map to `cr_cars` parameters. `ratingsCategoryId` is ignored. The
  `400` names only three of them, so do not treat that message as the full list. **Never confirm
  a filter from `queryParameters`** — `modelYear` works but is not echoed.
- **The cars api-key is extractable from the car page HTML server-side** — no browser needed.
- **`cr_cars` `year` is validated against the catalogue's span BEFORE the request**
  (`Cache.car_year_range()`, `MIN`/`MAX` of `car_index.year`; 2000–2028 measured 2026-09-06). CR answers a
  `modelYear` outside that span with a `400` — `"'modelYear' must be a valid year
  integer."` — which reached the caller as `fetch_failed(bad_request)` for an ordinary question
  about a 1990 Honda. **CR's accepted span IS the catalogue's, on both ends: 1999 → 400,
  2000 → 200, 2028 → 200 (empty), 2029/2030/2031/2040 → 400** (anonymous, 2026-09-06). There
  is NO sliver of years CR accepts with an empty 200 past the index max — `RECON.md` §13c-ii's
  old "roughly 2029" was an extrapolation. Below the span, or more than **`YEAR_LAG_ALLOWANCE`
  (1) above it**, is `invalid_filter_value(filter="year")` with the range in `candidates`, no
  request. **Exactly max+1 is SENT to CR**: the index is a snapshot (90-day TTL, refreshed only
  by `cr_car_search`) and a new model year lands at max+1, so a local refusal would say "2029
  is outside CR's catalogue" for cars CR lists until the index refreshes — a false absence, not
  a parameter error. Passed through, CR lists them or answers the `400` that `year_rejected`
  translates to the SAME error (code, filter, candidates), so the answer does not depend on
  whether `cr_car_search` ran first; with no index yet every value passes through the same way.
  The cost is one request for max+1 while the index is current. The lower bound stays exact: a
  catalogue only retires its oldest years, and a stale lower min passes a year CR then answers
  empty or 400, both handled. A `400` with no `year` stays `fetch_failed(bad_request)`.
- **An EMPTY `v2/cr/cars` page ships `"content": {}` — a dict, not `[]`** (measured 2026-09-05 on
  a make/year with no cars, an unknown make, and a `page` past the end). Only a list carries rows;
  `list_cars` treats anything else as empty, deliberately rather than by accident of `{}` being
  falsy.
- **`FetchFailed` carries `http_status` when a status caused it** (`server_error`, `no_such_route`,
  `bad_request`) and every surface passes it into `error.http_status` — SPEC §7 says "when a
  status caused it", and a cars `400`/`503` used to arrive with `http_status: null`.
- **IMPORTANT: a transport failure is projected into `error` by ONE function,
  `transport.failure_fields(exc)` → `{reason, retryable, http_status}`, with the code from
  `transport.failure_code(exc)`.** Every catch site unpacks it — the products fallbacks'
  `detail`, `az_failure`, the negative cache, the cars `_transport_error` — and none spells the
  three by hand. The asymmetry is honest, not papered over: a `Challenged`'s `reason` is its
  `challenge_type`, its `http_status` is always the status the challenge arrived on, and it is
  never retryable (surfaced and negatively cached, SPEC §7). `url` and `detail` are
  `FAILURE_DIAGNOSTIC_ONLY` and never escape. The `http_status: null` above was exactly a
  hand-spelled site left behind — so two tests pin this: `test_transport.py` checks the
  function reads every attribute the exceptions store (add one and it fails until you decide
  whether it escapes), and `test_boundaries.py` scans every `except FetchFailed/Challenged as
  exc` and every parameter annotated with either for an attribute read other than `.reason`
  (the one decision attribute: `no_such_route`, `bad_request`).
- **CR's `400` on `modelYear` is translated INSIDE `list_cars`**, beside the request, into the
  same `CarsQueryError` the index check raises — the repository decides which statuses are
  tool-level answers, as the products repository does. `cr_cars` therefore keeps one shape:
  `except CarsQueryError` / `except (FetchFailed, Challenged)`, and is never handed the caller's
  raw `year` back to re-decide from the exception path.

## Critical rules

- **Anonymous is a first-class mode, never an error.** Never treat a missing session as a failure.
- **IMPORTANT: the warnings vocabulary is a REGISTRY, `envelope.WARNING_TOKENS` /
  `WARNING_PREFIXES`, and every envelope validates `warnings[]` against it at construction.** An
  unregistered token fails the tool call loudly, like an unlisted `ToolError.code`; adding a
  warning means registering it there, in SPEC §7 *Warnings vocabulary* and in the bullet below
  (`tests/test_envelope.py` pins the three to each other). Before this, `warnings` was a bare
  `list[str]` spelled at a dozen emission sites and this prose was the only enumeration.
- **Every envelope a tool builds carries its base warnings — error envelopes included.**
  `session_expiring:<days>` is "on every tool that carries `session`", and the discovery state
  on every products tool; `cr_product`'s `unknown_product` and `cr_search`'s empty-query
  envelopes dropped both. `tests/test_boundaries.py` checks every `E.*Envelope(` construction
  in the tool modules passes the module's base-warnings function.
- **A display-group id passed as a category is answered with its OWNER.** `cr_ratings` prints
  `group_id` on every nested block, so `c200369` is the likeliest bare id in no index; the
  answer stays `unknown_category` (`not_in_index` once both discovery sources ran, else
  `discovery_incomplete` and retryable — the hint never overstates discovery) but names the category and the `group=` to
  pass, with `candidates: [{id, slug, name, group_id, group}]` (`Cache.group_owner`, over the
  `groups_json` ingest projects for FETCHED categories only — exactly when the id can have been
  seen). A slug is never looked up as a group id. **The category-id grammar is `cache._CID`'s
  alone: `resolve_category` parses the token once and carries the id on the `unknown`
  resolution (`Resolution.category_id`, None for a slug), and `Repository.resolve` looks the
  owner up from that — the token is never re-parsed.** A repository re-parse (`isdigit()`
  then `int()`) shipped once: `'²'` and `'①'` are `isdigit()` and not `int()`, a 20-digit id
  overflows SQLite's bind and a 5,000-digit one trips Python's conversion limit, so every
  digit-ish token `_CID` rejects fell through the slug lookup and RAISED out of `cr_ratings`,
  `cr_filters` and `cr_reliability` — the envelope bypassed for a caller-typed string.
- **`cache.row_id` owns the product / model-year key grammar** (a positive integer SQLite can
  bind, from an int or ASCII digits; everything else None), and `cr_product`/`cr_car` answer
  `unknown_product`/`unknown_car` from it without a lookup or a request. Before it, `int(id)`
  bound straight to SQLite — a 20-digit id was an OverflowError out of the tool — and a
  non-integer `model_year_id` became a `-1` that cost a real `modelYears/-1` request. The same
  `isdigit()`-then-`int()` parse sat under `group` (and under `_groupId` on CR's own payload):
  both now go through `normalize.as_int`, which never raises.
- **Warnings vocabulary** (all machine-readable, in `warnings[]`): `sitemap_pass_pending`,
  `empty_category`, `attribute_dictionary_missing`, **`ungrouped_products:<n>`** (products CR
  ships without a `_groupId`: `group: null`, `rank: null`, sorted after every group, and in
  nested mode one trailing block with `group_id: null` — never dropped, never a second group),
  `coercion_failed:<attributeId>` — **on cars
  the suffix is a FIELD NAME**, e.g. `coercion_failed:isRecommended`, because the cars payload has
  no attribute ids — `refresh_failed:<code>` (plus `refresh_failed:no_url`, which is not an error
  code but "no URL is known for this id"), **`refresh_skipped`** (a `refresh=true` inside the
  300 s cooldown was answered from a qualifying row younger than that — the read-only tools
  never prompt for `refresh`, so an injected refresh loop must not cost an 11 MB fetch per
  call; the `auth` probe passes `cooldown=False` and is exempt), `availability_not_cached`,
  `availability_stale`,
  `survey_flag_mismatch:<survey>`, `typeahead_unavailable`,
  `ratings_unavailable:<modelYearId>:<reason>`, `index_refresh_failed:<reason>`,
  `session_expiring:<days>` (the stored cookie's upper-bound life is inside 30 days; on every
  tool that carries `session`).
- **IMPORTANT: `no_results:<params>` and `empty_category` are MUTUALLY EXCLUSIVE, and both
  are rendered by ONE function, `envelope.no_results`.** `empty_category` means CR published
  nothing; `no_results` means the caller's filters excluded everything from a populated
  category — "too narrow" vs "CR rates nothing here", and only one of them is worth widening a
  filter over. `<params>` names the **tool's own parameters on both surfaces**: `cr_cars` used
  to emit `build_listing_params`' API translation, so a caller who passed `state="used"` was
  told `modelYearStateId=1` — a key it never typed, whose mapping (used=1, new=2) reads
  backwards. Every value is **percent-encoded**, deduped and length-bounded: unencoded, a
  documented `[min, max]` feature range renders `[3, 5]` whose `, ` splits the pair list,
  `brands=["a|b"]` collided with `brands=["a","b"]`, and a caller could forge a second
  vocabulary token inside a value; unbounded, one `features={…: "A"*200_000}` call returned a
  200 kB warning — the one place a caller could inflate a response without limit. `FilterSpec`
  walks `dataclasses.fields` for both `active` and `params`, so a seventh filter cannot be
  added to one and forgotten in the other (which would name a SUBSET of the filters applied,
  pointing at the wrong one); an empty list or dict is NOT active, because `features={}`
  constrains nothing. Only listing tools emit it — the search tools answer emptiness inside
  `data` by naming what was searched. **`envelope.no_results({})` RAISES, and both emission
  sites gate on the tool's active filters** (`spec.active`; `cr_cars`' `filters` dict): an
  empty dict rendered the bare `no_results:`, which the registry rejects at construction — a
  legitimate zero-row page became a tool crash. It was unreachable only by distance:
  `build_listing_params` refuses an unfiltered `cr_cars` before the request (CR's own `400`
  requires `car_type`/`make`/`state`, RECON §13c-ii; `year` or `category` alone do not
  count). An `offset` past the end is an empty PAGE of rows the filters matched — `total`
  intact, no warning, no error — never `no_results`.
- **IMPORTANT: a value quoted into `error.message` goes through `envelope.quoted` (lists:
  `quoted_list`; `candidates`/`attribute`: `envelope.candidates`/`clipped`) — never a bare
  `!r`.** The `no_results` hole on the message side: a dozen sites echoed the caller's token
  with `!r` unbounded, so a 200,000-character `category` came back as a 200,000-character
  message. `repr` is kept as the base (it escapes newlines, ANSI escapes and every other
  non-printable, and its quoting cannot be closed from inside a value), but `quoted` clips the
  VALUE at 60 characters (`QUOTE_MAX_CHARS`, `no_results`' bound; the longest CR name measured
  is under 40), bounds the RENDERING (escapes expand), describes an int past Python's
  4,300-digit `repr` limit instead of raising, and marks a cut OUTSIDE the quotes with the
  true length — `'AAAA'… (200,000 chars)` — so the clipped form can never be read as the
  value the server saw. Lists cap at 12 (`QUOTE_MAX_ITEMS`, `cr_filters`' value cap) with the
  count of distinct values left out. CR's names (group, slug, family, car type) go through the
  same helper — CR's content is attacker-influenceable here. `cr_search`/`cr_car_search`
  refuse a `query` past `SEARCH_QUERY_MAX_CHARS` (200) before any request, never clip it:
  `data.query` echoes what was searched. `tests/test_boundaries.py` scans the tool-facing
  modules for any `!r` and for an error-message interpolation that is neither a quoting call
  nor listed in `BARE_MESSAGE_EXPRS` with its reason.
- **IMPORTANT: filtering an EMPTY category must never blame the session.** `price_all_null` /
  `attribute_all_null` are `all()` over the products, so on zero products they are vacuously
  true and `filter_on_unavailable_attribute` fired — telling an agent "every value of 'price'
  is null — sign in to find out whether CR publishes it" about a category CR ships empty.
  Signing in cannot change that: it is the project's most dangerous failure mode inverted, a
  session claim about data that does not exist. Both checks are gated on `products_of(fi)`, so
  an empty category answers `empty_category` alone — which is also what `recommended`, `group`
  and `brands` already did, so the surfaces now agree.
- **Session health moves ONLY through `SessionState` events — never a setter.** The four
  writers report facts: the transport `on_rejected()` (the `/ec/login` route), the repository
  and the car page `on_marker(is_member, credential_present=)`, the sign-in flow
  `on_probe_verdict(verdict)`, `adopt()` `reconfigure()`. The jar rule — no `expired` (and no
  `active`) unless the transport asserted `hash` was in the jar after the response — is encoded
  once, in `on_marker`; `test_session_health_has_one_writer` scans `src/` for anything else
  touching health. `mark_active`/`mark_expired` are gone: a caller that chooses a state
  re-encodes the rule per surface, which is how surfaces diverge. The rejection LATCH
  (`credentials.rejected`, the jar's business) stays on the store, written by the transport only.
- **`SingleFlight` runs the work as its OWN task; every caller awaits it through `shield`, and
  the flight is cancelled only when its LAST caller leaves.** An MCP client cancelling
  `cr_ratings("tvs")` (mcp 2.1.1 interrupts the tool task) used to store the leader's
  `CancelledError` on the shared future and re-raise it in every waiter — a concurrent
  `cr_filters("tvs")` died cancelled with `cancelling() == 0`, and a cancelled `cr_car` killed
  the `cr_cars(detail="standard")` listing sharing its id (its `except (FetchFailed,
  Challenged)` never sees a `CancelledError`). A lone caller's cancellation still stops the
  11 MB download, and a newcomer never joins a dying flight.
- **Every multi-statement cache transaction is `BEGIN IMMEDIATE`, never a deferred `BEGIN`.**
  In WAL mode a transaction that reads first takes a snapshot, and once another connection has
  committed, its first write fails with SQLITE_BUSY_SNAPSHOT at once — the busy handler is NOT
  consulted (measured: "database is locked" after 0.000 s with `busy_timeout=30000`; IMMEDIATE
  waited 0.23 s and committed). Cross-process only — a second MCP host or
  `consumer-reports-mcp auth` beside the server — and `prune` runs at every startup.
- **IMPORTANT: an agent must never be able to read "no member session" as "CR did not rate this
  model".** Scores are `null`, never absent. `auth_state`, `data_tier`, `session` and
  `scores_available` are structural fields, not prose — plus `outputSchema` on every tool and a
  `data.notice` emitted only when gated fields are entirely null. This is the most dangerous
  failure mode in the project.
- **`data_tier` (the served row's tier) and `session` (the credential's health) are separate
  fields.** `auth_state` is derived from them. A cached member row served after the session
  lapsed is member data *and* a dead session; one enum cannot say both.
- **IMPORTANT: `auth_state` is derived from the SERVED row, or it is `null` — never from the
  session alone.** On the three products envelopes that carry it, a no-row error (unknown
  category, unknown product in no cached category, a parameter rejected before the fetch, a
  failed fetch with nothing cached) answers `auth_state: null` with `provenance: null`, and
  `session` alone says how the credential is. A `_error_state(rt)` helper once filled it in
  from health — `session_expired` if expired, else `anonymous` — which put `auth_state:
  "anonymous"` beside `session: "active"` on every such error: the tier of a row that does not
  exist, re-derived on one surface. An error with a row in hand (`unknown_product` in a cached
  category, a filter error after the fetch) keeps that row's tier. `cr_reliability` pins the
  literal `"anonymous"`; cars omit the key (one tier, SPEC §7): `null` = "no row to describe",
  omitted = "not a concept on this surface". The key is required and the enum stays in the
  `outputSchema` (`anyOf` with `null`). `tests/test_boundaries.py` walks every
  `E.*Envelope(auth_state=)` and accepts only `E.auth_state(...)`, `None`, or reliability's
  literal — do not "fix" the null back to a value.
- **The paywall is partial.** Only `overallDisplayScore` and `numeric-rating-score` values are
  gated. Owner satisfaction, predicted reliability, CR Recommended, prices and specs are public,
  so `scores_available` is an object — and its values are `available` / `absent` / `unavailable`,
  never booleans. Survey scores are absent for *members* outside appliances, so `false` would
  conflate "CR has none" with "you can't see it".
- **Never derive or estimate a score.** `overall_score` is CR's real value or `null`.
- **IMPORTANT: rank and compare WITHIN `_groupName`, never across it.** CR renders each group as
  a separate ratings table and `overallDisplayScore` is not comparable across groups — for members
  either. Within a group `_overallSortIndex` is an exact score rank (100% in 17/17 groups tested),
  and it is identical anonymous-vs-member, so anonymous callers get exact within-group ranking.
  **A cross-group score sort is supported and structurally labelled, never refused** — the
  envelope carries `sort: {key, order, scope}` with `scope: "cross_group"`, plus a `data.notice`.
  Withholding data is not this project's guard; structure is. `ranking`/`ranking_caveat`/
  `rank_scope` are all gone — `rank` is always within-group, stated once in the spec.
- **Sort on `_overallSortIndex` in both tiers, always.** Never on `overallDisplayScore` — that
  would reorder tied products for members only and break tier-independence.
- **`rank` is the position in CR's unfiltered group table.** It does not renumber under filters
  or `sort="price"`. Each group carries `size` (unfiltered) and `total` (matching). **`group` is
  never null** — a single-group category names itself (`_groupId` == `cid`).
- **CR's `type` filter is a no-op** — one option, the category you are on, in 5/5 categories
  tested. Product-type scoping is `args.scid`/`cats[]`/`subcats[]`, surfaced as
  `cr_categories(family=)`.
- **Flat mode defaults to CR page order `(group, rank)`.** An *explicit* `sort="overallScore"`
  with flat on a multi-group category is honoured and labelled `scope: "cross_group"`; note that
  ordering falls back to `overallDisplayScore` and is therefore member-only.
- **`cr_ratings` nests by group** when a category has several and no group was requested; flat only
  when the result set is score-comparable. `limit` applies per group in nested mode (default 10
  per group nested, 25 flat).
- **IMPORTANT: check `is_login_url(resp.url)` BEFORE parsing anything — the FINAL url, never
  `resp.history`, and as a ROUTE, never a substring.** A *successful* re-mint also redirects
  through the login host (2 hops), so a history check reports every healthy member fetch as a
  dead credential. And `"/ec/login" in url` matched `www.…/anything/ec/login-tips/`: one such
  page latched `rejected`, expired the session and evicted the cookie for the process. The
  check is `config.is_login_url` — `secure.consumerreports.org` AND the `/ec/login` path segment
  via `urlsplit` — in both `transport.fetch` and `ingest.classify`. CR rejects an invalid
  `hash` by redirecting to `secure.consumerreports.org/ec/login?error` — a 51 KB login page with
  **no payload and zero `data-subscriber` of either value** (`RECON.md` §10b). Under the
  marker-only rules that fires `payload_missing`, i.e. a schema-drift alarm for an ordinary
  expired cookie. It is `credential_rejected` → `auth_state: "session_expired"`, never a drift
  alarm. On rejection, drop the credential from the jar for the rest of the process and retry
  once without it, so a stale cookie costs one request instead of breaking anonymous browsing.
- **Detect auth from `data-subscriber` in the server HTML** (`"true"` = member). Never from
  "a cookie is configured", never from "scores are null", and **never from "Sign Out"** — that
  string is in the anonymous page too and fails toward `member`.
- **Collect `hash` and `userLicenses`, and persist rotations.** `hash` is the durable credential
  (365d) and alone restores the session; `userLicenses` is derived and rotates. Pinning the pasted
  value and ignoring rotations breaks renewal. Read rotations from `session.get_cookie(name,
  https_url)` — **not** `resp.cookies`, which is only the final response's `Set-Cookie` headers
  while the re-mint lands on a redirect hop.
- **IMPORTANT: when a cookie is configured, construct the session with `max_rotations=0,
  max_failures=None`, and assert `hash` is in the jar before classifying anything
  `session_expired`.** A wafer rotation rebuilds with an **empty cookie jar** and does not raise
  — so one transient 403 makes CR return a normal anonymous `200`, which looks exactly like an
  expired session. A missing credential is `fetch_failed(identity_rotated)` — a transport error,
  never cached, never retried here — not an auth state.
  **These are constructor kwargs, not per-request.** wafer has no per-request form, and a second
  session is not the workaround: `rate_limit` is per session, so two would double the request
  rate. One session per process, policy chosen from cookie presence at startup.
- **Inject the cookie with an explicit Domain**: `hash=…; Domain=.consumerreports.org; Path=/;
  Secure`. A pasted `name=value` carries no attributes and the re-mint crosses origins.
- **Never store credentials.** Cookie only, supplied by the user. Never ask for a password,
  automate login, solve a CAPTCHA, log a cookie, or write one to the cache.
- **`auth --browser` is an optional `[browser]` extra, and it NEVER touches the password field.**
  Launch installed Chrome (`channel="chrome"`, `headless=False`), a **fresh throwaway context** —
  never the user's profile, that is the same trust line that killed `rookiepy` — pre-tick
  `setAutoLogin` (it mints the 365-day `hash`; without it the session dies in days), then poll
  `context.cookies()` for `hash` and do nothing else. Never read, fill or submit username or
  password. **`context.cookies()` is every cookie in the window**: accept only a `hash` whose
  domain is `consumerreports.org` or a subdomain and whose value matches `BARE_HASH` (36
  chars); anything else keeps polling. A stray `hash` from another site used to become the
  capture and fail the flow permanently. Playwright obtains a cookie and exits; it is never on the data path and never a
  fallback for a blocked fetch.
- **IMPORTANT: the sign-in window must NOT advertise the automation.** Launch with
  `args=["--disable-blink-features=AutomationControlled"]`,
  `ignore_default_args=["--enable-automation"]`, and `new_context(no_viewport=True)`. CR's login
  form runs an INVISIBLE hCaptcha on every submit that keys on `navigator.webdriver` — measured
  on the real page (`RECON.md` §5 *Login*): `true` (Playwright's default) opened a puzzle and
  issued no token, `false` issued a token silently in <1 s, nothing else differed. A human who
  solved that puzzle in the flagged window was still answered "We still don't recognize that
  sign in" for credentials that worked at once in plain Chrome — the paywall-shaped failure of
  this flow, and it looks exactly like a mistyped credential. The blink flag does the work;
  `ignore_default_args` alone is a no-op on Playwright 1.62 (no `--enable-automation` in its
  default switches) and stays for the older releases `>=1.40` admits. `no_viewport` drops the
  emulated 1280×720 `screen` (equal to the viewport, no menu bar — no real desktop looks so).
  Warm-up visits, the bare `/ec/login` URL and `page.check()` were all measured irrelevant (CR
  already renders `setAutoLogin` checked; the tick is a guard).
- **IMPORTANT: the browser teardown is BOUNDED, and that bound is THE fix for orphaned
  Chrome.** `context.close()`/`browser.close()` wait for a driver reply and have no timeout; at
  loop shutdown `asyncio.run` cancels Playwright's pipe reader in the same sweep as the capture,
  so an unbounded close deadlocks the interpreter with Chrome open — measured (a harness sat in
  `kevent` for 26 min). **An orphan comes from Python staying ALIVE while wedged — never from
  Python being killed.** Measured with real Chrome and Playwright 1.62: `SIGTERM` and `SIGKILL`
  of the Python process mid-capture leave NOTHING — the driver's exit hook
  `process.kill(-pid, "SIGKILL")`s the detached browser group the moment its stdin closes.
  **On Windows that hook is `taskkill /pid <pid> /T /F`** — read in the 1.62 driver
  (`coreBundle.js` `killProcess`; the browser is spawned `detached` only off Windows, there is
  no process group to signal) — so a killed Python leaves nothing there either. So
  the pid kill is scoped to exactly one case, *Python alive, driver unresponsive*: `browser_auth`
  reads the pid at launch (browser-level CDP `SystemInfo.getProcessInfo`), bounds
  context+browser by `CLOSE_TIMEOUT_S` and the driver stop by `DRIVER_STOP_TIMEOUT_S`,
  `SIGTERM`s what is not confirmed closed (only while the process table still shows a
  Playwright-profiled browser on that pid — `/proc/<pid>/cmdline` on Linux, `ps -ww` where
  there is no procfs, `Get-CimInstance Win32_Process` on Windows since `tasklist` has no
  command-line column; `powershell` 5.1, never `pwsh`), and an `atexit` reaper covers a loop
  torn down under the capture. **IMPORTANT: `ps` needs `-ww`, and the query answers a
  STATUS, never a bare None.** The first CI run (2026-09-06) measured the guard's first
  spelling on the two platforms it had only been reasoned about: on ubuntu-latest procps cut
  the piped line at 80 columns — a real Chrome came back as `/opt/google/chrome/chrome
  --disable-field-trial-config --disable-background-netw`, before the profile marker — so the
  Linux guard NEVER matched a live browser and the pid kill never fired there (the padded-argv
  guard test caught it, as written to); macOS `ps` never truncates a pipe (measured) and
  accepts `-ww`, so the spelling is one for both, with Linux reading the kernel's own argv from
  `/proc` first. On windows-latest the same `Get-CimInstance` that had matched a padded
  `python.exe` child in the guard test returned None for the real Chrome — three causes
  (gone, a null `CommandLine`, a failed or timed-out query) were one silent value. The query
  is now a base64 `-EncodedCommand` (no quoting layer), writes to `[Console]::Out` (not the
  host's formatter), answers gone / unreadable / failed as distinct exit codes under
  `$ErrorActionPreference = 'Stop'`, and has `COMMAND_LINE_TIMEOUT_S` (15 s: a cold WMI
  provider host is slower than the old 5 s) — `_query_command_line` returns `(text, status)`,
  `status` in `found` / `gone` / `unreadable` / `query_failed:<why>`, and only `found` can
  match. Which of the three Windows causes it was is what the re-run confirms; the fix covers
  all three. **The query is decoded with `errors="replace"` and spawned with
  `CREATE_NO_WINDOW` on Windows**: PowerShell 5.1 writes the OEM code page while `text=True`
  decodes ANSI, and the profile path carries the user's name — a strict decode raised
  `UnicodeDecodeError` (not an `OSError`) out of the guard, out of `_close_browser`'s
  `finally`, replacing a captured token; and a console program spawned from a console-less
  server opens a console window. On Windows `os.kill` is `TerminateProcess` of the browser
  alone; the helpers leaving with it is what `tests/live/test_browser_hardware.py` measures.
  **The Windows branch is MEASURED by `.github/workflows/ci.yml`, not only pinned**: the guard
  test runs the real `Get-CimInstance` query on a Windows runner (it passed there on
  2026-09-06), and the hardware test (`CR_BROWSER_LIVE=1`, real Chrome on `about:blank`, no
  CR traffic) runs on Windows, macOS and Linux runners.
  **`src/` installs no `SIGTERM` handler**, so a Desktop `SIGTERM` skips `finally` and `atexit`
  both — harmless only because of the driver hook. The sign-in path never blocks the loop — the
  heartbeat gaps were nil, `sample` showed `kevent`, a thread-posted callback ran — so do not
  look for a sync call; look for an unbounded await at shutdown. **A teardown step that overruns
  is ABANDONED in its own task, never cancel-and-waited: `asyncio.wait_for` cannot bound a
  Playwright call** — `_inner_send` absorbs the first cancellation (sends `__abort__`, awaits
  the abort's reply, which a dead reader never delivers); measured with the timeout fired and
  `cancelling()` at 2, freed only by a third cancel. `SignInFlow.cancel()` returns within
  `CANCEL_WAIT_S` (shielded task) and re-raises only the CALLER's own cancellation; the task
  reads `cancelling()` before `save()`, so a cancellation a library swallowed cannot store a
  cookie behind a `cancelled` status. The Playwright import (0.28 s cold; the bundle ships no
  `.pyc`) runs via `asyncio.to_thread`.
- **`cr_sign_in` is the PRIMARY auth path; `auth --browser` and the paste are the fallbacks.** A
  Desktop user may never open a terminal. **Claude Desktop kills a local tool call at 60 s and
  progress notifications do not extend it; elicitation is unavailable to local stdio servers.**
  So the tool is non-blocking: `cr_sign_in` starts a background task (`capture_hash` → the
  SHARED `auth_tools.validate_cookies`, which the CLI also calls → `save` → `transport.adopt()`)
  and answers within ~2 s; `cr_auth_status(wait_s)` long-polls, **capped at 45 s**. Idempotent
  while one is in flight (`in_progress`). **IMPORTANT: the guard VERIFIES an `unverified`
  cookie; it never refuses blind.** Health leaves `unverified` only on a marker-bearing fetch
  (category page, car page) — `cr_categories`, `cr_search`, reliability and the cars API never
  move it — so in a fresh Desktop process a stored cookie can sit at `unverified` forever, and
  a guard that refuses on it refuses a DEAD cookie forever with a message claiming it works
  (that shipped once; so did the opposite defect, an `active`-only guard that opened a window on
  every start). Unforced: `none`/`expired` proceed; `days_left_max <= 0` proceeds whatever
  `session` says (the project's own bound says it cannot be live); `active` refuses
  `session_active` (verified live THIS process — the message may say so); `unverified` starts
  the task in a `verifying` phase that runs `validate_cookies` on the STORED cookie — the
  verdict goes to `health.on_probe_verdict()`: `member` → `active` and `refused /
  session_active` via the poll; `session_expired` / `credential_rejected` → `expired` and the
  window opens; anything else is no verdict:
  `failed` with that reason, no window on a guess. **`force` means only "replace a cookie
  verified live".** Refuses `env_override` when `CR_SESSION_COOKIE` is set (the env var wins
  over the file, so a capture would be silently ignored — the message names where to clear it;
  blank and an unexpanded `${…}` template both count as unset); refuses `browser_extra_missing`
  naming the install command, before any probe is spent. **Never return or log the cookie.**
  Annotations `readOnlyHint=False, idempotentHint=False, openWorldHint=True` — never the shared
  read-only object — so both clients prompt, and the description carries "only when the user
  explicitly asks". A failed validation stores nothing, same as the CLI. The task has a
  catch-all (`internal_error:<type>`): a live phase can never outlive the task.
- **IMPORTANT: a cookie saved mid-process is a NO-OP until `Transport.adopt()` runs.** The
  rotation policy is a constructor kwarg fixed per SESSION; `adopt()` re-reads the store under
  the lock, flips `cookie_configured`, clears `credentials.rejected`, calls
  `health.reconfigure()` (every other health event is a no-op while unconfigured, by design) and retires the
  session so the next request rebuilds with `max_rotations=0, max_failures=None`. Tier
  qualification reads `effective_tier` per call, so the anonymous row stops qualifying by itself.
  **A `www.` response that arrives after an adopt is `fetch_failed(policy_changed)`** —
  retryable, never cached, never classified — because classifying it under the new credential
  would latch a rejection of the OLD cookie onto the NEW one. **It is OUR state change, so every
  caller that owns a tool call retries it exactly once** (`transport.retry_once`:
  `get_category`, `get_reliability`, `CarsApi.page_info`, the sitemap pass) — the real sequence
  is "sign-in starts, the user takes a minute, the agent runs an 11 MB fetch, validation lands
  mid-fetch", and unretried that was a tool error. `products.xml` is retried once on ANY
  retryable failure: unfetched, `sitemap_done()` is false for the whole process and `c28700`
  answers `discovery_incomplete` until restart. A single sitemap lost to `policy_changed` is
  retried, not skipped — skipped, its categories are `not_in_index` for a TTL. wafer 0.5.0 has
  no `close()`; `__aexit__` is the only teardown and it interrupts nothing in flight.
- **Renewal: `session_expiring:<days>` fires on every tool carrying `session` inside 30 days**
  (`remaining_days_max`, truncated, clamped at 0; file source only; not once `expired`) —
  `cr_sign_in` and `cr_auth_status` included — and the `session_expired` notice names
  `cr_sign_in`, then the CLI, then the env var. Renewal is a plain `cr_sign_in`: a rejected or
  past-bound cookie proceeds unforced; `force` is for renewing early.
- **The `.mcpb` bundle's manifest `version` and `tools` are GENERATED** by
  `scripts/build_bundle.py` from `pyproject.toml` and `server.DESCRIPTIONS`; the template in
  `bundle/manifest.json` carries neither, and a versioned template is refused. **`uv sync` skips
  the bundle project's OWN extras but honours extras on a dependency**, so the bundle depends on
  `consumer-reports-mcp[browser]`. Pre-publication the source is vendored (`[tool.uv.sources]`
  path); the one-line switch is commented in `bundle/pyproject.toml`. **Desktop's Linux beta
  shipped 2026-06-30** (Ubuntu 22.04+/Debian 12+, apt), so `compatibility.platforms` declares
  `linux` too — the `uv` type is cross-platform and nothing in the bundle names a platform.
  CI runs the server and the bundle build on a Linux runner; an `.mcpb` install into the Linux
  Desktop itself has not been performed.
- **The `auth` probe category (`c35183`) is exempt from validate-the-id-first** — on a cold
  machine there is no index yet, and it is a shipped constant, not user input.
- **`auth` reads stdin, never argv** — a credential on a command line lands in shell history and
  the process table. Accept a bare `hash` (36 chars, the documented path), a `name=value;` cookie
  string, or a full cURL paste. `hash` ALONE is sufficient (`RECON.md` §5, §10i); it costs one
  extra redirect on the first request and nothing after.
- **Cache key includes the auth tier, and scored rows are never overwritten by unscored ones.**
  CR returns a normal `200` anonymous page when a session lapses.
- **IMPORTANT: key on `args.cid`, NOT the id you requested.** A subcategory URL redirects to the
  parent's path while keeping its own id and serves the PARENT's payload — `/c200367/` returns
  `cid=37162` with all 172 French-Door products. Requested id, final-URL id and `args.cid` can
  all differ. Record the requested id as an alias; the row belongs to the payload's category.
- **Validate a category id against the A-Z index BEFORE fetching** — CR answers a bad id with a
  `500`, not a `404`, and wafer retries 5xx three times. **`limit`/`offset` are validated before
  the fetch too** (`validate_paging`) — the cap depends only on `detail`, and rejecting after the
  fetch charged an 11 MB download for a parameter error. Only the *default* limit needs the
  payload, because it depends on the group count.
- **A slug the server has published must keep resolving.** Discovery prefers the sitemap's shorter
  canonical URL, and the slug is derived from that URL — so the background pass rewrites slugs the
  synchronous A-Z pass already handed out (measured: `cruise-lines` → `cruises`,
  `car-rental-companies` → `car-travel`). Every superseded slug is recorded in
  `category_slug_alias` and consulted only after `category_index` misses, so a live slug always
  beats a retired one. Without it a slug that worked at startup answers `unknown_category /
  not_in_index` seven minutes later — "CR does not rate it".
- **A cache hit requires a row at a tier ≥ the caller's EFFECTIVE tier** — `member` when
  `session` is `active`/`unverified`, `anonymous` when `none`/`expired`. A member row satisfies
  anyone. Key on the *session*, not on "is a cookie configured": with a lapsed cookie the latter
  qualifies no row at all, so every call refetches 11 MB and appends a row that also fails to
  qualify — a dead cookie ends up worse than no cookie. Tier qualification decides whether to hit
  the network; it never discards a row on the way back, so a failed fetch still serves what is
  cached.
- **`stale` means past-TTL and nothing else.** A scored row retained over a newer unscored one is
  reported by `superseded_at`.
- **IMPORTANT: decide "refetch?" from `Selection.checked_at`, NEVER from `sel.stale`.** Under
  never-downgrade the served row is the retained scored one; once past the TTL it is stale on
  every call for life, so `not sel.stale` as the hit test fetched 11 MB and appended a row on
  every call, forever — for the lapsed-membership case the rule exists for. `checked_at` is the
  `fetched_at` of the newest row the caller could have been served at their effective tier;
  `Repository._needs_refresh` reads it. One refetch after the TTL, then silence until the newer
  row ages out. For `cr_product` it is the CATEGORY's newest usable row, containing the product
  or not — a product CR dropped lives only in old rows.
- **Identical bytes inside the TTL append nothing.** `write_category(..., ttl_days=)` and
  `write_reliability(..., ttl_days=)` skip the INSERT when the newest same-tier row carries the
  same `payload_json` and is inside the TTL, and serve that row (`from_cache: false`,
  `fetched_at` unchanged — when CR first served these bytes). Only inside the TTL: past it the
  same bytes MUST land as a new row, because a row is also the record of a check. Pass
  `ttl_days` from the repository; the cache never owns the TTL.
- **The envelope is 8 keys**: `auth_state`, `session`, `scores_available`, `provenance`
  (`data_tier`, `fetched_at`, `cr_url`, `from_cache`, `stale`, `superseded_at`), `sort`,
  `warnings`, `error`, `data`. Provenance is grouped so it does not bury `data`. **Every tool
  wears the outer `{session, warnings, error, data}` — the two auth tools included**: their
  status object sits under `data` and `error` is always null there. An earlier draft made
  `cr_sign_in`/`cr_auth_status` flat, which contradicted SPEC §7's own table and made
  `session_expiring` structurally impossible on a tool that carries `session`.
- **Score ranges and family structure come from the CATEGORY page, not the reliability page** —
  `modelMin/MaxOverallDisplayScore` + `ratedModelsCount` ship for every sibling. Ingest enriches
  `category_index` for the whole family; `score_range_status` is `known`/`none_published`/
  `not_fetched`, never an ambiguous null. Under `not_fetched`, `family` and `groups` are
  **`null`, not `[]`** — an empty array is a positive claim that CR ships no groups, which is
  what a single-group category looks like.
- **`sort` must default to `null` in the schema**, not `"overallScore"` — otherwise an MCP call
  cannot distinguish "passed the default" from "omitted", which is the whole cross-group trigger.
- **Tool returns must be typed models, not `dict`** — under `mcp==2.1.1` a `-> dict` annotation
  yields an untyped `outputSchema`, i.e. the structural guard failing open.
- **Never return the raw payload** (a full category is ~357k tokens). `cr_ratings` defaults to
  `detail="standard"` with `limit=25` flat / 10 per group nested; `cr_product` is the drilldown.
  **`full` is capped at `limit=10`** and its nested DEFAULT is **5 per group** — re-measured on
  the real 172-product page by `scripts/measure_sizes.py` (nested-10 is 31k tokens even lean).
  Attribute records serialise leanly: `raw_value` only when it differs from `value`, and
  `unit`/`description`/`group` only when CR ships one. A null `value` is always present.
- **`cr_filters` MUST collapse numeric `data[]` to `{min,max}`.** As CR ships it the response is
  36,007 tokens, 94% of it the `features` filter embedding every distinct value. Range-collapsed
  with descriptions kept it is ~3.4k. Cap non-numeric value lists at 12. (Exact figures move with
  CR's catalogue — re-run `scripts/measure_sizes.py` rather than trusting a number here.)
- **Attribute descriptions are category constants** — serve them once via `cr_filters`, never
  per product. They more than double `cr_product`.
- **`dont_buy` ships in every product shape, including `summary`.** It is rare, which is why
  omitting it is dangerous — a CR safety warning must never be drilldown-only.
- **Store an ingest envelope, not the page and not bare `filterInstanceDATA`** — a ~10x size
  difference from the HTML, but `categoryAttributes` sits *outside* the payload, so
  `payload_json` is `{schema_version, filter_instance, category_attributes, family,
  supercategory, data_subscriber, final_url, requested_id, http_status}`. `family` is stored
  even though it is also projected into `category_index`: the index is derived, and a family
  parsing fix must not cost a re-fetch. Storing only `filterInstanceDATA` yields a cache that can never serve
  a unit or a description. `category_raw` is append-only with a `scored` column; no normalized
  per-product table, only a many-to-many index for routing.
- **Never prune the newest scored row for a category.** Age-based pruning would delete exactly
  what never-downgrade exists to keep. **`Cache.prune()` runs once at startup from the server
  lifespan** (rows past 3×TTL) — it had no runtime caller, so the cache had no bound at all.
- **`attributeId` is the stable key; `name` is not.**
- **IMPORTANT: `attributeTypeName` is a HOMONYM.** On a `categoryAttributes` definition it is
  `PRICING`/`SPEC`/`TEST_RESULT` and the dataType is in **`attributeDataTypeName`**; on a product
  `attrs[]` entry it IS the dataType. Read the wrong one and everything types as `"SPEC"`.
- **`categoryAttributes` is reached via `.cat` inside `console.log('[ ratings-wrapper ]', {…})`**
  — a debug statement left in production, in the same `<script>` as the payload. `.cat._id ==
  args.cid` (6/6), siblings are `.productFilterPayload.debug.categories[]` keyed by `_id`,
  `.subcats` are the display groups, `.scat` the supercategory. **Treat this anchor as the most
  fragile thing in the project** — a minifier removes it without warning, which is why the
  `attrs` → `attributeTypeName` fallback is load-bearing.
- **IMPORTANT: that anchor has a LIVE canary, `tests/live/test_live_canary.py`** (`CR_LIVE=1`,
  anonymous, two small category pages, 2 requests). The day the `console.log` is stripped the
  OFFLINE suite stays green — it pins both sides of the fallback and cannot say which side the
  live page is on — and every response loses units and descriptions behind one
  `attribute_dictionary_missing`. The canary fails with the MODE named (`anchor_missing`,
  `anchor_unparseable`, `cat_id_mismatch`, `dictionary_missing`/`_empty`, `definition_shape`,
  `ratings_unsortable`, `units_missing`, `descriptions_missing`, `coverage`, `family_missing`),
  on two categories so a template-wide break reads differently from a local one. It does NOT
  assert `unitName` as a key — Banks `c37154` ships definitions without it — nor the dataType
  set, array order or `sortOrder` on every rating definition. An empty `categoryAttributes: []`
  warns `attribute_dictionary_missing` like an absent one: it used to pass silently.
- **Nest on distinct `_groupId` in `data`, never on `len(subcats)`** — c37162 ships 4 subcats but
  only 3 groups have products.
- **IMPORTANT: `categoryAttributes` ARRAY ORDER differs between tiers** — same ids, same content,
  shuffled. So `detail="standard"` MUST select by `sortOrder` (missing sorts last, tie-break on
  `attributeId`); declaration order returns three *different* attributes to a member than to an
  anonymous caller, on the same category.
- **Join definitions from `categoryAttributes`, NOT `filterInstanceDATA.attrs`.** Both are on the
  category page; `attrs` misses attributes in most categories, `categoryAttributes` covered 100%
  in all 8 tested. Order: `categoryAttributes` → `attrs` → the entry's own `attributeTypeName`.
  (`attrs` keys it as `id`, per-product entries as `attributeId`.)
- **Never infer a unit.** `unitName` ships in the definition block.
- **Coerce by declared `dataType`, never by value shape.** `boolean` values are the strings
  `"Yes"`/`"No"`, and `text` attributes contain `"Yes"`/`"No"` too. **The type set is open** —
  at least seven exist (`custom`, `numeric-overall-score` beyond the obvious five). Any type not
  in the coercion table is carried through untouched, like `text`.
- **Never coerce `text`.** It holds numeric-looking dial positions (`"39"`, `"-1"`).
- **A value that will not coerce keeps `raw_value`, sets `value: null`, and warns.** Never guess.
- **General-purpose. No caller-specific workflow appears anywhere** — it exposes tools and
  writes nothing outside its own cache.

## HTTP

- **ALWAYS use wafer** (`~/code/wafer`, see its `llms.txt`) — never `urllib`, `requests` or `httpx`.
  Pin **`wafer-py>=0.4.9,<0.6`**, both ends deliberate. Every rule in this section is a
  *semantic* measured on 0.4.9 and 0.5.0, not an API signature, and wafer is pre-1.0 with no
  changelog — its minors are where the surface has moved (0.3.0 added `max_response_size`,
  `attempt_timeout`, `get_cookie`; 0.4.2 `RequestBlocked`; 0.5.0 a second `_rebuild_client`
  caller). A PyPI install has no lockfile, so without the ceiling a 0.6 would change these rules
  silently. The floor is real: the suite passes on released 0.4.9 (identical count), and the
  0.4.9→0.5.0 diff outside the AIA feature is nil. `tests/test_transport.py` pins the range to
  `pyproject.toml`, SPEC §9 and this line, and the installed version to the range — widening it
  means re-measuring first. **`llms.txt` documents the local checkout, not the release** — it
  currently runs ahead of the published tag, so check any API you adopt against the released
  version, not the working tree.
- **ALWAYS pair `timeout=` with `attempt_timeout=`.** `timeout` is a TOTAL budget across retries,
  not a per-attempt cap. Unpaired, one hanging request eats the budget and retries never fire.
- **The politeness interval is OUR gate (`Transport._space_out`), and wafer gets `rate_limit=0`.**
  wafer's `RateLimiter` is wait → send → record with no lock: five concurrent callers at
  `min_interval=2.0` sent at offsets `[0, 0, 0, 0, 0]`. The sitemap pass plus a parallel
  `cr_ratings`/`cr_reliability` is N× the configured rate under the user's cookie. The gate is
  an `asyncio.Lock` per host held across the WAIT only — never across the request, or 60-second
  downloads queue behind each other — stamping the last send as it releases. wafer's limiter is
  disabled so the two never stack (its retry backoff is separate). **The one-session rule is
  about sessions; it never addressed concurrency within one.** The config var is still
  `CR_MIN_REQUEST_INTERVAL_S`, SECONDS BETWEEN REQUESTS, default `2.0`, `0` disables; an "rps"
  value inverts the politeness ceiling. Test harnesses pass `0` — the fake session never waited.
- **Every fetch passes `Transport._check_url`: `https` and a host in `ALLOWED_HOSTS`** (exactly
  `www.`, `secure.`, `cars-api.consumerreports.org` — an exact set, not a suffix). URLs come from
  FETCHED content (`<loc>`s, `reliabilityURL`, cached `final_url`) and the jar seeds `hash` for
  `.consumerreports.org`, so without it CR's content could aim this process at a metadata host
  or hand the credential to any CR subdomain. Off-host is `fetch_failed(url_unresolved)`,
  not retryable, before a session is built. The sitemap pass walks at most `MAX_SITEMAPS`
  (1,000) `<loc>`s and records at most 50 error strings (`sitemap_failed` counts them all).
- **Size the timeouts for an 11 MB body** (`CR_TIMEOUT_S=120`, `CR_ATTEMPT_TIMEOUT_S=60`). An
  API-sized `attempt_timeout` kills the download mid-body, and wafer then replays the whole
  11 MB up to `max_retries` times.
- **One shared `AsyncSession`**; `SyncSession` is not thread-safe. Set `max_response_size`.
  One documented exception: `auth_tools.validate_cookies` builds a short-lived second session for
  a SINGLE probe request and closes it (SPEC §9) — the rule guards the sustained request rate,
  which one released probe does not affect.
  Single-flight on `(category_id, tier)` so two calls never fetch 11 MB twice — and the flight
  is its own task, so a cancelled caller never cancels it for the others (*Critical rules*).
- **Never log or return a response body** — WAF challenge pages carry tokens and member pages
  carry profile data. Status, `reason` and `<title>` only.
- **Derive `fetch_failed.reason` from the EXCEPTION TYPE, never by parsing
  `ConnectionFailed.reason`** — that attribute is free text, documented for exactly one case.
  Carry its text for diagnostics; classify on the type.
- **A challenge can arrive with a `200`** — check `resp.challenge_type is not None`, not just the
  status code.
- **`identity_rotated` is a handled path, not an assertion.** `_rebuild_client` empties the jar
  and rotation is not its only caller (the AIA cert-chain path calls it too). **The pin resolves
  to `0.5.0`, which HAS that feature — so this fires in production, it is not theoretical.** Re-seed `hash` from the store
  *before* a request if it is missing from the jar; classify a missing `hash` *after* a
  logged-out response as `fetch_failed(identity_rotated)`, uncached.
- Cache-first. Hit the network only on a miss or an explicit `refresh`.

## Development

```bash
uv sync --extra dev
.venv/bin/pytest tests/ -x -q          # tests (tests/live is skipped unless CR_LIVE=1)
.venv/bin/ruff check src/ tests/ scripts/   # lint (fix with --fix)
uv run --with tiktoken --no-sync scripts/measure_sizes.py   # sizing, needs scratch/ captures
uv run scripts/build_bundle.py         # the Claude Desktop .mcpb; no network, version generated
```

- **`CR_OFFLINE=1` makes every fetch fail fast as `fetch_failed(connection_failed)`** — the cache
  still serves. The stdio test uses it; so can a deliberately offline run.
- **`Settings` takes `HOME` from the environment mapping it is handed**, never from the host,
  so a test with `env={"HOME": tmp}` cannot write `session.json` into the real home. A test that
  builds `Settings(env={})` without `home=` will (it happened once; the harness now sets both).
- **A SPAWNED CLI's constructed environment must carry Windows' system variables.** The stdio
  test hands `consumer-reports-mcp` only `PATH`, `HOME` and the `CR_*` it means to; on the
  windows-latest runner (2026-09-06) that process died at `import asyncio` — `_overlapped`
  raised `OSError: [WinError 10106]`, Winsock unable to load its service providers, because
  `SystemRoot` was absent. Ours, not the image's: `tests/test_server.py` copies
  `WINDOWS_SYSTEM_VARS` (`SYSTEMROOT`, `SYSTEMDRIVE`, `WINDIR`, `COMSPEC`, `PATHEXT`) from the
  host when present — none names a user directory, so `HOME=tmp_path` still isolates the
  server, and on POSIX nothing is added.
- **`CR_BROWSER_LIVE=1 .venv/bin/pytest tests/live/test_browser_hardware.py`** opens a real
  Chrome on `about:blank` (no CR traffic) and measures the kill of last resort on this OS;
  `.github/workflows/ci.yml` runs it, and the whole suite, on Windows, macOS and Linux runners.

Run lint and tests before every commit.

- **Fixtures come from anonymous fetches, and are content-minimal as well as score-free.** Never
  commit member-only scores — and never commit a full anonymous payload either: it is still 1.1 MB
  of CR's catalogue. Synthetic brand/model strings, real attribute definitions, real filter shapes,
  real `_groupName` values.
- **MCP tools in Claude Code connect to the installed server, not your working tree.** Edits need
  an MCP restart; test inline with `.venv/bin/python -c "..."` first.
- **The server class is `MCPServer` from `mcp.server.mcpserver`** (`mcp >= 2`). `FastMCP` was
  renamed and `mcp.server.fastmcp` no longer exists — the old name is an import error.

## Git

- **NEVER commit without explicit permission** — only when the user actually asks.
- **NEVER add Claude attribution.**
- **Always bump version** in `pyproject.toml` (patch by default; ask before minor/major).
