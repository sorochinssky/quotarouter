"""Configuration: defaults, overrides, and the spend order.

Everything is read from the environment (`QUOTAROUTER_*`, plus each vendor's own key
variable), so a host integration needs no code changes — one variable changes the order:

    QUOTAROUTER_SEARCH_ORDER=exa,firecrawl,deepseek
    QUOTAROUTER_EXTRACT_ORDER=jina,firecrawl,exa

The old `ROUTER_*` names are still read as a fallback, so an existing deployment keeps
working through a rename.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import env_source

logger = logging.getLogger(__name__)

PREFIX = "QUOTAROUTER_"
LEGACY_PREFIX = "ROUTER_"

# Spend order defaults: the biggest free allowance first, the shared-credit vendor after the
# search-only ones, the paid fallback last. `keenable` and `tavily` are listed so that simply
# adding their key switches them on; a vendor without a key is skipped, never dialed.
DEFAULT_SEARCH_ORDER = ("keenable", "exa", "tavily", "firecrawl", "deepseek")
DEFAULT_EXTRACT_ORDER = ("jina", "keenable", "firecrawl", "tavily", "exa", "deepseek")

# Units the ledger counts per vendor. Search and extract can cost differently; each stage
# declares its own unit costs and the ledger accumulates them.
UNIT_CREDITS = "credits"
UNIT_REQUESTS = "requests"
UNIT_USD = "usd"


def _env(name: str, default: str = "") -> str:
    """Read `QUOTAROUTER_<name>`, falling back to the legacy `ROUTER_<name>`.

    Values come through `env_source`, so a host that keeps settings in a dotenv file (Hermes
    does) is honoured without the core knowing that host.
    """
    for key in (f"{PREFIX}{name}", f"{LEGACY_PREFIX}{name}"):
        raw = env_source.resolve(key)
        if raw:
            return raw
    return default


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
        logger.warning("quotarouter: %s=%r is not a number, using %s", name, raw, default)
        return default


def _order(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _env(name)
    if not raw:
        return default
    names = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    return names or default


def env(name: str, default: str = "") -> str:
    """Public accessor for `QUOTAROUTER_<name>` with the legacy `ROUTER_<name>` fallback.

    Other modules (alerts, retention knobs) read their settings through this, so there is one
    place that knows about the rename.
    """
    return _env(name, default)


def default_state_dir() -> Path:
    """`QUOTAROUTER_STATE_DIR` wins; otherwise XDG state (`~/.local/state/quotarouter`).

    Inside Hermes the integration keeps its own directory under XDG too, so the ledger is
    never mixed with the agent's state and the same file serves the CLI, the MCP server and
    the plugin.
    """
    explicit = _env("STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()
    xdg = (os.environ.get("XDG_STATE_HOME", "") or "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "quotarouter"


@dataclass
class Settings:
    search_order: tuple[str, ...] = field(default_factory=lambda: _order("SEARCH_ORDER", DEFAULT_SEARCH_ORDER))
    extract_order: tuple[str, ...] = field(default_factory=lambda: _order("EXTRACT_ORDER", DEFAULT_EXTRACT_ORDER))
    state_dir: Path = field(default_factory=default_state_dir)
    timeout: float = field(default_factory=lambda: _float("TIMEOUT", 25.0))
    extract_timeout: float = field(default_factory=lambda: _float("EXTRACT_TIMEOUT", 45.0))
    # Below this a page is a shell, not an article: the extract chain tries the next stage.
    # Deliberately small — a legitimately short page (example.com is ~160 chars) must NOT be
    # treated as blocked, or the chain burns the next vendor's credits and parks a healthy
    # stage. Bot/JS walls are caught by their own markers, not by length.
    min_page_chars: int = field(default_factory=lambda: int(_float("MIN_PAGE_CHARS", 40)))
    allow_keyless: bool = field(default_factory=lambda: _flag("ALLOW_KEYLESS", True))
    # When every configured stage is spent: answer from the anonymous free tier (True) or fail
    # honestly with the per-stage reason (False). See README "Last resort".
    keyless_last_resort: bool = field(default_factory=lambda: _flag("KEYLESS_LAST_RESORT", True))
    debug: bool = field(default_factory=lambda: _flag("DEBUG", False))
    # Per-URL fetches inside one stage run in a small pool: six URLs must not cost six
    # timeouts in a row. 1 means strictly sequential.
    extract_workers: int = field(default_factory=lambda: max(1, int(_float("EXTRACT_WORKERS", 4))))
    # The audit trail is bounded by rotation in the logging path and by `prune` on a schedule.
    log_max_bytes: int = field(default_factory=lambda: int(_float("LOG_MAX_BYTES", 8 * 1024 * 1024)))
    log_keep_days: int = field(default_factory=lambda: int(_float("LOG_KEEP_DAYS", 30)))
    # How often `quotarouter daemon` (or the systemd timer it prints) refreshes vendor balances.
    probe_interval: float = field(default_factory=lambda: _float("PROBE_INTERVAL", 21600.0))

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / "ledger.json"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "routes.jsonl"


_settings: Optional[Settings] = None


def settings(refresh: bool = False) -> Settings:
    """Cached settings; `refresh=True` re-reads the environment (CLI, tests, host reload)."""
    global _settings
    if _settings is None or refresh:
        _settings = Settings()
    return _settings


def reset_settings_for_tests() -> None:
    global _settings
    _settings = None
