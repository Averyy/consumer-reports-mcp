# PLAN.md — implementation plan for consumer-reports-mcp

> **Historical.** This is the plan the code was built from (written 2026-09-03, before any code
> existed; the last task was checked off 2026-09-05). It is kept because module docstrings and
> tests cite its task ids (`PLAN P2.2`, `P10.3`, …) and because §6 records the spec gaps the
> plan surfaced. It is not maintained as a description of the shipped server — `SPEC.md` is the
> design of record and `CLAUDE.md` the binding rules; where this file and they disagree, they
> win. Excluded from the sdist.

Design of record: `SPEC.md`. Evidence: `RECON.md`. Binding rules: `CLAUDE.md`. This file does
not restate them; it says what to build, in what order, and how to prove each piece works.
Section references below are `SPEC §n`, `RECON §n`, `CLAUDE.md` unless stated.

**Phases are BUILD ORDER, not gates.** Everything here ships together as v1. The phases exist
so a developer writes the thing whose dependents need it first — nobody stops at a phase
boundary, nothing is "phase 2 scope", and every task carries its own tests so the whole tree is
green at every step of the order.

---

## 0. Orientation

**What.** A Python 3.12+ MCP server (stdio) that exposes Consumer Reports ratings as nine
structured tools, cache-first over SQLite, with an anonymous first-class tier and an optional
member tier unlocked by a session cookie the user pastes in. Plus a `consumer-reports-mcp auth`
CLI (`--status`, `--forget`). When this was written zero lines of code existed; recon and three
spikes were done (`RECON.md` §10) and their results were already in the spec.

**Two surfaces, nothing shared between them** (SPEC §5, §7, RECON §11a):

| | Products (6 tools) | Cars (3 tools) |
|---|---|---|
| Source | `window.filterInstanceDATA` embedded in a category page — one GET per category | JSON API `cars-api.consumerreports.org`, public `x-api-key` read from a car page at runtime, no cookie |
| Discovery | A-Z index (236, names) **plus** 195 sitemap XMLs (346 total) | `v1/cr/keys` (7,095 model-years) |
| Paywall | **In the payload** — gated values arrive as `null` | **In the UI only** — the API returns everything to anyone |
| Auth marker | `data-subscriber="true"` in server HTML | `window.isSubscriber = true` in server HTML |
| Server posture | serve what the payload contains, label it | serve it — one tier, no auth machinery; the constraint is request cost, not access |
| Cache unit | category (`args.cid`), tiered, append-only | model-year, single-tier by construction |

**Four hard boundaries, each owned by exactly one module** (§1 below):

1. `wafer` is imported only by `transport.py`; a `WaferResponse` never leaves it.
2. `sqlite3` is imported only by `cache.py`.
3. `mcp` is imported only by `server.py`.
4. The credential file (`session.json`) and `CR_SESSION_COOKIE` are read/written only by
   `credentials.py`.

Everything between those boundaries — extraction, classification, normalization, filtering,
ranking, envelope building — is **pure functions over dicts**, exhaustively testable without I/O.

**Rules that silently corrupt everything if broken.** The docs name these; the tasks below each
cite the one they enforce. If a task's test does not pin one of these, it is not done.

1. "No member session" must never be readable as "CR did not rate this" — scores are `null`,
   never absent; `auth_state`, `session`, `data_tier`, `scores_available` are structural, and
   `scores_available` values are `available | absent | unavailable`, never booleans (SPEC §7,
   CLAUDE.md).
2. Auth is detected from `data-subscriber` (products) / `window.isSubscriber` (cars) in server
   HTML — never from "a cookie is configured", never from null scores, never from "Sign Out"
   (SPEC §7, RECON §5).
3. Check `"/ec/login" in resp.url` (the FINAL url, not `resp.history`) before parsing anything
   (SPEC §7, RECON §10b/§10i).
4. Cookie configured ⇒ session constructed `max_rotations=0, max_failures=None`; assert `hash`
   is in the jar before any `session_expired` classification, else `fetch_failed(identity_rotated)`,
   uncached (SPEC §6).
5. Cache key includes the auth tier; scored rows are never overwritten or pruned away by unscored
   ones; hits are qualified against the **effective tier** derived from `session`, not from
   cookie presence (SPEC §8).
6. Key on `args.cid`, never the requested id or the final URL (SPEC §5, RECON §9h).
7. Rank and compare within `_groupName`; sort on `_overallSortIndex` in both tiers; nest on
   distinct `_groupId` in `data`, never `len(subcats)` (SPEC §5, §7).
8. `detail="standard"` selects by `categoryAttributes.sortOrder` (missing last, tie on
   `attributeId`), never declaration order (RECON §10j).
9. `attributeTypeName` is a homonym: read `attributeDataTypeName` from definitions and
   `attributeTypeName` from product entries (RECON §9h). Coerce by declared type, never by value
   shape; never coerce `text` or `custom`.
10. `dont_buy`, `smart_buy`, `group`, `rank` ship in every product shape (SPEC §7).
11. Derive `scores_available` and `scored` from the whole payload; never hardcode the paywall
    boundary (SPEC §10).
12. Never return, log, or cache a response body; never store a password; cookies never reach the
    cache or tool output (SPEC §6, §12).
13. Cars: cars need no session at all — serve them in full; a cars
    `403` is "no such route", never an auth problem (SPEC §5).

---

## 1. Module layout

```
pyproject.toml                       uv project; entry points; deps mcp>=2,<3 and wafer-py>=0.4.9
src/consumer_reports_mcp/
  __init__.py                        __version__ only
  config.py                          Settings from CR_* env vars (SPEC §9 table) + paths. Pure.
  credentials.py                     BOUNDARY 4. session.json (0600, atomic), CR_SESSION_COOKIE,
                                     cURL/cookie-string parsing, SessionHealth (none/unverified/
                                     active/expired), rotation write-back, `rejected` latch.
  transport.py                       BOUNDARY 1. The one AsyncSession; policy from cookie presence;
                                     FetchResult dataclass; exception→reason classification; login
                                     URL check; jar re-seed/assert; credential_rejected retry-once;
                                     SingleFlight helper. Nothing wafer-typed escapes.
  extract.py                         Pure. HTML bytes → filterInstanceDATA dict, ratings-wrapper
                                     block, data-subscriber marker, <title>, initStore dict,
                                     car-page api key + isSubscriber. Brace matcher lives here.
  ingest.py                          Pure. (FetchResult fields + extract results) → IngestEnvelope
                                     (SPEC §8 payload_json), classification (member/anonymous/
                                     session_expired/credential_rejected/marker_missing/
                                     payload_missing), `scored`, family rows, product_index rows.
  attributes.py                      Pure. Definition join (categoryAttributes → attrs → entry),
                                     coercion table, {id,name,kind,value,raw_value,unit,description}.
  normalize.py                       Pure. Product shapes summary/standard/full, standard-attr
                                     selection, rank, scores_available derivation.
  query.py                           Pure. Filter engine, name/id resolution, sort, nesting,
                                     limit/offset/truncated, filter_on_unavailable_attribute.
  envelope.py                        Pydantic models for every tool's envelope, error model,
                                     auth_state derivation, data.notice rule. No I/O.
  cache.py                           BOUNDARY 2. Schema + migrations, category_raw append/select
                                     by effective tier, product_index, category_index (+aliases),
                                     reliability_raw, car_index, car_raw, pruning, parsed-row LRU.
  negcache.py                        In-memory 1h negative cache for drift codes + challenged.
  discovery.py                       A-Z index parser + sitemap parsers (pure) and the two-pass
                                     orchestrator; category id/slug resolution.
  repository.py                      Orchestration: get_category / get_reliability — cache-first,
                                     effective-tier qualification, single-flight, never-downgrade
                                     write, negative cache, refresh, session-health updates.
  reliability.py                     Pure initStore parser + cr_reliability assembly. The isolated
                                     second envelope; the only consumer of reliability_raw.
  tools_products.py                  Bodies of the six products tools as plain async functions
                                     taking a Runtime. No mcp import.
  cars/
    __init__.py
    api.py                           Cars API client over transport: key extraction, endpoints,
                                     403→no_such_route, isSubscriber read.
    index.py                         v1/cr/keys + v2/cr/carTypes parsing (pure) → car_index rows.
    normalize.py                     Pure. Model-year payload → car shapes; cars scores_available.
    tools.py                         cr_car_search, cr_cars, cr_car bodies; detail/request-cost logic.
  runtime.py                         Runtime dataclass wiring settings/credentials/transport/cache/
                                     repository/negcache; build_runtime(); one per process.
  server.py                          BOUNDARY 3. MCPServer, nine tool registrations with spec
                                     descriptions and outputSchema, lifespan→Runtime, stderr logs.
  cli.py                             `consumer-reports-mcp` → server; `auth [--browser|--status|--forget]`.
  browser_auth.py                    P9.4. Optional [browser] extra; captures `hash`, never the password.
  cars/repository.py                 P10.3. v2/cr/cars listing, per-id ratings, request-cost discipline.
scripts/
  build_fixtures.py                  Reads gitignored scratch/, writes content-minimal tests/fixtures/.
  measure_sizes.py                   tiktoken sizing over scratch/fid.json with score-fill (local).
tests/
  conftest.py                        Page/fixture factories, FakeWaferSession, tmp cache, Runtime.
  fixtures/                          Content-minimal, score-free, synthetic names (§4).
  test_*.py                          One file per module, named below.
  live/                              Anonymous network smoke, opt-in via CR_LIVE=1.
```

Import direction is strictly downward: `server → cli/tools → repository → {transport, cache,
discovery, ingest, normalize, query, envelope} → {extract, attributes, credentials, config}`.
An AST check in `tests/test_boundaries.py` (P0.2) enforces the four boundaries mechanically.

---

## 2. Decisions the docs leave open (taken here, one line each)

Each is an implementation choice within the spec, not a change to it. Where a choice exposes a
spec gap it is also listed in §6.

- **D1 Envelopes are pydantic models, not dicts.** Verified against `mcp==2.1.1`:
  `Tool.from_function` derives `outputSchema` from the return annotation, and a `dict[str, Any]`
  return yields only `{"type":"object","additionalProperties":true}` — the typed enums SPEC §7
  wants in `outputSchema` exist only if the return type is a model with `Literal` fields.
- **D2 Per-tool envelope classes, not one class with optional keys.** SPEC §7's "omitted, not
  null" per-tool table is enforced by class shape; `None` is serialised as `null` everywhere else.
- **D3 `sort` parameter defaults to `None`, meaning "CR's default".** The spec distinguishes an
  *explicit* `sort="overallScore"` (cross-group in flat mode) from the default; only a nullable
  parameter can express that. The envelope still echoes `key: "overallScore"`.
- **D4 Tools take a `Runtime` argument; `server.py` wraps them in closures.** Keeps `mcp` out of
  tool bodies so every tool is testable with a fake transport and a tmp database.
- **D5 SQLite calls are synchronous, one connection per call, WAL.** A stdio server serving one
  agent does not need an async DB layer; the biggest operation (a 1.2 MB JSON parse) is tens of
  ms. Parsed envelopes sit in an 8-entry in-process LRU keyed by `category_raw.rowid` so
  `cr_ratings` followed by `cr_product` on the same category parses once.
- **D6 The sitemap pass runs as a background task started in the server lifespan** whenever it is
  absent or past `CR_INDEX_TTL_DAYS`. 195 fetches at the 2 s interval is ~6.5 min; an
  unrecognised id encountered while the pass is pending **awaits the pass** rather than refusing
  (SPEC §5: "until the sitemap pass has run, an unrecognised id is a cache miss, not a refusal").
  `cr_categories`/`cr_search` report `warnings: ["sitemap_pass_pending"]` meanwhile.
- **D7 A sitemap-sourced id that 404s** is marked `category_index.dead_at` and the tool returns
  `error: unknown_category` with `reason: "retired"` — the spec names the behaviour but no code.
- **D8 `cr_search` matches category display name OR slug** (hyphens as spaces). 110 categories —
  including Televisions — have no display name until fetched; name-only matching reproduces the
  exact `unknown_category`-for-TVs failure SPEC §5 describes.
- **D9 "Drop the credential from the jar" on `credential_rejected`** is implemented as: CR has
  already cleared `hash` (RECON §10b) and `credentials.rejected = True` suppresses the pre-request
  re-seed for the rest of the process. wafer 0.4.9 has no cookie-removal API; nothing is invented.
- **D10 The `/ec/login` final-URL check runs in `transport.py` for every `www.consumerreports.org`
  fetch**, not only category pages: in a cookie-configured process the index, sitemaps,
  reliability and car pages all carry `hash`, and a rejected credential redirects any of them.
- **D11 The car page marker updates `SessionHealth`** exactly as `data-subscriber` does. It is a
  positive server-rendered marker for the same `hash` credential; SPEC §7's "only category-page
  fetches update `session`" was written about pages that carry no marker at all.
- **D12 The cars marker/key page is a constant**, `CARS_PAGE_URL =
  https://www.consumerreports.org/cars/acura/rdx/2026/overview/` — the page RECON §11/§13d
  measured. One fetch yields both the api key and `isSubscriber`; the key is never pinned.
- **D13 `cr_cars(detail="summary")` returns what `v2/cr/cars` carries** — identity, safety
  verdict, popular score, fuel economy, incentives — and omits the road-test keys entirely rather
  than nulling them, because on cars a `null` means "CR has no value". `scores_available` on a
  summary envelope reports **only the keys the listing actually carries**; road-test keys appear
  once `detail="standard"` has fetched them. Values are `available`/`absent` only.
- **D14 `car_raw` rows serve every caller** and carry no tier label — cars have one tier (SPEC §5).
- **D15 `car_index` and the cars taxonomy use `CR_INDEX_TTL_DAYS`; `car_raw` uses
  `CR_CACHE_TTL_DAYS`.** No cars TTLs are specified; these are the analogous ones.
- **D16 Cars `isRecommended` is the string `"Y"`/`"N"`** (RECON §11c); coerce to bool, anything
  else → `null` + `warnings: ["coercion_failed:isRecommended"]`.
- **D17 `franchise` is the first path segment of the canonical URL**, so the six products under
  `/cars/` report `franchise: "cars"`. Path prefix is never used for *routing* (CLAUDE.md).
- **D18 Both `data-subscriber` values present** on one page → `marker_missing` (drift), because
  guessing would fail toward `member`.
- **D19 `TooManyRedirects` → `fetch_failed(reason="redirect_loop", retryable=false)`**; the spec's
  reason table omits it.
- **D20 The `auth` validation fetch bypasses the index check** for its constant target `c35183`
  so a cold machine does not need a discovery pass to validate a cookie.
- **D21 Family `groups` are populated only from the fetched category's own `subcats`** (`[]` when
  empty); siblings enriched from another category's page keep `groups: null` — "we have not
  looked" — even when their score range is `known`.

---

## 3. Phases

Task ids are `P<phase>.<n>`. "Done when" is a pytest test (file::name, with the assertion) or a
shell command with its expected output. `pytest` means `.venv/bin/pytest -q`.

### Phase 0 — Skeleton, fixtures, guardrails

Goal: an installable package with a green (trivial) test suite, committed content-minimal
fixtures, and the boundary check wired into lint — so every later task lands on a runnable tree.

- [x] **P0.1 Project skeleton.**
  **Deliverable:** `pyproject.toml` (name `consumer-reports-mcp`, version `0.1.0`,
  `requires-python >= 3.12`, deps `mcp>=2,<3`, `wafer-py>=0.4.9`; dev extra `pytest`,
  `pytest-asyncio`, `ruff`; console scripts `consumer-reports-mcp = consumer_reports_mcp.cli:main`),
  `src/consumer_reports_mcp/__init__.py`, empty modules per §1, `tests/conftest.py`, ruff config
  (line length 100, `select = ["E","F","I","B","UP"]`), `.gitignore` additions (`.playwright-mcp/`
  already present — verify).
  **Governed by:** SPEC §9, CLAUDE.md *Development*, *Git* (version bump rule).
  **Depends on:** nothing.
  **Done when:** `uv venv && uv pip install -e ".[dev]" && .venv/bin/pytest -q` prints
  `1 passed` (`tests/test_package.py` asserts the version), and
  `.venv/bin/consumer-reports-mcp --help` exits 0 listing `auth`.
  *Superseded:* the test originally pinned the literal `"0.1.0"`, which let `__init__.py` drift
  from `pyproject.toml` — the server advertised 0.1.0 at package version 0.1.6. `__version__` now
  comes from `importlib.metadata` and the test compares it to `pyproject.toml`.

- [x] **P0.2 Boundary check in lint.**
  **Deliverable:** `tests/test_boundaries.py` walking `src/` with `ast` and asserting `import
  wafer`/`from wafer` appears only in `transport.py`, `sqlite3` only in `cache.py`, `mcp` only in
  `server.py`, and `os.environ["CR_SESSION_COOKIE"]`/`session.json` string literals only in
  `credentials.py`.
  **Governed by:** §0 boundaries; SPEC §6 (credential handling confined).
  **Depends on:** P0.1.
  **Done when:** `pytest tests/test_boundaries.py` passes, and deliberately adding `import wafer`
  to `cache.py` makes `test_boundaries::test_wafer_only_in_transport` fail.

- [x] **P0.3 Fixture builder and committed fixtures.**
  **Deliverable:** `scripts/build_fixtures.py` reading gitignored `scratch/` (`fid.json`,
  `pages/c37162.html`, `pages/c37154_banks.html`, `pages/c200228_redirect.html`, `rel37162.json`,
  `azindex.json`, `cars/*.json`) and writing `tests/fixtures/`:
  - `category_c37162.json` — ingest-envelope shape (SPEC §8): real `filters` with `data[]`
    trimmed (price ≤ 5 labels, brands ≤ 4, feature `data[]` ≤ 3), real `attrs`, real
    `category_attributes` (all 44 definitions — the join needs them), real `args`
    (`scid/cid/scat/cats/subcats`, drop `analytics`/`env`/`opts`), real `family` blocks (score
    ranges and counts are public, RECON §9h), and **8 synthetic products** across the 3 real
    `_groupName`s: brands `Brand A..D`, models `MODEL-001..008`, synthetic prices (one `null`),
    `_overallSortIndex` monotone within group with one tie, every gated value `null`, one
    `isDontBuy: true`, one `isSmartBuy: true`, one attribute value `"36 - 38"` on a
    `numeric-general` id (coercion failure), one product carrying an `attributeId` with no
    definition anywhere (fallback path), survey scores on 6 of 8.
  - `category_c37154_banks.json` — single group (`_groupId == cid`), empty `categories` filter,
    `price: null` on all, 0 recommended, no survey scores. 4 synthetic products.
  - `category_c200228.json` — 4 `subcats` but products in only 3 (RECON §10g nesting trap);
    `requested_id` differs from `args.cid` to exercise aliasing.
  - `reliability_c37162.json` — `initStore` shape, 3 synthetic brands, one lacking owner
    satisfaction, 2 sibling categories, one with `HasReliabilityData: false`, blurbs replaced by
    one-sentence placeholders.
  - `azindex.html` — 6 anchors with nested `<span>` text, mixed absolute/root-relative hrefs, one
    `&amp;`, one `/cars/` path.
  - `sitemap_products.xml` (2 locs) and `sitemap_28958.xml` (3 category URLs, one with an extra
    path segment, one under `/cars/`).
  - `cars/keys.json` (2 makes × 2 models × 3 model-years, synthetic names, New/Used mix, one
    model-year in two car types), `cars/cartypes.json` (2 types, 3 categories, one category under
    both types with different counts — RECON §13b), `cars/modelyear.json` (full
    `my23133-anon.json` structure with every score, rank and name replaced by synthetic values),
    `cars/carpage_anon.html` / `carpage_member.html` (env block with a 40-char synthetic
    `DRS_CARS_API_API_KEY`, `window.isSubscriber = false|true;`).
  **Governed by:** SPEC §12 (content-minimal, score-free), CLAUDE.md *Development*.
  **Depends on:** P0.1.
  **Done when:** `tests/test_fixtures.py::test_fixtures_are_content_minimal` asserts every
  fixture file is < 120 KB, no `brandName` matches a real brand list (`Bosch`, `LG`, `Samsung`,
  `GE`, `Whirlpool`, `Lexus`, `Acura` …), every `overallDisplayScore` and `numeric-rating-score`
  value is `null` in product fixtures, and no fixture contains `hash=` or `userLicenses`.

- [x] **P0.4 Page factories and fake transport in `conftest.py`.**
  **Deliverable:** `make_category_page(fixture, *, subscriber: "true"|"false"|None,
  fill_scores=False, omit_ratings_wrapper=False, omit_payload=False, title="…") -> bytes` that
  emits `<script>window.filterInstanceDATA = {json};\nconsole.log('[ ratings-wrapper ]',
  {json})</script>` with the `data-subscriber` attribute repeated N times (or absent) and the
  hidden `Sign Out` markup present in **every** variant; `fill_scores` writes deterministic
  synthetic scores (`50 + id % 30`, ratings `1 + id % 5`). `make_reliability_page(fixture)` (no
  marker). `make_car_page(is_subscriber, api_key)`. `FakeWaferSession` implementing `get`,
  `add_cookie`, `get_cookie`, `cookie_scope_summary` with a scripted queue of responses/exceptions
  and a request log; `FakeResponse` exposing exactly the `WaferResponse` attributes transport
  reads (`status_code, url, content, text, headers, history, cookies, challenge_type, elapsed`).
  `tmp_runtime()` building a `Runtime` over a tmp `cr.db`, tmp config dir and the fake session.
  **Governed by:** §0 "fixture tier is determined SOLELY by the marker the factory writes".
  **Depends on:** P0.3.
  **Done when:** `tests/test_conftest.py::test_factory_tier_is_marker_only` asserts that
  `make_category_page(f, subscriber="false", fill_scores=True)` contains
  `data-subscriber="false"` and non-null scores, and `subscriber="true", fill_scores=False`
  contains `data-subscriber="true"` and null scores — the two pages later tests use to prove
  detection ignores scores.

### Phase 1 — Config and credentials

Goal: settings and the credential store, so transport can choose its policy at construction.

- [x] **P1.1 `config.py`.**
  **Deliverable:** `Settings` (frozen dataclass) from `CR_CACHE_DIR`,
  `CR_CACHE_TTL_DAYS=30`, `CR_INDEX_TTL_DAYS=90`, `CR_MIN_REQUEST_INTERVAL_S=2.0`,
  `CR_TIMEOUT_S=120`, `CR_ATTEMPT_TIMEOUT_S=60`; constants `MAX_RESPONSE_SIZE = 64 * 2**20`,
  `CONFIG_DIR = ~/.config/consumer-reports-mcp`, `DEFAULT_CACHE_DIR = ~/.cache/consumer-reports-mcp`,
  `WWW = "https://www.consumerreports.org"`, `CARS_API = "https://cars-api.consumerreports.org/api/cars"`,
  `CARS_PAGE_URL` (D12), `AUTH_PROBE_CATEGORY = 35183`, `NEGATIVE_TTL_S = 3600`,
  `LIMIT_FLAT=25, LIMIT_NESTED=10, LIMIT_MAX=200, FULL_LIMIT_MAX=10`.
  **Governed by:** SPEC §9 *Config surface* (note: rate limit is seconds-between-requests).
  **Depends on:** P0.1.
  **Done when:** `tests/test_config.py::test_defaults` asserts the table values;
  `::test_rate_limit_is_seconds` asserts `Settings(env={"CR_MIN_REQUEST_INTERVAL_S":"0.5"}).min_request_interval_s == 0.5`
  and that no attribute named `rps` exists.

- [x] **P1.2 `credentials.py` — parse, store, precedence.**
  **Deliverable:** `parse_cookie_input(text) -> dict[str,str]` accepting a full "Copy as cURL"
  paste (`-H 'Cookie: …'` / `-H "cookie: …"` / `--cookie`), a bare `Cookie:` header, or a bare
  `name=value; name=value` string, keeping only `hash` and `userLicenses`; `CredentialStore`
  with `load()` (env var wins over file), `save(cookies)` (0600 via `os.open(..., 0o600)`, write
  tmp + `os.replace`), `forget()`, `status()` (tier, `captured_at` age, `hash` present — no
  network), `source: "env"|"file"|None`, `rejected: bool` latch, `record_rotation(name, value)`
  (writes back only when `source == "file"`, only when the value differs, never when rejected).
  `SessionHealth` enum + holder: `none` when no cookie, `unverified` initially with a cookie,
  `active`/`expired` set by callers. `session.json` schema per SPEC §6 (`schema_version`,
  `cookies`, `captured_at`).
  **Governed by:** SPEC §6 (tiers, precedence, store of record, no expiry countdown, no password).
  **Depends on:** P1.1.
  **Done when:** `tests/test_credentials.py::test_parse_curl_keeps_only_hash_and_userlicenses`,
  `::test_env_var_wins_over_file`, `::test_file_mode_0600` (skipped on Windows),
  `::test_rotation_writes_back_only_for_file_source` (env source: file untouched),
  `::test_rotation_not_written_when_rejected`, `::test_health_none_without_cookie_unverified_with`,
  and `::test_no_cookie_value_in_repr_or_logs` (repr/`status()` output never contains the value).

### Phase 2 — Extraction, classification, normalization (pure)

Goal: every byte-to-structure step the whole project rests on, tested against the fixtures,
with no I/O anywhere in the phase.

- [x] **P2.1 `extract.py` — anchors.**
  **Deliverable:** `brace_match(s, start) -> end` (string- and escape-aware);
  `extract_filter_instance(html: bytes) -> dict | None` (anchor `window.filterInstanceDATA`, `=`,
  first `{`, terminator `;\n`, RECON §10e); `extract_ratings_wrapper(html) -> dict | None` (anchor
  `console.log('[ ratings-wrapper ]',`, RECON §10f); `read_subscriber_marker(html) ->
  "true"|"false"|None` (D18 on both); `read_title(html) -> str | None` (first 64 KB, unescaped,
  ≤ 200 chars); `extract_init_store(html) -> dict | None`; `extract_cars_page(html) ->
  CarsPageInfo(api_key, is_subscriber: bool | None)` using the spike regexes
  (`"DRS_CARS_API_API_KEY"\s*:\s*"([^"]{10,60})"` plus the escaped-quote form,
  `window\.isSubscriber\s*=\s*(true|false)`).
  **Governed by:** SPEC §8 (anchor fragility), RECON §10e–§10g, §11a, §13d, §5 (no "Sign Out").
  **Depends on:** P0.4.
  **Done when:** `tests/test_extract.py::test_filter_instance_roundtrip` (factory page → dict
  equal to fixture `filter_instance`), `::test_ratings_wrapper_cat_id_matches_args_cid`,
  `::test_marker_true_false_none_and_both` (both present → `None`),
  `::test_sign_out_string_present_but_ignored` (anonymous page contains `Sign Out` twice and
  still reads `"false"`), `::test_payload_missing_returns_none_on_maintenance_page`,
  `::test_cars_page_key_and_marker`, and `::test_brace_match_handles_escaped_quotes_and_braces_in_strings`.

- [x] **P2.2 `ingest.py` — classification and the ingest envelope.**
  **Deliverable:** `classify(final_url, status, html, cookie_configured) -> Classification`
  with `kind ∈ {credential_rejected, member, session_expired, anonymous, marker_missing,
  payload_missing}` evaluated in SPEC §7 order (URL first; `payload_missing` beats
  `marker_missing`; carries `http_status` and `title` on the drift kinds; `empty_category`
  warning when all four keys present and `data` empty; `attribute_dictionary_missing` warning
  when the wrapper is absent). `build_envelope(...) -> IngestEnvelope` matching SPEC §8
  `payload_json` exactly (`schema_version=1`, `filter_instance`, `category_attributes` from
  `.cat.categoryAttributes`, `family[]` from `.cat` + `.productFilterPayload.debug.categories[]`
  with `score_min/max`, `rated_count` (`ratedModelsCount`, else null), `has_reliability_data`,
  `groups` per D21, `supercategory` from `.scat`, `data_subscriber`, `final_url`, `requested_id`
  when ≠ `args.cid` else null, `http_status`). `is_scored(filter_instance) -> bool` and
  `product_index_rows(envelope) -> list[(product_id, category_id, brand, model)]`.
  **Governed by:** SPEC §7 *auth_state detection*, *Partial and empty payloads*; SPEC §8; CLAUDE.md
  `args.cid` rule.
  **Depends on:** P2.1.
  **Done when:** `tests/test_ingest.py::test_auth_matrix` parametrised over the table in §4.2 —
  including `(cookie=True, page subscriber="false", fill_scores=True) → session_expired` and
  `(cookie=True, subscriber="true", fill_scores=False) → member` (detection ignores scores);
  `::test_login_url_beats_body` (`/ec/login` final URL with a full anonymous body →
  `credential_rejected`); `::test_login_in_history_only_is_not_rejection`;
  `::test_payload_missing_wins_over_marker_missing`; `::test_envelope_keys_exact`;
  `::test_requested_id_alias_when_cid_differs` (c200228 fixture); `::test_scored_false_on_anon_true_after_fill`;
  `::test_family_groups_null_for_siblings_list_for_self`.

- [x] **P2.3 `attributes.py` — join and coercion.**
  **Deliverable:** `build_definitions(envelope) -> dict[int, Definition]` joining in the order
  `category_attributes` (`attributeDataTypeName`, `unitName`, `description`, `displayName`,
  `attributeGroup`, `sortOrder`) → `filter_instance.attrs` (`dataType`, keyed `id`) → none;
  `normalize_entry(entry, defs, warnings) -> {id, name, kind, value, raw_value, unit, description,
  group}` where `kind` is the declared type (definition first, else the entry's own
  `attributeTypeName`), coercion table `numeric-rating-score|numeric-general|numeric-price|
  numeric-overall-score → number`, `boolean → "Yes"/"No" → bool`, `text|custom|<unknown> →
  untouched`; a failed coercion keeps `raw_value`, sets `value: null`, appends
  `coercion_failed:<id>`.
  **Governed by:** SPEC §7 *Attribute normalization*; RECON §1, §9h homonym; CLAUDE.md coercion rules.
  **Depends on:** P2.2.
  **Done when:** `tests/test_attributes.py::test_kind_read_from_attributeDataTypeName_not_attributeTypeName`
  (asserts no normalized `kind` equals `"SPEC"`/`"TEST_RESULT"`/`"PRICING"`),
  `::test_text_dial_position_not_coerced` (`"-1"` stays `"-1"`), `::test_boolean_yes_no`,
  `::test_text_yes_no_untouched`, `::test_unit_from_definition_never_inferred` (an entry named
  `Exterior width` with no definition has `unit: None`), `::test_unjoined_entry_kept_with_entry_type`,
  `::test_coercion_failure_keeps_raw_and_warns`, `::test_unknown_type_passes_through`.

- [x] **P2.4 `normalize.py` — product shapes, rank, `scores_available`.**
  **Deliverable:** `standard_attribute_ids(envelope) -> list[int]` (first three
  `numeric-rating-score` definitions sorted by `(sortOrder is None, sortOrder, attributeId)`;
  falls back to `attrs` defs, then to entry types); `rank_table(filter_instance) -> dict[product_id,
  int | None]` (dense rank on `_overallSortIndex` within `_groupId` over the unfiltered group);
  `product_shape(product, detail, envelope, defs, ranks, extra_attribute_ids=()) -> dict`
  producing SPEC §7 *Product shape* exactly — `group`, `rank`, `dont_buy`, `smart_buy`,
  `recommended`, `overall_score` (null never absent) at every level; `standard` adds `ratings`
  (three entries, `value` null when gated), `owner_satisfaction`, `predicted_reliability`; `full`
  adds `retailers`, `retailer_prices`, `attributes` grouped by `attributeGroup`;
  `scores_available(filter_instance, data_tier) -> dict` per SPEC §10 (`any` non-null over the
  whole payload; `absent` vs `unavailable` by tier) for `overall_score`, `attribute_ratings`,
  `owner_satisfaction`, `predicted_reliability`, `recommended_flag`.
  **Governed by:** SPEC §7 *Product shape*, *Tool contracts* (`standard`, `rank`), SPEC §10;
  RECON §10j, §9f (Banks), §9e.
  **Depends on:** P2.3.
  **Done when:** `tests/test_normalize.py::test_standard_ids_identical_after_shuffling_definitions`
  (shuffle `category_attributes` → same three ids), `::test_standard_ids_with_missing_sortOrder_sorts_last`,
  `::test_summary_has_group_rank_dont_buy_smart_buy_overall_score_null`,
  `::test_dont_buy_true_survives_summary`, `::test_rank_dense_within_group_ties_share`,
  `::test_rank_null_when_sort_index_null`, `::test_scores_available_unavailable_anon_absent_member`
  (same all-null payload → `unavailable` with `anonymous`, `absent` with `member`),
  `::test_recommended_flag_available_on_banks_with_zero_true`,
  `::test_scores_available_any_not_all` (6 of 8 survey scores → `available`).

### Phase 3 — Cache

Goal: the SQLite layer whose contract is the tier rules; the mixed-tier matrix lands here.

- [x] **P3.1 Schema and migrations.**
  **Deliverable:** `cache.py` opening `cr.db` (WAL, `PRAGMA user_version`) with:
  `category_raw(rowid, category_id INT, auth_tier TEXT, fetched_at TEXT, scored INT,
  schema_version INT, payload_json TEXT, final_url TEXT)` append-only;
  `product_index(product_id, category_id, brand_name, model_name, PRIMARY KEY(product_id,
  category_id))`; `category_index(category_id PK, slug, display_name, franchise, canonical_url,
  source TEXT CHECK IN ('az','sitemap','payload'), family_id, family_name, score_min, score_max,
  rated_count, has_reliability_data, groups_json, score_range_status, dead_at, first_seen,
  last_seen)`; `category_alias(alias_id PK, category_id)`; `discovery_runs(source PK, fetched_at,
  count)`; `reliability_raw(category_id, fetched_at, payload_json)`; `car_index(model_year_id PK,
  make_id, make, model_id, model, year, states_json, car_types_json, primary_car_type_id)`;
  `car_taxonomy(fetched_at, payload_json)`; `car_raw(model_year_id, fetched_at, schema_version,
  payload_json)` append-only. One connection per call (`contextmanager`).
  **Governed by:** SPEC §8 (tables, append-only, `scored` column, `source`), §5 (aliases).
  **Depends on:** P1.1.
  **Done when:** `tests/test_cache.py::test_schema_creates_and_is_idempotent` (open twice, same
  `user_version`), `::test_category_raw_has_no_update_or_delete_api` (module exposes no function
  that updates/deletes `category_raw` except `prune`).

- [x] **P3.2 Writes: append, index projection, never-downgrade by construction.**
  **Deliverable:** `write_category(envelope, tier, scored, fetched_at) -> rowid` appending a row,
  upserting `product_index`, upserting `category_index` rows for every `family[]` entry
  (`source='payload'` only if absent, enrichment columns always, `score_range_status` `known` when
  a range is present, `none_published` when the family block exists with null range) and
  `category_alias` when `requested_id` is set. `write_reliability(category_ids, payload)` fanning
  out one row per sibling id the payload names.
  **Governed by:** SPEC §8 *Auth tier is part of the cache key* rule 1–2; SPEC §7 *Family
  navigation*; SPEC §7 `cr_reliability` fan-out.
  **Depends on:** P3.1, P2.2.
  **Done when:** `tests/test_cache.py::test_anonymous_write_after_member_leaves_member_row_intact`,
  `::test_alias_recorded_and_row_filed_under_args_cid` (c200228 fixture: row under 200228's
  `args.cid`, alias maps requested id), `::test_family_enriches_seven_siblings_from_one_write`,
  `::test_score_range_status_three_values`, `::test_reliability_fanout_writes_row_per_named_sibling`.

- [x] **P3.3 Selection by effective tier, `superseded_at`, `stale`.**
  **Deliverable:** `select_category(category_id, effective_tier, ttl_days, now) -> Selection |
  None` returning the row per SPEC §8 (member rows qualify for anyone; anonymous rows only for
  effective `anonymous`; newest scored, else newest qualifying), with `superseded_at` (newest
  row of any tier newer than the served one and unscored, else null), `stale` (past TTL), and
  `any_row(category_id)` for the "serve what exists on failure" path (ignores qualification).
  `select_product_row(product_id, effective_tier)` applying the same rule with "still contains
  the product" and pruning dangling `product_index` entries on read. `prune(now)` deleting rows
  past `ttl_days * 3` **never** the newest scored row per category.
  **Governed by:** SPEC §8 *Row selection*, *stale vs superseded_at*, *Retention*; CLAUDE.md
  effective-tier rule.
  **Depends on:** P3.2.
  **Done when:** `tests/test_cache.py::test_mixed_tier_matrix` parametrised over §4.3 (all nine
  rows), `::test_expired_cookie_hits_anonymous_row` (effective `anonymous` from `expired`),
  `::test_unverified_misses_on_anonymous_only`, `::test_superseded_at_set_only_when_newer_unscored_exists`,
  `::test_fetched_at_is_served_rows_not_now`, `::test_prune_keeps_newest_scored_row_even_if_oldest`,
  `::test_product_row_must_contain_product_and_prunes_dangling_index`.

- [x] **P3.4 Discovery, cars and misc tables API.**
  **Deliverable:** `upsert_category_index(rows, source)`, `mark_dead(category_id, now)`,
  `resolve_category(token) -> Resolution` (accepts `cNNNN`, int, numeric str, slug; consults
  aliases; `ambiguous` when a slug hits > 1; `unknown` otherwise), `discovery_status()`,
  `list_categories(franchise, family)`, `search_categories(q)` (name or slug, D8),
  `search_products(q)` over `product_index` capped 25 with per-result `data_tier`/`fetched_at` from
  the newest row containing it; `write_car_index(rows)`, `write_car_taxonomy`,
  `select_car_raw(model_year_id, ttl)`, `write_car_raw`, `search_cars(q)`, `list_cars(car_type,
  category, make, year, state)`; parsed-envelope LRU (D5).
  **Governed by:** SPEC §7 `cr_categories`/`cr_search` contracts, §8 cars tables.
  **Depends on:** P3.1.
  **Done when:** `tests/test_cache.py::test_resolve_accepts_cNNNN_int_str_slug_alias`,
  `::test_resolve_ambiguous_slug_lists_candidates`, `::test_search_categories_matches_slug_without_display_name`
  (sitemap-only row `televisions` found by `"television"`), `::test_lru_parses_once` (two reads
  of the same rowid call `json.loads` once — patch it).

### Phase 4 — Transport (wafer)

Goal: the only wafer-facing code — one session, policy fixed at construction, every failure
classified by exception type, no response body escaping.

- [x] **P4.1 Session construction and `FetchResult`.**
  **Deliverable:** `build_session(settings, cookie_configured) -> AsyncSession` with
  `rate_limit=settings.min_request_interval_s`, `timeout=settings.timeout_s`,
  `attempt_timeout=settings.attempt_timeout_s`, `max_response_size=MAX_RESPONSE_SIZE`, and
  `max_rotations=0, max_failures=None` iff a cookie is configured (wafer defaults otherwise);
  `seed_cookies(session, cookies)` injecting `f"{name}={value}; Domain=.consumerreports.org;
  Path=/; Secure"` at `WWW`; `FetchResult(status, final_url, content: bytes, title: str | None,
  challenge_type, elapsed, redirected: bool)` — no `WaferResponse`, no headers, no cookies;
  `Transport` class holding the session, lazily built under an `asyncio.Lock` (llms.txt recipe 14).
  **Governed by:** SPEC §6 *Driving this through wafer* 1–4, SPEC §9, CLAUDE.md *HTTP*.
  All names verified present at the `v0.4.9` tag of `llms.txt`: `add_cookie`, `get_cookie`,
  `cookie_scope_summary`, `max_response_size`, `attempt_timeout`, `resp.history`,
  `resp.challenge_type`, `resp.url`, `RequestBlocked`, `ResponseTooLarge`, `EmptyResponse`, and
  the no-rotation return semantics. `add_cookie`/`get_cookie` are **sync** on `AsyncSession`.
  **Depends on:** P1.2, P0.4.
  **Done when:** `tests/test_transport.py::test_policy_from_cookie_presence` (patch the session
  class, assert kwargs `max_rotations=0, max_failures=None` with a cookie and absent without),
  `::test_timeout_always_paired_with_attempt_timeout`, `::test_cookie_injected_with_domain_path_secure`,
  `::test_fetch_result_has_no_body_text_or_headers_attributes`, `::test_single_session_per_process`.

- [x] **P4.2 `fetch()` — classification, jar assertion, rejection retry, rotation write-back.**
  **Deliverable:** `async fetch(url, *, headers=None) -> FetchResult` raising `FetchFailed(reason,
  retryable, detail)` where `reason` is derived **from the exception type**:
  `ConnectionFailed→connection_failed`, `WaferTimeout→timeout`, `EmptyResponse→empty_response`,
  `ResponseTooLarge→too_large`, `TooManyRedirects→redirect_loop` (D19); returned 5xx →
  `server_error`; returned empty body → `empty_response`; `Challenged(challenge_type, status)`
  for `ChallengeDetected`/`RequestBlocked`/`RateLimited` **or** a returned 403/429 **or** any
  response with `challenge_type is not None`. For `www.consumerreports.org` URLs: before the
  request, if a cookie is configured and not `rejected` and `get_cookie("hash", WWW) is None`,
  re-seed; after the response, if `"/ec/login" in final_url` → set `credentials.rejected`,
  `SessionHealth.expired`, and **retry once** (no re-seed), returning the retry with
  `credential_rejected=True` on the result; if the body reads `data-subscriber="false"` with a
  cookie configured and `get_cookie("hash", WWW) is None` and not rejected →
  `FetchFailed("identity_rotated", retryable=False)`; then `record_rotation("userLicenses",
  get_cookie("userLicenses", WWW))` when a cookie is configured. `SingleFlight` (dict of futures
  keyed by caller-supplied tuple).
  **Governed by:** SPEC §6 (1–3), SPEC §7 *Error taxonomy* (reasons table, "challenge with a
  200", `EmptyResponse` mapping, retries live in wafer), RECON §10b/§10c/§10i, CLAUDE.md *HTTP*.
  **Depends on:** P4.1, P2.1 (marker read for the jar assertion).
  **Done when:** `tests/test_transport.py::test_reason_from_exception_type` parametrised over the
  six exceptions, `::test_5xx_returned_is_server_error_retryable`, `::test_challenge_with_200_is_challenged`,
  `::test_403_under_no_rotation_is_challenged_not_fetch_failed`, `::test_login_final_url_sets_rejected_and_retries_once_without_reseed`
  (fake session: first response `url=…/ec/login?error`, second anonymous page; request log length
  2; `add_cookie` not called between), `::test_login_in_history_only_is_healthy`,
  `::test_identity_rotated_when_hash_missing_after_logged_out_page`, `::test_reseed_before_request_when_hash_missing`,
  `::test_userlicenses_rotation_recorded_from_jar_not_response`, `::test_no_retry_by_server_after_wafer_error`
  (request log length 1 on `ConnectionFailed`), `::test_fetch_failed_detail_never_contains_body`.

### Phase 5 — Discovery

Goal: the two-pass category index — A-Z for names, sitemaps for coverage — plus resolution.

- [x] **P5.1 Parsers (pure).**
  **Deliverable:** `parse_az_index(html) -> list[IndexRow(id, path, display_name)]` iterating
  `<a …href="…/cNNNN/…">…</a>`, stripping inner tags, unescaping entities, normalising absolute and
  root-relative hrefs; `parse_sitemap_index(xml) -> list[url]`; `parse_category_sitemap(xml) ->
  list[IndexRow(id, path, display_name=None)]` via `<loc>` and `/c(\d{4,6})/?$`; `slug_of(path)`
  (last segment before `/cNNNN/`), `franchise_of(path)` (D17).
  **Governed by:** SPEC §5 *Category discovery*, SPEC §10 (nested `<span>` trap), RECON §8,
  §9h, §12b.
  **Depends on:** P0.3.
  **Done when:** `tests/test_discovery.py::test_az_parses_all_six_including_nested_span_and_entities`
  (the naive `>([^<]+)</a>` regex is asserted to find fewer), `::test_sitemap_ids_slugs_franchises`
  (`/cars/...` row → `franchise == "cars"`), `::test_slug_is_last_segment_before_id`.

- [x] **P5.2 Two-pass orchestrator and background sitemap pass.**
  **Deliverable:** `ensure_az_index(refresh)` (one fetch; upsert `source='az'`; records
  `discovery_runs`), `run_sitemap_pass()` (195 fetches through the shared session; upsert
  `source='sitemap'` for new ids; `last_seen` for known; records the run; failures of individual
  sitemaps are logged to stderr and do not abort), `start_background_sitemap_pass(runtime)`
  returning an `asyncio.Task` guarded so it runs once per process, `await_sitemap_pass()`;
  `resolve(token)` = `cache.resolve_category` → if unknown and the pass is pending, await it and
  retry → `unknown_category` only when `discovery_runs` has both sources.
  **Governed by:** SPEC §5 (sitemaps are the authority; `unknown_category` only after both), D6.
  **Depends on:** P5.1, P4.2, P3.4.
  **Done when:** `tests/test_discovery.py::test_az_then_sitemap_union_is_superset_with_sources`,
  `::test_unknown_only_after_both_passes` (before the pass: resolution awaits the pass; after:
  `unknown`), `::test_display_names_only_from_az_until_payload` (sitemap-only row has
  `display_name None`, then set from `.cat.productGroupName` after a `write_category`),
  `::test_sitemap_pass_runs_once_per_process`.

### Phase 6 — Repository (orchestration)

Goal: cache-first category and reliability retrieval with the auth-state machine, single-flight,
negative cache and never-downgrade — the module every tool calls.

- [x] **P6.1 `negcache.py`.**
  **Deliverable:** `NegativeCache.put(key, code, now)`, `.get(key, now) -> code | None` with 1 h
  TTL; accepts only `payload_missing`, `marker_missing`, `reliability_payload_missing`,
  `challenged` (asserts otherwise).
  **Governed by:** SPEC §7 *Partial and empty payloads* (drift negatively cached; `fetch_failed`
  never).
  **Depends on:** P1.1.
  **Done when:** `tests/test_negcache.py::test_expires_after_hour`, `::test_rejects_fetch_failed`.

- [x] **P6.2 `repository.get_category(token, *, refresh=False) -> Served | ToolError`.**
  **Deliverable:** resolve token (P3.4; `unknown_category`/`ambiguous_category`/retired per D7;
  awaits the pending sitemap pass when unresolved, D6) → effective tier from `SessionHealth`
  (`active|unverified→member`, `none|expired→anonymous`) → `select_category`; hit and not
  `refresh` → serve. Otherwise, unless negatively cached, single-flight on `(category_id,
  effective_tier)`: `transport.fetch(canonical_url)` → `classify` → on `member`/`anonymous`/
  `session_expired`: update `SessionHealth` (only these kinds; `credential_rejected` already did),
  `write_category` (tier from marker, `scored` from payload), serve the row the selection rule now
  picks (never-downgrade means the new row may not be the served one → `superseded_at`). On
  drift kinds: negative-cache, then `any_row` → serve with `warnings += [f"refresh_failed:{code}"]`
  else `ToolError(code, http_status, title)`. On `FetchFailed`/`Challenged`: same fallback, error
  carries `reason`/`retryable`. `Served` carries `envelope`, `data_tier`, `fetched_at`, `cr_url`
  (=`final_url`), `from_cache`, `stale`, `superseded_at`, `warnings`, `session`. A sitemap-sourced
  id returning 404 → `mark_dead` + `unknown_category(reason="retired")`.
  **Governed by:** SPEC §7 *auth_state*, *Partial and empty payloads* ("a cached row is served
  with `error: null` whenever one exists"), SPEC §8 *Row selection*, *Concurrency*; CLAUDE.md
  effective-tier and never-downgrade rules; SPEC §5 (retired ids, validate before fetch).
  **Depends on:** P4.2, P3.3, P3.4, P6.1, P2.2.
  **Done when:** `tests/test_repository.py::test_auth_state_matrix_end_to_end` (§4.2 rows through
  the fake transport: asserts `data_tier`, `session`, cached-or-not, `error`),
  `::test_session_expired_page_is_served_as_data_with_error_null`,
  `::test_credential_rejected_falls_back_to_cache_then_anonymous_retry`,
  `::test_dead_cookie_costs_one_probe_then_hits_anonymous_rows` (second call: request log
  unchanged), `::test_unverified_member_row_hit_makes_no_request`, `::test_single_flight_two_concurrent_calls_one_fetch`,
  `::test_refresh_appends_never_promotes` (anon refresh over member row → `superseded_at` set,
  member `fetched_at` served), `::test_drift_negative_cached_then_stale_row_served_with_refresh_failed`,
  `::test_fetch_failed_not_negatively_cached` (second call fetches again),
  `::test_unknown_id_before_fetch_makes_no_request`, `::test_sitemap_404_marks_dead`,
  `::test_reliability_fetch_never_changes_session_health`.

- [x] **P6.3 `repository.get_reliability(token, *, refresh=False)`.**
  **Deliverable:** cache hit on `reliability_raw` else URL from a cached category envelope's
  `args.cats[].reliabilityURL` (any tier), else constructed `{canonical_path}/reliability/c{id}/`;
  a 404 on a **constructed** URL with nothing cached → `fetch_failed(url_unresolved)`, never
  `reliability_payload_missing`; `extract_init_store` failure → `reliability_payload_missing`
  (negative-cached); fan-out write. Never fetches a category page.
  **Governed by:** SPEC §7 `cr_reliability` *Fetching and caching*.
  **Depends on:** P6.2.
  **Done when:** `tests/test_repository.py::test_reliability_url_from_cached_args_cats`,
  `::test_reliability_constructed_url_404_is_url_unresolved`, `::test_reliability_never_fetches_category_page`
  (request log contains no category URL), `::test_reliability_fanout_hits_sibling_next_call`.

### Phase 7 — Query engine, envelopes, products tools

Goal: the six products tools, end to end over the fake transport and tmp cache.

- [x] **P7.1 `query.py` — filter engine.**
  **Deliverable:** `resolve_group(fi, value) -> group_id | InvalidFilterValue`; `resolve_brands(fi,
  values)` (names case-insensitive exact-after-trim or ids; `unknown_filter`/`invalid_filter_value`);
  `resolve_attribute(defs, key)` matching `attributeId`, `name` or `displayName`, raising
  `ambiguous_filter_name` with both ids when two definitions match; `apply_filters(products,
  spec, defs, envelope, data_tier) -> list | ToolError` for `group`, `brands` (OR), `price_min/max`
  (over `product.price`), `recommended` (true/false both filter), `features` (numeric `[min,max]`,
  boolean `true/false/"Yes"/"No"`, text exact); before filtering on any attribute or on price,
  `filter_on_unavailable_attribute(name, reason)` when every value in the **served payload** is
  null, `reason` = `unavailable` if `data_tier == anonymous` else `absent`.
  **Governed by:** SPEC §7 *Tool contracts* (filters), *Filter taxonomy*; RECON §9f (Banks no price).
  **Depends on:** P2.3, P2.4.
  **Done when:** `tests/test_query.py::test_brand_by_name_and_id_or_within`, `::test_price_range_local`,
  `::test_recommended_false_filters_to_non_recommended`, `::test_boolean_feature_accepts_true_and_Yes`,
  `::test_rating_filter_anonymous_is_filter_on_unavailable_attribute_unavailable`,
  `::test_rating_filter_on_retained_member_row_works_for_anonymous_caller` (data_tier member →
  filter applies, no error), `::test_banks_price_filter_is_absent_not_gated`,
  `::test_ambiguous_name_lists_both_ids`, `::test_name_and_displayName_of_same_def_not_ambiguous`,
  `::test_unknown_filter_is_error_not_dropped`.

- [x] **P7.2 `query.py` — sort, nest, page.**
  **Deliverable:** `order(products, sort, order, flat, multi_group) -> (list, SortInfo)`: within
  group `_overallSortIndex` asc (desc order reverses); `price` nulls last both directions;
  default flat = `(group sortOrder, rank)` with `scope: within_group`; explicit `overallScore` +
  flat + multi-group → `overallDisplayScore` desc, nulls last, `scope: cross_group`, `notice` text;
  `nest(products) -> groups` keyed on distinct `_groupId` present in `data`, group order by
  `subcats.sortOrder` then name; `page(items, limit, offset) -> (slice, total, truncated)` with
  `truncated = offset + limit < total`, empty slice with real `total` when `offset >= total`;
  `size` from the unfiltered group; `limit` default 25 flat / 10 nested, max 200, `full` capped 10
  (excess → `invalid_filter_value` naming the cap).
  **Governed by:** SPEC §5 *Ranking*, SPEC §7 *cr_ratings output*, *Tool contracts* (`sort`,
  `group_mode`, `limit`/`offset`, `full` cap); RECON §9g, §10g.
  **Depends on:** P7.1.
  **Done when:** `tests/test_query.py::test_within_group_order_identical_with_and_without_scores`
  (fixture vs `fill_scores` variant → same order), `::test_never_sorted_on_overallDisplayScore_within_group`
  (perturb display scores, order unchanged), `::test_nested_count_from_data_not_subcats`
  (c200228: 3 groups not 4), `::test_single_group_is_flat_and_group_named`, `::test_flat_default_is_page_order_scope_within_group`,
  `::test_flat_explicit_overallScore_multi_group_is_cross_group_with_notice`,
  `::test_price_nulls_last_both_directions`, `::test_rank_not_renumbered_under_filter` (Brand B's
  best keeps `rank: 3`), `::test_size_vs_total`, `::test_truncated_and_offset_past_total`,
  `::test_limit_per_group_nested`, `::test_full_capped_at_10`.

- [x] **P7.3 `envelope.py`.**
  **Deliverable:** pydantic models: `Provenance`, `SortInfo`, `ScoresAvailable` (products keys,
  `Literal["available","absent","unavailable"]`), `ToolError(code, message, reason?, retryable?,
  http_status?, title?, candidates?, unlocks?)`, and one envelope class per tool matching SPEC §7
  *Envelope fields by tool* (omitted keys are not fields): `RatingsEnvelope`, `ProductEnvelope`,
  `FiltersEnvelope`, `ReliabilityEnvelope` (`auth_state` fixed `"anonymous"`, survey-only
  `scores_available`, no `session`/`sort`/`superseded_at`), `CategoriesEnvelope`, `SearchEnvelope`
  (`session` + index provenance only), cars envelopes (P10). `auth_state(data_tier, session)`
  per SPEC §7; `maybe_notice(scores_available, auth_state) -> str | None` firing only when
  `overall_score == "unavailable"`; `warnings` always a list; `data` never populated alongside a
  non-null `error`.
  **Governed by:** SPEC §7 *Response envelope*, *Envelope fields by tool*, *Two structural guards*;
  D1, D2.
  **Depends on:** P2.4.
  **Done when:** `tests/test_envelope.py::test_eight_keys_on_ratings`, `::test_auth_state_derivation_table`
  (`member/expired→member`, `anonymous/expired→session_expired`, `anonymous/unverified→anonymous`),
  `::test_notice_only_when_unavailable` (member all-null → no notice),
  `::test_reliability_envelope_has_no_session_or_sort_field`, `::test_output_schema_has_enums`
  (`RatingsEnvelope.model_json_schema()["properties"]["auth_state"]["enum"]` is the 3-list),
  `::test_error_and_data_mutually_exclusive`.

- [x] **P7.4 `cr_ratings`, `cr_filters`, `cr_product`.**
  **Deliverable:** `tools_products.cr_ratings(rt, category, group=None, brands=None,
  price_min=None, price_max=None, recommended=None, features=None, sort=None, order="desc",
  group_mode="nested", attributes=None, limit=None, offset=0, detail="standard", refresh=False)`
  composing P5.2 → P7.1 → P7.2 → P2.4 → P7.3; `cr_filters(rt, category, refresh=False)` returning
  each CR filter with `parameter` mapping (`type`→null, `sort`→null, `custom`→`recommended`,
  `categories`→`group`, `price`→`price_min/price_max`), numeric `data[]` collapsed to `{min,max}`,
  non-numeric capped at 12 with `values_truncated`, feature definitions with `unit`,
  `description`, `group`, `kind`; `cr_product(rt, id, include_descriptions=False, refresh=False)`
  via `select_product_row` (refresh refetches only the chosen category), `unknown_product` with
  the suggestion text.
  **Governed by:** SPEC §7 (all three contracts and sizing decisions), CLAUDE.md `cr_filters`
  collapse rule, `dont_buy` rule.
  **Depends on:** P5.2, P7.2, P7.3.
  **Done when:** `tests/test_tools_products.py::test_ratings_anonymous_nested_default_with_notice`
  (three groups, `limit 10`, `scores_available.overall_score == "unavailable"`, `data.notice`
  present, every product `overall_score is None` and has `dont_buy`),
  `::test_ratings_member_fixture_no_notice_scores_populated`, `::test_ratings_group_filter_is_flat`,
  `::test_ratings_attributes_projection`, `::test_ratings_empty_category_warning_total_zero`,
  `::test_filters_numeric_collapsed_and_parameter_mapping` (no filter has a `data` list longer than
  12; `features` entries carry `range`), `::test_filters_single_group_has_no_group_values`,
  `::test_product_descriptions_off_by_default_and_on_request`, `::test_product_unknown_id_suggests_search`,
  `::test_product_refresh_refetches_only_selected_category`.

- [x] **P7.5 `cr_categories` and `cr_search`.**
  **Deliverable:** `cr_categories(rt, franchise=None, family=None, refresh=False)`: ensures the
  A-Z index (refresh refreshes only the index), lean rows `{id, slug, name, franchise,
  score_range_status}` unscoped; enriched rows (SPEC §7 shape, `family`/`groups` `null` under
  `not_fetched`) when scoped; `warnings: ["sitemap_pass_pending"]` while D6 runs; provenance =
  index fetch. `cr_search(rt, query, refresh=False)`: categories (name or slug, D8) then cached
  products, substring case-insensitive, no fuzzy, 25 per kind, exact-first-then-alphabetical,
  `searched_categories` always present.
  **Governed by:** SPEC §7 *Family navigation*, `cr_search`; SPEC §5 discovery; RECON §10h sizing.
  **Depends on:** P6.2, P7.3.
  **Done when:** `tests/test_tools_products.py::test_categories_unscoped_is_lean_with_status`,
  `::test_categories_family_enriched_after_one_fetch` (`family=28978` → 8 rows with ranges after
  one c37162 write; `groups` non-null only on the fetched one), `::test_categories_not_fetched_has_null_family_and_groups`,
  `::test_search_cold_cache_finds_category_by_slug_only_row`, `::test_search_product_miss_names_searched_categories`,
  `::test_search_model_number_exact_not_fuzzy` (`MODEL-001` does not match `MODEL-002`).

### Phase 8 — Reliability

Goal: the isolated second envelope.

- [x] **P8.1 `reliability.py` parser and `cr_reliability`.**
  **Deliverable:** `parse_reliability(init_store, category_id) -> {brands[], methodology,
  has_reliability_data, has_owner_satisfaction_data, siblings: [id…]}` reading
  `data.category.surveys.{reliability,ownerSatisfaction}.productGroupSurveyValue[]` (join brands
  by `brandId`, null for a missing survey, CR `sortOrder`), `survey100PtScore` only under
  `detail="full"`, methodology only with `include_methodology`; `cr_reliability(rt, category,
  detail="standard", include_methodology=False, refresh=False)` over P6.3 producing
  `ReliabilityEnvelope` (`auth_state` always `anonymous`; `scores_available` from the brand
  arrays with `absent` never `unavailable`; `has_reliability_data: false` ⇒ `brands: []`).
  **Governed by:** SPEC §7 `cr_reliability` (contract, auth semantics); RECON §9, §9d.
  **Depends on:** P6.3, P7.3.
  **Done when:** `tests/test_reliability.py::test_brand_without_owner_satisfaction_is_null_not_dropped`,
  `::test_auth_state_anonymous_even_with_member_session` (SessionHealth active → envelope
  `auth_state == "anonymous"`, no `session` key), `::test_no_survey_is_structural_false_with_empty_brands`,
  `::test_scores_available_never_unavailable`, `::test_drift_code_is_reliability_payload_missing`,
  `::test_fullscale_only_under_full`.

### Phase 9 — MCP server and CLI

Goal: the process boundary — nine tools registered with spec descriptions and typed output
schemas over stdio, and the `auth` command.

- [x] **P9.1 `runtime.py`.**
  **Deliverable:** `Runtime` dataclass (settings, credentials, health, transport, cache, negcache,
  repository, background tasks) and `build_runtime(settings) -> Runtime`; stderr logging setup
  (`logging.basicConfig(stream=sys.stderr)`, wafer logger at WARNING, a filter that drops any
  record whose message exceeds 2 KB — bodies never reach a log line).
  **Governed by:** SPEC §9 (stderr), §6 (no bodies), §8 *Concurrency* (one session).
  **Depends on:** P5.2, P6.2.
  **Done when:** `tests/test_runtime.py::test_build_runtime_uses_tmp_dirs_and_one_transport`,
  `::test_log_filter_drops_oversized_records`.

- [x] **P9.2 `server.py`.**
  **Deliverable:** `build_server(runtime) -> MCPServer` (`from mcp.server.mcpserver import
  MCPServer`, verified `mcp==2.1.1`; `mcp.server.fastmcp` raises `ModuleNotFoundError`), the
  **six products tools** registered as `@server.tool(name=…, description=<SPEC §7 text
  verbatim>, structured_output=True, annotations=ToolAnnotations(read_only_hint=True,
  idempotent_hint=True, open_world_hint=True))` closures whose return annotations are the P7.3
  envelope classes; a `register_cars_tools(server, runtime)` hook that P10.5 fills; `lifespan`
  that builds the runtime and starts the background sitemap pass; `main()` calling
  `server.run("stdio")` (sync; wraps anyio).
  **Governed by:** SPEC §7 *Tool descriptions*, *Two structural guards* (`outputSchema`), §9.
  **Depends on:** P9.1, P7.4, P7.5, P8.1.
  **Done when:** `tests/test_server.py::test_products_tools_with_spec_descriptions_and_output_schema`
  (`await server.list_tools()` → names ⊇ the six products tools; each `description` equals the
  SPEC §7 table text; each `output_schema` non-trivial; `cr_ratings`'s has `auth_state.enum`),
  `::test_stdio_initialize_and_tools_list` (spawn `.venv/bin/consumer-reports-mcp` with
  `CR_CACHE_DIR` tmp, write newline-delimited JSON-RPC `initialize` + `notifications/initialized`
  + `tools/list`, assert the six names in the reply, stdout carries only JSON-RPC frames),
  `::test_tool_call_returns_structured_content` (call `cr_categories` through
  `await server.call_tool(...)` with a fake transport → `structured_content` has `session`).

- [x] **P9.3 `cli.py` — `auth`, `--status`, `--forget`.**
  **Deliverable:** `consumer-reports-mcp` (no args) → `server.main()`; `auth` reads the paste from
  stdin (or `--paste-file`), `parse_cookie_input`, warns `hash absent — this session will not
  survive` when `hash` is missing, seeds a runtime with the parsed cookies **without saving**,
  runs `repository.get_category(AUTH_PROBE_CATEGORY, refresh=True)` bypassing the index (D20),
  prints `member session active` (classification `member`) or `that cookie did not authenticate
  (<reason>)` (`session_expired`/`credential_rejected`/error code) — on failure nothing is
  written to `session.json`, the fetched anonymous payload is still cached; on success `save()`.
  `--status` prints tier, source, capture age, `hash` present — no network. `--forget` deletes
  the file. Never prints a cookie value.
  **Governed by:** SPEC §6 *`consumer-reports-mcp auth`*, §9 entry points.
  **Depends on:** P9.1, P1.2, P5.2.
  **Done when:** `tests/test_cli.py::test_auth_success_writes_0600_and_caches_payload` (fake
  transport member page), `::test_auth_failure_writes_nothing_but_caches_anonymous_payload`,
  `::test_auth_warns_when_hash_absent`, `::test_status_no_network` (request log empty),
  `::test_forget_deletes`, `::test_output_never_contains_cookie_value`.

- [x] **P9.4 `browser_auth.py` — `auth --browser` (optional `[browser]` extra).**
  **Deliverable:** `src/consumer_reports_mcp/browser_auth.py` exposing
  `async def capture_hash(timeout_s: int = 300) -> str`, plus the `browser` extra in
  `pyproject.toml` (`playwright>=1.40`) and `--browser` / `--timeout` flags on `auth`.
  Flow, in order: import Playwright inside the function and on `ImportError` raise
  `BrowserExtraMissing` carrying the `pip install "consumer-reports-mcp[browser]"` line and a
  pointer to the paste path — never a traceback; `chromium.launch(channel="chrome",
  headless=False)`, and if no Chrome is found say so and name the paste path rather than
  downloading one; **fresh `browser.new_context()` with no `storage_state`** — never the user's
  profile, the same trust line that rejected `rookiepy` (SPEC §6); `page.goto(LOGIN_URL)`;
  best-effort pre-tick `setAutoLogin` via `page.check()`, wrapped so a selector change warns
  rather than fails; then **poll `context.cookies()` every 1 s for a `hash` cookie until
  `timeout_s`**, doing nothing else; return its value; `finally` close context and browser.
  The module **must not reference the username or password fields at all** — no fill, no read,
  no submit. Hand the returned value to the *same* validate-and-save path as the paste (P9.3):
  one code path from validation on.
  **Governed by:** SPEC §6 *`auth --browser`*, §13 (in scope, optional extra), CLAUDE.md.
  **Depends on:** P9.3.
  **Done when:** `tests/test_browser_auth.py::test_missing_extra_gives_install_hint_not_traceback`
  (import patched to raise `ImportError`; message contains `consumer-reports-mcp[browser]`, no
  `Traceback`); `::test_never_references_password_field` — parses the module with `ast` and
  asserts no string literal or attribute matches `/password|username/i` and that `fill` is never
  called; `::test_polls_until_hash_then_returns` and `::test_times_out_cleanly` against a fake
  context whose `cookies()` returns `[]` then `[{"name":"hash","value":"a"*36}]`, both asserting
  context and browser were closed; `::test_launches_headed_with_channel_chrome` asserts launch
  kwargs `channel="chrome", headless=False`; `::test_fresh_context_no_storage_state`. All run
  against a fake Playwright — **no test opens a real browser**.

### Phase 10 — Cars

Goal: the second architecture. The hazard here is request cost, not auth — `detail` makes it the caller's choice and the tests pin it.

- [x] **P10.1 `cars/api.py`.**
  **Deliverable:** `CarsApi(transport, cache, health)`: `page_info(refresh=False) -> (api_key,
  is_subscriber)` fetching `CARS_PAGE_URL` (D12) through the shared session, updating
  `SessionHealth` from `isSubscriber` when a cookie is configured (D11), caching the key in
  memory for the process; `get_json(path, params)` adding `x-api-key` + `accept:
  application/json`; a 403 with `challenge_type is None` → `FetchFailed("no_such_route",
  retryable=False)`, never `Challenged`; endpoints `keys(car_type_id=None)`, `car_types()`,
  `model_year(id)`, `glossary()`.
  **Governed by:** SPEC §5 *Cars* (key at runtime, `403` semantics), RECON §11b, §13d.
  **Depends on:** P4.2, P2.1.
  **Done when:** `tests/test_cars_api.py::test_key_read_from_page_not_pinned` (grep the source
  tree for a 40-char key literal → none), `::test_403_is_no_such_route_not_challenged`,
  `::test_403_with_challenge_type_is_challenged`, `::test_marker_true_sets_active_false_sets_expired`,
  `::test_marker_read_from_html_never_from_cookie_presence` (cookie configured + anonymous page →
  `is_subscriber False`).

- [x] **P10.2 `cars/index.py` and `cr_car_search`.**
  **Deliverable:** `parse_keys(json) -> list[CarIndexRow]` (flatten makes→models→modelYears;
  `states`, `car_types`, `primary_car_type_id` from `isPrimary == "Y"`), `parse_car_types(json)`
  (types → categories with `(car_type_id, category_id)` pairs, counts and bands);
  `ensure_car_index(rt, refresh)` (TTL per D15); `cars.tools.cr_car_search(rt, query,
  refresh=False)` — substring on `"{make} {model} {year}"`, exact-first, 25 cap, anonymous.
  **Governed by:** SPEC §5 (enumeration from `v1/cr/keys`), §7 `cr_car_search`; RECON §13a–§13b.
  **Depends on:** P10.1, P3.4.
  **Done when:** `tests/test_cars_index.py::test_keys_flatten_counts_and_primary_type`,
  `::test_category_scoped_by_car_type_pair_with_different_counts`,
  `::test_car_search_anonymous_makes_no_ratings_call` (request log has no `modelYears`).

- [x] **P10.3 `cars/repository.py` — listing, per-car ratings, request-cost discipline.**
  **Deliverable:** `list_cars(rt, *, car_type=None, make=None, state=None, year=None,
  detail="summary", limit, offset)` calling `v2/cr/cars` with `carTypeSlugName`/`slugMakeName`/
  `modelYearStateId` (they combine; paging works — RECON §13c-ii). **`state` defaults to
  unfiltered — both New and Used** (SPEC §5: 6,698 of 7,095 are Used). Applies `limit`/`offset`
  **after** the fetch because `size` counts models not model-years; `detail="standard"` then calls
  `get_model_year` once per returned row. `get_model_year(rt, id, refresh)` reads/writes `car_raw`
  (no `auth_tier` — one tier, SPEC §5) and never re-fetches a cached row.
  **Governed by:** SPEC §5 *Cars*, §8 `car_raw`, RECON §13c-ii, §13c-iii.
  **Depends on:** P10.2, P6.1.
  **Done when:** `tests/test_cars_repo.py::test_summary_issues_exactly_one_request` (fake
  transport log length 1 for a 20-row listing); `::test_standard_issues_one_request_per_row`
  (10 rows → 11 requests, and 0 extra when all are cached);
  `::test_limit_applied_after_fetch_not_passed_as_size` (asserts no `size=` reflects `limit`);
  `::test_filters_combine` (`car_type` + `make` both appear in the query);
  `::test_state_defaults_to_both` (no `modelYearStateId` in the query unless `state` was passed,
  and a mixed New/Used fixture returns both);
  `::test_cached_model_year_not_refetched`.

- [x] **P10.4 `cars/normalize.py`, `cr_car`, `cr_cars`.**
  **Deliverable:** `car_shape(payload, detail) -> dict` — identity, `state`, `car_type`,
  `category` (`ratingsCategory` pair), `overall_score` (`testRatings.overallTestScore`),
  `road_test_score`, `rank` (`overallRank`), `score_range` (`overallTestScoreMax/Min`),
  `sort_index` (`overallScoreSortIndex`), `recommended` (D16), `predicted_reliability`
  (`model.reliabilityRatings.predictedReliabilityScore`), `owner_satisfaction`, `price` bands,
  `fuel_economy`; `full` adds `ratings[]` (composite + per-test), `feature_group_ratings`,
  `safety_verdict`, `crash_tests`, `specs`, `warranty`; nulls are CR absences (no `null`→gating
  language anywhere); cars `scores_available` values `available | absent` from the payload
  `cr_car(rt, model_year_id, detail="standard",
  refresh=False)`; `cr_cars(rt, car_type=None, make=None, state=None, year=None,
  detail="summary", limit=25, offset=0, refresh=False)`: rows from `v2/cr/cars` via
  `repository.list_cars` (P10.3); `detail="standard"` fetches ratings **only for the returned
  page** (one request per row, single-flight per id, `limit` then defaults to 10 and caps at 15 —
  Claude Desktop kills a tool call at 60 s and 25 rows projected to ~51 s),
  ordered `(make, model, year desc)`. `category` requires `car_type` → else
  `invalid_filter_value`. Summary rows omit the road-test keys rather than nulling them, and
  `scores_available` reports only the keys the listing carries (`available`/`absent` only).
  **Governed by:** SPEC §5 *Cars*, §7 `cr_cars`/`cr_car`; RECON §11c, §11d, §13b–§13c.
  **Depends on:** P10.2, P10.3, P7.3.
  **Done when:** `tests/test_cars_tools.py::test_car_full_shape_from_fixture`,
  `::test_car_nulls_are_absences_not_gating` (a null field yields `scores_available: "absent"`,
  never `"unavailable"`), `::test_summary_omits_roadtest_keys_and_reports_only_listing_keys`,
  `::test_standard_fetches_only_page_of_results` (limit 3 of 6 → exactly 3 `modelYears`
  requests), `::test_cars_category_requires_car_type` (`category` without `car_type` →
  `invalid_filter_value`), `::test_cars_same_category_two_types_differ`,
  `::test_cars_envelope_omits_auth_state_and_data_tier` (asserts the keys are absent, per SPEC).

- [x] **P10.5 Register the cars tools on the server.**
  **Deliverable:** `register_cars_tools(server, runtime)` in `server.py` adding `cr_car_search`,
  `cr_cars`, `cr_car` with `structured_output=True`, the P10.4 envelope classes as return
  annotations, and the three cars descriptions from the SPEC tool-description table **verbatim**.
  None mentions a membership: CR's cars API needs no session, so all three answer in full for
  every caller. What `cr_cars`' description names instead is the request cost — `detail="standard"`
  costs one request per returned row.
  **Governed by:** SPEC §7 *Tool descriptions*, §10 ("each tool ships with its description and
  `outputSchema` in the same commit").
  **Depends on:** P9.2, P10.4.
  **Done when:** `tests/test_server.py::test_registered_tools_match_the_spec`
  (`await server.list_tools()` → exactly the names SPEC lists; `cr_car`'s `output_schema` has
  `error` and `scores_available`), and `::test_stdio_initialize_and_tools_list`. Written when
  there were nine tools; `cr_sign_in` and `cr_auth_status` later made it eleven, so the test
  reads the count from SPEC rather than hard-coding it.

### Phase 11 — Sizing, live smoke, release checks

Goal: the numbers in the spec re-measured on real payloads, one anonymous network smoke, and
the definition-of-done checklist run.

- [x] **P11.1 Sizing pass.**
  **Deliverable:** `scripts/measure_sizes.py` (local only; reads `scratch/fid.json` + the `.cat`
  block from `scratch/pages/c37162.html`, fills scores deterministically, runs the real
  `product_shape`/`nest`/`cr_filters` code, counts with `tiktoken cl100k_base`) printing the
  SPEC §7 tables (`summary/standard/full` × `flat 25 / nested 5 / 10 / 25`, `cr_filters`,
  `cr_product` with/without descriptions, `cr_categories` lean/enriched).
  **Governed by:** SPEC §7 *Response sizing*, §10 *Sizing pass*, RECON §10h.
  **Depends on:** P7.4, P7.5.
  **Done when:** `uv run scripts/measure_sizes.py` prints the table and `standard` nested-10 is
  ≤ 4,000 tokens, `cr_filters` ≤ 4,000, `full` nested-5 (the default) ≤ 20,000; any larger
  figure moves the default in `config.py` and is recorded in the script output.

- [x] **P11.2 Anonymous live smoke (opt-in).**
  **Deliverable:** `tests/live/test_live_anonymous.py`, skipped unless `CR_LIVE=1`, using a real
  `Transport` with **no cookie**: fetch `c35183` → classification `anonymous`, ≥ 1
  `data-subscriber="false"`, payload parses with 4 keys, `.cat._id == args.cid`; fetch the A-Z
  index → ≥ 200 rows; fetch `/sitemaps/products.xml` → ≥ 150 locs and one category sitemap parses;
  fetch `CARS_PAGE_URL` → 40-char key and `isSubscriber is False`; `v2/cr/carTypes` → 7 types;
  `c200228` → `final_url` shorter than requested and `args.cid == 200228`. Never fetches
  `v2/cr/modelYears/`.
  **Governed by:** RECON §10b (no cookie → anonymous page), §10g, §12b, §13d; SPEC §5 cars posture.
  **Depends on:** P9.2, P10.1.
  **Done when:** `CR_LIVE=1 .venv/bin/pytest tests/live -q` passes (≈ 8 requests at the 2 s
  interval), and without the variable prints `skipped`.

- [x] **P11.3 Release checks.**
  **Deliverable:** `README.md` install block unchanged in substance (already matches SPEC §9);
  `pyproject.toml` version confirmed `0.1.0`; `LICENSE` present; `.gitignore` covers
  `session.json`, `cr.db*`, `scratch/`, `notes/`, `.playwright-mcp/` (it does).
  **Governed by:** CLAUDE.md *Git*, SPEC §12, §13.
  **Depends on:** everything.
  **Done when:** `.venv/bin/ruff check src/ tests/ && .venv/bin/pytest -q` is green,
  `git status --porcelain | grep -E 'session.json|cr.db|scratch/'` prints nothing, and
  `grep -rn "fastmcp\|requests\|httpx\|urllib.request" src/` prints nothing.

---

## 4. Test strategy

### 4.1 Fixtures vs live

| Layer | Data | Network |
|---|---|---|
| extract / ingest / attributes / normalize / query / envelope / reliability / cars.normalize | committed content-minimal fixtures + `conftest` factories | none |
| cache / repository / discovery / tools / cli / server | same fixtures through `FakeWaferSession` + tmp `cr.db` | none |
| transport | `FakeWaferSession` scripted responses and exceptions; wafer's real classes only in `test_policy_from_cookie_presence` (constructor kwargs captured by a patch) | none |
| `tests/live/` | real CR, **anonymous only** | opt-in `CR_LIVE=1`, ~8 requests |

No test anywhere uses a member cookie, and CI has none. Member-tier behaviour is proven by the
marker, not by data (4.2).

### 4.2 Building content-minimal fixtures, and the tier of a fixture

CLAUDE.md forbids committing member scores **and** full anonymous payloads. So:

- `scripts/build_fixtures.py` is the only path from `scratch/` to `tests/fixtures/`; it keeps
  real *structure* (filter shapes, all attribute definitions, `args`, family blocks, group names)
  and replaces *content* (8 synthetic products, synthetic brands/models/prices, trimmed value
  lists). P0.3's `test_fixtures_are_content_minimal` pins the size and the absence of real brand
  strings, scores and cookies, so a regenerated fixture that leaks is caught by the suite.
- **A fixture's tier is decided by the `data-subscriber` value the factory writes, never by
  whether scores are filled.** `make_category_page(f, subscriber=…, fill_scores=…)` varies the
  two independently, and the ingest matrix includes the two off-diagonal cells:

| cookie configured | final URL | marker | scores filled | expected |
|---|---|---|---|---|
| no | category | `false` | no | `anonymous`, session `none`, cached anon |
| no | category | `false` | **yes** | `anonymous` — scores are not evidence |
| yes | category | `true` | no | `member`, session `active`, cached member, `scored=False` |
| yes | category | `true` | yes | `member`, `scored=True` |
| yes | category | `false` | no | `session_expired` (auth_state), `error: null`, session `expired`, cached anon |
| yes | `…/ec/login?error` | none | — | `credential_rejected` → rejected latch, retry once, serve anonymous as `session_expired`; nothing from the login response cached |
| yes | category, `/ec/login` in history only | `true` | yes | `member` (healthy re-mint) |
| any | category | none | — | `marker_missing` (payload present) |
| any | maintenance page | none | — | `payload_missing` with `http_status` + `title`; wins over `marker_missing` |
| yes | category | `false`, `hash` gone from jar | — | `fetch_failed(identity_rotated)`, uncached, session untouched |
| any | A-Z / sitemap / reliability page | none | — | session untouched |
| any | any, `challenge_type` set, status 200 | — | — | `challenged`, negative-cached |

Synthetic member-tier data is generated at test time by `fill_scores=True` (`50 + id % 30`) and
never written to disk, so the repository holds no CR score, real or shaped like one.

### 4.3 The mixed-tier cache selection matrix (P3.3 / P6.2)

Rows are `(tier, scored, fetched_at)`; `now = t0`; TTL 30 d.

| rows present | session | effective | expect |
|---|---|---|---|
| anon/unscored t-1d | none | anonymous | hit anon |
| anon/unscored t-1d | unverified | member | **miss** → fetch |
| anon/unscored t-1d | active | member | miss → fetch |
| anon/unscored t-1d | expired | anonymous | hit anon (dead cookie ≠ worse than none) |
| member/scored t-10d + anon/unscored t-1d | none | anonymous | serve member, `fetched_at` t-10d, `superseded_at` t-1d |
| member/scored t-10d + anon/unscored t-1d | active | member | serve member, same provenance |
| member/unscored t-10d + anon/unscored t-1d | none | anonymous | serve anon (newest qualifying, none scored) |
| member/scored t-40d | active | member | hit, `stale: true`; refresh attempt; on failure served with `refresh_failed` |
| member/scored t-10d, then `refresh=true` while anonymous | expired | anonymous | new anon row appended; member row still served; `superseded_at` set |

Plus: pruning at `3 × TTL` never removes the newest scored row per category even when it is the
oldest row; a failed fetch with any row present returns that row with `error: null`.

### 4.4 Cars request cost, without a membership (P10.3)

Cars need no session at all, so this is tested purely on the fake transport's request log:

- `detail="summary"` over a 20-row listing → **exactly one** request.
- `detail="standard"` over 10 rows → **11** requests; re-run with `car_raw` pre-seeded → **1**.
- `limit` is applied after the fetch: no `size=` in the query mirrors it (RECON §13c-ii).
- `car_type` + `make` both appear in the query string, confirming filters combine.
- A cars envelope carries no `auth_state`/`data_tier`, and `scores_available` never says
  `unavailable`.

### 4.5 What is deliberately not unit-tested

wafer's own behaviour (rotation, retries, challenge detection, cookie scoping) — measured in
RECON §10 and exercised only by the opt-in live smoke. The server never re-implements it.

---

## 5. Definition of done (v1)

- [x] All Phase 0–11 tasks checked; `ruff check` and `pytest -q` green; `tests/live` green once
      with `CR_LIVE=1` (2026-09-03: 4 passed, ~8 anonymous requests).
- [x] `claude mcp add consumer-reports -- uv --directory "$PWD" run consumer-reports-mcp` registers
      nine tools, and an agent with no CR knowledge can, anonymously: `cr_categories(franchise=
      "appliances")` → `cr_filters` → `cr_ratings` (nested, `notice` present, every `overall_score`
      null, `auth_state: "anonymous"`) → `cr_product` → cite `provenance.cr_url` + `fetched_at`;
      `cr_search("television")` finds `c28700` on a cold cache after the sitemap pass.
- [ ] `auth --browser` with the extra absent prints the install hint and exits non-zero; with it
  installed, one manual run captures a token and reports `member session active`.
- [x] The same flow with a pasted session: `auth` reports `member session active`; `cr_ratings`
      reports `auth_state: "member"`, populated scores, no `notice`; after `auth --forget` the
      same category is still served from the retained scored row with `superseded_at` once an
      anonymous refresh has happened. (2026-09-04, c35183: 8/8 overall and 24/24 attribute
      scores populated, `notice` null, member row appended beside the anonymous ones. The
      downgrade half was run with an empty credential store over the same cache rather than by
      deleting the real credential — identical code path, and the scored row was still served
      with `superseded_at` naming the newer unscored one. `detail="standard"` returned the same
      three attributes at both tiers, so the shuffled `categoryAttributes` order is handled.
      A bare `hash` paste re-minted and persisted a 197-byte `userLicenses`.)
- [~] `cr_reliability(c37162)` answers anonymously with `auth_state: "anonymous"` and no
      `session` key (verified 2026-09-04, and unchanged under a member session); `cr_reliability` on a `HasReliabilityData: false` sibling returns
      `has_reliability_data: false, brands: []`.
- [x] Cars: `cr_car_search("RDX")` works with no session; `cr_car(23133)` returns a populated
      `overall_score` with no session and no `auth_state`/`data_tier` key; `cr_cars` with
      `detail="summary"` issues one listing request in the common case — `list_cars` pages until
      `need` rows are in hand, so it is a measurement, not an invariant, and the first call of a
      process also spends a `www` fetch for the api-key page (not counted in `requests_made`)
      plus `v2/cr/carTypes` when `car_type=` is passed.
- [x] `grep -rn` of the source tree finds no cookie literal, no api-key literal, no `print(` of a
      body, no `requests`/`httpx`/`urllib.request`, no `fastmcp`. (2026-09-04, plus a full-history
      audit: no credential in any blob on any commit.)
- [x] The sizing script's figures are within the §7 budgets and the defaults in `config.py`
      match them. (2026-09-04: "all within SPEC §7 budgets".)
- [x] `pyproject.toml` version bumped per CLAUDE.md when the first commit is made (with
      permission, no attribution).

---

## 6. Spec issues found while planning

**All 20 were resolved in the docs on 2026-09-03** — every one is now specified in SPEC.md,
RECON.md or CLAUDE.md rather than living as a plan-local decision. The list is kept as the record
of what was found and which way each went; the §2 decisions it references are now spec text.

1. ~~**wafer pin vs the AIA feature.**~~ **RESOLVED 2026-09-03 in SPEC §6/§9 and CLAUDE.md.** The
   docs had called the AIA certificate-chain path (which calls `_rebuild_client` and empties the
   jar) "unreachable by version". PyPI now carries `wafer-py 0.5.0`, whose `v0.4.9..v0.5.0`
   history is exactly that feature, so `wafer-py>=0.4.9` resolves to 0.5.0 on a fresh install and
   the path is reachable **now** — verified, along with `wafer._aia` importing there. The docs now
   say so; `identity_rotated` was already a real branch with a test (P4.2), so nothing in the plan
   changed.
2. ~~**Anonymous `cr_cars` "price bands and fuel economy".**~~
   **→ MOOT.** The premise was that per-model-year fuel economy lived only behind the gate.
   `v2/cr/cars` carries `fuelEconomySpecs` per model-year in the listing itself, and cars are no
   longer gated at all, so there is nothing to source from the taxonomy.

3. ~~**Cars `scores_available` has no value for "not fetched on purpose".**~~ `available`/`absent`
   describe a payload that was read; a refused listing has neither.
   **→ RESOLVED, then MOOT:** a fourth value was added, then removed when cars stopped being
   gated (2026-09-03). Cars report `available`/`absent` only.

4. ~~**Cached `car_raw` rows and later anonymous callers.**~~ SPEC §5/§7 gate the *fetch*; nothing
   says whether a row a member fetched earlier may be served to a `none`/`expired` process on the
   same machine. D14 serves it, matching the products Goal-2 posture. Reversible in one line.
   **→ RESOLVED:** SPEC §8 serves them, matching the products Goal-2 posture.

5. ~~**When the sitemap pass runs.**~~ 195 fetches at `CR_MIN_REQUEST_INTERVAL_S=2.0` is ~6.5 min;
   the spec says "run once and cached" without saying when or what an unresolved id does
   meanwhile. D6: background at startup, resolution awaits it, tools warn `sitemap_pass_pending`.
   **→ RESOLVED:** SPEC §5 specifies background-at-startup, awaited on a miss, with `sitemap_pass_pending`.

6. ~~**Retired categories have no error code.**~~ SPEC §5 says a sitemap id that 404s is marked dead
   and must not raise `payload_missing`, but the taxonomy has no code for the tool response.
   D7: `unknown_category` with `reason: "retired"`.
   **→ RESOLVED:** SPEC §5/§7: `unknown_category` with `reason: "retired"`.

7. ~~**`TooManyRedirects` is not in the `fetch_failed.reason` table.**~~ D19 adds `redirect_loop`.
   **→ RESOLVED:** SPEC §7 adds `redirect_loop`, not retryable.

8. ~~**`cr_search` category matching by display name only**~~ cannot find the 110 sitemap-only
   categories (Televisions, Mattresses) on a cold cache — the failure SPEC §5 calls the worst
   direction. D8 matches slugs too.
   **→ RESOLVED:** SPEC §7 matches slugs too, and §8 makes `display_name` nullable.

9. ~~**"Explicit `sort=overallScore`" vs the default `overallScore`.**~~ The distinction the spec
   draws for cross-group ordering needs a nullable parameter (D3).
   **→ RESOLVED:** SPEC §7 requires the schema default to be `null`.

10. ~~**No spec text for the cars tool descriptions, and no row for the cars tools in the
    envelope-fields-by-tool table.**~~ P9.2 supplies descriptions in the spec's register and the
    cars envelopes carry `auth_state`, `session`, `scores_available`, `provenance`, `warnings`,
    `error`, `data` (no `sort`). Worth adding to SPEC §7.
   **→ RESOLVED:** SPEC §7 adds all three descriptions and a cars envelope table.

11. ~~**Both `data-subscriber` values on one page**~~ is unspecified. D18 treats it as
    `marker_missing`.
   **→ RESOLVED:** SPEC §7: `marker_missing`.

12. ~~**"Only category-page fetches update `session`"**~~ predates cars; the car page carries a
    positive marker for the same credential. D11 lets it update `SessionHealth`. If the owner
    prefers strict reading, `cars/api.py` records the marker but does not touch health — one line.
   **→ RESOLVED:** SPEC §7 now reads "only pages carrying an auth marker".

13. ~~**The `/ec/login` check is written for category fetches**~~ but a rejected `hash` redirects
    every `www.` fetch in a cookie-configured process (index, sitemaps, reliability, car page).
    D10 applies it in transport to all `www.` URLs; the marker step stays category-only.
   **→ RESOLVED:** SPEC §7 applies it to every `www.` fetch, never to `cars-api`.

14. ~~**`category_index` columns.**~~ SPEC §8 lists `(category_id, slug, display_name, franchise,
    source)`, but §7 requires `cr_categories` to serve family, score range, rated count, groups,
    `score_range_status`, the canonical URL, aliases and dead-marking from the index. P3.1
    defines the columns and a `category_alias` table; the spec's schema block is incomplete.
   **→ RESOLVED:** SPEC §8 gives the full schema plus `category_alias`.

15. ~~**Family `groups` for siblings.**~~ `.subcats` describes only the fetched category; the SPEC §8
    envelope example shows `groups` per family entry. D21: siblings keep `groups: null`.
   **→ RESOLVED:** SPEC §8: siblings keep `groups: null`.

16. ~~**Cars `isRecommended` is `"Y"`/`"N"`**~~ (RECON §11c) and the spec never says how to coerce
    it; D16.
   **→ RESOLVED:** SPEC §5 gives it its own mapping.

17. ~~**`auth` validation vs "validate the id against the index before fetching".**~~ On a cold
    machine the `auth` command would otherwise need a discovery pass first; D20 exempts its
    constant target.
   **→ RESOLVED:** SPEC §6 exempts the constant probe category.

18. ~~**Cars TTLs are unspecified**~~ (index, taxonomy, `car_raw`); D15.
   **→ RESOLVED:** SPEC §8: 30-day `car_raw`, 90-day index and taxonomy.

19. ~~**`outputSchema` requires model return types.**~~ Not a spec error, but SPEC §7's "declare
    `outputSchema` on every tool" is only achievable with typed envelope classes under
    `mcp==2.1.1` (a `dict` return yields an untyped object schema). D1/D2 make this structural.
   **→ RESOLVED:** SPEC §7 states the typed-model requirement.

20. ~~**`RECON.md` §11e still lists "whether `v2/cr/modelYears/` accepts multiple ids" as
    unmeasured**~~, while §13c measures it (comma path `400`, filters ignored). Documentation only.
   **→ RESOLVED:** RECON §11e marked answered.

