# Design: e2e over the real call path

## The stand

One run = one temporary `HERMES_HOME` (a directory under the system temp dir, deleted or kept
behind a flag). Inside it:

```
<home>/config.yaml                      web.search_backend: quotarouter,
                                        extract_backend: quotarouter, keyless_rescue: false
<home>/.env                             vendor keys (copied from the run's environment)
<home>/plugins/quotarouter/__init__.py  register(ctx) -> ctx.register_web_search_provider(...)
<home>/plugins/quotarouter/plugin.yaml  name: quotarouter, provides_web_providers: [quotarouter]
<home>/plugins/quotarouter/quotarouter  symlink to the package in this repository
<home>/state/quotarouter/               ledger.json + routes.jsonl as written by the plugin
```

Why `HERMES_HOME` and not an edit of the live config: Hermes reads its config on every call,
so switching `web.search_backend` in the live profile instantly moves the Telegram bot onto
an experimental backend. An isolated home gives the same code path and zero risk.

Why a symlink to the package and not a copy: e2e must exercise **the** code that lives in the
repository. A copy would drift from every edit.

## Run steps (each one an entry in the report)

| # | Step | What it proves |
|---|---|---|
| 1 | `discover_plugins()` → `get_provider("quotarouter")` | the loader finds the plugin, and the backend name matches the config |
| 2 | `web_search_tool(query, 3)` | the whole path: tool → registry → provider → stage → vendor; the `{success, data.web[]}` envelope |
| 3 | `asyncio.run(web_extract_tool([url]))` | the asynchronous branch and the keyless extract via Jina |
| 4 | `routes.jsonl` and `ledger.json` under `<home>/state/quotarouter` | accounting is written and isolation holds |
| 5 | the same call with empty keys and `QUOTAROUTER_KEYLESS_LAST_RESORT=false` | honest degradation: `success: false` with reasons, no traceback |
| 6 | the live profile is unchanged | `~/.local/state/quotarouter` did not appear or grow |

Step 5 runs in its own process with a different `HERMES_HOME`, so no keys leak into it.

## What counts as evidence

- Step 2: `success: true`, ≥1 result, and `data.backend` starts with `quotarouter:`.
- Step 3: `results[0].content` is non-empty and at least `QUOTAROUTER_MIN_PAGE_CHARS` long.
- Step 4: `routes.jsonl` gained a line with `capability=search` and a real vendor in
  `served_by`; in `ledger.json` the serving vendor shows `used > 0` (or `source: api` for
  Firecrawl, where the remainder comes from the vendor).
- Step 5: the tool returns JSON with `success: false`, the error text names the stages, and
  no process crashed.
- The run's exit code is 0 only if every step passed.

## Why a separate marker rather than `live`

`live` means "goes to the internet". E2E additionally needs an installed Hermes and spends
quota through the tool path. Separate markers let you run the adapters live without raising
the stand, and the other way round. Both are excluded from a plain `pytest`.

## The report

`e2e-report.json` in the repository root (the path is printed at the end):

```json
{"ok": true, "steps": [{"name": "search_tool", "ok": true,
                        "detail": "3 results via quotarouter:exa", "seconds": 1.4}],
 "home": "/tmp/...", "spent": {"search": 1, "extract": 1}}
```

The report is not decoration: it is the only evidence that the run happened, and it belongs
in a release note or an incident review.

## Findings of the first run (2026-11-22)

The stand found three defects the offline levels had missed — which is the proof that this
level is needed:

1. **The provider did not inherit `WebSearchProvider`.** The Hermes registry refused it with
   `tried to register a web provider that does not inherit from WebSearchProvider` and said
   nothing in the agent's answer. The plugin loaded "successfully" and registered nothing.
   Fix: import the base class, falling back to `object` so standalone tests still run.
2. **The "short page = wall" threshold was set to 400 characters.** example.com carries 156
   characters of real text; the chain called that a block, parked Jina for 60 seconds and
   spent a Firecrawl credit on a perfectly good page. Fix: threshold 40 (an empty shell);
   walls are caught by their markers (`enable javascript`, `captcha`, …), not by length.
3. **`except Exception as exc` in the Jina adapter read `exc.kind`** — a non-network exception
   crashed the adapter instead of answering "this page did not come out". Fix: `StageError`
   and everything else are handled separately.
