# Design

## Context

Today the chain is a property of the process: `Settings.search_order` and `Settings.extract_order`
are read from the environment once, and `Router.candidates()` walks them. Two chains exist because
the cheap vendor for a query is not the cheap vendor for a page. Everything the router knows about
a vendor — key, capability, balance, pause, spend — already lives in one ledger, shared by the CLI,
the MCP server and the Hermes gateway on the same machine.

The missing piece is not accounting. It is the ability to say "this call, these stages" without
losing the accounting that makes the router worth using.

## Goals

- One call may name its own chain, down to a single vendor, without touching the process
  configuration.
- The default chain is unaffected by any custom call, and concurrent callers do not interfere.
- A custom call is charged, parked and audited exactly like a default call.
- A deployment can refuse overrides entirely.
- Zero behaviour change when no override is requested.

## Non-goals

Per-stage options, weighted or adaptive selection, cost budgets per call, and a per-call override
inside Hermes' tool schema. See "Out of scope" in the proposal.

## Decisions

### D1. A keyword argument on the existing entry points, not a new method

`run_search()` / `run_extract()` gain `stages`, `chain`, `fallback`, `ignore_pause`. Alternatives
considered:

- **Separate methods** (`run_search_with(chain, …)`): readable at the call site, but duplicates the
  walk, and the two copies drift — the walk is the part with the pause, charge and audit rules.
- **A `Chain` object** (`Chain([...])`, `Chain.named("free")`): the most extensible, and the right
  shape once per-stage options exist. Today it would add a public type with four fields and no
  behaviour, and a test suite for a data holder.

Chosen: keyword arguments now; a `Chain` object can be introduced later as an accepted type for
the same argument without breaking callers, because a union type widens.

### D2. Strict by default

`stages=["firecrawl"]` means firecrawl or an honest failure. The alternative — silently continuing
into the default order — makes "one provider" untrue and hides a vendor problem behind a
successful-looking call. Callers who want "prefer this, then normal" pass `fallback=True`, which
continues with the default order after the custom stages, skipping duplicates, and restores the
end-of-chain policy (including the anonymous tier) because that is what "normal" means.

### D3. Both a list and a name is an error

Passing `stages=` and `chain=` together has no defensible meaning. Resolving it by precedence
teaches callers a rule they will forget; refusing it costs one test and zero support questions.

### D4. Invalid chains are caller errors, raised before the first network call

An unknown stage, a capability mismatch, an empty list, an unknown name: all are `ValueError`s
raised during resolution. They are not `StageError`s: the vendor did nothing wrong and the ledger
must not move. The MCP layer maps `ValueError` to a tool result with `isError: true`, so a model
that passes nonsense gets an explanation and the server stays up.

Note the deliberate asymmetry with the environment order, where a typo only warns and is skipped:
configuration is written once and must not be able to break a running service, while a call-time
argument is written per call and must not silently mean something else.

### D5. Force is a diagnostic, not a bypass

`ignore_pause=True` allows one attempt against a paused stage. It exists for "is this vendor back
yet?" and for reproducing a failure. It does not skip credential checks, does not skip capability
checks, does not clear another stage's pause, and does not touch the ledger differently: the call is
charged, and a failure re-parks the stage with the vendor's own reason. The audit line marks it
`forced: true` so a later question — "why did this call touch a parked vendor?" — has an answer.

Force never applies to the whole chain implicitly: it must be asked for per call, and it is the
caller's explicit statement that spending money now is acceptable.

### D6. Named chains are a file, not a one-line variable

A chain is not one list. It is a pair — the search stages and the extract stages — plus what it is
for and what it may spend. `QUOTAROUTER_CHAINS="fast=exa,jina"` cannot express that: it would have
to be applied to both capabilities and then silently skip `jina` on search, which is exactly the
kind of quiet misconfiguration this package keeps fixing. It also cannot carry comments, and a
production configuration needs comments.

So the file is the mechanism, and it uses TOML because Python 3.11 has a parser in the standard
library — a configuration format should not be the reason to add a dependency:

```toml
# ~/.config/quotarouter/chains.toml

[chains.fast]
description = "Search only; never fetch pages"
search = ["exa", "firecrawl"]

[chains.free]
description = "Nothing that can cost money"
search = ["keenable", "exa", "firecrawl"]
extract = ["jina", "firecrawl"]
budget = "free"

[chains.single-vendor]
description = "For reproducing one vendor's answer"
search = ["firecrawl"]
extract = ["firecrawl"]
strict = true
```

- Location: `QUOTAROUTER_CHAINS_FILE` if set, else `~/.config/quotarouter/chains.toml`, else
  nothing (only the defaults and the shorthand exist).
- A chain that declares only `search` refuses extract calls by name — "chain 'fast' has no extract
  stages" — rather than falling back to the default. Silent substitution is the failure mode this
  design exists to remove.
- A malformed entry (unknown key, a stage name that does not exist, an empty list, a bad `budget`)
  is reported and dropped; the rest of the file still loads. A configuration typo must not stop a
  service.
- The single-line `QUOTAROUTER_CHAINS` shorthand stays for a throwaway setup and applies one list
  to both capabilities; when set, it replaces the file entirely (one rule, not a merge), with a
  warning that the file was ignored. Precedence by "explicit environment wins" matches the
  existing `QUOTAROUTER_*` / `ROUTER_*` rule.
- Read once, at settings construction. Editing the file needs a restart, which is what every other
  setting here does; a hot reload would need a file watcher and a story about half-written files.

An unknown *name* passed at call time is an error listing the names that exist — the caller can be
told; a config file cannot.

### D7. The operator's kill switch refuses, it does not substitute

`QUOTAROUTER_ALLOW_CHAIN_OVERRIDE=0` makes an override request fail with a message naming the
setting. Silently serving the default order would make a client believe it got what it asked for.

### D8. Budget is a ceiling, not a depth knob

A budget per call has three honest jobs, and one it cannot do:

- **`budget="free"`** — the call may not spend money. A stage is eligible if it is unmetered
  (`jina`, the anonymous tier) or if its current period consumption is still inside its declared
  free grant (`exa`, `firecrawl`, `tavily`, `keenable`). A balance-funded stage (`deepseek`) is
  never eligible, and a free grant that is spent is not overrun into paid usage.
- **`max_cost_usd`** — refuse the call when the estimate for what it asks (results wanted, pages
  requested) exceeds the ceiling. The estimate uses the stage's own declared unit cost, and the
  refusal names both numbers. Cheap, exact for search, an upper bound for extract.
- **Selection pressure** — with a ceiling, the router prefers stages that cost less, which is
  already the default order's intent.

What a budget cannot do: **set research depth**. Depth is how much material the caller gathers —
how many calls it makes, how many results per call, how many pages it extracts, whether it
decomposes the question. Those decisions live in the agent's loop, one level above any single
call. A larger per-call budget buys a wider page of results and more pages, which is *breadth per
call*, not depth of investigation. Concretely: with free allowances unspent, four agents can make
twenty calls for nothing, and a $2 ceiling on one call changes nothing about that.

So the router refuses to guess. A caller that wants a deeper investigation makes more calls; the
ledger's job is to say what each one cost and to stop a single call from being expensive by
accident.

To make "free" a fact rather than an inference, each stage declares how it is funded:
`free` (unmetered or a free monthly grant) or `balance` (prepaid money). The ledger already knows
the consumption; the declaration says what that consumption means.

### D9. A mixed extract call must say which stage delivered which page

`run_extract` already walks pages across stages: whatever a stage did not deliver moves on. With
selective chains this stops being an edge case — `fallback=True` on a five-URL extract can
legitimately end with three pages from Jina and two from Firecrawl — and today the audit line
carries one `served_by` for the batch, which would then be a half-truth.

The record therefore gains a per-stage count, `delivered: {"jina": 3, "firecrawl": 2}`, and the
payload entry keeps the stage that produced it where the vendor echoes no provenance. A count is
enough to answer the accounting question without turning the log into a per-page database.

### D10. "Has room" is not a reservation, and this change does not pretend otherwise

Two concurrent calls can both see room for the last credit and both spend it. That race exists
today; selective chains make it likelier, because pinning one vendor concentrates calls on one
counter. The fix would be a reservation (charge, then reconcile), which changes the ledger's
contract and every test around it. This change does not do it: it records the overshoot in the
audit line (`spent` per attempt is real, and the vendor's own probe corrects the balance on the
next call) and leaves the reservation for the time the counter is ever observed to matter. Saying
this here is the point — an unstated race becomes a mystery later.

## Resolution

```
resolve(capability, stages=None, chain=None, budget=None, max_cost_usd=None) -> ResolvedChain

  if stages is not None and chain is not None: refuse "pass one, not both"
  if not allow_chain_override and (stages or chain): refuse naming the setting

  if stages:  names = stages;                mode = "custom"
  elif chain: names = settings.chains[chain]; mode = "named"   (unknown name -> refuse)
  else:       names = default_order(capability); mode = "default"

  if not names: refuse "empty chain"
  for name in names:
     if name not in registry: refuse, listing the registry
     if not capability_supported(name): refuse
  dedupe while keeping the first occurrence

  fallback_names = default_order(capability) if (fallback and mode != "default") else ()
  return ResolvedChain(mode, names, fallback_names, allow_keyless=(mode == "default" or fallback),
                       budget=budget or chain_budget, max_cost_usd=max_cost_usd)
```

A budget filter is applied during selection, not resolution: a stage excluded by `budget="free"` is
skipped with the reason `costs money (balance-funded)` or `free grant spent`, so the refusal text
still explains every stage. The anonymous tier counts as free and may serve a `budget="free"` call
that a strict chain would have refused — the budget says "no money", not "one vendor".

The walk then works on `ResolvedChain.names + ResolvedChain.fallback_names`, and the anonymous tier
is consulted only when `allow_keyless` is true and nothing in the chain served.

`ignore_pause` is passed to selection, not to resolution: it only changes whether a paused stage is
dropped from the candidate list, and each attempt it allows is recorded as forced.

## Accounting and audit

Nothing in the charge path changes. `routes.jsonl` records:

```
{"ts": …, "capability": "search", "served_by": "firecrawl", "ok": true,
 "chain_mode": "custom", "requested": ["firecrawl"], "forced": false, "chain": [...]}
```

Readers that predate the fields ignore them. `quotarouter status --json` adds the named chains and
the override flag, so a caller can discover what it may ask for.

## Surfaces

- Library: `router.run_search("q", 5, stages=["firecrawl"])`,
  `router.run_search("q", 5, chain="free", fallback=True)`.
- MCP: `web_search {query, limit, stages?, chain?, fallback?, force?}` — optional properties, so
  every existing client keeps working; the tool description states that an override is honoured
  only when the server allows it.
- CLI: `quotarouter call search "q" --stages firecrawl --no-fallback`,
  `quotarouter route --chain free` (resolve and print, no network).
- Hermes: unchanged. `web_search(query, limit)` has no room for a chain, and inventing a hidden
  argument would tie this package to one host's tool schema. In Hermes the lever stays
  `QUOTAROUTER_SEARCH_ORDER` per process or per profile.

## Risks

- **A model pins an expensive vendor.** Mitigation: server-side kill switch, refusal instead of
  substitution, and the audit field. The vendor's own key and quota remain the last guard.
- **Strict chains turn a degraded day into a failed job.** That is the point of strictness, and it
  is opt-in per call; the default path is untouched.
- **Two ways to describe a chain (list and name) drift.** Both resolve through one function and one
  test table, and the name form is only a lookup.
- **Audit growth.** A few short fields per line, plus a count per delivering stage on extract
  lines; rotation already bounds the file.
- **A single-vendor chain concentrates load on one rate limit.** The default order spreads calls
  across vendors; pinning one means its per-minute limit is the whole budget, and `429` becomes a
  caller-visible failure with no failover. That is what was asked for, and the refusal text says
  which stage refused and why, so the caller can decide to fall back.
- **A strict chain has no second opinion on a blocked page.** With `fallback=False`, an extract
  call that hits a domain blocked for that vendor has nowhere else to go — today the next stage
  covers it.
- **Timeouts no longer compose.** A default call succeeds if any vendor answers inside its timeout;
  a strict call waits for the one vendor and fails at that timeout. The per-call deadline, not a
  per-stage one, becomes the caller's budget of patience.
- **Chain names are an interface.** Renaming a chain in the file breaks every caller that used the
  old name. Names therefore stay stable, and a missing name is refused with the list of what exists
  rather than a "did you mean" guess.
- **Read once means a restart.** An operator who edits the file and sees no effect will ask why;
  the guide must say so in the same paragraph as the format.
- **The file is configuration, not a secret store.** It names stages and budgets; keys stay in the
  environment file. Nothing in it may be used to build a path or a command.
- **No cache between calls.** The same query on two chains costs twice; the ledger makes that
  visible but nothing dedupes it. A cache is a separate change with its own freshness story.

## Evidence

What counts as proof for each part of the work — the gate is offline tests plus one live check:

- **Resolution rules** — a table-driven test per row of the decision above, asserting both the
  chosen stages and the refusal message.
- **Strictness** — a fake stage that raises; assert the call fails and that the *other* stages'
  call counters stay at zero.
- **Accounting identity** — the same fake-vendor fixtures used for the default chain, replayed
  through a custom chain; assert identical ledger deltas and pause kinds.
- **Audit** — read the produced `routes.jsonl` line and assert the three new fields.
- **Isolation and concurrency** — two threads, two chains, a barrier inside the fake stage; assert
  each thread dialed only its own stage and the settings object is unchanged afterwards.
- **Protocol** — an in-process MCP test: bad chain argument returns `isError`, the next request
  still answers.
- **Live** — one call pinned to one vendor that has a key (`exa`), asserting the audit line says
  `chain_mode: custom`; this is the only live evidence, and it costs one search.
- **Operator surface** — `quotarouter route --chain free` prints the same stages the call would
  dial, checked by the test that runs the call.
- **Configuration** — the file is parsed from a temporary path in a test: one good chain, one entry
  per malformed form, precedence against the shorthand. The guide's commands are executed by a test,
  so documentation cannot drift from behaviour.
- **Budget** — a fake stage per funding kind; assert that a balance-funded stage's call counter is
  zero under `budget="free"`, that a spent free grant is skipped rather than overrun, and that a
  ceiling refuses with the estimate before any network call.
- **Per-page attribution** — a five-page batch split 3/2 between two fake stages; assert the
  `delivered` counts in the audit line.
