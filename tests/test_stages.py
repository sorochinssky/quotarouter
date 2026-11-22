"""Stage adapters against recorded vendor payloads (VCR-style, offline).

These fixtures are copies of real responses (trimmed), so a vendor schema change shows up
here as a test failure instead of as an empty search in production.
"""

from __future__ import annotations

import json

import httpx
import pytest

from quotarouter import errors, http_client, types
from quotarouter.stages.exa import ExaStage
from quotarouter.stages.firecrawl import FirecrawlStage
from quotarouter.stages.jina import JinaStage, _split_reader_output, looks_like_stub
from quotarouter.stages.keenable import KeenableStage
from quotarouter.stages.tavily import TavilyStage
from conftest import json_response, text_response


def wire(handler):
    http_client.set_transport(httpx.MockTransport(handler))


# --- Exa ---------------------------------------------------------------------------------
EXA_SEARCH = {  # trimmed real reply
    "requestId": "adff5cf2",
    "results": [
        {"id": "https://github.com/nousresearch/hermes-agent/", "title": "NousResearch/hermes-agent",
         "url": "https://github.com/nousresearch/hermes-agent/", "highlights": ["agent framework"]},
    ],
    "costDollars": {"total": 0.007, "search": {"neural": 0.007}},
}


def test_exa_search_maps_rows_and_reports_cost(keys):
    keys(EXA_API_KEY="k")
    wire(lambda r: json_response(EXA_SEARCH))

    outcome = ExaStage().search("hermes", 5)
    assert outcome.ok
    row = outcome.payload["data"]["web"][0]
    assert row == {"title": "NousResearch/hermes-agent", "url": "https://github.com/nousresearch/hermes-agent/",
                   "description": "agent framework", "position": 1}
    assert outcome.spent == pytest.approx(0.007)


def test_exa_empty_results_is_an_empty_failure(keys):
    keys(EXA_API_KEY="k")
    wire(lambda r: json_response({"results": [], "costDollars": {"total": 0}}))
    outcome = ExaStage().search("nope", 5)
    assert not outcome.ok and outcome.failure.kind == types.EMPTY
    assert outcome.spent == 0.0


def test_exa_402_raises_quota(keys):
    keys(EXA_API_KEY="k")
    wire(lambda r: json_response({"error": "NO_MORE_CREDITS"}, status=402))
    with pytest.raises(errors.QuotaError):
        ExaStage().search("x", 5)


def test_exa_extract_keeps_per_url_failures(keys):
    keys(EXA_API_KEY="k")

    def handler(request):
        return json_response({
            "results": [{"url": "https://a.example", "title": "A", "text": "hello"}],
            "statuses": [{"id": "https://b.example", "status": "error", "error": {"tag": "CRAWL_NOT_FOUND"}}],
        })

    wire(handler)
    results = ExaStage().extract(["https://a.example", "https://b.example"])
    assert results[0]["content"] == "hello"
    assert results[1]["url"] == "https://b.example" and "CRAWL_NOT_FOUND" in results[1]["error"]


# --- Firecrawl ---------------------------------------------------------------------------
def test_firecrawl_search_and_credits(keys):
    keys(FIRECRAWL_API_KEY="k")
    wire(lambda r: json_response({"success": True, "creditsUsed": 2,
                                  "data": {"web": [{"url": "https://x", "title": "X", "description": "d"}]}}))
    outcome = FirecrawlStage().search("x", 5)
    assert outcome.payload["data"]["web"][0]["title"] == "X"
    assert outcome.spent == 2.0


def test_firecrawl_scrape_reports_origin_status(keys):
    keys(FIRECRAWL_API_KEY="k")
    wire(lambda r: json_response({"success": True, "data": {"markdown": "",
                                                            "metadata": {"statusCode": 404, "sourceURL": "https://x"}}}))
    results = FirecrawlStage().extract(["https://x"])
    assert "HTTP 404 from origin" in results[0]["error"]


def test_firecrawl_scrape_uses_source_url_as_the_identity(keys):
    keys(FIRECRAWL_API_KEY="k")
    wire(lambda r: json_response({"success": True, "data": {
        "markdown": "# Hi", "metadata": {"title": "T", "sourceURL": "https://x/final"}}}))
    results = FirecrawlStage().extract(["https://x"])
    assert results[0]["metadata"]["sourceURL"] == "https://x/final"


def test_firecrawl_probe_reports_remaining_and_period():
    """The probe must expose what the vendor says, so the ledger can mirror it."""
    stage = FirecrawlStage()
    stage.__dict__["_env"] = lambda name: "k"  # bypass credential lookup for this unit test
    wire(lambda r: json_response({"success": True, "data": {
        "remainingCredits": 989, "planCredits": 1000,
        "billingPeriodStart": "2026-10-30T07:28:51.871Z", "billingPeriodEnd": "2026-11-30T07:28:51.871Z"}}))

    probed = stage.probe_quota()
    assert probed is not None
    limit, reset_at, source = probed
    assert limit == 1000 and source == "api" and reset_at is not None
    assert stage.remaining_override == 989
    assert stage.period_override == "firecrawl:2026-11-30"


# --- Jina --------------------------------------------------------------------------------
def test_jina_parses_the_reader_header():
    text = ("Title: Example Domain\n\nURL Source: https://example.com/\n\n"
            "Published Time: Thu, 19 Nov 2026 22:10:26 GMT\n\nMarkdown Content:\n"
            "This domain is for use in documentation examples.\n\n[Learn more](https://iana.org/x)\n")
    title, source, body = _split_reader_output(text)
    assert title == "Example Domain"
    assert source == "https://example.com/"
    assert body.startswith("This domain is for use")
    assert "Markdown Content:" not in body


@pytest.mark.parametrize("content,expected", [
    ("x" * 1000, False),
    ("short", True),
    ("", True),
    ("Please enable JavaScript to continue. " + "x" * 500, True),
    ("Verify you are human " + "x" * 500, True),
    ("Checking your browser " + "x" * 500, True),
    # A legitimately short page: 156 characters of real text. Treating it as a stub would
    # spend the next vendor's credits on a perfectly good page.
    ("This domain is for use in documentation examples without needing permission. "
     "Avoid use in operations. [Learn more](https://iana.org/help/example-domains)", False),
])
def test_stub_detection(content, expected):
    assert looks_like_stub(content, 40) is expected


def test_example_com_page_is_served_not_escalated(keys):
    """Regression from e2e: Jina returned example.com (156 chars) — content, not a wall."""
    page = ("Title: Example Domain\n\nURL Source: https://example.com/\n\nMarkdown Content:\n"
            "This domain is for use in documentation examples without needing permission. "
            "Avoid use in operations.\n\n[Learn more](https://iana.org/help/example-domains)\n")
    wire(lambda r: text_response(page))
    results = JinaStage().extract(["https://example.com/"])
    assert "error" not in results[0], results[0]
    assert "documentation examples" in results[0]["content"]


def test_jina_per_url_throttle_becomes_an_error_entry():
    wire(lambda r: httpx.Response(429, headers={"retry-after": "13"}))
    results = JinaStage().extract(["https://x"])
    assert results[0]["error"].startswith("jina: throttle:")
    assert "429" in results[0]["error"]


# --- Tavily / Keenable ---------------------------------------------------------------
def test_tavily_search_maps_rows(keys):
    keys(TAVILY_API_KEY="k")
    wire(lambda r: json_response({"results": [{"url": "https://t", "title": "T", "content": "c"}]}))
    outcome = TavilyStage().search("q", 3)
    assert outcome.payload["data"]["web"][0]["url"] == "https://t"
    assert outcome.spent == 1.0


def test_tavily_usage_clamps_remaining(keys):
    stage = TavilyStage()
    stage.__dict__["_env"] = lambda name: "k"
    wire(lambda r: json_response({"key": {"usage": 250}, "account": {"plan_limit": 1000}}))
    probed = stage.probe_quota()
    assert probed is not None and probed[0] == 1000
    assert stage.remaining_override == 750


def test_keenable_search_reports_units(keys):
    keys(KEENABLE_API_KEY="k")
    wire(lambda r: json_response({"results": [{"url": "https://k", "title": "K", "snippet": "s"}],
                                 "usage": {"credits": 1}}))
    outcome = KeenableStage().search("q", 3)
    assert outcome.payload["data"]["web"][0]["url"] == "https://k"
    assert outcome.spent == 1.0


def test_jina_domain_block_is_a_page_error_not_a_stage_error():
    """A 403 that names one domain must not look like a broken key."""
    from quotarouter import errors

    body = ("Anonymous access to domain github.com blocked until Sun Nov 22 2026 10:47:49 GMT+0000 "
            "(Coordinated Universal Time) due to previous abuse")
    assert errors.is_domain_block(body)
    assert not errors.is_domain_block("Invalid API key")
    assert not errors.is_domain_block("")
