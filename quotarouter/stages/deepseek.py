"""DeepSeek Search / Fetch — paid fallback on the model API key.

$0.75 per 1,000 successful requests per endpoint, 5 QPS. No monthly allowance: the balance
is money, so the ledger treats `DEEPSEEK_BALANCE_USD` (optional override) as the ceiling and
`/user/balance` as the live availability check. A negative balance answers 402, which the
router already classifies as QUOTA and parks until the operator tops up.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .. import errors, http_client, types
from ..types import Outcome
from .base import Capability, Stage

logger = logging.getLogger(__name__)

API = "https://api.deepseek.com"
USD_PER_CALL = 0.00075  # $0.75 / 1000 successful requests


class DeepSeekStage(Stage):
    name = "deepseek"
    display_name = "DeepSeek Search"
    key_env = "DEEPSEEK_API_KEY"
    unit = "usd"
    monthly_allowance = None  # balance, not a monthly allowance
    capability = Capability(search=True, extract=True, search_units=USD_PER_CALL, extract_units=USD_PER_CALL)

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
            raise errors.TransientError(f"DeepSeek returned non-JSON ({exc})", status=resp.status_code) from exc

    def search(self, query: str, limit: int) -> Outcome:
        try:
            data = self._post("/v1/tools/search", {"query": query, "max_results": max(1, min(int(limit), 50))}, 30.0)
        except errors.StageError as exc:
            # Every other stage reports failure as an Outcome; raising here made the stage's
            # contract depend on which vendor it was (a 402 arrived as an exception, not as
            # `kind=quota`).
            return self._fail(exc.kind, f"deepseek: {exc}", status=exc.status, retry_after=exc.retry_after)
        rows = []
        for index, item in enumerate(data.get("results") or [], start=1):
            rows.append(types.web_hit(
                str(item.get("url") or ""),
                str(item.get("title") or ""),
                str(item.get("summary") or item.get("content") or item.get("description") or ""),
                index,
            ))
        if not rows:
            return self._fail(types.EMPTY, "DeepSeek returned no results")
        return self._outcome(types.search_ok(rows), spent=USD_PER_CALL, cost_usd=USD_PER_CALL)

    def extract(self, urls: list[str]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for url in urls:
            try:
                data = self._post("/v1/tools/fetch", {"url": url}, 25.0)
            except errors.StageError as exc:
                results.append(types.page_error(url, f"deepseek: {exc}", kind=exc.kind))
                continue
            content = str(data.get("content") or data.get("markdown") or "")
            if not content.strip():
                results.append(types.page_error(url, "deepseek: empty page"))
                continue
            results.append(types.document(str(data.get("url") or url), str(data.get("title") or ""), content))
        return results

    def probe_quota(self) -> Optional[tuple[float, Optional[float], str]]:
        """`is_available: false` means the account cannot pay — park it instead of dialing."""
        limit = self._env("DEEPSEEK_BALANCE_USD")
        try:
            resp = http_client.get(f"{API}/user/balance", headers=self._headers(), timeout=10.0)
            if resp.status_code >= 400:
                return None
            data = resp.json()
            available = bool(data.get("is_available"))
            total = 0.0
            for info in data.get("balance_infos") or []:
                try:
                    total += float(info.get("total_balance") or 0.0)
                except (TypeError, ValueError):
                    continue
            if not available or total <= 0:
                self.blocked_reason = "deepseek: balance empty (is_available=false)"
                self.remaining_override = 0.0
                return 0.0, None, "api"
            self.remaining_override = total
            return max(total, float(limit or 0.0)), None, "api"
        except Exception as exc:
            logger.debug("deepseek balance probe failed: %s", exc)
            return None
