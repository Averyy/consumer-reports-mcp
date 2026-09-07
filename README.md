# consumer-reports-mcp

[![CI](https://github.com/Averyy/consumer-reports-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Averyy/consumer-reports-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://github.com/Averyy/consumer-reports-mcp/blob/main/LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)

Consumer Reports ratings as MCP tools, run locally against your own membership. Nothing is hosted
and nothing is shared.

An [MCP](https://modelcontextprotocol.io) server: it gives an AI assistant — Claude Desktop, Claude
Code, or any MCP client — a set of typed tools for querying Consumer Reports, instead of the
assistant guessing from memory. Beta, and tested on Linux, macOS and Windows on every push.

CR has no member API and no export. Its front end ships each category's whole dataset as JSON
embedded in the page, so one request returns scores, reliability, prices and specs as structured
data.

## Without a membership

The paywall covers two things: the Overall Score and the numeric per-attribute test scores.
Everything else is public. Predicted reliability, owner satisfaction, CR Recommended / Don't Buy /
Smart Buy, prices and retailer spread, full spec sheets, brand reliability surveys, category score
ranges, and exact within-group ranking (CR's ordering field is identical with and without a
session).

Cars need no login at all. The cars API returns Overall Scores, road tests, per-test ratings,
reliability and crash tests in full.

Gated values come back as `null` and are never missing, beside typed `auth_state` and
`scores_available` fields, so an agent can't read "no member session" as "CR did not rate this".
Scores are never estimated.

## Tools

| Tool | Returns |
|---|---|
| `cr_categories(franchise?, family?)` | Categories with ids, slugs, score ranges and family |
| `cr_search(query)` | Which category covers a product, or a model by name/number |
| `cr_filters(category)` | What's filterable and the legal values, with units and descriptions |
| `cr_ratings(category, …)` | Models ranked within CR's display groups: score, price, flags, attributes |
| `cr_product(id)` | One product in full: every scored attribute, specs, retailer prices |
| `cr_reliability(category)` | Brand-ranked predicted reliability and owner satisfaction |
| `cr_car_search(query)` | Make / model / year to a car id |
| `cr_cars(car_type?, make?, year?, state?, …)` | Car model-years with safety verdict, fuel economy, incentives |
| `cr_car(model_year_id)` | One car in full: Overall Score, road test, reliability, crash tests, specs |
| `cr_sign_in(force?)` | Connect or renew your membership. Opens CR's own sign-in page in a browser window, only when you ask |
| `cr_auth_status(wait_s?)` | Session health, days left on the cookie, and the state of a sign-in in progress |

Filtering runs locally over the cached category, so combinations CR's own UI doesn't offer cost no
extra requests.

Ranking is always within a CR display group, because CR renders each group as its own table and
the scores aren't comparable between groups, for members either. A whole-category ordering is still
available, labelled `sort.scope: "cross_group"`.

Every response wears the same envelope: `session`, `warnings`, `error`, `data`, plus `auth_state`,
`scores_available` and `provenance` where they mean something. `warnings[]` is a closed, documented
vocabulary (`empty_category`, `no_results:<filters>`, `session_expiring:<days>`, …) that the server
checks itself against, and `error` is a structured value with a closed `code`, never prose alone.
A bad parameter comes back naming the parameter and the legal values: an unknown brand lists the
brands, a car year outside CR's catalogue gives the span, and a display-group id passed as a
category names the category it belongs to.

## Install

Needs Python 3.12+ and [`uv`](https://docs.astral.sh/uv/). The in-conversation sign-in also
needs an installed Chrome or Edge; pasting a cookie doesn't.

**Claude Desktop.** Build the bundle and open it:

```bash
git clone https://github.com/Averyy/consumer-reports-mcp && cd consumer-reports-mcp && uv sync
uv run scripts/build_bundle.py      # → dist/consumer-reports-mcp-<version>.mcpb — double-click it
```

Desktop brings its own `uv`, installs the pinned release from PyPI inside the bundle and starts it.

**Claude Code**, or any stdio MCP client — the package is on PyPI:

```bash
claude mcp add consumer-reports -- uvx consumer-reports-mcp
```

For the in-conversation sign-in, which needs Playwright, register it with the `[browser]` extra
instead:

```bash
claude mcp add consumer-reports -- uvx --from "consumer-reports-mcp[browser]" consumer-reports-mcp
```

No account needed to start.

## Using your membership

The server holds one session cookie and never your password.

### Signing in from the conversation

Ask Claude to connect your membership. `cr_sign_in` opens your installed Chrome (or Edge) on CR's
own sign-in page in a throwaway window, separate from your normal profile. You type your password
into CR's form, and the server never reads, fills or submits that field. It confirms "remember me"
is ticked, which is what makes the session last a year instead of days, verifies the cookie against
one real request, stores it, and starts using it with no restart. Claude polls `cr_auth_status` for
the result.

The tool isn't read-only, so your client asks before running it. In Claude Code register the
server with the `[browser]` extra (the second `claude mcp add` above). From a terminal the same
flow is `uvx --from "consumer-reports-mcp[browser]" consumer-reports-mcp auth --browser`.

### Pasting a cookie

If no browser window can open, on a headless box or in a container, log in normally and run this in
the DevTools console (`F12`, `⌥⌘J` on macOS):

```js
document.cookie.match(/(?:^|;\s*)hash=([^;]*)/)[1]
```

First time, Chrome and Firefox make you type `allow pasting` before they accept that line. It
prints a 36-character token. Right-click it, **Copy string contents**, then run
`uvx consumer-reports-mcp auth` and paste. It's verified against one real request before saving.

`CR_SESSION_COOKIE=hash=…` works too, and is the Desktop bundle's optional "Session cookie"
setting. It writes nothing to disk and wins over the stored file, so `cr_sign_in` refuses while
it's set and says where to clear it. `auth --status` shows what's stored, `auth --forget` deletes
it.

### Expiry and renewal

A "remember me" `hash` carries a fixed 365-day expiry from the sign-in, and using it doesn't
extend that. The browser sign-in records the cookie's real expiry (and refuses a session-only
one — that is remember-me not having taken); a pasted cookie carries none, so its countdown is
assumed from the capture date. Inside the last 30 days every response carries
`session_expiring:<days>` in `warnings[]`, and `cr_auth_status` shows the same countdown with
`expiry_basis` saying whether it is measured or assumed.

Once it passes, CR rejects the cookie and the server continues anonymously: `session: expired`,
`auth_state: session_expired`, `null` scores, and a notice naming the fix. Scores already cached
keep being served, because a scored row is never replaced by an unscored one.

`cr_sign_in` verifies a stored session before opening anything, so a user who's still signed in
gets a refusal. A cookie CR has rejected, or one past its expiry, is renewed with no flag.
`force: true` replaces a session that's verified live, for renewing early.

### What it won't do

It never asks for a password, automates a login, solves a CAPTCHA, logs a cookie, writes one to the
cache, or echoes a response body. `auth` reads stdin, so nothing lands in your shell history.

Treat the stored session as a long-lived credential: it restores a member session on its own, with
no password and no second factor. It's written `0600` where the platform enforces that. Windows
doesn't. On shared or containerised hosts use `CR_SESSION_COOKIE`, which writes nothing to disk.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CR_SESSION_COOKIE` | unset | Session cookie (`hash=…`); takes precedence over the stored file, and blocks `cr_sign_in` while set |
| `CR_CACHE_DIR` | `~/.cache/consumer-reports-mcp` | SQLite cache location |
| `CR_CACHE_TTL_DAYS` | `30` | Category and car payload TTL |
| `CR_INDEX_TTL_DAYS` | `90` | Category index and cars index TTL |
| `CR_MIN_REQUEST_INTERVAL_S` | `2.0` | Minimum seconds between requests to each CR host, held across concurrent calls; `0` disables |
| `CR_TIMEOUT_S` / `CR_ATTEMPT_TIMEOUT_S` | `120` / `60` | Total and per-attempt fetch budgets |
| `CR_OFFLINE` | unset | `1` serves the cache only and never touches the network |

The session file and cache live under `$HOME`. A container can point `HOME` or `CR_CACHE_DIR`
anywhere writable.

## Scope

346 product categories across 7 franchises, plus 7,095 car model-years. Six franchises are the ones
CR names: `appliances`, `home-garden`, `electronics-computers`, `health`, `money`, `babies-kids`.
The seventh, `cars`, holds six product categories that live under a `/cars/` path: tires, dash
cams, tire stores, electric scooters and so on. Those are products. Vehicles have their own tools.

CR's own A-Z index lists only 236 of the categories. It omits Televisions, Mattresses,
Dishwashers, Bluetooth Speakers and 106 others, so the server reads CR's sitemaps as well. That
pass runs in the background at startup, and a lookup that misses the index waits for it. A pass
that failed, say from being offline at startup, is retried by the next such lookup once the
network is back.

Category at a time, on demand, cached locally.

## Development

```bash
uv sync --extra dev
.venv/bin/pytest tests/ -q            # tests/live is skipped unless CR_LIVE=1
CR_LIVE=1 .venv/bin/pytest tests/live/test_live_canary.py -q   # anonymous, 2 requests: is the
                                      # attribute dictionary still on CR's page?
.venv/bin/ruff check src/ tests/ scripts/
.venv/bin/ruff format --check src/ tests/ scripts/
uv run scripts/build_bundle.py        # the Claude Desktop bundle, version taken from pyproject
```

[`SPEC.md`](https://github.com/Averyy/consumer-reports-mcp/blob/main/SPEC.md) is the design of record, [`RECON.md`](https://github.com/Averyy/consumer-reports-mcp/blob/main/RECON.md) the measured evidence behind
it, [`PLAN.md`](https://github.com/Averyy/consumer-reports-mcp/blob/main/PLAN.md) the plan the code was built from (historical; module docstrings cite its
task ids), and [`MCP-AUTH-PATTERN.md`](https://github.com/Averyy/consumer-reports-mcp/blob/main/MCP-AUTH-PATTERN.md) the in-conversation sign-in written up as a pattern for
other local MCP servers.

## Legal

Not affiliated with or endorsed by Consumer Reports. "Consumer Reports" is a trademark of Consumer
Reports, Inc., used here only to name the service this tool reads.

This tool uses **your own** membership, on **your own** machine, for **your own** use. CR's
[User Agreement](https://www.consumerreports.org/2015/01/user-agreement/) governs that account and
complying with it is your responsibility. Not legal advice.

## License

[MIT](https://github.com/Averyy/consumer-reports-mcp/blob/main/LICENSE)

## Contributing

Issues and pull requests are welcome at
[github.com/Averyy/consumer-reports-mcp/issues](https://github.com/Averyy/consumer-reports-mcp/issues).
Read [`SPEC.md`](https://github.com/Averyy/consumer-reports-mcp/blob/main/SPEC.md) first — it is
the design of record, and most of its rules exist because something measurable went wrong without
them. `ruff check`, `ruff format --check` and the test suite all run in CI on three operating
systems; `CR_LIVE=1` tests hit Consumer Reports anonymously and are opt-in.
