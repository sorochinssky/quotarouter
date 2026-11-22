"""Hermes Agent adapter: the router as ONE web provider named `quotarouter`.

Hermes' contract (`agent/web_search_provider.py`):

* `is_available()` — cheap and offline, no network;
* `search()` / `extract()` — sync or async; `extract()` may be either, the dispatcher adapts;
* the class MUST inherit `WebSearchProvider` — the registry rejects anything else and the
  agent then sees "no provider" while the plugin looks loaded.

Selecting it is one config line: `web.search_backend: quotarouter`.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from .. import config, env_source, routing, types

logger = logging.getLogger(__name__)

try:  # Hermes is present at runtime; the core stays importable without it.
    from agent.web_search_provider import WebSearchProvider as _ProviderBase
except Exception:  # pragma: no cover - exercised by the standalone test run
    _ProviderBase = object  # type: ignore[assignment,misc]

PROVIDER_NAME = "quotarouter"


def _hermes_env(name: str) -> str:
    """Hermes' own lookup: `os.environ` plus the active profile's `.env` file.

    Registered here, in the integration, so the core stays free of Hermes imports. Without
    this, keys written only into `~/.hermes/.env` would be invisible to the chain.
    """
    try:
        from agent.web_search_provider import get_provider_env  # type: ignore[import-not-found]

        return get_provider_env(name) or ""
    except Exception:
        return ""


env_source.register_resolver(_hermes_env)

_SHARED: Optional["RouterProvider"] = None
_SHARED_LOCK = threading.Lock()


def get_router() -> routing.Router:
    """The process-wide router, so the CLI and the provider share one ledger instance."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = RouterProvider()
        return _SHARED.router


class RouterProvider(_ProviderBase):  # type: ignore[misc,valid-type]
    """Web search + extract backend that spends free allowances in a fixed order."""

    name = PROVIDER_NAME
    display_name = "Quota router"

    def __init__(self) -> None:
        self._router: Optional[routing.Router] = None
        self._lock = threading.Lock()

    @property
    def router(self) -> routing.Router:
        """Built lazily so plugin import stays cheap and settings are read at call time."""
        with self._lock:
            if self._router is None:
                self._router = routing.Router()
            return self._router

    def reset_for_tests(self) -> None:
        with self._lock:
            self._router = None

    # --- provider surface ---------------------------------------------------------------
    def is_available(self) -> bool:
        """True when any stage has a credential (or the keyless tier is enabled). No network."""
        try:
            settings = config.settings(refresh=True)
            for stage in self.router.stages.values():
                if stage.has_credential():
                    return True
            return bool(settings.allow_keyless)
        except Exception as exc:
            logger.debug("quotarouter.is_available failed: %s", exc)
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
        logger.info("quotarouter search '%s' limit=%d -> %s", query, limit, result.summary())
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
        logger.info("quotarouter extract %d url(s) -> %s", len(urls), result.summary())
        if not result.ok:
            reason = routing.format_failure(result, "extract")
            return [types.page_error(url, reason) for url in urls]
        return result.payload

    def get_setup_schema(self) -> dict[str, Any]:
        """Row for the `hermes tools` picker."""
        return {
            "name": "Quota router (spend free allowances first)",
            "badge": "quotarouter",
            "tag": "Routes web_search/web_extract across the configured providers by remaining free quota.",
            "env_vars": [],
        }
