"""What the ledger is charged, and what an "error page" means to the router.

These two things were wrong in ways no test noticed: a six-page extract charged one credit, and
a vendor-level refusal delivered per page was filed as a bad page instead of pausing the stage.
Both make the router's quota arithmetic optimistic — the one thing it exists to get right.
"""

from __future__ import annotations

import json

import httpx
import pytest

from quotarouter import config, http_client, routing


@pytest.fixture(autouse=True)
def firecrawl_only(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setenv("ROUTER_EXTRACT_ORDER", "firecrawl")
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "firecrawl")
    config.reset_settings_for_tests()
    yield
    http_client.set_transport(None)
    config.reset_settings_for_tests()


def firecrawl_scrape(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content.decode("utf-8"))
    url = body["url"]
    return httpx.Response(200, json={"success": True, "data": {
        "markdown": f"text of {url} " * 20,
        "metadata": {"sourceURL": url, "title": url, "statusCode": 200},
    }})


def test_reset_transport_really_drops_the_mock():
    """A leftover mock used to make a "live" run answer from fixtures without a word."""
    http_client.set_transport(httpx.MockTransport(lambda request: httpx.Response(200, json={})))
    assert http_client._client is not None
    http_client.reset_transport()
    assert http_client._client is None, "reset must drop the client that carries the mock"


def test_extract_charges_one_unit_per_delivered_page(use_transport, tmp_path):
    use_transport(firecrawl_scrape)
    router = routing.Router(settings=config.settings(refresh=True))
    result = router.run_extract(["https://a.example/", "https://b.example/", "https://c.example/"])
    assert result.ok and len(result.payload) == 3, result.chain_text()

    used = router.ledger.get("firecrawl").used
    assert used == 3.0, f"three pages through a per-page vendor must cost three credits, got {used}"


def test_extract_charges_nothing_for_pages_the_site_refused(use_transport):
    """A 404 from the site is final and free: no vendor credit, no next stage."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        if body["url"].endswith("gone"):
            return httpx.Response(200, json={"success": True, "data": {
                "markdown": "", "metadata": {"statusCode": 404, "sourceURL": body["url"]}}})
        return firecrawl_scrape(request)

    use_transport(handler)
    router = routing.Router(settings=config.settings(refresh=True))
    result = router.run_extract(["https://ok.example/", "https://gone.example/gone"])
    assert len(result.payload) == 2
    assert router.ledger.get("firecrawl").used == 1.0, "only the page that came out is charged"


def test_vendor_quota_refusal_parks_the_stage_as_quota(use_transport):
    """Per-page delivery must not disguise a dead account as a bad page."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"success": False, "error": "Insufficient credits"})

    use_transport(handler)
    router = routing.Router(settings=config.settings(refresh=True))
    result = router.run_extract(["https://a.example/", "https://b.example/"])

    assert not result.ok
    chain = result.chain_text()
    assert "quota" in chain, chain
    assert "bad_page" not in chain, chain
    state = router.ledger.get("firecrawl")
    assert state.pause_kind == "quota", state.pause_kind


def test_page_specific_refusal_still_does_not_pause(use_transport):
    """The other half of the rule: a closed page is not a broken vendor."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"success": True, "data": {
            "markdown": "", "metadata": {"statusCode": 403, "sourceURL": body["url"]}}})

    use_transport(handler)
    router = routing.Router(settings=config.settings(refresh=True))
    result = router.run_extract(["https://closed.example/"])

    assert "403 from origin" in result.payload[0]["error"]
    assert router.ledger.get("firecrawl").pause_kind == "", "a 403 from the site must not park the vendor"


def test_firecrawl_probe_does_not_let_a_bonus_inflate_the_window(use_transport):
    """A bonus can push remaining above the plan size; the window stays the plan size."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "data": {
            "remainingCredits": 1200, "planCredits": 1000,
            "billingPeriodEnd": "2026-11-30T07:28:51Z"}})

    use_transport(handler)
    router = routing.Router(settings=config.settings(refresh=True))
    report = router.probe_all(force=True)
    state = router.ledger.get("firecrawl")
    assert state.limit == 1000.0
    assert state.remaining() == 1000.0, "the plan cap holds; a bonus is not extra window"
    assert report["firecrawl"]["remaining"] == 1000.0


def test_two_processes_do_not_lose_each_others_charges(tmp_path):
    """The gateway plugin, a cron probe and the MCP server all write the same ledger.

    Two `Ledger` objects in one interpreter stand in for two processes: each holds its own
    in-memory copy and its own lock, exactly like separate processes do. Without a
    cross-process lock and a re-read before writing, the second charge overwrites the first and
    the ledger believes a vendor still has quota it already spent.
    """
    from quotarouter.ledger import Ledger

    path = tmp_path / "ledger.json"
    first, second = Ledger(path), Ledger(path)
    first.set_limit("firecrawl", 100.0)
    second.set_limit("firecrawl", 100.0)          # both have the state in memory now
    first.charge("firecrawl", 3.0)
    second.charge("firecrawl", 2.0)

    assert Ledger(path).get("firecrawl").used == 5.0


def test_a_reported_balance_wins_over_the_local_counter(tmp_path):
    from quotarouter.ledger import Ledger

    path = tmp_path / "ledger.json"
    ledger = Ledger(path)
    ledger.set_limit("firecrawl", 1000.0)
    ledger.charge("firecrawl", 4.0)
    ledger.set_remaining("firecrawl", 955.0)
    assert Ledger(path).get("firecrawl").used == 45.0, "the vendor's own number wins"
