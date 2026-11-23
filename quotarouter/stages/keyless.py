"""Keyless last resort — the anonymous public free tiers (Exa / Parallel / Firecrawl / Keenable).

Modelled on Hermes' own keyless MCP ring: no key, no account, vendor-side rate limits only.
This stage never has a credential of its own and never reports a quota: the ledger does not
meter it. It answers only when every configured stage is spent or paused, and the result is
marked `served_by=keyless` so the log line never pretends a free trial paid for it.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from .. import errors, http_client, types
from .base import Capability, Stage

logger = logging.getLogger(__name__)

# Same endpoints Hermes' bundled keyless ring uses; kept in one place so a vendor change is one edit.
EXA_MCP = "https://mcp.exa.ai/mcp"
PARALLEL_MCP = "https://search.parallel.ai/mcp"
_SESSION_ID = uuid.uuid4().hex  # Parallel rate-limits per session; random per process, never persisted


def _mcp_call(url: str, tool: str, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Minimal MCP-over-HTTP tool call: JSON-RPC 2.0, `tools/call`."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": arguments}}
    resp = http_client.post(url,
                            headers={"Content-Type": "application/json",
                                     # The MCP servers reject a client that does not accept both:
                                     # 406 "Client must accept both application/json and
                                     # text/event-stream". This one header is the difference
                                     # between the last-resort tier working and failing on every
                                     # single call — which is exactly what it did.
                                     "Accept": "application/json, text/event-stream"},
                            json=payload, timeout=timeout)
    headers = http_client.header_map(resp)
    text = resp.text or ""
    if resp.status_code >= 400:
        raise errors.classify(resp.status_code, text, headers)
    if "text/event-stream" in (resp.headers.get("content-type") or ""):
        return _sse_result(text)
    try:
        return resp.json()
    except ValueError as exc:
        raise errors.TransientError(f"keyless MCP returned non-JSON ({exc})", status=resp.status_code) from exc


def _sse_result(text: str) -> dict[str, Any]:
    """Read a JSON-RPC answer that arrived framed as server-sent events."""
    answers: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[len("data:"):].strip()
        if not body or body == "[DONE]":
            continue
        try:
            parsed = json.loads(body)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            answers.append(parsed)
    for answer in answers:
        if "result" in answer or "error" in answer:
            return answer
    # Anything else is a keep-alive (`event: ping`) or a progress note, not an answer. Returning
    # one of those would look like an empty page and pause the tier with the wrong reason.
    raise errors.TransientError("keyless MCP sent an event stream with no JSON-RPC answer")


def _mcp_text(result: dict[str, Any]) -> str:
    """MCP tool results arrive as `content: [{type: text, text: ...}]`."""
    body = result.get("result") or {}
    parts = []
    for item in body.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    text = "\n".join(parts).strip()
    # A tool that rejects its arguments answers 200 with `isError: true` and the complaint as
    # content. Read as a page, that complaint becomes the page text — a documentation page
    # silently replaced by "MCP error -32602: Invalid arguments".
    if body.get("isError"):
        raise errors.TransientError(f"keyless tool error: {text[:300]}")
    return text


class KeylessStage(Stage):
    name = "keyless"
    display_name = "Anonymous free tier"
    key_env = ""
    unit = "requests"
    monthly_allowance = None  # not metered by an account
    capability = Capability(search=True, extract=True, search_units=0.0, extract_units=0.0)

    def has_credential(self) -> bool:
        from ..config import settings

        return settings().allow_keyless

    def search(self, query: str, limit: int) -> types.Outcome:
        try:
            reply = _mcp_call(EXA_MCP, "web_search_exa", {"query": query, "numResults": max(1, min(limit, 10))}, 30.0)
            text = _mcp_text(reply)  # a tool error surfaces here too, not only at the transport
        except errors.StageError as exc:
            return self._fail(exc.kind, f"keyless: {exc}", status=exc.status, retry_after=exc.retry_after)
        if not text:
            return self._fail(types.EMPTY, "keyless: empty reply")
        rows = self._parse_rows(text, limit)
        if not rows:
            return self._fail(types.EMPTY, "keyless: reply had no results")
        return self._outcome(types.search_ok(rows), spent=0.0)

    @staticmethod
    def _parse_rows(text: str, limit: int) -> list[dict[str, Any]]:
        """Accept either a JSON list of hits or the vendor's prose blocks.

        The prose blocks are `Title:` / `URL:` / `Published:` / `Author:` / `Highlights:` followed
        by the page body. Reading them line by line attached whatever came after the URL, which
        was `Published: N/A` — so every result arrived with an empty title and a useless snippet.
        """
        try:
            data = json.loads(text)
            candidates = data if isinstance(data, list) else (data.get("results") or data.get("web") or [])
            rows = [
                types.web_hit(str(c.get("url") or ""), str(c.get("title") or ""),
                              str(c.get("snippet") or c.get("text") or c.get("description") or ""), i)
                for i, c in enumerate(candidates, start=1) if c.get("url")
            ]
            if rows:
                return rows[:limit]
        except (ValueError, AttributeError):
            pass

        blocks: list[dict[str, str]] = []
        current: dict[str, str] = {}
        in_body = False
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line.startswith("Title:"):
                if current.get("url"):
                    blocks.append(current)
                current = {"url": "", "title": line[len("Title:"):].strip(), "description": ""}
                in_body = False
            elif line.startswith("URL:") and current:
                current["url"] = line[len("URL:"):].strip()
            elif line.startswith(("Published:", "Author:")) and current:
                continue
            elif line.startswith("Highlights:") and current:
                in_body = True
            elif in_body and current and not current["description"]:
                if line and not line.startswith(("#", ">", "|", "-", "*")) and len(line) > 20:
                    current["description"] = line[:400]
        if current.get("url"):
            blocks.append(current)
        return [types.web_hit(b["url"], b.get("title", ""), b.get("description", ""), i)
                for i, b in enumerate(blocks[:limit], start=1)]

    def extract(self, urls: list[str]) -> list[dict[str, Any]]:
        def one(url: str) -> dict[str, Any]:
            try:
                # `urls`, plural: the tool validates its arguments and rejects `url`.
                reply = _mcp_call(EXA_MCP, "web_fetch_exa", {"urls": [url]}, 45.0)
                text = _mcp_text(reply)
            except errors.StageError as exc:
                # Keep the kind: a throttled or exhausted anonymous tier must pause this stage,
                # not look like a page the site refused.
                return types.page_error(url, f"keyless: {exc}", kind=exc.kind)
            except Exception as exc:  # one page must not abort the batch
                logger.warning("keyless: unexpected error for %s: %s", url, exc)
                return types.page_error(url, f"keyless: {type(exc).__name__}: {exc}",
                                        kind=types.TRANSIENT)
            if not text.strip():
                return types.page_error(url, "keyless: empty page")
            return types.document(url, "", text, source_url=url)

        return [one(url) for url in urls]
