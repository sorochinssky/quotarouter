"""Hermes provider adapter: exposes the router as ONE backend named `router`.

Hermes' contract (agent/web_search_provider.py):
  * `is_available()` — cheap, offline, no network;
  * `search()` / `extract()` — sync or async; `extract()` may be either, the dispatcher adapts.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from . import config, routing, types

logger = logging.getLogger(__name__)

try:  # Hermes is present at runtime; the offline test suite must import without it.
    from agent.web_search_provider import WebSearchProvider as _ProviderBase
except Exception:  # pragma: no cover - exercised by the standalone test run
    _ProviderBase = object  # type: ignore[assignment,misc]

PROVIDER_NAME = "router"

_SHARED: Optional["RouterProvider"] = None
_SHARED_LOCK = threading.Lock()


def get_router() -> routing.Router:
    """The process-wide router — what the CLI uses, so the ledger and probe cache are shared."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = RouterProvider()
        return _SHARED.router


class RouterProvider(_ProviderBase):  # type: ignore[misc,valid-type]
    """Web search + extract backend that spends free allowances in a fixed order.

    Inherits `agent.web_search_provider.WebSearchProvider` when Hermes is importable: the
    registry refuses any provider that is not an instance of that ABC, so this is not
    cosmetic — without the base class the plugin loads and registers nothing.
    """

    name = PROVIDER_NAME
    display_name = "Quota router"

    def __init__(self) -> None:
        self._router: Optional[routing.Router] = None
        self._lock = threading.Lock()

    # --- lazily built so plugin import stays cheap and settings are read at call time ----
    @property
    def router(self) -> routing.Router:
        with self._lock:
            if self._router is None:
                self._router = routing.Router()
            return self._router

    def reset_for_tests(self) -> None:
        with self._lock:
            self._router = None

    # --- provider surface ---------------------------------------------------------------
    def is_available(self) -> bool:
        """Any stage with a credential (or the keyless tier) can serve: no network here."""
        try:
            settings = config.settings(refresh=True)
            for stage in self.router.stages.values():
                if stage.has_credential():
                    return True
            return bool(settings.allow_keyless)
        except Exception as exc:
            logger.debug("router.is_available failed: %s", exc)
            return False

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        try:
            limit = max(1, min(int(limit), 20))
        except (TypeError, ValueError):
            limit = 5
        result = self.router.run_search(query, limit)
        self.router.log_route("search", result, {"query": query, "limit": limit})
        logger.info("web-router search '%s' limit=%d -> %s", query, limit, result.summary())
        if not result.ok:
            return types.search_fail(routing.format_failure(result, "search"))
        payload = dict(result.payload)
        data = dict(payload.get("data") or {})
        data["backend"] = f"{PROVIDER_NAME}:{result.stage}"
        payload["data"] = data
        return payload

    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        urls = [str(u) for u in urls]
        result = self.router.run_extract(urls)
        self.router.log_route("extract", result, {"urls": urls})
        logger.info("web-router extract %d url(s) -> %s", len(urls), result.summary())
        if not result.ok:
            reason = routing.format_failure(result, "extract")
            return [types.page_error(url, reason) for url in urls]
        return result.payload

    def get_setup_schema(self) -> dict[str, Any]:
        """Row for the `hermes tools` picker."""
        return {
            "name": "Quota router (spend free allowances first)",
            "badge": "router",
            "tag": "Routes web_search/web_extract across the configured providers by remaining free quota.",
            "env_vars": [],
        }
