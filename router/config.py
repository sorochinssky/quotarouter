"""Configuration: defaults, overrides and the stage registry.

Everything is read from the environment (`ROUTER_*`, plus each vendor's own key variable),
so the plugin needs no edits to Hermes' config.yaml beyond `web.search_backend: router`.

Only ONE variable is needed for the common change — the order:

    ROUTER_SEARCH_ORDER=exa,firecrawl,deepseek
    ROUTER_EXTRACT_ORDER=jina,firecrawl,exa
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Spend order defaults: the biggest free allowance first, the shared-credit vendor after the
# search-only ones, the paid fallback last. `keenable` and `tavily` are listed so that simply
# adding their key in .env switches them on; a vendor without a key is skipped, never dialed.
DEFAULT_SEARCH_ORDER = ("keenable", "exa", "tavily", "firecrawl", "deepseek")
DEFAULT_EXTRACT_ORDER = ("jina", "keenable", "firecrawl", "tavily", "exa", "deepseek")

# Units the ledger counts per vendor. Search and extract can cost differently; the stage
# declares its own unit costs and the ledger simply accumulates them.
UNIT_CREDITS = "credits"
UNIT_REQUESTS = "requests"
UNIT_TOKENS = "tokens"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, default) or "").strip()


def _flag(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("web-router: %s=%r is not a number, using %s", name, raw, default)
        return default


def _order(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _env(name)
    if not raw:
        return default
    names = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    if not names:
        return default
    return names


def default_state_dir() -> Path:
    home = _env("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(_env("ROUTER_STATE_DIR") or (Path(home) / "state" / "web-router"))


@dataclass
class Settings:
    search_order: tuple[str, ...] = field(default_factory=lambda: _order("ROUTER_SEARCH_ORDER", DEFAULT_SEARCH_ORDER))
    extract_order: tuple[str, ...] = field(default_factory=lambda: _order("ROUTER_EXTRACT_ORDER", DEFAULT_EXTRACT_ORDER))
    state_dir: Path = field(default_factory=default_state_dir)
    timeout: float = field(default_factory=lambda: _float("ROUTER_TIMEOUT", 25.0))
    extract_timeout: float = field(default_factory=lambda: _float("ROUTER_EXTRACT_TIMEOUT", 45.0))
    # Below this a page is a shell, not an article: the extract chain tries the next stage.
    # Deliberately small — a legitimately short page (example.com is ~160 chars) must NOT be
    # treated as blocked, or the chain burns the next vendor's credits and parks a healthy
    # stage. Bot/JS walls are caught by their own markers, not by length.
    min_page_chars: int = field(default_factory=lambda: int(_float("ROUTER_MIN_PAGE_CHARS", 40)))
    allow_keyless: bool = field(default_factory=lambda: _flag("ROUTER_ALLOW_KEYLESS", True))
    # When every configured stage is spent: answer from the anonymous free tier (True) or fail
    # honestly with the per-stage reason (False). See README "Last resort".
    keyless_last_resort: bool = field(default_factory=lambda: _flag("ROUTER_KEYLESS_LAST_RESORT", True))
    debug: bool = field(default_factory=lambda: _flag("ROUTER_DEBUG", False))

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / "ledger.json"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "routes.jsonl"


_settings: Optional[Settings] = None


def settings(refresh: bool = False) -> Settings:
    """Cached settings; `refresh=True` re-reads the environment (used by the CLI and tests)."""
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings


def reset_settings_for_tests() -> None:
    global _settings
    _settings = None
