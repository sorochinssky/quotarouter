"""Shared types for the router: one response envelope, one error taxonomy.

The success shapes are Hermes' own web-provider contract (see
`agent/web_search_provider.py`), so the router never translates for the caller:

    search  -> {"success": True, "data": {"web": [{"title", "url", "description", "position"}]}}
    extract -> [{"url", "title", "content", "raw_content", "metadata"}]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# --- Error kinds ---------------------------------------------------------------------------
# QUOTA      : the allowance is spent (402, insufficient_quota, credit balance exhausted)
# AUTH       : the key is wrong/revoked (401/403) — retried only after the operator fixes it
# THROTTLE   : rate limit (429) — the vendor works again in seconds
# TRANSIENT  : 5xx, timeout, DNS, connection reset — retry soon
# EMPTY      : the vendor answered successfully with nothing usable (0 hits / blank page)
# BAD_PAGE   : extract only — a page that came back blocked, truncated or a JS stub
# TIMEOUT    : the call exceeded its deadline
# UNKNOWN    : anything unclassified — treated like TRANSIENT
QUOTA = "quota"
AUTH = "auth"
THROTTLE = "throttle"
TRANSIENT = "transient"
EMPTY = "empty"
BAD_PAGE = "bad_page"
TIMEOUT = "timeout"
UNKNOWN = "unknown"

# Failures that mean "this vendor cannot serve us for a while".
VENDOR_DOWN_KINDS = frozenset({QUOTA, AUTH, THROTTLE, TRANSIENT, TIMEOUT, UNKNOWN})


@dataclass(frozen=True)
class Failure:
    """A vendor call that produced no usable result."""

    kind: str
    message: str
    retry_after: Optional[float] = None  # seconds, if the vendor said so


@dataclass
class Outcome:
    """Normalised result of one stage attempt.

    `payload` is either a search envelope (`{"web": [...]}`) or an extract list
    (`[{"url", "title", "content", ...}]`). It is None when `failure` is set.
    """

    payload: Any = None
    failure: Optional[Failure] = None
    spent: float = 0.0            # units (credits/requests) this call consumed
    cost_usd: float = 0.0         # only vendors that report real money cost
    http_status: Optional[int] = None
    raw_headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.failure is None


def search_ok(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"success": True, "data": {"web": rows}}


def search_fail(error: str) -> dict[str, Any]:
    return {"success": False, "error": error}


def web_hit(url: str, title: str, description: str, position: int) -> dict[str, Any]:
    """One search row, key order as the model sees it."""
    return {
        "title": title,
        "url": url,
        "description": description,
        "position": position,
    }


def document(url: str, title: str, content: str, *, source_url: Optional[str] = None) -> dict[str, Any]:
    """One successful extract entry; `raw_content` mirrors `content` for the legacy pipeline."""
    return {
        "url": url,
        "title": title,
        "content": content,
        "raw_content": content,
        "metadata": {"sourceURL": url if source_url is None else source_url, "title": title},
    }


def page_error(url: str, error: str, *, kind: str = "") -> dict[str, Any]:
    """One page that did not come out.

    `kind` separates "this page is closed to everyone" (empty — the site's own answer) from
    "the vendor refused" (`quota`, `auth`, `throttle`, `transient`). The router treats those
    two differently: a page-specific refusal must not park the stage, a vendor refusal must.
    """
    return {"url": url, "title": "", "content": "", "error": error, "kind": kind}
