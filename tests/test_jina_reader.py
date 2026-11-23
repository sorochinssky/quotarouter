"""Jina Reader — the stage that serves every extract by default, for free.

Its whole job is deciding whether what came back is a page or a wall, and it is the stage whose
mistakes cost the least to catch: no credits, so a wrong verdict here just changes the route.
"""

from __future__ import annotations

import httpx
import pytest

from quotarouter import http_client
from quotarouter.stages.jina import JinaStage, _split_reader_output, looks_like_stub


def wire(handler):
    http_client.set_transport(httpx.MockTransport(handler))


PAGE = """Title: Example Domain

URL Source: https://example.com/

Published Time: Thu, 19 Nov 2026 22:11:52 GMT

Markdown Content:
# Example Domain

This domain is for use in documentation examples without needing permission.
"""


# --- the header block ---------------------------------------------------------------------
def test_headers_are_separated_from_the_body():
    title, source, body = _split_reader_output(PAGE)
    assert title == "Example Domain"
    assert source == "https://example.com/"
    assert body.startswith("# Example Domain")
    assert "Markdown Content" not in body


def test_a_document_without_headers_is_all_body():
    title, source, body = _split_reader_output("# Just text\n\nand more text")
    assert title == "" and source == ""
    assert body.startswith("# Just text")


def test_a_second_header_block_does_not_end_the_body():
    """Some pages print a header block, a blank line, then more headers before the text."""
    text = "Title: A\n\nURL Source: https://a\n\nBody starts here"
    title, source, body = _split_reader_output(text)
    assert title == "A" and source == "https://a" and body == "Body starts here"


# --- stub detection -----------------------------------------------------------------------
@pytest.mark.parametrize("content", [
    "",
    "   ",
    "short",
    "Enable JavaScript and cookies to continue",
    "Please verify you are a human",
])
def test_walls_and_fragments_are_stubs(content):
    assert looks_like_stub(content, min_chars=40) is True


def test_a_real_page_is_not_a_stub():
    assert looks_like_stub(PAGE, min_chars=40) is False


# --- extract ------------------------------------------------------------------------------
def test_a_delivered_page_is_a_document():
    wire(lambda r: httpx.Response(200, text=PAGE))
    pages = JinaStage().extract(["https://example.com"])
    assert pages[0]["content"].startswith("# Example Domain")
    assert pages[0]["url"] == "https://example.com/"


def test_a_blocked_domain_is_that_page_s_only_problem():
    wire(lambda r: httpx.Response(403, text=(
        "Anonymous access to domain github.com blocked until Sun Nov 22 2026 10:47:49 GMT+0000 "
        "(Coordinated Universal Time) due to previous abuse")))
    page = JinaStage().extract(["https://github.com/x"])[0]
    assert "blocked for this domain" in page["error"]
    assert page.get("kind") in ("", None), "no stage-level kind: the tier is healthy"


def test_a_rate_limit_keeps_its_kind_so_the_router_can_pause():
    wire(lambda r: httpx.Response(429, text="slow down", headers={"retry-after": "30"}))
    page = JinaStage().extract(["https://a"])[0]
    assert page["kind"] == "throttle"
    assert page["error"]


def test_a_timeout_is_a_page_error_with_its_kind():
    def handler(request):
        raise httpx.ReadTimeout("took too long")

    wire(handler)
    page = JinaStage().extract(["https://a"])[0]
    assert page["error"] and page["kind"]


def test_a_stub_page_is_not_delivered():
    wire(lambda r: httpx.Response(200, text="Title: x\nURL Source: https://a\n\nEnable JavaScript"))
    page = JinaStage().extract(["https://a"])[0]
    assert "blocked or empty" in page["error"]


def test_every_requested_url_comes_back_in_order():
    def handler(request):
        if "second" in str(request.url):
            return httpx.Response(200, text="short")
        return httpx.Response(200, text=PAGE)

    wire(handler)
    pages = JinaStage().extract(["https://first", "https://second", "https://third"])
    assert len(pages) == 3, "one entry per requested URL, delivered or not"
    assert pages[0].get("content") and pages[2].get("content")
    assert pages[1].get("error"), pages[1]
    # `url` is the page's canonical address from the reader; what the caller asked for is kept
    # beside it in metadata, so a redirect cannot silently re-key the caller's own list.
    assert pages[0]["metadata"]["sourceURL"] == "https://first"


def test_the_reader_needs_no_key():
    stage = JinaStage()
    assert stage.has_credential() is True
    assert stage.not_ready_reason() == ""
