"""quotarouter — spend free monthly allowances first, route by remaining quota.

One provider-agnostic core:

    from quotarouter import Router
    router = Router()
    result = router.run_search("query", limit=5)            # -> RouteResult
    payload = router.run_extract(["https://example.com/"])  # -> RouteResult as well
    pinned = router.run_search("query", 5, stages=["exa"])  # one call, its own chain

Integrations live behind the core and stay optional:

    quotarouter.integrations.hermes  — Hermes Agent web provider (`web.search_backend`)
    quotarouter.integrations.mcp     — MCP server for any MCP client
    quotarouter.cli                  — status / probe / route / chains / call / selftest / serve-mcp

Nothing here imports Hermes, so the package works in a plain Python process.
"""

from __future__ import annotations

from .config import Settings, settings
from .routing import Router, RouteResult, format_failure

__version__ = "0.3.2"

__all__ = ["Router", "RouteResult", "Settings", "settings", "format_failure", "__version__"]
