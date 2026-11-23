"""HTTP client factory — the only place that touches the network transport.

Tests replace the transport with `httpx.MockTransport`, so every stage is exercised with
recorded vendor payloads and no socket is opened.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from . import errors

_client: Optional[httpx.Client] = None


def set_transport(transport: httpx.BaseTransport | None) -> None:
    """Install a mock transport (tests) or reset to the real one (None)."""
    global _client
    if _client is not None:
        _client.close()
        _client = None
    if transport is not None:
        _client = httpx.Client(transport=transport, timeout=60.0)


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=60.0)
    return _client


def _call(what: str, function, *args, **kwargs) -> httpx.Response:
    """Turn a transport failure into the package's own error type.

    Every stage catches `StageError` and decides whether to pause itself, and every one of those
    branches was dead code for real network problems: `httpx.ReadTimeout` and `ConnectError`
    arrived as bare exceptions, so a vendor that had gone dark looked like one bad page and was
    dialed again on the next call, and the one after that.
    """
    try:
        return function(*args, **kwargs)
    except httpx.TimeoutException as exc:
        raise errors.TransientError(f"{what} timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise errors.TransientError(f"{what} failed: {type(exc).__name__}: {exc}") from exc


def get(url: str, *, headers: dict[str, str] | None = None, params: dict[str, Any] | None = None,
        timeout: float = 25.0) -> httpx.Response:
    return _call("GET " + url, client().get, url, headers=headers, params=params, timeout=timeout)


def post(url: str, *, headers: dict[str, str] | None = None, json: Any = None,
         timeout: float = 25.0) -> httpx.Response:
    return _call("POST " + url, client().post, url, headers=headers, json=json, timeout=timeout)


def header_map(response: httpx.Response) -> dict[str, str]:
    try:
        return {k.lower(): v for k, v in response.headers.items()}
    except Exception:
        return {}


def reset_transport() -> None:
    """Drop any installed transport so the next call goes to the network (used by live tests).

    This used to set a `_TRANSPORT` global that nothing reads, so a mock installed by an
    earlier test stayed in force and a "live" run silently answered from fixtures. Closing the
    client is the only thing that actually removes the transport.
    """
    set_transport(None)
