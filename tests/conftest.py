"""Shared fixtures: an isolated state dir, a mock HTTP transport, and a stage factory.

Every test runs with no network: `http_client.set_transport(MockTransport(handler))` swaps the
only place the code touches the wire. Nothing here imports Hermes, so the suite runs in CI
without an agent install.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quotarouter import config, env_source, http_client  # noqa: E402


_VENDOR_VARS = ("EXA_API_KEY", "FIRECRAWL_API_KEY", "TAVILY_API_KEY", "KEENABLE_API_KEY",
                "DEEPSEEK_API_KEY", "DEEPSEEK_BALANCE_USD",
                "ROUTER_SEARCH_ORDER", "ROUTER_EXTRACT_ORDER", "ROUTER_ALLOW_KEYLESS",
                "ROUTER_KEYLESS_LAST_RESORT", "ROUTER_MIN_PAGE_CHARS", "ROUTER_TIMEOUT")


@pytest.fixture(autouse=True)
def isolated_env(request, tmp_path, monkeypatch):
    """Fresh state dir, no vendor keys, no leftover settings per test.

    Live tests are the exception: they exist to talk to real vendors, so the operator's keys
    (exported before pytest) must survive. They still get an isolated state dir, because a
    live run must never touch the production ledger.
    """
    monkeypatch.setenv("ROUTER_STATE_DIR", str(tmp_path / "state"))
    # A host integration registers a key resolver at import time (Hermes reads the profile's
    # .env). A unit test must not inherit the operator's real keys through it.
    saved_resolvers = env_source.registered_resolvers()
    env_source.clear_resolvers()
    if request.node.get_closest_marker("live") is not None:
        for var in ("ROUTER_SEARCH_ORDER", "ROUTER_EXTRACT_ORDER"):
            monkeypatch.delenv(var, raising=False)
    else:
        for var in _VENDOR_VARS:
            monkeypatch.delenv(var, raising=False)
    config.reset_settings_for_tests()
    yield
    http_client.set_transport(None)
    env_source.set_resolvers(saved_resolvers)
    config.reset_settings_for_tests()


@pytest.fixture
def use_transport():
    """Install a handler: `call(request) -> httpx.Response`."""

    def _install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        http_client.set_transport(httpx.MockTransport(handler))

    return _install


@pytest.fixture
def keys(monkeypatch):
    """Set vendor keys in the environment for the duration of a test."""

    def _set(**kw: str) -> None:
        for name, value in kw.items():
            monkeypatch.setenv(name, value)

    return _set


def json_response(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(data).encode("utf-8"),
        headers={"content-type": "application/json", **(headers or {})},
    )


def text_response(text: str, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, content=text.encode("utf-8"), headers=headers or {})
