# E2E verification of the plugin in a real Hermes

## Why

What was proven so far is the **core** of the router: 78 offline tests on
`httpx.MockTransport` and 4 live adapter tests. What was not proven is the operational part:
that Hermes actually picks the plugin up, serves `web_search` / `web_extract` through it, and
writes quota accounting to the right place. Four things sit between "the adapter answers" and
"the tool answers the agent" — the plugin loader, the provider registry, the tool response
envelope and the config — and any of the four can fail silently.

The repository needs an end-to-end run over the real call path, with real vendors, in an
isolated profile, so the check costs the live agent neither quota nor corrupted state.

## What Changes

- A new test level `tests/e2e/`: a run of `web_search_tool` / `web_extract_tool` from Hermes
  with the `quotarouter` backend, in an isolated `HERMES_HOME`, with a JSON report and an
  exit code.
- A degradation scenario: empty keys → the tool returns an honest error naming every stage,
  with no traceback.
- Proof of isolation: the ledger and the route log appear under the isolated `HERMES_HOME`,
  not in the live profile.
- `docs/testing.md` — the ladder of test levels: what each level checks, what it costs in
  quota, what counts as evidence, how to run it.
- `pyproject.toml` gains the `e2e` marker; it does not run by default.

## Out of scope

- Installing the plugin into the live profile and switching `web.search_backend` to
  `quotarouter` — a separate operator action (it needs a gateway restart).
- Cron jobs, alerts, quota-spend monitoring.
- Load testing and concurrent calls.

## Impact

- Files touched: `pyproject.toml`, new `tests/e2e/`, new `docs/testing.md`, README (the
  testing section).
- Production quota risk: an e2e run spends 1 search + 1 page; the `live`/`e2e` markers keep
  it out of a plain `pytest`.
- Live profile risk: zero — isolation is done through `HERMES_HOME` and is checked by a
  dedicated step.
