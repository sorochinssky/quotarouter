"""Level 3: real vendor APIs. Off by default (`-m live`), because they spend free quota.

Run before a release:

    ROUTER_LIVE=1 EXA_API_KEY=... FIRECRAWL_API_KEY=... python -m pytest -m live -q

Every test here asserts on live behaviour, so a vendor changing its contract fails the suite
instead of silently degrading the chain. Each one costs real money/quota, so they stay tiny:
one search, one scrape.
"""

from __future__ import annotations

import os

import pytest

from router import http_client, routing
from router.stages.exa import ExaStage
from router.stages.firecrawl import FirecrawlStage
from router.stages.jina import looks_like_stub

pytestmark = pytest.mark.live


def live_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        pytest.skip(f"{name} not set")
    return value


@pytest.fixture(autouse=True)
def no_mock_transport():
    """Make sure nothing left a MockTransport installed: live means live."""
    http_client.reset_transport()


def test_exa_live_search_returns_rows_and_reports_cost():
    live_env("EXA_API_KEY")
    outcome = ExaStage().search("Nous Research Hermes Agent", 3)
    assert outcome.ok, outcome.failure
    rows = outcome.payload["data"]["web"]
    assert rows and rows[0]["url"].startswith("http")
    assert outcome.spent > 0, "a live search must report what it cost"


def test_firecrawl_live_credit_usage_is_parseable():
    live_env("FIRECRAWL_API_KEY")
    stage = FirecrawlStage()
    probed = stage.probe_quota()
    assert probed is not None, "credit-usage must be readable, otherwise the ledger is blind"
    limit, reset_at, source = probed
    assert limit > 0 and source == "api"
    assert stage.remaining_override is not None and stage.remaining_override <= limit
    assert reset_at is not None, "the reset date drives the ledger window roll"


def test_jina_live_reader_returns_real_text():
    text = live_env("ROUTER_LIVE_URL") if "ROUTER_LIVE_URL" in os.environ else "https://example.com/"
    results = routing.Router().run_extract([text]).payload
    assert results and results[0]["content"], results
    assert not looks_like_stub(results[0]["content"], 400)


def test_deepseek_live_balance_is_readable():
    live_env("DEEPSEEK_API_KEY")
    from router.stages.deepseek import DeepSeekStage

    stage = DeepSeekStage()
    probed = stage.probe_quota()
    # `None` means the probe itself failed (network/HTML) — that is a real finding for a live run.
    assert probed is not None, "balance must be readable"
    limit, _reset_at, source = probed
    assert source == "api"
    assert limit >= 0.0
