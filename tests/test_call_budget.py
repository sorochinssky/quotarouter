"""Budgets: "no money" and "not more than this", and the difference between them.

The stages do not count the same unit — some spend USD, some vendor credits, some nothing at all —
so a dollar ceiling cannot price a credit-metered stage without inventing an exchange rate. That
rule, the free-grant discipline behind it, and the refusal that happens before a vendor is dialed
are what this file pins down. A budget is a ceiling on one call; it is not a depth setting.
"""

from __future__ import annotations

import pytest

from quotarouter import config, ledger as ledger_mod, routing, types
from quotarouter.stages.base import Capability, Stage


class Paid(Stage):
    """A stage with an exact unit, funding kind and allowance."""

    key_env = ""

    def __init__(self, name, *, unit="credits", search_units=1.0, extract_units=1.0,
                 funding="free", allowance=None, fail=False):
        self.name = name
        self.capability = Capability(search=True, extract=True,
                                     search_units=search_units, extract_units=extract_units)
        self.unit = unit
        self.funding = funding
        self.monthly_allowance = allowance
        self.fail = fail
        self.calls: list[str] = []

    def search(self, query, limit):
        self.calls.append("search")
        if self.fail:
            raise types_failure("never")
        return types.search_ok([{"title": self.name, "url": f"https://{self.name}.example/"}])

    def extract(self, urls):
        self.calls.append("extract")
        if self.fail:
            raise types_failure("never")
        return [types.document(url, self.name, "body " * 20, source_url=url) for url in urls]


def types_failure(message):
    from quotarouter import errors

    return errors.TransientError(message)


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_KEYLESS_LAST_RESORT", "0")
    monkeypatch.setenv("QUOTAROUTER_ALLOW_KEYLESS", "0")

    def _make(order, **stages):
        settings = config.Settings(search_order=tuple(order), extract_order=tuple(order),
                                   state_dir=tmp_path)
        ledger = ledger_mod.Ledger(tmp_path / "ledger.json")
        return routing.Router(settings=settings, stages=stages, ledger=ledger), ledger

    return _make


# --- budget=free --------------------------------------------------------------------------
def test_a_balance_funded_stage_never_serves_a_free_call(make):
    router, _ = make(["deepseek", "exa"],
                     deepseek=Paid("deepseek", unit="usd", funding="balance", search_units=0.001),
                     exa=Paid("exa", unit="usd", search_units=0.007, allowance=10.0))
    result = router.run_search("q", 3, budget="free")

    assert result.ok and result.stage == "exa"
    assert result.budget == "free"
    assert router.stages["deepseek"].calls == [], "money on the balance is still money"
    assert "balance-funded" in result.summary()


def test_the_same_call_without_a_budget_may_spend_the_balance(make):
    router, _ = make(["deepseek"],
                     deepseek=Paid("deepseek", unit="usd", funding="balance", search_units=0.001))
    result = router.run_search("q", 3)

    assert result.ok and result.stage == "deepseek"
    assert result.budget is None


def test_an_unmetered_stage_is_free(make):
    """Jina and the anonymous tier cost nothing: they need no grant to qualify."""
    router, _ = make(["jina"], jina=Paid("jina", unit="requests", allowance=None))
    assert router.run_search("q", 3, budget="free").stage == "jina"


def test_a_spent_free_grant_is_skipped_rather_than_overrun(make):
    """The vendor window can be wider than the free grant; the grant is the line that matters."""
    router, ledger = make(["exa", "jina"],
                          exa=Paid("exa", unit="credits", allowance=50.0),
                          jina=Paid("jina", unit="requests"))
    ledger.set_limit("exa", 50.0, source="local")
    ledger.charge("exa", 50.0)

    result = router.run_search("q", 3, budget="free")

    assert result.stage == "jina"
    assert router.stages["exa"].calls == []
    assert "free grant spent" in result.summary(), result.summary()


def test_a_grant_with_room_is_used(make):
    router, ledger = make(["exa"], exa=Paid("exa", unit="credits", allowance=100.0))
    ledger.set_limit("exa", 100.0, source="local")
    ledger.charge("exa", 99.0)

    assert router.run_search("q", 3, budget="free").stage == "exa"


def test_a_free_call_still_charges_the_ledger(make):
    router, ledger = make(["exa"], exa=Paid("exa", unit="credits", allowance=100.0))
    ledger.set_limit("exa", 100.0, source="local")

    router.run_search("q", 3, budget="free")
    assert ledger.get("exa").used == pytest.approx(1.0)


# --- the ceiling --------------------------------------------------------------------------
def test_a_ceiling_above_the_estimate_lets_the_call_through(make):
    router, _ = make(["exa"], exa=Paid("exa", unit="usd", search_units=0.007))
    result = router.run_search("q", 3, max_cost_usd=0.01)

    assert result.ok and result.max_cost_usd == 0.01


def test_an_estimate_equal_to_the_ceiling_passes(make):
    router, _ = make(["exa"], exa=Paid("exa", unit="usd", search_units=0.01))
    assert router.run_search("q", 3, max_cost_usd=0.01).ok


def test_one_cent_above_the_ceiling_is_refused_before_dialing(make):
    router, _ = make(["exa"], exa=Paid("exa", unit="usd", search_units=0.0101))
    result = router.run_search("q", 3, max_cost_usd=0.01)

    assert not result.ok
    assert router.stages["exa"].calls == [], "the ceiling must be checked before the wire"
    assert "exceeds the ceiling" in result.summary(), result.summary()


def test_the_refusal_names_the_estimate_and_the_ceiling(make):
    router, _ = make(["exa"], exa=Paid("exa", unit="usd", search_units=0.25))
    result = router.run_search("q", 3, max_cost_usd=0.01)
    text = result.summary()

    assert "0.2500" in text and "0.0100" in text


def test_a_ceiling_counts_pages_not_calls(make):
    """Extract is priced per page: five pages at 0.01 cost five times one search."""
    router, _ = make(["exa"], exa=Paid("exa", unit="usd", extract_units=0.01))
    result = router.run_extract(["https://a", "https://b", "https://c", "https://d"],
                                max_cost_usd=0.03)

    assert not result.ok
    assert router.stages["exa"].calls == []


def test_a_credit_metered_stage_is_not_priced_at_a_guess(make):
    """A dollar ceiling cannot price credits; such a stage is held to its free grant instead."""
    router, ledger = make(["firecrawl"],
                          firecrawl=Paid("firecrawl", unit="credits", allowance=1000.0))

    ledger.set_limit("firecrawl", 1000.0, source="local")
    assert router.run_search("q", 3, max_cost_usd=0.0001).stage == "firecrawl"

    ledger.charge("firecrawl", 999.0)
    second = router.run_search("q", 3, max_cost_usd=0.0001)
    assert not second.ok
    assert "cannot price vendor credits" in second.summary(), second.summary()


def test_free_and_ceiling_together_do_not_add_features(make):
    """The stricter budget wins, and `free` makes a ceiling irrelevant."""
    router, _ = make(["exa"], exa=Paid("exa", unit="usd", search_units=0.007))
    result = router.run_search("q", 3, budget="free", max_cost_usd=5.0)

    assert result.budget == "free"
    assert result.max_cost_usd is None
