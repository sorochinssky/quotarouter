# Tasks: provider-agnostic core

## 1. Rename and split

- [x] 1.1 `git mv router quotarouter`, `git mv quotarouter/provider.py quotarouter/integrations/hermes.py`.
  Check: `python3 -c "import quotarouter"` succeeds; `grep -rn "from router" --include=*.py .` finds nothing.
- [x] 1.2 All imports in the package, the shim and the tests point at `quotarouter`.
  Check: `python3 -m pytest -q` → 102 passed.
- [x] 1.3 Environment prefix `QUOTAROUTER_` with the legacy `ROUTER_` fallback.
  Check: `test_config.py` covers both names; a run with only `ROUTER_SEARCH_ORDER` still reorders the chain.
- [x] 1.4 Provider name becomes `quotarouter`; manifest and symlink follow.
  Check: `test_provider_registers_under_the_name_hermes_will_configure` passes.

## 2. Second host

- [x] 2.1 `quotarouter/integrations/mcp.py` — stdio JSON-RPC 2.0, tools `web_search`,
  `web_extract`, `quota_status`.
  Check: `test_mcp_integration.py` answers `initialize`, `tools/list` and `tools/call` in a subprocess.
- [x] 2.2 `python3 -m quotarouter.cli serve-mcp` as the documented client entry point.
  Check: the module starts, prints nothing but protocol lines, and exits on EOF.
- [x] 2.3 The core imports no host framework.
  Check: `test_core_imports_no_host_framework` passes.

## 3. Publication readiness

- [x] 3.1 README rewritten in English: purpose, the "why not hermes-seek" table, three
  installation paths (Hermes, MCP, library), stage order, operations, error policy, testing.
  Check: the README answers "what is it", "how do I install it", "how do I test it".
- [x] 3.2 `docs/testing.md` and `docs/vendor-probes.md` in English.
  Check: no Cyrillic in either file.
- [x] 3.3 OpenSpec artifacts in English; `openspec/config.yaml` context updated for the new
  layout. Check: `openspec validate --strict` passes for the change.
- [x] 3.4 `pyproject.toml`: name `quotarouter`, version 0.2.0, readme, license, classifiers,
  project urls, console script `quotarouter`.
  Check: `pip install -e . && quotarouter status` works in a scratch venv.

## 4. Gates

- [x] 4.1 `python3 -m pytest -q` → 102 passed, 12 deselected, offline.
  Check: no network access needed; the run takes about two seconds.
- [x] 4.2 `python3 tests/e2e/harness.py` → six of six steps, `backend: quotarouter:exa`.
  Check: exit code 0 and `e2e-report.json` written.
- [x] 4.3 Live adapters on real keys.
  Check: `python3 -m pytest -m "live and not e2e" -q` → all pass.
- [x] 4.4 No Cyrillic left in tracked files.
  Check: `grep -rlP "[\x{0400}-\x{04FF}]" --include=*.md --include=*.py --include=*.yaml --include=*.toml .` → empty.
