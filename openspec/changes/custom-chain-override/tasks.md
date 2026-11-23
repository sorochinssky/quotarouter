# Tasks

Order matters: configuration and resolution first, because every later step is a caller of it.
Each task carries the command that proves it, and the result that counts as proof.

## 1. Configuration surface

- [ ] 1.1 Parse `QUOTAROUTER_CHAINS` into an immutable name → tuple mapping in `Settings`; a
  malformed segment is dropped with a warning naming the segment. Check:
  `python -m pytest tests/test_chain_config.py -q` → green, including a new case per malformed form
  (`no-equals`, `name=`, `=stage`).
- [ ] 1.2 Add `QUOTAROUTER_ALLOW_CHAIN_OVERRIDE` (default on) to `Settings`. Check:
  `python -c "from quotarouter import config; print(config.Settings().allow_chain_override)"` → `True`.

## 2. Resolution

- [ ] 2.1 Add `resolve_chain(capability, stages=None, chain=None, fallback=False)` returning mode,
  names and whether the anonymous tier is allowed, with the refusals from the design. Check:
  `python -m pytest tests/test_chain_resolution.py -q` → one passing case per row of the decision
  table, each asserting the chosen names *and* the refusal text.
- [ ] 2.2 Refuse `stages` together with `chain`, an empty list, an unknown name (listing what
  exists), and a capability mismatch. Check: the same file, four cases asserting `ValueError` with
  the named argument in the message.
- [ ] 2.3 Refuse any override when the kill switch is off, naming the setting. Check: a case
  asserting the message contains `QUOTAROUTER_ALLOW_CHAIN_OVERRIDE` and that no stage was dialed.

## 3. The walk

- [ ] 3.1 `Router.run_search` / `run_extract` accept `stages`, `chain`, `fallback`, `ignore_pause`
  and walk the resolved chain; `candidates()` takes the resolved chain instead of reading
  `settings.*_order` directly. Check: `python -m pytest -q` → the existing 296 tests pass unchanged,
  which is the no-regression proof.
- [ ] 3.2 A strict chain never consults the anonymous tier. Check: a test with every named stage
  refusing, the anonymous tier enabled globally, asserting the failure and that the keyless stage's
  call counter is zero.
- [ ] 3.3 `fallback=True` continues into the default order, skipping names already tried, and
  restores the end-of-chain policy. Check: a test asserting the exact attempt order with no
  duplicate stage names.

## 4. Accounting, force and audit

- [ ] 4.1 A custom call charges, parks and unparks through the existing ledger calls. Check: the
  default-chain accounting tests replayed with a custom chain produce identical `used`, `limit` and
  pause kinds.
- [ ] 4.2 `ignore_pause=True` allows one attempt against a parked stage, charges it, and re-parks on
  failure with the vendor's own kind. Check: a test asserting one attempt, `forced: true` in the
  audit line, and the stage still parked.
- [ ] 4.3 `log_route` records `chain_mode`, `requested` and `forced`; a default call records
  `chain_mode: default`. Check: read the written `routes.jsonl` in a test and assert the three
  fields for both a default and a custom call.

## 5. Operator surface

- [ ] 5.1 `quotarouter call search "q" --stages firecrawl [--fallback] [--force]` and the extract
  equivalent. Check: `quotarouter call search "test" --stages exa --json` → one attempt on `exa`,
  `chain_mode: custom` in the JSON.
- [ ] 5.2 `quotarouter route --chain <name>` resolves and prints without a network call. Check:
  `QUOTAROUTER_CHAINS="free=exa,jina" quotarouter route --chain free` → prints `exa` first and
  `jina` as unsupported-for-search, and makes no request (assert with a MockTransport test).
- [ ] 5.3 `status --json` lists the named chains and the override flag. Check: the JSON contains a
  `chains` object and `allow_chain_override`.

## 6. Protocol surface

- [ ] 6.1 `web_search` / `web_extract` accept optional `stages`, `chain`, `fallback`, `force`, with
  the same names and semantics as the library. Check:
  `python -m pytest tests/test_mcp_protocol.py -q` → schema test asserts the properties are present
  and not required.
- [ ] 6.2 A bad chain argument returns `isError: true` and the server answers the next request.
  Check: a protocol test calling twice in one session: first a bad chain, then `quota_status`.
- [ ] 6.3 `quota_status` reports the named chains and whether overrides are allowed. Check: the tool
  output contains both.

## 7. Evidence beyond unit tests

- [ ] 7.1 Concurrency: two threads, two chains, a barrier inside the fake stage. Check: each thread
  dialed only its own stage; `settings.search_order` is unchanged afterwards.
- [ ] 7.2 Live: one search pinned to `exa`. Check:
  `python -m pytest -m "live and not e2e" -q` → green, and the audit line for that call reads
  `chain_mode: custom`, `requested: ["exa"]`.
- [ ] 7.3 Full gate. Check: `scripts/preflight.sh --live` → `PREFLIGHT PASSED`, then
  `python -m pytest -m e2e -q` → 8 passed (the plugin path is untouched, and that is the point).

## 8. Documentation and release

- [ ] 8.1 README: a "Choosing a chain per call" section with the three ways in (library, MCP, CLI),
  the strictness rule, and the Hermes limitation stated plainly. Check: the examples in it are the
  ones the tests execute.
- [ ] 8.2 CONTRIBUTING "The chain is data" gains the override rules and the trap (force spends
  money). Check: the section names every new environment variable.
- [ ] 8.3 `CHANGELOG.md` under `Unreleased`, with the compatibility note (new optional arguments,
  additive audit fields). Check: `git grep -n "Unreleased" CHANGELOG.md` shows the entry.
- [ ] 8.4 Archive the change after merge: `openspec archive custom-chain-override` and confirm
  `openspec list` no longer shows it. Check: `openspec validate --change custom-chain-override`
  before archiving, `openspec list` after.
