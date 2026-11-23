"""Where a vendor key is looked up.

The core cannot assume a host: under Hermes a key may live in the profile's `.env` file
(loaded by `agent.web_search_provider.get_provider_env`), under a plain script it lives in
`os.environ`, under a container in the process environment too. So the core owns only the
order of lookups, and a host may register its own resolver:

    from quotarouter import env_source
    env_source.register_resolver(lambda name: my_conf(name))

Order: `os.environ` (the most explicit source, easiest to override in a test) → a dotenv file →
registered resolvers. The first non-empty answer wins.

The dotenv file is `QUOTAROUTER_ENV_FILE` when set, otherwise the conventional locations in
order: `./.env` and `~/.config/quotarouter/.env`. Without those defaults the package only found
its keys inside Hugging-Hermes, because a host registers a resolver there — every other MCP
client silently ran on the anonymous tier with `credential: false` for vendors that had keys on
the same machine.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

Resolver = Callable[[str], str]

_resolvers: list[Resolver] = []
_dotenv_cache: Optional[tuple[str, dict[str, str]]] = None


def register_resolver(resolver: Resolver) -> None:
    """Add a host-specific lookup. Called by an integration, never by the core."""
    if resolver not in _resolvers:
        _resolvers.append(resolver)


def registered_resolvers() -> list[Resolver]:
    """A copy of the registered resolvers, so a caller can restore them later."""
    return list(_resolvers)


def set_resolvers(resolvers: list[Resolver]) -> None:
    """Replace the resolver list (test isolation: a host must not leak into a unit test)."""
    _resolvers[:] = list(resolvers)


def clear_resolvers() -> None:
    """Test hook: forget every registered resolver."""
    _resolvers.clear()


DEFAULT_ENV_FILES = ("~/.config/quotarouter/.env",)


def _dotenv_paths() -> list[Path]:
    """Every place a key may live, in order. Explicit setting wins, conventions follow."""
    raw = (os.environ.get("QUOTAROUTER_ENV_FILE", "") or "").strip()
    if raw:
        return [Path(raw).expanduser()]
    candidates = [Path.cwd() / ".env"] + [Path(entry).expanduser() for entry in DEFAULT_ENV_FILES]
    return [path for path in candidates if path.is_file()]


def _first_dotenv_value(name: str) -> str:
    for path in _dotenv_paths():
        value = (_read_dotenv(path).get(name, "") or "").strip()
        if value:
            return value
    return ""


def _read_dotenv(path: Path) -> dict[str, str]:
    """Tiny, dependency-free `KEY=VALUE` reader: comments, blanks, optional quotes."""
    global _dotenv_cache
    key = str(path)
    if _dotenv_cache and _dotenv_cache[0] == key:
        return _dotenv_cache[1]
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip().strip('"').strip("'")
    except OSError as exc:
        logger.debug("quotarouter: env file %s unreadable (%s)", path, exc)
    _dotenv_cache = (key, values)
    return values


def resolve(name: str) -> str:
    """The value of `name`, or an empty string. Never raises, never logs a value."""
    value = (os.environ.get(name, "") or "").strip()
    if value:
        return value
    value = _first_dotenv_value(name)
    if value:
        return value
    for resolver in list(_resolvers):
        try:
            value = (resolver(name) or "").strip()
        except Exception as exc:  # a resolver must never break a call
            logger.debug("quotarouter: env resolver failed for %s (%s)", name, exc)
            continue
        if value:
            return value
    return ""
