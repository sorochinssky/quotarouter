# How quotarouter is tested

Six levels. Each answers its own question and pays its own price. Run them bottom-up:
levels 1–4 are free and fast, levels 5–6 spend vendor quota and need an installed Hermes.

```
1. Contracts and accounting     pytest -q             0 s, $0      always
2. Adapters on mocks            pytest -q             1 s, $0      always
3. Recorded + hostile responses pytest -q             1 s, $0      always
4. Operational surface          pytest -q             1 s, $0      always
5. Live vendors                 pytest -m live -q     3 s, ~$0.01  before a release
6. End-to-end in real Hermes    pytest -m e2e -q     15 s, ~$0.01  before a release and
                                                                  after any change to the
                                                                  shim, the manifest or config
```

`scripts/preflight.sh` runs levels 1–4, `hermes plugins validate hermes_plugin`, and
`build` + `twine check` in one command; `scripts/preflight.sh --live` adds levels 5–6. There is
no CI: the gate is local, and `docs/development.md` says what it is for.

## Level 1 — contracts and accounting (`tests/test_errors.py`, `tests/test_ledger.py`)

**What it proves:** vendor responses are classified correctly (402 → quota, 401 → auth,
429 → throttle, 5xx → transient), and the ledger computes the remainder, rolls the window and
lifts a pause correctly.

**Cost:** zero.

**Shape:** parameter tables. Each row is a real vendor response with its expected kind. A new
vendor or a new error code adds a row, not a new test file.

## Level 2 — adapters and the chain on mocks (`tests/test_stages.py`, `tests/test_routing.py`)

**What it proves:** an adapter turns a vendor response into the Hermes envelope; the chain
walks the order, parks a failed stage, and **does not touch the second stage when the first
one succeeded**.

**Cost:** zero — `httpx.MockTransport` replaces the single network entry point
(`quotarouter/http_client.py`).

**What it does not prove:** that the vendor actually answers that way. Hence level 3.

## Level 3 — recorded and hostile responses (`tests/test_adversarial.py`, `tests/fixtures/`)

**What it proves:** the chain survives real response bodies (captured from production on
2026-11-22) and hostile ones: `{"success": true, "data": {"web": []}}`, HTTP 200 carrying an
error body, a dropped connection, a changed response schema, `403 from origin` (the website
answering, not the vendor failing — the chain must not spend the next stage's credits).

**Cost:** zero.

**How to extend the fixtures:** `tests/fixtures/` holds trimmed real responses. Capture a new
response by hand, add the file, add a scenario. If a vendor changes its schema, it shows up
here as a failure instead of as an empty result in production.

## Level 4 — operational surface (`tests/test_ops.py`)

**What it proves:** the parts an operator leans on after the plugin "works" behave:

* alerts fire on `no-stage`, `keyless` and `quota`, honour the cooldown, can be forced, reach
  all three transports, and **never raise** — a broken command or unreachable webhook must not
  fail a search;
* the audit trail rotates at the configured size from inside the logging path, and `prune`
  drops only lines that carry an old timestamp (an unparsable line is evidence: keep it);
* `daemon` runs exactly N cycles, survives a failing cycle, and reports which vendors gave no
  answer; `schedule` prints a cron line and systemd units without installing anything;
* several URLs are fetched concurrently — proved with a `threading.Barrier`, which only lets
  both requests through if they actually overlap, and would time out on a sequential loop.

## Level 5 — live vendors (`tests/test_live.py`, `tests/test_mcp_integration.py`)

**What it proves:** the keys are alive, endpoints and prices have not moved, Firecrawl's
`credit-usage` parses, the DeepSeek balance reads, Jina returns text, and the MCP server
answers `initialize` / `tools/list` / `tools/call` in a real subprocess.

**Cost:** one Exa search (~$0.007), one `credit-usage` call (free), one Jina page (free), one
DeepSeek `balance` (free). The MCP tests cost nothing — they never leave the ledger.

```bash
set -a && . ~/.hermes/.env && set +a
python3 -m pytest -m live -q -rA
```

**Rule:** `live` tests deliberately do not mock the network. If a vendor is unreachable, the
test fails — that is the signal.

**Careful:** the e2e tests are marked `e2e` *and* `live` (they hit the network too), so
`pytest -m live` runs them as well. Live adapters only, without the stand:
`pytest -m "live and not e2e"`.

## Level 6 — end-to-end in a real Hermes (`tests/e2e/`)

**What it proves:** Hermes really picks the plugin up and hands the result to the **model**.
Four things sit between "the adapter answered" and "the agent got it", and each fails on its
own:

1. the plugin loader (was the directory found, is it in `plugins.enabled`);
2. the provider registry — it **refuses** any provider that does not inherit
   `agent.web_search_provider.WebSearchProvider`; without the base class the plugin loads and
   registers nothing;
3. the tool response envelope (`success`, `data.web[]`, `backend`);
4. config reading (`web.search_backend: quotarouter`).

**Cost:** 1 search + 1 page. The run prints what it spent.

### How the stand is built

`tests/e2e/harness.py` assembles a temporary `HERMES_HOME`:

```
<tmp>/hermes-home/config.yaml              plugins.enabled: [quotarouter], web.*_backend: quotarouter
<tmp>/hermes-home/.env                     vendor keys (taken from the run's environment)
<tmp>/hermes-home/plugins/quotarouter -> … symlink to the repository's hermes_plugin/
<tmp>/hermes-home/state/quotarouter/       this run's ledger.json + routes.jsonl
```

The child process starts with `HERMES_HOME=<tmp>` and `PYTHONPATH=/usr/local/lib/hermes-agent`.
Isolation is not a claim: the `isolation` step fingerprints the live `~/.hermes` (config +
state) before and after, and fails if anything moved.

The plugin is symlinked, never copied: the stand must exercise the code that lives in this
repository.

### Run steps

| Step | What it checks |
|---|---|
| `plugin_registry` | the plugin is found, enabled, and `get_provider("quotarouter")` returns an object |
| `search_tool` | `web_search_tool` → `success: true`, ≥1 result, `backend` = `quotarouter:<stage>` |
| `extract_tool` | `web_extract_tool` → non-empty `content` |
| `state_isolated` | `routes.jsonl` and `ledger.json` appear under the isolated home, and the log carries `served_by` |
| `degradation_without_keys` | with no keys and no keyless fallback the tool returns `success: false` with a per-stage reason, not a traceback |
| `isolation` | the live profile did not change |

### Commands

```bash
# full run, report in e2e-report.json, exit 0 only if every step passed
set -a && . ~/.hermes/.env && set +a
python3 tests/e2e/harness.py

# no vendor calls (zero cost): registry, degradation, isolation
python3 tests/e2e/harness.py --no-live

# keep the stand directory to inspect ledger.json / routes.jsonl
python3 tests/e2e/harness.py --keep-home

# the same through pytest (marked e2e+live, excluded from a plain run)
python3 -m pytest -m e2e -q
```

Variables: `HERMES_ROOT` (default `/usr/local/lib/hermes-agent`), `HERMES_LIVE_HOME`
(default `~/.hermes`), `E2E_QUERY`, `E2E_URL`, `QUOTAROUTER_E2E_CHILD_TIMEOUT` (180 s),
`QUOTAROUTER_LIVE_STATE` (default `~/.local/state/quotarouter`).

### Report

`e2e-report.json` in the repository root: `ok`, `home`, `spent`, `notes`, `steps[]` with
`name/ok/seconds/detail`. It is the only evidence that the run happened — attach it to a
release and to any incident review.

### When an e2e run is mandatory

- `hermes_plugin/` (the shim, `plugin.yaml`) or `quotarouter/integrations/hermes.py` changed;
- `web.search_backend` / `web.extract_backend` / `plugins.enabled` changed;
- Hermes itself was upgraded (`hermes --version` moved) — the provider contract may have moved;
- before installing the plugin into a live profile.

## Level 7 — the surface the levels above skipped

A coverage run (`pytest --cov=quotarouter --cov-report=term-missing`) showed which modules the
first six levels never entered. Each file below exists because something there was uncovered
*and* costly to get wrong, not because a percentage was missing.

- `test_mcp_protocol.py` — the MCP layer in-process: `initialize` negotiates a supported
  revision instead of echoing whatever the client asked for, `tools/list` carries schemas,
  a malformed argument returns `isError` and leaves the server alive, unknown methods and
  notifications do not crash the loop.
- `test_cli.py` — every command against a fake router: `status`, `probe`, `selftest`, `prune`,
  `serve-mcp`, and the honest `fallback: keyless` line when nothing is configured.
- `test_resilience.py` — a corrupt `ledger.json` (`{not json at all`) must not silently reset
  the counters, and a half-readable file must keep what it can parse.
- `test_hermes_provider.py` — the Hermes adapter: `is_available()` stays cheap and offline,
  `search()` clamps the limit, a total failure returns a readable result instead of raising.
- `test_state_machine.py` — states that only exist across calls: a vendor refusal on every page
  parks the stage with the vendor's kind, an origin refusal on every page does not park it,
  the anonymous tier being disabled, and a second process having spent credits meanwhile.
- `test_alerts_and_keyless.py` — the alerting layer on its worst day (dead webhook, 500, a
  half-configured Telegram target, an unparsable cooldown file) and the anonymous tier's
  protocol edges (a ping-only event stream, an empty answer, the operator killing the tier).
- `test_jina_reader.py` — the stage that serves every extract by default: header/body split,
  stub detection, blocked domains staying page-level, rate limits keeping their kind, and every
  requested URL coming back in order.
- `test_stage_base.py` — parallel fetching preserving the caller's order, a single worker
  staying sequential, and an adapter that raises a bare exception being parked as `transient`
  instead of killing the call.

### What the coverage run itself found

Writing these tests exposed a defect no level above could see: `http_client.get/post` did not
translate `httpx` transport errors into `StageError`. Every `except errors.StageError` branch in
every stage was therefore dead code for real network problems — a vendor that had gone dark
looked like one bad page, and it was dialed again on the next call. The translation now happens
in one place (see `http_client._call`), and `test_jina_reader.py` pins it: a timeout is a
`transient` page error, so the router can pause the stage instead of re-dialing it.

## What these levels do not cover

- Load behaviour and concurrent calls (vendor QPS limits).
- Long-term ledger life: the real window roll-over on the 30th is only tested on a fake clock
  (`test_ledger.py`).
- Work through `delegate_task` and subagents — that path initialises plugins differently.
- The live model's behaviour (what queries it writes) — that is not about the plugin.

## Debugging a failed step

1. `FAIL plugin_registry` → the plugin directory was not found or not enabled: check
   `plugins.enabled`, and that `hermes_plugin/quotarouter` is a symlink, not an empty directory.
2. `FAIL search_tool` with `success: false` → read the error: it lists every stage with the
   reason it was skipped (`no EXA_API_KEY set`, `quota spent 10/10 usd`, …).
3. `FAIL extract_tool` → open `<home>/state/quotarouter/routes.jsonl`; the chain line carries
   a reason per stage.
4. `FAIL isolation` → the stand wrote into the live profile. That is a stand bug, not a plugin
   bug, and it must not be ignored: the next run will corrupt production counters.
