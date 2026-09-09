# RECON — measured facts about Consumer Reports

What we know about CR's payload, paywall and auth, and **how we know it**. `SPEC.md` decides
what to build; this file is the evidence those decisions rest on.

Everything here is measured, not inferred. Where a claim is unverified it says so.

- **Phase 0** (anonymous) — 2026-09-01
- **Phase 0.5** (live member session via Playwright) — 2026-09-01

Reference category throughout: French-Door Refrigerators, `c37162`, 172 products.

---

## 1. The payload

A plain GET of a category URL embeds `window.filterInstanceDATA` — 1,165,423 bytes of JSON on
`c37162`, inside an ~11 MB page. Top-level keys: `filters`, `data`, `args`, `attrs`.

Per product (16 keys): `index`, `id`, `productGroupHierarchy`, `price`, `_price`,
`overallDisplayScore`, `_overallSortIndex`, `brandId`, `brandName`, `modelName`,
`expertRatings`, `surveys`, `attrs`, `_groupId`, `_groupName`, `_shoppingParsed`.

`args` also carries `scid`/`cid` and a `cats[]` array of sibling categories with `typeURL` and
`reliabilityURL` — a second discovery path alongside the A-Z index. Not every `cats[]` entry
is a category: some carry a slug where the others carry an id (§9h, measured 2026-09-06).

### Envelope universality — 11 categories

Identical envelope and **the same seven filter ids** on: French-Door Refrigerators (172
products), Smartphones (73), Banks (69), Gas Ranges (66), Countertop Microwaves (64), Gas
Dryers (38), Induction Cooktops (22), Antivirus for Windows (18), Phone/TV/Internet Bundles
(16), Flat-Top Grills (10), Robotic Vacuums (8).

Spans appliances, electronics, home-garden and two service categories. One parser.

- **Attribute counts vary 2–24.** Phone/TV/Internet Bundles has two. Code must not assume a
  category has three scored attributes.
- **Largest category observed: 172**, against a `limit` cap of 200. *(Superseded by §12c —
  Televisions 303 and Mattresses 293 exceed the cap; both were invisible here because neither is
  in the A-Z index.)*
- **A-Z index**: 236 categories — home-garden 84, appliances 68, health 34,
  electronics-computers 22, money 15, babies-kids 13. Televisions are absent entirely; only
  telecom *services* appear.

### Attribute join

The definition block keys attributes as **`id`**; per-product entries key them as
**`attributeId`**. Same integers, different field names.

**`attrs` is not the full dictionary — `categoryAttributes` is.** The same page embeds nine
`categoryAttributes` blocks (the category plus its siblings, ~2.5 MB further into the HTML,
outside `filterInstanceDATA`). Coverage of the attributeIds products actually carry:

| Category | product attrs | via `filterInstanceDATA.attrs` | via `categoryAttributes` |
|---|---|---|---|
| French-Door Refrigerators | 37 | 23 | **37** |
| Gas Dryers | 25 | 17 | **25** |
| Gas Ranges | 23 | 17 | **23** |
| Flat-Top Grills | 22 | 13 | **22** |
| Robotic Vacuums | 20 | 13 | **20** |
| Antivirus for Windows | 20 | 20 | **20** |
| Induction Cooktops | 11 | 10 | **11** |
| Phone/TV/Internet Bundles | 2 | 2 | **2** |

**100% on all 8; `attrs` falls short on 6 of 8.** Entries carry `attributeId`, `name`,
`displayName`, `unitName`, `description`, `attributeGroup` (`"Specs"` / `"Features"`),
`sortOrder`, `attributeDataTypeName`, `isFilterSuppressed`, `isProductGroupMainAttribute` —
strictly more than `attrs`.

> An earlier draft recorded "14 entries (38%) have no definition" and routed the fix through the
> 2 MB reliability page. Both were wrong: the gap is against `attrs` only, and the complete
> dictionary was on the category page the whole time. `window.initStore` on a *category* page
> holds only `env` — the populated `initStore` is a reliability-page thing, and it was never
> where these definitions had to come from.

`dataType` distribution on `c37162`: `numeric-general` 8, `numeric-rating-score` 7, `text` 4,
`boolean` 3, `numeric-price` 1, `custom` 1. The definition `data[]` (distinct values) is
populated for `numeric-general`, `boolean` and `text`, and empty for `numeric-rating-score`
and `numeric-price`.

### Filter taxonomy

Every filter is `{label, id, type, args, data}` with options in `data[]`:

| `id` | `type` | Option shape |
|---|---|---|
| `type` | `radio` | `{id, label, sortOrder}` |
| `categories` | `checkbox` | `{id, label, sortOrder}` |
| `price` | `input-text` | `{label: <number>}` — discrete points, **not** a min/max range |
| `brands` | `checkbox` | `{id, label, sortOrder}` |
| `features` | `features` | full attribute definition |
| `sort` | `sort` | `{id, label, prop, args}` — `overallScore` → `product.overallDisplayScore` |
| `custom` | `custom` | `{id, name, label, prop, data}` — **contains `isRecommended`** |

Two traps: there is **no top-level `isRecommended` filter** (it lives inside `custom`, with
`prop: "expertRatings.isRecommended"`), and **`price` is not a range type**.

---

## 2. The paywall boundary

Measured on the same 172 products in both states, **on one category (`c37162`) only.**
Only two things are gated there. *(The original note that other franchises were "assumed" is
superseded by §9f, which diffed four more across five franchises.)*

| Field | Anonymous | Member |
|---|---|---|
| `overallDisplayScore` | null 172/172 | **populated 172/172** (scale 0–100, observed 43–79) |
| `attrs[].value` (`numeric-rating-score`) | null 1204/1204 | **populated 1204/1204** (scale 1–5) |
| `surveys.ownerSatisfaction.surveyScore` | **populated 158/172** (1–5) | same |
| `surveys.reliability.surveyScore` | **populated 158/172** (1–4) | same |
| `expertRatings.isRecommended` | **11 of 172 true** | **identical, 11** |
| `_overallSortIndex` | **populated 172/172** | same |
| `price`, `_price`, `_shoppingParsed`, specs, model names | populated | same |

Scored attributes are **present with `value: null`** anonymously, never absent — which is what
makes the "null, never missing" contract in `SPEC.md` §7 implementable.

> An earlier draft of `SPEC.md` claimed anonymous strips "every score" and `expertRatings` is
> "all false". Both were wrong.

### Anonymous is a usable tier

Reliability, owner satisfaction, CR Recommended, price, retailer spread and the full spec sheet
are all public. A user with no membership can legitimately shortlist.

---

## 3. `_overallSortIndex` — whole-category measurement, SUPERSEDED by §9g

> **SUPERSEDED 2026-09-02. Do not build against this section — see §9g.** The numbers below
> are real but were measured across the **whole category**, ignoring `_groupName`. CR renders
> each display group as a separate ratings table, and *within* a group `_overallSortIndex` is
> an exact score rank — 100% agreement in 17 of 17 groups across 6 categories, and identical
> anonymous-vs-member. The whole-category figure measures only how many groups a category has
> and how much their score ranges overlap. The `ranking: "approximate"` conclusion at the foot
> of this section is **withdrawn**: `SPEC.md` ranks within group always, and a cross-group score
> sort is supported but structurally labelled `sort.scope: "cross_group"` rather than refused.
> Retained here as the evidence trail for how the group structure was found.

Populated anonymously, **lower is better**: the 11 CR Recommended models occupy indices 4–38
(mean 19.1) against 178.4 for the rest. It ranks over the *parent product group*, not the
category — range 4–348 across 172 models, with gaps.

Scored against the true values on the same products:

- **88.2% pairwise agreement** (1,736 of 14,706 comparisons inverted)
- **6 of 171** adjacent pairs misordered
- **The truly top-rated model (score 79) sits at anonymous position 18 of 172**

The ordering is locally smooth but wrong at the top *of the concatenated category* — which is
not a thing CR ever displays. **The break is a table boundary, not an ordering error** (§9g):
the "truly top-rated model at position 18" is the 79 that heads the third group's table, exactly
where CR's own member page puts it.

> **Retracted.** This section originally concluded `ranking: "approximate"`. That conclusion is
> withdrawn — see §9g. Within a group the ordering *is* CR's score ranking, exactly.

## 4. Score derivation — tested and rejected

Regressed anonymous signals against true scores, n=172 (158 with both survey scores):

| Model | R² | Mean abs error | Max error | Within ±1 | Within ±3 |
|---|---|---|---|---|---|
| `_overallSortIndex` alone | 0.554 | 4.8 | 29.0 | 2% | 16% |
| \+ owner sat, reliability, Recommended, price | 0.836 | 2.8 | **13.1** | 22% | 69% |

True range is 43–79 — a 36-point span — so a 13.1-point max error is over a third of the scale.
**Scores cannot be derived.** Recorded here so the experiment is not repeated hopefully.

---

## 5. Auth — conclusions

> **The working behind this section lives in a gitignored `notes/auth-recon.md`**, not in the
> public repo: the cookie-by-cookie ablation, the per-cookie property table and the re-mint
> transcript. They were load-bearing while the auth design was being settled and are not needed
> to build against it. What is kept here is everything the design actually rests on, so the
> reasoning stays checkable — see `SPEC.md` §12.

**`hash` is the durable credential.** It carries a 365-day expiry and, on its own, fully restores
a member session: the first request returns `data-subscriber="true"` with every score populated.
It does **not** itself rotate — after a re-mint it keeps the same value and the same expiry.

**Re-minting works over plain HTTP, with no JavaScript.** Confirmed twice: through Playwright's
raw HTTP client in 2026-09-01, and end to end through wafer in §10i. A plain HTTP client holding
only `hash` recovers full member access on its *first* request, via a redirect through
`secure.consumerreports.org/ec/login` that lands back on the requested page. This is why no
browser automation is needed on the data path.

**`userLicenses` is derived, short-lived and rotates.** It is *sufficient* to authenticate but
**not necessary** — remove it and a session carrying `hash` regenerates it. An earlier draft of
this file called it "necessary and sufficient, proven by ablation"; the necessity half was never
tested and is false. It is reissued with a **new value** on each re-mint, which is why the client
must persist rotations rather than pinning what it was handed. A *stale* `userLicenses` alongside
a valid `hash` still authenticates (§10i).

**`userToken`'s 1-day expiry is not a constraint** — it is regenerated from `hash` during ordinary
browsing, which is why members are not asked to log in daily.

**The cookies we need are not HttpOnly**, so `document.cookie` can read them and a console
one-liner is a valid capture gesture — and `SPEC.md` §6 now makes it the documented path. *(This
section originally recommended "Copy as cURL"; that is withdrawn.)* cURL is still accepted: it is one
right-click and captures the whole header, not because it is required. (An earlier draft argued
HttpOnly made cURL mandatory; that generalised wrongly from `JSESSIONID`, which belongs to the
login app on `path=/ec`.)

### `data-subscriber` is the auth marker

Server-rendered, so a plain wafer GET sees it with no JavaScript:

| | `data-subscriber="true"` | `data-subscriber="false"` |
|---|---|---|
| Anonymous | 0 | **1,548** |
| Member | **present** | 0 |

**Do not use "Sign Out".** It appears **twice in the anonymous page** inside hidden account-nav
markup, so keying on it reports every anonymous fetch as a member session — silently, and in the
most damaging direction.

Note this marker is *not* the whole story: a **rejected** credential never reaches a page carrying
it at all (§10b). Detection reads the final URL first, then the marker.

### Login — recorded, never automated

CR's login is a form POST to `secure.consumerreports.org/ec/login`. Measured 2026-09-03 by reading
the page (no login attempted): fields `username`, `password`, `setAutoLogin`, `rurl`, plus hidden
`captchaToken` / `captchaEKey` / `captchaSiteKey`. **hCaptcha is wired into the form**; there is no
SSO and no MFA.

**The captcha runs on EVERY submit, it is invisible, and `navigator.webdriver` is what decides
whether it stays invisible.** CR's login bundle (`content-login.*.js`, read 2026-09-05) intercepts
the submit, loads hCaptcha lazily (`render=explicit`, `size: "invisible"`), calls
`execute({async: true})`, writes the three hidden fields and only then posts the form; it logs
`puzzleWasShown` as the exception. Measured the same day on the real login page, injecting
hCaptcha with CR's own site key exactly as that handler does — nothing typed, nothing posted:
a Playwright-launched Chrome 152 with **`navigator.webdriver === true` got a visible puzzle at
~0.9 s and no token (2/2 runs)**; the same Chrome with
**`--disable-blink-features=AutomationControlled` (`webdriver === false`) got a token issued
silently at ~0.8 s and no puzzle (3/3)**. Every other observable was identical between the two:
user agent, languages, timezone, plugins, `checksiteconfig` → `pass: true`, the cookies CR set,
and the form (`action=/ec/login`, `rurl` empty, `setAutoLogin` already `checked`). A `www.`
warm-up visit first adds only `userReferrer` and a FullStory sampling cookie — not a factor.
`ignore_default_args=["--enable-automation"]` alone changes nothing on Playwright 1.62, whose
default switch list (read from its driver bundle) no longer carries that switch. *Observed by a
user, 2026-09-05, and inferred for the server-side step (no login is ever attempted here):* a
human who solved that puzzle in the flagged window was still answered "We still don't recognize
that sign in" for credentials that signed in at once in a plain Chrome window — a token from a
browser hCaptcha has flagged verifies as one, and CR reports it as a wrong credential. No login
is ever automated: a human signs in, and the server only reads the resulting cookie.

**`setAutoLogin` is the "remember me" box, and it is what mints the durable 365-day `hash`.**
A login without it yields a session that expires in days. This is shipped, not hypothetical:
`browser_auth.REMEMBER_ME_SELECTOR` re-ticks it in the `cr_sign_in` / `auth --browser` window
(`SPEC.md` §6 step 3). CR renders the box already checked, so the tick is a guard, not a fix.

**The box is checked on every server render, and no script touches it** (measured 2026-09-07,
read-only, no login attempted). Both `GET /ec/login` and `GET /ec/login?error` — the page a
failed submit lands on — ship `<input name="setAutoLogin" … checked="checked" type="checkbox">`;
the form is `<form id="login-form" method="POST" action="/ec/login">`, a full-page POST, not an
SPA; and `content-login.*.js` contains no reference to `setAutoLogin`, the checkbox id or
`.checked` — its only lifecycle hook is `onpageshow` → `location.reload()` on a bfcache restore,
which re-renders server-side, checked. So a failed submit does NOT lose the tick. The flow
re-asserts it on every poll anyway, which covers the user un-ticking it and CR changing the
default.

**ANSWERED 2026-09-08: a lapsed `userLicenses` suppresses the `hash` re-mint.** The captures
did not die — *we killed them*, by sending a stored entitlement token alongside the durable
cookie. Measured on the 2026-09-07 credential at ~31 h, one request each, through the transport:

| Jar | Verdict |
|---|---|
| `hash` + stored `userLicenses` | `session_expired` — anonymous body, `data-subscriber="false"` |
| **`hash` alone** | **`member`** — 8/8 scored |
| `hash` + stored `userLicenses`, repeated | `session_expired` |

Order-independent, same credential, same minute; dropping `userLicenses` from `session.json`
restored member access with no new sign-in. That `hash` carried a browser-measured expiry of
**2027-09-07** and was still good — so the "remember-me did not take / session-only `hash`"
hypothesis below is **falsified**, and the countdown was not lying after all.

The token names its own clock. `userLicenses` is `d`(gzipped licenses)`&t=`(ms issue time)`&s=`
(40-char signature); the 2026-09-07 copy carried `t = 2026-09-07T14:48:35Z`, the moment our
write-back stored it. Re-read against `t` rather than against capture, the 09-05 timeline below
lands exactly: written back 21:34:06Z, last member fetch 2026-09-06T21:33:53Z — **13 seconds
under 24 h after the token was minted**, not "24 h 02 min after capture". So `userLicenses`
lapses ~24 h after its own `t`, and once lapsed CR serves the anonymous page instead of
following the `hash` through the re-mint. It is not inert: it is a veto.

Why this hit every day and not once: the stored copy is written back mid-process and only ever
read by the NEXT process, so it is always older than the session that sends it. The fix is
`SPEC.md` §6's `durable_only` — `userLicenses` is seeded and persisted only when it is the sole
credential — at the cost of one redirect per cold start (§10a A3: the re-mint is once per
session, nothing after).

**A capture of 2026-09-05 stopped working after about a day** — *superseded by the entry above;
kept because the reasoning shows what the missing measurement cost.* The
browser flow at the time returned only the cookie's value; the `expires` Playwright reports
(POSIX seconds, `-1` for a session cookie) was discarded, `session.json` stored only
`captured_at`, and the status counted down from the 365-day constant — it said 364 the day the
cookie died. A session-only `hash` (remember-me not taking) fits the symptom exactly and is the
leading hypothesis, but the one fact that would confirm it was never recorded. The flow now
returns and stores the expiry, and refuses a `hash` with none (`SPEC.md` §6).

*The timeline, reconstructed 2026-09-07 from the cache and the session file:* captured
2026-09-05T21:31:28Z; `session.json` rewritten at 21:34:06Z by the `userLicenses` write-back of
the first member fetch (the transport never writes `hash` back, so the stored `hash` is the one
the browser handed over); member pages (`data-subscriber="true"`) fetched with it through
**2026-09-06T21:33:53Z — 24 h 02 min after capture**; anonymous rows from 21:53:54Z on (not
attributable to this cookie from the cache alone); and a probe on 2026-09-07 at ~40 h answered
`session_expired`. A death inside a day and a lifetime of at least 24 h 02 min together fit a
24-hour server-side session — the same span as `userToken`'s 1-day expiry — better than the
vaguer "days" recorded above. It does not distinguish remember-me not taking from CR revoking
a durable `hash` early; only the next capture's recorded `expires_at` can.

*The next capture, 2026-09-07T13:57Z, through the same automation window (`auth --browser`,
`--disable-blink-features=AutomationControlled`, "stay signed in" pre-checked — confirmed by
the user):* CR minted a `hash` expiring **2027-09-07T13:57:41Z — 365 days**, the same term as
the 2026-09-01 plain-Chrome login. So the window CAN mint the durable cookie, and the 09-05
capture is the outlier: either the box was unticked at the moment of submit that day, or CR
issued a short session under a condition on its side. Which of those it was is unrecoverable.
What differs from here on is that the per-`hash` log line and the stored `expires_at` would
name it. (That log line was itself dropped on the 09-07 run: the CLI configured logging only
for the validation step, after the browser had closed — fixed the same day.)

**A `hash` behind a lapsed `userLicenses` is IGNORED, not rejected** (measured 2026-09-07,
through the transport: `hash` + `userLicenses` in the jar): the category page comes back `200`,
**no redirect at all** (`resp.history` empty — no re-mint hop through `secure.`), the ordinary
anonymous body with `data-subscriber="false"`, and `hash` **still in the jar** afterwards.

> **This experiment was originally titled "a lapsed `hash`", and that attribution was wrong.**
> Both cookies in the jar were ~40 h old and only one of them was tested — the 2026-09-08 A/B
> above shows the `userLicenses` alone accounts for every symptom, and that the `hash` beside
> it may well have been live. **The `hash`-lapsed row is therefore UNMEASURED**: no genuinely
> expired `hash` has ever been observed, because none has been allowed to age a year. Read the
> signature below as "a vetoed re-mint", which is what it is known to be.

So of the credential states, three are measured and differ: *absent* → the anonymous page (§5);
*malformed* → redirect to `/ec/login?error`, no payload, cookie cleared (§10b); *vetoed by a
lapsed `userLicenses`* → the anonymous page with the cookie left in place. The transport's
classification is defensible for each — the vetoed case is `session_expired` (marker `false`,
`hash` asserted in the jar), never `credential_rejected` and never `payload_missing` — but note
what that verdict cost while the veto was reachable: it read `session_expired` on a credential
with 363 days left and sent the user to sign in again. The classifier was reasoning correctly
about a genuinely anonymous page; the page was our own doing. Removing the veto is what makes
`session_expired` honest, which is why the fix is in the store and not in the classifier.

### Verified end to end

A plain server-side GET carrying the cookie returns every overall score and every attribute
value, with no JavaScript and no client-side hydration. The authenticated envelope is
structurally identical to the anonymous one — same four top-level keys, same product count, same
attribute definitions, same seven filter ids, same sixteen product keys. Only values fill in.

---

## 6. Response sizing — measured on a populated member payload

The original figures in `SPEC.md` were taken on anonymous data with every score `null`, and
were wrong by 45–75%. Re-measured 2026-09-01 (~3.6 chars/token on dense JSON):

| Shape | `limit=10` | `limit=25` | all 172 |
|---|---|---|---|
| `summary` | 408 | 833 | 5,138 |
| `standard` | 881 | **2,014** | 13,260 |
| `full` | 1,614 | 3,847 | 25,897 |

| Single shape | Tokens | Previously claimed |
|---|---|---|
| Raw category payload | **357,057** | ~213,000 |
| `cr_product` with descriptions | 2,507 | ~1,240 |
| `cr_product` without descriptions | 1,126 | — |

On `c37162`, a product carries 37 attribute entries; **23 have a description, 8 have a unit**.
Descriptions are identical for every product in a category, so carrying them per product
duplicates the same text 172 times for a 2.2× cost.

## 7. Smart Buy and shopping data

`_shoppingParsed` is present on 100% of products across every category checked, always
`{count, prices, isSmartBuy}`. Retailer `count` ranges 0–9.

`isSmartBuy` is **not paywalled** and is genuinely sparse: 0/172 French-Door Refrigerators,
**9/66 Gas Ranges**, 0/18 Antivirus for Windows. Real signal where present.

## 8. The A-Z index carries names

All 236 category links carry display text — 236 distinct names, e.g. `Mini-Splits` → `c37159`,
`Room Air Purifiers` → `c29550`. One cached fetch gives a complete name → id map, which is what
makes cold-cache category search possible. HTML entities need unescaping (`&amp;`).

## 9. Reliability pages — a second envelope carrying genuinely new, fully anonymous data

Each category exposes a `reliabilityURL` (in `args.cats`), e.g.
`/appliances/refrigerators/french-door-refrigerator/reliability/c37162/` (2.4 MB).

### 9a. `reliabilityURL` is bimodal, and `false` is CR's answer, not a gap

*Measured 2026-09-09 over every category cached locally (52 with a payload on hand), reading
each payload's own `args.cats[]` entry for its `args.cid`:*

| `reliabilityURL` on the id's own entry | Categories | `HasReliabilityData` |
|---|---|---|
| a URL string | 27 | 1 |
| the boolean **`false`** | 25 | 0 / absent |

No third form, no null, no empty string. So the field is a **statement**: CR runs a brand survey
on this category, or it does not. `false` is not a missing value to be worked around — it is the
answer, and the only place the answer is available without fetching anything.

Categories measured `false` include upright freezers (`c33041`), which is what surfaced this:
`cr_reliability` treated "the payload says `false`" and "no payload cached" as the same unknown,
constructed `/appliances/freezers/upright-freezers/reliability/c33041/`, and CR returned the
404 it should. The filed report reasoned that the nested `freezers` franchise segment had
confused a URL builder — a good hypothesis, and wrong: there is no page at any URL, because CR
publishes no survey for the category. See `SPEC.md` §7 for the fix.

**It does not contain `filterInstanceDATA`.** It carries `window.initStore` — 2,033,720 bytes,
shaped `{data: {supercategory, category, categories[], models[], taxonomy}, env, seo}`.

> An earlier draft of this file called it redundant and out of scope, on the basis of keyword
> counts alone. That was wrong on every count — it was never parsed. Corrected below.

**Brand-level survey ratings, populated anonymously.** `data.category.surveys.{reliability,
ownerSatisfaction}.productGroupSurveyValue[]` — **17 brands each**, populated with no session —
though not every brand carries both surveys (see the dashes below), so per-brand gaps are real
and must be `null` rather than omitted rows. Each carries `brandId`, `brandName`, `surveyScore` (1–5), `survey10PtScore`,
`survey100PtScore` and `sortOrder`:

| Brand | Reliability | Owner satisfaction |
|---|---|---|
| Thermador | 4 (75/100) | 5 (81/100) |
| Bosch | 4 (63/100) | 4 (79/100) |
| Fisher & Paykel | 4 (73/100) | 3 (57/100) |
| Maytag | 3 (59/100) | — |
| GE | 3 (56/100) | — |

This is **not** what the main payload holds. There, `surveys.reliability` is the brand's score
stamped onto each product; here it is the brand ranking itself, on three scales, with
methodology text in `blurb` / `footnote` / `infoText`.

**One fetch covers nine categories.** `data.categories[]` carries 9 sibling categories, each
with its own brand survey arrays.

**Category score range leaks anonymously.** `modelMinOverallDisplayScore` /
`modelMaxOverallDisplayScore` on the category = **43 / 79**, exactly matching the authenticated
per-product range measured in §2. The *range* is public even though individual scores are not.

**`HasReliabilityData` / `HasOwnerSatisfactionData`** flags say which categories have survey
data at all — useful for not fetching pages that hold nothing.

**Five per-model fields absent from `filterInstanceDATA`**, in `data.models[]` (172 models):
`firstPublishedDate`, `modelAvailabilityName` (all `"Available"` here; presumably marks
discontinued models elsewhere), `bestBuyUpdateDate`, `cuModelId`, `overallScoreDescription`, and
**`asin` on 102/172** models.

**Incidental finding, and it explains §4.** `overallScoreDescription` states:

> "The Overall Score blends our expert testing, predicted reliability, and owner satisfaction
> into one number—weighted to spotlight what matters most..."

Expert testing is the paywalled component. So the public fields reconstruct only part of the
formula by construction, which is why the regression in §4 plateaus at R² 0.836 no matter what
is added. Derivation does not fail for want of a better model; it fails because the largest
input is missing.

## 9b. The reliability payload also carries categoryAttributes — but it is not needed

The reliability page carries `categoryAttributes` too (48 entries on `c37162`), which is how the
completeness gap was first noticed. It is **not** the source to use — §1 shows the identical
dictionary is on the category page already being fetched. Recorded only so the 2 MB page is not
mistaken for the required source.

## 9c. Category score ranges are anonymous

> **See also §9h:** these same values ship on the **category page** for every sibling, so no
> reliability fetch is needed to read them. This section measured them on the reliability page
> first; the category-page source is the one the design uses.

`modelMinOverallDisplayScore` / `modelMaxOverallDisplayScore`, per category and per sibling, with
no session — the French-Door range (43–79) matches the authenticated per-product range measured
in §2 exactly:

| Category | Score range |
|---|---|
| Top-Freezer Refrigerators | 28–82 |
| Bottom-Freezer Refrigerators | 56–85 |
| Side-by-Side Refrigerators | 49–74 |
| French-Door Refrigerators | 43–79 |

Enough to compare *categories* without a membership, which no other surface supports.

## 9d. Two follow-up measurements

- **`hash` does not rotate.** After a re-mint from `hash` alone: same value, same expiry
  (2027-09-01), while `userLicenses` was reissued with a new value. A static `hash` therefore
  stays valid for its full year, which is what makes the `CR_SESSION_COOKIE` env-var path
  workable with no write-back.
- **`data-subscriber` does not appear on reliability pages** — zero occurrences of either value.
  The generic "no marker ⇒ `marker_missing`" rule would therefore false-alarm on every
  `cr_reliability` call, which is why that tool has its own auth semantics (`SPEC.md` §7).

## 9e. `isDontBuy` is real but rare

Zero `isDontBuy` products across all 7 categories parsed (French-Door Refrigerators, Gas Ranges,
Gas Dryers, Induction Cooktops, Robotic Vacuums, Antivirus, Phone/TV/Internet Bundles). The
field is always present and always boolean. Rare enough that it will not be exercised by casual
testing, which is an argument for carrying it in every shape rather than only in drilldowns.

## 9f. Sequential checks of the remaining open questions — 2026-09-02

### Paywall boundary outside appliances — HOLDS

Member-vs-anonymous diff on four non-appliance categories (Digital Bathroom Scales `c34485`,
Banks `c37154`, Video Baby Monitors `c201196`, Dutch Ovens `c200210`):

| Field | Anonymous | Member |
|---|---|---|
| `overallDisplayScore` | 0 | all |
| `numeric-rating-score` values | 0 | all |
| `isRecommended` | 12 · 0 · 2 · 10 | **identical** |
| `price`, `_overallSortIndex` | full | identical |

Same two things gated, now confirmed across **five franchises** (appliances, health, money,
babies-kids, home-garden). Only electronics-computers lacks a paired diff.

**Survey scores are absent in all four — in both states.** Owner satisfaction and predicted
reliability are 0/N for members too. They are not paywalled outside appliances; they are simply
not collected for those product types. Any hardcoded "owner_satisfaction is always available"
would be wrong for most of the catalogue (`SPEC.md` §10).

**Banks carry no `price`** (0 in both states) — services priced differently.

### `_overallSortIndex` accuracy — SUPERSEDED by §9g

> **SUPERSEDED 2026-09-02.** Still whole-category measurements. The span-ratio hypothesis
> developed below, and the claim that contiguity is "the provable test", are both **withdrawn**
> — §9g shows fidelity is not a per-category property at all: every category is exactly ranked
> within its groups, and Dutch Ovens is 100% exact while spanning 1–26 for 20 products (not
> contiguous). `_groupName` is the discriminator, and no geometric predictor is needed.

| Category | n | index range | ratio | pairwise agreement | true best at |
|---|---|---|---|---|---|
| Banks | 69 | 1–69 | 1.00 | **100%** | 1st |
| Digital Bathroom Scales | 23 | 3–30 | 1.30 | **100%** | 1st |
| Dutch Ovens | 20 | 1–26 | 1.30 | **100%** | 1st |
| Video Baby Monitors | 6 | 1–6 | 1.00 | **100%** | 1st |
| Gas Ranges | 66 | –178 | 2.70 | 96.6% | — |
| French-Door Refrigerators | 172 | 4–348 | 2.02 | 88.2% | 18th |

**Anonymous ranking is exact in most categories.** It degrades only where the category is a
*sub-type* within a larger product group, so the index ranks the whole group and the category's
members are interleaved with siblings. `scid != cid` on every category tested, so that is not the
discriminator; **the index-span-to-count ratio is** — ratio ≈ 1 means a dense local rank.

The relationship is not monotonic (gas range at 2.70 beats French-Door at 2.02), so ratio is a
signal, not a proof. This section originally proposed contiguity as "the provable test"; **that
is withdrawn** (§9g) — contiguity is sufficient but not necessary, and grouping by `_groupName`
removes the need for any geometric predictor.

### Largest category — 172, unchanged

Sampled 18 further categories, three per franchise, plus the 11 already parsed. Largest found:
All-In-One Printers 159, Noise-Canceling Headphones 123, against French-Door Refrigerators' 172.
**Nothing approaching 200** across ~29 categories. *(Superseded by §12c: two categories exceed
200. This sample could not have found them — it drew from the A-Z index, which omits both.)* `modelCounts` on the page is *not* a usable
proxy for payload size (52 tested / 81 dna / 16 non-tested vs 172 actual), and HEAD returns no
`content-length`, so a full 236-category census would require full GETs and is not worth it.

### Some A-Z index URLs redirect — the fetcher MUST follow redirects

Four sampled categories returned no payload on a non-following GET. Three were redirects to a
shorter canonical path (`/health/milk-milk-alternatives/plant-milk/c200228/` →
`/health/milk-milk-alternatives/c200228/`), and all three carry a normal payload once followed:
34, 50 and 14 products. The fourth was a 404 on a category id guessed by hand, not one from the
index.

**Implication:** the canonical URL can differ from the A-Z index URL while the `cNNNN` id stays
the same — which is why the cache keys on `category_id`, never on URL.

### Session lifetime — still unanswerable

Only elapsed time answers it. The renewal mechanism is confirmed (§5); nothing further can be
measured today.

## 9g. The anonymous sort index is identical to the member one

Fetched each category twice — once with the live member jar, once from a brand-new cookie-free
browser context — and joined on product id:

| Category | products | `_overallSortIndex` identical | anon scores | member scores read in anon order |
|---|---|---|---|---|
| Banks | 69 | **69/69** | all null | descending, no break |
| Dutch Ovens | 20 | **20/20** | all null | descending, no break |
| French-Door Refrigerators | 172 | **172/172** | all null | descending, **breaks at position 5** |

**Anonymous ordering therefore reproduces CR's own ordering exactly** — the field is the same
field, not an estimate. This is categorically different from the score derivation rejected in §4:
that would infer a hidden number; this reads a published one.

### RETRACTED — the divergence was a measurement artifact

The table below was produced by sorting a whole category by `_overallSortIndex` and reading
member scores. **That comparison is invalid.** Products carry `_groupName` / `_groupId`, and CR
renders each group as a **separate ratings table**. Grouping first:

| Category | whole-category agreement | per-group agreement | groups |
|---|---|---|---|
| Exterior Paints | 69.5% | **100% (all)** | 2 |
| All-In-One Printers | 83.7% | **100% (all)** | 4 |
| French-Door Refrigerators | 88.2% | **100% (all)** | 3 |
| Top-Freezer Refrigerators | 90.3% | **100% (all)** | 3 |
| Side-by-Side Refrigerators | 92.7% | **100% (all)** | 2 |
| Gas Ranges | 96.6% | **100% (all)** | 2 |

**17 of 17 groups are exactly score-ordered.** `_overallSortIndex` is a faithful score rank
within a display group. The groups correspond exactly to the `categories` filter — option counts
match group counts in all six (3/3, 3/3, 2/2, 2/2, 4/4, 2/2).

Direct confirmation in a member render of
`c37162`: three tables — `30 INCH AND NARROWER WIDTHS (8)`, `31 - 33 INCH WIDTHS (12)`,
`34 INCH AND WIDER WIDTHS (152)`. 8+12+152 = 172. Each table descends internally, and the second
table opens **higher** than the first one closes; concatenated in page order that reproduces
exactly the break at position 5 reported above as evidence of divergence. So the break is a table
boundary, and the "true best at 18th" is the top of the third table — where CR's own member page
puts it.

**Scores are not comparable across groups.** Sorting a whole category by `overallDisplayScore`
mixes separately-scored populations, which is why CR never displays it that way. This applies to
**members too**, not just anonymous callers.

> Cross-group index order does not track cross-group score order (that is the 69.5–96.6% column).
> Whether that is because scores are group-relative or because the global index interleaves groups
> on another key is not established — but the correct behaviour is the same either way: rank
> within group, never across.

### Original (retracted) whole-category measurement

| Category | n | pairwise agreement | adjacent inversions | true best at | span ratio |
|---|---|---|---|---|---|
| Smartphones | 73 | **100%** | 0 | 1st | 3.85 |
| Banks | 69 | **100%** | 0 | 1st | 1.00 |
| Infant Car Seats | 46 | **100%** | 0 | 1st | 2.30 |
| Digital Bathroom Scales | 23 | **100%** | 0 | 1st | 1.30 |
| Dutch Ovens | 20 | **100%** | 0 | 1st | 1.30 |
| Bottom-Freezer Refrigerators | 20 | 98.9% | 1 | 1st | 1.80 |
| Gas Ranges | 66 | 96.6% | 4 | 1st | 2.70 |
| Side-by-Side Refrigerators | 54 | 92.7% | 1 | 1st | 2.22 |
| Top-Freezer Refrigerators | 58 | 90.3% | 16 | 1st | 1.10 |
| French-Door Refrigerators | 172 | 88.2% | 6 | **18th** | 2.02 |
| All-In-One Printers | 159 | **83.7%** | **36** | 2nd | 1.64 |
| Exterior Paints | 15 | **69.5%** | 2 | 2nd | 1.53 |

`_overallSortIndex` was identical anonymous-vs-member in **all 12**. So the ordering is reproduced
faithfully; the question is only whether CR's ordering *is* score ordering, and **in 7 of 12 it is
not** — from a single inversion up to 36 across 159 products.

**`_overallSortIndex` is therefore not a score rank.** It is a default display order that often
coincides with score order and sometimes diverges materially.

### Why no geometric predictor was found

Contiguity and span-ratio were both proposed as predictors of fidelity and both failed. They
failed because they were predictors of the wrong thing: fidelity is not a per-category property
at all. Every category is perfectly ranked **within its groups**; the whole-category number only
measured how many groups a category has and how much their score ranges overlap. The categories
scoring 100% whole-category (Banks, Smartphones, Dutch Ovens, Scales, Car Seats) are simply the
ones with a single display group.

`_groupName` is the field that was needed throughout. It ships in every product record.

An earlier draft of `SPEC.md` proposed labelling a ranking "exact" when the index is dense and
contiguous over `1..n`. **Dutch Ovens spans 1–26 for 20 products — not contiguous — and is 100%
exact.** Contiguity is sufficient, not necessary. The span ratio is not monotonic either (gas
range 2.7x beats French-Door 2.0x).

**No reliable anonymous predictor of score-order fidelity is known.** What is always true is that
the order is CR's own, so that is what the envelope claims (`SPEC.md` §5).

Note also that CR's own `sort` filter maps `overallScore` → `product.overallDisplayScore`, which
is null anonymously — so `_overallSortIndex` is very likely the order CR itself renders for
logged-out visitors, which is exactly what this reproduces.

## 9h. Structural checks — 2026-09-02 (anonymous)

Five categories across three franchises: French-Door Refrigerators `c37162`, Top-Freezer
`c28722`, Banks `c37154`, Smartphones `c28726`, Robotic Vacuum and Mop Combos `c201152`.

### The `type` filter is a no-op — 5 of 5

| Category | `type` options |
|---|---|
| French-Door Refrigerators | 1 — `{id: 37162, label: "French-Door Refrigerators"}` |
| Top-Freezer Refrigerators | 1 — `{id: 28722, …}` |
| Banks | 1 — `{id: 37154, …}` |
| Smartphones | 1 — `{id: 28726, …}` |
| Robotic Vacuum and Mop Combos | 1 — `{id: 201152, …}` |

**Every `type` filter contains exactly one option: the category you are already on.** It is a
label, not a filter — selecting it is a no-op. Product-*type* scoping is not done here (below).

### Product-type scoping lives in `args`, not in a filter

`args` carries the family structure directly:

- `scid` / `scat` — the supercategory (`28978`, "Refrigerators").
- **`cats[]` — the sibling categories**, i.e. the product types: Top-Freezer, Bottom-Freezer,
  French-Door, Side-by-Side, Built-In, Mini Fridges, Outdoor (7 on `c37162`). Each with
  `typeURL` and `reliabilityURL`.
- **`subcats[]` — the display groups** (3 on `c37162`), each with its own `typeURL`.

This is the axis that keeps microwaves out of a refrigerator search, and it is free on every
category fetch.

> **`cats[]` MIXES category ids with product-type entries whose `id` is a SLUG (measured
> 2026-09-06).** Front-Load Washers `c28739` and Electric Dryers `c30562` both ship a `cats[]`
> entry with `id: "washer-dryer-pairs"` beside the numeric siblings. So `id` is not always a
> `cNNNN` number, and a family parser that does `int(id)` raises on CR's own content: both
> categories answered a bare `ValueError` instead of an envelope, for every caller on every
> call (`cr_ratings`, `cr_filters` and `cr_product` alike), until non-numeric ids were skipped
> (`ingest._int` → `None`; a slug keys no category and is never guessed at). The numeric
> siblings on the same page are kept. This was the first case of CR's *own* payload, rather
> than caller input, reaching an `int()` unguarded.

### `categories` option ids ARE `_groupId`

| Category | `categories` options | distinct `_groupId` | identical |
|---|---|---|---|
| French-Door | 200367, 200369, 200371 | same three | **yes** |
| Top-Freezer | 200378, 200379, 200380 | same three | **yes** |
| Banks / Smartphones / Robotic Vacuums | **0 options** | 1 (== `cid`) | n/a |

Single-group categories ship an **empty** `categories` filter, not a one-option one.

### `_groupName` on a single-group category is the category name

Banks: `_groupId` = `37154` (== `cid`), `_groupName` = `"Banks"`, all 69 products. So **`group`
is never null** — a single-group category names itself. Nesting is still decided by distinct
`_groupId` count.

### Sibling categories share no products

French-Door (172) ∩ Top-Freezer (58) = **0**. The product sets are disjoint.

### A subcategory URL redirects but keeps its id, and serves the PARENT payload

`GET /appliances/refrigerators/30-inch-and-narrower-widths/c200367/`

- 301 → `/appliances/refrigerators/french-door-refrigerator/c200367/` — the **path** becomes the
  parent's slug, the **id stays `c200367`**.
- The payload is the parent's: `args.cid = 37162`, 172 products — not the 8 products in that
  width group.

**So the requested id, the final URL's id and `args.cid` can all disagree.** The cache must key
on **`args.cid`**; keying on the requested id files French-Door's 172 products under `c200367`.

### A bad category id returns HTTP 500, not 404

`c29027` and `c28978` (both invented) → `500`, 9,206 bytes, no payload. CR does not 404 an
unknown category. Since wafer retries 5xx, an unvalidated id costs four requests to learn
nothing — validating against the A-Z index before fetching is load-bearing, not a nicety.

### The A-Z index: 236 categories, and the anchor text is wrapped

236 distinct `cNNNN` ids with display names, matching §8. **The link text is inside a nested
`<span>`**, so `>([^<]+)</a>` matches 2 of 236. Strip inner tags within the anchor instead.
Hrefs are a mix of absolute and root-relative.

Corrected ids: **Robotic Vacuums = `c35183`** (8 products) — `c201152` is a different, larger
category ("Robotic Vacuum and Mop Combos", 21). Smartphones = `c28726`. Flat-Top Grills =
`c200796`. Phone TV Internet Bundles = `c34937`.

### `categoryAttributes`: nine blocks, and it is NOT a `window.*` assignment

> **Superseded by §10f.** The blocks sit inside a `console.log('[ ratings-wrapper ]', …)` object,
> and `.cat` names the current category's block directly — so neither the dedupe nor the
> `profileImage.fileName` key described below is needed. Kept as the trail that located them.

Nine occurrences at ~2.79–2.98 MB into the HTML (one more at ~6.5 MB), nested inside a larger
JSON structure — **no `window.*` assignment within 260 KB before the first**. The block for the
current category appears **twice**, so extraction must dedupe. Each block sits beside its
category's metadata: `ratedModelsCount`, `modelMin/MaxOverallDisplayScore`,
`productGroupIntroText`, and `profileImage.fileName` of the form `37162-hero-french-door-…`.
On `c37162` the block carries **44 definitions** against `attrs`' 24.

### TRAP: `attributeTypeName` is a homonym

The same key means different things on the two objects, and reading the wrong one silently
breaks coercion:

| Object | `attributeTypeName` | dataType lives in |
|---|---|---|
| `categoryAttributes` definition | `PRICING` / `SPEC` / `TEST_RESULT` | **`attributeDataTypeName`** |
| product `attrs[]` entry | `numeric-rating-score`, `text`, … | `attributeTypeName` itself |

An implementer joining definition → entry and reading `attributeTypeName` off the definition gets
`"SPEC"`, which is in no coercion table.

### A seventh dataType

Definition `attributeDataTypeName` values across the full dictionary: `boolean`,
`numeric-general`, **`numeric-overall-score`**, `numeric-price`, `numeric-rating-score`, `text`.
`numeric-overall-score` does not appear in the §1 counts, which were taken over `attrs`.

### Category score ranges are on the CATEGORY page, not just the reliability page

38 `modelMinOverallDisplayScore` occurrences on `c37162`, one per sibling block, **anonymously**:

| Sibling | min–max | rated |
|---|---|---|
| `c28722` Top-Freezer | 28–82 | 58 |
| `c28719` Bottom-Freezer | 56–85 | 20 |
| `c37162` French-Door | 43–79 | 172 |
| `c28721` Side-by-Side | 49–74 | 54 |
| `c28720` Built-In | 38–78 | 30 |
| Compact Refrigerators | null–null | 0 |
| Mini Fridges | 50–80 | 5 |
| Outdoor Refrigerators | 28–84 | 8 |

Identical to the reliability payload's values (§9c), so **`cr_categories` needs no reliability
fetch to serve score ranges** — one category fetch enriches its whole family. Note the
reliability payload lists **9** categories where `args.cats` lists **7** (it adds Compact
Refrigerators and Refrigerator drawers), so a fan-out list should come from whichever payload is
being parsed, not assumed equal.

Score range and survey data are independent: Mini Fridges and Outdoor have ranges with
`HasReliabilityData: false`.

## 10. Spike results — 2026-09-03

Run through **wafer-py 0.4.9** (the published release, not the working checkout), anonymously
except where noted. Scripts in the gitignored `scratch/spikes/`.

### 10a. Cookie injection and scoping — Spike A1

No network needed. `add_cookie(raw, url)` then `get_cookie(name, url)`:

| Injected as | visible at `www.` | visible at `secure.` | over `http://` |
|---|---|---|---|
| `hash=…; Domain=.consumerreports.org; Path=/; Secure` | yes | **yes** | no |
| `hash=…; Path=/; Secure` (no Domain) | yes | **no** | no |
| `hash=…; Domain=consumerreports.org; Path=/; Secure` | yes | **yes** | no |

`cookie_scope_summary()` normalises the leading dot away (`domain: consumerreports.org`), so the
dotted and undotted forms are equivalent. **Omitting `Domain` makes the cookie host-only and it is
never offered to `secure.consumerreports.org`** — which is where the re-mint redirect goes. The
`Secure` flag is honoured: nothing is exposed over plain http.

**An injected cookie is genuinely transmitted.** Proven against an echo endpoint:
`httpbin.org/cookies` returned `{"cookies": {"probe": "dddd…"}}` for an injected value, and the
cookie remained in the jar afterwards.

### 10b. CR REJECTS an invalid `hash` — the anonymous-fallback assumption is WRONG

The most consequential result of the spikes. Same URL (`c35183`), three credential states:

| Cookie state | redirects | final URL | bytes | payload | `data-subscriber` |
|---|---|---|---|---|---|
| **none** | 0 | the category URL | 931,269 | **yes** | `false` × 88 |
| **invalid `hash` (36 chars)** | 2 | `secure.consumerreports.org/ec/login?error` | 51,500 | **no** | **0 of either** |
| **invalid `hash` (wrong length)** | 2 | `secure.consumerreports.org/ec/login?error` | 51,500 | **no** | **0 of either** |

The redirect chain is `…/c35183/` → `302` → `/ec/login?loginMethod=auto` → `302` →
`/ec/login?error`. CR **validates the credential, rejects it, and clears the cookie** — `hash` is
gone from the jar afterwards.

**`SPEC.md` §5/§7 assumed a rejected cookie returns the ordinary anonymous page with a `200`, and
built `session_expired` detection on `data-subscriber="false"` + a configured cookie.** That
holds for *no* cookie. It does **not** hold for a *bad* one: there is no payload to parse and no
marker of either value, so the specified rules would fire `payload_missing` — a loud schema-drift
alarm — for an ordinary expired credential, and negatively cache it for an hour.

**The reliable signal is the final URL**: `/ec/login` in `resp.url`, available before any parsing.

> **Scope of this result.** Measured with a *malformed* `hash`. A genuinely *expired* one was not
> tested — that needs a real member cookie that has lapsed. §5's ablation removed cookies
> entirely (→ anonymous page); it never presented a malformed one. Both states must be handled;
> only the malformed case is measured.

### 10c. Cookie loss is not caused by rotation — Spike A5

Injecting a dummy `hash` and fetching CR emptied it from the jar under **every** session config,
including `max_rotations=0, max_failures=None`, with `rotations == 0` and the client object
unchanged. That is **not** wafer discarding the jar: it is §10b — CR rejecting the credential and
clearing it. The httpbin control (§10a) shows an injected cookie surviving a request normally.

Two things follow. `resp.rotations` is **not** a usable detector of credential loss (it read `0`
throughout), which leaves `get_cookie("hash", …)` as the only reliable check — as specified. And
the empty-jar-on-rotation hazard remains real but was **not** what this measured.

### 10d. Fetch timing — Spike A7

`c37162`, 10.5 MiB decompressed: `elapsed` 0.22 s edge-cached, 2.04 s otherwise (~5 MiB/s). The
`CR_TIMEOUT_S=120` / `CR_ATTEMPT_TIMEOUT_S=60` pair has ample headroom.

### 10e. `filterInstanceDATA` extraction — Spike B1

Brace-matching from the first `{` after `=`, string- and escape-aware, succeeded on **6 of 6**
categories; the terminator is always `;\n`. All six carry exactly `{args, attrs, data, filters}`.
Offsets cluster at 289–312 KB. Product counts: 172, 69, 34, 58, 16, 8.

### 10f. `categoryAttributes` lives in a `console.log` — Spike B2

The nine blocks are the second argument of a **debug statement left in production**:

```js
console.log('[ ratings-wrapper ]', { … })   // ~6.9 MB object, at ~1.47 MB into the page
```

It sits in the same `<script>` as `filterInstanceDATA`, immediately after it. Its shape:

| Path | Contents |
|---|---|
| `.cat` | **This category's own block** — `_id`, `categoryAttributes`, score range, counts |
| `.productFilterPayload.debug.categories[]` | The sibling categories, each the same shape |
| `.subcats` | The display groups |
| `.scat` | The supercategory |

**`.cat._id == args.cid` on 6 of 6 categories**, so selection is direct — there is no need to
search nine blocks, dedupe a repeat, or key off `profileImage.fileName`. The sibling id key is
**`_id`**. Sibling counts vary by family (8, 8, 7, 4, 2, 1), so the list must come from the
payload, never a constant.

Blocks carry `_id`, `productGroupName`, `productGroupSlugName`, `parentProductGroupId`,
`modelMin/MaxOverallDisplayScore`, `modelCounts`, `HasReliabilityData`,
`HasOwnerSatisfactionData`, `productGroupIntroText` and `categoryAttributes`.

> **This anchor is a build artifact and should be treated as the most fragile thing in the
> project.** A `console.log` can be stripped by a minifier or a cleanup commit at any time, with
> no other change to the page. The `attrs` → `attributeTypeName` fallback chain is what keeps the
> server working the day it disappears, so that chain is load-bearing, not defensive decoration.

### 10g. Remaining Spike B checks

- **A-Z index carries no `data-subscriber`** — zero of either value, matching the reliability
  page. Confirms that only category pages may update session state.
- **Redirects** — `/health/milk-milk-alternatives/plant-milk/c200228/` redirects once to the
  shorter canonical path; payload intact, 34 products, `args.cid` 200228.
- **Banks** — `_groupId` = `cid` = 37154, `_groupName` = `"Banks"`, `categories` filter has **0**
  options, `price` is `None`.
- **`subcats` can list a group that has no products.** `c37162` ships 4 `subcats` but only 3
  distinct `_groupId` values across its 172 products. Nesting must key on distinct `_groupId`
  from the data, never on `len(subcats)`.
- **Storage sizes** — the family block serialises to 109–1,063 bytes and `categoryAttributes` to
  1.3–26 KB, against a 17 KB–1.2 MB payload. Storing family in the ingest envelope is free.

### 10h. Response sizing — Spike C

Anonymous payload with deterministic score-fill (shape-equivalent to a member response); counted
with `tiktoken` `cl100k_base`.

**`cr_ratings`, tokens:**

| detail | flat 25 | nested 5 | nested 10 | nested 25 |
|---|---|---|---|---|
| `summary` | 1,487 | 925 | 1,672 | 2,674 |
| **`standard`** | **3,012** | 1,840 | **3,380** | 5,419 |
| `full` | 22,136 | 13,371 | 24,837 | **39,878** |

(4-group `c200228`: `standard` nested-10 = 3,559 — the group count matters less than expected.)

- **The nested default of 10 is confirmed** at ~3.4k tokens.
- **`SPEC.md` §7's "~2,014 tokens" for flat `standard`/25 is low: the real figure is 3,012**, +50%
  rather than the predicted +15–20%, because `owner_satisfaction` and `predicted_reliability`
  moved into `standard`.
- **`full` does not belong at `limit=25`** — 39,878 tokens nested.

**`cr_filters` as specified is 36,007 tokens — unusable.** 94% of it is the `features` filter,
whose options embed every distinct value in the category: "Total usable capacity" alone is 9,431
tokens. Capping `price`'s 254 labels saves 1,494 and misses the point.

| Variant | Tokens |
|---|---|
| As CR ships it | **36,007** |
| Collapse numeric `data[]` to `{min,max}`, keep descriptions | **3,502** |
| …without descriptions | 2,891 |
| …keeping every non-numeric value | 4,284 |

Collapsing numeric value lists to a range is what does the work; truncation is not needed.

**Other shapes:** `cr_product` 885 tokens (2,262 with descriptions — the 2.2× ratio §7 predicts).
`cr_categories` fully enriched over all 236 A-Z rows is **19,569** tokens, against 6,590 for
id/slug/name/franchise alone — **scale both by ~1.47x for the true 346** (§12a): roughly 28,700
and 9,700 — so it needs a default scope, not an unfiltered dump.

### 10i. Member-session steps — Spike A2/A3/A4/A6, 2026-09-03

First member traffic ever sent to CR through wafer (all previous member measurement was
Playwright). Target `c35183`, 8 products.

**A2 — `hash` alone re-mints a full member session through wafer.** Injected as the only member
cookie:

| | Result |
|---|---|
| status / final URL | `200`, the category page (not a login page) |
| redirects | **2**, via `secure.consumerreports.org/ec/login` |
| `data-subscriber` | `true` × 88, `false` × 0 |
| `overallDisplayScore` | **8/8 populated** |
| `numeric-rating-score` values | **72/72 populated** |
| `hash` after | still in the jar |
| `userLicenses` after | **re-minted, 197 bytes, value differs from the captured one** |

So §5's browser-measured re-mint reproduces exactly through a plain HTTP client, and the rotation
that `SPEC.md` §6 requires the client to persist is real and observable.

> **TRAP: `/ec/login` appears in the redirect chain on SUCCESS too.** The successful re-mint above
> passes through the same login host as the rejection in §10b — the difference is only where it
> *ends*. Detection must read `resp.url` (the final URL); a check against `resp.history` would
> report every healthy member fetch as a rejected credential.

**A3 — the re-mint is once per session.** A second request on the same session: `200`, **0
redirects**, still `data-subscriber="true"`, 8/8 scored.

**A4 — `hash` + a SUPERSEDED `userLicenses` authenticates.** Injecting the original (now
superseded) `userLicenses` alongside `hash`: `200`, no login redirect, `data-subscriber="true"`
× 88, 8/8 scored. CR does not reject a superseded entitlement token when a valid `hash`
accompanies it.

> **A4 tested SUPERSEDED, never LAPSED, and the conclusion it was given did not survive.** The
> token here had been replaced minutes earlier by the re-mint in A2 — it was still inside its
> ~24 h `t` window. It was read as "so `auth` storing both cookies is safe", which held for a
> day and then took the session down every time (§5, measured 2026-09-08): past `t + ~24 h` the
> same token vetoes the re-mint. A stored `userLicenses` is *always* the lapsed kind by the time
> a cold start sends it, so what A4 licensed is precisely what §5 now forbids. A4's own reading
> stands: **storing `hash` alone works**, which is what the store does now.

**A6 — nothing member-identifying reaches the cache.**

| Check | Result |
|---|---|
| `args` keys, member vs anonymous | identical; **no key differs in value** |
| `hash` value present in stored `filter_instance` | no |
| `userLicenses` value present | no |
| `"userInfo"` present | no |
| `"@"` (email-shaped) present | no |
| same checks against the stored `.cat` block | all no |

So `SPEC.md` §6's "the cache holds response data only" is now measured rather than asserted.

### 10j. `categoryAttributes` ARRAY ORDER IS NOT STABLE — and it decided `detail="standard"`

The `.cat` block is *not* byte-identical between tiers, and the cause matters:

| Comparison | Result |
|---|---|
| same set of `attributeId`s | **yes** (30 vs 30) |
| same array **order** | **NO** — member starts `[7738, 2942, 31, 10437, 765]`, anonymous starts `[7614, 764, 1027, 7738, 2591]` |
| any entry differing in **content** | **none** (0 of 30) |

Same data, shuffled. The consequence for `detail="standard"`, which takes the first three
`numeric-rating-score` attributes:

| Selection rule | Member | Anonymous | Tier-stable |
|---|---|---|---|
| **by `sortOrder`** | Carpet, Bare floors, Navigation | Carpet, Bare floors, Navigation | **YES** |
| by declaration order | Edges, Data privacy, Bare floors | Carpet, Pet hair, Data security | **NO** |

An earlier draft of `SPEC.md` defined `standard` as "the first three in `attrs[]` **declaration
order**", justified as "a fixed, category-stable order … so anonymous and member responses have
the same shape". **That justification was false and the rule was a silent bug**: the same category
would have returned three entirely different attributes depending on whether a session was
configured. Sorting by `sortOrder` is what actually delivers the promised property.

**`sortOrder` is absent on some entries in both tiers**, so the sort needs an explicit tiebreak:
missing `sortOrder` sorts last, ties broken by `attributeId`. Without it the comparison is
either unstable or raises on `None`.

## 11. Cars — a second architecture, and a paywall enforced only in the UI

Measured 2026-09-03, anonymously and against a live member session. Cars were previously recorded
as out of scope on the basis that they "run on a separate API with a different structure". That is
true, and the separate API turns out to be **larger, cleaner and completely unauthenticated**.

### 11a. Nothing about the products architecture applies

| | Products | Cars |
|---|---|---|
| Data source | `filterInstanceDATA` embedded in the category page | a JSON API |
| Auth marker | `data-subscriber` in the HTML | **`window.isSubscriber`** in the HTML |
| Discovery | sitemaps + A-Z index, 346 categories | `cr/keys`, **7,095 model-years** |
| Auth to read data | member cookie | **none — a public app key only** |

A car page carries **no `data-subscriber`** (0 of either value) and no `filterInstanceDATA`. It
carries `window.initStore` (~5 KB: make, model, modelYear, `currentCarId`, `testCarIds`, and an
environment block holding the API base and key) plus `window.isSubscriber` / `window.isAnonymous`,
server-rendered so a plain GET sees them.

### 11b. The endpoints

Host `cars-api.consumerreports.org`, authenticated by a static **`x-api-key`** header. The key is
public — it ships in the car page's own environment block as `DRS_CARS_API_API_KEY`, 40 chars — so
it is read from the live page at runtime, never pinned. **No cookie is required or used.**

| Endpoint | Bytes | Contents |
|---|---|---|
| `v1/cr/makes` | 16 KB | every make |
| `v2/cr/carTypes?slugCarTypeName=` | 49 KB | 7 car types then categories, with `categoryId`, tested/untested counts, price and range bands |
| `v1/cr/keys` | 1.3 MB *filtered* | the identity index. The figures below — **42 makes, 248 models, 2,653 model-years** — are the `modelYearCarTypeId=105` (sedans) slice, which is how it was first measured; **unfiltered it is 50 / 677 / 7,095 and 3.1 MB** (§13a). Each entry has `modelYearId`, `modelYearCarTypes[]`, `modelYearStates[]` (New/Used) |
| **`v2/cr/modelYears/{modelYearId}`** | 57 KB | **the ratings payload** — one model-year in full |
| `v1/crReliabilitySurvey/modelYearId/{id}` | 726 B | survey detail |
| `v1/cr/glossary/1` | 53 KB | the attribute dictionary — names and descriptions for every rating |

> **A wrong path returns `403`, not `404`.** Sixteen candidate paths were probed; the twelve that
> do not exist all answered `403` with a 43-byte body. This is API-Gateway behaviour, and it means
> a typo is indistinguishable from a permission problem unless you know the route is real. Treat
> `403` on cars as "no such route" and never as "sign in".

### 11c. The ratings payload

`v2/cr/modelYears/23133` (2026 Acura RDX), 1,085 leaf fields, **zero nulls**:

- `modelYear.cars[].testRatings.overallTestScore` — **populated**, CR's headline Overall Score
- `.testRatings.roadTestScore` — populated
- `.testRatings.ratings[]` — named test groups (`ACCELERATION`, `TRANSMISSION`, `ROUTINE
  HANDLING`, ...) each with `compositeRatingScore` and per-test `testRatingScore`
- `.testRatings.featureGroupRatings[]`, `.testRatings.safetyVerdict.ratingScore`
- `.overallScoreSortIndex` — **the cars analogue of `_overallSortIndex`**
- `.ratingsCategory.overallRank`, `.overallTestScoreMax`, `.overallTestScoreMin` — rank within
  category **and** the category's score range, both explicit and both populated
- `modelYear.crPopularScore`, `modelYear.expertRatings.isRecommended` — **the string `"Y"`/`"N"`,
  not a boolean and not the products surface's `"Yes"`/`"No"`**, so it needs its own mapping
- `model.reliabilityRatings.predictedReliabilityScore` — populated
- `model.ownerSatisfactionRatings.factors[].predictedFactorScore`
- `modelYear.crashTestRatings[]` — NHTSA and IIHS
- `modelYearSpecs`, `carSpecs`, `fuelEconomySpecs`, `warranty`, `price`, `incentive`

### 11d. The cars API is FULLY AVAILABLE ANONYMOUSLY — verified twice

**Re-verified 2026-09-03 across 15 fetches**, SHA-256 byte-compared, anonymous (public
`x-api-key` only) against the same request carrying a valid member `hash`. Six model-years chosen
to span the catalogue — 3 New, 3 Used, oldest a 2003 model — plus every other endpoint:

| Endpoint class | Fetches | Identical |
|---|---|---|
| `v2/cr/modelYears/{id}` — the ratings payload | 6 | **6/6** |
| `v1/crReliabilitySurvey/modelYearId/{id}` | 6 | **6/6** |
| `v2/cr/carTypes`, `v1/cr/glossary/1`, `v1/cr/makes` | 3 | **3/3** |
| | **15** | **15/15, zero differences** |

Byte sizes ranged 116 B to 67,838 B and every SHA-256 matched. The first pass also flattened one
payload to 1,085 leaf fields: no member-only keys, no anon-only keys, **zero** differing values,
**zero** nulls.

**And the gated-on-the-website numbers are genuinely populated anonymously:**

Values are withheld here deliberately — the finding is *which fields come back populated to an
anonymous caller*, and printing CR's member-facing ratings for named cars would serve no purpose
this table does not already serve. (`•` = populated, `—` = CR publishes no value.)

| Car | overallTestScore | roadTest | rank | category range | predRel | Rec |
|---|---|---|---|---|---|---|
| Lexus IS 2026 (New) | **•** | • | • | • | • | • |
| Lexus ES Hybrid 2025 (New) | **•** | • | • | • | • | • |
| Lexus ES 2026 (New) | — | — | — | — | — | — |
| Lexus IS 2003 (Used) | — | • | — | — | — | — |
| Lexus IS 2017 (Used) | **•** | • | — | — | — | — |
| Lexus IS 2025 (Used) | **•** | • | — | — | — | — |

> **The nulls that do appear are real absences, not gating.** Lexus ES 2026 is untested; used cars
> carry no `overallRank` or category range because CR computes those for current models. Since the
> API never withholds, **a `null` on cars means "CR has no value" — never "you cannot see it"**,
> which is the exact inverse of the products surface. Any `scores_available` derivation for cars
> reports `absent`, never `unavailable`.

**Scale note:** the sedans slice of `cr/keys` alone holds 2,653 model-years, of which only **97
are New** and 2,556 are Used-only. The cars catalogue is overwhelmingly historical, and all of it
answers anonymously.

### 11d-ii. But CR does gate cars — in the presentation layer only

The same car page, fetched two ways:

| | anonymous | member |
|---|---|---|
| `window.isSubscriber` | **`false`** | **`true`** |
| page bytes | 648,873 | 698,496 |
| the score `74` rendered near "score" | **0 occurrences** | 5 |
| `roadTestScore` `81` rendered | **0** | 1 |
| "paywall" markers in the HTML | **39** | — |
| "become a member" | 2 | 1 |
| `overallTestScore` / `predictedReliability` / `isRecommended` / crash tests in the HTML | **0 each** | — |

The anonymous car page is effectively a marketing page: prices and fuel economy, an "Overall
Score" *label* with no value, and an upsell. Everything else is fetched client-side from an
endpoint that asks for nothing but a key CR publishes.

> **So the cars API leaks precisely what CR's own UI withholds.** This is categorically different
> from the products surface, where the paywall is enforced *in the payload* — read a product
> category anonymously and the gated numbers are genuinely `null`, so an anonymous fetch cannot
> obtain them. On cars, an anonymous fetch obtains everything.
>
> **This is a measurement, not a technique.** The endpoint requires no authentication and CR
> publishes the key in its own page source, so nothing here defeats a control — what it serves
> anonymously is what it serves anonymously. What the measurement establishes is the *gap*: CR
> renders these numbers only for members and marks the anonymous page as paywalled 39 times over,
> so its intent is unambiguous even though its API does not enforce it. **`SPEC.md` §5 serves cars
> in full regardless**: an earlier design gated them on `window.isSubscriber`, and that was
> withdrawn because it returned less than a plain HTTP request would.

### 11e. Still unmeasured for cars

- ~~Used cars.~~ **Measured** (§13e): the used A-Z is a 17.2 MB page yielding 305 model links;
  `/sitemaps/cars.xml` (51 per-make sitemaps) is the cheaper route and was not walked.
- ~~Whether `v2/cr/modelYears/` accepts multiple ids.~~ **Answered: no** (§13c) — the comma path
  is a `400` and the query filter is silently ignored, so a listing costs N requests.
- **Tires, car seats and car batteries** are *products*, not cars — they live in the A-Z index and
  are already covered.
- **Rate limits and WAF behaviour on `cars-api`** — 20-odd requests were sent without incident.

## 12. Discovery — the A-Z index is INCOMPLETE, and sitemaps are the real index

Measured 2026-09-03, anonymously. This corrects §8 and every claim resting on "236 categories".

### 12a. Categories the A-Z index does not list

The A-Z index at `/cro/a-to-z-index/products/index.htm` lists 236 categories. It is **not the
catalogue.** Confirmed missing, each serving a completely normal `filterInstanceDATA` payload:

| Category | id | Products | Groups | In A-Z? |
|---|---|---|---|---|
| **Televisions** | `c28700` | **303** | 6 | **no** |
| **Mattresses** | `c28705` | **293** | 3 | **no** |
| Tablets | `c34387` | 38 | 2 | **no** |
| Sound bars | `c28698` | — | — | **no** |
| Dishwashers | `c28687` | — | — | **no** (confirmed 2026-09-06) |

Televisions, mattresses and dishwashers are among CR's best-known ratings. A search of the index
for `televis`, `speaker`, `tablet` and `watch` returns **zero** hits, and `mattress` returns only
"Mattress Stores" — a services category, not the mattresses themselves.

**The full crawl was run 2026-09-03 — all 195 sitemaps, 0 failures. The answer is 346
categories.**

| Source | Categories |
|---|---|
| A-Z index | 236 |
| **Sitemap crawl** | **346** |
| In sitemaps, **not** in the A-Z index | **110** |
| In the A-Z index, not in sitemaps | **0** |

**The sitemap union is a strict superset**, so it can replace the A-Z index for coverage outright
— nothing is lost by trusting it. The index is missing **47% more categories than it lists**.

Franchise spread of the 346 (by URL path): home-garden 108, appliances 90, health 58,
electronics-computers 38, babies-kids 23, money 23, and **6 product categories living under
`/cars/` paths** — tires, tire stores, dash cams, electric scooters. Those are *products*, on the
products surface, not the cars API (§11).

A sample of 10 of the 110 missing ids, fetched and parsed:

| id | Category | Products | Page |
|---|---|---|---|
| `c34960` | Wireless & Bluetooth Speakers | 178 | 6.6 MB |
| `c32968` | Humidifiers | 121 | 4.7 MB |
| `c33123` | Garbage Disposals | 64 | 1.9 MB |
| `c33043` | High Chairs | 32 | 1.3 MB |
| `c34702` | Homeowners insurance | 28 | 0.9 MB |
| `c37249` | Veggie burgers | 15 | 0.8 MB |
| `c201102` | Dash Cams | 15 | 1.1 MB |
| `c201047` | Portable Chargers | 14 | 0.7 MB |
| `c33597` | Rice Cookers | 6 | 0.6 MB |
| `c33007` | — | **404, no payload** | — |

Mainstream categories throughout — humidifiers, garbage disposals, high chairs, homeowners
insurance, Bluetooth speakers. Not obscure long-tail entries.

> **One of ten sampled ids 404s.** The sitemaps carry at least some stale entries, so a
> sitemap-sourced id is a *candidate* rather than a guarantee: a 404 on first fetch is expected
> occasionally and must be handled as "category retired", not as schema drift. One failure in ten
> is too small a sample to put a rate on.

> **An earlier draft treated the A-Z index as the complete discovery surface and cached it with a
> 90-day TTL.** That is wrong, and it fails in the project's worst direction: an agent asking for
> TV ratings would get `unknown_category` — "CR does not cover this" — for one of the most-rated
> categories CR publishes.

### 12b. The sitemaps ARE a complete, machine-readable index

```
/sitemap.xml                       -> 6 sitemaps: products, cars, video-hub, cda, cq, espanol
  /sitemaps/products.xml           -> 195 per-supercategory sitemap URLs
    /products/sitemap/{scid}       -> XML listing that supercategory's URLs, category ids included
  /sitemaps/cars.xml               -> 51 sitemaps: 50 makes plus a `types` index
```

Each `/products/sitemap/{scid}` is real XML (`<urlset><url><loc>`), not HTML — no scraping, no
nested-`<span>` trap. `/products/sitemap/28958` yields
`.../electronics-computers/sound-bars/c28698/`.

195 fetches gives a complete category list against 1 for the A-Z index, so the A-Z index stays as
the cheap warm path (it carries display names, §8) and the sitemap crawl is what makes coverage
*correct*. Both are cacheable with a long TTL.

### 12c. Categories DO exceed the 200-product cap

`RECON.md` §9f recorded "nothing approaching 200 across ~29 categories" and §11 listed "whether
any category exceeds 200" as unverified. **Answered: yes.** Televisions 303 and mattresses 293
both exceed the `limit` maximum of 200, and both were invisible to the sampling that produced the
172 figure because neither is in the A-Z index. `offset` is load-bearing, not precautionary, and
the TV page is 2.6 MB while mattresses is **14.4 MB** — larger than the 11 MB French-Door page
used for every size estimate in `SPEC.md` §7.

## 13. Cars — the rest of the API surface

Measured 2026-09-03, anonymously, extending §11.

### 13a. Scale, and a corrected count

`v1/cr/keys` **unfiltered** returns the whole catalogue: **50 makes, 677 models, 7,095
model-years**, 3.1 MB. §11 quoted 2,653 model-years — that was the `modelYearCarTypeId=105`
(sedans) slice, not the total.

`v2/cr/modelYears` (bare) reports the same total with counts:
`{modelYears: 7095, cars: 7095, makes: 50, models: 674}`. By state: **6,698 Used, 397 New.**
The car catalogue is overwhelmingly historical.

### 13b. The taxonomy repeats categories across car types, with different counts

`v2/cr/carTypes` returns **7 car types** holding **58 category slots but only 39 distinct
categories** — 15 categories appear under more than one type. Five of those report **different
counts depending on the parent type**:

| Category | Under | tested / notTested |
|---|---|---|
| `13762` Electric luxury SUVs | Hybrids/EVs | 9 / 10 |
| | SUVs | 9 / 10 |
| | Luxury Cars & SUVs | **14 / 11** |
| `11359` Electric SUVs | Hybrids/EVs | 14 / 9 |
| | SUVs | **13 / 8** |

So a cars "category" is not a globally unique population — it is scoped by its parent car type,
the same way a product `_groupName` scopes a ratings table. **A car category id alone is not a
complete address**; the pair `(carTypeId, categoryId)` is.

### 13c. What the endpoints will and will not do

| Attempt | Result |
|---|---|
| `v2/cr/modelYears/{id}` (path) | **the ratings payload** — full `testRatings` |
| `v2/cr/modelYears/{a},{b}` (comma path) | **`400`** — no batching |
| `v2/cr/modelYears?modelYearId=a,b` | `200`, **filter silently ignored** — returns the default page |
| `v2/cr/modelYears` (bare) | paginated *listing*, `size`/`page`/`totalElements`/`totalPages` |
| `...?page=2`, `?page=3` | echoed back as `pageNumber`, **but the same ids are returned** — paging does not work |
| `...?modelYearCarTypeId=` / `?categoryId=` | **ignored**, `totalElements` unchanged at 7,095 |
| `...?modelYearStateId=1` | **works** (6,698 Used / 397 New) — and switches the response to a lean `{"modelYearId": N}` shape |
| listing entries | carry `modelYear.cars[]` but **no `testRatings`** |

**Consequences for the design.** There is no bulk ratings call: a listing of N cars with scores
costs **N requests** to `v2/cr/modelYears/{id}`. *(This paragraph also said enumeration must come
from `v1/cr/keys` because the listing endpoint's paging is broken — superseded by §13c-ii: true of
`v2/cr/modelYears`, but `v2/cr/cars` pages correctly and is the listing source.)*

### 13c-ii. `v2/cr/cars` — the real listing endpoint, found 2026-09-03

`v2/cr/cars` rejects an unfiltered call with a `400` that **names its own filters**:

> `At least one the filter parameter must be provided: 'slugMakeName', 'modelYearStateId',
> 'carTypeSlugName'`

That is a far better listing surface than `v1/cr/keys`, and unlike `v2/cr/modelYears` it works:

| Property | `v2/cr/modelYears` (listing) | **`v2/cr/cars`** |
|---|---|---|
| Pagination | **broken** — `page=` echoes back, same ids | **works** — pages 1/2/3 return disjoint sets |
| Filters | `modelYearStateId` only; carType/category ignored | **`slugMakeName`, `carTypeSlugName`, `modelYearStateId`, and they combine** |
| Per-car data | identity only | identity **plus** the fields below |

**The `400` names only the *required-one-of* filters, not all of them.** Two more work and were
missed on the first pass because they are absent from that message — re-probed and confirmed
2026-09-03:

| Filter | Effect |
|---|---|
| `carTypeSlugName=suvs` | 299 (baseline) |
| `+ slugMakeName=acura` | 4 |
| `+ modelYearStateId=2` | 219 |
| **`+ categoryId=11359`** | **24** — the cars category filter, so `(carTypeId, categoryId)` is addressable |
| **`+ modelYear=2026`** | **187**, every row 2026; `2019` → 119, every row 2019 |
| `+ ratingsCategoryId=11359` | **ignored** — 299, unchanged. `categoryId` is the working name |

> **`queryParameters` is not a reliable "was my filter applied" check.** `modelYear` filters
> correctly but does **not** appear in the echo, while an undocumented `carsPerModel` does. Verify
> a filter by its effect on `totalElements`, never by the echo.

**What each entry carries** — one request, no per-car fetch:

`modelYearId`, `makeId`/`makeName`/`slugMakeName`, `modelId`/`modelName`/`slugModelName`,
`modelYear`, `modelYearStateId`/`Name` (New/Used), `carId`, `carVersionName`, `powerTrainType`,
`testStateId`/`testStateName` ("Test Completed"), `modelGenerationId`/`StartYear`/`Summary`
(prose), `crPopularScore`, **`safetyVerdictRatingScore`**, `fuelEconomySpecs`
(`annualFuelCostDollar`, `annualFuelConsumptionGal`, combined mpg), `incentive`
(`cashValueUpTo`, dates), `ratingsCopiedFrom*`.

> **`size` counts MODELS, not model-years.** `size=3` returned 18 `modelYearId`s on page 1 — each
> model brings all of its years. A caller asking for "10 cars" gets 10 models and however many
> years they carry, so any limit the tool exposes must be applied after the fetch, not passed
> through as `size`.

**Three further paging facts, measured 2026-09-03:**

- **`size` is capped at 50.** Above it the API answers `400` with
  `"default and maximum value of 'size' is 50"` — so a large `need` pages rather than widening.
- **`totalElements` counts models too**, matching `size`. The model-year population is
  `counts[name == "cars"].value`, and `counts` is a **list** of `{name, value}` objects, not a
  map. Reading `totalElements` as a row count understates the population by roughly the average
  number of years per model.
- **`page` is 1-based.** `page=0` comes back with `first: false`, which is the tell — a 0-based
  reading silently skips the first page and looks like a short result set rather than an error.

**An unknown model-year is a `200`, not a `404` (measured 2026-09-04).**
`v2/cr/modelYears/99999999` returns 81 bytes:
`{"response": {}, "responseSummary": {"responseCount": 0, "requestQueryParameters": []}}`, while a
real id returns `responseCount: 1` with `response.modelYear` populated. The route never 404s, so
"no such car" is only readable from the payload — and a client that does not read it caches an
all-null car and reports every score as an absence rather than a miss.

**`modelYear` is validated as a VALUE, and an empty listing is a dict (measured 2026-09-05).**
Against `slugMakeName=honda`: `modelYear` 1990, 1995, 1998, 1999, 2030 and 2035 → **`400`**
`{"errors":[{"httpCode":400,"category":"USER","key":"default","text":"'modelYear' must be a
valid year integer."}]}`; 2000, 2010, 2025, 2026 → `200` with rows; 2027 and 2028 → `200` with
**`"content": {}`** — an empty *dict*, not `[]` — and `counts` present. The local `car_index`
spans exactly 2000–2028 (7,095 rows: 131 for 2000, 342 for 2025, 3 for 2028). **The accepted
span IS the catalogue's, on both ends — re-measured 2026-09-06, anonymously, against
`slugMakeName=honda`:** 1999 → `400`; 2000 → `200` with 6 rows; 2028 → `200` with `content: {}`
(Honda has no 2028); **2029, 2030, 2031 and 2040 → `400`**, the same `'modelYear' must be a
valid year integer.` text. An earlier reading of this section put the ceiling at "roughly
2029" by extrapolation; 2029 had not been probed, and it is a 400. So there is no sliver of
years CR accepts with an empty 200 past the index max: a year outside the catalogue is a
*parameter* error CR answers with a 400, not an empty result; untranslated it reached the caller
as `fetch_failed(bad_request)`. The bound tracks CR's catalogue rather than a constant, so a
local index that lags a new model year (90-day TTL) would refuse max+1 while CR lists it — which
is why `validate_year` sends exactly max+1 through to CR. An unknown make (`slugMakeName=zzzz-nomake`) is a `200` with
`content: {}` and every `counts` value 0, and a `page` past the end is a `200` with `content: {}`,
`last: true` and the real `counts`.

### 13c-iii. There is still NO bulk ratings call — 11 further probes

`overallTestScore`, `roadTestScore` and the per-test `ratings[]` appear in **no** collection
response. Probed and rejected: `?fields=`, `?include=`, `?expand=cars` (all silently ignored,
byte-identical to the bare listing), `v1`/`v2` `cr/compare` (`403`), `v2/cr/cars?modelYearIds=`
(`400`), `modelYears/{a}/{b}` (`403`), `cr/testRatings` on both versions (`403`), and a
`ratingsCategoryId`-filtered listing (ignored). **`v2/cr/modelYears/{id}` remains the only source
of road-test data, one model-year per request.**

**The api-key is required**: the same call without the `x-api-key` header is a `403`.

### 13d. The API key needs no browser

`DRS_CARS_API_API_KEY` is extractable from any car page's server-rendered HTML with a plain
regex over a wafer GET — no JavaScript, no browser. So the "read the key from the live page at
runtime, never pin it" rule in `SPEC.md` §5 is implementable on the normal fetch path.

### 13e. Used cars

`/cars/types/used/ratings-reliability/` is a second A-Z index — a **17.2 MB** page yielding 305
distinct model links, disjoint in model-year from the new-car index (2025 vs 2026 entries for the
same models). `/sitemaps/cars.xml` lists 51 sitemaps — the 50 makes plus a `types` index — and is
the cheaper route.

### 13f. Other CR surfaces seen but not built against

Observed in passing while measuring; recorded so they are not mistaken for undiscovered:

- `member-service-api.consumerreports.org/api/member/v1/...` — the member's own profile, saved
  cars and notifications. **Member-identifying, deliberately never called.**
- `ecq-ecom-api-1.consumerreports.org` — commerce/alerts.
- `/sitemaps/cda.xml`, `/sitemaps/cq.xml` — the Digital Archive and CR Quarterly. Editorial, not
  ratings.

## 14. Typeahead — a category resolver, and NOT a product search

Measured 2026-09-03, anonymously. `SPEC.md` had dismissed the typeahead endpoints as "unverified
and belonging to the API family this project does not build against" — a principle that stopped
holding when `cars-api` came into scope, so they were tested.

**`/api/search/typeahead?query=` works, with no auth and no api-key.** The parameter is `query`
(not `q` or `term`, both `400`), and it requires **at least 3 characters**. Responses are 0.5–2 KB.

Each result is `{id, label, type, links}` with `type` one of `CATEGORY`, `SUPER_CATEGORY` or
`REDIRECT`, and `links` carrying `overview` / `ratings` / `recommended` paths — the `ratings` link
contains the `/cNNNN/` id:

| Query | Results | Notable |
|---|---|---|
| `refrigerator` | 10 | refrigerators, wine refrigerators `c33835`, outdoor `c201295`, compact `c29738` |
| `televisions` | 1 | **tvs → `c28700`** — the category the A-Z index omits |
| `mattress` | 5 | mattresses `c28705`, mattress toppers `c33632`, mattress stores `c34706` |
| **`B36CD10ENS`** (a model number) | **0** | — |

**The zero result for a model number is the finding that matters.** It confirms there is no
cross-category *product* search behind this endpoint, so `SPEC.md` §7's decision to keep live
product search out of v1 stands on evidence rather than assumption.

As a *category* resolver it is better than local matching, because it is CR's own matching rather
than substring comparison: it resolves the synonym `televisions` → `tvs`, and ranks related
categories. `SPEC.md` §7 uses it as `cr_search`'s first category source with the local index as
the fallback.

`/api/search/integration/typeahead?query=` also answers (2.6 KB) with a different shape —
`redirect[]` and `suggestedSearch[]`, including product-finder redirects. Not needed for v1.

## 15. Still unverified

Everything the spikes closed has moved to §10. What remains:

**Needs elapsed time, not effort**

- **Whether a captured session survives its expected ~year.** The renewal mechanism is confirmed
  end to end (§5, §10i) — only the calendar proves CR does not invalidate sessions for other
  reasons. Answered by using the server and noting when a rejection first appears.

**Needs a state we cannot manufacture**

- ~~Why the 2026-09-05 capture died after about a day.~~ **Answered 2026-09-08** (§5): it did
  not die. A stored `userLicenses` lapses ~24 h after its own `t` stamp and then vetoes the
  `hash` re-mint; the 09-07 credential probed `member` with `hash` alone at ~31 h and
  `session_expired` with the token beside it, either order. Both "deaths" were ours.
- **What a genuinely EXPIRED `hash` does** — REOPENED 2026-09-08, having been marked answered on
  09-07. That experiment had a lapsed `userLicenses` in the same jar, and the lapsed token alone
  produces the whole signature it recorded (§5), so the `hash` in it was never shown to be dead.
  §10b's redirect remains the *malformed* case. Needs a `hash` allowed to reach its year — the
  calendar item above, not a spike.
- **What a wafer identity rotation does to a live member session.** §10c established that the
  jar loss seen in testing was CR clearing a rejected cookie, *not* rotation — so the rotation
  hazard itself is still un-observed. It stays mitigated by construction (`max_rotations=0`
  whenever a cookie is configured) and by asserting `hash` is in the jar before classifying.

**Needs more volume than is polite to generate**

- **WAF behaviour under sustained member traffic.** Roughly a dozen member requests have now been
  sent through wafer without incident (§10i); that is not "sustained".
- ~~Whether any category exceeds 200 products.~~ **Answered: yes** — Televisions 303, Mattresses
  293 (§12c). Both were invisible to earlier sampling because neither is in the A-Z index.

**Never attempted, and out of scope**

- **The paywall boundary in electronics-computers** — confirmed in the other five franchises
  (§9f); the derive-don't-hardcode rule (`SPEC.md` §10) makes this a documentation gap, not a
  correctness one.
- **The mobile app API and MPII** in `SPEC.md` §5 — never called. (Typeahead is measured, §14.)
- ~~Cars — out of scope.~~ **In scope and measured** (§11, §13).

**New, from the 2026-09-03 discovery work**

- ~~The true category count.~~ **Answered: 346** (§12a), from a complete 195-sitemap crawl.
- **How many sitemap ids are stale.** 1 of 10 sampled 404d; the other 109 missing ids were not
  individually fetched.
- **Whether any *car* endpoint offers batching.** Comma paths `400` and query filters are ignored
  (§13c); no batch form was found, but the API was not exhaustively probed.
- **Used-car coverage.** 305 model links from the used A-Z; `/sitemaps/cars.xml` (51 per-make
  sitemaps) is the cheaper route and was not walked.
- **Whether cars pages carry a `Don't Buy` analogue.** `isRecommended` is present; no
  safety-warning flag was looked for.
