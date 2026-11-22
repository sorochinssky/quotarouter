# Operational surface: scheduled probing, degradation alerts, log retention, parallel pages

## Why

The router already chooses a stage by remaining quota and records why. What it could not do:
notice a dead vendor on its own, tell anyone about it, keep its own audit trail bounded, or
fetch several pages without paying one timeout after another. Every one of those was listed as
"operator glue" — a cron line here, a `logrotate` there — which means in practice nobody does it
until something breaks quietly.

## What changes

* **Scheduled probing without installing anything.** `quotarouter daemon` runs the probe cycle
  (`--once` for cron, `--interval` for the foreground); `quotarouter schedule` prints a cron line
  and a systemd service+timer. The repository installs nothing: enabling a timer is the
  operator's decision.
* **Alerts on degradation.** `quotarouter/alerts.py`: three transports (Telegram Bot API,
  generic webhook, shell command), one cooldown per alert kind (`no-stage`, `keyless`, `quota`),
  state on disk. Nothing configured means nothing sent. A transport failure is logged and
  swallowed — an unreachable webhook must never fail a search.
* **Bounded audit trail.** `routes.jsonl` rotates inside the logging path at
  `QUOTAROUTER_LOG_MAX_BYTES`, and `quotarouter prune --days N` drops old lines (also run by
  `daemon` each cycle). An unparsable line is kept: losing evidence is worse than a stray line.
* **Parallel page fetching.** `QUOTAROUTER_EXTRACT_WORKERS` (default 4) fetches the URLs of one
  stage call in a pool; the chain stays sequential, so one stage still cannot hide behind
  another. Measured: four pages via Jina, 2.47 s → 1.18 s.
* **`quota_probe` MCP tool** next to `quota_status`: ask the vendors for a real balance instead
  of reading the ledger's belief.

## Not changing

* No CI. The gate is `scripts/preflight.sh` plus `docs/development.md`; a local green run is the
  contract. (A rule in the agent instructions was the explicit ask.)
* No keyless promotion, no change to stage order, no change to the error policy.
* Nothing is enabled by default that talks to the outside world: alerts stay silent until a
  transport is configured.

## Impact

`quotarouter/{alerts,retention}.py` are new; `config.py` gains `extract_workers`, `log_max_bytes`,
`log_keep_days`, `probe_interval` and a public `env()`; `routing.log_route` rotates and alerts;
`stages/base.py` gains `_map_urls`; `jina.py`/`firecrawl.py` fetch per URL through it;
`integrations/mcp.py` gains `quota_probe`; `cli.py` gains `prune`, `daemon`, `schedule`,
`alert-test`; `hermes_plugin/__init__.py` imports the package explicitly instead of a symlink
(`hermes plugins validate` flagged the link as `symlink_escape`).
