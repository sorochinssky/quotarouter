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
