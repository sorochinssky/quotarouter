#!/usr/bin/env bash
# The pre-push gate. Run from the repository root: scripts/preflight.sh [--live]
# No CI in this repository by choice: this script is the gate, and a green run is what "done"
# means. --live adds the vendor and end-to-end suites (they spend real credits).
set -uo pipefail
cd "$(dirname "$0")/.."

PY="${PY:-python3}"                 # interpreter that has pytest + the package deps
BUILD_PY="${BUILD_PY:-$PY}"         # interpreter that has `build` and `twine`
fail=0
step() { printf '\n=== %s ===\n' "$1"; }
check() { if [ "$1" -ne 0 ]; then fail=1; printf 'FAILED: %s\n' "$2"; fi; }

step "offline suite"
"$PY" -m pytest -q; check $? "offline suite"

step "plugin validation"
if command -v hermes >/dev/null 2>&1; then
  hermes plugins validate hermes_plugin 2>&1 | tail -3; check "${PIPESTATUS[0]}" "plugin validation"
else
  # The Hermes plugin is one of three ways in, and a contributor who uses the library or the MCP
  # server has no reason to install Hermes. Skipping is honest; failing here only taught people
  # to ignore this script.
  printf 'hermes is not installed: skipped (the Hermes plugin is optional)\n'
fi

step "packaging"
if "$BUILD_PY" -c 'import build' >/dev/null 2>&1; then
  rm -rf dist build
  "$BUILD_PY" -m build >/dev/null 2>&1; check $? "python -m build"
  if "$BUILD_PY" -m twine --version >/dev/null 2>&1; then
    "$BUILD_PY" -m twine check dist/*; check $? "twine check"
  else
    # `import twine` succeeds for a half-installed twine (its CLI imports rich and requests);
    # asking the tool for its own version is the only honest check.
    printf 'twine is not usable in %s: skipped (pip install ".[dev]")\n' "$BUILD_PY"
  fi
else
  printf 'build not installed in %s: skipped (pip install build twine)\n' "$BUILD_PY"
fi

if [ "${1:-}" = "--live" ]; then
  step "live vendors"
  "$PY" -m pytest -m "live and not e2e" -q; check $? "live vendors"
  step "end to end"
  "$PY" -m pytest -m e2e -q; check $? "end to end"
fi

printf '\n'
if [ "$fail" -eq 0 ]; then
  echo "PREFLIGHT PASSED"
else
  echo "PREFLIGHT FAILED"
fi
exit "$fail"
