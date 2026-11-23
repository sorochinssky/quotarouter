"""The chains: there are two, their order is data, and a typo must not shrink them silently.

`search_order` and `extract_order` are separate lists because the cheap vendor for a query is not
the cheap vendor for a page. Everything else here follows from that: a stage that cannot do the
capability is skipped with a reason, one name is a valid chain, and an unknown name is reported
at construction instead of surfacing as a mystery at the first failure.
"""

from __future__ import annotations

import logging

import pytest

from quotarouter import config, ledger as ledger_mod, routing, types
from quotarouter.stages.base import Capability, Stage


class Only(Stage):
    """A stage with an exact capability set and a fixed answer."""

    key_env = ""

    def __init__(self, name: str, search: bool, extract: bool, label: str = "") -> None:
        self.name = name
        self.capability = Capability(search=search, extract=extract,
                                     search_units=1.0, extract_units=1.0)
        self.unit = "requests"
        self.calls: list[str] = []
        self.label = label or name

    def search(self, query, limit):
        self.calls.append("search")
        return types.search_ok([{"title": self.label, "url": f"https://{self.name}.example/"}])

    def extract(self, urls):
        self.calls.append("extract")
        return [types.document(f"https://{self.name}.example/", self.label, "body " * 20,
                               source_url=url) for url in urls]


@pytest.fixture
def build(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("QUOTAROUTER_ALLOW_KEYLESS", "0")

    def make(search=(), extract=(), **stages):
        settings = config.Settings(search_order=tuple(search), extract_order=tuple(extract),
                                   state_dir=tmp_path)
        return routing.Router(settings=settings, stages=stages,
                              ledger=ledger_mod.Ledger(tmp_path / "ledger.json"))

    return make


# --- two chains, not one ----------------------------------------------------------------
def test_search_and_extract_walk_different_orders(build):
    """A single chain would have to put Jina Reader in front of search, where it cannot serve."""
    pages = Only("jina", search=False, extract=True)
    search = Only("exa", search=True, extract=False)
    router = build(search=("exa", "jina"), extract=("jina", "exa"), exa=search, jina=pages)

    assert router.run_search("q", 3).stage == "exa"
    assert router.run_extract(["https://a"]).stage == "jina"
    assert search.calls == ["search"]
    assert pages.calls == ["extract"]


def test_a_stage_without_the_capability_is_skipped_with_that_reason(build):
    search_only = Only("exa", search=True, extract=False)
    router = build(search=("exa",), extract=("exa",), exa=search_only)

    result = router.run_extract(["https://a"])
    assert not result.ok
    assert "does not support extract" in result.summary()


# --- the order is data ------------------------------------------------------------------
def test_one_name_is_a_valid_chain(build):
    """Some setups want exactly one vendor: no failover, and a failure that says so."""
    only = Only("exa", search=True, extract=False)
    router = build(search=("exa",), extract=(), exa=only)

    assert router.run_search("q", 3).stage == "exa"
    assert len(router.candidates("search")[0]) == 1


def test_the_order_decides_who_answers_first(build):
    first = Only("exa", search=True, extract=False, label="exa")
    second = Only("firecrawl", search=True, extract=False, label="firecrawl")
    router = build(search=("firecrawl", "exa"), extract=(), exa=first, firecrawl=second)

    assert router.run_search("q", 3).stage == "firecrawl"
    assert first.calls == [], "the vendor later in the chain must not be dialed"


def test_a_typo_in_the_order_is_reported_once_at_startup(build, caplog):
    """`fircrawl` used to cost nothing at startup and everything at the first 402."""
    stage = Only("firecrawl", search=True, extract=False)
    with caplog.at_level(logging.WARNING):
        router = build(search=("firecrawl", "fircrawl"), extract=(), firecrawl=stage)

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("fircrawl" in message for message in warnings), warnings
    assert any("firecrawl" in message for message in warnings if "fircrawl" in message), \
        "the warning should list the stages that do exist"
    assert router.run_search("q", 3).stage == "firecrawl", "the typo must not break the rest"


def test_an_order_with_every_name_known_is_quiet(build, caplog):
    stage = Only("exa", search=True, extract=False)
    with caplog.at_level(logging.WARNING):
        build(search=("exa",), extract=("exa",), exa=stage)
    assert not [r for r in caplog.records if "stage order" in r.getMessage()]


def test_the_cli_sees_the_same_skip_reasons(build):
    """`quotarouter route` prints exactly what the router decided, not a second opinion."""
    stage = Only("exa", search=True, extract=False)
    router = build(search=("exa", "nope"), extract=(), exa=stage)

    skips = dict(router.skips("search"))
    assert "unknown stage" in skips["nope"]
