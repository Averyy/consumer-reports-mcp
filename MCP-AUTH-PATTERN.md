# In-conversation sign-in for a local MCP server

A pattern for the case where a local MCP server needs a credential that only the user can
produce, and the user may never open a terminal. Written from building it once
(`consumer-reports-mcp`) so it can be built again elsewhere without rediscovering the walls. It
stands alone; nothing here requires reading that project's spec.

The short version: **start the sign-in in the background and return at once; let the client poll
a status tool; make the transport able to adopt the credential while running; annotate the tool so
the client asks first; and design the renewal before the first sign-in works.** Every one of those
clauses exists because the obvious design fails on Claude Desktop in a specific, measured way.

---

## 1. The problem

The server reads a site on the user's behalf. Some of what it reads is behind a login. The site
has no API key and no OAuth; what it has is a session cookie minted when a human signs in through
the site's own form, possibly through a captcha, possibly through a password manager.

The user is on Claude Desktop. They installed the server by opening a bundle. They will never see
a shell, so "run `my-server auth` and paste the cookie" is not an instruction they can follow.
The only surface they have is the conversation, and the only thing the conversation can do is
call tools.

So the credential has to be obtained through a tool call. That sentence contains all of the
difficulty.

The pieces you already have from a terminal-first design still apply and are worth keeping:

- a **credential store** (a file with restrictive permissions, or an env var for headless hosts,
  with a fixed precedence between them);
- a **validation routine** that makes one real request with the candidate credential and answers
  "authenticated", "rejected", or "could not check" — and stores nothing on the last two;
- a **browser capture** that opens the site's own sign-in page in a fresh, throwaway browser
  context, ticks "remember me" if the durable cookie depends on it, polls the context's cookies
  for the one you need, and never reads, fills or submits any form field.

The pattern is about wiring those three into a tool that works under Desktop's constraints.

## 2. Why elicitation is not the answer

MCP has elicitation: a server-initiated request for structured input from the user. It looks like
the right tool and it is not, for two reasons.

The practical one: **Claude Desktop declares no elicitation capability to a local stdio server.**
The request has nowhere to go. You can test this in one line — check the client capabilities the
host advertises on `initialize` — and the answer decides the matter before any design argument.

The principled one: even where it exists, elicitation carries *text the user types into the
client*. The one thing you must never accept through your own surface is a password. A sign-in
flow's whole security story is "the user types their password into the site's form, in a real
browser, and the server only ever sees the resulting cookie." Elicitation would invert that.

## 3. The 60-second wall

**Claude Desktop kills a local tool call at 60 seconds. Progress notifications do not extend
it.** Claude Code has no such limit, which is precisely why you will not notice this while
developing: everything works in the terminal client and dies in the desktop one. The 60 s
figure was measured on one Claude Desktop build (September 2026); it is an observation, not a
documented contract, so re-verify it on the build you target before sizing anything to it.

The measurement that made this concrete was not the sign-in. It was a listing tool that costs
one HTTP request per returned row, behind a 2-second politeness interval: 21.6 s at ten rows,
projecting to ~51 s at the documented maximum of twenty-five — with nothing left for one slow
response. That cap was lowered to fifteen (~35 s worst case). The sign-in is worse: a human
takes as long as they take. A tool that blocks until the cookie appears is a tool that is killed
mid-flow on the one client this feature exists for.

Two consequences:

1. **Any tool that can exceed the cap must be non-blocking.** Start the work, return a status,
   let the client poll.
2. **The poll itself must stay under the cap.** A long-poll that waits up to 45 s leaves margin
   for the round trip; one that waits 60 s is a coin flip.

Audit every tool you have against the cap, not only the sign-in. Any "one request per row"
shape is a candidate.

## 4. The shape: start, poll, adopt

Two tools and a small state machine.

### `sign_in(force=false)` — returns within ~2 s

Give it **the same outer envelope as every other tool in your server** — whatever that is; here
`{session, warnings, error, data}` — with the status object under `data`:

```
session:      the credential's current health (unchanged by this call)
warnings:     the same machine-readable list every tool carries (the renewal warning, §8)
error:        always null — a refusal is a status with a reason, not an error-taxonomy entry
data:
  status:       waiting | verifying | in_progress | refused | failed
  reason:       machine-readable cause for refused/failed, else null
  instructions: the only free-text field — what to do next
  browser:      which installed browser opened, once known
  expires_in_s: seconds until the open window gives up
```

Do not make these two tools flat "because the status IS the answer". That was done here once:
the spec's own by-tool table said every tool carries `warnings`/`error`, the sign-in tool could
not, and the renewal warning that "rides every tool with a session field" was structurally
impossible on the one tool most concerned with renewal. Consistency of the outer shape is what
lets an agent read every response the same way.

It runs a background task: `[verify_stored()] → capture() → validate() → store.save()` →
`transport.adopt()`. The flow object holds a `phase`
(`idle → [verifying →] waiting → validating → active | refused | failed`), a `reason`, and a
change counter with an `asyncio.Event` that pulses on every transition, so pollers wake on
change rather than sleeping through it.

Two details that make the fast return useful rather than merely fast:

- **Wait briefly for the launch to report before answering.** The capture takes an `on_launch`
  callback that fires once a window is up. `sign_in` waits up to ~1.5 s for either that callback
  or a failure. A launch failure ("no browser installed") is therefore answered by the call
  itself, as `status: failed`, not discovered three polls later; a slow launch is reported as
  `waiting` with `browser: null` and the poll fills it in.
- **Idempotent while in flight.** A second call — `force` included — answers `in_progress` and
  opens nothing. One window, not three.
- **The window must not advertise the automation, or the site's captcha will reject a correct
  password.** This is the failure that looks least like itself, so it is worth stating plainly.
  A browser launched by an automation driver sets `navigator.webdriver = true` and passes
  `--enable-automation`. Sign-in forms commonly run an *invisible* captcha on submit that keys
  on exactly that bit, and a flagged submission does not fail as a captcha — the site reports
  it as **wrong credentials**. Measured here on the real login page, with the site's own key:
  `webdriver: true` produced a visible puzzle and no token (2/2 runs); `false` issued a token
  silently in under a second (3/3); nothing else observable differed — same UA, languages,
  timezone, cookies, form. The user typed a correct password, solved a puzzle, and was told
  "we still don't recognize that sign in" — for credentials that worked immediately in a plain
  browser window. Launch with the automation blink feature disabled and the driver's
  automation switch ignored, and let the page see the real window and screen rather than an
  emulated viewport no desktop has. Those are the driver's own options for a human-driven
  window, not a bypass: the captcha still runs and still decides, the human still types their
  own password into the site's own form, and nothing about authentication or authorisation is
  defeated — what changes is that a bot-detection heuristic stops reporting something false.
  Pin the flags with a test, because that constant is also where a future
  `--disable-web-security` would be quietly appended.

### `auth_status(wait_s=0)` — the poll

The same outer envelope, with the facts under `data`:

```
session:       none | unverified | active | expired
warnings:      including the renewal warning (§8)
error:         always null
data:
  source:        env | file | memory | null
  captured_at:   when the stored credential was captured
  days_left_max: an UPPER bound on its remaining life (see §8), or null
  sign_in:       idle | verifying | waiting | validating | active | refused | failed
  reason:        the last refusal's or failure's reason, or null
  browser:       as above
```

`wait_s > 0` long-polls for a phase change, capped at 45 s, and returns immediately when nothing
is in flight. Returning on *any* change (including `waiting → validating`) is fine: the agent
sees progress and polls again.

### The guard: verify, do not refuse blind

The obvious guard — "refuse unless `force` while a credential is stored and not known dead" —
is wrong, and it is wrong in a way that only shows up in month twelve. The reasoning that led
to it was sound as far as it went: under a desktop client every server start is a fresh process
that sits at *unverified* until a fetch that carries the site's logged-in marker, so a guard
that fires only on *active* is dead there and a user who is already signed in gets a window on
every start. That shipped once. The fix — fire on *unverified* too — shipped next, and it has the
mirror-image defect: health only ever leaves *unverified* on a marker-bearing fetch, and in a
fresh process nothing may ever make one (here: the listing, search, survey and car-API tools
never touch the marker). So a **dead** cookie sits at *unverified* forever, the guard refuses it
forever with a message asserting that it works, and the user recovers only by discovering
`force`. That makes the guard advisory rather than protective, and renewal — the case the whole
feature exists for — is the case it breaks.

The resolution is to **verify** an *unverified* credential rather than guess about it. You
already have the validation routine; it costs one request. Unforced:

- *none* or *expired*: proceed.
- past the credential's own lifetime bound (`days_left_max <= 0`, §8): proceed, whatever the
  health says — your own arithmetic says it cannot be live.
- *active* (a marker-bearing fetch in this process saw it live): refuse, `session_active`. The
  message may say the credential works, because that is known.
- *unverified*: start the background task in a `verifying` phase that runs the validation
  routine against the **stored** credential. `authenticated` → mark health active (the probe is a
  marker-bearing fetch) and report `refused / session_active` through the poll. `rejected` →
  mark health expired and go on to open the window. `could_not_check` or anything else → no
  verdict, so no window on a guess: `failed` with that reason. The call still answers inside its
  launch wait when the probe is quick and says `verifying` when it is not.

`force` then means exactly one thing: replace a credential verified live.

### The other refusals, and why each exists

- `env_override`: an environment variable that takes precedence over the stored file means a
  captured cookie would be silently ignored on the next start. Refuse, and **say where to clear
  it** — the extension's settings field in Desktop, the env block of the MCP server entry, the
  shell. Treat a blank value as not set, because a Desktop config field left empty arrives as
  `""` — and treat an unexpanded `${…}` template the same way, so a host that passes the
  manifest's placeholder through for an empty field cannot make the refusal permanent.
- `browser_extra_missing`: name the exact install command and the terminal fallback. Check with
  `importlib.util.find_spec`, not an import — the check must cost nothing. Check it before any
  verification probe is spent.
- `offline`: if the server has a no-network mode, validation cannot run; say so before opening a
  window.

### The failures the poll reports

`browser_not_found`, `window_closed` (distinguish it from `capture_timeout` — the user did
something), `capture_timeout`, the validation verdicts (`session_expired`, `credential_rejected`:
nothing stored), `could_not_check:<reason>` (a transport failure is not a verdict: nothing
stored, and the user has to sign in again — accept that rather than storing an unverified
cookie; from the pre-flight check it also means no window was opened), `save_failed:<type>`,
`internal_error:<type>` (a catch-all around the whole task — without one, an unexpected
exception after validation leaves the phase at `validating` forever with the task done, and a
poll that never resolves), `cancelled` (server shutdown cancels the task; nothing stored — and
have the task read `asyncio.current_task().cancelling()` before its one irreversible step, the
save, because a library between the task and its await can swallow the `CancelledError` and the
task would otherwise store a credential and report `active` after the caller was told
`cancelled`).

## 5. Live credential adoption

This is the part that is easy to get wrong and hard to notice.

### The problem

A well-built transport chooses its behaviour *from the credential* at construction time. In this
project: with a cookie configured, the HTTP session is built with identity rotation disabled
(`max_rotations=0, max_failures=None`), because a rotation empties the cookie jar and turns one
transient 403 into a false "your session expired". Without a cookie, the full rotation ladder is
on. The kwargs are constructor-only, and a second session is not the workaround — the politeness
rate limit is per session, so two sessions would double the request rate.

So `cookie_configured` is read once, the session is built once, and **a cookie saved mid-process
is a no-op until restart.** The file is written, `auth_status` reports it, the next request goes
out on the old session with no cookie, and the user sees `null` scores after a sign-in that said
it succeeded.

### The shape of the fix

One method on the transport, `adopt()`, that makes the store the source of truth again. In
sketch — the names are this project's; the shape is the point:

```python
async def adopt(self) -> bool:
    async with self._lock:
        configured = store.has_credential()   # re-read the STORE, not an argument
        old, self._session = self._session, None    # the next request rebuilds
        self.policy = policy_for(configured)        # whatever the rebuild derives from it
        reset_rejection_latch()                     # a rejection was about the OLD credential
        reset_health_to_cold_start(configured)      # "unverified" with one, "none" without
        self.generation += 1                        # the generation counter (below)
    if old is not None:
        await retire(old)                           # best-effort; never raises
    return configured
```

Whatever your rejection latch and health state are called, both must go back to the state a
fresh process would start in for the new configuration — that is the whole method.

Things that had to be true for this to be safe:

- **The health state needs an explicit reset.** A guard like "`mark_active` is a no-op while no
  credential is configured" is correct and means `mark_active` cannot take you from `none` to
  anything. Add a reset that goes to the cold-start state for the new configuration.
- **Cache tier qualification must read the session health per call, not at startup.** If a
  cached anonymous row qualifies for a caller whose effective tier is now "member", adoption
  changes nothing visible. Verify this with a test that fetches anonymously, signs in, fetches
  again, and asserts a second network request at the member tier. Do not assume it.
- **A request that straddles the adopt must be discarded, not classified.** This is the subtle
  one. Suppose a request went out under the old cookie, the old cookie is rejected (the site
  redirects to its login page), and while that response is in flight the user's new sign-in is
  adopted. The response arrives, the transport's post-processing sees "rejected" — and latches
  `rejected=True` and `health=expired` onto the *new* credential. Or: the request went out with
  no cookie, returns a logged-out page, and post-processing now sees `cookie_configured=True`
  with an empty jar. Neither outcome is recoverable without a restart. The fix is a generation
  counter: `fetch()` records `self.adoptions` before the request and, if it changed by the time
  the response arrives, raises a retryable transport error (`policy_changed`) — never cached,
  never an auth state. In-flight requests keep their reference to the old session object and
  finish on it; if your HTTP library's teardown does not abort live requests (wafer's
  `__aexit__` releases an owned challenge solver and nothing else), retiring the old session
  interrupts nobody.
- **A discarded straddler must be retried once by whoever owns the tool call, or it surfaces as
  a tool error.** The generation counter is a transport-level rule; the transport cannot retry
  because it does not know the request is idempotent, but every caller that fetches a whole
  page or listing does. The likely real sequence is mundane: the sign-in starts, the user takes
  a minute, the agent runs a large fetch meanwhile, validation lands mid-fetch. Without the retry
  the agent sees the server's own state change as `fetch_failed`. Retry exactly once, under the
  new policy; a second `policy_changed` in a row still surfaces.
- **The straddler can hit your background work, not only a tool call.** Anything long-running
  that started at process boot — here a 195-request sitemap pass — is in flight during exactly
  the window a first-run sign-in lands in. If that pass hinges on one fetch (an index file),
  losing that fetch to `policy_changed` can leave the process in a "discovery incomplete" state
  until restart, with every lookup of an unindexed item answering "not found yet" forever. Audit
  every startup task for a single fetch it cannot recover from, and give that fetch one retry
  on any retryable failure.
- **Idempotent.** Adopting the same state twice costs one more session build and nothing else.
  Adopting after `forget` drops the credential the same way — the method reflects the store.

After a successful validation you may reasonably mark the session `active` rather than
`unverified`: the validation *was* a marker-bearing fetch with this exact credential, in this
process, seconds ago. State the reasoning in a comment; it is a judgement call.

## 6. Security bounding

The tool opens a window on the user's screen and writes a credential file. Bound it in five
ways.

1. **It is not read-only, and it says so.** Annotate `readOnlyHint=false`,
   `idempotentHint=false`, `openWorldHint=true`. Both Claude Code and Claude Desktop decide
   whether to prompt from exactly these hints. Do not reuse a shared read-only annotations
   object out of habit — that is the whole bug.
2. **The description carries the consent rule.** "Call this only when the user explicitly asks
   to connect their membership — never to fill in null values on your own." The description is
   the one piece of your spec an agent is guaranteed to read; put the rule there, and also in
   the server's `instructions`.
3. **Never return, format or log the token.** It travels `capture()` → `validate()` →
   `save()` and appears in no envelope, no `reason`, no `instructions`, no log line. Make the
   envelope models incapable of carrying it (no field could hold it), and test that a secret
   token never appears in any tool output or captured log.
4. **Refuse under an env override** (§4). A sign-in that succeeds and is then ignored is worse
   than one that refuses and explains.
5. **Know what an unwanted invocation can and cannot do.** It can open a window and, if the
   user then signs in, store a cookie the user chose to mint. It cannot read a password, cannot
   act without the user typing, cannot override the env var, and cannot make the server do
   anything with the cookie it would not do with a pasted one. The residual risk is a client's
   "Always allow": after that the prompt is gone and only the description stands between an
   agent and an unasked-for window. That is why the description says what it says, and why the
   window opens on the *site's* page in a *throwaway* context rather than anything of yours.

## 7. The `.mcpb` bundle recipe

Desktop users install a bundle, not a clone. The recipe for a Python server, with the reason for
each line:

- **`manifest_version: "0.4"`, `server.type: "uv"`.** Desktop downloads its own `uv` (pinned
  0.9.7 at the time of writing), requires a `pyproject.toml` in the bundle, runs plain `uv sync`
  there, and runs `mcp_config`: `{"command": "uv", "args": ["run", "--directory", "${__dirname}",
  "<your-console-script>"]}`. No Python needs to exist on the machine beforehand. The `uv` type
  wants an `entry_point`; a four-line `src/server.py` that calls your CLI's `main()` satisfies it.
- **`uv sync` does not install the bundle project's own extras — but extras named in a
  dependency specification are installed normally.** So the bundle's `pyproject.toml` declares
  no extras and depends on `your-server[browser]`. Desktop users get the browser dependency;
  PyPI users installing the server itself stay lean. A bundle project with no `[build-system]`
  is a virtual project: `uv sync` installs its dependencies and nothing else.
- **Before PyPI publication**, vendor the server's source into the bundle at build time and point
  `[tool.uv.sources]` at the path. Leave the one-line switch to a pinned release as a comment in
  that file, so the next person does not have to work it out.
- **Generate the manifest's `version` from `pyproject.toml`. Never type it.** Keep the
  checked-in manifest without a version and have the build script refuse one that has it. Do the
  same for the `tools` list, from wherever your tool descriptions live. A version typed in two
  places drifts; it is a matter of when.
- **`user_config`** with a `sensitive: true, required: false` string mapped to your env var, for
  the machine where no window can open. Its description should say that a value there overrides
  the in-conversation sign-in. Treat blank as unset.
- **`compatibility.platforms: ["darwin", "win32", "linux"]`.** Desktop's Linux beta shipped
  2026-06-30; a `uv`-type bundle names no platform, so declare all three and let CI run the
  server on each.
- **The build needs no network** and the test suite should check the generated manifest against
  `pyproject.toml` from the zip without installing anything.

## 8. Renewal — the part that actually matters

Everything above gets a user signed in on day one. What decides whether the tool is still working
in month eleven is what happens when the cookie dies, because it will: durable session cookies
have a fixed expiry (365 days here) that using them does not extend, and the user will have
forgotten they ever signed in.

Three things, in order of importance:

1. **Detect expiry per fetch, and label it structurally.** A logged-out response with a cookie
   configured is a distinct state (`session: expired`, `auth_state: session_expired`), never
   silently `anonymous`. This is the floor; without it the tool degrades in silence.
2. **Name the fix where the failure is seen.** The notice that accompanies the first
   post-expiry response says: call the sign-in tool; or run the CLI; or update the env var if it
   came from there. An agent reading that can offer the user the right next step in the same
   turn.
3. **Warn before it happens.** From the capture date and the cookie's fixed lifetime you have an
   upper bound on the days left (capture can post-date the mint, so the real deadline is that day
   *or earlier*). Inside a window — 30 days here — put `session_expiring:<days>` in `warnings[]`
   on every tool that carries a session field, and expose the same number from the status tool
   and the CLI. Stop emitting it once the session is already expired; the envelope says so then,
   and "expiring" on top is noise. The env-var path has no capture date and gets no bound.

Then make renewal one gesture with **no flag**: a plain `sign_in`. A credential the host has
rejected in this process, one the pre-flight check (§4) finds rejected, or one past its own
lifetime bound all proceed unforced — the same flow, a new cookie, validated, stored, adopted,
nothing restarted. `sign_in(force=true)` is for renewing *early*, replacing a credential that is
verified live. If your renewal path needs the user to know about `force`, the guard is wrong
(§4).

## 9. Checklist for a new server

Credential and validation

- [ ] Store: file with restrictive mode, env var with stated precedence; `env_override` is the
      same non-blank rule `load()` uses.
- [ ] One validation routine shared by the CLI and the tool, returning `authenticated` /
      `rejected` / `could_not_check:<reason>`; stores nothing on the last two.
- [ ] Browser capture: installed browser via `channel`, then a fallback ladder, nothing
      downloaded; throwaway context; "remember me" pre-ticked if the durable cookie depends on it;
      polls cookies; never touches a form field; `on_launch` callback; closed window
      distinguishable from timeout.
- [ ] The window does not advertise the automation (blink automation feature disabled, the
      driver's automation switch ignored, real viewport), pinned by a test. Otherwise an
      invisible captcha keyed on `navigator.webdriver` rejects a correct password and the site
      reports it as wrong credentials — indistinguishable from a typo, and it will be reported
      to you as one.

The tools

- [ ] `sign_in` returns within ~2 s; background task; brief launch wait; idempotent in flight;
      the guard VERIFIES an *unverified* credential (a `verifying` phase running the validation
      routine on the stored one; window only on rejection; `refused / session_active` via the
      poll on success; no window on "could not check"), proceeds unforced on *none*, *expired*
      and past-bound, refuses only a credential verified live; `force` = replace a verified-live
      one; `env_override` (names where; blank and `${…}` are unset), extra missing (names the
      command, checked before any probe), offline.
- [ ] The window closes on EVERY exit path — success, timeout, closed by the user, `cancel()`,
      task cancellation, interpreter shutdown — and the teardown is BOUNDED: a browser
      automation's `close()` waits for a driver reply with no timeout, and at loop shutdown the
      driver's reader is cancelled in the same sweep as the capture, so an unbounded close
      deadlocks the process with the window open. **That wedged-but-alive process is the only
      source of an orphaned browser**: a killed process leaves none, because Playwright's
      driver kills the browser's process group from its own exit hook when its stdin closes
      (measured: `SIGTERM` and `SIGKILL` mid-capture, real Chrome, Playwright 1.62 — nothing
      left). So the bound is the fix. Abandon a step that overruns in its own task (never
      cancel-and-wait it — the library may absorb the first cancellation to send an abort),
      read the browser pid at launch, and terminate by pid what the graceful path did not
      confirm closed — that covers the one remaining case, Python alive and the driver
      unresponsive — from the capture's `finally` and from an `atexit` reaper. Know that a
      process with no `SIGTERM` handler runs neither `finally` nor `atexit` on a host's
      `SIGTERM`, and is safe there only because of the driver's hook — on Windows too: the
      driver has no process group to signal there and runs `taskkill /T /F` instead (read in
      Playwright 1.62's driver, not assumed). If Windows is a declared platform, the pid guard
      needs a Windows command-line query (`Get-CimInstance Win32_Process`; `tasklist` has no
      command-line column) — a silent no-op there is a lie in the docs — decoded with
      `errors="replace"` (the markers are ASCII; PowerShell's OEM output under Python's ANSI
      decode raised on a non-ASCII username) and spawned with `CREATE_NO_WINDOW`. Then
      MEASURE it: a GitHub Actions Windows runner is a real Windows machine with Chrome
      installed, so the branch that "needs a machine" needs a workflow file. The first run
      of that workflow earned its keep on both platforms nobody had run it on: Linux `ps`
      cuts a piped line at 80 columns (`-ww`, or read `/proc/<pid>/cmdline`), so the guard
      had never matched a real Chrome there; and the Windows query came back as one bare
      None for three different reasons — have it answer a status (gone, unreadable, failed)
      so a guard that never fires can say why. The heavy import runs off the loop.
- [ ] `auth_status` with a long-poll capped under the client's tool-call limit (45 s for a 60 s
      cap, re-measured on the client build you target); returns immediately when idle.
- [ ] Both wear the server's ordinary outer envelope with the status object under `data`; typed
      models with enum fields and an `outputSchema`; no field at any depth can hold the token —
      checked by field NAME, not by description text; a test proves a secret token appears in no
      output and no log.
- [ ] The task has a catch-all so no live phase outlives it, and reads `cancelling()` before
      its irreversible step.
- [ ] Annotations: not read-only, not idempotent, open-world — a separate object from any shared
      read-only one. Description carries the consent rule. Server `instructions` repeat it.
- [ ] Shutdown cancels an in-flight sign-in.

The transport

- [ ] `adopt()`: lock, re-read the store, flip the policy, clear the rejection latch, reset
      health to the cold-start state, retire the session, bump a generation counter.
- [ ] A response that arrives after an adopt is a retryable transport error, never classified,
      never cached — tested for both the logged-out and the rejected case — and every caller
      that owns a tool call retries it exactly once, tested with an adopt mid-fetch.
- [ ] Every startup background task audited for a single fetch it cannot recover from; that
      fetch retried once on any retryable failure, tested with an adopt mid-fetch.
- [ ] Tier qualification reads session health per call — tested end to end (anonymous fetch, sign
      in, member refetch, no restart).

Renewal

- [ ] Expiry detected per fetch and labelled structurally.
- [ ] The expired notice names the sign-in tool, the CLI, and the env var.
- [ ] `session_expiring:<days>` on every session-carrying tool inside the window — the sign-in
      tool included; same number from the status tool and the CLI; suppressed once expired.
- [ ] A plain `sign_in` renews a rejected or past-bound credential with no flag; `force` is
      only for renewing early. Tested: a stored credential the host rejects, in a process that
      has never seen the marker, gets a window without `force`.

The bundle

- [ ] Manifest 0.4, `server.type: "uv"`, `entry_point` shim, `mcp_config` runs the console
      script, `user_config` sensitive field mapped to the env var, platforms
      darwin/win32/linux.
- [ ] Browser extra on the *dependency*, not on the bundle project.
- [ ] Version and tool list generated; a versioned template refused; a test that reads the built
      zip.
- [ ] Every "one request per row" tool audited against the 60 s cap.

## 10. What is project-specific and what is universal

Universal — take as given for any local MCP server that needs a user credential:

- The 60-second tool-call cap on Claude Desktop, and that progress notifications do not extend
  it; the absence of elicitation for local stdio servers; therefore start/poll.
- The bundle recipe: `uv` server type, Desktop-supplied `uv`, extras-on-the-dependency, generated
  version, darwin/win32/linux.
- The annotation and description rules, and "never return the token".
- The env-override refusal and the "name where to fix it" rule.
- The renewal shape: detect, name the fix, warn ahead, one gesture to renew.
- The adoption principle: if any part of your transport's behaviour is chosen from the
  credential at construction, you need an explicit adopt step, and a straddling response must be
  discarded rather than classified.

Project-specific — re-derive for the target site:

- Which cookie is durable, its lifetime, whether "remember me" is what mints it, and whether it
  rotates (here: one 36-character cookie, 365 days, fixed, does not rotate; a second cookie is
  derived and rotates and is written back from the jar, never from the response).
- How a rejected credential presents (here: a redirect to the login host, checked on the *final*
  URL only — the successful re-mint also passes through it) and what a logged-in page carries
  that a logged-out one does not (a server-rendered marker, never "Sign Out" text).
- Whether the transport's rotation policy actually depends on the credential (it does when the
  HTTP library empties the cookie jar on rotation); the exact constructor kwargs.
- The validation target: the cheapest page that renders the login marker, exempt from any
  "validate the id first" rule because it is a shipped constant.
- The per-row cost that sets the listing cap.
- The browser ladder's executable names, if the site or the platform differ.
