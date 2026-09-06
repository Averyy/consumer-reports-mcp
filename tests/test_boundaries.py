"""Static checks over the source tree, enforced mechanically with `ast`. Five families:

**P0.2 — the four module boundaries**
1. wafer is imported only by transport.py
2. sqlite3 is imported only by cache.py
3. mcp is imported only by server.py
4. session.json / CR_SESSION_COOKIE literals appear only in credentials.py

**`auth_state` derivation (SPEC §7)** — every `E.*Envelope(auth_state=...)` in the tool
modules is `E.auth_state(...)` over a served row, or `None`; never a literal or a helper. And
`provenance=` beside it is null exactly when `auth_state=` is: both describe the served row.

**Base warnings (SPEC §7)** — every `E.*Envelope(...)` a tool module builds, error envelopes
included, passes its module's base-warnings call inline in `warnings=`.

**Failure projection (SPEC §7)** — outside transport.py, a name bound to a caught
`FetchFailed`/`Challenged` is read only for a decision (`.reason`); the fields that reach
`error` come from `transport.failure_fields` alone.

**Quoted text (SPEC §7)** — no f-string in a tool-facing module uses `!r`, and every value
interpolated into an error message is an `envelope.quoted`/`quoted_list` call or an
expression listed with its reason in `BARE_MESSAGE_EXPRS`.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "consumer_reports_mcp"


def _modules(root: Path | None = None) -> dict[str, ast.Module]:
    root = root or SRC
    out = {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        out[rel] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return out


def _imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            # dynamic imports count too: importlib.import_module("x") / __import__("x")
            fn = node.func
            dynamic = (isinstance(fn, ast.Name) and fn.id == "__import__") or (
                isinstance(fn, ast.Attribute) and fn.attr == "import_module"
            )
            if dynamic and node.args and isinstance(node.args[0], ast.Constant):
                if isinstance(node.args[0].value, str):
                    names.add(node.args[0].value.split(".")[0])
    return names


def _string_literals(tree: ast.Module) -> list[str]:
    out = [
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    for n in ast.walk(tree):  # f-strings: join their constant parts so "session" + ".json" is seen
        if isinstance(n, ast.JoinedStr):
            out.append("".join(v.value for v in n.values if isinstance(v, ast.Constant)))
    return out


def _files_importing(pkg: str, root: Path | None = None) -> set[str]:
    return {rel for rel, tree in _modules(root).items() if pkg in _imports(tree)}


def test_wafer_only_in_transport():
    assert _files_importing("wafer") <= {"transport.py"}


def test_sqlite3_only_in_cache():
    assert _files_importing("sqlite3") <= {"cache.py"}


def test_mcp_only_in_server():
    assert _files_importing("mcp") <= {"server.py"}


def test_credential_literals_only_in_credentials():
    offenders = set()
    for rel, tree in _modules().items():
        for lit in _string_literals(tree):
            if "session.json" in lit or "CR_SESSION_COOKIE" in lit:
                offenders.add(rel)
    assert offenders <= {"credentials.py"}, offenders


def test_no_forbidden_http_clients():
    forbidden = {"requests", "httpx", "urllib3", "aiohttp"}
    for rel, tree in _modules().items():
        assert not (_imports(tree) & forbidden), rel
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("urllib.request"), rel
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("urllib.request") for a in node.names), rel


def test_boundary_check_detects_a_violation(tmp_path):
    """Sanity: `import wafer` in a cache.py must be reported by the walker."""
    fake_src = tmp_path / "consumer_reports_mcp"
    fake_src.mkdir()
    (fake_src / "cache.py").write_text("import wafer\n")
    (fake_src / "transport.py").write_text("from wafer import AsyncSession\n")
    (fake_src / "sneaky.py").write_text("import importlib\nw = importlib.import_module('wafer')\n")
    (fake_src / "sneakier.py").write_text("w = __import__('wafer')\n")
    assert _files_importing("wafer", fake_src) == {
        "cache.py",
        "transport.py",
        "sneaky.py",
        "sneakier.py",
    }


# --------------------------------------------------------------------------- auth_state


def _is_envelope_ctor(fn: ast.expr) -> bool:
    return (
        isinstance(fn, ast.Attribute)
        and fn.attr.endswith("Envelope")
        and isinstance(fn.value, ast.Name)
        and fn.value.id == "E"
    )


def _kw_sources(func: ast.AST, node: ast.Call, arg: str) -> list[str] | None:
    """The source of `arg=` on one call — None when the keyword is absent — where a bare
    name is resolved to every assignment to it in the enclosing function, so `state` counts
    as whatever `state = ...` was."""
    kw = next((k for k in node.keywords if k.arg == arg), None)
    if kw is None:
        return None
    exprs = [kw.value]
    if isinstance(kw.value, ast.Name):
        exprs = [
            a.value
            for a in ast.walk(func)
            if isinstance(a, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == kw.value.id for t in a.targets)
        ]
    return [ast.unparse(e) for e in exprs]


def _auth_state_sources(tree: ast.Module) -> list[tuple[int, str, list[str]]]:
    """Every `E.<Something>Envelope(auth_state=...)`: (line, class, sources)."""
    return [(line, cls, auth) for line, cls, auth, _ in _row_field_sources(tree)]


def _row_field_sources(tree: ast.Module) -> list[tuple[int, str, list[str], list[str] | None]]:
    """Every `E.<Something>Envelope(auth_state=...)`: (line, class, auth_state sources,
    provenance sources) — the second None when no `provenance=` keyword was passed."""
    out = []
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    for func in funcs:
        for node in ast.walk(func):
            if not (isinstance(node, ast.Call) and _is_envelope_ctor(node.func)):
                continue
            auth = _kw_sources(func, node, "auth_state")
            if auth is None:
                continue
            out.append((node.lineno, node.func.attr, auth, _kw_sources(func, node, "provenance")))
    return out


def _derived_or_null(src: str) -> bool:
    """`E.auth_state(...)`, `None`, or the conditional `E.auth_state(...) if <row> else None`."""
    return src == "None" or (src.startswith("E.auth_state(") and src.endswith((")", "else None")))


def test_auth_state_is_derived_from_a_served_row_or_null():
    """SPEC §7: `auth_state` is `E.auth_state(data_tier, session)` over a SERVED row, `null`
    when there is none, and the pinned literal on `cr_reliability`; cars pass no such keyword.
    No tool module may derive it any other way. A helper that chose `anonymous` or
    `session_expired` from the session alone once answered `auth_state: "anonymous"` beside
    `session: "active"` on every no-row error — the tier of a row that did not exist, and a
    second derivation re-encoded per surface, which is how surfaces diverge."""
    allowed = {
        "tools_products.py": _derived_or_null,
        "reliability.py": lambda src: src == "'anonymous'",
    }
    for rel, tree in _modules().items():
        calls = _auth_state_sources(tree)
        if rel not in allowed:
            assert not calls, f"{rel} passes auth_state= (only products envelopes carry it)"
            continue
        assert calls, rel
        for line, cls, sources in calls:
            assert sources, f"{rel}:{line} {cls}(auth_state=<unbound name>)"
            for src in sources:
                assert allowed[rel](src), f"{rel}:{line} {cls}(auth_state={src})"


def _null_together(auth: str, prov: str) -> bool:
    """`auth_state=` and `provenance=` sources agree on whether a row was served: both `None`,
    both unconditional (a derived tier beside a built provenance), or both conditional on the
    same fact (`... if served is not None else None`)."""
    conditional = auth.endswith("else None"), prov.endswith("else None")
    if auth == "None" or prov == "None":
        return auth == prov
    return conditional[0] == conditional[1]


def test_auth_state_and_provenance_are_null_together_at_every_site():
    """SPEC §7: the two fields describe ONE served row, so a site that passes a derived
    `auth_state=` passes the row's provenance beside it, and a site that passes `None` passes
    `provenance=None`. `_ratings_error` once took the served tier and hard-coded
    `provenance=None`, so every `sort`/`order`/`group` error after the fetch answered
    `auth_state: "member"` with no source, age or cache state — member data from nowhere.
    `E.RowEnvelope` refuses that at runtime; this pins the sites so the refusal is never the
    first thing a caller sees."""
    offenders = []
    checked = 0
    for rel, tree in _modules().items():
        if rel == "reliability.py":  # the pinned literal describes no row (SPEC §7)
            continue
        for line, cls, auth, prov in _row_field_sources(tree):
            checked += 1
            if prov is None:
                offenders.append(f"{rel}:{line} {cls}(auth_state=…) without provenance=")
                continue
            for a in auth:
                for p in prov:
                    if not _null_together(a, p):
                        offenders.append(f"{rel}:{line} {cls}(auth_state={a}, provenance={p})")
    assert checked >= 6, checked  # the sites exist; a moved module must not blind this
    assert not offenders, offenders


def test_null_together_check_detects_a_split(tmp_path):
    """Sanity: a derived tier beside `provenance=None`, a null tier beside a built provenance,
    a conditional tier beside an unconditional `None`, and three correct pairings."""
    fake = tmp_path / "tools_products.py"
    fake.write_text(
        "def a(served):\n"
        "    return E.RatingsEnvelope(auth_state=E.auth_state(served.data_tier, served.session), "
        "provenance=None)\n"
        "def b(served):\n"
        "    return E.RatingsEnvelope(auth_state=None, provenance=_provenance(served))\n"
        "def c(served):\n"
        "    return E.RatingsEnvelope(auth_state=E.auth_state(t, s) if served else None, "
        "provenance=None)\n"
        "def d(served):\n"
        "    state = E.auth_state(served.data_tier, served.session)\n"
        "    return E.RatingsEnvelope(auth_state=state, provenance=_provenance(served))\n"
        "def e(rt):\n"
        "    return E.FiltersEnvelope(auth_state=None, provenance=None)\n"
        "def f(served):\n"
        "    return E.RatingsEnvelope(auth_state=E.auth_state(t, s) if served is not None else "
        "None, provenance=_provenance(served) if served is not None else None)\n"
    )
    verdicts = {
        line: all(_null_together(a, p) for a in auth for p in prov)
        for line, _, auth, prov in _row_field_sources(ast.parse(fake.read_text()))
    }
    assert verdicts == {2: False, 4: False, 6: False, 9: True, 11: True, 13: True}


def test_auth_state_check_detects_a_guess(tmp_path):
    """Sanity: a helper call, a literal, and a name bound to a literal are all reported."""
    fake = tmp_path / "tools_products.py"
    fake.write_text(
        "async def a(rt):\n"
        "    return E.RatingsEnvelope(auth_state=_error_state(rt), session='none')\n"
        "async def b(rt):\n"
        "    state = 'anonymous'\n"
        "    return E.FiltersEnvelope(auth_state=state)\n"
        "async def c(rt, served):\n"
        "    state = E.auth_state(served.data_tier, served.session)\n"
        "    return E.ProductEnvelope(auth_state=state)\n"
        "async def d(rt, tier):\n"
        "    state = E.auth_state(tier, 'none') if tier else None\n"
        "    return E.ProductEnvelope(auth_state=state)\n"
    )
    tree = ast.parse(fake.read_text())
    verdicts = {
        line: all(_derived_or_null(s) for s in srcs) for line, _, srcs in _auth_state_sources(tree)
    }
    assert verdicts == {2: False, 5: False, 8: True, 11: True}


# --------------------------------------------------------------------------- base warnings


def _envelope_calls(tree: ast.Module) -> list[tuple[int, str, str | None]]:
    """Every `E.<Something>Envelope(...)` construction: (line, class, warnings= source)."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (
            isinstance(fn, ast.Attribute)
            and fn.attr.endswith("Envelope")
            and isinstance(fn.value, ast.Name)
            and fn.value.id == "E"
        ):
            continue
        kw = next((k for k in node.keywords if k.arg == "warnings"), None)
        out.append((node.lineno, fn.attr, ast.unparse(kw.value) if kw else None))
    return out


def test_every_envelope_a_tool_builds_carries_its_base_warnings():
    """SPEC §7: `session_expiring:<days>` rides on EVERY tool that carries `session`, and the
    products tools also carry the discovery state — on error envelopes too, since "the cookie
    is about to expire" is as true on an `unknown_product` as on a hit. Each tool module has one
    function for that, and every envelope it constructs must pass it: `cr_product`'s
    `unknown_product` and `cr_search`'s empty-query envelopes once dropped it, and no test
    noticed because none asked those two paths about the session.

    The convention this enforces: the base-warnings call appears INLINE in the `warnings=`
    argument of every envelope construction (`warnings=_base_warnings(rt) + extra`), and the
    check is a source-text match on that argument — `ws = _base_warnings(rt) + extra` bound to
    a name and passed as `warnings=ws` is correct code this test would reject, so write it
    inline."""
    required = {
        "tools_products.py": "_base_warnings(rt)",
        "cars/tools.py": "rt.session_warnings()",
        "auth_tools.py": "rt.session_warnings()",
        "reliability.py": "rt.discovery.warnings()",  # no `session` on this tool (SPEC §7)
    }
    offenders = []
    for rel, needle in required.items():
        calls = _envelope_calls(_modules()[rel])
        assert calls, rel
        for line, cls, src in calls:
            if src is None or needle not in src:
                offenders.append(f"{rel}:{line} {cls}(warnings={src})")
    assert not offenders, offenders


# --------------------------------------------------------------------------- failure projection

FAILURE_TYPES = {"FetchFailed", "Challenged"}
# the attributes a catch site may read to DECIDE something (`exc.reason == "no_such_route"`);
# every other attribute reaches an error envelope only through `transport.failure_fields`
DECISION_ATTRS = {"reason"}


def _names_failure_type(node: ast.expr | None) -> bool:
    return node is not None and any(
        isinstance(n, ast.Name) and n.id in FAILURE_TYPES for n in ast.walk(node)
    )


def _hand_projections(tree: ast.Module) -> tuple[int, list[tuple[int, str]]]:
    """(scopes scanned, offenders). A scope is a name bound to a transport failure — by an
    `except FetchFailed/Challenged as name` or a parameter annotated with either — and an
    offender is a `name.<attr>` read inside it with <attr> outside `DECISION_ATTRS`."""
    scopes: list[tuple[str, list[ast.stmt]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.name and _names_failure_type(node.type):
            scopes.append((node.name, node.body))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            for arg in args.posonlyargs + args.args + args.kwonlyargs:
                if _names_failure_type(arg.annotation):
                    scopes.append((arg.arg, node.body))
    offenders = []
    for name, body in scopes:
        for stmt in body:
            for n in ast.walk(stmt):
                if (
                    isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Name)
                    and n.value.id == name
                    and n.attr not in DECISION_ATTRS
                ):
                    offenders.append((n.lineno, f"{name}.{n.attr}"))
    return len(scopes), sorted(offenders)


def test_transport_failures_are_projected_by_one_function():
    """SPEC §7 *Error taxonomy*: `reason`, `retryable` and `http_status` reach an error
    envelope through `transport.failure_fields` and nothing else. Outside transport.py, a name
    bound to a `FetchFailed`/`Challenged` may be read only for a decision (`.reason`); reading
    `.http_status`, `.challenge_type`, `.status`, `.retryable` — or an attribute that does not
    exist yet — is a second copy of the projection, which is exactly the site that gets left
    behind when the exception grows a field (that shipped once: `http_status: null` on every
    cars `400`/`503`). `test_transport.py` pins the other half — that the one function reads
    every attribute the exceptions store."""
    scanned = 0
    offenders = []
    for rel, tree in _modules().items():
        if rel == "transport.py":  # owns the types and the projection
            continue
        n, found = _hand_projections(tree)
        scanned += n
        offenders += [f"{rel}:{line} {what}" for line, what in found]
    assert scanned >= 10, scanned  # the catch sites exist; a renamed class must not blind this
    assert not offenders, offenders


def test_projection_check_detects_a_hand_spelled_site(tmp_path):
    """Sanity: a dict literal in a handler, a `ToolError(...)` in an annotated helper, and a
    handler that unpacks the projection (reading only `.reason` by hand)."""
    fake = tmp_path / "tools.py"
    fake.write_text(
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except FetchFailed as exc:\n"
        "        return {'reason': exc.reason, 'http_status': exc.http_status}\n"
        "def g(exc: FetchFailed | Challenged):\n"
        "    return E.ToolError(code='challenged', retryable=False, http_status=exc.status)\n"
        "def h():\n"
        "    try:\n"
        "        pass\n"
        "    except (FetchFailed, Challenged) as exc:\n"
        "        return failure_fields(exc), exc.reason == 'no_such_route', str(exc)\n"
    )
    scanned, offenders = _hand_projections(ast.parse(fake.read_text()))
    assert scanned == 3
    assert offenders == [(5, "exc.http_status"), (7, "exc.status")]


# --------------------------------------------------------------------------- quoted text

# The modules whose f-strings can reach a response: every tool module, the two repositories,
# the query layer and discovery. `envelope.py` owns the helper; the cache, transport and
# normalisation layers raise ValueErrors for programming errors and build no messages.
MESSAGE_MODULES = {
    "repository.py",
    "query.py",
    "tools_products.py",
    "reliability.py",
    "discovery.py",
    "auth_tools.py",
    "cars/repository.py",
    "cars/tools.py",
}
ERROR_CONSTRUCTORS = {"ToolError", "QueryError", "CarsQueryError"}
QUOTING_CALLS = {"quoted", "quoted_list"}
# Expressions that may appear bare in a message: ints the code produced or validated, module
# constants, and lists ALREADY rendered through `quoted_list` (the reason a name is here is
# written beside it). Anything else — a parameter, a CR field — goes through `quoted`, or is
# added here deliberately.
BARE_MESSAGE_EXPRS = {
    "cap",  # config limit caps
    "limit",  # only after the int check, in the "exceeds the cap" branch
    "cid",  # a resolved category id
    "pid",  # a `row_id`-validated product id
    "product_id",  # same, in the repository
    "cat",  # a cars category id after `as_int`
    "value",  # `validate_year`'s int
    "year",  # `year_rejected`: the int `validate_year` put in `params`
    "span[0]",
    "span[1]",  # `car_year_range()` ints
    "owner['id']",
    "owner['group_id']",  # ints from `group_owner`
    "res.category_id",
    "served.category_id",
    "sel.category_id",  # resolved ids
    "DETAILS",
    "SORT_KEYS",  # constants
    "ids",
    "legal",
    "hint",  # pre-rendered: `quoted_list` results, or a literal-only fragment
    "group_name",
    "owner_name",  # pre-rendered through `quoted`
}


def _error_message_exprs(node: ast.Call) -> ast.expr | None:
    """The `message` expression of an error construction, positional or keyword."""
    kw = next((k for k in node.keywords if k.arg == "message"), None)
    if kw is not None:
        return kw.value
    return node.args[1] if len(node.args) > 1 else None


def _unquoted_interpolations(tree: ast.Module) -> tuple[int, list[tuple[int, str]]]:
    """(f-strings scanned, offenders). Two rules over one module: no f-string anywhere uses
    `!r` (the idiom `quoted` replaces); and inside an error construction's message, every
    interpolated expression is a `quoted`/`quoted_list` call or is in `BARE_MESSAGE_EXPRS`."""
    offenders: list[tuple[int, str]] = []
    scanned = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            scanned += 1
            for part in node.values:
                if isinstance(part, ast.FormattedValue) and part.conversion != -1:
                    what = f"!{chr(part.conversion)} on {ast.unparse(part.value)}"
                    offenders.append((part.lineno, what))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
        if name not in ERROR_CONSTRUCTORS:
            continue
        message = _error_message_exprs(node)
        if message is None:
            continue
        for js in ast.walk(message):
            if not isinstance(js, ast.JoinedStr):
                continue
            for part in js.values:
                if not isinstance(part, ast.FormattedValue):
                    continue
                expr = part.value
                fn2 = expr.func if isinstance(expr, ast.Call) else None
                called = fn2.attr if isinstance(fn2, ast.Attribute) else getattr(fn2, "id", None)
                if called in QUOTING_CALLS:
                    continue
                text = ast.unparse(expr)
                if text not in BARE_MESSAGE_EXPRS:
                    offenders.append((part.lineno, text))
    return scanned, sorted(set(offenders))


def test_caller_and_cr_text_reach_a_message_only_through_quoted():
    """SPEC §7 *Error taxonomy*: a value quoted into `error.message` goes through
    `envelope.quoted` (or `quoted_list`), which escapes and BOUNDS it. `!r` was the idiom at a
    dozen sites, and a 200,000-character `category` came back as a 200,000-character message —
    the `no_results` hole, never closed here. So no f-string in a tool-facing module may use
    `!r`, and every interpolation in an error message is a quoting call or an expression
    listed in `BARE_MESSAGE_EXPRS` with the reason it needs no quoting."""
    scanned = 0
    offenders = []
    for rel in sorted(MESSAGE_MODULES):
        n, found = _unquoted_interpolations(_modules()[rel])
        scanned += n
        offenders += [f"{rel}:{line} {what}" for line, what in found]
    assert scanned >= 40, scanned  # the f-strings exist; a moved module must not blind this
    assert not offenders, offenders


def test_quoting_check_detects_an_unquoted_site(tmp_path):
    """Sanity: `!r` anywhere, a bare parameter in a message, and a correct site."""
    fake = tmp_path / "tools.py"
    fake.write_text(
        "def f(token, cap):\n"
        "    log.info(f'{token!r}')\n"
        "    return ToolError('unknown_category', f'{token} is not a category; cap {cap}')\n"
        "def g(token):\n"
        "    raise QueryError('unknown_filter', message=f'{E.quoted(token)} is unknown')\n"
    )
    scanned, offenders = _unquoted_interpolations(ast.parse(fake.read_text()))
    assert scanned == 3
    assert offenders == [(2, "!r on token"), (3, "token")]
