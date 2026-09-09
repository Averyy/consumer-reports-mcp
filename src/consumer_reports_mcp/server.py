"""BOUNDARY 3: the only module that imports mcp (PLAN P9.2, P10.5).

Eleven tools over stdio, each with its SPEC §7 description and a typed `outputSchema` — the
structural guard that lets a client see `auth_state` and `scores_available` as enums rather than
prose. Tool bodies live in `tools_products.py`, `reliability.py`, `cars/tools.py` and
`auth_tools.py`; this module only wraps them in closures over the process `Runtime`.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import __version__
from . import envelope as E
from .config import Settings
from .runtime import Runtime, build_runtime, configure_logging

log = logging.getLogger(__name__)

# SPEC §7 "Tool descriptions are part of the contract" — the table text, verbatim. The only
# transformation is dropping the table's `**` markdown emphasis: MCP descriptions are plain
# text and literal asterisks would be noise; tests/test_server.py re-reads SPEC.md to check.
DESCRIPTIONS: dict[str, str] = {
    "cr_ratings": (
        "Consumer Reports ratings for a product category, ranked within CR's own display "
        'groups. A `null` score means "not visible in this session", never "CR did not rate '
        'this model" — check `auth_state`, or `status` on the attribute itself, which reads '
        "`not_applicable` where CR does not run that test on that model. Scores are never "
        "estimated."
    ),
    "cr_product": (
        "Full Consumer Reports record for one product: every scored attribute, specs, owner "
        "satisfaction, retailer prices. Same `null` rule as `cr_ratings`."
    ),
    "cr_filters": (
        "What is filterable in a category and the legal values, including attribute "
        "descriptions and units. Call this before guessing filter names."
    ),
    "cr_reliability": (
        "Consumer Reports brand-level predicted reliability and owner satisfaction for a "
        "category. Brand-level, not model-level — these are survey results per brand, not a "
        "rating of any single product. Available without a membership."
    ),
    "cr_categories": (
        "The 346 Consumer Reports product categories with ids and slugs. Cars have their own tools."
    ),
    "cr_search": (
        "Find which category covers a product, or a model by name/number among already-cached "
        "categories. Category hits are ranked and carry `match`: a `partial` first hit is the "
        'best of weak fits, not the answer. An empty result means "not in what has been '
        'fetched", never '
        '"CR does not rate it" — the response names what was searched.'
    ),
    "cr_car_search": (
        "Find a Consumer Reports car by make, model and year. Returns identifiers, not ratings "
        "— pass one to `cr_car`."
    ),
    "cr_cars": (
        "List Consumer Reports car model-years by type, make or new/used. Returns safety "
        "verdict, popular score, fuel economy and incentives in one request; pass "
        '`detail="standard"` to add road-test scores, which cost one request per car.'
    ),
    "cr_car": (
        "Full Consumer Reports record for one car model-year: Overall Score, road-test score, "
        "per-test ratings, predicted reliability, owner satisfaction, crash tests and specs."
    ),
    "cr_sign_in": (
        "Connect the user's Consumer Reports membership: opens a browser window on their screen "
        "at CR's own sign-in page, where they type their password into CR's form; the server "
        "keeps only the resulting session cookie and never sees the password. Call this ONLY "
        "when the user explicitly asks to connect or renew their membership — never to fill in "
        "null scores on your own. Returns within seconds; poll `cr_auth_status` for the outcome."
    ),
    "cr_auth_status": (
        "Whether a Consumer Reports member session is configured and healthy, how many days the "
        "stored cookie has left at most, and the state of any sign-in in progress. Never returns "
        "the cookie. Pass `wait_s` (up to 45) to wait for a running sign-in to finish."
    ),
}

ANNOTATIONS = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
# `cr_sign_in` is deliberately NOT read-only and NOT idempotent: it opens a window on the user's
# screen and writes the session file, and both clients prompt for a tool declared that way. It
# must never share the read-only annotations object above (SPEC §6 *cr_sign_in*).
SIGN_IN_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False, idempotent_hint=False, open_world_hint=True
)
AUTH_STATUS_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True, idempotent_hint=True, open_world_hint=False
)
PRODUCT_TOOLS = (
    "cr_ratings",
    "cr_product",
    "cr_filters",
    "cr_reliability",
    "cr_categories",
    "cr_search",
)
CARS_TOOLS = ("cr_car_search", "cr_cars", "cr_car")
AUTH_TOOLS = ("cr_sign_in", "cr_auth_status")


class RuntimeHolder:
    """The process Runtime, built by the lifespan (or injected by tests)."""

    def __init__(self, runtime: Runtime | None = None) -> None:
        self.runtime = runtime

    @property
    def rt(self) -> Runtime:
        if self.runtime is None:
            raise RuntimeError("runtime not initialised — the server lifespan has not run")
        return self.runtime


def build_server(runtime: Runtime | None = None, *, settings: Settings | None = None) -> MCPServer:
    holder = RuntimeHolder(runtime)

    @contextlib.asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[dict[str, Any]]:
        if holder.runtime is None:
            configure_logging()
            holder.runtime = build_runtime(settings or Settings(env=os.environ), env=os.environ)
        rt = holder.runtime
        try:
            # retention first (SPEC §8): rows older than 3×TTL go, never the newest scored row
            # per category — the one call site, so the cache has a bound at all
            deleted = rt.cache.prune(rt.clock(), rt.settings.cache_ttl_days)
            if deleted:
                log.info("pruned %d cache rows past 3×TTL", deleted)
        except Exception as exc:  # a retention failure must never keep the server from serving
            log.warning("cache prune failed: %s", type(exc).__name__)
        try:
            # the A-Z index first and synchronously (one request), the sitemap pass behind it
            await rt.discovery.ensure_az_index()
            rt.discovery.start_background_sitemap_pass()
        except Exception as exc:  # startup discovery is best-effort; tools retry on demand
            log.warning("startup discovery failed: %s", type(exc).__name__)
        try:
            yield {"runtime": rt}
        finally:
            await rt.discovery.cancel()
            if rt.sign_in is not None:
                await rt.sign_in.cancel()

    server = MCPServer(
        name="consumer-reports",
        version=__version__,
        instructions=(
            "Consumer Reports ratings as structured tools. Products: cr_categories → cr_filters "
            "→ cr_ratings → cr_product; cr_reliability for brand surveys; cr_search to find a "
            "category. Cars: cr_car_search → cr_car, or cr_cars to list. A null score is a "
            "session limitation, never an absence of a rating — read auth_state and "
            "scores_available, which are typed enums in every response. The exception is "
            "status: 'not_applicable' on an attribute: CR does not run that test on that model, "
            "so it is neither rated nor gated. When the user asks to "
            "connect their membership, call cr_sign_in and then cr_auth_status(wait_s=45); "
            "never call cr_sign_in unasked."
        ),
        lifespan=lifespan,
    )
    register_products_tools(server, holder)
    register_cars_tools(server, holder)
    register_auth_tools(server, holder)
    return server


def _tool(server: MCPServer, name: str, annotations: ToolAnnotations = ANNOTATIONS):
    return server.tool(
        name=name, description=DESCRIPTIONS[name], annotations=annotations, structured_output=True
    )


def register_products_tools(server: MCPServer, holder: RuntimeHolder) -> None:
    from . import tools_products as T
    from .reliability import cr_reliability as _cr_reliability

    @_tool(server, "cr_ratings")
    async def cr_ratings(
        category: str | int,
        group: str | int | None = None,
        brands: list[str | int] | None = None,
        price_min: float | None = None,
        price_max: float | None = None,
        recommended: bool | None = None,
        features: dict[str, Any] | None = None,
        sort: Literal["overallScore", "price"] | None = None,
        order: Literal["asc", "desc"] = "desc",
        group_mode: Literal["nested", "flat"] = "nested",
        attributes: list[str | int] | None = None,
        limit: int | None = None,
        offset: int = 0,
        detail: Literal["summary", "standard", "full"] = "standard",
        refresh: bool = False,
    ) -> E.RatingsEnvelope:
        return await T.cr_ratings(
            holder.rt,
            category,
            group=group,
            brands=brands,
            price_min=price_min,
            price_max=price_max,
            recommended=recommended,
            features=features,
            sort=sort,
            order=order,
            group_mode=group_mode,
            attributes=attributes,
            limit=limit,
            offset=offset,
            detail=detail,
            refresh=refresh,
        )

    @_tool(server, "cr_product")
    async def cr_product(
        id: int, include_descriptions: bool = False, refresh: bool = False
    ) -> E.ProductEnvelope:
        return await T.cr_product(
            holder.rt, id, include_descriptions=include_descriptions, refresh=refresh
        )

    @_tool(server, "cr_filters")
    async def cr_filters(category: str | int, refresh: bool = False) -> E.FiltersEnvelope:
        return await T.cr_filters(holder.rt, category, refresh=refresh)

    @_tool(server, "cr_reliability")
    async def cr_reliability(
        category: str | int,
        detail: Literal["standard", "full"] = "standard",
        include_methodology: bool = False,
        refresh: bool = False,
    ) -> E.ReliabilityEnvelope:
        return await _cr_reliability(
            holder.rt,
            category,
            detail=detail,
            include_methodology=include_methodology,
            refresh=refresh,
        )

    @_tool(server, "cr_categories")
    async def cr_categories(
        franchise: str | None = None, family: str | int | None = None, refresh: bool = False
    ) -> E.CategoriesEnvelope:
        return await T.cr_categories(holder.rt, franchise=franchise, family=family, refresh=refresh)

    @_tool(server, "cr_search")
    async def cr_search(query: str, refresh: bool = False) -> E.SearchEnvelope:
        return await T.cr_search(holder.rt, query, refresh=refresh)


def register_cars_tools(server: MCPServer, holder: RuntimeHolder) -> None:
    from .cars import tools as C

    @_tool(server, "cr_car_search")
    async def cr_car_search(query: str, refresh: bool = False) -> E.CarSearchEnvelope:
        return await C.cr_car_search(holder.rt, query, refresh=refresh)

    @_tool(server, "cr_cars")
    async def cr_cars(
        car_type: str | None = None,
        category: int | None = None,
        make: str | None = None,
        year: int | None = None,
        state: Literal["new", "used"] | None = None,
        detail: Literal["summary", "standard"] = "summary",
        limit: int | None = None,
        offset: int = 0,
        refresh: bool = False,
    ) -> E.CarsEnvelope:
        return await C.cr_cars(
            holder.rt,
            car_type=car_type,
            category=category,
            make=make,
            year=year,
            state=state,
            detail=detail,
            limit=limit,
            offset=offset,
            refresh=refresh,
        )

    @_tool(server, "cr_car")
    async def cr_car(
        model_year_id: int, detail: Literal["standard", "full"] = "standard", refresh: bool = False
    ) -> E.CarEnvelope:
        return await C.cr_car(holder.rt, model_year_id, detail=detail, refresh=refresh)


def register_auth_tools(server: MCPServer, holder: RuntimeHolder) -> None:
    from . import auth_tools as A

    @_tool(server, "cr_sign_in", SIGN_IN_ANNOTATIONS)
    async def cr_sign_in(force: bool = False) -> E.SignInEnvelope:
        return await A.cr_sign_in(holder.rt, force=force)

    @_tool(server, "cr_auth_status", AUTH_STATUS_ANNOTATIONS)
    async def cr_auth_status(wait_s: int = 0) -> E.AuthStatusEnvelope:
        return await A.cr_auth_status(holder.rt, wait_s=wait_s)


def main() -> int:
    """`consumer-reports-mcp` with no arguments: serve MCP over stdio."""
    import sys

    from .config import ConfigError

    configure_logging()
    try:
        settings = Settings(env=os.environ)
    except ConfigError as exc:
        print(f"consumer-reports-mcp: {exc}", file=sys.stderr)
        return 2
    build_server(settings=settings).run("stdio")
    return 0
