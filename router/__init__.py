"""hermes-web-router — quota-aware multi-provider routing for Hermes `web_search` / `web_extract`.

Registered as ONE Hermes provider (`router`): pick it with

    web.search_backend: router      # optional
    web.extract_backend: router     # optional: separate chain per capability

Order, quotas and pauses are described in README.md and docs/vendor-probes.md.
"""

from __future__ import annotations

from .provider import RouterProvider

__version__ = "0.1.0"


def register(ctx) -> None:
    """Plugin entry point — called once at load time."""
    ctx.register_web_search_provider(RouterProvider())
