# Provider-agnostic core, second host, publication readiness

## Why

The router was built as a Hermes plugin: `router/` imported Hermes' provider base, the
provider name was `router`, the docs were Russian, and the package could not be used outside
this one installation. Three costs of that shape are already real:

- another host (Claude Desktop, Cursor, a plain script) had to re-implement the chain;
- the name `router` says nothing about quota, and it collided with the generic word everywhere
  in logs and config;
- a repository that cannot be read by anyone outside the household is not publishable, and the
  quota logic is the part worth publishing.

## What Changes

- The package is renamed `router` -> `quotarouter` and split into a host-agnostic core and
  integrations. `import quotarouter` imports no Hermes code.
- `quotarouter/integrations/hermes.py` — the Hermes provider (what `router/provider.py` was).
- `quotarouter/integrations/mcp.py` — a new MCP stdio server (line-delimited JSON-RPC 2.0,
  no SDK) exposing `web_search`, `web_extract`, `quota_status` to any MCP client.
- `python3 -m quotarouter.cli serve-mcp` starts that server, so a client config needs no
  wrapper script.
- Environment prefix `ROUTER_` -> `QUOTAROUTER_`; the old names are still read, so a running
  deployment keeps working through the rename.
- Provider name in Hermes becomes `quotarouter`; the plugin manifest and the shim follow.
- Every artifact is translated to English; `pyproject.toml` gains publication metadata
  (name, readme, classifiers, urls, console script) and the MIT license stands.
- A new test module `tests/test_mcp_integration.py` runs the MCP server in a real subprocess.

## Out of scope

- Publishing to PyPI, and renaming the GitHub repository (operator actions; both need an
  account-level decision).
- Installing the plugin into the live Hermes profile and switching the live backends — that is
  a separate operator action with a gateway restart.
- Vendors beyond the ones already implemented; Keeenable and Tavily stay keyless stubs that
  switch on with a key.

## Impact

- Every module path changes: `router.*` -> `quotarouter.*`; `tests/` and the shim follow.
- The rename is backwards compatible on the environment side only; Python import paths break
  for any external consumer, which is acceptable because there is none yet.
- `pyproject.toml` name changes from `hermes-web-router` to `quotarouter`.
