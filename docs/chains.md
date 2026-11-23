# Named chains

A chain is the ordered list of providers a call may use. This package has a default chain per
capability, and it can also have named chains you declare in a file. A caller then names one for a
single call, or pins the providers explicitly, without touching the default.

Two things about the shape matter:

- **A chain is a pair of lists.** A search and a page are priced differently by the same vendor, so
  `search` and `extract` are declared separately. A chain may declare only one of them.
- **A chain is a promise.** If a chain declares `budget = "free"`, a caller cannot spend money
  through it. The strictest of the chain's budget and the call's budget applies.

The default chain is configuration too, but it is not in this file: it comes from
`QUOTAROUTER_SEARCH_ORDER` and `QUOTAROUTER_EXTRACT_ORDER`. Run `quotarouter status` to see the
orders in force.

## Where the file lives

The default location is `~/.config/quotarouter/chains.toml`. Two settings change that:

- `QUOTAROUTER_CHAINS_FILE` — read this file instead.
- `QUOTAROUTER_CHAINS` — a one-line shorthand, for a throwaway setup:
  `QUOTAROUTER_CHAINS="fast=exa,firecrawl"`. It applies one list to both capabilities, and when it
  is set it **replaces the file** entirely. There is no merging and no precedence to reason about.

The file is read once, when the process starts. After editing it, restart the process (or the
gateway) — a running process keeps the chains it read.

## The format

The file is TOML. Every chain is a `[chains.<name>]` table.

| Key | Type | Meaning |
| --- | --- | --- |
| `search` | list of stage names | Providers this chain may use for a search |
| `extract` | list of stage names | Providers this chain may use for a page |
| `description` | string | For humans; shown by `quotarouter chains` |
| `budget` | `"free"` | This chain never spends money |
| `max_cost_usd` | number | Ceiling for one call; a call above it is refused before dialing |
| `strict` | boolean, default `true` | `false` lets the chain continue into the default order |

A chain must declare at least one of `search` and `extract`. Stage names are the ones this build
has: `deepseek`, `exa`, `firecrawl`, `jina`, `keenable`, `keyless`, `tavily`.

A bad entry is dropped and reported; the other entries still load. A configuration typo is not an
outage.

## Three examples

One vendor, no tail. Any failure is the answer:

```toml
[chains.firecrawl-only]
description = "Reproduce one vendor's answer"
search = ["firecrawl"]
extract = ["firecrawl"]
```

Nothing that can cost money. The anonymous tier is not named here, so a strict chain of one
provider stays one provider:

```toml
[chains.free]
description = "Free allowances only"
search = ["keenable", "exa", "firecrawl"]
extract = ["jina", "firecrawl"]
budget = "free"
```

A preference, not a wall: try these first, then behave normally:

```toml
[chains.prefer-jina]
description = "Jina first, the default order after it"
extract = ["jina"]
strict = false
```

## How to check a chain without spending anything

```bash
quotarouter chains                    # what the file gave, and what it dropped
quotarouter route --chain free        # which stages this chain would dial, per capability
quotarouter route --stages firecrawl  # the same for an explicit list
quotarouter route --budget free       # the default order, restricted to free allowances
```

None of these commands dials a provider. They answer the question before you pay for it.

One real call, if you want one:

```bash
quotarouter call search "quotarouter" --chain free --json
quotarouter call extract https://example.com/ --stages jina
```

## Reading a refusal

A refusal means the request was wrong, not that a provider failed. The message names the argument:

- `stages names 'fircrawl', which is not a stage this build has (deepseek, exa, ...)` — a typo. The
  message lists the real names.
- `chain 'fast' declares no extract stages (it has: search)` — the chain exists, but not for this
  capability. It will not fall back to the default; that is what `strict = false` is for.
- `stages and chain are mutually exclusive` — pass one or the other.
- `cannot be combined with budget=free: every stage in it is funded by a prepaid balance` — the
  request contradicts itself. `deepseek` spends money from a balance; a free call cannot use it.
- `per-call chains are disabled in this deployment (QUOTAROUTER_ALLOW_CHAIN_OVERRIDE=0)` — the
  operator turned the feature off for this host.

## What a budget does, and what it does not

`budget = "free"` restricts the call to providers that cost nothing: unmetered ones (Jina, the
anonymous tier) and those still inside their free monthly allowance. A provider funded by a prepaid
balance is never eligible, and a free allowance that is spent is skipped rather than overrun into
paid usage.

`max_cost_usd` compares the estimate for one call with the ceiling and refuses before dialing.
Vendors do not count the same unit — some spend dollars, some spend credits — so a dollar ceiling
cannot price a credit-metered provider without inventing an exchange rate. That provider is held to
the free-grant rule instead: usable inside its allowance, skipped once it is spent.

A budget bounds **one call**. It does not set research depth: depth is how many calls you make, how
many results you ask for, and how many pages you extract. Those are the caller's decisions.

## Troubleshooting

- **`quotarouter chains` says `source: none`** — no file at the path it printed. Create it, or point
  `QUOTAROUTER_CHAINS_FILE` at it.
- **`source: shorthand`** — `QUOTAROUTER_CHAINS` is set and the file is ignored on purpose.
- **A name is missing from the list** — read the `dropped` lines: the entry is named with its reason.
- **A call still uses the default order** — the caller did not name a chain, or
  `QUOTAROUTER_ALLOW_CHAIN_OVERRIDE=0` refused it loudly.
