"""The Hermes adapter: the one integration that is used on every single call right now.

Its contract is narrow and easy to get wrong: `is_available()` must be cheap and offline,
`search()` must clamp a limit the model invented, and a total failure must come back as a
result the agent can read — not as an exception the agent turns into "tool crashed".
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from quotarouter import config, routing, types
from quotarouter.integrations import hermes


@dataclass
class Stage:
    name: str = "exa"
    credential: bool = True

    def has_credential(self) -> bool:
        return self.credential


@dataclass
class Router:
    stages: dict = field(default_factory=lambda: {"exa": Stage()})
    search_result: routing.RouteResult | None = None
    extract_result: routing.RouteResult | None = None
    limits: list = field(default_factory=list)
    logged: list = field(default_factory=list)

    def run_search(self, query: str, limit: int) -> routing.RouteResult:
        self.limits.append(limit)
        return self.search_result or routing.RouteResult()

    def run_extract(self, urls: list[str]) -> routing.RouteResult:
        return self.extract_result or routing.RouteResult()

    def log_route(self, capability: str, result, extra: dict) -> None:
        self.logged.append((capability, extra))


@pytest.fixture
def provider(monkeypatch):
    instance = hermes.RouterProvider()
    fake = Router()
    monkeypatch.setattr(type(instance), "router", property(lambda self: fake))
    return instance, fake


def test_is_available_is_true_with_one_credential(provider):
    instance, fake = provider
    assert instance.is_available() is True


def test_is_available_falls_back_to_the_keyless_tier(provider, monkeypatch):
    instance, fake = provider
    fake.stages = {"exa": Stage(credential=False)}
    monkeypatch.setattr(config, "settings", lambda refresh=False: config.Settings(), raising=False)
    monkeypatch.setenv("QUOTAROUTER_ALLOW_KEYLESS", "1")
    assert instance.is_available() is True


def test_is_available_is_false_when_nothing_can_serve(provider, monkeypatch):
    instance, fake = provider
    fake.stages = {"exa": Stage(credential=False)}
    monkeypatch.setenv("QUOTAROUTER_ALLOW_KEYLESS", "0")
    monkeypatch.setattr(config, "settings",
                        lambda refresh=False: config.Settings(allow_keyless=False), raising=False)
    assert instance.is_available() is False


@pytest.mark.parametrize("asked, expected", [(0, 1), (-5, 1), (100, 20), (7, 7)])
def test_the_limit_a_model_asks_for_is_clamped(provider, asked, expected):
    instance, fake = provider
    fake.search_result = routing.RouteResult()
    fake.search_result.stage = "exa"
    fake.search_result.payload = types.search_ok([])
    instance.search("q", asked)
    assert fake.limits == [expected]


def test_a_nonsense_limit_falls_back_to_five(provider):
    instance, fake = provider
    fake.search_result = routing.RouteResult()
    fake.search_result.stage = "exa"
    fake.search_result.payload = types.search_ok([])
    instance.search("q", "many")  # type: ignore[arg-type]
    assert fake.limits == [5]


def test_a_search_result_names_the_stage_that_served_it(provider):
    instance, fake = provider
    fake.search_result = routing.RouteResult()
    fake.search_result.stage = "firecrawl"
    fake.search_result.payload = types.search_ok([types.web_hit("https://a", "A", "d", 1)])
    payload = instance.search("q", 3)
    assert payload["data"]["backend"] == "quotarouter:firecrawl"
    assert payload["data"]["web"][0]["url"] == "https://a"


def test_a_dead_chain_returns_a_readable_failure_not_an_exception(provider):
    instance, fake = provider
    fake.search_result = routing.RouteResult()  # nothing served
    payload = instance.search("q", 3)
    assert payload["success"] is False
    assert payload.get("error")


def test_extract_failure_names_every_requested_url(provider):
    instance, fake = provider
    fake.extract_result = routing.RouteResult()
    pages = instance.extract(["https://a", "https://b"])
    assert [page["url"] for page in pages] == ["https://a", "https://b"]
    assert all(page.get("error") for page in pages)


def test_extract_success_returns_the_pages(provider):
    instance, fake = provider
    fake.extract_result = routing.RouteResult()
    fake.extract_result.stage = "jina"
    fake.extract_result.payload = [types.document("https://a", "A", "text")]
    pages = instance.extract(["https://a"])
    assert pages[0]["content"] == "text"


def test_the_route_is_logged_once_per_call(provider):
    """Auditing is what makes "why this vendor" answerable a week later."""
    instance, fake = provider
    fake.search_result = routing.RouteResult()
    fake.search_result.stage = "exa"
    fake.search_result.payload = types.search_ok([])
    instance.search("q", 3)
    assert [entry[0] for entry in fake.logged] == ["search"]


def test_the_picker_row_advertises_no_required_keys(provider):
    instance, _ = provider
    row = instance.get_setup_schema()
    assert row["badge"] == "quotarouter"
    assert row["env_vars"] == [], "keys are read through the chain, not through this row"
