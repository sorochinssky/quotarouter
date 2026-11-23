"""The stage base class and the router's last-resort protection.

Two things live here that the chain depends on and nobody looks at: how pages are fetched in
parallel without losing the caller's order, and what happens when an adapter is simply broken.
"""

from __future__ import annotations

import threading

import pytest

from quotarouter import config, routing, types
from quotarouter.stages.base import Stage


class Stub(Stage):
    name = "stub"
    key_env = "STUB_KEY"

    def search(self, query, limit):
        return types.search_ok([])

    def extract(self, urls):
        return []


# --- identity -----------------------------------------------------------------------------
def test_a_stage_without_its_key_says_which_key(monkeypatch):
    monkeypatch.delenv("STUB_KEY", raising=False)
    assert Stub().not_ready_reason() == "no STUB_KEY set"
    assert Stub().has_credential() is False


def test_a_stage_with_a_key_is_ready(monkeypatch):
    monkeypatch.setenv("STUB_KEY", "x")
    assert Stub().not_ready_reason() == ""
    assert Stub().has_credential() is True


def test_a_stage_with_no_key_requirement_is_always_ready():
    class Keyless(Stage):
        name = "nl"
        key_env = ""

    assert Keyless().has_credential() is True


def test_the_default_capability_claims_search_only():
    """The default is search: a stage must opt in to extract, which costs real money per page."""
    assert Stub().supports_search() is True
    assert Stub().supports_extract() is False


# --- parallel fetching --------------------------------------------------------------------
def test_pages_come_back_in_the_order_they_were_asked_for():
    """Parallel fetching reorders completion, never results."""
    def fetch(url: str) -> dict:
        if url.endswith("slow"):
            threading.Event().wait(0.05)
        return {"url": url, "content": "x"}

    pages = Stage._map_urls(["https://slow", "https://fast", "https://mid"], fetch)
    assert [page["url"] for page in pages] == ["https://slow", "https://fast", "https://mid"]


def test_one_worker_means_strictly_sequential(monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_EXTRACT_WORKERS", "1")
    config.reset_settings_for_tests()
    seen: list[str] = []

    def fetch(url: str) -> dict:
        seen.append(url)
        return {"url": url, "content": "x"}

    Stage._map_urls(["a", "b", "c"], fetch)
    assert seen == ["a", "b", "c"]


def test_an_empty_url_list_needs_no_work():
    assert Stage._map_urls([], lambda url: {"url": url}) == []


# --- a broken adapter ---------------------------------------------------------------------
def _broken_router(tmp_path, monkeypatch):
    from quotarouter.stages.base import Capability

    class Broken(Stage):
        name = "broken"
        key_env = ""
        capability = Capability(search=True, extract=False, search_units=1.0)
        unit = "requests"

        def search(self, query, limit):
            raise RuntimeError("adapter bug: 'NoneType' object has no attribute 'get'")

    monkeypatch.setenv("QUOTAROUTER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("QUOTAROUTER_ALLOW_KEYLESS", "0")
    monkeypatch.setenv("QUOTAROUTER_SEARCH_ORDER", "broken")
    config.reset_settings_for_tests()
    return routing.Router(settings=config.settings(refresh=True), stages={"broken": Broken()})


def test_an_adapter_bug_does_not_kill_the_call_and_parks_the_stage(tmp_path, monkeypatch):
    router = _broken_router(tmp_path, monkeypatch)
    result = router.run_search("q", 3)
    assert not result.ok
    assert result.attempts[0].reason.startswith("crashed:")
    state = router.ledger.get("broken")
    assert state.is_paused(router.now())
    assert state.pause_kind == types.TRANSIENT
