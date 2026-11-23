"""Resolution: one chain per call, decided before anything is dialed, refused when impossible.

Three sources (an explicit list, a named chain, the default order), at most one of them naming
stages. A mistake in the request is the caller's, so it is a `ValueError` — raised before the first
network call, naming the argument — not a per-stage reason the caller has to decode.
"""

from __future__ import annotations

import pytest

from quotarouter import config, ledger as ledger_mod, routing, types
from quotarouter.stages.base import Capability, Stage


class Fake(Stage):
    key_env = ""

    def __init__(self, name, search=True, extract=False, unit="requests",
                 search_units=1.0, extract_units=1.0, funding="free",
                 allowance=None, label=""):
        self.name = name
        self.capability = Capability(search=search, extract=extract,
                                     search_units=search_units, extract_units=extract_units)
        self.unit = unit
        self.funding = funding
        self.monthly_allowance = allowance
        self.calls: list[str] = []
        self.label = label or name

    def search(self, query, limit):
        self.calls.append("search")
        return types.search_ok([{"title": self.label, "url": f"https://{self.name}.example/"}])

    def extract(self, urls):
        self.calls.append("extract")
        return [types.document(url, self.label, "body " * 20, source_url=url) for url in urls]


CHAIN_FILE = """
[chains.fast]
search = ["firecrawl"]

[chains.free]
search = ["exa"]
extract = ["jina"]
budget = "free"

[chains.loose]
search = ["firecrawl"]
strict = false

[chains.pages]
extract = ["jina"]

[chains.capped]
search = ["exa"]
max_cost_usd = 0.05
"""


@pytest.fixture
def make(tmp_path, monkeypatch):
    # The last-resort gate is `KEYLESS_LAST_RESORT`; `ALLOW_KEYLESS` is the stage's own credential.
    monkeypatch.setenv("QUOTAROUTER_KEYLESS_LAST_RESORT", "0")
    monkeypatch.setenv("QUOTAROUTER_ALLOW_KEYLESS", "0")
    path = tmp_path / "chains.toml"
    path.write_text(CHAIN_FILE, encoding="utf-8")

    def _make(**overrides):
        stages = overrides.pop("stages", None)
        if stages is None:
            stages = {
                "firecrawl": Fake("firecrawl", extract=True, unit="credits"),
                "exa": Fake("exa", unit="usd", search_units=0.007),
                "jina": Fake("jina", search=False, extract=True),
                "deepseek": Fake("deepseek", extract=True, unit="usd", funding="balance",
                                 search_units=0.00075),
                "keyless": Fake("keyless", extract=True, search_units=0.0, extract_units=0.0),
            }
        settings = config.Settings(
            search_order=("exa", "firecrawl"), extract_order=("jina", "firecrawl"),
            state_dir=tmp_path, chains_path=path, **overrides)
        return routing.Router(settings=settings, stages=stages,
                              ledger=ledger_mod.Ledger(tmp_path / "ledger.json"))

    return _make


# --- the default is untouched -------------------------------------------------------------
def test_nothing_asked_for_means_the_configured_order(make):
    router = make()
    resolved = router.resolve("search")

    assert resolved.mode == "default"
    assert resolved.names == ("exa", "firecrawl")
    assert resolved.fallback_names == ()
    assert resolved.budget is None and resolved.max_cost_usd is None
    assert resolved.allow_keyless is False, "follows QUOTAROUTER_KEYLESS_LAST_RESORT here"


def test_the_default_call_still_serves(make):
    router = make()
    result = router.run_search("q", 3)

    assert result.ok and result.stage == "exa"
    assert result.chain_mode == "default"
    assert result.requested == []


# --- an explicit list ---------------------------------------------------------------------
def test_one_name_is_a_chain(make):
    router = make()
    result = router.run_search("q", 3, stages=["firecrawl"])

    assert result.ok and result.stage == "firecrawl"
    assert result.chain_mode == "custom"
    assert result.requested == ["firecrawl"]


def test_the_named_list_is_walked_in_order(make):
    stages = {"exa": Fake("exa"), "firecrawl": Fake("firecrawl")}
    router = make(stages=stages)

    router.run_search("q", 3, stages=["firecrawl", "exa"])
    assert stages["firecrawl"].calls == ["search"]
    assert stages["exa"].calls == [], "in a chain, the second stage is only a fallback"


def test_a_string_instead_of_a_list_is_refused(make):
    with pytest.raises(ValueError, match="must be a list"):
        make().resolve("search", stages="firecrawl")


def test_an_empty_list_is_refused(make):
    with pytest.raises(ValueError, match="empty"):
        make().resolve("search", stages=[])


def test_an_unknown_name_is_refused_with_the_list_of_real_ones(make):
    with pytest.raises(ValueError) as excinfo:
        make().resolve("search", stages=["fircrawl"])
    message = str(excinfo.value)
    assert "fircrawl" in message and "firecrawl" in message


def test_a_capability_mismatch_is_refused_before_the_call(make):
    with pytest.raises(ValueError, match="does not support extract"):
        make().resolve("extract", stages=["exa"])


def test_a_list_and_a_name_together_are_refused(make):
    with pytest.raises(ValueError, match="mutually exclusive"):
        make().resolve("search", stages=["exa"], chain="fast")


# --- named chains -------------------------------------------------------------------------
def test_a_named_chain_resolves_to_its_own_list(make):
    router = make()
    result = router.run_search("q", 3, chain="fast")

    assert result.stage == "firecrawl"
    assert result.chain_mode == "named"
    assert result.requested == ["fast"]


def test_an_unknown_chain_lists_what_exists(make):
    with pytest.raises(ValueError) as excinfo:
        make().resolve("search", chain="nope")
    message = str(excinfo.value)
    assert "nope" in message and "fast" in message and "free" in message


def test_a_search_only_chain_refuses_an_extract(make):
    with pytest.raises(ValueError) as excinfo:
        make().resolve("extract", chain="fast")
    assert "no extract stages" in str(excinfo.value)


def test_a_loose_chain_falls_back_on_its_own(make):
    resolved = make().resolve("search", chain="loose")

    assert resolved.mode == "named"
    assert resolved.names == ("firecrawl",)
    assert resolved.fallback_names == ("exa",), "the default order minus what was already tried"


def test_a_strict_chain_has_no_tail(make):
    assert make().resolve("search", chain="fast").fallback_names == ()


def test_fallback_is_opt_in_for_an_explicit_list(make):
    resolved = make().resolve("search", stages=["firecrawl"], fallback=True)
    assert resolved.fallback_names == ("exa",)

    strict = make().resolve("search", stages=["firecrawl"])
    assert strict.fallback_names == ()


def test_fallback_never_repeats_a_stage(make):
    resolved = make().resolve("search", stages=["exa", "firecrawl"], fallback=True)
    assert resolved.fallback_names == ()


# --- the operator's kill switch -----------------------------------------------------------
def test_overrides_can_be_disabled_and_the_refusal_names_the_setting(make):
    router = make(allow_chain_override=False)

    with pytest.raises(ValueError, match="QUOTAROUTER_ALLOW_CHAIN_OVERRIDE"):
        router.resolve("search", stages=["exa"])
    with pytest.raises(ValueError, match="QUOTAROUTER_ALLOW_CHAIN_OVERRIDE"):
        router.resolve("search", chain="fast")


def test_a_budget_alone_is_not_an_override(make):
    """Restricting a call cannot pin an expensive vendor, so the kill switch does not apply."""
    resolved = make(allow_chain_override=False).resolve("search", budget="free")
    assert resolved.mode == "default"
    assert resolved.budget == "free"


# --- budget plumbing ----------------------------------------------------------------------
@pytest.mark.parametrize("chain_budget,chain_ceiling,call_budget,call_ceiling,"
                         "expect_budget,expect_ceiling", [
    (None, None, None, None, None, None),
    (None, None, "free", None, "free", None),
    (None, None, None, 0.01, None, 0.01),
    ("free", None, None, 0.5, "free", None),
    ("free", None, None, None, "free", None),
    (None, 0.05, None, 0.01, None, 0.01),
    (None, 0.05, None, 0.5, None, 0.05),
    (None, 0.05, "free", None, "free", None),
])
def test_the_stricter_budget_wins(chain_budget, chain_ceiling, call_budget, call_ceiling,
                                  expect_budget, expect_ceiling):
    assert routing._stricter(call_budget, call_ceiling, chain_budget,
                             chain_ceiling) == (expect_budget, expect_ceiling)


def test_a_chain_budget_cannot_be_loosened_by_the_caller(make):
    resolved = make().resolve("search", chain="free", max_cost_usd=5.0)
    assert resolved.budget == "free"
    assert resolved.max_cost_usd is None


def test_a_chain_ceiling_can_be_tightened(make):
    resolved = make().resolve("search", chain="capped", max_cost_usd=0.001)
    assert resolved.max_cost_usd == 0.001


@pytest.mark.parametrize("kwargs,fragment", [
    ({"budget": "cheap"}, "budget must be one of"),
    ({"max_cost_usd": -1}, "negative"),
    ({"max_cost_usd": "cheap"}, "must be a number"),
    ({"max_cost_usd": True}, "must be a number"),
])
def test_a_malformed_budget_is_a_caller_error(make, kwargs, fragment):
    with pytest.raises(ValueError, match=fragment):
        make().resolve("search", **kwargs)


def test_a_free_budget_with_one_balance_stage_is_impossible(make):
    """`deepseek` is prepaid money: asking for it under budget=free contradicts itself."""
    with pytest.raises(ValueError) as excinfo:
        make().resolve("search", stages=["deepseek"], budget="free")
    message = str(excinfo.value)
    assert "budget=free" in message and "deepseek" in message
