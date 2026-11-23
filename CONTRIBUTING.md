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
python -m build && twine check dist/*     # packaging
```

Skip the live and e2e levels only when the keys or a Hermes install are genuinely unavailable,
and say so in the pull request.

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
