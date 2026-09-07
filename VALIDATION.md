# Live validation — real member session, every tool

The offline suite proves the code against fixtures. This is the other half: the eleven tools
driven the way a member actually uses them, against Consumer Reports, with the credential
stored by `consumer-reports-mcp auth`. Run it after a release, after a sign-in, and whenever CR
is suspected of having changed something the fixtures cannot show.

```bash
uv run --no-sync scripts/live_scenarios.py            # ~2-4 min, ~30 requests, 2 s apart
uv run --no-sync scripts/live_scenarios.py --anonymous   # the same run with no cookie
```

The runner prints one line per check and exits non-zero on any failure. It reads the same
`session.json` and cache as the server, so a member run also warms the cache the server serves
from. It opens no browser: the sign-in check calls `cr_sign_in` only to confirm it is REFUSED
for a live session (a dead cookie would make it open a window — run `auth --browser` first).

## What "working" means

Every products envelope is held to the paywall-honesty rules, not just to "no exception":

| Check | Member run | Anonymous run |
|---|---|---|
| `auth_state` on a served row | `member` | `anonymous` |
| `scores_available.overall_score` | `available` | `unavailable` |
| `overall_score` on rated products | a number, never null | null on every product |
| `data.notice` | absent | present, names the paywall, never `cr_sign_in` unasked |
| `session` | `active` after the first marker-bearing fetch | `none` |
| `warnings[]` | no `session_expiring` inside 30 days of a fresh capture | no session warnings at all |

Cars are one tier: `scores_available` values are `available`/`absent`, never `unavailable`,
and the envelope carries no `auth_state` at all, in both runs.

## The scenarios

Each is a question a member would ask, and the tool chain an assistant would run for it.

1. **"Which French-door fridge under $2,500 does CR recommend?"**
   `cr_search("french door refrigerator")` → first hit is `c37162` with `match: exact|full` →
   `cr_ratings(c37162, price_max=2500, recommended=True)` → every product recommended, priced
   ≤ 2,500, scored → `cr_product(<top id>)` carries `dont_buy`, the full attribute list and a
   `rank` → `cr_filters(c37162)` collapses numeric ranges to `{min, max}` and stays under ~4k
   tokens.
2. **"Best dishwasher, and which brands hold up?"** — `c28687` is a sitemap-only id, so this
   is the cold-start path CR's A-Z index cannot answer: `cr_ratings("c28687")` serves rows,
   never `unknown_category`; `cr_reliability("c28687")` returns brand surveys with
   `auth_state: "anonymous"` (one tier) and no `session` key.
3. **"A quiet front-load washer"** — `cr_search("washing machines")` resolves by CR's own
   label to Front-Load Washers `c28739`, the category whose `args.cats[]` mixes ids and slugs;
   `cr_ratings("c28739", attributes=["Noise"])` projects the attribute on every row without a
   crash, null where a product lacks it.
4. **"Show me every TV"** — Televisions `c28700` has 303 products: `cr_ratings("c28700",
   group_mode="flat", limit=200)` then `offset=200` reach every product exactly once; `total`
   agrees across pages.
5. **"Mattresses"** — `c28705` is the 14 MB page: it fetches inside the timeouts and serves.
6. **"Is the 2025 RAV4 reliable?"** — `cr_car_search("Toyota RAV4")` → `cr_car(<2025 id>)`
   with `scores_available` reporting `available`/`absent` only → `cr_cars(make="toyota",
   year=2025, detail="standard", limit=3)` costs one request per row and carries road-test
   keys → `cr_cars(car_type="suvs", state="new", limit=5)` lists New only.
7. **"What is my session?"** — `cr_auth_status()` reports `source: file`, `expiry_basis:
   measured`, `days_left_max` close to 365 for a fresh capture; `cr_sign_in()` on the live
   cookie is `refused / session_active` without opening a window.
8. **Errors stay envelopes** — an unknown category is `unknown_category` with a `reason`;
   `cr_product(1)` is `unknown_product`; `cr_cars(make="honda", year=1990)` is
   `invalid_filter_value` with the legal range in `candidates` and no request; `cr_search("x")`
   is an empty answer, not an error; a display-group id (`c200369`) names its owner category.
9. **Discovery** — `cr_categories()` lists 300+ categories with both sources recorded, and
   `cr_categories(family=...)` scopes to a family.

## Running the same checks through an agent (Claude Desktop / Claude Code)

The script exercises the Python API. To exercise the MCP surface itself — the tool schemas,
the descriptions, an assistant's reading of the envelopes — paste the block below into a
conversation whose MCP server has been restarted since the last sign-in. The agent runs the
tools; you read its report against the expected column.

```
Run a validation pass over the consumer-reports MCP tools. For each step call the tool,
then report: the tool called, `session`, `auth_state` (if present), `scores_available`,
the first two `warnings`, and whether the expectation held. Do not call cr_sign_in.

1. cr_auth_status()                          → session active or unverified, expiry_basis measured, days_left_max > 300
2. cr_search("french door refrigerator")      → categories[0].id == "c37162", match exact
3. cr_ratings("c37162", price_max=2500, recommended=true)
                                              → auth_state member, scores_available.overall_score available,
                                                every product has a numeric overall_score, no data.notice
4. cr_product(<first product id from step 3>, include_descriptions=true)
                                              → dont_buy present, rank present, attributes with descriptions
5. cr_filters("c37162")                       → a numeric filter collapsed to {min, max}
6. cr_ratings("c28687")                       → rows served (this id is not in CR's A-Z index)
7. cr_reliability("c28687")                   → brands listed, auth_state "anonymous", no session key
8. cr_search("washing machines")              → "c28739" among the category hits
9. cr_ratings("c28739", attributes=["Noise"], limit=5)
                                              → projected_attributes on every row, no error
10. cr_ratings("c28700", group_mode="flat", limit=200) then offset=200
                                              → 200 + 103 rows, total 303, no overlap
11. cr_car_search("Toyota RAV4")              → hits with model_year_id; no auth_state key on cars
12. cr_car(<newest RAV4 id>)                  → scores_available values only available/absent
13. cr_cars(make="toyota", year=2025, detail="standard", limit=3)
                                              → ≤3 rows, states is a list
14. cr_ratings("c999999999")                  → error.code unknown_category, auth_state null
15. cr_ratings("c200369")                     → unknown_category whose candidates name c37162 and the group
16. cr_cars(make="honda", year=1990)          → invalid_filter_value with a {min, max} in candidates
17. cr_categories()                           → total ≥ 300, no sitemap_pass_pending warning

Finish with a table of step, tool, pass/fail, and the one field that decided it.
```

Anonymous variant: the same block on a server started with no `session.json` (or
`CR_SESSION_COOKIE` unset). Expect `auth_state: anonymous`, `overall_score` null on every
product, `scores_available.overall_score: unavailable`, and a `data.notice` that names the
paywall without telling the agent to call `cr_sign_in`.

## When a check fails

- `auth_state: anonymous` on a member run with `session: expired` → the cookie lapsed
  (RECON §5). Run `consumer-reports-mcp auth --browser`, then re-run.
- `payload_missing` or `attribute_dictionary_missing` on a category that served yesterday →
  CR changed the page. Run the canary: `CR_LIVE=1 .venv/bin/pytest tests/live/test_live_canary.py`
  names which anchor broke.
- `challenged` → stop; the negative cache holds it for an hour. Do not retry in a loop.
- A cars check failing with `no_such_route` on a route that worked → RECON §13; a 403 there is
  a route error, never auth.
