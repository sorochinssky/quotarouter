"""Hermes integration contract + CLI.

These tests pin the shape Hermes consumes: the provider must expose the legacy envelope,
`is_available()` must never touch the network, and the tool wrapper must receive the exact
dict/list it expects. The CLI is exercised the same way a human runs it.
"""

from __future__ import annotations

import json

import httpx
import pytest

from quotarouter import cli, config, types
from quotarouter.integrations.hermes import RouterProvider
from conftest import json_response


def test_provider_registers_under_the_name_hermes_will_configure():
    assert RouterProvider.name == "quotarouter"
    provider = RouterProvider()
    assert provider.supports_search() and provider.supports_extract()


def test_is_available_is_offline_and_true_with_a_key(keys, monkeypatch):
    keys(EXA_API_KEY="k")

    def explode(request):  # noqa: ARG001
        raise AssertionError("is_available() must not open a socket")

    from quotarouter import http_client

    http_client.set_transport(httpx.MockTransport(explode))
    assert RouterProvider().is_available() is True


def test_is_available_false_without_any_path(keys, monkeypatch):
    """Two ways to be unusable: no key anywhere AND the keyless tier switched off.

    Jina needs no key by design, so the assertion is about configured vendors: with only the
    keyless flag off and no anonymous stage in the registry, the provider reports itself down.
    """
    monkeypatch.setenv("ROUTER_ALLOW_KEYLESS", "false")
    config.reset_settings_for_tests()

    provider = RouterProvider()
    from quotarouter.stages import KeylessStage, JinaStage

    for name, stage in list(provider.router.stages.items()):
        if isinstance(stage, (KeylessStage, JinaStage)):
            monkeypatch.setattr(stage, "has_credential", lambda: False)
    assert provider.is_available() is False


def test_search_payload_matches_the_hermes_envelope(keys, monkeypatch, use_transport):
    keys(EXA_API_KEY="k")
    use_transport(lambda r: json_response({"results": [{"url": "https://a", "title": "A", "highlights": ["s"]}],
                                           "costDollars": {"total": 0.007}}))
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa")
    config.reset_settings_for_tests()

    payload = RouterProvider().search("q", 3)
    assert payload["success"] is True
    assert set(payload["data"]) >= {"web", "backend"}
    assert payload["data"]["backend"] == "quotarouter:exa"
    row = payload["data"]["web"][0]
    assert list(row) == ["title", "url", "description", "position"], "key order is part of the wire contract"
    assert isinstance(row["position"], int)


def test_extract_returns_the_hermes_list_shape(keys, monkeypatch, use_transport):
    keys(FIRECRAWL_API_KEY="k")

    def handler(request):
        return json_response({"success": True, "data": {"markdown": "x" * 900,
                                                        "metadata": {"title": "T", "sourceURL": "https://a"}}})

    use_transport(handler)
    monkeypatch.setenv("ROUTER_EXTRACT_ORDER", "firecrawl")
    config.reset_settings_for_tests()

    results = RouterProvider().extract(["https://a"])
    assert isinstance(results, list)
    entry = results[0]
    for key in ("url", "title", "content", "raw_content", "metadata"):
        assert key in entry, key
    assert entry["raw_content"] == entry["content"]


def test_search_failure_returns_success_false_with_every_reason(keys, monkeypatch, use_transport):
    keys(EXA_API_KEY="k", FIRECRAWL_API_KEY="k")

    def handler(request):
        if request.url.host == "api.exa.ai":
            return json_response({"error": "quota"}, status=402)
        if request.url.path.endswith("/credit-usage"):
            return json_response({"success": True, "data": {"remainingCredits": 500, "planCredits": 1000}})
        return json_response({"error": "boom"}, status=502)

    use_transport(handler)
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa,firecrawl")
    monkeypatch.setenv("ROUTER_KEYLESS_LAST_RESORT", "false")
    config.reset_settings_for_tests()

    payload = RouterProvider().search("q", 3)
    assert payload["success"] is False
    assert "exa" in payload["error"] and "firecrawl" in payload["error"]


def test_route_log_is_written(keys, monkeypatch, use_transport, tmp_path):
    keys(EXA_API_KEY="k")
    use_transport(lambda r: json_response({"results": [{"url": "https://a", "title": "A", "highlights": []}],
                                           "costDollars": {"total": 0.007}}))
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa")
    config.reset_settings_for_tests()

    RouterProvider().search("q", 1)
    lines = (tmp_path / "state" / "routes.jsonl").read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[-1])
    assert record["capability"] == "search"
    assert record["served_by"] == "exa"
    assert record["ok"] is True
    assert record["chain"] == ["exa=ok"]


# --- CLI --------------------------------------------------------------------------------
def test_cli_status_on_empty_state(capsys, tmp_path):
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "search order" in out
    assert "exa" in out and "firecrawl" in out and "jina" in out


def test_cli_status_json_shape(capsys):
    assert cli.main(["status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["search_order"][0] == "keenable"
    assert data["state_dir"].endswith("state")
    vendors = {row["vendor"]: row for row in data["vendors"]}
    assert vendors["exa"]["unit"] == "usd"
    assert vendors["firecrawl"]["unit"] == "credits"


def test_cli_disable_then_enable_persists(capsys, tmp_path):
    assert cli.main(["disable", "exa", "--reason", "test"]) == 0
    from quotarouter import ledger as L

    assert L.Ledger(config.settings(refresh=True).ledger_path).get("exa").pause_kind == "disabled"
    assert cli.main(["enable", "exa"]) == 0
    assert not L.Ledger(config.settings(refresh=True).ledger_path).get("exa").is_paused()


def test_cli_route_shows_skips(capsys, keys, monkeypatch):
    keys(EXA_API_KEY="k")
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa,firecrawl,jina")
    monkeypatch.setenv("ROUTER_EXTRACT_ORDER", "jina,firecrawl")
    config.reset_settings_for_tests()
    assert cli.main(["route"]) == 0
    out = capsys.readouterr().out
    assert "usable: exa" in out
    # Firecrawl has no key here, so the reason must name ITS variable, not another's
    assert "skip firecrawl: no FIRECRAWL_API_KEY set" in out
    assert "skip tavily: no TAVILY_API_KEY set" in out
    # extract order in this test is jina,firecrawl: jina must be first there
    assert "usable: jina" in out
