"""Error taxonomy: what each vendor answer means, and how long the stage rests."""

from __future__ import annotations

import pytest

from router import errors, types
from router.ledger import backoff_seconds


@pytest.mark.parametrize("status,body,expected", [
    (402, '{"error":"NO_MORE_CREDITS"}', types.QUOTA),
    (402, "Insufficient Balance", types.QUOTA),
    (401, "invalid api key", types.AUTH),
    (403, "forbidden", types.AUTH),
    (403, '{"error":"insufficient_quota"}', types.QUOTA),      # 403 that really means quota
    (429, "Too Many Requests", types.THROTTLE),
    (200, '{"error":"rate limit exceeded"}', types.THROTTLE),
    (500, "boom", types.TRANSIENT),
    (503, "unavailable", types.TRANSIENT),
    (400, "bad query", types.TRANSIENT),
    (None, '{"error":"credit balance is too low"}', types.QUOTA),
])
def test_classify(status, body, expected):
    assert errors.classify(status, body).kind == expected


def test_retry_after_is_parsed():
    exc = errors.classify(429, "", {"Retry-After": "42"})
    assert exc.retry_after == 42.0


def test_retry_after_garbage_is_ignored():
    exc = errors.classify(429, "", {"Retry-After": "soon"})
    assert exc.retry_after is None


def test_backoff_throttle_doubles_and_caps():
    assert backoff_seconds("throttle", 1) == 60.0
    assert backoff_seconds("throttle", 2) == 120.0
    assert backoff_seconds("throttle", 10) == 600.0


def test_backoff_respects_retry_after():
    assert backoff_seconds("throttle", 3, retry_after=5.0) == 5.0


def test_backoff_auth_pauses_a_day():
    assert backoff_seconds("auth", 1) == 86400.0
