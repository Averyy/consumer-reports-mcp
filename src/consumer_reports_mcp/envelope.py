"""Pydantic envelopes for every tool (PLAN P7.3, SPEC §7 *Response envelope*, D1/D2).

Typed models, not dicts: under `mcp==2.1.1` only a model return yields an `outputSchema` with
the real enums, which is the structural guard that makes the null-score contract checkable by
something other than the model's attention. Per-tool classes enforce SPEC §7 *Envelope fields by
tool* — an omitted key is not a field, never a null.

Shapes vary by `detail`: each shape is its own model, and `products` is a union, so pydantic
serialises each item by its own class and `ratings` never appears as `null` on a summary row.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Annotated, Any, Literal
from urllib.parse import quote

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_serializer

from .config import SEARCH_QUERY_MAX_CHARS
from .credentials import ENV_VAR

AuthState = Literal["anonymous", "member", "session_expired"]
Session = Literal["none", "unverified", "active", "expired"]
DataTier = Literal["anonymous", "member"]
Availability = Literal["available", "absent", "unavailable"]
CarAvailability = Literal["available", "absent"]
SignInStatus = Literal["waiting", "verifying", "in_progress", "refused", "failed"]
SignInPhase = Literal["idle", "verifying", "waiting", "validating", "active", "refused", "failed"]
CredentialSource = Literal["env", "file", "memory"]

# `auth_state` is derived from the SERVED row (`auth_state()` below), so on the three products
# envelopes that carry it the value is `null` exactly when no row was served — an error envelope
# whose `provenance` is null for the same reason. It is never filled in from the session alone:
# with no row there is no tier, and a guess of "anonymous" beside `session: "active"` is the
# tier of a row that does not exist, sitting next to a verified-live credential (SPEC §7).
AUTH_STATE_DESCRIPTION = (
    "Derived from the served row's tier and the session. null only on an error envelope where "
    "no row was served (provenance is null too); read `session` for the credential's health."
)

# The warnings vocabulary (SPEC §7 *Response envelope*, CLAUDE.md *Warnings vocabulary*) — the
# one enumeration that exists in code. `ToolError.code` is a closed Literal; `warnings[]` was a
# bare `list[str]` with every token spelled at its emission site, so a typo, a stray parameter
# name or a token nobody documented all shipped as "machine-readable". Every envelope's
# `warnings` is validated against this registry AT CONSTRUCTION: an unregistered token is a
# bug in this process and fails the tool call loudly, the way an unlisted `code` already does,
# rather than reaching an agent as a word it has never been told about. Registering a token
# here is the whole procedure for adding one; `tests/test_envelope.py` pins the registry to the
# documented vocabulary in both directions.
WARNING_TOKENS = frozenset(
    {
        "sitemap_pass_pending",  # the sitemap source is not yet authoritative (discovery)
        "empty_category",  # CR published no products in the category
        "attribute_dictionary_missing",  # `categoryAttributes` absent; units/descriptions lost
        "refresh_skipped",  # `refresh=true` inside the cooldown, answered from a young row
        "availability_not_cached",  # no reliability row on hand to merge availability from
        "availability_stale",  # merged from a reliability row past its TTL
        "typeahead_unavailable",  # CR's typeahead failed; only the local index was searched
    }
)
WARNING_PREFIXES = frozenset(
    {
        "no_results",  # `no_results:<k=v,…>` — the caller's filters excluded everything
        "ungrouped_products",  # `ungrouped_products:<n>` — products CR ships with no group
        "coercion_failed",  # `coercion_failed:<attributeId>` (cars: `:<field>`)
        "refresh_failed",  # `refresh_failed:<code>` — a cached row answered for a failed fetch
        "survey_flag_mismatch",  # `survey_flag_mismatch:<survey>` — CR's flag vs its rows
        "ratings_unavailable",  # `ratings_unavailable:<modelYearId>:<reason>` (cars)
        "index_refresh_failed",  # `index_refresh_failed:<reason>` (cars index)
        "session_expiring",  # `session_expiring:<days>` — the cookie's upper bound is near
    }
)


def is_known_warning(token: str) -> bool:
    """A bare token from `WARNING_TOKENS`, or `<prefix>:<non-empty detail>` with a prefix from
    `WARNING_PREFIXES`. A prefixed token with nothing after the colon is not a warning."""
    if not isinstance(token, str):
        return False
    head, sep, detail = token.partition(":")
    if not sep:
        return head in WARNING_TOKENS
    return head in WARNING_PREFIXES and bool(detail)


def _check_warnings(values: list[str]) -> list[str]:
    unknown = [w for w in values if not is_known_warning(w)]
    if unknown:
        raise ValueError(f"unregistered warning token(s) {unknown!r}; see envelope.WARNING_TOKENS")
    return values


# Every envelope's `warnings` field: `list[str]` in the outputSchema, registry-checked in code.
Warnings = Annotated[list[str], AfterValidator(_check_warnings)]

NOTICE_TEMPLATE = (
    "overall_score and ratings are null for every product in this response because auth_state "
    "is {auth_state}. This is a session limitation, not an absence of CR ratings."
)
# SPEC §7: `session_expired` names the path it can be fixed on — the in-conversation tool first,
# then the CLI, then the env var for the one deployment where neither writes anything useful.
EXPIRED_FIX_TEMPLATE = (
    " The stored session cookie has expired or was rejected by Consumer Reports: call "
    "`cr_sign_in` to connect the membership again (or run `consumer-reports-mcp auth`; if the "
    "cookie came from the {env_var} environment variable, update it there)."
)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- shared blocks


class ScoresAvailable(Strict):
    """Derived per SPEC §10 — never hardcoded. Three states, never booleans."""

    overall_score: Availability
    attribute_ratings: Availability
    owner_satisfaction: Availability
    predicted_reliability: Availability
    recommended_flag: Availability


class SurveyScoresAvailable(Strict):
    """`cr_reliability`: survey keys only, `absent` never `unavailable` (SPEC §7)."""

    predicted_reliability: Literal["available", "absent"]
    owner_satisfaction: Literal["available", "absent"]


class Provenance(Strict):
    data_tier: DataTier
    fetched_at: str | None
    cr_url: str | None
    from_cache: bool
    stale: bool = Field(description="past CR_CACHE_TTL_DAYS — and nothing else")
    superseded_at: str | None = Field(
        description="a scored row was retained over a newer unscored fetch made at this time"
    )


class ReliabilityProvenance(Strict):
    fetched_at: str | None
    cr_url: str | None
    from_cache: bool
    stale: bool


class IndexProvenance(Strict):
    """`cr_categories` / `cr_search`: the INDEX fetch, not any category payload."""

    fetched_at: str | None
    cr_url: str | None
    from_cache: bool


class CarProvenance(Strict):
    fetched_at: str | None
    cr_url: str | None
    from_cache: bool
    stale: bool


class SortInfo(Strict):
    key: Literal["overallScore", "price"]
    order: Literal["asc", "desc"]
    scope: Literal["within_group", "cross_group"]


class ToolError(Strict):
    code: Literal[
        "unknown_category",
        "ambiguous_category",
        "unknown_filter",
        "invalid_filter_value",
        "ambiguous_filter_name",
        "unknown_product",
        "unknown_car",
        "session_expired",
        "fetch_failed",
        "challenged",
        "payload_missing",
        "marker_missing",
        "reliability_payload_missing",
        "credential_rejected",
        "filter_on_unavailable_attribute",
    ]
    message: str
    reason: str | None = None
    retryable: bool | None = None
    http_status: int | None = None
    title: str | None = None
    candidates: list[dict[str, Any]] | None = None
    filter: str | None = None
    attribute: str | None = None


# --------------------------------------------------------------------------- product shapes


class Rating(Strict):
    id: int
    name: str | None
    value: float | None = Field(description="null when not visible in this session, never absent")


class Attribute(Strict):
    """A normalized attribute (SPEC §7). Serialised leanly: `raw_value` only when it differs
    from `value` (a coercion failure or a coerced string), and `unit`/`description`/`group`
    only when CR ships one — `value: null` itself is always present."""

    id: int | None
    name: str | None
    kind: str | None
    value: Any
    raw_value: Any
    unit: str | None
    description: str | None
    group: str | None

    @model_serializer(mode="wrap")
    def _lean(self, handler):
        data = handler(self)
        if data.get("raw_value") == data.get("value"):
            data.pop("raw_value", None)
        for key in ("unit", "description", "group"):
            if data.get(key) is None:
                data.pop(key, None)
        return data


class ProductSummary(Strict):
    id: int
    brand: str | None
    model: str | None
    group: str | None = Field(description="_groupName: the unit within which scores compare")
    rank: int | None = Field(description="position in CR's unfiltered group table; within-group")
    price: float | None
    overall_score: float | None = Field(description="CR's Overall Score, or null; never estimated")
    recommended: bool | None
    dont_buy: bool | None = Field(description="CR's safety/defect warning — in every shape")
    smart_buy: bool | None
    projected_attributes: list[Attribute] | None = None

    @model_serializer(mode="wrap")
    def _lean(self, handler):
        data = handler(self)
        if data.get("projected_attributes") is None:  # only when `attributes=` was asked for
            data.pop("projected_attributes", None)
        return data


class ProductStandard(ProductSummary):
    ratings: list[Rating]
    owner_satisfaction: float | None
    predicted_reliability: float | None


class ProductFull(ProductStandard):
    retailers: int | None
    retailer_prices: list[float]
    attributes: list[Attribute]


Product = ProductSummary | ProductStandard | ProductFull


class CategoryRef(Strict):
    id: int
    slug: str | None
    name: str | None


class GroupBlock(Strict):
    group: str | None = Field(
        description="CR's display group; null only on the trailing block of products CR ships "
        "without a group (see ungrouped_products in warnings)"
    )
    group_id: int | None
    size: int = Field(description="products in CR's group, unfiltered")
    total: int = Field(description="products matching this call")
    truncated: bool
    products: list[Product]


class NestedRatings(Strict):
    category: CategoryRef
    detail: Literal["summary", "standard", "full"]
    group_mode: Literal["nested"]
    groups: list[GroupBlock]
    notice: str | None


class FlatRatings(Strict):
    category: CategoryRef
    detail: Literal["summary", "standard", "full"]
    group_mode: Literal["flat"]
    products: list[Product]
    size: int
    total: int
    truncated: bool
    notice: str | None


class RatingsEnvelope(Strict):
    auth_state: AuthState | None = Field(description=AUTH_STATE_DESCRIPTION)
    session: Session
    scores_available: ScoresAvailable | None
    provenance: Provenance | None
    sort: SortInfo | None
    warnings: Warnings = Field(
        description=(
            "Machine-readable tokens. `no_results:<k=v,...>` means the filters in THIS request "
            "excluded every product - widen or drop one; `data.size` is the unfiltered count. "
            "That is distinct from `empty_category`, which means Consumer Reports published no "
            "products in the category at all. Values are percent-encoded."
        )
    )
    error: ToolError | None
    data: NestedRatings | FlatRatings | None


class ProductData(Strict):
    category: CategoryRef
    product: ProductFull
    availability: str | None = Field(
        description="modelAvailabilityName, merged only when a reliability row is cached"
    )
    notice: str | None


class ProductEnvelope(Strict):
    auth_state: AuthState | None = Field(description=AUTH_STATE_DESCRIPTION)
    session: Session
    scores_available: ScoresAvailable | None
    provenance: Provenance | None
    warnings: Warnings
    error: ToolError | None
    data: ProductData | None


# --------------------------------------------------------------------------- cr_filters


class FilterOption(Strict):
    id: int | str | None
    label: str | None


class NumericRange(Strict):
    min: float | None
    max: float | None


class FeatureFilter(Strict):
    id: int
    name: str | None
    display_name: str | None
    kind: str | None
    unit: str | None
    description: str | None
    group: str | None
    range: NumericRange | None = Field(description="numeric attributes: the observed bounds")
    values: list[Any] | None = Field(description="non-numeric attributes: distinct values, ≤ 12")
    values_truncated: bool

    @model_serializer(mode="wrap")
    def _lean(self, handler):
        data = handler(self)
        if data.get("display_name") == data.get("name"):
            data.pop("display_name", None)
        for key in ("range", "values", "unit", "description", "group"):
            if data.get(key) is None:
                data.pop(key, None)
        if data.get("values_truncated") is False:
            data.pop("values_truncated", None)
        return data


class FilterBlock(Strict):
    id: str
    label: str | None
    type: str | None
    parameter: Literal["brands", "group", "price_min/price_max", "features", "recommended"] | None
    options: list[FilterOption] | None
    range: NumericRange | None
    features: list[FeatureFilter] | None
    values_truncated: bool


class FiltersData(Strict):
    category: CategoryRef
    filters: list[FilterBlock]
    groups: list[FilterOption]
    standard_ratings: list[FilterOption] = Field(
        description="the three rating attributes detail=standard returns, by sortOrder"
    )


class FiltersEnvelope(Strict):
    auth_state: AuthState | None = Field(description=AUTH_STATE_DESCRIPTION)
    session: Session
    scores_available: ScoresAvailable | None
    provenance: Provenance | None
    warnings: Warnings
    error: ToolError | None
    data: FiltersData | None


# --------------------------------------------------------------------------- cr_reliability


class BrandSurvey(Strict):
    brand_id: int
    brand_name: str | None
    predicted_reliability: float | None
    owner_satisfaction: float | None


class BrandSurveyFull(BrandSurvey):
    """`detail="full"` adds the /100 scale — omitted, not null, at `standard`."""

    predicted_reliability_100: float | None
    owner_satisfaction_100: float | None


class Methodology(Strict):
    reliability: str | None
    owner_satisfaction: str | None
    footnote: str | None = Field(description="CR's source line, e.g. which surveys")


class ReliabilityData(Strict):
    category: CategoryRef
    brands: list[BrandSurvey | BrandSurveyFull]
    methodology: Methodology | None
    has_reliability_data: bool
    has_owner_satisfaction_data: bool


class ReliabilityEnvelope(Strict):
    auth_state: Literal["anonymous"]
    scores_available: SurveyScoresAvailable | None
    provenance: ReliabilityProvenance | None
    warnings: Warnings
    error: ToolError | None
    data: ReliabilityData | None


# ------------------------------------------------------------------ cr_categories, cr_search


class CategoryLean(Strict):
    id: str
    slug: str | None
    name: str | None
    franchise: str | None
    score_range_status: Literal["known", "none_published", "not_fetched"]


class FamilyRef(Strict):
    id: int
    name: str | None


class GroupRef(Strict):
    id: int
    name: str


class CategoryEnriched(CategoryLean):
    family: FamilyRef | None
    score_range: NumericRange | None
    rated_count: int | None
    groups: list[GroupRef] | None


class CategoriesData(Strict):
    categories: list[CategoryLean | CategoryEnriched]
    total: int
    scoped: bool
    discovery: dict[str, Any]


class CategoriesEnvelope(Strict):
    session: Session
    provenance: IndexProvenance | None
    warnings: Warnings
    error: ToolError | None
    data: CategoriesData | None


class SearchCategoryHit(Strict):
    kind: Literal["category"]
    id: str
    slug: str | None
    name: str | None
    franchise: str | None
    source: str


class SearchProductHit(Strict):
    kind: Literal["product"]
    id: int
    brand: str | None
    model: str | None
    category_id: str
    category_name: str | None
    data_tier: DataTier | None
    fetched_at: str | None


class SearchData(Strict):
    query: str
    categories: list[SearchCategoryHit]
    products: list[SearchProductHit]
    searched_categories: list[CategoryRef]


class SearchEnvelope(Strict):
    session: Session
    provenance: IndexProvenance | None
    warnings: Warnings
    error: ToolError | None
    data: SearchData | None


# --------------------------------------------------------------------------- cars


class CarScoresAvailable(Strict):
    """Cars: `available`/`absent` only — the API never withholds (SPEC §5).

    A key is `null` when this response did not look: `cr_cars(detail="summary")` fetches no
    per-car ratings, so it reports the road-test keys as null rather than `absent`, which would
    claim CR published no value — and a page with ZERO rows reports every key null, because
    `any()` over nothing is a claim about nothing. Null is "not reported here", never a data
    absence."""

    overall_score: CarAvailability | None = None
    road_test_score: CarAvailability | None = None
    predicted_reliability: CarAvailability | None = None
    owner_satisfaction: CarAvailability | None = None
    recommended_flag: CarAvailability | None = None
    safety_verdict: CarAvailability | None = None
    popular_score: CarAvailability | None = None
    fuel_economy: CarAvailability | None = None


class CarSearchHit(Strict):
    model_year_id: int
    make: str | None
    model: str | None
    year: int | None
    states: list[str]
    car_types: list[dict[str, Any]]


class CarSearchData(Strict):
    query: str
    cars: list[CarSearchHit]


class CarSearchEnvelope(Strict):
    session: Session
    provenance: CarProvenance | None
    warnings: Warnings
    error: ToolError | None
    data: CarSearchData | None


class CarsData(Strict):
    cars: list[dict[str, Any]]
    detail: Literal["summary", "standard"]
    # the model-year population; null only when CR sent no `counts` and pages remain —
    # `truncated` is then true, and the fetched prefix is never reported as the population
    total: int | None
    truncated: bool
    requests_made: int


class CarsEnvelope(Strict):
    session: Session
    scores_available: CarScoresAvailable | None
    provenance: CarProvenance | None
    warnings: Warnings = Field(
        description=(
            "Machine-readable tokens. `no_results:<k=v,...>` means the filters in THIS request "
            "matched no model-year - widen or drop one. Keys are this tool's own parameter "
            "names. Values are percent-encoded."
        )
    )
    error: ToolError | None
    data: CarsData | None


class CarEnvelope(Strict):
    session: Session
    scores_available: CarScoresAvailable | None
    provenance: CarProvenance | None
    warnings: Warnings
    error: ToolError | None
    data: dict[str, Any] | None


# --------------------------------------------------------------------------- auth tools


class SignInData(Strict):
    """The status object `cr_sign_in` answers with. It never carries a cookie value — nothing in
    this module can, by construction."""

    status: SignInStatus = Field(
        description="waiting: a window opened, poll cr_auth_status; verifying: a stored session "
        "is being checked first and a window opens only if CR rejects it, poll cr_auth_status; "
        "in_progress: one was already running; refused: nothing started (see reason); failed: "
        "it started and could not finish"
    )
    reason: str | None = Field(
        description="machine-readable cause for refused/failed — session_active, env_override, "
        "browser_extra_missing, browser_not_found, window_closed, capture_timeout, offline, "
        "session_expired, credential_rejected, could_not_check:<reason>, save_failed:<type>, "
        "internal_error:<type>"
    )
    instructions: str = Field(description="what to do next — the only free-text field")
    browser: str | None = Field(description="which installed browser opened, once known")
    expires_in_s: int | None = Field(description="seconds until the open window gives up")


class SignInEnvelope(Strict):
    """`cr_sign_in` (SPEC §6 *cr_sign_in*, §7): the project's outer shape around a status
    object. A refusal or failure is `data.status` with a machine-readable `data.reason`, not an
    error-taxonomy entry, so `error` is always null; the key exists because every tool has it."""

    session: Session
    warnings: Warnings
    error: ToolError | None
    data: SignInData


class AuthStatusData(Strict):
    """Facts about the credential and any sign-in in flight. Values never."""

    source: CredentialSource | None = Field(
        description="where the credential came from: env (read-only, wins over file), file, "
        "memory (a validation in progress), or null when anonymous"
    )
    captured_at: str | None
    days_left_max: float | None = Field(
        description="365 minus the stored cookie's age: an UPPER bound on its remaining life, "
        "never a promise; null unless the cookie came from the stored file"
    )
    sign_in: SignInPhase = Field(
        description="idle: nothing in flight; verifying: the stored session is being checked "
        "before any window opens; waiting: a window is open; validating: a token was captured "
        "and is being checked; active: the last sign-in succeeded; refused: the stored session "
        "is live, nothing changed (see reason); failed: see reason"
    )
    reason: str | None
    browser: str | None


class AuthStatusEnvelope(Strict):
    """`cr_auth_status` (SPEC §7): the same outer shape. `error` is always null."""

    session: Session
    warnings: Warnings
    error: ToolError | None
    data: AuthStatusData


# --------------------------------------------------------------------------- derivations


# `no_results:<k=v,…>` — ONE renderer for both surfaces (SPEC §7). Values are caller input,
# so they are percent-encoded, deduped and bounded. Unencoded they were not machine-readable:
# a documented `[min, max]` feature range renders `[3, 5]`, whose `, ` splits the pair list,
# and `brands=["a|b"]` collided with `brands=["a","b"]`. Unbounded they were a hole in this
# project's size discipline: one `features={...: "A"*200_000}` call returned a 200 kB warning.
NO_RESULTS_MAX_ITEMS = 8  # values rendered from a list/dict before the truncation mark
NO_RESULTS_MAX_VALUE = 60  # characters per rendered value
NO_RESULTS_MAX_TOTAL = 300  # characters of the whole warning
TRUNCATED = "\u2026"

# The same discipline for the OTHER place caller text lands in a response: an `error.message`
# that quotes the token the error is about, and the `candidates`/`attribute` fields beside it
# (SPEC \u00a77 *Error taxonomy*). Every such site used `!r` unbounded, so a 200,000-character
# `category` came back as a 200,000-character message \u2014 the `no_results` hole, never closed
# here. `repr()` IS the right base (it escapes newlines, ANSI escapes and every other
# non-printable, and its quoting is unambiguous: a value holding `'` is rendered in `"`, one
# holding both escapes the quote), so a value can never read as the end of the quoted token
# and the start of more prose; what `repr` does not do is bound the length. `quoted()` clips
# the VALUE before quoting, so no escape sequence is cut in half, and bounds the rendering
# too (escapes expand: `\x1b` is four characters); a cut is marked OUTSIDE the quotes, with
# the true length \u2014 `'AAAA'\u2026 (200,000 chars)` \u2014 so a caller can never mistake the
# clipped form for the value the server saw, and a value that itself ends in `\u2026` is not
# mistaken for a cut one. 60 characters is `no_results`' per-value bound: the longest CR name,
# slug or brand measured is under 40, so a legitimate token is always shown whole and a junk
# one is recognisable from its head. Lists are capped at 12 \u2014 `cr_filters`' value-list cap,
# and enough for every legal set measured (7 car types, \u2264 6 display groups, \u2264 ~10 car
# categories under one type) \u2014 with the count of what was left out.
QUOTE_MAX_CHARS = NO_RESULTS_MAX_VALUE
QUOTE_MAX_RENDERED = 2 * QUOTE_MAX_CHARS + 2  # the repr of a clipped value, escapes included
QUOTE_MAX_ITEMS = 12


def clipped(text: str, limit: int = QUOTE_MAX_CHARS) -> str:
    """`text` cut to `limit` characters, marked with `\u2026` when it was \u2014 the form for a
    STRUCTURED field (`candidates`, `attribute`, a `no_results` value before encoding), where
    JSON escaping already covers control characters and only the length needs bounding."""
    return text if len(text) <= limit else text[:limit] + TRUNCATED


def _repr(value: Any) -> str:
    """`repr`, except that Python refuses to render an int past 4,300 digits
    (`sys.get_int_max_str_digits`) — a `ValueError` out of an error message, for a caller
    who passed one. Such a value is described, never rendered."""
    try:
        return repr(value)
    except ValueError:
        if isinstance(value, int):
            return f"<an integer of about {int(value.bit_length() * 0.30103) + 1:,} digits>"
        return f"<a {type(value).__name__} holding an integer past the digit limit>"


def _head(text: str) -> str:
    head = text[:QUOTE_MAX_CHARS]
    while len(repr(head)) > QUOTE_MAX_RENDERED:  # every character escaped: bound the rendering
        head = head[:-1]
    return head


def quoted(value: Any) -> str:
    """A caller's or CR's value as it appears INSIDE a message: repr-quoted, escaped, and
    bounded \u2014 `'tvs'`, `'AAAA'\u2026 (200,000 chars)`, `12`, `['a', 'b']`. The one way a value
    reaches `error.message` (SPEC \u00a77); `tests/test_boundaries.py` scans for any other."""
    if isinstance(value, str):
        head = _head(value)
        text = repr(head)
        if len(head) < len(value):
            text += f"{TRUNCATED} ({len(value):,} chars)"
        return text
    text = _repr(value)  # an int's digits, a list's items: unbounded too
    if len(text) > QUOTE_MAX_RENDERED:
        return text[:QUOTE_MAX_CHARS] + f"{TRUNCATED} ({len(text):,} chars)"
    return text


def quoted_list(values: Iterable[Any], render: Callable[[Any], str] = quoted) -> str:
    """Values rendered through `render` (default `quoted`), joined with `, ` and capped at
    `QUOTE_MAX_ITEMS` \u2014 `'a', 'b'\u2026 (3 more)`. Identical renderings are shown once (two
    clipped 5,000-character tokens look the same), but the count of what was left out is
    over the DISTINCT inputs, so 200 such tokens read as three and `(197 more)`, never as
    three. A renderer that composes a label MUST still pass the text through `quoted`:
    `lambda r: f"{quoted(r['name'])} ({r['id']})"`."""
    items = list(values)
    rendered = list(dict.fromkeys(render(v) for v in items))
    try:
        distinct = len(set(items))
    except TypeError:  # unhashable rows (dicts): distinct by rendering
        distinct = len(rendered)
    shown = rendered[:QUOTE_MAX_ITEMS]
    text = ", ".join(shown)
    if distinct > len(shown):
        text += f"{TRUNCATED} ({distinct - len(shown):,} more)"
    return text


def candidates(items: Iterable[Any]) -> list[Any] | None:
    """`error.candidates`, bounded like the message beside it: at most `QUOTE_MAX_ITEMS`
    entries, every string value in them `clipped`. None when there is nothing \u2014 an empty
    list would claim "no legal values", which is never what a site means (SPEC \u00a77)."""
    out: list[Any] = []
    for item in items:
        if len(out) >= QUOTE_MAX_ITEMS:
            break
        if isinstance(item, Mapping):
            out.append({k: clipped(v) if isinstance(v, str) else v for k, v in item.items()})
        else:
            out.append(clipped(item) if isinstance(item, str) else item)
    return out or None


def _render_value(v: Any) -> str:
    """One value as canonical text: booleans lowercase (never Python's `True`), an integral
    float without its `.0` — the MCP schema types prices as `float`, so a client's `500`
    arrives as `500.0` and would otherwise render differently than the same call in-process."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return v if isinstance(v, str) else _repr(v)  # `str(10**5000)` raises (see `_repr`)


def _encode(text: str) -> str:
    """Percent-encode, so no value can carry the grammar's own delimiters: `,` `=` `:` `|` and
    newlines all escape, which keeps pairs splittable and stops a caller forging a second
    `k=v` (or a second vocabulary token) inside a value."""
    return quote(clipped(text, NO_RESULTS_MAX_VALUE), safe="")


def _cap(rendered: list[str]) -> str:
    """Dedup and bound an already-encoded list of values."""
    seen = list(dict.fromkeys(rendered))
    out = seen[:NO_RESULTS_MAX_ITEMS]
    if len(seen) > NO_RESULTS_MAX_ITEMS:
        out.append(_encode(TRUNCATED))
    return "|".join(out)


def no_results(params: Mapping[str, Any]) -> str:
    """The warning naming the filters that excluded everything. Keys are the TOOL's own
    parameter names on both surfaces — an agent that never typed `modelYearStateId` cannot act
    on being told about it.

    Refuses an EMPTY parameter set. With nothing to name the token would be the bare
    `no_results:`, which the registry rejects — so an envelope built from it fails at
    construction, turning "zero rows" into a tool crash. But "zero rows with no filter" is
    not what this warning means (SPEC §7: the caller's filters excluded everything), so the
    decision of what an unfiltered emptiness says belongs to the emission site, which gates on
    its active filters (`FilterSpec.active`; `cr_cars`' filter dict). Raising here, with the
    rule in the message, is where a developer sees a site that forgot to."""
    if not params:
        raise ValueError(
            "no_results needs at least one filter to name: an unfiltered empty result is not "
            "'the caller's filters excluded everything' — gate the emission on the active filters"
        )
    parts: list[str] = []
    for key in sorted(params):
        value = params[key]
        if isinstance(value, Mapping):
            rendered = _cap(
                [
                    f"{_encode(str(k))}:{_encode(_render_value(v))}"
                    for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))
                ]
            )
        elif isinstance(value, list | tuple | set):
            rendered = _cap([_encode(_render_value(v)) for v in value])
        else:
            rendered = _encode(_render_value(value))
        parts.append(f"{key}={rendered}")
    token = "no_results:" + ",".join(parts)
    return token if len(token) <= NO_RESULTS_MAX_TOTAL else token[:NO_RESULTS_MAX_TOTAL] + TRUNCATED


def bad_query(q: str) -> ToolError | None:
    """`cr_search`/`cr_car_search`'s `query`, already stripped: the error for an empty or an
    over-long one, else None. `data.query` echoes the value and the typeahead URL carries it,
    so past `SEARCH_QUERY_MAX_CHARS` it is refused before either — never clipped, since a
    search over a prefix reported as the whole would misstate what was searched — and never
    quoted, since its length is the whole complaint."""
    if not q:
        return ToolError(
            code="invalid_filter_value", message="query must not be empty", filter="query"
        )
    if len(q) > SEARCH_QUERY_MAX_CHARS:
        return ToolError(
            code="invalid_filter_value",
            message=f"query is {len(q):,} characters; the limit is {SEARCH_QUERY_MAX_CHARS}",
            filter="query",
        )
    return None


def auth_state(data_tier: str, session: str) -> str:
    """SPEC §7: member if the served row is member; session_expired if anonymous data and the
    credential is dead; anonymous otherwise. Called only with a SERVED row's tier — with no row
    the envelope carries `auth_state: null` (`AUTH_STATE_DESCRIPTION`), never a guess."""
    if data_tier == "member":
        return "member"
    if data_tier == "anonymous" and session == "expired":
        return "session_expired"
    return "anonymous"


def maybe_notice(
    scores: dict[str, str] | ScoresAvailable | None, state: str, *, has_products: bool = True
) -> str | None:
    """Fires only when `overall_score == "unavailable"` — never on nullness alone (SPEC §7) —
    and only when there are products for it to be about: an empty category has nothing null,
    and `empty_category` in `warnings` already carries that fact."""
    if scores is None or not has_products:
        return None
    value = scores["overall_score"] if isinstance(scores, dict) else scores.overall_score
    if value != "unavailable":
        return None
    text = NOTICE_TEMPLATE.format(auth_state=state)
    if state == "session_expired":
        text += EXPIRED_FIX_TEMPLATE.format(env_var=ENV_VAR)
    return text


def error_model(err: Any) -> ToolError:
    """From `repository.ToolError` / `query.QueryError` to the envelope model."""
    if isinstance(err, ToolError):
        return err
    if hasattr(err, "as_dict"):
        return ToolError(**err.as_dict())
    extra = dict(getattr(err, "extra", {}) or {})
    candidates = extra.pop("candidates", None)
    if candidates is not None and candidates and not isinstance(candidates[0], dict):
        candidates = [{"id": c} for c in candidates]
    return ToolError(
        code=err.code,
        message=err.message,
        reason=extra.pop("reason", None),
        candidates=candidates,
        filter=extra.pop("filter", None),
        attribute=extra.pop("attribute", None),
    )
