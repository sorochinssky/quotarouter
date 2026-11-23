"""Behaviour that only shows up when several calls share one ledger.

The unit tests cover one stage answering one call. These cover the states the router is
actually in during a bad day: every page of a batch refused by the vendor, the anonymous tier
turned off, a second process having spent credits meanwhile.
"""

from __future__ import annotations

import json

import httpx
import pytest

from quotarouter import config, http_client, ledger as ledger_mod, routing, types
from quotarouter.stages.firecrawl import FirecrawlStage


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("FIRECRAWL_API_KEY", "k")
    monkeypatch.setenv("QUOTAROUTER_ALLOW_KEYLESS", "0")
    config.reset_settings_for_tests()
    yield tmp_path
    config.reset_settings_for_tests()


def router_with(stages: dict, tmp_path) -> routing.Router:
    settings = config.settings(refresh=True)
    return routing.Router(settings=settings, stages=stages)


def test_a_vendor_refusal_on_every_page_parks_the_stage_with_that_kind(workdir):
    """A 402 per page is the vendor saying "no money", not the site saying "go away". Parking it
    as `bad_page` would keep it in the chain and spend a request on it every call."""
    def handler(request):
        return httpx.Response(402, content=b'{"error": "Insufficient credits"}',
                              headers={"content-type": "application/json"})

    http_client.set_transport(httpx.MockTransport(handler))
    router = router_with({"firecrawl": FirecrawlStage()}, workdir)
    result = router.run_extract(["https://a", "https://b"])

    assert result.payload and all(page.get("error") for page in result.payload)
    state = router.ledger.get("firecrawl")
    assert state.is_paused(router.now())
    assert state.pause_kind == types.QUOTA, state.pause_kind


def test_an_origin_refusal_on_every_page_does_not_park_the_stage(workdir):
    """The site answering 403 is a page result — the vendor is healthy, so it must stay usable."""
    def handler(request):
        # Firecrawl answers 200 with `success: false` and the origin's status when the SITE
        # refused the page; that is the shape the router has to recognise.
        return httpx.Response(200, content=json.dumps(
            {"success": True, "data": {"markdown": "", "metadata": {"statusCode": 403}}}).encode(),
            headers={"content-type": "application/json"})

    http_client.set_transport(httpx.MockTransport(handler))
    router = router_with({"firecrawl": FirecrawlStage()}, workdir)
    result = router.run_extract(["https://a"])
    assert "403 from origin" in result.payload[0].get("error", "")
    assert not router.ledger.get("firecrawl").is_paused(router.now())


def test_a_top_level_403_from_the_api_parks_it_as_a_key_problem(workdir):
    """A 403 on the API itself is about our key, not about one page: park, do not keep dialing."""
    def handler(request):
        return httpx.Response(403, content=b"forbidden", headers={"content-type": "text/plain"})

    http_client.set_transport(httpx.MockTransport(handler))
    router = router_with({"firecrawl": FirecrawlStage()}, workdir)
    router.run_extract(["https://a"])
    state = router.ledger.get("firecrawl")
    assert state.is_paused(router.now())
    assert state.pause_kind == types.AUTH


def test_the_anonymous_tier_is_not_used_when_the_operator_turned_it_off(workdir):
    monkeypatch_settings = config.settings(refresh=True)
    router = router_with({}, workdir)
    result = router.run_search("q", 3)
    assert not result.ok
    assert "keyless" not in [attempt.stage for attempt in result.attempts]


def test_a_charge_from_another_process_is_not_overwritten(workdir):
    """Two objects, one file: the second charge must land on top of the first, not replace it."""
    settings = config.settings(refresh=True)
    first = ledger_mod.Ledger(settings.ledger_path)
    first.set_limit("firecrawl", 10.0)
    second = ledger_mod.Ledger(settings.ledger_path)
    second.charge("firecrawl", 2.0)
    first.charge("firecrawl", 1.0)
    assert ledger_mod.Ledger(settings.ledger_path).get("firecrawl").used == 3.0


def test_the_audit_line_names_the_stage_that_served_and_the_chain(workdir):
    def handler(request):
        return httpx.Response(200, content=json.dumps(
            {"success": True, "data": {"markdown": "# Page", "metadata": {}}}).encode(),
            headers={"content-type": "application/json"})

    http_client.set_transport(httpx.MockTransport(handler))
    router = router_with({"firecrawl": FirecrawlStage()}, workdir)
    result = router.run_extract(["https://a"])
    router.log_route("extract", result, {"urls": ["https://a"]})

    line = json.loads((config.settings().log_path).read_text().splitlines()[-1])
    assert line["capability"] == "extract"
    assert line["served_by"] == "firecrawl"
    assert line["ok"] is True
    assert line["chain"] and isinstance(line["chain"], list)
