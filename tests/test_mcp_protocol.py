"""The MCP server's protocol layer, in process.

This is the code every other agent actually talks to, and it had zero coverage: the only test
ran the server in a subprocess, where measured lines are invisible and failures show up as
"connection closed". A client breaking here is not a Hermes bug — it is the package's whole
reason for existing outside Hermes.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field

import pytest

from quotarouter import errors, routing, types
from quotarouter.integrations import mcp


# --- a router that does not touch the network -------------------------------------------
@dataclass
class FakeState:
    limit: float = 1000.0
    used: float = 250.0
    pause_reason: str = ""
    paused: bool = False

    def is_paused(self, now: float) -> bool:
        return self.paused


@dataclass
class FakeStage:
    unit: str = "credits"
    funding: str = "free"
    credential: bool = True
    search: bool = True
    extract: bool = True

    def has_credential(self) -> bool:
        return self.credential

    def supports_search(self) -> bool:
        return self.search

    def supports_extract(self) -> bool:
        return self.extract


class FakeLedger:
    def __init__(self) -> None:
        self.state = FakeState()

    def get(self, name: str) -> FakeState:
        return self.state


@dataclass
class FakeRouter:
    stages: dict = field(default_factory=lambda: {"exa": FakeStage()})
    ledger: FakeLedger = field(default_factory=FakeLedger)
    search_result: routing.RouteResult | None = None
    extract_result: routing.RouteResult | None = None
    logged: list = field(default_factory=list)
    probed: list = field(default_factory=list)

    def now(self) -> float:
        return 1_000.0

    def run_search(self, query: str, limit: int, **options) -> routing.RouteResult:
        self.options = options
        return self.search_result or routing.RouteResult()

    def run_extract(self, urls: list[str], **options) -> routing.RouteResult:
        self.options = options
        return self.extract_result or routing.RouteResult()

    def log_route(self, capability: str, result, extra: dict) -> None:
        self.logged.append((capability, extra))

    def probe_all(self, force: bool = False) -> dict:
        self.probed.append(force)
        return {"exa": {"remaining": 9.5}}


@pytest.fixture
def server(monkeypatch):
    fake = FakeRouter()
    monkeypatch.setattr(mcp, "get_router", lambda: fake, raising=False)
    return mcp.McpServer(), fake


def text_of(response: dict) -> str:
    return response["result"]["content"][0]["text"]


# --- handshake ---------------------------------------------------------------------------
def test_initialize_echoes_a_version_it_actually_implements(server):
    """Echoing the request claimed support for revisions never written: the reference client
    negotiated "2025-11-25" and believed it."""
    engine, _ = server
    response = engine.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "1999-01-01"}})
    assert response["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION
    assert response["result"]["serverInfo"]["name"] == "quotarouter"
    assert "tools" in response["result"]["capabilities"]


def test_initialize_keeps_a_version_it_supports(server):
    engine, _ = server
    response = engine.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18"}})
    assert response["result"]["protocolVersion"] == "2025-06-18"


def test_ping_is_answered(server):
    engine, _ = server
    assert engine.handle({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}


def test_a_notification_gets_no_answer(server):
    engine, _ = server
    assert engine.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert engine.handle({"jsonrpc": "2.0", "method": "something/unknown"}) is None


def test_an_unknown_method_is_a_protocol_error(server):
    engine, _ = server
    response = engine.handle({"jsonrpc": "2.0", "id": 3, "method": "prompts/list"})
    assert response["error"]["code"] == -32601


# --- tools -------------------------------------------------------------------------------
def test_tools_list_declares_the_four_tools_with_schemas(server):
    engine, _ = server
    tools = engine.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})["result"]["tools"]
    by_name = {tool["name"]: tool for tool in tools}
    assert set(by_name) == {"web_search", "web_extract", "quota_status", "quota_probe"}
    assert by_name["web_search"]["inputSchema"]["required"] == ["query"]
    assert by_name["web_extract"]["inputSchema"]["required"] == ["urls"]


def test_an_unknown_tool_is_reported_as_such(server):
    engine, _ = server
    response = engine.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                              "params": {"name": "does_not_exist", "arguments": {}}})
    assert response["error"]["code"] == -32602


def test_web_search_without_a_query_is_a_tool_error_not_a_crash(server):
    engine, _ = server
    response = engine.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                              "params": {"name": "web_search", "arguments": {"query": "  "}}})
    assert response["result"]["isError"] is True
    assert "query" in text_of(response)


def test_web_search_reports_which_stage_served_it(server, monkeypatch):
    engine, fake = server
    fake.search_result = routing.RouteResult()
    fake.search_result.stage = "exa"
    fake.search_result.payload = types.search_ok([types.web_hit("https://a", "A", "d", 1)])
    monkeypatch.setattr(routing, "format_failure", lambda result, cap: "none")
    response = engine.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                              "params": {"name": "web_search", "arguments": {"query": "q"}}})
    payload = json.loads(text_of(response))
    assert payload["data"]["backend"] == "quotarouter:exa"
    assert response["result"]["isError"] is False
    assert fake.logged and fake.logged[0][0] == "search"


def test_a_search_that_nothing_could_serve_is_an_error_result(server):
    engine, fake = server
    fake.search_result = routing.RouteResult()  # no payload, no stage
    response = engine.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                              "params": {"name": "web_search", "arguments": {"query": "q"}}})
    assert response["result"]["isError"] is True


def test_web_extract_without_urls_is_a_tool_error(server):
    engine, _ = server
    response = engine.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                              "params": {"name": "web_extract", "arguments": {"urls": []}}})
    assert response["result"]["isError"] is True
    assert "urls" in text_of(response)


def test_web_extract_returns_pages(server):
    engine, fake = server
    fake.extract_result = routing.RouteResult()
    fake.extract_result.stage = "jina"
    fake.extract_result.payload = [types.document("https://a", "A", "text")]
    response = engine.handle({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                              "params": {"name": "web_extract", "arguments": {"urls": ["https://a"]}}})
    pages = json.loads(text_of(response))["results"]
    assert pages[0]["content"] == "text"
    assert response["result"]["isError"] is False


def test_a_failed_extract_names_every_url(server):
    engine, fake = server
    fake.extract_result = routing.RouteResult()
    response = engine.handle({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                              "params": {"name": "web_extract",
                                         "arguments": {"urls": ["https://a", "https://b"]}}})
    pages = json.loads(text_of(response))["results"]
    assert [p["url"] for p in pages] == ["https://a", "https://b"]
    assert all(p.get("error") for p in pages)
    assert response["result"]["isError"] is True


def test_quota_status_reports_each_stage(server):
    engine, fake = server
    response = engine.handle({"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                              "params": {"name": "quota_status", "arguments": {}}})
    report = json.loads(text_of(response))
    assert report["vendors"]["exa"]["credential"] is True
    assert report["vendors"]["exa"]["used"] == 250.0
    assert report["vendors"]["exa"]["capabilities"] == {"search": True, "extract": True}
    assert report["vendors"]["exa"]["funding"] == "free"
    assert "chains" in report and "allow_chain_override" in report


def test_quota_probe_forces_a_refresh(server):
    engine, fake = server
    engine.handle({"jsonrpc": "2.0", "id": 13, "method": "tools/call",
                   "params": {"name": "quota_probe", "arguments": {}}})
    assert fake.probed == [True]


def test_a_tool_that_raises_becomes_an_error_result(server, monkeypatch):
    engine, fake = server

    def boom(*args, **kwargs):
        raise errors.TransientError("vendor fell over")

    monkeypatch.setattr(fake, "run_search", boom)
    response = engine.handle({"jsonrpc": "2.0", "id": 14, "method": "tools/call",
                              "params": {"name": "web_search", "arguments": {"query": "q"}}})
    assert response["result"]["isError"] is True
    assert "vendor fell over" in text_of(response)
    # and the server still answers the next request
    assert engine.handle({"jsonrpc": "2.0", "id": 15, "method": "ping"})["result"] == {}


# --- stdio framing -----------------------------------------------------------------------
def test_serve_skips_blank_lines_and_reports_bad_json(server):
    engine, _ = server
    out = io.StringIO()
    engine.serve(io.StringIO('\n\nnot json\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n'), out)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert lines[0]["error"]["code"] == -32700
    assert lines[1]["result"] == {}


def test_serve_writes_nothing_for_notifications(server):
    engine, _ = server
    out = io.StringIO()
    engine.serve(io.StringIO('{"jsonrpc":"2.0","method":"notifications/initialized"}\n'), out)
    assert out.getvalue() == ""


# --- what the model is told ---------------------------------------------------------------
def test_the_tools_say_that_a_call_may_pin_and_that_pinning_can_cost_money():
    """The model chooses from the description: if it does not say so, the feature is unreachable."""
    tools = {tool["name"]: tool for tool in mcp.TOOLS}
    for name in ("web_search", "web_extract"):
        tool = tools[name]
        text = tool["description"]
        assert "stages" in text and "chain" in text, name
        assert "spends money" in text, f"{tool['name']}: a paid pin must be stated, not implied"
        properties = tool["inputSchema"]["properties"]
        required = tool["inputSchema"].get("required", [])
        for option in ("stages", "chain", "fallback", "force", "budget", "max_cost_usd"):
            assert option in properties, f"{name} is missing {option}"
            assert option not in required, f"{name}: {option} is optional and must not be required"


def test_a_refused_request_is_a_tool_error_and_the_session_survives(monkeypatch):
    """The kill switch and a bad argument both come from the resolver. A client must see an
    actionable error and be able to send the next request on the same connection."""

    class Refusing(FakeRouter):
        def run_search(self, query, limit, **options):
            raise ValueError(
                "per-call chains are disabled in this deployment "
                "(QUOTAROUTER_ALLOW_CHAIN_OVERRIDE=0); use the configured order")

    monkeypatch.setattr(mcp, "get_router", lambda: Refusing(), raising=False)
    engine = mcp.McpServer()

    first = engine.handle({"jsonrpc": "2.0", "id": 30, "method": "tools/call",
                           "params": {"name": "web_search",
                                      "arguments": {"query": "q", "stages": ["exa"]}}})
    assert first["result"]["isError"] is True
    assert "QUOTAROUTER_ALLOW_CHAIN_OVERRIDE" in text_of(first)

    second = engine.handle({"jsonrpc": "2.0", "id": 31, "method": "tools/call",
                            "params": {"name": "quota_status", "arguments": {}}})
    assert second["result"]["isError"] is False, "one refusal must not poison the session"
