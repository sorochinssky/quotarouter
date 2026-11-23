"""The two integration surfaces: the Hermes adapter and the MCP transport wrapper.

Both are thin, and both are where the package meets a host it does not own. The adapter must never
turn a missing Hermes module into a crash, and `serve_stdio` is the exact code an external agent's
pipe goes through — tested here in process, with real bytes, not only through a subprocess.
"""

from __future__ import annotations

import io
import json
import sys
import types as pytypes

import pytest

from quotarouter import config, routing
from quotarouter.integrations import hermes, mcp


# --- key resolution through the host -------------------------------------------------------
def _fake_agent(monkeypatch, get_provider_env):
    agent = pytypes.ModuleType("agent")
    provider = pytypes.ModuleType("agent.web_search_provider")
    setattr(provider, "get_provider_env", get_provider_env)
    setattr(agent, "web_search_provider", provider)
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.web_search_provider", provider)


def test_a_key_written_into_the_host_profile_is_visible(monkeypatch):
    """Hermes keeps its keys in ~/.hermes/.env; without this hook the chain cannot see them."""
    _fake_agent(monkeypatch, lambda name: {"EXA_API_KEY": "from-profile"}.get(name, ""))
    assert hermes._hermes_env("EXA_API_KEY") == "from-profile"
    assert hermes._hermes_env("FIRECRAWL_API_KEY") == ""


def test_a_broken_host_module_is_not_an_outage(monkeypatch):
    def explode(name):
        raise RuntimeError("host changed its API")

    _fake_agent(monkeypatch, explode)
    assert hermes._hermes_env("EXA_API_KEY") == "", (
        "a host that renamed its helper must degrade to \"no key\", not to a crashed search")


def test_the_adapter_shares_one_router_with_the_core():
    assert hermes.get_router() is routing.shared_router(), (
        "a second router would mean a second ledger, and two processes spending the same quota")


def test_reset_for_tests_forgets_the_cached_router():
    provider = hermes.RouterProvider()
    first = provider.router
    provider.reset_for_tests()
    assert provider.router is not first


def test_is_available_says_no_instead_of_raising(monkeypatch):
    provider = hermes.RouterProvider()

    def broken(refresh: bool = False):
        raise RuntimeError("settings file unreadable")

    monkeypatch.setattr(hermes.config, "settings", broken)
    assert provider.is_available() is False


def test_is_available_is_true_without_any_network_call(monkeypatch):
    class Stage:
        def has_credential(self):
            return True

    provider = hermes.RouterProvider()
    monkeypatch.setattr(type(provider), "router", property(lambda self: pytypes.SimpleNamespace(
        stages={"exa": Stage()})))
    monkeypatch.setattr(hermes.config, "settings", lambda refresh=False: config.Settings())
    assert provider.is_available() is True


# --- serve_stdio: the wrapper between a pipe and a text stream -----------------------------
def _rpc(*messages):
    return "".join(json.dumps(message) + "\n" for message in messages).encode("utf-8")


class Sink(io.BytesIO):
    """A stream whose `close` does nothing: `serve_stdio` owns the wrapper it builds and the
    wrapper closes its buffer, which is right for a real pipe and unhelpful for an assertion."""

    def close(self) -> None:  # pragma: no cover - the point is that it is not called
        pass


def test_serve_stdio_answers_over_raw_bytes(monkeypatch):
    """A client speaks bytes on a pipe. The wrapper must wrap them, answer, and return 0."""
    monkeypatch.setattr(mcp, "get_router", lambda: pytypes.SimpleNamespace(
        stages={}, ledger=None), raising=False)
    stdin = Sink(_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18"}},
                            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
    stdout = Sink()

    assert mcp.serve_stdio(stdin, stdout) == 0
    lines = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines() if line]
    assert lines[0]["result"]["protocolVersion"] == "2025-06-18"
    assert {tool["name"] for tool in lines[1]["result"]["tools"]} >= {"web_search", "web_extract"}


def test_serve_stdio_ends_quietly_on_empty_input(monkeypatch):
    stdout = Sink()
    assert mcp.serve_stdio(Sink(b""), stdout) == 0
    assert stdout.getvalue() == b""


# --- argument handling in the MCP tools ----------------------------------------------------
class Recorder:
    def __init__(self):
        self.limits = []
        self.stages = {}
        self.ledger = None

    def now(self):
        return 0.0

    def run_search(self, query, limit, **options):
        self.limits.append(limit)
        return routing.RouteResult()

    def run_extract(self, urls, **options):
        raise ValueError("chain 'nope' is not declared (known chains: free)")

    def log_route(self, capability, result, extra):
        pass


def test_a_limit_the_model_invented_is_clamped_not_crashed(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(mcp, "get_router", lambda: recorder, raising=False)
    engine = mcp.McpServer()

    for given in ("abc", None, 0, 99):
        arguments = {"query": "q"}
        if given is not None:
            arguments["limit"] = given
        engine.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "web_search", "arguments": arguments}})

    # Junk, a missing limit and a zero all mean "you did not say": the default stands. 99 is a real
    # request and is clamped to the documented ceiling instead of being refused.
    assert recorder.limits == [5, 5, 5, 20]


def test_a_refused_extract_chain_is_a_tool_error(monkeypatch):
    monkeypatch.setattr(mcp, "get_router", lambda: Recorder(), raising=False)
    response = mcp.McpServer().handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "web_extract",
                   "arguments": {"urls": ["https://a"], "chain": "nope"}}})

    assert response["result"]["isError"] is True
    assert "known chains" in response["result"]["content"][0]["text"], (
        "the error has to say what exists, otherwise the model retries the same guess")
