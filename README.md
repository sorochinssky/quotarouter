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
- `quotarouter.integrations.mcp` — an MCP stdio server for Claude Desktop, Cursor, Codex,
  opencode or any other MCP client (`web_search`, `web_extract`, `quota_status`, `quota_probe`).
- `quotarouter.cli` — status, quota probe, "who would serve the next call", live selftest.

The choice is made from **remaining quota in a persistent ledger**, not from an error that
already happened. A pause survives a restart and expires on the vendor's own reset date.

## How it fits together

```
  your code, an MCP client, or Hermes
               |
               v
   Router.run_search() / run_extract()        quotarouter/routing.py
               |
     1. read the ledger   ~/.local/state/quotarouter/ledger.json
     2. ask free vendors  exa -> firecrawl -> deepseek -> tavily -> keenable
        for their real balance first (probe, costs nothing)
     3. skip what is spent or parked, walk the rest in order
     4. account for what was spent, log the chain to routes.jsonl
               |
               v
     one answer, or one honest error naming every stage
```

The ledger is the only shared state, and it is a plain JSON file written atomically, so a CLI
run, an MCP server and a Hermes gateway on the same machine agree about what is left. Everything
else is a stage: a small adapter with a key, a unit cost, an optional balance probe, and one
decision — did this response actually deliver a page?

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

## Install

```bash
pip install .          # from a checkout — the package is not on PyPI yet
# once it is:  pip install quotarouter
```

Then pick how you want to use it. All three read the same keys, from the same places.

```bash
cp .env.example .env           # fill in the vendors you actually have; never commit it
```

Keys are looked up in this order: `os.environ`, then `QUOTAROUTER_ENV_FILE`, then `./.env`, then
`~/.config/quotarouter/.env`. Inside Hermes the adapter adds the profile's own `.env` as one more
source. A vendor without a key is skipped, never dialed; with no keys at all the router still
answers through the anonymous tier and says so on stderr.

### As a library

```python
from quotarouter import Router, format_failure

router = Router()
result = router.run_search("model context protocol", limit=3)
if not result.ok:
    raise SystemExit(format_failure(result, "search"))

print(result.stage)                                    # who served it: exa, firecrawl, ...
for row in (result.payload.get("data") or {}).get("web") or []:
    print(row["url"])
```

A complete runnable version, including extract and the per-page error shape, is
[`examples/library.py`](examples/library.py):

```bash
python examples/library.py "model context protocol" https://example.com/
```

### As an MCP server (any MCP client)

The server speaks line-delimited JSON-RPC 2.0 over stdio and needs no SDK — one request per
line, nothing else on stdout. Tools: `web_search {query, limit?}`, `web_extract {urls}`,
`quota_status {}` (what the ledger believes) and `quota_probe {}` (ask the vendors for their real
balance and store it — no credits spent).

Claude Desktop, Cursor and anything else that reads `mcpServers`:

```json
{
  "mcpServers": {
    "quotarouter": {
      "command": "quotarouter",
      "args": ["serve-mcp"],
      "env": {"QUOTAROUTER_ENV_FILE": "/home/me/.config/quotarouter/.env"}
    }
  }
}
```

`opencode` uses an array form in `~/.config/opencode/opencode.jsonc`:

```json
{
  "mcp": {
    "quotarouter": {
      "type": "local",
      "enabled": true,
      "command": ["quotarouter", "serve-mcp"],
      "environment": {"QUOTAROUTER_ENV_FILE": "/home/me/.config/quotarouter/.env"}
    }
  }
}
```

Substitute `["python3", "-m", "quotarouter", "serve-mcp"]` if the console script is not on the
client's `PATH` (a GUI app often has a shorter one).

Verified against a real third-party agent, not only in unit tests: `opencode` reports
`quotarouter connected` in `mcp list`, calls `quota_status` and `web_search`, and gets real URLs
back. The same four tools were exercised through the reference `mcp` SDK client: `initialize` →
protocol `2025-11-25`, `tools/list`, `tools/call`, and a bad argument returning `isError` without
killing the server.

### As a Hermes plugin

The plugin is an ordinary Hermes plugin directory: `hermes_plugin/__init__.py` calls
`ctx.register_web_search_provider(RouterProvider())`. `RouterProvider` inherits
`agent.web_search_provider.WebSearchProvider` — that inheritance is mandatory, because the
registry refuses a provider without it and silently registers nothing.

```bash
# 1. connect the plugin: a real directory, not a symlink to hermes_plugin/. A symlink that
#    leaves the plugins directory is refused by `hermes plugins validate` (symlink_escape), and
#    the plugin then loads nothing.
cp -r /path/to/quotarouter/hermes_plugin ~/.hermes/plugins/quotarouter

# 2. enable the plugin, point both backends at it
hermes plugins enable quotarouter
hermes config set web.search_backend quotarouter
hermes config set web.extract_backend quotarouter
hermes config set web.keyless_rescue false   # the keyless tier is our own last stage
```

Plugins are read when the gateway starts. `hermes plugins disable` followed by `enable` reloads
them in a running process, which is enough after a code change; a fresh install wants the
service itself restarted, with whatever command your install uses for the `hermes-gateway` unit.

The shim imports `quotarouter` from your Python environment, so `pip install .` (or
`pip install -e .`) has to come first. Vendor keys are ordinary variables in `~/.hermes/.env`.

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

One name is a valid chain (`QUOTAROUTER_SEARCH_ORDER=exa` means "exa or nothing"), a stage that
cannot do the capability is skipped with that reason, and a misspelled name is reported at
startup instead of silently shortening the chain. The order is per process, not per call: the
router owns the choice so the ledger stays correct across the CLI, the MCP server and Hermes.

Why these defaults: Exa gives roughly 1 400 searches for $10; Firecrawl draws on one shared
pool of 1 000 credits (2 credits per search, 1 per page); Jina Reader is free and therefore
goes first on extract. Measurements are in `docs/vendor-probes.md`.

The legacy `ROUTER_*` variable names are still read, so a rename does not break an existing
deployment.

## Choosing a chain per call

The default order is a property of the process. A single call can also name its own chain — down to
one vendor — and say what it may spend, without changing anything for the next call.

```python
router.run_search("q", 5, stages=["firecrawl"])              # this vendor or an honest failure
router.run_search("q", 5, chain="free", budget="free")       # a name from the chain file, no money
router.run_extract(urls, chain="prefer-jina", fallback=True)  # then the default order
router.run_search("q", 5, stages=["exa"], ignore_pause=True)  # one diagnostic attempt, charged
```

The same from the CLI:

```bash
quotarouter call search "q" --stages firecrawl
quotarouter route --chain free          # what it would dial, without dialing
quotarouter chains                      # what the chain file produced
```

And over MCP, as optional arguments on `web_search` / `web_extract`: `stages`, `chain`, `fallback`,
`force`, `budget`, `max_cost_usd`. An old client that knows none of them keeps the default chain.

Rules that make it predictable:

- **Strict by default.** A named chain never continues into the default order and never reaches for
  the anonymous tier. `fallback=True` (or `strict = false` in the file) asks for that explicitly.
- **A wrong argument is a caller error.** An unknown name, a stage without the capability, an empty
  list, both `stages` and `chain`, or a budget the chain cannot satisfy is refused before any
  vendor is dialed, and the message names what exists.
- **Same accounting.** A pinned call charges the ledger, parks stages and writes the audit line
  exactly like a default call. The line records `chain_mode`, `requested`, `budget` and — for a
  batch split across vendors — how many pages each stage delivered.
- **A chain's budget is a promise.** A caller can tighten it, never loosen it.
- **The operator can refuse the whole feature.** `QUOTAROUTER_ALLOW_CHAIN_OVERRIDE=0` makes
  overrides an error rather than a silent substitution — useful when a model in a foreign client
  must not be able to pin a paid vendor.
- **Not available inside Hermes tools.** `web_search`/`web_extract` there take `query`/`urls` only;
  the lever inside Hermes is the environment order, per process. The MCP and library surfaces carry
  the per-call choice.

Named chains live in `~/.config/quotarouter/chains.toml`
(`QUOTAROUTER_CHAINS_FILE` points elsewhere). The format, examples, precedence and the refusal
messages are documented in [`docs/chains.md`](docs/chains.md).

## Operations

```bash
python3 -m quotarouter.cli status           # what each vendor has left
python3 -m quotarouter.cli probe            # ask vendors for their balance, write the ledger
python3 -m quotarouter.cli route            # who serves the next call, and why the rest do not
python3 -m quotarouter.cli route --chain free  # the same for a named chain, without dialing
python3 -m quotarouter.cli chains           # what the chain file gave, and what it dropped
python3 -m quotarouter.cli call search "q" --stages exa   # one call through one vendor
python3 -m quotarouter.cli selftest         # one live search + one live extract
python3 -m quotarouter.cli disable exa --reason "key rotation"
python3 -m quotarouter.cli enable exa
python3 -m quotarouter.cli reset firecrawl  # clear local accounting after a plan change
python3 -m quotarouter.cli prune --days 30  # drop audit lines older than 30 days
python3 -m quotarouter.cli daemon --once    # one probe cycle, then exit (what cron calls)
python3 -m quotarouter.cli schedule         # print cron line + systemd units
python3 -m quotarouter.cli alert-test       # send one alert through the configured transport
```

Add `--json` to any command for machine-readable output.

### Scheduled probing

Run `probe` on a schedule so the first search of the day does not learn about an empty pool from
a 402. `quotarouter schedule` **prints** the units and installs nothing — enabling them is the
operator's decision:

```bash
$ python3 -m quotarouter.cli schedule
# 1. cron — one line, one cycle per run
17 */6 * * * cd /srv/quotarouter && /usr/bin/python3 -m quotarouter.cli daemon --once
# 2. systemd (user units) — write both files, then: systemctl --user enable --now quotarouter-probe.timer
...
```

`daemon --interval N` is the same cycle in the foreground (containers, tmux). `--once` is one
cycle and exit. Every cycle also prunes the audit trail by `QUOTAROUTER_LOG_KEEP_DAYS`.

### Alerts when the chain degrades

A silently-degraded chain is worse than a loud failure: answers keep coming from the keyless
tier for a week and nobody notices the paid key died. Configure **any one** transport and the
router speaks up:

```bash
QUOTAROUTER_ALERT_TELEGRAM="<bot token>:<chat id>"   # Bot API sendMessage
QUOTAROUTER_ALERT_WEBHOOK="https://example/hook"     # POST {"text": ...}
QUOTAROUTER_ALERT_COMMAND="notify-send quotarouter {kind}"   # shell, {kind} substituted
QUOTAROUTER_ALERT_COOLDOWN=900                       # seconds, per kind
```

Kinds: `no-stage` (nothing could serve), `keyless` (the paid chain was out), `quota` (a vendor
parked itself). Nothing configured means nothing sent — the default deployment is silent. A
transport failure is logged and swallowed: an unreachable webhook never fails a search. The
cooldown state lives in `<state>/alerts.json`, so a restart does not reopen the floodgates.

### Audit trail

`routes.jsonl` rotates itself at `QUOTAROUTER_LOG_MAX_BYTES` (8 MB) — the logging path does it,
so no cron job is needed for that. Age-based trimming is `quotarouter prune --days N`
(`QUOTAROUTER_LOG_KEEP_DAYS`, 30 by default), which `daemon` runs once per cycle.

### Parallel page fetching

One `web_extract` call with several URLs asks each vendor for pages in a small pool
(`QUOTAROUTER_EXTRACT_WORKERS`, default 4; `1` = strictly sequential). Four pages through Jina:
**2.47 s sequential → 1.18 s with four workers**, same four pages, no credits spent. The chain
itself stays sequential — one stage at a time — so a stage still cannot hide behind another.

## Error policy

| What the vendor returned | Kind | What the router does |
|---|---|---|
| 402 / `Insufficient Balance` / `NO_MORE_CREDITS` | `quota` | park until the window resets, else retry in 15 min doubling to 6 h |
| 401 / 403 unrelated to quota | `auth` | park 24 h — a human must look |
| 429 | `throttle` | pause per `Retry-After`, else 60 s with doubling |
| 5xx, timeout, connection reset | `transient` | pause 60 s, try the next stage |
| 200 but empty | `empty` | next stage |
| Extract: every URL failed | `bad_page` | next stage |
| Extract: 403/404/410 from the page itself | — | **not** a stage failure: report the site's own answer |

That last row matters. `403 from origin` is the website answering, not the vendor failing.
Spending a second vendor's credits on a page that is closed to everyone is waste.

### Who parked a vendor, and who can unpark it

Every pause records its origin: `auto` (the router saw a reason) or `operator` (`disable`).
The two behave differently on purpose, because the reason behind them resolves differently.

- **`auto`** — lifts itself. The vendor's balance endpoint is free, so a paused stage is still
  probed on every call (rate-limited to once a minute): the moment it reports room again, the
  pause is dropped and the stage is back in the chain. A retry deadline is only a backstop for
  the next time the balance has not come back — 15 minutes, doubling per consecutive failure, up
  to six hours. Top up an account and the next search uses it; no `enable` needed.
- **`operator`** — `quotarouter disable <vendor>` writes an infinite deadline that no probe,
  no top-up and no window roll may lift. Only `quotarouter enable <vendor>` does.

`quotarouter status` shows which one you are looking at: `quota 29m (auto)` versus
`stopped: chasing a bug`. `quotarouter recheck` runs the probe sweep immediately instead of
waiting for the next search — useful right after a top-up, and safe to run from cron.

Knobs: `QUOTAROUTER_QUOTA_RETRY` (default 900 s, `0` restores the old 24 h park) and
`QUOTAROUTER_QUOTA_RETRY_MAX` (default 21600 s).

Records written by an older version carried no origin. They are read by their reason: a
`disabled` pause whose text came from a balance probe is treated as `auto`, one that says
"operator" stays an operator stop.

## Testing

Full ladder — `docs/testing.md`: what each level proves, what it costs, what counts as
evidence. In short:

```bash
python3 -m pytest -q                 # 288 tests, offline, no network, no keys
python3 -m pytest -m live -q         # real vendors, ~$0.01, spends quota
python3 -m pytest -m e2e -q          # end-to-end inside a real Hermes, ~$0.01
python3 tests/e2e/harness.py         # the same without pytest, writes e2e-report.json
python3 -m pytest -q --cov=quotarouter --cov-report=term-missing   # 100% line coverage
```

Eight levels:

1. `test_errors.py`, `test_ledger.py` — response classification and accounting, as tables.
2. `test_stages.py`, `test_routing.py` — adapters and the chain on `httpx.MockTransport`.
3. `test_adversarial.py` — recorded vendor responses (`tests/fixtures/`) and hostile cases:
   `success: true` with no rows, HTTP 200 carrying an error body, dropped connection, schema
   change.
4. `test_ops.py` — alerting (cooldown, all three transports, failure isolation), log retention
   (rotate, prune), the probe daemon, `schedule` output, and the per-URL pool (proved with a
   threading barrier, which only lets both fetches through if they overlap).
5. `test_live.py`, `test_mcp_integration.py` — real vendors and a real MCP subprocess.
   Marked `live` / offline respectively.
6. `tests/e2e/` — the plugin inside a real Hermes: loader, provider registry, tool envelope,
   config reading, isolation from the live profile. Marked `e2e`.
7. Protocol and operational surface: `test_mcp_protocol.py` (the MCP server in-process),
   `test_cli.py` (every command against a fake router), `test_hermes_provider.py`,
   `test_resilience.py` (corrupt ledger, half-written state), `test_state_machine.py`
   (states that only exist across calls), `test_self_healing.py` (machine pauses versus
   operator stops), `test_jina_reader.py`, `test_stage_base.py`.
8. `test_alerts_and_keyless.py` — the alerting layer on its worst day and the anonymous tier's
   protocol edges.

Levels 5 and 6 do not run by default: `addopts = "-m 'not live and not e2e'"` in
`pyproject.toml`. The harness and the examples above were run on Python 3.11, 3.12, 3.13 and
3.14.

There is no CI in this repository by choice: `scripts/preflight.sh` is the gate (offline suite,
`hermes plugins validate`, `build` + `twine check`), and `--live` adds the vendor and end-to-end
levels. What it checks and why is written down in `docs/development.md`.

The code was also reviewed by models that did not write it — `scripts/model_review.py` runs that
review (free tiers only) and `docs/reviews/2026-11-22-model-review.md` records what came back,
what was fixed and what was rejected with a reason. Two of those findings were accounting bugs
that made the ledger optimistic, which is the failure mode this project exists to prevent.

## What is verified, and what is only written

Being useful about quotas means being honest about evidence. `docs/vendor-probes.md` records
the difference per vendor, and it is part of the contract:

- **Exercised against the live API**: `exa` (search + contents), `firecrawl` (search, scrape,
  credit balance), `jina` (Reader), `deepseek` (balance probe — the stage is parked while the
  account has no funds), and the anonymous tier (search and fetch through a public MCP server).
- **Covered by tests only, never seen answering**: `tavily` and `keenable`. Fixtures for both
  were written by hand from the vendor documentation; no key was ever used. They are wired in
  and skipped without a key, but treat them as unproven until `docs/vendor-probes.md` says
  otherwise.
- **Not verified at all**: Claude Desktop, Cursor, Codex and Claude Code as MCP clients. The
  server was proven with the reference SDK client and with `opencode`; for the others the
  configuration above is "should work", not "tested".
- **Per-call chains, verified live**: a search pinned to `exa` (`chain_mode: custom` in the audit
  line), and an extract through the `free` chain served by `jina` with `delivered: {"jina": 1}`.
  The budget rules and the refusals are covered offline; a live call under a *spent* grant is not
  something a test can arrange without burning an allowance.

Two things this package deliberately does not do: it does not answer from a cache, and it does
not hide a degraded call. If nothing serves, the error names every stage and why it was out.

## Project

- [CHANGELOG.md](CHANGELOG.md) — what changed, and why the version moved
- [CONTRIBUTING.md](CONTRIBUTING.md) — the gate, the rules enforced by tests, how to add a vendor
- [SECURITY.md](SECURITY.md) — how to report a credential-handling bug, and what the package
  writes where
- [docs/chains.md](docs/chains.md) — how to configure named chains (with an example file)
- [docs/development.md](docs/development.md), [docs/testing.md](docs/testing.md),
  [docs/vendor-probes.md](docs/vendor-probes.md)

## State files

```
~/.local/state/quotarouter/ledger.json    quotas, pauses, billing periods
~/.local/state/quotarouter/routes.jsonl   one JSON line per call: who served it, which chain it
                                         walked, who could not and why
~/.config/quotarouter/chains.toml         named chains (optional), read once at startup
~/.local/state/quotarouter/routes.jsonl.1 previous generation, after rotation
~/.local/state/quotarouter/alerts.json    last time each alert kind was sent (cooldown state)
```

`QUOTAROUTER_STATE_DIR` overrides the directory. Both files are written atomically
(`os.replace`) and survive a gateway restart.

## License

MIT — see `LICENSE`.
