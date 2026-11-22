# Development and the pre-push gate

Instructions for an agent (or a human) changing this repository. One command runs everything
below: `scripts/preflight.sh`. There is deliberately **no CI** — the gate is local and must be
run before every push; a green local gate is what "done" means here.

## The gate, in order

1. `python3 -m pytest -q` — offline suite. No network, no real keys. Must be green before
   anything else.
2. `python3 -m pytest -m "live and not e2e" -q` — live vendors. Needs real keys in the
   environment; skip only if keys are genuinely unavailable, and say so in the commit message.
3. `python3 -m pytest -m e2e -q` — end-to-end through a real Hermes install
   (`tests/e2e/harness.py` builds a throwaway `HERMES_HOME`).
4. `hermes plugins validate hermes_plugin` — catalog-admission gate. **Must say "Validation
   passed"**: it catches manifest drift, missing registration, and the `symlink_escape` that a
   convenience symlink inside the plugin directory causes.
5. `python -m build && twine check dist/*` — packaging. A wheel that does not build is not
   publishable, no matter how green the tests are.
6. `quotarouter status` from the built wheel in a clean venv — proves the package works without
   the source tree (the plugin shim does not excuse a broken wheel).

## Invariants enforced by tests, not by review

- **No Cyrillic in tracked files.** Publication artifacts (README, docs, OpenSpec, docstrings,
  commit messages, log lines) are English. `tests/test_packaging.py` walks every tracked file.
- **The core does not import the host.** `quotarouter/` must not import `agent`, `hermes_cli`,
  `tools`, or `hermes_plugins`; host coupling belongs in `quotarouter/integrations/`. Enforced
  by an AST check and by importing the core with those roots blocked.
- **Secrets never appear** in code, docs, fixtures or logs. Vendors are configured through
  `QUOTAROUTER_*` / `ROUTER_*` environment variables; a probe result may contain a balance, not
  a key.
- **Alerts never break a call.** A transport failure is logged and swallowed; the search must
  still return.
- **A degraded call is never silent.** When nothing serves, or the keyless tier serves, or a
  vendor parks itself out of quota, `alerts.notify` fires (subject to the cooldown).

## Rules of the road

- Commit messages: English, imperative mood, one subject line, body explains *why*.
- Every behaviour change lands with a test that fails without it. A bug fix starts with the
  reproducing test.
- Prefer the offline suite for iteration; run live/e2e before pushing, not before every edit.
- When adding a vendor stage: a new module in `quotarouter/stages/`, register it in
  `stages/__init__.py`, add its default position in `config.py` orders, add a fixture from a
  real response in `tests/fixtures/`, and extend `docs/vendor-probes.md` with how to read its
  balance.
- Do not raise the keyless tier's priority or enable it by default — it is a last resort and it
  hides vendor failures.
- Plugin manifest changes are picked up only after `hermes plugins disable/enable <name>`;
  `web.*_backend` changes apply without a restart.

## Scheduling and operations

Nothing is installed by this repository on its own: `quotarouter schedule` **prints** the cron
line and systemd units, and enabling them is the operator's decision. The same is true of
alerts — `QUOTAROUTER_ALERT_TELEGRAM/WEBHOOK/COMMAND` are empty by default and nothing is sent.
