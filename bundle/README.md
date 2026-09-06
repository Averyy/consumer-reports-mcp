# Claude Desktop bundle

The `.mcpb` that installs `consumer-reports-mcp` into Claude Desktop with one click, browser
sign-in included. Everything in this directory is a template; the artifact is built.

```bash
uv run scripts/build_bundle.py        # → dist/consumer-reports-mcp-<version>.mcpb
```

Then open the file (double-click, or Claude Desktop → Settings → Extensions → Install). Desktop
downloads its own `uv`, runs `uv sync` here, and starts `uv run --directory <bundle>
consumer-reports-mcp`. Nothing else is installed on the machine.

## What the build generates, and why

- **`manifest.json` here carries no `version` and no `tools`.** The build stamps the version from
  the root `pyproject.toml` and the tool list from `server.DESCRIPTIONS`, so neither can drift
  from the code. A template with a version in it is refused.
- **The server is a pinned PyPI dependency, and the pin is generated.** `pyproject.toml` depends
  on `consumer-reports-mcp[browser]==0`; the `==0` is a placeholder like `version = "0"`, and the
  build stamps the root version over both, so a bundle always installs the release it was built
  for. Desktop's `uv sync` resolves that pin from PyPI and the build has no network, so build
  from the released tag after the publish — built before it, the bundle packs fine and fails at
  the user's `uv sync`.
- **`[browser]` is on the dependency, not an extra of this project.** `uv sync` skips the bundle
  project's own extras but honours extras named in a dependency spec, so Desktop users get
  Playwright and the in-conversation sign-in works out of the box.

## Signing in

Ask Claude to connect your Consumer Reports membership. `cr_sign_in` opens your installed Chrome
(or Edge) on CR's own sign-in page in a throwaway window; you sign in there with "remember me"
ticked; the server validates the resulting cookie against one real request, stores it (`0600`),
and adopts it without a restart. Claude then polls `cr_auth_status` for the result.

The optional **Session cookie** field in the extension's settings is only for machines where no
browser window can open. Anything typed there is passed as `CR_SESSION_COOKIE`, which overrides
the stored session — so `cr_sign_in` refuses while it is set, and says where to clear it.

The cookie lasts 365 days from the sign-in and using it never extends that. Inside the last 30
days every response carries `session_expiring:<days>`; when it lapses, responses say
`session_expired` and name `cr_sign_in` as the fix.
