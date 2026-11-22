"""E2E stand: the plugin inside a real Hermes, in an isolated HERMES_HOME.

Why a separate stand when unit tests exist: between "the adapter answered" and "the agent got
a result" sit four things, and each of them fails on its own — the plugin loader, the provider
registry (it silently refuses any provider that does not inherit `WebSearchProvider`), the tool
response envelope, and config reading. Offline tests see none of them.

Isolation: the run lives in its own `HERMES_HOME` (config.yaml + plugins + state). The live
profile is neither read nor written, and a dedicated step proves it.

Usage:

    python tests/e2e/harness.py                 # live run, costs 1 search + 1 page
    python tests/e2e/harness.py --no-live       # keyless steps only: degradation, isolation
    python tests/e2e/harness.py --keep-home     # keep the stand directory for inspection

Exit code: 0 all steps passed, 1 a step failed, 2 the stand could not be built.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
HERMES_ROOT = Path(os.environ.get("HERMES_ROOT", "/usr/local/lib/hermes-agent"))
LIVE_HOME = Path(os.environ.get("HERMES_LIVE_HOME", str(Path.home() / ".hermes")))
LIVE_STATE = Path(os.environ.get("QUOTAROUTER_LIVE_STATE",
                                 str(Path.home() / ".local" / "state" / "quotarouter")))
CHILD_TIMEOUT = float(os.environ.get("QUOTAROUTER_E2E_CHILD_TIMEOUT", "180"))
PLUGIN_NAME = "quotarouter"

# Keys the stand passes to the child process. Absent ones simply do not reach its environment.
VENDOR_KEYS = (
    "EXA_API_KEY",
    "FIRECRAWL_API_KEY",
    "DEEPSEEK_API_KEY",
    "TAVILY_API_KEY",
    "KEENABLE_API_KEY",
    "JINA_API_KEY",
)


# ---------------------------------------------------------------------------------------
# Child scenario. A separate file on disk because it must run in a process with a different
# HERMES_HOME: swapping env inside this process does not reinitialise Hermes.
# ---------------------------------------------------------------------------------------
CHILD_SOURCE = r'''
"""One run of the steps inside Hermes. Prints a JSON report to stdout."""

import asyncio
import json
import os
import sys
import time
import traceback
from pathlib import Path

HERMES_ROOT = os.environ["E2E_HERMES_ROOT"]
sys.path.insert(0, HERMES_ROOT)

STEPS = []


def step(name):
    def wrap(fn):
        STEPS.append((name, fn))
        return fn
    return wrap


def _result(payload):
    """A tool answers with a JSON string; tolerate an already-parsed dict."""
    return json.loads(payload) if isinstance(payload, str) else payload


@step("plugin_registry")
def plugin_registry(state):
    from hermes_cli.plugins import discover_plugins, get_plugin_manager

    discover_plugins(force=True)
    loaded = get_plugin_manager()._plugins.get("quotarouter")
    if loaded is None:
        raise AssertionError("the quotarouter plugin was not discovered")
    if not loaded.enabled:
        raise AssertionError(f"the plugin is installed but not enabled: {loaded.error}")

    from agent.web_search_registry import get_provider

    provider = get_provider("quotarouter")
    if provider is None:
        raise AssertionError("the Hermes registry returned no provider named 'quotarouter'")
    state["provider"] = type(provider).__module__ + "." + type(provider).__name__
    return {"provider": state["provider"], "available": provider.is_available()}


@step("search_tool")
def search_tool(state):
    from tools.web_tools import web_search_tool

    data = _result(web_search_tool(state["query"], 3))
    if not data.get("success"):
        raise AssertionError(f"search returned an error: {data.get('error')}")
    web = (data.get("data") or {}).get("web") or []
    if not web:
        raise AssertionError("search reported success but returned no results")
    backend = (data.get("data") or {}).get("backend") or ""
    state["backend"] = backend
    state["search_urls"] = [row.get("url") for row in web[:3]]
    return {"backend": backend, "results": len(web), "first": web[0].get("url")}


@step("extract_tool")
def extract_tool(state):
    from tools.web_tools import web_extract_tool

    data = _result(asyncio.run(web_extract_tool([state["url"]])))
    results = data.get("results") or []
    if not results:
        raise AssertionError(f"extract returned no pages: {data}")
    content = results[0].get("content") or ""
    minimum = int(os.environ.get("QUOTAROUTER_MIN_PAGE_CHARS", "40"))
    if len(content) < minimum:
        raise AssertionError(f"the page is shorter than {minimum} characters: {len(content)}, "
                             f"error={results[0].get('error')}")
    state["extract_chars"] = len(content)
    return {"chars": len(content), "url": results[0].get("url")}


@step("search_degradation")
def search_degradation(state):
    """No stage can serve this call: expect an honest error, not a traceback."""
    from tools.web_tools import web_search_tool

    data = _result(web_search_tool(state["query"], 3))
    if data.get("success"):
        raise AssertionError("expected a refusal, got success - the stand still holds vendor keys")
    error = str(data.get("error") or "")
    if "quotarouter" not in error:
        raise AssertionError(f"the error did not come from the router: {error[:200]}")
    state["degraded_error"] = error
    return {"error_head": error[:200]}


@step("state_isolated")
def state_isolated(state):
    """Accounting must land under THIS HERMES_HOME, not in the live profile."""
    state_dir = Path(os.environ["QUOTAROUTER_STATE_DIR"])
    ledger_path = state_dir / "ledger.json"
    routes_path = state_dir / "routes.jsonl"
    if not routes_path.exists():
        raise AssertionError(f"the route log was not written: {routes_path}")
    lines = [json.loads(line) for line in routes_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise AssertionError("the route log is empty")
    served = [line for line in lines if line.get("ok") and line.get("served_by")]
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
    return {
        "state_dir": str(state_dir),
        "routes": len(lines),
        "served_by": sorted({line["served_by"] for line in served}),
        "vendors_tracked": sorted((ledger.get("vendors") or {}).keys()),
    }


def main():
    state = {
        "query": os.environ.get("E2E_QUERY", "Nous Research Hermes Agent"),
        "url": os.environ.get("E2E_URL", "https://example.com/"),
    }
    only = [part.strip() for part in os.environ.get("E2E_ONLY_STEP", "").split(",") if part.strip()]
    selected = [(name, fn) for name, fn in STEPS if not only or name in only]
    report = {"steps": [], "ok": True}
    for name, fn in selected:
        started = time.time()
        try:
            detail = fn(state) or {}
            report["steps"].append({"name": name, "ok": True, "seconds": round(time.time() - started, 2),
                                    "detail": detail})
        except Exception as exc:
            report["steps"].append({"name": name, "ok": False, "seconds": round(time.time() - started, 2),
                                    "detail": f"{type(exc).__name__}: {exc}",
                                    "traceback": traceback.format_exc()[-1500:]})
            report["ok"] = False
            break  # the remaining steps depend on the previous ones
    report["state"] = {k: v for k, v in state.items() if k in ("backend", "search_urls", "extract_chars")}
    print("===E2E-REPORT===")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
'''


@dataclass
class StepReport:
    name: str
    ok: bool
    seconds: float = 0.0
    detail: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "seconds": self.seconds, "detail": self.detail}


@dataclass
class RunReport:
    ok: bool = True
    steps: list[StepReport] = field(default_factory=list)
    home: str = ""
    spent: dict[str, int] = field(default_factory=lambda: {"search": 0, "extract": 0})
    notes: list[str] = field(default_factory=list)

    def add(self, step: StepReport) -> None:
        self.steps.append(step)
        self.ok = self.ok and step.ok

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "home": self.home,
            "spent": self.spent,
            "notes": self.notes,
            "steps": [s.as_dict() for s in self.steps],
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }


# ---------------------------------------------------------------------------------------
# The stand
# ---------------------------------------------------------------------------------------
def vendor_keys(present_only: bool = True) -> dict[str, str]:
    keys = {name: os.environ[name] for name in VENDOR_KEYS if os.environ.get(name)}
    if present_only:
        return keys
    return {name: os.environ.get(name, "") for name in VENDOR_KEYS}


def build_home(base: Path, *, keys: dict[str, str], allow_keyless: bool = False,
               search_order: Optional[str] = None, extract_order: Optional[str] = None) -> Path:
    """Build an isolated HERMES_HOME: config, .env, plugin.

    The plugin is a symlink to `hermes_plugin/`, which itself links to `../quotarouter`, so the
    stand exercises exactly the package in this repository - not a copy that drifts.
    """
    home = base / "hermes-home"
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    (home / "state").mkdir(parents=True, exist_ok=True)

    plugin_link = home / "plugins" / PLUGIN_NAME
    if plugin_link.is_symlink():
        plugin_link.unlink()
    elif plugin_link.exists():
        shutil.rmtree(plugin_link)
    plugin_link.symlink_to(REPO_ROOT / "hermes_plugin", target_is_directory=True)

    config = [
        "plugins:",
        "  enabled:",
        f"    - {PLUGIN_NAME}",
        "web:",
        f"  search_backend: {PLUGIN_NAME}",
        f"  extract_backend: {PLUGIN_NAME}",
        f"  keyless_rescue: {'true' if allow_keyless else 'false'}",
    ]
    (home / "config.yaml").write_text("\n".join(config) + "\n", encoding="utf-8")

    env_lines = [f"{name}={value}" for name, value in keys.items() if value]
    if not allow_keyless:
        env_lines.append("QUOTAROUTER_KEYLESS_LAST_RESORT=false")
    if search_order:
        env_lines.append(f"QUOTAROUTER_SEARCH_ORDER={search_order}")
    if extract_order:
        env_lines.append(f"QUOTAROUTER_EXTRACT_ORDER={extract_order}")
    (home / ".env").write_text("\n".join(env_lines) + ("\n" if env_lines else ""), encoding="utf-8")
    return home


def child_python() -> str:
    """The Hermes interpreter: it has httpx and the agent package itself."""
    candidate = HERMES_ROOT / "venv" / "bin" / "python"
    return str(candidate) if candidate.exists() else sys.executable


def run_child(home: Path, *, label: str, extra_env: Optional[dict[str, str]] = None,
              keys: Optional[dict[str, str]] = None) -> tuple[StepReport | None, dict[str, Any]]:
    """Run the child scenario in an isolated home and parse its report."""
    child = home / "e2e_child.py"
    child.write_text(CHILD_SOURCE, encoding="utf-8")

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
        "HERMES_HOME": str(home),
        "QUOTAROUTER_STATE_DIR": str(home / "state" / PLUGIN_NAME),
        "E2E_HERMES_ROOT": str(HERMES_ROOT),
        "PYTHONPATH": str(HERMES_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
        **(keys or {}),
        **(extra_env or {}),
    }
    started = time.time()
    proc = subprocess.run([child_python(), str(child)], capture_output=True, text=True,
                          env=env, timeout=CHILD_TIMEOUT, cwd=str(REPO_ROOT))
    elapsed = round(time.time() - started, 2)
    stdout = proc.stdout or ""
    if "===E2E-REPORT===" not in stdout:
        detail = (stdout[-800:] + "\n" + (proc.stderr or "")[-800:]).strip()
        return StepReport(f"child[{label}]", False, elapsed,
                          f"no report, exit code {proc.returncode}: {detail}"), {}
    payload = stdout.split("===E2E-REPORT===", 1)[1].strip().splitlines()[0]
    report = json.loads(payload)
    return StepReport(f"child[{label}]", bool(report.get("ok")), elapsed, None), report


def live_state_fingerprint() -> str:
    """Fingerprint of the live profile: proof the stand did not touch it."""
    digest = hashlib.sha256()
    config = LIVE_HOME / "config.yaml"
    if config.exists():
        digest.update(config.read_bytes())
    for state_dir in (LIVE_STATE, LIVE_HOME / "state" / PLUGIN_NAME):
        if not state_dir.exists():
            continue
        for path in sorted(state_dir.iterdir()):
            if path.is_file():
                digest.update(path.name.encode())
                digest.update(str(path.stat().st_size).encode())
                digest.update(str(int(path.stat().st_mtime)).encode())
    return digest.hexdigest()


def run(live: bool = True, keep_home: bool = False, base_dir: Optional[Path] = None) -> RunReport:
    """Run the whole stand. Returns a report; never raises."""
    report = RunReport()
    if not (HERMES_ROOT / "tools" / "web_tools.py").exists():
        report.add(StepReport("hermes_available", False, 0.0, f"no Hermes installation in {HERMES_ROOT}"))
        return report

    base = Path(base_dir) if base_dir else Path(tempfile.mkdtemp(prefix="quotarouter-e2e-"))
    report.home = str(base)
    before = live_state_fingerprint()

    try:
        # --- 1. the live path: plugin, search, extract, accounting --------------------
        if live:
            keys = vendor_keys()
            if not keys:
                report.add(StepReport("vendor_keys", False, 0.0,
                                      "no vendor key in the environment - a live run is impossible"))
            else:
                home = build_home(base, keys=keys)
                _, child = run_child(home, label="live", keys=keys, extra_env={
                    # degradation has its own keyless stand; with keys present that step
                    # would necessarily fail
                    "E2E_ONLY_STEP": "plugin_registry,search_tool,extract_tool,state_isolated",
                })
                for item in child.get("steps", []):
                    report.add(StepReport(item["name"], item["ok"], item.get("seconds", 0.0),
                                          item.get("detail")))
                if child.get("ok"):
                    report.spent["search"] += 1
                    report.spent["extract"] += 1

        # --- 2. degradation: with no keys the stand must refuse honestly --------------
        degraded_base = base / "degraded"
        degraded_base.mkdir(parents=True, exist_ok=True)
        degraded_home = build_home(degraded_base, keys={}, allow_keyless=False)
        _, child = run_child(degraded_home, label="degraded", keys={},
                             extra_env={"E2E_ONLY_STEP": "plugin_registry,search_degradation"})
        steps = child.get("steps", [])
        names = [s["name"] for s in steps]
        degraded_ok = bool(child.get("ok")) and "search_degradation" in names
        report.add(StepReport(
            "degradation_without_keys", degraded_ok, 0.0,
            next((s.get("detail") for s in steps if s["name"] == "search_degradation"),
                 "the step did not run")))
    finally:
        after = live_state_fingerprint()
        report.add(StepReport(
            "isolation", before == after, 0.0,
            "the live profile is untouched" if before == after else "the live profile CHANGED during the run"))
        if not keep_home:
            shutil.rmtree(base, ignore_errors=True)
        else:
            report.notes.append(f"stand directory kept: {base}")

    return report


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="E2E run of the quotarouter plugin inside Hermes.")
    parser.add_argument("--no-live", action="store_true", help="no vendor calls (zero cost)")
    parser.add_argument("--keep-home", action="store_true", help="keep the stand directory")
    parser.add_argument("--report", default=str(REPO_ROOT / "e2e-report.json"),
                        help="where to write the report")
    args = parser.parse_args(argv)

    report = run(live=not args.no_live, keep_home=args.keep_home)
    path = Path(args.report)
    path.write_text(json.dumps(report.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    for step in report.steps:
        mark = "OK  " if step.ok else "FAIL"
        detail = step.detail if isinstance(step.detail, str) else json.dumps(step.detail, ensure_ascii=False)
        print(f"{mark} {step.name} ({step.seconds}s) {detail or ''}")
    print(f"\nreport: {path}")
    print("RESULT:", "all steps passed" if report.ok else "some steps failed")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
