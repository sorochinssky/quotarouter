"""Persistent quota ledger: what each vendor has spent, what it has left, and who is paused.

Lives in one JSON file (default `~/.hermes/state/web-router/ledger.json`), written
atomically, guarded by an in-process lock and a cross-process file lock.

Why a file and not memory: Hermes restarts (gateway restart, `/new`, crash) must not
resurrect a vendor whose monthly allowance is spent. In-memory pauses are what make the
sibling plugins ask an empty vendor again after every restart.

Reset rules, per vendor:
  * `period` vendors (exa, firecrawl, tavily, keenable, brave, parallel) — the window is a
    calendar month or the vendor's own billing period; the ledger rolls the window when the
    stored period key changes.
  * `rolling` vendors (jina) — a sliding rate window; nothing monthly to roll.
  * `balance` vendors (deepseek) — no window; the vendor reports availability.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

LOCK_TIMEOUT_SECONDS = 5.0


def utcnow() -> float:
    return time.time()


def iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds") if ts else None


def month_key(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else utcnow(), tz=timezone.utc)
    return f"{dt.year:04d}-{dt.month:02d}"


def next_month_epoch(ts: Optional[float] = None) -> float:
    dt = datetime.fromtimestamp(ts if ts is not None else utcnow(), tz=timezone.utc)
    year, month = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return datetime(year, month, 1, tzinfo=timezone.utc).timestamp()


@dataclass
class VendorState:
    """Everything the router remembers about one vendor."""

    vendor: str
    period: str = ""                       # "2026-11" | vendor's own window key | ""
    limit: float = 0.0                     # allowance for this period, in vendor units
    used: float = 0.0                      # units spent this period
    source: str = "local"                  # "api" when the vendor reported it, "local" when estimated
    reset_at: Optional[float] = None       # epoch seconds; None = no monthly window
    paused_until: Optional[float] = None
    pause_kind: str = ""
    pause_reason: str = ""
    consecutive_failures: int = 0
    last_success: Optional[float] = None
    last_failure: Optional[float] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def remaining(self) -> float:
        return max(0.0, self.limit - self.used)

    def has_room(self, cost: float) -> bool:
        return self.limit > 0 and (self.limit - self.used) >= cost

    def is_paused(self, now: Optional[float] = None) -> bool:
        return self.paused_until is not None and self.paused_until > (now if now is not None else utcnow())

    def pause_left(self, now: Optional[float] = None) -> float:
        """Seconds left on the pause; 0 when not paused. Infinite off a `disable`."""
        moment = now if now is not None else utcnow()
        if self.paused_until is None or self.paused_until <= moment:
            return 0.0
        return self.paused_until - moment


class Ledger:
    """JSON-backed quota state. Thread-safe; resilient to a corrupted file."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser()
        self._lock = threading.RLock()
        self._state: dict[str, VendorState] = {}
        self._loaded = False

    # --- persistence ------------------------------------------------------------------
    def _load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            if not self.path.exists():
                self._state = {}
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                vendors = raw.get("vendors", {})
                self._state = {name: _state_from_dict(name, data) for name, data in vendors.items()}
            except Exception as exc:  # a corrupted ledger must never break search
                logger.warning("quotarouter: ledger %s unreadable (%s); starting fresh", self.path, exc)
                self._state = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "updated_at": iso(utcnow()), "vendors": {n: asdict(s) for n, s in self._state.items()}}
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".ledger-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)  # atomic on POSIX and Windows
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # --- access -----------------------------------------------------------------------
    def get(self, vendor: str) -> VendorState:
        with self._lock:
            self._load()
            state = self._state.get(vendor)
            if state is None:
                state = VendorState(vendor=vendor)
                self._state[vendor] = state
            return state

    def set_limit(self, vendor: str, limit: float, *, source: str = "local", reset_at: Optional[float] = None,
                  period: Optional[str] = None) -> VendorState:
        """Declare the vendor's allowance for the current window, rolling it if the window changed."""
        with self._lock:
            state = self.get(vendor)
            want_period = period if period is not None else month_key()
            if state.period != want_period:
                state.period = want_period
                state.used = 0.0
                # A fresh window clears a quota pause: the vendor will answer again.
                if state.pause_kind == "quota":
                    state.paused_until = None
                    state.pause_kind = ""
                    state.pause_reason = ""
            state.limit = float(limit)
            state.source = source
            if reset_at is not None:
                state.reset_at = reset_at
            self._save()
            return state

    def charge(self, vendor: str, units: float, *, cost_usd: float = 0.0) -> VendorState:
        with self._lock:
            state = self.get(vendor)
            if units:
                state.used = round(state.used + float(units), 6)
            if cost_usd:
                state.extra["cost_usd"] = round(float(state.extra.get("cost_usd", 0.0)) + float(cost_usd), 6)
            state.last_success = utcnow()
            state.consecutive_failures = 0
            self._save()
            return state

    def note_success(self, vendor: str) -> VendorState:
        with self._lock:
            state = self.get(vendor)
            state.last_success = utcnow()
            state.consecutive_failures = 0
            # A successful call proves the vendor is back: drop a transient pause.
            if state.pause_kind in ("", "throttle", "transient", "timeout", "unknown"):
                state.paused_until = None
                state.pause_kind = ""
                state.pause_reason = ""
            self._save()
            return state

    def pause(self, vendor: str, until: float, kind: str, reason: str = "") -> VendorState:
        with self._lock:
            state = self.get(vendor)
            state.paused_until = max(state.paused_until or 0.0, float(until))
            state.pause_kind = kind
            state.pause_reason = reason[:300]
            state.consecutive_failures += 1
            state.last_failure = utcnow()
            self._save()
            return state

    def disable(self, vendor: str, reason: str = "disabled by operator") -> VendorState:
        """Operator kill switch: paused forever (until `enable`)."""
        return self.pause(vendor, float("inf"), "disabled", reason)

    def enable(self, vendor: str) -> VendorState:
        with self._lock:
            state = self.get(vendor)
            state.paused_until = None
            state.pause_kind = ""
            state.pause_reason = ""
            self._save()
            return state

    def snapshot(self) -> dict[str, VendorState]:
        with self._lock:
            self._load()
            return {name: state for name, state in self._state.items()}

    def reset(self, vendor: str) -> VendorState:
        """Forget the local accounting for one vendor (after a plan change or a vendor reset)."""
        with self._lock:
            self._load()
            state = VendorState(vendor=vendor)
            self._state[vendor] = state
            self._save()
            return state

    def reset_all(self) -> None:
        with self._lock:
            self._state = {}
            self._loaded = True
            self._save()


def _state_from_dict(vendor: str, data: dict[str, Any]) -> VendorState:
    known = {f for f in VendorState.__dataclass_fields__}  # type: ignore[attr-defined]
    clean = {k: v for k, v in (data or {}).items() if k in known and k != "vendor"}
    return VendorState(vendor=vendor, **clean)


def backoff_seconds(kind: str, attempt: int, retry_after: Optional[float] = None) -> float:
    """How long to pause a vendor after a failure.

    quota      -> caller decides (until the window resets); AUTH -> 24h (operator should fix the key)
    throttle   -> 60s, doubling per consecutive failure, capped at 10 min
    otherwise  -> 60s, capped at 10 min
    """
    if retry_after:
        return max(1.0, min(float(retry_after), 600.0))
    if kind == "throttle":
        return min(60.0 * math.pow(2, max(0, attempt - 1)), 600.0)
    if kind == "auth":
        return 24 * 3600.0
    return 60.0
