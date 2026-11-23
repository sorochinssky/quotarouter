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

### D6. Named chains, parsed once

Configuration:

```
QUOTAROUTER_CHAINS="fast=exa,firecrawl;free=jina,exa;single=firecrawl"
```

Parsed by `Settings` into an immutable mapping. A malformed segment (no `=`, empty name, empty
list) is dropped with a warning naming the segment: a configuration typo must not stop a service.
An unknown *name* passed at call time is an error listing the names that exist — the caller can be
told; a config file cannot.

### D7. The operator's kill switch refuses, it does not substitute

`QUOTAROUTER_ALLOW_CHAIN_OVERRIDE=0` makes an override request fail with a message naming the
setting. Silently serving the default order would make a client believe it got what it asked for.

## Resolution

```
resolve(capability, stages=None, chain=None) -> ResolvedChain

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
  return ResolvedChain(mode, names, fallback_names, allow_keyless=(mode == "default" or fallback))
```

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
- **Audit growth.** Three short fields per line; rotation already bounds the file.

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
