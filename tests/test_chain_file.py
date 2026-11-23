"""The chain file: what the operator wrote, what survived parsing, and what did not.

A chain is a pair of stage lists plus a description, a budget and a strictness flag — that is a
data file, not a one-line variable. The rules under test: a typo costs one entry and never the
service, the shorthand replaces the file rather than merging with it, and a file nobody can read
leaves the default order in charge instead of taking the process down.
"""

from __future__ import annotations

import logging

import pytest

from quotarouter import chains, config


KNOWN = {"exa", "firecrawl", "jina", "deepseek", "keyless"}


def _load(tmp_path, text: str, name: str = "chains.toml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return chains.load(lambda key: "", KNOWN, path=path)


GOOD = """
[chains.fast]
description = "Search only"
search = ["exa", "firecrawl"]

[chains.free]
description = "Nothing that can cost money"
search = ["exa", "firecrawl"]
extract = ["jina", "firecrawl"]
budget = "free"

[chains.single]
search = ["firecrawl"]
strict = false
"""


# --- the happy shape ---------------------------------------------------------------------
def test_a_chain_is_a_pair_of_lists(tmp_path):
    loaded = _load(tmp_path, GOOD)

    assert loaded.source == "file"
    assert loaded.names() == ("fast", "free", "single")
    fast = loaded.get("fast")
    assert fast is not None
    assert fast.search == ("exa", "firecrawl")
    assert fast.extract == ()
    assert fast.capabilities() == ("search",), "a search-only chain says so about itself"
    assert fast.description == "Search only"
    assert fast.budget is None
    assert fast.strict is True

    free = loaded.get("free")
    assert free is not None
    assert free.search == ("exa", "firecrawl")
    assert free.extract == ("jina", "firecrawl")
    assert free.budget == "free"
    single = loaded.get("single")
    assert single is not None and single.strict is False


def test_a_duplicate_name_is_dialed_once(tmp_path):
    loaded = _load(tmp_path, '[chains.dup]\nsearch = ["exa", "exa", "firecrawl"]\n')
    dup = loaded.get("dup")
    assert dup is not None and dup.search == ("exa", "firecrawl")


# --- one bad entry, not a broken service -------------------------------------------------
@pytest.mark.parametrize("entry,fragment", [
    ('[chains.bad]\nsearch = ["exa"]\ntimeout = 5\n', "unknown key"),
    ('[chains.bad]\nsearch = ["exa", "fircrawl"]\n', "not a stage"),
    ('[chains.bad]\nsearch = []\n', "empty"),
    ('[chains.bad]\nsearch = "exa"\n', "must be a list"),
    ('[chains.bad]\nbudget = "cheap"\nsearch = ["exa"]\n', "budget must be one of"),
    ('[chains.bad]\nmax_cost_usd = -1\nsearch = ["exa"]\n', "non-negative"),
    ('[chains.bad]\nstrict = "yes"\nsearch = ["exa"]\n', "strict must be"),
    ('[chains.bad]\ndescription = 5\nsearch = ["exa"]\n', "description must be"),
    ('[chains.bad]\nwhatever = 1\n', "unknown key"),
    ('[chains.bad]\n', "neither search nor extract"),
    ('[chains.bad]\nsearch = [1, 2]\n', "non-string"),
])
def test_a_malformed_entry_is_dropped_and_reported(tmp_path, entry, fragment):
    loaded = _load(tmp_path, entry + '\n[chains.good]\nsearch = ["exa"]\n')

    assert loaded.names() == ("good",), "the valid entry must survive its neighbour"
    assert len(loaded.problems) == 1
    assert loaded.problems[0].entry == "chains.bad"
    assert fragment in loaded.problems[0].reason


def test_a_chain_that_is_not_a_table_is_reported(tmp_path):
    loaded = _load(tmp_path, "[chains]\nflat = 1\n")
    assert loaded.names() == ()
    assert "must be a table" in loaded.problems[0].reason


def test_a_chains_key_that_is_not_a_table_is_reported(tmp_path):
    loaded = _load(tmp_path, "chains = 5\n")
    assert loaded.names() == ()
    assert "table of tables" in loaded.problems[0].reason


def test_a_file_without_chains_is_reported(tmp_path):
    loaded = _load(tmp_path, "[something]\nkey = 1\n")
    assert loaded.names() == ()
    assert loaded.problems and "no [chains.*] tables" in loaded.problems[0].reason


# --- the file is optional, and a broken one is not an outage -----------------------------
def test_a_missing_file_means_no_named_chains(tmp_path):
    loaded = chains.load(lambda key: "", KNOWN, path=tmp_path / "absent.toml")

    assert loaded.names() == ()
    assert loaded.source == "none"
    assert loaded.problems == (), "a fresh install is not a configuration error"


def test_an_unreadable_file_is_reported_and_ignored(tmp_path):
    """A directory where a file belongs is the portable stand-in for EACCES (tests run as root)."""
    path = tmp_path / "chains.toml"
    path.mkdir()
    loaded = chains.load(lambda key: "", KNOWN, path=path)

    assert loaded.names() == ()
    assert loaded.problems and "unreadable" in loaded.problems[0].reason


def test_broken_toml_is_reported_with_the_path(tmp_path):
    loaded = _load(tmp_path, "[chains.bad\nsearch = [")

    assert loaded.names() == ()
    assert "invalid TOML" in loaded.problems[0].reason
    assert "chains.toml" in loaded.problems[0].entry


# --- the shorthand replaces, it does not merge -------------------------------------------
def test_the_shorthand_replaces_the_file_and_says_so(tmp_path, caplog):
    path = tmp_path / "chains.toml"
    path.write_text(GOOD, encoding="utf-8")
    settings = {"QUOTAROUTER_CHAINS": "quick=exa;bad-segment"}

    with caplog.at_level(logging.WARNING):
        loaded = chains.load(lambda key: settings.get(key, ""), KNOWN, path=path)

    assert loaded.source == "shorthand"
    assert loaded.names() == ("quick",), "the file's names must not survive the replacement"
    quick = loaded.get("quick")
    assert quick is not None
    assert quick.search == ("exa",)
    assert quick.extract == ("exa",), "one list serves both capabilities"
    warnings = [r.getMessage() for r in caplog.records]
    assert any("ignored" in message for message in warnings), warnings
    assert any("bad-segment" in problem.entry for problem in loaded.problems)


def test_the_shorthand_can_be_malformed_without_crashing():
    loaded = chains.parse_shorthand("=exa;;name=;ok=exa,exa;typo=fircrawl", KNOWN)
    assert loaded.names() == ("ok",)
    assert len(loaded.problems) == 3, "every unusable segment is reported, none is silently dropped"
    assert any("fircrawl" in problem.reason for problem in loaded.problems)


# --- settings wiring ---------------------------------------------------------------------
def test_settings_parse_the_file_once(tmp_path, monkeypatch):
    path = tmp_path / "chains.toml"
    path.write_text(GOOD, encoding="utf-8")
    monkeypatch.setenv("QUOTAROUTER_CHAINS_FILE", str(path))
    monkeypatch.delenv("QUOTAROUTER_CHAINS", raising=False)
    config.reset_settings_for_tests()

    settings = config.settings()
    assert settings.chains_path == path
    first = settings.chains
    assert first.names() == ("fast", "free", "single")
    assert settings.chains is first, "the file is configuration: read once, remembered"


def test_the_default_location_is_used_when_nothing_says_otherwise(monkeypatch):
    monkeypatch.delenv("QUOTAROUTER_CHAINS_FILE", raising=False)
    monkeypatch.delenv("ROUTER_CHAINS_FILE", raising=False)
    config.reset_settings_for_tests()

    assert str(config.settings().chains_path).endswith("/.config/quotarouter/chains.toml")


def test_override_is_allowed_by_default(monkeypatch):
    monkeypatch.delenv("QUOTAROUTER_ALLOW_CHAIN_OVERRIDE", raising=False)
    config.reset_settings_for_tests()
    assert config.settings().allow_chain_override is True
