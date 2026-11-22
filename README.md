# quotarouter

Quota-aware routing for web search and page extraction.

Every vendor gives you a free monthly allowance. Spend that first, know what is left
**before** the call, and stop collecting `402 Insufficient Balance` in the logs. Hermes
cannot do this by itself: `web.search_backend` takes a single name, and its built-in keyless
rescue fires *after* the failure, again on every call.

`quotarouter` is a small provider-agnostic core plus thin integrations:

- `quotarouter` — the core package. No Hermes imports. `Router.run_search()` /
  `Router.run_extract()` pick a stage and account for it.
- `quotarouter.integrations.hermes` — a Hermes web-search provider. The model keeps calling
  `web_search` / `web_extract`; routing happens underneath.
- `quotarouter.integrations.mcp` — an MCP stdio server for Claude Desktop, Cursor, or any
  other MCP client (`web_search`, `web_extract`, `quota_status`).
- `quotarouter.cli` — status, quota probe, "who would serve the next call", live selftest.

The choice is made from **remaining quota in a persistent ledger**, not from an error that
already happened. A pause survives a restart and expires on the vendor's own reset date.

## Why not just install hermes-seek / hermes-sift

| Requirement | seek/sift | quotarouter |
|---|---|---|
| Provider chain | yes | yes |
| Pick by **remaining** quota before the call | no, reacts to 402 | yes, ledger + `probe` |
| Pause survives a restart | no, in memory | yes, JSON on disk |
| Return when the window resets | 24 h after 402 | on the vendor's `billingPeriodEnd` |
| Check that "success" is not empty | no | yes: EMPTY / BAD_PAGE / stub detection |
| Keyless vendor first on extract (saves credits) | — | yes, Jina Reader |
| Audit: why this stage | partly | `routes.jsonl` + `quotarouter route` |
| Usable outside Hermes | no | yes, core library + MCP server |

## Install: Hermes

The plugin is an ordinary Hermes plugin directory: `hermes_plugin/__init__.py` calls
`ctx.register_web_search_provider(RouterProvider())`. `RouterProvider` inherits
`agent.web_search_provider.WebSearchProvider` — that inheritance is mandatory, because the
registry refuses a provider without it and silently registers nothing.

```bash
# 1. connect the plugin: symlink the shim directory (it links to the package itself)
ln -s /path/to/quotarouter/hermes_plugin ~/.hermes/plugins/quotarouter

# 2. enable the plugin, point both backends at it
hermes plugins enable quotarouter
hermes config set web.search_backend quotarouter
hermes config set web.extract_backend quotarouter
hermes config set web.keyless_rescue false   # the keyless tier is our own last stage

# 3. restart: plugins are loaded at startup
systemctl --user restart hermes-gateway
```

Vendor keys are ordinary environment variables in `~/.hermes/.env`:
`EXA_API_KEY`, `FIRECRAWL_API_KEY`, `DEEPSEEK_API_KEY`, optionally `TAVILY_API_KEY`,
`KEENABLE_API_KEY`, `JINA_API_KEY`. A vendor without a key is skipped, never dialed.

## Install: any MCP client

```json
{
  "mcpServers": {
    "quotarouter": {
      "command": "python3",
      "args": ["-m", "quotarouter.cli", "serve-mcp"]
    }
  }
}
```

The server speaks line-delimited JSON-RPC 2.0 and needs no SDK — one request per line,
nothing else on stdout. Tools: `web_search {query, limit?}`, `web_extract {urls}`,
`quota_status {}`.

## Install: as a library

```python
from quotarouter import Router, format_failure

router = Router()
result = router.run_search("nous research hermes agent", limit=3)
if result.ok:
    for row in result.results:
        print(result.served_by, row["url"])
else:
    print(format_failure(result))
```

## Stage order

```
search : keenable -> exa -> tavily -> firecrawl -> deepseek
extract: jina -> keenable -> firecrawl -> tavily -> exa -> deepseek
```

Override with one environment variable — the order is data, not code:

```bash
QUOTAROUTER_SEARCH_ORDER=exa,firecrawl,deepseek
QUOTAROUTER_EXTRACT_ORDER=jina,firecrawl,exa
```

Why these defaults: Exa gives roughly 1 400 searches for $10; Firecrawl draws on one shared
pool of 1 000 credits (2 credits per search, 1 per page); Jina Reader is free and therefore
goes first on extract. Measurements are in `docs/vendor-probes.md`.

The legacy `ROUTER_*` variable names are still read, so a rename does not break an existing
deployment.

## Operations

```bash
python3 -m quotarouter.cli status           # what each vendor has left
python3 -m quotarouter.cli probe            # ask vendors for their balance, write the ledger
python3 -m quotarouter.cli route            # who serves the next call, and why the rest do not
python3 -m quotarouter.cli selftest         # one live search + one live extract
python3 -m quotarouter.cli disable exa --reason "key rotation"
python3 -m quotarouter.cli enable exa
python3 -m quotarouter.cli reset firecrawl  # clear local accounting after a plan change
```

Add `--json` to any command for machine-readable output.

Run `probe` from cron so the first search of the day does not learn about an empty pool from
a 402:

```cron
17 */6 * * * cd /path/to/quotarouter && . ~/.hermes/.env && /usr/bin/python3 -m quotarouter.cli probe --quiet
```

## Error policy

| What the vendor returned | Kind | What the router does |
|---|---|---|
| 402 / `Insufficient Balance` / `NO_MORE_CREDITS` | `quota` | park until the window resets (24 h if unknown) |
| 401 / 403 unrelated to quota | `auth` | park 24 h — a human must look |
| 429 | `throttle` | pause per `Retry-After`, else 60 s with doubling |
| 5xx, timeout, connection reset | `transient` | pause 60 s, try the next stage |
| 200 but empty | `empty` | next stage |
| Extract: every URL failed | `bad_page` | next stage |
| Extract: 403/404/410 from the page itself | — | **not** a stage failure: report the site's own answer |

That last row matters. `403 from origin` is the website answering, not the vendor failing.
Spending a second vendor's credits on a page that is closed to everyone is waste.

## Testing

Full ladder — `docs/testing.md`: what each level proves, what it costs, what counts as
evidence. In short:

```bash
python3 -m pytest -q                 # 102 tests, offline, no network — CI-ready
python3 -m pytest -m live -q         # real vendors, ~$0.01, spends quota
python3 -m pytest -m e2e -q          # end-to-end inside a real Hermes, ~$0.01
python3 tests/e2e/harness.py         # the same without pytest, writes e2e-report.json
```

Five levels:

1. `test_errors.py`, `test_ledger.py` — response classification and accounting, as tables.
2. `test_stages.py`, `test_routing.py` — adapters and the chain on `httpx.MockTransport`.
3. `test_adversarial.py` — recorded vendor responses (`tests/fixtures/`) and hostile cases:
   `success: true` with no rows, HTTP 200 carrying an error body, dropped connection, schema
   change.
4. `test_live.py`, `test_mcp_integration.py` — real vendors and a real MCP subprocess.
   Marked `live` / offline respectively.
5. `tests/e2e/` — the plugin inside a real Hermes: loader, provider registry, tool envelope,
   config reading, isolation from the live profile. Marked `e2e`.

Levels 4 and 5 do not run by default: `addopts = "-m 'not live and not e2e'"` in
`pyproject.toml`.

## State files

```
~/.local/state/quotarouter/ledger.json    quotas, pauses, billing periods
~/.local/state/quotarouter/routes.jsonl   one JSON line per call: who served it, who could not and why
```

`QUOTAROUTER_STATE_DIR` overrides the directory. Both files are written atomically
(`os.replace`) and survive a gateway restart.

## License

MIT — see `LICENSE`.
