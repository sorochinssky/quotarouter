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

step "types"
if "$PY" -m mypy --version >/dev/null 2>&1; then
  # The package ships py.typed, so its annotations are a promise to every consumer's checker.
  "$PY" -m mypy --ignore-missing-imports quotarouter; check $? "mypy"
else
  printf 'mypy not installed in %s: skipped (pip install ".[dev]")\n' "$PY"
fi

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

step "installed wheel"
# The plugin shim does not excuse a broken wheel: the package has to work with no source tree on
# sys.path. `status` needs no network and touches an isolated state dir, so this is an offline step.
if command -v uv >/dev/null 2>&1; then
  WHL="$(ls -1 dist/*.whl 2>/dev/null | tail -1)"
  if [ -n "$WHL" ]; then
    WHEEL_DIR="$(mktemp -d)"
    if uv venv -q "$WHEEL_DIR/venv" && VIRTUAL_ENV="$WHEEL_DIR/venv" uv pip install -q "$WHL"; then
      # Every local file is redirected: the step must not depend on the operator's own chains file
      # or ledger, or it silently tests this machine instead of the artifact.
      QUOTAROUTER_STATE_DIR="$WHEEL_DIR/state" QUOTAROUTER_ENV_FILE="$WHEEL_DIR/absent.env" \
        QUOTAROUTER_CHAINS_FILE="$WHEEL_DIR/absent.toml" QUOTAROUTER_ALLOW_KEYLESS=0 \
        "$WHEEL_DIR/venv/bin/quotarouter" status >/dev/null
      check $? "quotarouter status from the wheel"
      "$WHEEL_DIR/venv/bin/python" -c 'import pathlib, sys, quotarouter as p; root = pathlib.Path(p.__file__).parent; sys.exit(0 if (root / "py.typed").exists() else 1)'
      check $? "py.typed inside the installed wheel"
    else
      fail=1; printf 'FAILED: the wheel could not be installed\n'
    fi
    rm -rf "$WHEEL_DIR"
  else
    fail=1; printf 'FAILED: no wheel in dist/ to install\n'
  fi
else
  printf 'uv is not installed: skipped (install uv to check the wheel in a clean venv)\n'
fi

if [ "${1:-}" = "--live" ]; then
  step "live vendors"
if [ -z "${EXA_API_KEY:-}${FIRECRAWL_API_KEY:-}" ]; then
  # A live run without keys is not a code failure: the suites skip and the e2e harness reports
  # "degradation_without_keys". Say what to do instead of leaving four red tests to decode.
  printf 'no vendor key in the environment: live steps need them (set -a; . ~/.hermes/.env; set +a)\n'
fi
  "$PY" -m pytest -m "live and not e2e" -q; check $? "live vendors"
  step "end to end"
  "$PY" -m pytest -m e2e -q; check $? "end to end"
  step "the shipped example"
  # The README tells a visitor to run this file; run it, with an isolated state dir. It needs no
  # keys — the anonymous tier answers — which is also why it cannot live in the offline suite.
  EXAMPLE_STATE="$(mktemp -d)"
  # `PYTHONPATH=.` is how a contributor runs it from a checkout without installing; the wheel is
  # checked separately above, so this step is about the sample being executable at all.
  PYTHONPATH="$PWD" QUOTAROUTER_STATE_DIR="$EXAMPLE_STATE" \
    "$PY" examples/library.py "python asyncio taskgroup" >/dev/null
  check $? "examples/library.py"
  rm -rf "$EXAMPLE_STATE"
fi

printf '\n'
if [ "$fail" -eq 0 ]; then
  echo "PREFLIGHT PASSED"
else
  echo "PREFLIGHT FAILED"
fi
exit "$fail"
