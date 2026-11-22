## ADDED Requirements

### Requirement: The end-to-end run goes through Hermes' public functions

The check MUST call `tools.web_tools.web_search_tool` and `tools.web_tools.web_extract_tool`,
not the router's internal classes: only that way do the plugin loader, the provider registry,
the response envelope and config reading get exercised.

#### Scenario: A tool search is served by the chain
- **WHEN** the isolated profile sets `web.search_backend: quotarouter` and vendor keys are present
- **THEN** `web_search_tool` returns JSON with `success: true` and a non-empty `data.web`
- **AND** `data.backend` starts with `quotarouter:`, meaning a chain stage answered

#### Scenario: A tool extract is served by the chain
- **WHEN** `web_extract_tool` is called for a reachable page
- **THEN** `results[0].content` is non-empty and longer than `QUOTAROUTER_MIN_PAGE_CHARS`

### Requirement: The stand is isolated from the live profile

A run MUST use its own `HERMES_HOME` and MUST NOT read or write the live profile's state.

#### Scenario: State is written into the isolated directory
- **WHEN** the run has finished a search and an extract
- **THEN** `ledger.json` and `routes.jsonl` live in `<temp>/state/quotarouter`
- **AND** the live `~/.local/state/quotarouter` is unchanged

#### Scenario: The live config is not switched
- **WHEN** the stand sets `web.search_backend: quotarouter`
- **THEN** the edit exists only in `<temp>/config.yaml`, and the live `config.yaml` matches its
  backup by hash

### Requirement: Degradation without vendors is honest

If no stage can serve the call, the tool MUST return `success: false` with a per-stage reason
and MUST NOT raise.

#### Scenario: No keys and the fallback disabled
- **WHEN** no vendor key is present and `QUOTAROUTER_KEYLESS_LAST_RESORT=false`
- **THEN** `web_search_tool` returns `success: false`
- **AND** the error text names every stage and why it was skipped

### Requirement: A run produces a machine-readable report and an exit code

Every run MUST write a JSON report listing each step, its duration and its failure reason, and
MUST exit 0 only when every step passed.

#### Scenario: A successful run
- **WHEN** all steps passed
- **THEN** the report carries `"ok": true` and one entry per step
- **AND** the exit code is 0

#### Scenario: A failed step
- **WHEN** at least one step failed its check
- **THEN** the report carries that step with `"ok": false` and a `detail`
- **AND** the exit code is not 0

### Requirement: Testing instructions live in the repository

The repository MUST contain `docs/testing.md` with the ladder of test levels: what each level
checks, what quota it spends, what counts as evidence, and the command that runs it.

#### Scenario: A newcomer finds how to run the tests
- **WHEN** a contributor opens `docs/testing.md`
- **THEN** every level states its command, its quota cost and its acceptance criterion

#### Scenario: e2e does not run by default
- **WHEN** `python3 -m pytest -q` runs
- **THEN** tests marked `live` and `e2e` are excluded
