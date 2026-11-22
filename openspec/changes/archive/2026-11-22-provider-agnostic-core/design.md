# Design: core plus integrations, and what "generic" means here

## The split

```
quotarouter/                    core: no Hermes, no MCP, no CLI framework
  types.py  errors.py  ledger.py  config.py  http_client.py  routing.py
  stages/                       one adapter per vendor
  cli.py                        argparse: status, probe, route, selftest, disable, enable,
                                reset, serve-mcp
  integrations/
    hermes.py                   WebSearchProvider subclass, one backend name
    mcp.py                      stdio JSON-RPC 2.0 server, three tools
```

The core exposes `Router.run_search(query, limit)` and `Router.run_extract(urls)`, both
returning a `RouteResult` with `ok`, `served_by`, `results`/`pages`, `attempts` and `error`.

The rule that keeps the split honest: **nothing under `quotarouter/` may import `hermes_cli`,
`agent` or `tools`**, and `quotarouter/integrations/__init__.py` imports its members lazily,
so `import quotarouter` stays clean. That rule is enforced by a test, not by a convention:
`test_core_imports_no_host_framework` walks the core modules' source for the forbidden names.

## Why MCP without an SDK

The protocol surface used here is four methods (`initialize`, `tools/list`, `tools/call`, and
the `notifications/initialized` notification). An SDK would add a dependency, a version-drift
surface, and an async runtime to a package whose only runtime dependency is `httpx`. One JSON
object per line, one writer, nothing else on stdout — that is the whole implementation, and it
is testable by piping a line into the process.

## Naming

`quotarouter` was chosen because it names the two things the package does (quota, routing) and
does not name a host. Ten candidate names checked against PyPI returned 404 on 2026-11-22,
`quotarouter` included; `quota-router`, `webquota`, `quotafetch`, `metered-web` and
`allowance-router` are free as well, and remain fallbacks if publishing ever collides.

## Migration of the environment prefix

`QUOTAROUTER_*` wins, `ROUTER_*` is read as a fallback (see `config._env`). That keeps cron
lines, systemd units and `.env` files working while the operator migrates in their own time —
a rename must not require a synchronised restart across hosts.

## What the rename changed in behaviour (and what it did not)

Unchanged: routing order, ledger format, error taxonomy, cost accounting, pause semantics,
keyless last resort.

Changed: provider name (`router` -> `quotarouter`), so `web.search_backend` must be updated;
the backend string in tool responses (`quotarouter:<stage>`); error text prefix
(`quotarouter: no search provider served this call (...)`); default state directory
(`~/.local/state/quotarouter`, previously under `~/.hermes/state/`).

## Evidence for the rename

- `python3 -m pytest -q` — 102 passed, including the MCP, config and packaging modules.
- `python3 -m pytest -m "live and not e2e" -q` — live adapters on real keys.
- `python3 tests/e2e/harness.py` — six steps in a real Hermes, with
  `provider: hermes_plugins.quotarouter...RouterProvider` and `backend: quotarouter:exa` in the
  report.
- `grep -rn "from router\|import router"` over the tree — no hits.
