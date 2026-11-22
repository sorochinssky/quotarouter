"""Firecrawl — search + scrape. Free plan: 1,000 credits/month, 2 credits per search.

Unlike the other vendors Firecrawl tells us the truth about the window: `/team/credit-usage`
returns `remainingCredits`, `planCredits` and `billingPeriodEnd`. The router uses it
instead of a calendar-month guess, so a vendor-side reset date is respected exactly.

Credits are shared with page extraction, which is why the default search order puts
Firecrawl after the search-only vendors.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .. import errors, http_client, ledger, types
from ..types import Outcome
from .base import Capability, Stage

logger = logging.getLogger(__name__)

API = "https://api.firecrawl.dev"
SEARCH_CREDITS_PER_CALL = 2.0   # per 10 results, rounded up
EXTRACT_CREDITS_PER_PAGE = 1.0
MONTHLY_CREDITS = 1000.0


class FirecrawlStage(Stage):
    name = "firecrawl"
    display_name = "Firecrawl"
    key_env = "FIRECRAWL_API_KEY"
    unit = "credits"
    monthly_allowance = MONTHLY_CREDITS
    capability = Capability(
        search=True, extract=True,
        search_units=SEARCH_CREDITS_PER_CALL, extract_units=EXTRACT_CREDITS_PER_PAGE,
    )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._env(self.key_env)}", "Content-Type": "application/json"}

    def _call(self, method: str, path: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        if method == "GET":
            resp = http_client.get(f"{API}{path}", headers=self._headers(), timeout=timeout)
        else:
            resp = http_client.post(f"{API}{path}", headers=self._headers(), json=payload, timeout=timeout)
        headers = http_client.header_map(resp)
        text = resp.text or ""
        self._raise_for_status(resp.status_code, text, headers)
        try:
            return resp.json()
        except ValueError as exc:
            raise errors.TransientError(f"Firecrawl returned non-JSON ({exc})", status=resp.status_code) from exc

    def search(self, query: str, limit: int) -> Outcome:
        data = self._call("POST", "/v2/search", {"query": query, "limit": max(1, min(int(limit), 20))}, 30.0)
        web = ((data.get("data") or {}).get("web")) or []
        rows = []
        for index, item in enumerate(web, start=1):
            rows.append(types.web_hit(
                str(item.get("url") or ""),
                str(item.get("title") or ""),
                str(item.get("description") or item.get("snippet") or ""),
                index,
            ))
        if not rows:
            return self._fail(types.EMPTY, "Firecrawl returned no results")
        spent = float(data.get("creditsUsed") or SEARCH_CREDITS_PER_CALL)
        return self._outcome(types.search_ok(rows), spent=spent)

    def extract(self, urls: list[str]) -> list[dict[str, Any]]:
        return self._map_urls(urls, self._one)

    def _one(self, url: str) -> dict[str, Any]:
        """One page: 1 credit when it succeeds, nothing when the site itself refused it."""
        try:
            data = self._call("POST", "/v2/scrape", {"url": url, "formats": ["markdown"]}, 45.0)
        except errors.StageError as exc:
            return types.page_error(url, str(exc))
        body = data.get("data") or {}
        content = str(body.get("markdown") or body.get("content") or "")
        meta = body.get("metadata") or {}
        origin_status = meta.get("statusCode")
        if isinstance(origin_status, int) and origin_status >= 400:
            return types.page_error(url, f"HTTP {origin_status} from origin {url}")
        if not content.strip():
            return types.page_error(url, "empty page")
        return types.document(str(meta.get("sourceURL") or url), str(meta.get("title") or ""), content)

    def probe_quota(self) -> Optional[tuple[float, Optional[float], str]]:
        """Real remaining credits and the vendor's own period end — the only exact window we get.

        Reported through `remaining_override`/`period_override` so the router can store
        "spent = limit − remaining" instead of trusting its own counter.
        """
        data = self._call("GET", "/v2/team/credit-usage", None, 15.0)
        usage = data.get("data") or {}
        remaining = float(usage.get("remainingCredits") or 0.0)
        limit = float(usage.get("planCredits") or remaining or 0.0)
        period_end = usage.get("billingPeriodEnd")
        reset_epoch: Optional[float] = None
        period_key: Optional[str] = None
        if period_end:
            try:
                from datetime import datetime

                dt = datetime.fromisoformat(str(period_end).replace("Z", "+00:00"))
                reset_epoch = dt.timestamp()
                period_key = f"firecrawl:{dt.date().isoformat()}"
            except ValueError:
                logger.debug("firecrawl: unparseable billingPeriodEnd %r", period_end)
        self.remaining_override = remaining
        self.period_override = period_key
        return limit, reset_epoch, "api"
