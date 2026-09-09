# consumer-reports-mcp

MCP server exposing a Consumer Reports member's own subscription as structured ratings tools.
Python 3.12+, public (MIT) at `github.com/Averyy/consumer-reports-mcp`, on PyPI.

- `SPEC.md` — design of record. Read the relevant section before changing anything structural.
- `RECON.md` — measured facts about CR's pages, payloads, paywall and auth. Cite it; do not
  re-derive it. New measurements go there, not here.
- `MCP-AUTH-PATTERN.md` — the in-conversation sign-in as a reusable pattern.

## Commands

```bash
uv sync --extra dev --extra browser
.venv/bin/pytest tests/ -x -q                        # tests/live is skipped unless CR_LIVE=1
.venv/bin/ruff check src/ tests/ scripts/            # lint (--fix)
.venv/bin/ruff format --check src/ tests/ scripts/   # CI gates on formatting too
uv run --with tiktoken --no-sync scripts/measure_sizes.py   # response sizing; needs scratch/
uv run scripts/build_bundle.py                       # Claude Desktop .mcpb; build from a released tag
CR_LIVE=1 .venv/bin/pytest tests/live/test_live_canary.py           # 2 anonymous CR requests
CR_BROWSER_LIVE=1 .venv/bin/pytest tests/live/test_browser_hardware.py  # real Chrome, no CR traffic
```

Run lint, format check and the full suite before every commit. Python runs via `uv` only.

## Repository etiquette

- Never commit without being asked. No Claude attribution in commits.
- Bump the patch version in `pyproject.toml` on every change that touches `src/` and run
  `uv lock`; ask before a minor or major bump.
- IMPORTANT: after every push, watch CI to completion (`gh run watch <id> --exit-status`) and
  fix a failure before reporting done. After a tag, watch the Release run the same way, then
  confirm the version on PyPI (`curl -s https://pypi.org/pypi/consumer-reports-mcp/<v>/json`).
  A push is not finished until both are green.
- IMPORTANT: never tag, release, `uv publish`, or change repo visibility from an agent. Those
  are the owner's actions.
- Never commit member-only scores or a full CR payload. Fixtures are anonymous, content-minimal
  and use synthetic brand/model strings.
- Never log, return, or write to the cache a cookie value or a response body.
- MCP tools in this Claude Code session talk to the INSTALLED server, not the working tree.
  Test edits inline with `.venv/bin/python -c` first.

## Non-obvious facts you will otherwise get wrong

Products
- The data source is the category page: a GET embeds `window.filterInstanceDATA`. There is no
  product API to reverse-engineer; do not build against `products-api.consumerreports.org`.
- The A-Z index is incomplete (236 of 346 categories). Sitemaps are the second source and run
  in the background; `unknown_category` is only honest once both have run.
- Key cache rows on `args.cid`, never the requested id: subcategory URLs serve the parent's
  payload. Validate an id before fetching (CR answers a bad id with 500, not 404).
- Rank and compare only within `_groupName`; sort on `_overallSortIndex` in both tiers.
- `categoryAttributes` is reached through a `console.log` left in production; it is the most
  fragile anchor in the project and the live canary exists for it.
- `attributeTypeName` is a homonym (type on definitions, dataType on product entries). Coerce
  by declared dataType, never by value shape; never coerce `text`; never infer a unit.
- A `0` on a `numeric-rating-score` is CR's not-applicable marker, never a score: `value` is
  null, `status` is `not_applicable`, `raw_value` keeps the 0 (`attributes.is_not_applicable`).
  On every other kind a `0` is a measurement and passes through untouched.
- Never derive or estimate a score. A null score is CR's value or a session limitation, never
  "unrated".
- `cr_reliability`'s `cr_url` names the page for the id ASKED FOR. One fetch fans out to every
  sibling, so a row's `final_url` is another category's page as often as not.

Cars
- Cars are a second architecture (`cars-api`, public key read from the car page, no cookie),
  fully available anonymously: a null means CR has no value.
- On `cars-api` a 403 means "no such route", an unknown model-year is an empty 200, and `size`
  counts models, not model-years (cap 50). Read SPEC §5 and RECON §13 before touching paging.

Auth
- Anonymous is a first-class mode, never an error.
- Auth is detected from `data-subscriber` (products) or `window.isSubscriber` (cars) in server
  HTML. Never from a cookie being configured, never from null scores, never from "Sign Out".
- `hash` is the durable credential; `userLicenses` rotates and is written back. Check
  `is_login_url` on the FINAL url as a route before parsing anything.
- Session health moves only through `SessionState` events; there is no setter.
- Claude Desktop kills a tool call at 60 s, so `cr_sign_in` is non-blocking and every await on
  a background pass is bounded. A cookie saved mid-process is a no-op until `Transport.adopt()`.
- The sign-in window must not advertise automation (`LAUNCH_ARGS`); CR's invisible hCaptcha
  keys on `navigator.webdriver`.

Envelope
- Tool returns are typed models, never `dict`. `sort` defaults to null in the schema.
- `warnings[]` and `error.code` are closed registries validated at construction; add new tokens
  to `envelope.py` and SPEC §7 together. Values quoted into messages go through
  `envelope.quoted`, never a bare `!r`.
- `auth_state` is derived from the served row or is null; `provenance` is null or set with it.

HTTP
- Always wafer, pinned `wafer-py>=0.4.9,<0.6`; widening means re-measuring. Never `urllib`,
  `requests` or `httpx`.
- Pair `timeout=` with `attempt_timeout=`; size both for an 11 MB body. One shared session per
  process; `rate_limit=0` because the politeness gate is ours (`Transport._space_out`).
- Classify failures by exception type, never by parsing reason text. A challenge can arrive
  with a 200.

Dev environment
- `CR_OFFLINE=1` fails every fetch fast; the cache still serves.
- `Settings` reads `HOME` from the env mapping it is handed; tests must pass both `env` and
  `home`, or they write `session.json` into the real home.
- Every multi-statement SQLite transaction is `BEGIN IMMEDIATE`.
