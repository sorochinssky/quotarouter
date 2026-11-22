"""Tavily — search + extract. Free "Researcher" plan: 1,000 credits/month, no card.

Basic search = 1 credit, basic extract = 1 credit per 5 successful URLs. The vendor exposes
`GET /usage` with the spent and limit for the current key, so the ledger can trust it.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .. import errors, http_client, types
from ..types import Outcome
from .base import Capability, Stage

logger = logging.getLogger(__name__)

API = "https://api.tavily.com"
SEARCH_CREDITS = 1.0
EXTRACT_CREDITS_PER_PAGE = 0.2  # one credit per five successful extractions
MONTHLY_CREDITS = 1000.0


class TavilyStage(Stage):
    name = "tavily"
    display_name = "Tavily"
    key_env = "TAVILY_API_KEY"
    unit = "credits"
    monthly_allowance = MONTHLY_CREDITS
    capability = Capability(
        search=True, extract=True,
        search_units=SEARCH_CREDITS, extract_units=EXTRACT_CREDITS_PER_PAGE,
    )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._env(self.key_env)}", "Content-Type": "application/json"}

    def _post(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        resp = http_client.post(f"{API}{path}", headers=self._headers(), json=payload, timeout=timeout)
        headers = http_client.header_map(resp)
        text = resp.text or ""
        self._raise_for_status(resp.status_code, text, headers)
        try:
            return resp.json()
        except ValueError as exc:
            raise errors.TransientError(f"Tavily returned non-JSON ({exc})", status=resp.status_code) from exc

    def search(self, query: str, limit: int) -> Outcome:
        data = self._post("/search", {"query": query, "max_results": max(1, min(int(limit), 20)),
                                      "search_depth": "basic"}, 30.0)
        rows = []
        for index, item in enumerate(data.get("results") or [], start=1):
            rows.append(types.web_hit(
                str(item.get("url") or ""),
                str(item.get("title") or ""),
                str(item.get("content") or item.get("raw_content") or ""),
                index,
            ))
        if not rows:
            return self._fail(types.EMPTY, "Tavily returned no results")
        return self._outcome(types.search_ok(rows), spent=SEARCH_CREDITS)

    def extract(self, urls: list[str]) -> list[dict[str, Any]]:
        data = self._post("/extract", {"urls": urls, "extract_depth": "basic"}, 45.0)
        results = []
        for item in data.get("results") or []:
            content = str(item.get("raw_content") or item.get("content") or "")
            if content.strip():
                results.append(types.document(str(item.get("url") or ""), str(item.get("title") or ""), content))
        for bad in data.get("failed_results") or []:
            results.append(types.page_error(str(bad.get("url") or ""), str(bad.get("error") or "tavily: failed")))
        return results

    def probe_quota(self) -> Optional[tuple[float, Optional[float], str]]:
        resp = http_client.get(f"{API}/usage", headers=self._headers(), timeout=10.0)
        if resp.status_code >= 400:
            return None
        data = resp.json() if resp.text else {}
        key = (data.get("key") or {}) if isinstance(data, dict) else {}
        account = (data.get("account") or {}) if isinstance(data, dict) else {}
        used = key.get("usage")
        limit = account.get("plan_limit") or key.get("limit")
        if used is None or limit is None:
            return None
        limit_f = float(limit)
        self.remaining_override = max(0.0, limit_f - float(used))
        return limit_f, None, "api"
