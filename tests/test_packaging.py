"""Configuration sources, and the packaging rules that keep the package host-agnostic.

Two kinds of check live here:

1. `Settings` reads `QUOTAROUTER_*` first and the legacy `ROUTER_*` second, so a rename never
   forces a synchronised restart.
2. The core imports no host framework, and no tracked artifact carries Cyrillic — the two
   rules that make the repository publishable and the package reusable.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

from quotarouter import config

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = REPO_ROOT / "quotarouter"
# modules a host owns: the core must never reach into them
FORBIDDEN_ROOTS = ("agent", "hermes_cli", "tools", "hermes_plugins")
CYRILLIC = re.compile(r"[\u0400-\u04FF]")
TEXT_SUFFIXES = {".md", ".py", ".yaml", ".yml", ".toml", ".txt", ".json", ".cfg", ".ini"}


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    """Every test starts from an environment with no router variables at all."""
    for key in list(__import__("os").environ):
        if key.startswith("QUOTAROUTER_") or key.startswith("ROUTER_"):
            monkeypatch.delenv(key, raising=False)
    config.reset_settings_for_tests()
    yield
    config.reset_settings_for_tests()


def test_defaults_apply_when_nothing_is_set():
    settings = config.settings(refresh=True)
    assert settings.search_order == config.DEFAULT_SEARCH_ORDER
    assert settings.extract_order == config.DEFAULT_EXTRACT_ORDER
    assert settings.min_page_chars == 40
    assert settings.ledger_path.name == "ledger.json"


def test_new_prefix_reorders_the_chain(monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_SEARCH_ORDER", "exa, firecrawl ,deepseek")
    assert config.settings(refresh=True).search_order == ("exa", "firecrawl", "deepseek")


def test_legacy_prefix_still_works(monkeypatch):
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa,firecrawl")
    assert config.settings(refresh=True).search_order == ("exa", "firecrawl")


def test_new_prefix_wins_over_the_legacy_one(monkeypatch):
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa")
    monkeypatch.setenv("QUOTAROUTER_SEARCH_ORDER", "firecrawl,exa")
    assert config.settings(refresh=True).search_order == ("firecrawl", "exa")


def test_empty_value_is_not_an_empty_chain(monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_SEARCH_ORDER", "   ")
    assert config.settings(refresh=True).search_order == config.DEFAULT_SEARCH_ORDER


def test_unparsable_number_falls_back_and_warns(monkeypatch, caplog):
    monkeypatch.setenv("QUOTAROUTER_MIN_PAGE_CHARS", "forty")
    with caplog.at_level("WARNING"):
        assert config.settings(refresh=True).min_page_chars == 40
    assert any("min_page_chars" in record.message.lower() or "MIN_PAGE_CHARS" in record.message
               for record in caplog.records)


def test_state_dir_honours_the_explicit_variable(monkeypatch, tmp_path):
    monkeypatch.setenv("QUOTAROUTER_STATE_DIR", str(tmp_path / "state"))
    settings = config.settings(refresh=True)
    assert settings.ledger_path == tmp_path / "state" / "ledger.json"
    assert settings.log_path == tmp_path / "state" / "routes.jsonl"


def test_state_dir_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert config.default_state_dir() == tmp_path / "xdg" / "quotarouter"


# --- packaging rules ----------------------------------------------------------------------
def _core_modules() -> list[Path]:
    return [path for path in CORE_ROOT.rglob("*.py") if "integrations" not in path.parts]


def _imported_roots(path: Path) -> set[str]:
    """Top-level module names this file imports, read from the AST (docstrings do not count)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import stays inside the package
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_core_imports_no_host_framework():
    offenders = []
    for path in _core_modules():
        hit = _imported_roots(path) & set(FORBIDDEN_ROOTS)
        if hit:
            offenders.append(f"{path.relative_to(REPO_ROOT)}: imports {sorted(hit)}")
    assert not offenders, "the core must stay importable without a host: " + ", ".join(offenders)


def test_integrations_are_not_imported_by_the_core():
    """`import quotarouter` must not pull Hermes or the MCP server in."""
    tree = ast.parse((CORE_ROOT / "__init__.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "integrations" in node.module:
            raise AssertionError("quotarouter/__init__.py must not import integrations eagerly")
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "integrations" not in alias.name, \
                    "quotarouter/__init__.py must not import integrations eagerly"


def test_no_cyrillic_in_tracked_artifacts():
    offenders = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or ".git/" in str(path) or "__pycache__" in str(path):
            continue
        if path.suffix not in TEXT_SUFFIXES:
            continue
        if path.name == "e2e-report.json":
            continue
        if CYRILLIC.search(path.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, "these files still carry Cyrillic: " + ", ".join(sorted(offenders))


BLOCKED_IMPORT_SCRIPT = """
import sys

ALLOWED = "quotarouter"


class Blocker:
    def find_module(self, name, path=None):
        return None

    def find_spec(self, name, path=None, target=None):
        root = name.split(".")[0]
        if root in ("agent", "hermes_cli", "tools", "hermes_plugins"):
            raise ImportError(f"host module {root!r} must not be imported by the core")
        return None


sys.meta_path.insert(0, Blocker())
import quotarouter  # noqa: E402

assert quotarouter.Router is not None
print("ok")
"""


def test_core_imports_without_hermes(tmp_path):
    """A separate process with the host modules blocked: the import must still work.

    A subprocess, not a monkeypatched `sys.modules`: deleting modules inside the test session
    leaves imported singletons from the previous copy behind and breaks later tests.
    """
    script = tmp_path / "blocked_import.py"
    script.write_text(BLOCKED_IMPORT_SCRIPT, encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          env=env, cwd=str(tmp_path), timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip().endswith("ok")


def test_the_review_notes_are_tracked_while_raw_answers_are_not():
    """`reviews/` in .gitignore also swallowed `docs/reviews/`, so the review notes silently
    never entered the repository — the file existed on one machine and nowhere else."""
    import subprocess

    root = Path(__file__).resolve().parents[1]

    def ignored(rel: str) -> bool:
        return subprocess.run(["git", "check-ignore", "-q", rel], cwd=root).returncode == 0

    assert ignored("reviews/nemotron.json"), "raw model answers must stay out of git"
    assert not ignored("docs/reviews/2026-11-22-model-review.md"), "the review notes must be tracked"


def test_the_cli_builds_its_router_from_the_core():
    """The CLI is what an MCP client starts. Building its router from the Hermes adapter made
    every non-Hermes host depend on Hermes' key lookup."""
    from quotarouter import cli, routing

    assert cli.get_router is routing.shared_router



# --- the promises a stranger reads first ---------------------------------------------------
def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_the_package_ships_the_pep_561_marker():
    """`Typing :: Typed` in the classifiers is a claim; `py.typed` is the claim arriving.

    Without the marker, mypy and pyright treat every annotation in this package as an
    unverified hint and silently ignore it.
    """
    marker = CORE_ROOT / "py.typed"
    assert marker.is_file(), "quotarouter/py.typed is missing"
    assert marker.read_text(encoding="utf-8") == "", "the marker must stay empty"


def test_the_version_has_one_source():
    """Two hand-copied version strings drift; the build reads the package's own `__version__`."""
    pyproject = _read("pyproject.toml")
    assert 'dynamic = ["version"]' in pyproject
    assert '[tool.hatch.version]' in pyproject
    assert 'path = "quotarouter/__init__.py"' in pyproject
    assert "\nversion = " not in pyproject, "a literal version in pyproject would win over the package"


def test_the_plugin_manifest_agrees_with_the_package():
    from quotarouter import __version__

    manifest = _read("hermes_plugin/plugin.yaml")
    assert f"version: {__version__}" in manifest


def test_a_dotenv_can_never_be_committed_but_its_template_is():
    """The README tells people to keep keys in `./.env`. Nothing ignored that file."""
    def ignored(rel: str) -> bool:
        return subprocess.run(["git", "check-ignore", "-q", rel], cwd=REPO_ROOT).returncode == 0

    assert ignored(".env"), "a filled-in key file must be ignored"
    assert ignored(".env.local"), "so must every variant"
    assert not ignored(".env.example"), "the template has to be tracked"


def _load_example():
    import importlib.util

    path = REPO_ROOT / "examples" / "library.py"
    assert path.is_file(), "the README links examples/library.py"
    spec = importlib.util.spec_from_file_location("qr_example_library", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_the_documented_example_uses_the_real_api(capsys):
    """The README sample once called `result.results` and `result.served_by`; neither exists.

    The example is executable on purpose: a doc that cannot run is a doc that lies, and the
    first thing a visitor copies is exactly this code.
    """
    from quotarouter.routing import RouteAttempt, RouteResult

    example = _load_example()

    class FakeRouter:
        def run_search(self, query, limit):
            return RouteResult(payload={"success": True, "data": {"web": [
                {"title": "Example", "url": "https://example.com/"}]}}, stage="exa")

        def run_extract(self, urls):
            return RouteResult(payload=[
                {"url": "https://example.com/", "content": "body", "title": "Example"},
                {"url": "https://closed.example/", "error": "403 from origin"},
            ], stage="jina")

    assert example.search(FakeRouter(), "q") == 0
    out = capsys.readouterr().out
    assert "served by: exa" in out and "https://example.com/" in out

    assert example.extract(FakeRouter(), ["a", "b"]) == 0
    out = capsys.readouterr().out
    assert "403 from origin" in out, "a page error must be visible, not swallowed"


def test_the_documented_example_reports_a_total_failure(capsys):
    from quotarouter.routing import RouteAttempt, RouteResult

    example = _load_example()

    class DeadRouter:
        def run_search(self, query, limit):
            return RouteResult(payload=None, stage="", attempts=[
                RouteAttempt(stage="exa", ok=False, reason="quota: 402 Insufficient balance")])

    assert example.search(DeadRouter(), "q") == 1
    out = capsys.readouterr().out
    assert "no search provider served" in out and "exa" in out


def test_every_annotation_in_the_package_resolves():
    """`from __future__ import annotations` stores hints as strings, so a missing import in a
    signature is invisible until something introspects it — a type checker, a framework, a user
    with `py.typed` and an editor. `stages/base.py` used `Callable` without importing it.
    """
    import importlib
    import inspect
    import pkgutil
    import typing

    import quotarouter

    broken: list[str] = []
    for module_info in pkgutil.walk_packages(quotarouter.__path__, "quotarouter."):
        module = importlib.import_module(module_info.name)
        targets = []
        for name, obj in vars(module).items():
            if inspect.isfunction(obj) and obj.__module__ == module.__name__:
                targets.append((name, obj))
            elif inspect.isclass(obj) and obj.__module__ == module.__name__:
                for method_name, raw in vars(obj).items():
                    # `staticmethod` and `classmethod` objects are not functions; skipping them
                    # left `_map_urls` uncovered, and that is exactly where the missing import
                    # was. Unwrap before deciding.
                    method = getattr(raw, "__func__", raw)
                    if inspect.isfunction(method):
                        targets.append((f"{name}.{method_name}", method))
        for label, function in targets:
            try:
                typing.get_type_hints(function)
            except Exception as exc:  # NameError, TypeError on an odd annotation
                broken.append(f"{module_info.name}:{label}: {exc}")

    assert not broken, "unresolvable annotations: " + "; ".join(broken)
