## ADDED Requirements

### Requirement: The core imports no host framework

Modules under `quotarouter/` (outside `integrations/`) MUST NOT import `hermes_cli`, `agent`
or `tools`, so the routing logic is usable as a plain library.

#### Scenario: Importing the core alone
- **WHEN** a process runs `import quotarouter` with no Hermes installed
- **THEN** the import succeeds and `quotarouter.Router` is available

#### Scenario: The rule is enforced, not documented
- **WHEN** a core module starts importing a Hermes module
- **THEN** `test_core_imports_no_host_framework` fails

### Requirement: Hosts are attached as integrations

Each host MUST live in its own module under `quotarouter/integrations/` and MUST be imported
lazily, so one host's requirements never leak into another's.

#### Scenario: Hermes gets one backend
- **WHEN** the plugin is enabled and `web.search_backend: quotarouter`
- **THEN** `get_provider("quotarouter")` returns an object and tool results carry
  `backend: quotarouter:<stage>`

#### Scenario: Any MCP client gets three tools
- **WHEN** an MCP client sends `tools/list`
- **THEN** the answer lists `web_search`, `web_extract` and `quota_status`
- **AND** `tools/call` for `web_search` without a `query` reports an error instead of crashing

### Requirement: The rename keeps existing deployments working

Configuration MUST read the `QUOTAROUTER_*` variables first and fall back to `ROUTER_*`, so a
deployment does not have to restart in step with the rename.

#### Scenario: Only the legacy prefix is set
- **WHEN** `ROUTER_SEARCH_ORDER=exa,firecrawl` is set and no `QUOTAROUTER_*` variable is
- **THEN** the search chain is `exa, firecrawl`

#### Scenario: Both prefixes are set
- **WHEN** both `ROUTER_SEARCH_ORDER` and `QUOTAROUTER_SEARCH_ORDER` are set
- **THEN** the `QUOTAROUTER_*` value wins

### Requirement: Publication artifacts are in English and carry no secrets

The repository MUST be readable by someone outside this installation: English README, docs and
OpenSpec artifacts, an OSI license, project metadata, and no credential values in any tracked
file.

#### Scenario: A tracked file is scanned for secrets
- **WHEN** the tracked tree is searched for vendor key values
- **THEN** no file matches; only variable names appear

#### Scenario: The package installs and runs
- **WHEN** `pip install -e .` completes in a clean environment
- **THEN** `quotarouter status` runs and reports vendor state without needing Hermes
