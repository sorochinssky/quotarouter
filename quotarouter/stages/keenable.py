"""Keenable — search + fetch. 100,000 free requests per month with a key (no card).

The largest free allowance we can find, which is why it is first in the default search
order whenever its key exists. Responses carry `usage.credits`, so the local counter is
reconciled on every call.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .. import errors, http_client, types
from ..types import Outcome
from .base import Capability, Stage

logger = logging.getLogger(__name__)

API = "https://api.keenable.ai"
MONTHLY_REQUESTS = 100_000.0


class KeenableStage(Stage):
    name = "keenable"
    display_name = "Keenable"
    key_env = "KEENABLE_API_KEY"
    unit = "requests"
    monthly_allowance = MONTHLY_REQUESTS
    capability = Capability(search=True, extract=True, search_units=1.0, extract_units=1.0)

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._env(self.key_env), "Content-Type": "application/json"}

    def _post(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        resp = http_client.post(f"{API}{path}", headers=self._headers(), json=payload, timeout=timeout)
        headers = http_client.header_map(resp)
        text = resp.text or ""
        self._raise_for_status(resp.status_code, text, headers)
        try:
            return resp.json()
        except ValueError as exc:
            raise errors.TransientError(f"Keenable returned non-JSON ({exc})", status=resp.status_code) from exc

    def search(self, query: str, limit: int) -> Outcome:
        data = self._post("/v1/search", {"query": query, "max_results": max(1, min(int(limit), 50))}, 30.0)
        items = data.get("results") or data.get("data") or []
        rows = []
        for index, item in enumerate(items, start=1):
            rows.append(types.web_hit(
                str(item.get("url") or ""),
                str(item.get("title") or ""),
                str(item.get("snippet") or item.get("description") or ""),
                index,
            ))
        if not rows:
            return self._fail(types.EMPTY, "Keenable returned no results")
        spent = float(((data.get("usage") or {}).get("credits")) or 1.0)
        return self._outcome(types.search_ok(rows), spent=spent)

    def extract(self, urls: list[str]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for url in urls:
            try:
                data = self._post("/v1/fetch", {"url": url}, 45.0)
            except errors.StageError as exc:
                results.append(types.page_error(url, str(exc)))
                continue
            content = str(data.get("content") or data.get("markdown") or data.get("text") or "")
            if not content.strip():
                results.append(types.page_error(url, "keenable: empty page"))
                continue
            results.append(types.document(str(data.get("url") or url), str(data.get("title") or ""), content,
                                          source_url=url))
        return results

    def probe_quota(self) -> Optional[tuple[float, Optional[float], str]]:
        """No public remaining-requests endpoint: the calendar-month local counter stands."""
        return None
