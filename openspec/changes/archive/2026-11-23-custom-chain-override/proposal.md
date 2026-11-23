# Proposal

## Why

The chain is chosen once per process, from the environment. Every caller in that process shares
it: a research job that needs one vendor's coverage, a support case that must reproduce a result
with a known vendor, an evaluation that compares two vendors on the same query, and a workload
that must not touch the shared credit pool all have the same two options — restart a process with
a different `QUOTAROUTER_SEARCH_ORDER`, or accept the default order. The router already knows how
to account for any stage; what is missing is the ability to say "this call, these stages".

## What Changes

- `Router.run_search()` and `Router.run_extract()` accept three new optional arguments:
  `stages=[...]` (an explicit, ordered list of stage names), `chain="name"` (a named chain from
  configuration), `fallback=True` (continue into the default order once the custom stages are
  exhausted), and `ignore_pause=True` (a deliberate probe of a parked stage).
- **Named chains live in a configuration file**, `~/.config/quotarouter/chains.toml`, because a
  chain is a pair (search stages, extract stages) with a description and a budget — that is a data
  file, not a one-line variable. `QUOTAROUTER_CHAINS_FILE` points elsewhere; a single-line
  `QUOTAROUTER_CHAINS="name=stage,stage"` shorthand remains for throwaway setups and, when set,
  wins over the file. A configuration guide (`docs/chains.md`) ships with the change: format,
  examples, precedence, how to verify a chain without calling it, and how to debug a refusal.
- **At most one source per call**: an explicit list and a name are mutually exclusive (an
  ambiguous request is refused instead of silently resolved), and either beats the default order.
  Passing neither keeps today's behaviour byte for byte.
- **A custom chain is strict by default**: it does not fall back to the default order, and it does
  not use the anonymous (keyless) tier. "Only this vendor" means only that vendor, or an honest
  failure.
- **Validation fails fast**: an unknown stage name, a stage that cannot do the requested
  capability, or an empty list raises `ValueError` at the call site. This is programmer input, not
  vendor data. The MCP server turns it into `isError` and stays alive.
- **Accounting is unchanged**: a custom call goes through the same ledger, the same pauses and the
  same charging. `routes.jsonl` gains `chain_mode` (`default` / `custom` / `named`), `requested`
  (the names asked for) and `forced`, so a later accounting question has an answer.
- **CLI**: `quotarouter call search "query" --stages firecrawl [--fallback] [--force]` and
  `quotarouter route --chain free` (show a resolved chain without calling it).
- **MCP**: `web_search` / `web_extract` accept optional `stages`, `chain`, `fallback`, `force`;
  `quota_status` lists the named chains the server knows.
- **A per-call budget**: `budget="free"` restricts the call to stages that cost no money —
  unmetered ones and those still inside their free monthly grant — and `max_cost_usd` refuses a
  call whose estimated spend would exceed the ceiling, before any vendor is dialed. A budget is a
  ceiling on one call, not a depth setting; the design says what it does and does not buy.
- **Kill switch**: `QUOTAROUTER_ALLOW_CHAIN_OVERRIDE=0` makes the server ignore per-call overrides
  entirely and serve from the default order, so a model in a foreign client cannot pin an
  expensive vendor.
- No **BREAKING** changes: new keyword arguments default to today's behaviour, the ledger schema
  change is additive, and the Hermes integration is untouched.

## Capabilities

### New Capabilities

- `chain-selection`: how the router decides which stages may serve one call — the default order,
  named chains from a configuration file, per-call overrides, their precedence, their validation,
  and what a custom call records.
- `call-budget`: what a single call is allowed to spend — the free-only mode, the cost ceiling,
  what "free" means per vendor, and the refusal that happens before a vendor is dialed.

### Modified Capabilities

None. The existing `provider-agnostic-core`, `ops-surface` and `end-to-end-verification`
requirements stay as they are; the audit trail keeps its rotation rules, and the core still
imports no host.

## Impact

- `quotarouter/routing.py` — candidate selection and the walk take a resolved chain instead of
  reading `settings.search_order` / `settings.extract_order` directly.
- `quotarouter/config.py` — `chains` (parsed, immutable), `allow_chain_override`.
- `quotarouter/cli.py` — `call` and `route --chain`; `status --json` reports named chains and the
  budget modes a caller may ask for.
- `quotarouter/integrations/mcp.py` — tool schemas gain optional properties; argument validation
  maps `ValueError` to `isError`.
- `quotarouter/integrations/hermes.py` — unchanged behaviour, but the provider passes no override:
  Hermes' `web_search` signature is `(query, limit)`, so in Hermes the lever stays configuration.
- Documentation: `docs/chains.md` (the operator's configuration guide), README section on chains,
  MCP tool description, `CHANGELOG.md`.
- Tests: a new offline file for the resolution rules plus additions to the MCP protocol tests.

## Out of scope

These stay with the operator and are deliberately not built here:

- **Which named chains exist in production.** The mechanism ships; the names, and which vendor
  each one means, are configuration.
- **Whether the override is enabled on a given host.** The flag exists; the policy is the
  operator's.
- **Per-stage options** (a different timeout, model or language per vendor inside one call).
- **Weighted, adaptive or latency-based selection.** Adding them before there is a chain selector
  to hang them on would fix a design that has not been measured.
- **A per-call override inside Hermes tools.** That needs a change on the Hermes side of the
  contract, not here.
- **Research depth as a router feature.** Depth is a property of the caller's loop — how many
  calls it makes, how many results it asks for, how many pages it fetches — not of one call's
  budget. The budget bounds cost; choosing depth stays with the agent.
- **A spend ledger per budget owner.** There is one ledger per state directory; separating budgets
  per tenant or per project is a deployment question. Per-page attribution inside one extract call
  IS in scope, because a mixed batch is normal once chains are selective.
