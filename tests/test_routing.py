"""Stage routing end to end on a mock transport: order, failover, pauses, honesty."""

from __future__ import annotations

import json

import httpx
import pytest

from quotarouter import config, ledger as L, routing, types
from conftest import json_response, text_response

FIRE = "api.firecrawl.dev"
EXA = "api.exa.ai"
JINA = "r.jina.ai"


def firecrawl_usage(remaining: float, period_end: str = "2026-11-30T07:28:51Z", plan: float = 1000):
    return {"success": True, "data": {"remainingCredits": remaining, "planCredits": plan,
                                      "billingPeriodEnd": period_end}}


def exa_ok(n: int = 3, cost: float = 0.007):
    return {"requestId": "r", "results": [{"url": f"https://site/{i}", "title": f"T{i}",
                                           "highlights": [f"snippet {i}"]} for i in range(n)],
            "costDollars": {"total": cost}}


def firecrawl_search(n: int = 2):
    return {"success": True, "creditsUsed": 2,
            "data": {"web": [{"url": f"https://fc/{i}", "title": f"F{i}", "description": "d"} for i in range(n)]}}


def build(handler, monkeypatch, *, order: str, keys: dict[str, str]):
    for name, value in keys.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", order)
    monkeypatch.setenv("ROUTER_EXTRACT_ORDER", order)
    config.reset_settings_for_tests()
    from quotarouter import http_client

    http_client.set_transport(httpx.MockTransport(handler))
    return routing.Router()


def test_first_stage_serves_and_second_never_searching(use_transport, keys, monkeypatch):
    called: list[str] = []
    searches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request.url.host)
        if request.url.host == EXA:
            searches.append("exa")
            return json_response(exa_ok())
        if request.url.host == FIRE:
            if request.url.path.endswith("/credit-usage"):
                return json_response(firecrawl_usage(991))
            searches.append("firecrawl")  # only a SEARCH counts; the balance GET is harmless
            return json_response(firecrawl_search())
        raise AssertionError(f"unexpected call to {request.url.host}")

    keys(EXA_API_KEY="k", FIRECRAWL_API_KEY="k")
    use_transport(handler)
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa,firecrawl")
    config.reset_settings_for_tests()
    router = routing.Router()

    result = router.run_search("hermes", 3)
    assert result.ok and result.stage == "exa"
    # Firecrawl may be asked for its BALANCE (that is a GET /credit-usage), but it must never
    # be asked to search while exa has room — the whole point of the router.
    assert "firecrawl" not in searches
    assert searches == ["exa"]
    assert result.payload["data"]["web"][0]["url"] == "https://site/0"
    assert router.ledger.get("exa").used == pytest.approx(0.007)


def test_quota_402_on_first_stage_falls_over_and_parks_it(use_transport, keys, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == EXA:
            return json_response({"error": "NO_MORE_CREDITS"}, status=402)
        if request.url.host == FIRE:
            if request.url.path.endswith("/credit-usage"):
                return json_response(firecrawl_usage(991))
            return json_response(firecrawl_search())
        raise AssertionError(request.url)

    keys(EXA_API_KEY="k", FIRECRAWL_API_KEY="k")
    use_transport(handler)
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa,firecrawl")
    config.reset_settings_for_tests()
    router = routing.Router()

    result = router.run_search("q", 2)
    assert result.ok and result.stage == "firecrawl"
    assert router.ledger.get("exa").pause_kind == types.QUOTA
    assert router.ledger.get("exa").is_paused()
    # second call must not dial exa again
    calls: list[str] = []
    use_transport(lambda r: (calls.append(r.url.host), handler(r))[1])
    again = routing.Router()
    assert again.run_search("q2", 2).stage == "firecrawl"
    assert EXA not in calls, "a parked vendor must not be asked again"


def test_429_pauses_for_a_minute_then_recovers(use_transport, keys, monkeypatch):
    state = {"fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == EXA:
            if state["fail"]:
                return json_response({"error": "rate limit"}, status=429)
            return json_response(exa_ok())
        return json_response(firecrawl_search())

    keys(EXA_API_KEY="k", FIRECRAWL_API_KEY="k")
    use_transport(handler)
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa,firecrawl")
    config.reset_settings_for_tests()
    now = [1000.0]
    router = routing.Router(clock=lambda: now[0])

    assert router.run_search("q", 2).stage == "firecrawl"
    exa = router.ledger.get("exa")
    assert exa.pause_kind == types.THROTTLE
    assert exa.paused_until == pytest.approx(1060.0)

    state["fail"] = False
    now[0] += 61  # a minute later the vendor answers again
    assert router.run_search("q", 2).stage == "exa"


def test_empty_result_is_a_failure_not_a_success(use_transport, keys, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == EXA:
            return json_response({"results": [], "costDollars": {"total": 0.0}})
        return json_response(firecrawl_search())

    keys(EXA_API_KEY="k", FIRECRAWL_API_KEY="k")
    use_transport(handler)
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa,firecrawl")
    config.reset_settings_for_tests()
    router = routing.Router()

    result = router.run_search("q", 2)
    assert result.ok and result.stage == "firecrawl"
    assert router.ledger.get("exa").used == 0.0, "an empty answer costs nothing"


def test_stage_without_key_is_skipped_with_a_reason(use_transport, keys, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(firecrawl_search())

    keys(FIRECRAWL_API_KEY="k")
    use_transport(handler)
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa,firecrawl")
    monkeypatch.setenv("ROUTER_KEYLESS_LAST_RESORT", "false")
    config.reset_settings_for_tests()
    router = routing.Router()

    usable, skipped = router.candidates("search")
    assert [s.name for s in usable] == ["firecrawl"]
    assert any(s.stage == "exa" and "EXA_API_KEY" in s.reason for s in skipped)


def test_all_stages_out_returns_honest_error(use_transport, keys, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == EXA:
            return json_response({"error": "quota"}, status=402)
        return json_response({"error": "boom"}, status=500)

    keys(EXA_API_KEY="k", FIRECRAWL_API_KEY="k")
    use_transport(handler)
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa,firecrawl")
    monkeypatch.setenv("ROUTER_KEYLESS_LAST_RESORT", "false")
    config.reset_settings_for_tests()
    router = routing.Router()

    result = router.run_search("q", 2)
    assert not result.ok
    message = routing.format_failure(result, "search")
    assert "exa" in message and "quota" in message
    assert "firecrawl" in message and "transient" in message


def test_firecrawl_probe_uses_the_vendors_own_window(use_transport, keys, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/credit-usage"):
            return json_response(firecrawl_usage(remaining=750))
        return json_response(firecrawl_search())

    keys(FIRECRAWL_API_KEY="k")
    use_transport(handler)
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "firecrawl")
    config.reset_settings_for_tests()
    router = routing.Router()

    assert router.run_search("q", 2).stage == "firecrawl"
    state = router.ledger.get("firecrawl")
    assert state.limit == 1000
    assert state.source == "api"
    assert state.period == "firecrawl:2026-11-30", "the vendor's billing period, not our calendar guess"
    assert state.used == pytest.approx(250 + 2)


def test_firecrawl_exhaustion_by_balance_never_dials_search(use_transport, keys, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/credit-usage"):
            return json_response(firecrawl_usage(remaining=0))
        raise AssertionError("search must not be dialed when the balance is zero")

    keys(FIRECRAWL_API_KEY="k")
    use_transport(handler)
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "firecrawl")
    monkeypatch.setenv("ROUTER_KEYLESS_LAST_RESORT", "false")
    config.reset_settings_for_tests()
    router = routing.Router()

    result = router.run_search("q", 2)
    assert not result.ok
    assert "quota spent" in result.summary()


def test_crashing_stage_does_not_kill_the_call(use_transport, keys, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == EXA:
            return httpx.Response(200, content=b"<html>not json</html>",
                                  headers={"content-type": "text/html"})
        return json_response(firecrawl_search())

    keys(EXA_API_KEY="k", FIRECRAWL_API_KEY="k")
    use_transport(handler)
    monkeypatch.setenv("ROUTER_SEARCH_ORDER", "exa,firecrawl")
    config.reset_settings_for_tests()
    router = routing.Router()

    assert router.run_search("q", 2).stage == "firecrawl"


# --- extract ---------------------------------------------------------------------------

PAGE = "Lorem ipsum dolor sit amet, " * 40  # well over the 400-char suspicion floor


def test_extract_prefers_jina_and_spends_no_credits(use_transport, keys, monkeypatch):
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request.url.host)
        if request.url.host == JINA:
            return text_response(f"Title: Example\n\nURL Source: https://example.com/\n\nMarkdown Content:\n{PAGE}")
        if request.url.path.endswith("/credit-usage"):
            return json_response(firecrawl_usage(991))
        raise AssertionError(f"firecrawl must not be asked to scrape: {request.url}")

    keys(FIRECRAWL_API_KEY="k")
    use_transport(handler)
    monkeypatch.setenv("ROUTER_EXTRACT_ORDER", "jina,firecrawl")
    config.reset_settings_for_tests()
    router = routing.Router()

    results = router.run_extract(["https://example.com/"]).payload
    assert len(results) == 1
    assert results[0]["title"] == "Example"
    assert results[0]["url"] == "https://example.com/"
    assert PAGE[:40] in results[0]["content"]
    assert "r.jina.ai" in called
    assert not any("scrape" in str(u) for u in called), "a scrape would spend credits"
    # The ledger mirrors Firecrawl's own balance (991 reported => 9 spent), but the only
    # credit consumer of THIS call was jina, which bills nothing.
    assert router.ledger.get("firecrawl").used == pytest.approx(9.0)


@pytest.mark.parametrize("body", [
    "",  # blank
    "Enable JavaScript and cookies to continue",  # JS wall
    "Checking your browser before accessing the site.",  # bot check
    "short",
])
def test_extract_moves_on_when_jina_returns_a_stub(use_transport, keys, monkeypatch, body):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == JINA:
            return text_response(f"Title: x\n\nMarkdown Content:\n{body}")
        if request.url.path.endswith("/credit-usage"):
            return json_response(firecrawl_usage(900))
        return json_response({"success": True, "data": {"markdown": PAGE, "metadata": {
            "title": "Real", "sourceURL": "https://example.com/"}}})

    keys(FIRECRAWL_API_KEY="k")
    use_transport(handler)
    monkeypatch.setenv("ROUTER_EXTRACT_ORDER", "jina,firecrawl")
    config.reset_settings_for_tests()
    router = routing.Router()

    result = router.run_extract(["https://example.com/"])
    assert result.stage == "firecrawl"
    assert result.payload[0]["content"] == PAGE
    assert router.ledger.get("firecrawl").used == pytest.approx(101)


def test_extract_reports_the_origin_status_not_a_blank_page(use_transport, keys, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response({"success": True, "data": {"markdown": "", "metadata": {
            "title": "x", "statusCode": 403, "sourceURL": "https://example.com/"}}})

    keys(FIRECRAWL_API_KEY="k")
    use_transport(handler)
    monkeypatch.setenv("ROUTER_EXTRACT_ORDER", "firecrawl")
    monkeypatch.setenv("ROUTER_KEYLESS_LAST_RESORT", "false")
    config.reset_settings_for_tests()
    router = routing.Router()

    result = router.run_extract(["https://example.com/"])
    # A page that the origin refuses is a per-URL answer, not a stage failure: the caller gets
    # an entry with the reason instead of an empty result it cannot explain.
    assert result.payload is not None, result.summary()
    assert "HTTP 403 from origin" in result.payload[0]["error"]
