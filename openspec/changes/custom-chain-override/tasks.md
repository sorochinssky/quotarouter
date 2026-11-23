# Tasks

Order matters: configuration and resolution first, because every later step is a caller of it.
Each task carries the command that proves it, and the result that counts as proof.

## 1. Configuration surface

- [ ] 1.1 Read a chain file: `QUOTAROUTER_CHAINS_FILE` if set, else
  `~/.config/quotarouter/chains.toml`, else nothing. Parse with `tomllib`. Produce an immutable
  mapping name → `(search stages, extract stages, description, budget, strict)`. Check:
  `python -m pytest tests/test_chain_file.py -q` → green, including a file with one capability
  declared and one with both.
- [ ] 1.2 Invalidate an entry, keep the file: unknown key, unknown stage name, empty list, bad
  budget value, wrong type. Each is reported by name and dropped; every other entry still loads.
  Check: a case per malformed form asserting the surviving names and the reported message.
- [ ] 1.3 Keep the single-line `QUOTAROUTER_CHAINS` shorthand: one list applied to both
  capabilities; when set it replaces the file, and the log says the file was ignored. Check: a test
  with both present asserting only the shorthand's names exist and the warning was emitted.
- [ ] 1.4 Add `QUOTAROUTER_ALLOW_CHAIN_OVERRIDE` (default on) to `Settings`. Check:
  `python -c "from quotarouter import config; print(config.Settings().allow_chain_override)"` → `True`.
- [ ] 1.4b An unreadable chain file (missing, permission denied, broken TOML) is reported once and
  the default order serves. Check: three cases, each asserting startup succeeds, the message names
  the path, and `settings.search_order` is the default.
- [ ] 1.5 Declare funding per stage: `funding = "free"` (unmetered or free monthly grant) or
  `"balance"` (prepaid money). `deepseek` is the only stage declaring `balance` today. Check:
  `python -m pytest tests/test_funding.py -q` → every stage class answers, and the status surface
  shows it.

## 2. Resolution

- [ ] 2.1 Add `resolve_chain(capability, stages=None, chain=None, fallback=False, budget=None,
  max_cost_usd=None)` returning mode, names, whether the anonymous tier is allowed, and the budget
  filter it applied. Check: `python -m pytest tests/test_chain_resolution.py -q` → one passing case
  per row of the decision table, each asserting the chosen names *and* the refusal text.
- [ ] 2.2 Refuse `stages` together with `chain`, an empty list, an unknown name (listing what
  exists), a capability mismatch, and a chain that declares no stages for the requested capability.
  Check: the same file, five cases asserting `ValueError` with the named argument in the message.
- [ ] 2.3 Refuse any override when the kill switch is off, naming the setting. Check: a case
  asserting the message contains `QUOTAROUTER_ALLOW_CHAIN_OVERRIDE` and that no stage was dialed.

## 3. The walk

- [ ] 3.1 `Router.run_search` / `run_extract` accept `stages`, `chain`, `fallback`, `ignore_pause`,
  `budget`, `max_cost_usd` and walk the resolved chain; `candidates()` takes the resolved chain
  instead of reading `settings.*_order` directly. Check: `python -m pytest -q` → the existing 296
  tests pass unchanged, which is the no-regression proof.
- [ ] 3.2 A strict chain never consults the anonymous tier. Check: a test with every named stage
  refusing, the anonymous tier enabled globally, asserting the failure and that the keyless stage's
  call counter is zero.
- [ ] 3.3 `fallback=True` continues into the default order, skipping names already tried, and
  restores the end-of-chain policy. Check: a test asserting the exact attempt order with no
  duplicate stage names.

## 4. Budget

- [ ] 4.1 `budget="free"` selects only free-funded stages and free grants that still have room; a
  balance-funded stage never serves it; every skip carries a reason. Check:
  `python -m pytest tests/test_call_budget.py -q` → a case per funding kind, asserting the attempt
  list and that the balance-funded stage's call counter is zero.
- [ ] 4.2 An exhausted free grant is skipped, not overrun into paid usage. Check: a case with
  `used == allowance` asserting the skip reason and that the next free stage served.
- [ ] 4.3 `max_cost_usd` refuses before dialing, naming the estimate and the ceiling; for extract
  the estimate is documented as an upper bound. Check: two cases (search exact, extract bounded)
  asserting `ValueError` and zero network calls.
- [ ] 4.4 An impossible combination (free budget + strict chain of one balance-funded stage) is a
  caller error before any network call, ledger unchanged. Check: a case asserting the message names
  both the chain and the budget.
- [ ] 4.5 An unavailable budget value is a caller error, not a silent default. Check: a case with
  `budget="cheap"` asserting the message lists the accepted values.

- [ ] 4.6 A ceiling over a chain that includes a credit-metered stage applies the free-grant rule
  instead of converting units; the refusal names the rule. Check: a case with a spent credit grant
  asserting the skip reason, and a case with an unspent grant asserting the stage served.
- [ ] 4.7 The applied budget is the stricter of the chain's and the call's; a call cannot loosen a
  free chain. Check: a four-cell table (chain none/ceiling/free × call none/ceiling/free), asserting
  the applied budget and which stages were dialed.
- [ ] 4.8 The ceiling comparison is inclusive: an estimate exactly equal to the ceiling passes, one
  cent above refuses. Check: two cases pinning `>` and not `>=`, so the boundary cannot drift.

## 5. Accounting, force and audit

- [ ] 5.1 A custom call charges, parks and unparks through the existing ledger calls. Check: the
  default-chain accounting tests replayed with a custom chain produce identical `used`, `limit` and
  pause kinds.
- [ ] 5.2 `ignore_pause=True` allows one attempt against a parked stage, charges it, and re-parks on
  failure with the vendor's own kind. Check: a test asserting one attempt, `forced: true` in the
  audit line, and the stage still parked.
- [ ] 5.3 `log_route` records `chain_mode`, `requested`, `forced` and `budget`; a default call
  records `chain_mode: default` and no budget. Check: read the written `routes.jsonl` in a test and
  assert the fields for a default call, a custom call and a free call.
- [ ] 5.4 An extract call served by more than one stage records `delivered: {stage: pages}`. Check:
  a test with a five-page batch split 3/2 asserting the counts, and a single-vendor batch keeping a
  single entry.

## 6. Operator surface

- [ ] 6.1 `quotarouter call search "q" --stages firecrawl [--fallback] [--force] [--budget free]
  [--max-cost 0.01]` and the extract equivalent. Check:
  `quotarouter call search "test" --stages exa --json` → one attempt on `exa`,
  `chain_mode: custom` in the JSON.
- [ ] 6.2 `quotarouter route --chain <name>` resolves and prints without a network call, including
  the budget filter's verdict per stage. Check:
  `QUOTAROUTER_CHAINS="free=exa,jina" quotarouter route --chain free` → prints `exa` first and
  `jina` as unsupported-for-search, and makes no request (assert with a MockTransport test).
- [ ] 6.3 `status --json` lists the named chains, their capabilities, their descriptions, the
  funding of each stage and the override flag. Check: the JSON contains `chains`,
  `allow_chain_override` and a funding field per stage.
- [ ] 6.4 `quotarouter chains` prints what the file gave, with the path it read and any entries it
  dropped. Check: run against a file with one bad entry → the good chain is printed and the bad one
  is reported with its reason.

## 7. Protocol surface

- [ ] 7.1 `web_search` / `web_extract` accept optional `stages`, `chain`, `fallback`, `force`,
  `budget`, `max_cost_usd`, with the same names and semantics as the library. Check:
  `python -m pytest tests/test_mcp_protocol.py -q` → schema test asserts the properties are present
  and not required.
- [ ] 7.2 A bad chain or budget argument returns `isError: true`, the message is actionable
  (lists what exists), and the server answers the next request. Check: a protocol test calling
  twice in one session: first a bad chain, then `quota_status`.
- [ ] 7.3 The tool description states that a call may pin stages and that this can spend money —
  the model must be able to know before it uses it. Check: the description text contains both
  sentences; asserted in the schema test.
- [ ] 7.4 `quota_status` reports the named chains, whether overrides are allowed and each stage's
  funding. Check: the tool output contains all three.

- [ ] 7.5 The real stdio transport is exercised, not only the in-process one: a subprocess speaks
  `initialize`, a pinned search, a bad chain, `quota_status`. Check:
  `python -m pytest tests/test_mcp_stdio.py -q` (the gap left open by earlier rounds).

## 8. Evidence beyond unit tests

- [ ] 8.1 Concurrency: two threads, two chains, a barrier inside the fake stage. Check: each thread
  dialed only its own stage; `settings.search_order` is unchanged afterwards.
- [ ] 8.1b Load: eight threads, three chains, a barrier; no stage is dialed by a thread that did
  not name it, and the shared router's defaults are unchanged. Check: the same file, eight threads.
- [ ] 8.2 Live: one search pinned to `exa`, and one free-only extract. Check:
  `python -m pytest -m "live and not e2e" -q` → green, and the audit lines read `chain_mode:
  custom` / `budget: free`.
- [ ] 8.3 Full gate. Check: `scripts/preflight.sh --live` → `PREFLIGHT PASSED`, then
  `python -m pytest -m e2e -q` → 8 passed (the plugin path is untouched, and that is the point).

## 9. Documentation and release

- [ ] 9.1 `docs/chains.md` — the operator's guide: file location and precedence, the full format
  with every key, three worked examples (one vendor, free-only, fallback), how to verify a chain
  without spending anything (`route`, `chains`, `status --json`), what a refusal message means, and
  the reload rule (restart). Ships `docs/chains.example.toml` to copy. Check: every command in it
  is executed by `tests/test_docs_examples.py`, the fenced TOML blocks parse, and the example file is
  byte-identical to the one the tests load.
- [ ] 9.2 README: a "Choosing a chain per call" section with the three ways in (library, MCP, CLI),
  the strictness rule, the budget in one paragraph, and the Hermes limitation stated plainly. Check:
  the examples in it are the ones the tests execute.
- [ ] 9.3 CONTRIBUTING "The chain is data" gains the file format, the override rules and the trap
  (force spends money). Check: the section names every new environment variable and the file path.
- [ ] 9.4 `CHANGELOG.md` under `Unreleased`, with the compatibility note (new optional arguments,
  additive audit fields). Check: `git grep -n "Unreleased" CHANGELOG.md` shows the entry.
- [ ] 9.5 `hermes_plugin/plugin.yaml` version matches `quotarouter.__version__` (third place that
  carried it), and the description no longer spells out the default order. Check: a packaging test
  asserting equality; `git grep -c 'keenable ->' hermes_plugin/plugin.yaml` → 0.
- [ ] 9.6 The built sdist contains `docs/chains.md` and the example chain file. Check:
  `python -m build --sdist && tar tzf dist/*.tar.gz | grep -c 'docs/chains'` → 2 (md + example).
- [ ] 9.7 Archive the change after merge: `openspec archive custom-chain-override` and confirm
  `openspec list` no longer shows it. Check: `openspec validate --change custom-chain-override`
  before archiving, `openspec list` after.
