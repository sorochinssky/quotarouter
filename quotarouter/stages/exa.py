"""Exa — neural search + page contents. Free plan: $10 of credit per month.

Units: the vendor bills real money per call (`costDollars.total`), so the ledger stores
USD and the search cost is ~$0.007 per call. If Exa's reply carries no cost object we
estimate with `DEFAULT_SEARCH_USD` so the local counter still moves.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .. import errors, http_client, types
from ..types import Outcome
from .base import Capability, Stage

logger = logging.getLogger(__name__)

API = "https://api.exa.ai"

DEFAULT_SEARCH_USD = 0.007     # measured: 5 neural results + highlights
DEFAULT_EXTRACT_USD = 0.001    # $1 per 1,000 pages, one content type
MONTHLY_CREDIT_USD = 10.0


class ExaStage(Stage):
    name = "exa"
    display_name = "Exa"
    key_env = "EXA_API_KEY"
    unit = "usd"
    monthly_allowance = MONTHLY_CREDIT_USD
    capability = Capability(search=True, extract=True, search_units=DEFAULT_SEARCH_USD, extract_units=DEFAULT_EXTRACT_USD)

    # --- helpers ----------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self._env(self.key_env), "Content-Type": "application/json"}

    @staticmethod
    def _retry_recommended(status: Optional[int]) -> bool:
        return status in (429, 408) or (status or 0) >= 500

    def _post(self, path: str, payload: dict[str, Any], timeout: float) -> tuple[dict[str, Any], dict[str, str]]:
        resp = http_client.post(f"{API}{path}", headers=self._headers(), json=payload, timeout=timeout)
        headers = http_client.header_map(resp)
        text = resp.text or ""
        self._raise_for_status(resp.status_code, text, headers)
        try:
            return resp.json(), headers
        except ValueError as exc:
            raise errors.TransientError(f"Exa returned non-JSON ({exc})", status=resp.status_code) from exc

    # --- work -------------------------------------------------------------------------
    def search(self, query: str, limit: int) -> Outcome:
        data, _ = self._post(
            "/search",
            {"query": query, "numResults": max(1, min(int(limit), 20)), "contents": {"highlights": True}},
            25.0,
        )
        rows = []
        for index, item in enumerate(data.get("results") or [], start=1):
            highlights = item.get("highlights") or []
            rows.append(types.web_hit(
                str(item.get("url") or ""),
                str(item.get("title") or ""),
                " ".join(str(h) for h in highlights)[:2000] or str(item.get("summary") or ""),
                index,
            ))
        if not rows:
            return self._fail(types.EMPTY, "Exa returned no results")
        cost = float(((data.get("costDollars") or {}).get("total")) or DEFAULT_SEARCH_USD)
        return self._outcome(types.search_ok(rows), spent=cost, cost_usd=cost)

    def extract(self, urls: list[str]) -> list[dict[str, Any]]:
        data, _ = self._post("/contents", {"urls": urls, "text": True}, 45.0)
        results = []
        for item in data.get("results") or []:
            content = str(item.get("text") or "")
            if content.strip():
                results.append(types.document(str(item.get("url") or ""), str(item.get("title") or ""), content))
        for bad in data.get("statuses") or []:
            if str(bad.get("status")) == "error":
                results.append(types.page_error(str(bad.get("id") or ""), str((bad.get("error") or {}).get("tag") or "error")))
        return results

    def probe_quota(self) -> Optional[tuple[float, Optional[float], str]]:
        """Exa has no public balance endpoint: the ledger's local USD estimate is authoritative."""
        return None
