# Tasks: e2e verification in a real Hermes

## 1. The stand

- [x] 1.1 `tests/e2e/harness.py` builds a temporary `HERMES_HOME` (config.yaml, .env,
  plugins/quotarouter with a symlink to the package). Check: the directory exists and
  `config.yaml` contains `search_backend: quotarouter`.
- [x] 1.2 A child runner executes steps 1–5 in a separate process with `HERMES_HOME=<temp>` and
  `PYTHONPATH=/usr/local/lib/hermes-agent`. Check: step 1 sees the `quotarouter` provider in
  the Hermes registry.
- [x] 1.3 An isolation step: `~/.local/state/quotarouter` is not created by the live call.
  Check: the fingerprint of the live profile is identical before and after the run.

## 2. Scenarios

- [x] 2.1 `search_tool`: `success: true`, ≥1 result, `data.backend` = `quotarouter:<name>`.
- [x] 2.2 `extract_tool`: `results[0].content` is non-empty.
- [x] 2.3 Accounting: `ledger.json` and `routes.jsonl` appear under the isolated home, and the
  log carries `served_by`.
- [x] 2.4 Degradation: without keys and with `QUOTAROUTER_KEYLESS_LAST_RESORT=false` the tool
  returns `success: false` naming the stages, with no traceback.

## 3. Documentation

- [x] 3.1 `docs/testing.md`: the ladder of levels (unit / mocked chain / recorded responses /
  live adapters / e2e), each with what it checks, what it costs, what counts as evidence and
  the command that runs it.
- [x] 3.2 README: the testing section links to `docs/testing.md` and lists the e2e commands.

## 4. Gates

- [x] 4.1 `pyproject.toml`: the `e2e` marker, and both (`live`, `e2e`) excluded from `addopts`;
  `pytest -q` stays offline and green.
- [x] 4.2 `pytest -q` → every test passes, no network call.
- [x] 4.3 `python3 -m pytest -m e2e -q` → the stand runs, `e2e-report.json` is written, exit 0.
- [x] 4.4 An e2e run on live keys: search and extract served by real vendors, report attached
  to the task and the event log.
