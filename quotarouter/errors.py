"""Map HTTP status / vendor error text onto the router's error taxonomy.

One place decides what a failure means, so every stage adapter stays dumb:
it raises `QuotaError` / `AuthError` / `ThrottleError` / `TransientError` and the
router turns them into pauses with the right duration.
"""

from __future__ import annotations

import re
from typing import Optional

from . import types


class StageError(Exception):
    """A stage could not serve this call. `kind` drives the pause the router applies."""

    kind = types.UNKNOWN

    def __init__(self, message: str, *, status: Optional[int] = None, retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class QuotaError(StageError):
    kind = types.QUOTA


class AuthError(StageError):
    kind = types.AUTH


class ThrottleError(StageError):
    kind = types.THROTTLE


class TransientError(StageError):
    kind = types.TRANSIENT


_QUOTA_MARKERS = (
    "insufficient_quota",
    "insufficient quota",
    "insufficient balance",
    "out of credits",
    "no_more_credits",
    "credit balance",
    "quota exceeded",
    "monthly limit",
    "billing",
    "payment required",
)

_THROTTLE_MARKERS = ("rate limit", "rate-limit", "ratelimit", "too many requests", "slow down", "qps")


def _retry_after(headers: Optional[dict[str, str]]) -> Optional[float]:
    if not headers:
        return None
    for key, value in headers.items():
        if key.lower() == "retry-after":
            try:
                return float(str(value).strip())
            except (TypeError, ValueError):
                return None
    return None


def classify(status: Optional[int], body: str = "", headers: Optional[dict[str, str]] = None) -> StageError:
    """Build the StageError that best describes a failed HTTP call.

    Order matters: 402 is always QUOTA even though it is also an auth-looking 4xx.
    """
    text = (body or "").lower()
    retry_after = _retry_after(headers)

    if status == 402:
        return QuotaError(f"HTTP 402 {_short(body)}", status=status, retry_after=retry_after)
    if status in (401, 403):
        # Vendors also answer 403 for "key revoked"; a 403 whose body says quota is a quota.
        if any(m in text for m in _QUOTA_MARKERS):
            return QuotaError(f"HTTP {status} {_short(body)}", status=status, retry_after=retry_after)
        return AuthError(f"HTTP {status} {_short(body)}", status=status, retry_after=retry_after)
    if status == 429 or any(m in text for m in _THROTTLE_MARKERS):
        return ThrottleError(f"HTTP {status} {_short(body)}", status=status, retry_after=retry_after)
    if status is not None and status >= 500:
        return TransientError(f"HTTP {status} {_short(body)}", status=status, retry_after=retry_after)
    if any(m in text for m in _QUOTA_MARKERS):
        # 400/200-with-error-body that names an exhausted allowance.
        return QuotaError(f"quota: {_short(body)}", status=status, retry_after=retry_after)
    if status is not None and 400 <= status < 500:
        # Bad request of ours, not the vendor's state: do not pause the stage for long.
        return TransientError(f"HTTP {status} {_short(body)}", status=status, retry_after=retry_after)
    return TransientError(f"HTTP {status} {_short(body)}", status=status, retry_after=retry_after)


# A vendor blocking ONE domain is not a vendor that is down: refusing to serve github.com says
# nothing about example.com, so the stage must not be parked for it. These markers appear in
# Jina's anonymous tier ("Anonymous access to domain github.com blocked until ...").
_DOMAIN_BLOCK_MARKERS = (
    "anonymous access to domain",
    "abusealleviationerror",
    "blocked until",
)

_TRIM = re.compile(r"\s+")


def is_domain_block(text: str) -> bool:
    """True when the vendor refused THIS domain, not the whole account."""
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _DOMAIN_BLOCK_MARKERS)


def short(text: str, limit: int = 160) -> str:
    """Public one-line excerpt of a vendor body, for error text a human will read."""
    return _short(text, limit)


def _short(body: str, limit: int = 160) -> str:
    return _TRIM.sub(" ", (body or "").strip())[:limit]
