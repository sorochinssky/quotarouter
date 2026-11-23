# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.3.2] — 2026-11-23

### Changed

- The README installs with `pip install quotarouter`: 0.3.1 is published on PyPI, so the
  line that told the reader to install from a checkout (and promised PyPI "once it is")
  was describing a state that no longer exists. Published metadata cannot be edited,
  so this ships as a new release.

## [0.3.1] — 2026-11-23

### Fixed

- A ledger entry whose counters are not numbers is dropped by name and reported, instead of being
  loaded as-is. Before this, a file edited by hand (or truncated by a full disk) could hand the
  router a list where it expected a float and fail inside `charge()` — halfway through a call, where
  it looks like a vendor outage. The other entries are kept, as with any malformed record.
- `jina` had a branch after `_raise_for_status` that could never run; the page-level error now
  comes from the classifier itself.

### Changed

- Line coverage is 100%, up from 94%: the offline suite now exercises every branch of the CLI,
  the integrations, the ledger's corruption paths, the retention edges and each vendor's
  non-JSON and empty-answer handling. `pytest -q` runs 498 tests.

## [0.3.0] — 2026-11-23

### Added

- **A call can name its own chain**, down to a single vendor: `run_search(..., stages=["exa"])`,
  `chain="free"`, `fallback=True`, `ignore_pause=True`; the same optional arguments on the MCP
  tools (`stages`, `chain`, `fallback`, `force`, `budget`, `max_cost_usd`). Without them a call
  walks the configured order exactly as before.
- **Named chains in a file**: `~/.config/quotarouter/chains.toml` (`QUOTAROUTER_CHAINS_FILE`),
  a chain being a pair of stage lists plus a description, a budget and a strictness flag. A
  malformed entry is reported and dropped; the rest still loads. `QUOTAROUTER_CHAINS` remains as a
  one-line shorthand and replaces the file when set. Guide: `docs/chains.md`.
- **A per-call budget**: `budget="free"` refuses to spend money (no balance-funded stage, no
  overrun of a spent free grant), and `max_cost_usd` refuses a call whose estimate exceeds the
  ceiling before any vendor is dialed. A stage declares how it is funded (`free` / `balance`), so
  "free" is a fact rather than an inference.
- `quotarouter chains`, `quotarouter call`, and `route --chain/--stages/--budget/--max-cost` —
  all of them resolve without dialing a vendor.
- The audit line records `chain_mode`, `requested`, `budget` and, for a batch served by more than
  one vendor, `delivered: {stage: pages}`. All fields are additive; older readers ignore them.

### Changed

- `quota_status` over MCP now reports `vendors`, `chains` and `allow_chain_override`, and each
  vendor's `funding`. The vendor map moved under `vendors`.
- The plugin manifest version is checked against `quotarouter.__version__` by a test, and its
  description no longer repeats the default order.

### Added (earlier)

- `quotarouter recheck` — probe the vendors now and release machine pauses immediately,
  instead of waiting for the next search.
- `QUOTAROUTER_QUOTA_RETRY` / `QUOTAROUTER_QUOTA_RETRY_MAX` — how long a vendor parked for an
  unfunded account waits before it is retried (default 900 s, doubling, capped at 6 h).

### Changed

- A vendor parked because its account cannot pay is now a machine pause with a deadline, not an
  infinite one: it returns by itself as soon as a balance probe reports room. Only
  `disable` (an operator stop) is permanent. Records written before this distinction are read
  by their reason.
- An empty page from any stage is a failure, not a result: `selftest` no longer counts a
  whitespace-only page as delivered.

### Fixed

- Transport failures (timeouts, connection resets, TLS errors) were never translated into the
  package's own error type, so every stage's failure handling was dead code for real network
  problems: a vendor that had gone dark looked like one bad page and was dialed again on the
  next call.
- The keyless tier negotiated MCP without accepting `text/event-stream` (every call answered
  `406`) and passed `{"url": …}` where `{"urls": [...]}` is required, so the last-resort stage
  had never worked and reported an error message as page content.
- `ledger.iso()` raised `OverflowError` on an operator stop (`inf` deadline), which aborted
  `status` and every probe report.
- A corrupt ledger entry no longer discards the readable ones; a ledger whose JSON parses but
  has no vendor map is reported and rebuilt instead of silently zeroing all counters.
- Tavily extract returned only the successful URLs and lost the caller's order; a failed URL is
  now a page error in its original position.

## [0.2.0] — 2026-11-22

First version meant to be used outside the author's machine.

### Added

- Provider-agnostic core: `Router.run_search()` / `Router.run_extract()`, persistent quota
  ledger, pause kinds with backoff, and an audit trail (`routes.jsonl`).
- Stages: `exa`, `firecrawl`, `deepseek`, `tavily`, `keenable`, `jina` (Reader, works without a
  key), and an anonymous MCP-based tier as the last resort.
- Integrations: a Hermes web-search provider and an MCP stdio server (`web_search`,
  `web_extract`, `quota_status`, `quota_probe`) that works with any MCP client.
- CLI: `status`, `probe`, `route`, `selftest`, `serve-mcp`, `prune`, `daemon`, `schedule`,
  `disable`, `enable`, `reset`, `alert-test`.
- Bounded per-URL parallelism for extract, log rotation, alert transports (webhook, Telegram,
  command) with a cooldown, and a page-level error policy that does not park a healthy stage for
  a blocked website.

[Unreleased]: https://github.com/sorochinssky/quotarouter/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/sorochinssky/quotarouter/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/sorochinssky/quotarouter/releases/tag/v0.3.1
[0.3.0]: https://github.com/sorochinssky/quotarouter/releases/tag/v0.3.0
[0.2.0]: https://github.com/sorochinssky/quotarouter/releases/tag/v0.2.0
