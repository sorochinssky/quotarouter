# Contributing

Bug reports and pull requests are welcome. This is a small package with an unusually strict
gate, so here is the short version of what "done" means.

## Before you open an issue

Run these and paste the output — most reports are answerable from them:

```bash
python -c "import quotarouter, sys; print(quotarouter.__version__, sys.version)"
quotarouter status --json          # what the ledger believes about each vendor
quotarouter route                  # who would serve the next search and extract, and why not
```

For a failed call, the line in `<state dir>/routes.jsonl` carries the reason per stage
(`exa: quota (402) -> firecrawl=ok`). Replace anything that looks like a key before posting.

## Setting up

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env        # fill in the keys you actually have; never commit it
python -m pytest -q         # offline, no network, no keys needed
```

Python 3.11, 3.12 and 3.13 are supported and tested; 3.14 works too.

## The gate

`scripts/preflight.sh` runs everything, and a green run is what a mergeable change means here.
There is no CI by choice: the gate is local. The steps, in order, are described in
[docs/development.md](docs/development.md). The short version:

```bash
python -m pytest -q                       # offline suite
python -m pytest -m "live and not e2e" -q # real vendors, needs keys, spends a fraction of a cent
python -m pytest -m e2e -q                # end-to-end inside a real Hermes install
hermes plugins validate hermes_plugin     # manifest and registration, if you touched the shim
python -m mypy --ignore-missing-imports quotarouter   # the package ships py.typed
python -m build && twine check dist/*     # packaging
```

Skip the live and e2e levels only when the keys or a Hermes install are genuinely unavailable,
and say so in the pull request.

## The chain is data

There are exactly two chains, and they are separate lists because the cheap vendor for a query is
not the cheap vendor for a page:

```
search : keenable -> exa -> tavily -> firecrawl -> deepseek
extract: jina -> keenable -> firecrawl -> tavily -> exa -> deepseek
```

Both come from the environment and are read once per process:

```bash
QUOTAROUTER_SEARCH_ORDER=exa,firecrawl,deepseek
QUOTAROUTER_EXTRACT_ORDER=jina,firecrawl,exa
```

What to know before changing one:

- **One name is a valid chain.** `QUOTAROUTER_SEARCH_ORDER=exa` means "exa or nothing": no
  failover, and the error says so. Useful for a single free account and for measuring one vendor.
- **A stage that cannot do the capability is skipped**, with the reason `does not support extract`.
  The extract chain may name a search-only vendor; it just never serves.
- **A misspelled name is reported at startup** (`... is not a stage this build has; the chain is
  shorter than it looks`) and then skipped at call time. It is a warning, not an error: a chain
  that names a vendor from a newer release must still run on an older one.
- **`keyless` is not in the chains.** The anonymous tier is a fallback that serves when nothing in
  the chain could, and it is switched by `QUOTAROUTER_ALLOW_KEYLESS` / `KEYLESS_LAST_RESORT`
  rather than by position.
- **The order is not per call.** There is no way to ask one request for a specific vendor; the
  router owns the choice so the ledger stays correct across processes. If you need a different
  chain for a workload, run it in a process with a different environment or state directory.
- **Order is not priority by quality, only by spend.** Put the free allowance you have most of
  first, and the paid balance last.

`quotarouter route` prints the decision — usable stages in order, then every skip with its reason
— so a chain question is answerable without a live call.

## Rules enforced by tests, not by review

- **Every behaviour change lands with a test that fails without it.** A bug fix starts with the
  reproducing test.
- **English only** in tracked files: code, comments, docs, commit messages, log lines. A test
  walks the repository for Cyrillic.
- **The core stays host-agnostic**: `quotarouter/` must not import `agent`, `hermes_cli`, `tools`
  or `hermes_plugins`. Host coupling belongs in `quotarouter/integrations/`.
- **No secrets** in code, docs, fixtures or fixtures' filenames. Vendors are configured through
  environment variables; a test fixture carries a balance, never a key.
- **A test may not cost money.** Anything that spends vendor credits is marked `live` and is
  excluded from the default run.

## Pull requests

- One logical change per pull request, one commit per step of it.
- Describe the failure you saw, not the diff you wrote; the diff is right there.
- New behaviour comes with a test; a fixed bug comes with the test that reproduces it.
- If the change touches the plugin shim, the provider contract or the MCP surface, run the live
  and e2e levels too (see the gate above) — the offline suite cannot see a registry change.
- A documentation change that alters an example has to keep the example executable: the gate runs
  `examples/library.py` against the installed wheel.

## Style

Match the file you are editing: short functions, comments that explain *why* (the interesting
ones in this package record a bug that was paid for), error messages that name the vendor and
the reason, and docstrings that state the contract rather than the mechanics.

Commit messages: English, imperative subject line, body explaining the why. One logical change
per commit.

## Vendors

Adding a stage means adding a file in `quotarouter/stages/`, registering it in
`quotarouter/stages/__init__.py`, and covering four things with tests on `httpx.MockTransport`:
a normal answer, an empty answer (not a success), a quota refusal, and a page-level refusal that
must not park the stage. `quotarouter/stages/base.py` documents the contract; `docs/vendor-probes.md`
records which vendors have actually been exercised against their live API and which are only
covered by hand-written fixtures. Do not move a row from the second column to the first without
a probe output to point at.
