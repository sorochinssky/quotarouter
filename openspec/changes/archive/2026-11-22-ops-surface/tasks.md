# Tasks

## Alerts

- [x] `alerts.py`: `AlertSettings`, `notify(kind, text, now=, force=, extra=)`, cooldown on disk
- [x] Transports: Telegram `<token>:<chat id>`, webhook `{"text": ...}`, shell command with `{kind}`
- [x] Kinds: `no-stage`, `keyless`, `quota`; nothing configured means nothing sent
- [x] Wire `routing.log_route`: alert on total failure, keyless service, and a parked vendor
- [x] An alert failure never raises out of the call path

## Retention

- [x] `retention.maybe_rotate(path, max_bytes)` — atomic rename, one generation
- [x] `retention.prune(path, days)` / `prune_directory` — keep unparsable lines
- [x] Rotation runs inside `log_route` at `QUOTAROUTER_LOG_MAX_BYTES`
- [x] `quotarouter prune --days N --json`

## Scheduling

- [x] `daemon --once` / `--interval N` (`run_daemon` injectable for tests)
- [x] `schedule` prints cron + systemd service + timer, installs nothing
- [x] `EnvironmentFile` only when a real env file exists

## Parallel pages

- [x] `stages/base.py::_map_urls` — `ThreadPoolExecutor`, order preserved, `1` = sequential
- [x] `jina.py` / `firecrawl.py` fetch per URL through it
- [x] Test with `threading.Barrier`: sequential code times out, parallel code passes
- [x] Live measurement recorded in the README (2.47 s → 1.18 s on four pages)

## MCP

- [x] `quota_probe` tool: probe vendors, return the report, spend no credits
- [x] Tool list asserted in `test_mcp_integration.py`

## Distribution

- [x] `hermes_plugin/__init__.py`: explicit import, no symlink (`symlink_escape`)
- [x] `hermes plugins validate hermes_plugin` → Validation passed
- [x] `python -m build`, `twine check dist/*`, wheel installed in a clean venv, MCP handshake
- [x] `scripts/preflight.sh` as the gate; `docs/development.md` as the rule (no CI)

## Verification

- [x] Offline suite: 128 passed, 12 deselected
- [x] `preflight.sh` green (offline + validate + build + twine)
- [x] Live: `web_extract` through the reloaded plugin
