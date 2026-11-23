"""Adversarial and recorded-response tests: the chain must survive what vendors really send.

Level 2 (recorded payloads from `tests/fixtures`) and level 4 (adversarial: a "success" that
carries nothing, a 200 with an error body, a hang, a schema change) live here.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from quotarouter import config, http_client, routing, types
from conftest import json_response, text_response

FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def fixture_text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


# --- recorded vendor answers -------------------------------------------------------------
def test_recorded_exa_404_falls_to_recorded_firecrawl(keys, monkeypatch):
    """Exa exhausted (real 402 body) -> Firecrawl serves (real search body)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.exa.ai":
            return json_response(fixture("exa-402.json"), status=402)
        if request.url.path.endswith("/credit-usage"):
            return json_response(fixture("firecrawl-usage.json"))
        return json_response(fixture("firecrawl-search-ok.json"))

    keys(EXA_API_KEY="k", FIRECRAWL_API_KEY="k")
    http_client.set_transport(httpx.MockTransport(handler))
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa,firecrawl")
    monkeypatch.setenv("ROUTER_KEYLESS_LAST_RESORT", "false")
    config.reset_settings_for_tests()

    result = routing.Router().run_search("example", 5)
    assert result.stage == "firecrawl"
    assert result.payload["data"]["web"][0]["url"] == "https://example.com/"


def test_recorded_jina_js_wall_moves_to_firecrawl(keys, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "r.jina.ai":
            return text_response(fixture_text("jina-js-wall.txt"))
        if request.url.path.endswith("/credit-usage"):
            return json_response(fixture("firecrawl-usage.json"))
        return json_response(fixture("firecrawl-scrape-ok.json"))

    keys(FIRECRAWL_API_KEY="k")
    http_client.set_transport(httpx.MockTransport(handler))
    monkeypatch.setenv("ROUTER_EXTRACT_ORDER", "jina,firecrawl")
    config.reset_settings_for_tests()

    result = routing.Router().run_extract(["https://protected.example/"])
    assert result.stage == "firecrawl"
    assert "Example Domain" in result.payload[0]["content"]


def test_recorded_deepseek_zero_balance_parks_the_stage(keys, monkeypatch):
    """A real `is_available: false` balance must park the stage instead of dialing it."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/balance"):
            return json_response(fixture("deepseek-balance-empty.json"))
        if request.url.host == "api.exa.ai":
            return json_response(fixture("exa-search-ok.json"))
        raise AssertionError("deepseek search must not be dialed with an empty balance")

    keys(DEEPSEEK_API_KEY="k", EXA_API_KEY="k")
    http_client.set_transport(httpx.MockTransport(handler))
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "deepseek,exa")
    config.reset_settings_for_tests()
    router = routing.Router()

    assert router.run_search("q", 3).stage == "exa"
    deepseek = router.ledger.get("deepseek")
    # Parked by the machine, not stopped by an operator: the pause has a deadline and will be
    # re-probed. `disabled` here used to mean `inf`, which only a human could lift.
    assert deepseek.pause_kind == types.QUOTA
    assert deepseek.pause_origin == "auto"
    assert deepseek.paused_until and deepseek.paused_until != float("inf")
    assert "balance empty" in deepseek.pause_reason


# --- adversarial -----------------------------------------------------------------------
def test_success_true_but_no_rows_is_not_success(keys, monkeypatch):
    """Vendors do answer `{"success": true, "data": {"web": []}}` — that is a failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.exa.ai":
            return json_response({"results": []})
        if request.url.path.endswith("/credit-usage"):
            return json_response(fixture("firecrawl-usage.json"))
        return json_response(fixture("firecrawl-search-ok.json"))

    keys(EXA_API_KEY="k", FIRECRAWL_API_KEY="k")
    http_client.set_transport(httpx.MockTransport(handler))
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa,firecrawl")
    config.reset_settings_for_tests()

    result = routing.Router().run_search("q", 3)
    assert result.stage == "firecrawl"
    assert "empty" in result.summary()


def test_http_200_with_an_error_body_is_handled(keys, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.exa.ai":
            return json_response({"error": "invalid api key"})
        if request.url.path.endswith("/credit-usage"):
            return json_response(fixture("firecrawl-usage.json"))
        return json_response(fixture("firecrawl-search-ok.json"))

    keys(EXA_API_KEY="k", FIRECRAWL_API_KEY="k")
    http_client.set_transport(httpx.MockTransport(handler))
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa,firecrawl")
    config.reset_settings_for_tests()

    result = routing.Router().run_search("q", 3)
    assert result.ok and result.stage == "firecrawl"


def test_connection_error_parks_and_moves_on(keys, monkeypatch):
    calls = {"exa": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.exa.ai":
            calls["exa"] += 1
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path.endswith("/credit-usage"):
            return json_response(fixture("firecrawl-usage.json"))
        return json_response(fixture("firecrawl-search-ok.json"))

    keys(EXA_API_KEY="k", FIRECRAWL_API_KEY="k")
    http_client.set_transport(httpx.MockTransport(handler))
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa,firecrawl")
    config.reset_settings_for_tests()
    router = routing.Router()

    assert router.run_search("q", 3).stage == "firecrawl"
    assert router.ledger.get("exa").is_paused(), "a dead host must be parked, not retried forever"
    router.run_search("q2", 3)
    assert calls["exa"] == 1, "the parked host must not be dialed again"


def test_vendor_schema_change_does_not_crash_the_chain(keys, monkeypatch):
    """An unexpected payload (new field names) must degrade to "no results", not an exception."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.exa.ai":
            return json_response({"hits": [{"link": "https://x"}]})  # not `results`
        if request.url.path.endswith("/credit-usage"):
            return json_response(fixture("firecrawl-usage.json"))
        return json_response(fixture("firecrawl-search-ok.json"))

    keys(EXA_API_KEY="k", FIRECRAWL_API_KEY="k")
    http_client.set_transport(httpx.MockTransport(handler))
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa,firecrawl")
    config.reset_settings_for_tests()

    result = routing.Router().run_search("q", 3)
    assert result.stage == "firecrawl"


def test_keyless_last_resort_served_and_labelled(keys, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.exa.ai":
            return json_response(fixture("exa-402.json"), status=402)
        if request.url.host == "mcp.exa.ai":
            rows = [{"title": "T", "url": "https://free.example/", "snippet": "s"}]
            return json_response({"jsonrpc": "2.0", "id": 1, "result": {"content": [
                {"type": "text", "text": json.dumps(rows)}]}})
        raise AssertionError(f"unexpected host {request.url.host}")

    keys(EXA_API_KEY="k")
    http_client.set_transport(httpx.MockTransport(handler))
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa")
    config.reset_settings_for_tests()

    result = routing.Router().run_search("q", 3)
    assert result.stage == "keyless"
    assert result.payload["data"]["web"][0]["url"] == "https://free.example/"


def test_keyless_off_means_honest_failure(keys, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(fixture("exa-402.json"), status=402)

    keys(EXA_API_KEY="k")
    http_client.set_transport(httpx.MockTransport(handler))
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa")
    monkeypatch.setenv("ROUTER_KEYLESS_LAST_RESORT", "false")
    config.reset_settings_for_tests()

    result = routing.Router().run_search("q", 3)
    assert not result.ok
    assert "quota" in routing.format_failure(result, "search")


def test_route_log_records_every_stage(keys, monkeypatch, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.exa.ai":
            return json_response(fixture("exa-402.json"), status=402)
        if request.url.path.endswith("/credit-usage"):
            return json_response(fixture("firecrawl-usage.json"))
        return json_response(fixture("firecrawl-search-ok.json"))

    keys(EXA_API_KEY="k", FIRECRAWL_API_KEY="k")
    http_client.set_transport(httpx.MockTransport(handler))
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa,firecrawl")
    config.reset_settings_for_tests()
    router = routing.Router()

    router.log_route("search", router.run_search("q", 3), {"query": "q"})
    record = json.loads((tmp_path / "state" / "routes.jsonl").read_text(encoding="utf-8").strip())
    assert record["served_by"] == "firecrawl"
    assert record["chain"][0].startswith("exa: quota")
    assert record["chain"][1] == "firecrawl=ok"
