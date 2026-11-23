"""Walking a chosen chain: strictness, the opt-in tail, force, and what the audit says.

The point of a per-call chain is that it does what it says. So: a strict chain never quietly
reaches for the anonymous tier, a fallback never dials a stage twice, a forced attempt is one
attempt and is recorded as forced, and a batch split across two vendors says so per page instead
of naming one of them.
"""

from __future__ import annotations

import json
import threading

import pytest

from quotarouter import config, ledger as ledger_mod, routing, types
from quotarouter.stages.base import Capability, Stage


class Fake(Stage):
    key_env = ""

    def __init__(self, name, search=True, extract=False, answer=None, fail=None):
        self.name = name
        self.capability = Capability(search=search, extract=extract,
                                     search_units=1.0, extract_units=1.0)
        self.unit = "requests"
        self.funding = "free"
        self.monthly_allowance = None
        self.calls: list[str] = []
        self.answer = answer
        self.fail = fail

    def search(self, query, limit):
        self.calls.append("search")
        if self.fail is not None:
            raise self.fail
        if self.answer is not None:
            return self.answer
        return types.search_ok([{"title": self.name, "url": f"https://{self.name}.example/"}])

    def extract(self, urls):
        self.calls.append("extract")
        if self.fail is not None:
            raise self.fail
        if self.answer is not None:
            return self.answer(urls)
        return [types.document(url, self.name, "body " * 20, source_url=url) for url in urls]


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_KEYLESS_LAST_RESORT", "1")
    monkeypatch.setenv("QUOTAROUTER_ALLOW_KEYLESS", "1")

    def _make(order=("exa",), extract_order=("jina",), **stages):
        settings = config.Settings(search_order=tuple(order), extract_order=tuple(extract_order),
                                   state_dir=tmp_path)
        return routing.Router(settings=settings, stages=stages,
                              ledger=ledger_mod.Ledger(tmp_path / "ledger.json"))

    return _make


def _records(router):
    path = router.settings.log_path
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- strictness ---------------------------------------------------------------------------
def test_a_strict_chain_never_reaches_the_anonymous_tier(make):
    from quotarouter import errors

    keyless = Fake("keyless", extract=True)
    router = make(order=("exa",), exa=Fake("exa", fail=errors.TransientError("down")),
                  keyless=keyless)

    result = router.run_search("q", 3, stages=["exa"])

    assert not result.ok
    assert keyless.calls == [], "a chain of one means one; the free tier is not a safety net here"
    assert "transient: down" in result.summary()


def test_the_default_path_still_has_its_last_resort(make):
    from quotarouter import errors

    keyless = Fake("keyless", extract=True)
    router = make(order=("exa", "keyless"), exa=Fake("exa", fail=errors.TransientError("down")),
                  keyless=keyless)

    assert router.run_search("q", 3).stage == "keyless"


def test_fallback_walks_the_default_order_without_repeating_a_name(make):
    from quotarouter import errors

    exa = Fake("exa", fail=errors.TransientError("down"))
    firecrawl = Fake("firecrawl")
    router = make(order=("exa", "firecrawl"), exa=exa, firecrawl=firecrawl)

    result = router.run_search("q", 3, stages=["exa"], fallback=True)

    assert result.stage == "firecrawl"
    assert [a.stage for a in result.attempts if not a.reason.startswith("skipped")] == \
        ["exa", "firecrawl"]
    assert result.chain_mode == "custom"


def test_a_named_stage_is_not_dialed_twice_by_the_tail(make):
    from quotarouter import errors

    exa = Fake("exa", fail=errors.TransientError("down"))
    router = make(order=("exa",), exa=exa)

    router.run_search("q", 3, stages=["exa"], fallback=True)
    assert exa.calls == ["search"]


# --- force --------------------------------------------------------------------------------
def test_force_spends_one_attempt_on_a_parked_stage_and_records_it(make):
    from quotarouter import errors

    exa = Fake("exa", fail=errors.QuotaError("HTTP 402 out of credits"))
    router = make(order=("exa",), exa=exa)
    router.ledger.pause("exa", router.now() + 3600, types.QUOTA, "parked before the test",
                        origin=ledger_mod.ORIGIN_AUTO)

    forced = router.run_search("q", 3, stages=["exa"], ignore_pause=True)
    router.log_route("search", forced, {"query": "q"})

    assert exa.calls == ["search"], "exactly one attempt"
    assert not forced.ok
    assert router.ledger.get("exa").is_paused(router.now()), "a failed force re-parks the stage"
    record = _records(router)[-1]
    assert record["forced"] is True
    assert record["chain_mode"] == "custom"


def test_force_does_not_lift_an_operator_stop(make):
    exa = Fake("exa")
    router = make(order=("exa",), exa=exa)
    router.ledger.disable("exa", "we are leaving this vendor")

    result = router.run_search("q", 3, stages=["exa"], ignore_pause=True)

    assert not result.ok
    assert exa.calls == []
    assert "stopped by operator" in result.summary()


def test_a_forced_call_still_charges(make):
    router = make(order=("exa",), exa=Fake("exa"))
    router.ledger.pause("exa", router.now() + 3600, types.TRANSIENT, "temporary",
                        origin=ledger_mod.ORIGIN_AUTO)

    assert router.run_search("q", 3, stages=["exa"], ignore_pause=True).ok
    assert router.ledger.get("exa").used == pytest.approx(1.0)


# --- the audit trail ----------------------------------------------------------------------
def test_a_default_call_is_marked_default(make):
    router = make(order=("exa",), exa=Fake("exa"))
    router.log_route("search", router.run_search("q", 3), {"query": "q"})

    record = _records(router)[-1]
    assert record["chain_mode"] == "default"
    assert record["requested"] == []
    assert "budget" not in record and "forced" not in record


def test_a_free_call_records_its_budget(make):
    router = make(order=("exa",), exa=Fake("exa"))
    router.log_route("search", router.run_search("q", 3, budget="free"), {"query": "q"})

    assert _records(router)[-1]["budget"] == "free"


def test_a_batch_split_across_stages_records_each_count(make):
    urls = [f"https://p{i}.example/" for i in range(5)]

    def first(urls):
        return ([types.document(url, "jina", "body " * 20, source_url=url) for url in urls[:3]]
                + [types.page_error(url, "blocked for this domain") for url in urls[3:]])

    router = make(extract_order=("jina", "firecrawl"),
                  jina=Fake("jina", search=False, extract=True, answer=first),
                  firecrawl=Fake("firecrawl", extract=True))
    result = router.run_extract(urls, fallback=True)
    router.log_route("extract", result, {"urls": urls})

    assert result.ok
    assert result.delivered == {"jina": 3, "firecrawl": 2}
    record = _records(router)[-1]
    assert record["delivered"] == {"jina": 3, "firecrawl": 2}
    assert record["served_by"] == "firecrawl", "the last stage that delivered is the one named"


def test_a_single_vendor_batch_keeps_one_count(make):
    urls = ["https://a.example/", "https://b.example/"]
    router = make(extract_order=("jina",), jina=Fake("jina", search=False, extract=True))
    result = router.run_extract(urls, stages=["jina"])

    assert result.delivered == {"jina": 2}


# --- concurrency --------------------------------------------------------------------------
def test_eight_threads_three_chains_keep_their_own_stages(make):
    """A chain is resolved per call: the shared router must not remember the last choice."""
    barrier = threading.Barrier(8)
    stages = {name: Fake(name) for name in ("exa", "firecrawl", "jina")}
    router = make(order=("exa",), extract_order=("jina",), **stages)
    before = (router.settings.search_order, router.settings.extract_order)

    served: list[tuple[int, str]] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(index: int, chain: list[str]) -> None:
        try:
            barrier.wait(timeout=10)
            result = router.run_search("q", 3, stages=chain)
            with lock:
                served.append((index, result.stage))
        except BaseException as exc:  # a thread failure must surface in the test
            errors.append(exc)

    chains = [["exa"], ["firecrawl"], ["jina"]]
    threads = [threading.Thread(target=worker, args=(i, chains[i % 3])) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, errors
    assert len(served) == 8
    for index, stage in served:
        only = chains[index % 3][0]
        assert stage == only, f"thread {index} was pinned to {only} but was served by {stage}"
    for name, stage in stages.items():
        assert len(stage.calls) == sum(1 for _, s in served if s == name)
    assert (router.settings.search_order, router.settings.extract_order) == before
