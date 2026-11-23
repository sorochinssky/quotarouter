"""MCP server: hand the same router to any MCP client, with zero dependencies.

Transport is MCP stdio: one JSON-RPC 2.0 message per line on stdin/stdout. That is the
whole protocol for tools — no SDK needed, and no version drift to track.

Exposed tools:

    web_search   {"query": str, "limit": int?, <chain options>?}  -> ranked results as JSON text
    web_extract  {"urls": [str], <chain options>?}                -> page contents as JSON text
    quota_status {}                              -> what is left per vendor, and the named chains
    quota_probe  {}                              -> ask the vendors for their real balance

<chain options> is `stages`, `chain`, `fallback`, `force`, `budget`, `max_cost_usd`: a call may
name the stages it wants (down to one vendor) and what it may spend, instead of accepting the
operator's default order. Optional, so an old client cannot notice they exist.

Wire it into a client with a command, for example (Claude Desktop `claude_desktop_config.json`):

    {"mcpServers": {"quotarouter": {"command": "python3", "args": ["-m", "quotarouter.cli", "serve-mcp"]}}}

Run it by hand to see the framing:

    echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 -m quotarouter.cli serve-mcp
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, BinaryIO, Optional, TextIO

from .. import routing, types
from ..routing import shared_router as get_router

logger = logging.getLogger(__name__)

SERVER_NAME = "quotarouter"
PROTOCOL_VERSION = "2025-06-18"          # the newest revision this server implements
SUPPORTED_PROTOCOLS = (PROTOCOL_VERSION, "2024-11-05")

#: Options shared by both call tools. Every one is optional: a client that knows none of them keeps
#: getting the configured chain, byte for byte.
CHAIN_PROPERTIES: dict[str, dict[str, Any]] = {
    "stages": {
        "type": "array", "items": {"type": "string"},
        "description": (
            "Pin this call to these providers, in this order; one name is a complete chain. "
            "Only those providers are dialed, and the call fails honestly if none can serve. "
            "Pinning a paid provider spends money — say so to the user before using it."
        ),
    },
    "chain": {
        "type": "string",
        "description": "Use a named chain from this deployment's configuration file instead of "
                       "the default order. Mutually exclusive with `stages`.",
    },
    "fallback": {
        "type": "boolean",
        "description": "After the named stages are exhausted, continue with the default order.",
    },
    "force": {
        "type": "boolean",
        "description": (
            "One diagnostic attempt against a provider this router has parked (out of quota, "
            "throttled). It is charged like any call and re-parked if it fails. It does not "
            "override an operator's stop, a missing key or a missing capability."
        ),
    },
    "budget": {
        "type": "string", "enum": ["free"],
        "description": "'free' means this call may not spend money: only providers inside their "
                       "free allowance may serve it.",
    },
    "max_cost_usd": {
        "type": "number", "minimum": 0,
        "description": "Dollar ceiling for this call. The call is refused before any provider is "
                       "dialed when the estimate exceeds it.",
    },
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "web_search",
        "description": (
            "Search the web through a quota-aware chain of providers. Free monthly "
            "allowances are spent first, so this stays cheap until they run out. "
            "Optional: pin the providers for this one call (`stages` or `chain`) and set a "
            "budget — pinning a paid provider spends money."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20,
                          "description": "How many results to return (default 5)."},
                **CHAIN_PROPERTIES,
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_extract",
        "description": (
            "Fetch clean page text for one or more URLs through the same chain "
            "(a keyless reader first, paid scrapers only when it fails). Optional: pin the "
            "providers for this one call (`stages` or `chain`) and set a budget."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"},
                         "description": "Absolute URLs to fetch."},
                **CHAIN_PROPERTIES,
            },
            "required": ["urls"],
        },
    },
    {
        "name": "quota_status",
        "description": (
            "Show what is left of each provider's allowance right now, which providers are funded "
            "by a prepaid balance, and the named chains this deployment offers."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "quota_probe",
        "description": (
            "Ask the providers themselves for their remaining balance and store it, so the "
            "next call routes on facts instead of reaction to an error. Costs no vendor credits."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class McpServer:
    """Protocol layer only: no I/O of its own, so it can be tested without subprocesses."""

    def __init__(self) -> None:
        self.initialized = False

    # --- JSON-RPC core ------------------------------------------------------------------
    def handle(self, message: dict[str, Any]) -> Optional[dict[str, Any]]:
        """One request in, one response out; notifications return None."""
        method = str(message.get("method") or "")
        message_id = message.get("id")
        is_notification = "id" not in message

        if method == "initialize":
            params = message.get("params") or {}
            requested = str(params.get("protocolVersion") or "")
            # Answer with a revision we actually implement. Echoing whatever the client asked
            # claims support for revisions never written — the reference client negotiated
            # "2025-11-25" against this server and believed it.
            negotiated = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
            self.initialized = True
            return _result(message_id, {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": _version()},
            })
        if method in ("notifications/initialized", "initialized"):
            self.initialized = True
            return None
        if method == "ping":
            return _result(message_id, {})
        if method == "tools/list":
            return _result(message_id, {"tools": TOOLS})
        if method == "tools/call":
            return self._call_tool(message_id, message.get("params") or {})
        if is_notification:
            return None
        return _error(message_id, -32601, f"method not found: {method}")

    # --- tools --------------------------------------------------------------------------
    def _call_tool(self, message_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        try:
            if name == "web_search":
                return _result(message_id, self._web_search(arguments))
            if name == "web_extract":
                return _result(message_id, self._web_extract(arguments))
            if name == "quota_status":
                return _result(message_id, self._quota_status())
            if name == "quota_probe":
                return _result(message_id, self._quota_probe())
        except Exception as exc:  # a tool failure is a result, not a transport failure
            logger.warning("quotarouter mcp: tool %s failed: %s", name, exc)
            return _result(message_id, _text_result(f"error: {exc}", is_error=True))
        return _error(message_id, -32602, f"unknown tool: {name}")

    @staticmethod
    def _chain_options(arguments: dict[str, Any]) -> dict[str, Any]:
        """The optional chain arguments, translated to the library's names.

        `force` is the protocol's word for `ignore_pause`; keeping one name on the wire and another
        in the library is a translation the protocol layer should own, not the caller.
        """
        return {
            "stages": arguments.get("stages"),
            "chain": arguments.get("chain"),
            "fallback": bool(arguments.get("fallback") or False),
            "ignore_pause": bool(arguments.get("force") or False),
            "budget": arguments.get("budget"),
            "max_cost_usd": arguments.get("max_cost_usd"),
        }

    def _web_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _text_result("error: 'query' is required", is_error=True)
        try:
            limit = max(1, min(int(arguments.get("limit") or 5), 20))
        except (TypeError, ValueError):
            limit = 5

        router = get_router()
        try:
            result = router.run_search(query, limit, **self._chain_options(arguments))
        except ValueError as exc:
            return _text_result(f"error: {exc}", is_error=True)
        router.log_route("search", result, {"query": query, "limit": limit})
        if not result.ok:
            return _text_result(routing.format_failure(result, "search"), is_error=True)
        payload = dict(result.payload)
        payload["data"] = {**(payload.get("data") or {}), "backend": f"quotarouter:{result.stage}"}
        return _text_result(json.dumps(payload, ensure_ascii=False, indent=2))

    def _web_extract(self, arguments: dict[str, Any]) -> dict[str, Any]:
        urls = [str(u) for u in (arguments.get("urls") or []) if str(u).strip()]
        if not urls:
            return _text_result("error: 'urls' must contain at least one URL", is_error=True)
        router = get_router()
        try:
            result = router.run_extract(urls, **self._chain_options(arguments))
        except ValueError as exc:
            return _text_result(f"error: {exc}", is_error=True)
        router.log_route("extract", result, {"urls": urls})
        if not result.ok:
            reason = routing.format_failure(result, "extract")
            return _text_result(json.dumps(
                {"results": [types.page_error(url, reason) for url in urls]},
                ensure_ascii=False, indent=2), is_error=True)
        return _text_result(json.dumps({"results": result.payload}, ensure_ascii=False, indent=2))

    def _quota_status(self) -> dict[str, Any]:
        """Everything a caller needs to choose a chain on purpose rather than by guess.

        A model that cannot see the chain names invents them; a model that cannot see which
        provider costs money pins the expensive one by accident.
        """
        from .. import config

        router = get_router()
        settings = config.settings()
        now = router.now()
        vendors = {}
        for name, stage in router.stages.items():
            state = router.ledger.get(name)
            vendors[name] = {
                "credential": stage.has_credential(),
                "capabilities": {"search": stage.supports_search(), "extract": stage.supports_extract()},
                "funding": getattr(stage, "funding", "free"),
                "limit": state.limit,
                "used": round(state.used, 4),
                "unit": stage.unit,
                "paused": state.is_paused(now),
                "pause": state.pause_reason or None,
            }
        report = {
            "vendors": vendors,
            "chains": settings.chains.as_dict(),
            "chains_source": settings.chains.source,
            "allow_chain_override": settings.allow_chain_override,
        }
        return _text_result(json.dumps(report, ensure_ascii=False, indent=2))

    def _quota_probe(self) -> dict[str, Any]:
        """Refresh balances from the vendors. A vendor that cannot answer is reported, not fatal."""
        router = get_router()
        report = router.probe_all(force=True)
        return _text_result(json.dumps(report, ensure_ascii=False, indent=2))

    # --- transport ----------------------------------------------------------------------
    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        """Read line-delimited JSON-RPC until stdin closes."""
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                _write(stdout, _error(None, -32700, "parse error"))
                continue
            response = self.handle(message)
            if response is not None:
                _write(stdout, response)


def serve_stdio(stdin: Optional[BinaryIO] = None, stdout: Optional[BinaryIO] = None) -> int:
    """Entry point for `quotarouter serve-mcp`: text-mode stdio, line delimited."""
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    if hasattr(input_stream, "buffer"):
        input_stream = input_stream.buffer  # type: ignore[assignment]
    if hasattr(output_stream, "buffer"):
        output_stream = output_stream.buffer  # type: ignore[assignment]
    import io

    reader = io.TextIOWrapper(input_stream, encoding="utf-8", newline="\n")  # type: ignore[arg-type]
    writer = io.TextIOWrapper(output_stream, encoding="utf-8", newline="\n", write_through=True)
    try:
        McpServer().serve(reader, writer)
    finally:
        writer.flush()
    return 0


# --- helpers ------------------------------------------------------------------------------
def _version() -> str:
    from .. import __version__

    return __version__


def _result(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    # `isError` is always present: an explicit false beats a field a client has to default
    # itself, and clients that log the raw result then show the outcome unambiguously.
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


def _write(stream: TextIO, message: dict[str, Any]) -> None:
    stream.write(json.dumps(message, ensure_ascii=False) + "\n")
    stream.flush()
