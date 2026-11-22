"""Optional integrations around the core router.

Each module here adapts the same `Router` to one host:

* `hermes` — a Hermes Agent web provider, registered as one backend name;
* `mcp`    — an MCP stdio server, so any MCP client gets `web_search` / `web_extract`.

The import is lazy on purpose: importing the package must not require the host to be
installed (`quotarouter.integrations.hermes` needs Hermes, `mcp` needs nothing).
"""

from __future__ import annotations

__all__ = ["hermes", "mcp"]
