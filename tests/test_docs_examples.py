"""The operator guide is executable: every TOML block parses, every command runs.

Documentation drifts because nothing checks it. So the guide's blocks are parsed here, the shipped
example is loaded through the same loader a deployment uses, and the commands the guide tells an
operator to run are run — with no credentials, which also proves they dial nothing.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from quotarouter import chains, cli, config

REPO_ROOT = Path(__file__).resolve().parents[1]
GUIDE = REPO_ROOT / "docs" / "chains.md"
EXAMPLE = REPO_ROOT / "docs" / "chains.example.toml"
KNOWN = {"deepseek", "exa", "firecrawl", "jina", "keenable", "keyless", "tavily"}


def _toml_blocks() -> list[str]:
    text = GUIDE.read_text(encoding="utf-8")
    return re.findall(r"```toml\n(.*?)```", text, re.S)


def test_the_guide_has_blocks_to_check():
    assert len(_toml_blocks()) >= 3, "the guide is supposed to show a format and examples"


def test_every_toml_block_in_the_guide_parses():
    for block in _toml_blocks():
        tomllib.loads(block)


def test_every_guide_example_is_a_usable_chain():
    for block in _toml_blocks():
        loaded = chains.parse_document(tomllib.loads(block), KNOWN)
        assert not loaded.problems, (block, loaded.problems)
        assert loaded.specs, block
        for spec in loaded.specs.values():
            assert spec.stages_for("search") or spec.stages_for("extract")


def test_the_shipped_example_loads_without_a_problem():
    loaded = chains.load(lambda key: "", KNOWN, path=EXAMPLE)

    assert loaded.problems == ()
    assert set(loaded.names()) == {"fast", "free", "single-vendor", "prefer-jina"}
    assert loaded.get("free").budget == "free"
    assert loaded.get("prefer-jina").strict is False


@pytest.fixture
def example_env(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_CHAINS_FILE", str(EXAMPLE))
    monkeypatch.setenv("QUOTAROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("QUOTAROUTER_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.delenv("QUOTAROUTER_CHAINS", raising=False)
    for key in ("EXA_API_KEY", "FIRECRAWL_API_KEY", "TAVILY_API_KEY", "KEENABLE_API_KEY",
                "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("QUOTAROUTER_ALLOW_KEYLESS", "0")
    monkeypatch.setenv("QUOTAROUTER_KEYLESS_LAST_RESORT", "0")
    config.reset_settings_for_tests()
    return tmp_path


def test_the_guide_commands_run_without_credentials(example_env, capsys):
    """`chains` and `route` are advertised as free of network calls; prove it by having no keys."""
    assert cli.main(["chains"]) == 0
    listing = capsys.readouterr().out
    assert "free" in listing and "source: file" in listing

    assert cli.main(["route", "--chain", "free"]) == 0
    route = capsys.readouterr().out
    assert "search: named" in route and "extract: named" in route
    assert "no EXA_API_KEY set" in route, "with no keys every stage is skipped, and it says why"


def test_the_guide_json_shape_is_the_one_it_documents(example_env, capsys):
    assert cli.main(["chains", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report) == {"source", "path", "chains", "problems", "allow_chain_override"}


# --- the README is documentation too ------------------------------------------------------
README = REPO_ROOT / "README.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"


def _python_blocks_of_readme() -> list[str]:
    text = README.read_text(encoding="utf-8")
    return re.findall(r"```python\n(.*?)```", text, re.S)


def test_the_readme_still_shows_python():
    assert _python_blocks_of_readme(), "the first thing a visitor reads has a code sample"


def test_every_call_in_the_readme_sample_is_a_real_call():
    """A sample that names an argument the method does not take reads fine and fails for the reader.

    Signatures are the cheapest way to catch that without network: the names come from the README,
    the acceptance comes from the installed function.
    """
    import inspect

    from quotarouter import Router

    blocks = _python_blocks_of_readme()
    calls = [match for block in blocks
             for match in re.findall(r"router\.(\w+)\(([^)]*)\)", block)]
    assert calls, "the sample is supposed to call the router"

    for method, arguments in calls:
        function = getattr(Router, method, None)
        assert function is not None, f"README calls router.{method}, which does not exist"
        signature = inspect.signature(function)
        for name in re.findall(r"(\w+)\s*=", arguments):
            assert name in signature.parameters, (
                f"README passes {name}= to router.{method}, which does not accept it")


@pytest.mark.parametrize("version", ["0.2.0", "0.3.0", "0.3.1"])
def test_every_release_in_the_changelog_links_somewhere(version):
    text = CHANGELOG.read_text(encoding="utf-8")
    assert f"## [{version}]" in text, f"the tag exists, so the changelog must have a section"
    assert f"[{version}]: https://" in text, f"{version} needs a link definition, or the heading is dead text"


def test_the_unreleased_link_starts_at_the_newest_release():
    """A compare link pinned to an older tag quietly hides everything since."""
    text = CHANGELOG.read_text(encoding="utf-8")
    released = sorted(re.findall(r"^\[(\d+\.\d+\.\d+)\]:", text, re.M),
                      key=lambda v: tuple(int(part) for part in v.split(".")))
    newest = released[-1]
    match = re.search(r"^\[Unreleased\]: \S+compare/v(\S+?)\.\.\.HEAD", text, re.M)
    assert match is not None
    assert match.group(1) == newest, f"Unreleased compares from {match.group(1)}, newest tag is {newest}"
