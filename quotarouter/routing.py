"""Routing core: choose by remaining quota, fail over on failure, remember everything.

The order of work for one call:

1. build the candidate list — configured order ∩ has credential ∩ has room ∩ not paused
2. sync quota from vendors that report it (`probe_quota`), at most every `PROBE_TTL`
3. walk the candidates; charge the ledger on success, pause the stage on failure
4. if nothing served and the order allows it, try the keyless last resort
5. return the honest error naming every stage that was skipped and why

Deliberately absent: retries inside a stage (a stale vendor wastes the caller's time),
merging results across vendors (a chain is not a fan-out), and any network call from
`is_available()`.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import config, errors, ledger as ledger_mod, types
from .stages import Stage
from .types import Outcome

logger = logging.getLogger(__name__)

PROBE_TTL_SECONDS = 60.0     # don't ask Firecrawl for the balance on every single search
KEYLESS_NAME = "keyless"


@dataclass
class RouteAttempt:
    """One stage's contribution to a call — the audit trail behind `routed_via`."""

    stage: str
    ok: bool
    reason: str = ""
    spent: float = 0.0
    remaining: Optional[float] = None

    def as_line(self) -> str:
        if self.ok:
            return f"{self.stage}=ok"
        return f"{self.stage}: {self.reason}"


@dataclass
class RouteResult:
    payload: Any = None
    stage: str = ""
    attempts: list[RouteAttempt] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.payload is not None

    def summary(self) -> str:
        return "; ".join(a.as_line() for a in self.attempts)

    def chain_text(self) -> str:
        """`exa=ok` or `exa: quota (402) -> firecrawl=ok` — one line for logs and the CLI."""
        return " -> ".join(a.as_line() for a in self.attempts) or "nothing tried"

    def skipped(self) -> list[TriggerReason]:
        return []


@dataclass(frozen=True)
class TriggerReason:
    stage: str
    reason: str


class Router:
    """Stateless per call; all memory lives in the ledger."""

    def __init__(self, stages: dict[str, Stage] | None = None, ledger: Optional[ledger_mod.Ledger] = None,
                 settings: Optional[config.Settings] = None,
                 clock: Callable[[], float] = time.time):
        self.settings = settings or config.settings()
        self.stages = stages if stages is not None else _build_default_stages()
        self.ledger = ledger or ledger_mod.Ledger(self.settings.ledger_path)
        self.clock = clock
        self._last_probe: dict[str, float] = {}

    def now(self) -> float:
        """The router's clock — tests inject one, the CLI prints dates against it."""
        return self.clock()

    # --- quota sync -------------------------------------------------------------------
    def _sync_quota(self, stage: Stage, force: bool = False) -> None:
        """Refresh the vendor's own numbers, at most once per PROBE_TTL."""
        now = self.clock()
        last = self._last_probe.get(stage.name, 0.0)
        if not force and (now - last) < PROBE_TTL_SECONDS:
            return
        self._last_probe[stage.name] = now
        try:
            stage.remaining_override = None  # type: ignore[attr-defined]
            stage.period_override = None  # type: ignore[attr-defined]
            stage.blocked_reason = ""  # type: ignore[attr-defined]
            probed = stage.probe_quota()
        except Exception as exc:  # a probe must never break a search
            logger.debug("quotarouter: quota probe for %s failed: %s", stage.name, exc)
            return
        if probed is None:
            self._ensure_default_limit(stage)
            return
        limit, reset_at, source = probed
        period = getattr(stage, "period_override", None)
        state = self.ledger.set_limit(stage.name, limit, source=source, reset_at=reset_at, period=period)
        remaining = getattr(stage, "remaining_override", None)
        if remaining is not None:
            state.used = max(0.0, float(limit) - float(remaining))
            self.ledger.charge(stage.name, 0.0)  # persists the corrected `used`
        blocked = getattr(stage, "blocked_reason", "")
        if blocked:
            self.ledger.disable(stage.name, blocked)

    def _ensure_default_limit(self, stage: Stage) -> None:
        allowance = stage.monthly_allowance
        if allowance is None:
            return
        state = self.ledger.get(stage.name)
        current_period = ledger_mod.month_key(self.clock())
        if state.period != current_period or state.limit <= 0 or state.limit != float(allowance):
            self.ledger.set_limit(
                stage.name, float(allowance), source="local",
                reset_at=ledger_mod.next_month_epoch(self.clock()), period=current_period,
            )

    def probe_all(self, force: bool = True) -> dict[str, dict[str, Any]]:
        """Ask every stage with a credential what it has left, and store the answer.

        This is what `router probe` (and a cron job) calls: the point is to know the remaining
        free allowance BEFORE a search, so the first call of the day does not discover an empty
        pool from a 402.
        """
        report: dict[str, dict[str, Any]] = {}
        for name, stage in self.stages.items():
            if name == KEYLESS_NAME:
                continue
            if not stage.has_credential():
                report[name] = {"remaining": None, "reason": f"no {stage.key_env or 'credential'} set"}
                continue
            try:
                self._sync_quota(stage, force=force)
            except Exception as exc:
                report[name] = {"remaining": None, "error": f"probe raised: {exc}"}
                continue
            state = self.ledger.get(name)
            report[name] = {
                "remaining": None if state.limit <= 0 else state.remaining(),
                "limit": state.limit,
                "unit": stage.unit,
                "source": state.source,
                "reset_at": ledger_mod.iso(state.reset_at),
                "paused": _pause_text(state, self.clock()) if state.is_paused(self.clock()) else None,
            }
        return report

    def skips(self, capability: str) -> list[tuple[str, str]]:
        """Vendors that cannot serve this call right now, with the reason (for `router route`)."""
        return [(s.stage, s.reason) for s in self.candidates(capability)[1]]

    # --- candidate selection -----------------------------------------------------------
    def candidates(self, capability: str) -> tuple[list[Stage], list[TriggerReason]]:
        """Stages that may serve this call, in order, plus the reason each skipped one is out."""
        order = self.settings.search_order if capability == "search" else self.settings.extract_order
        usable: list[Stage] = []
        skipped: list[TriggerReason] = []
        now = self.clock()

        for name in order:
            stage = self.stages.get(name)
            if stage is None:
                skipped.append(TriggerReason(name, "unknown stage (not in the registry)"))
                continue
            supports = stage.supports_search() if capability == "search" else stage.supports_extract()
            if not supports:
                skipped.append(TriggerReason(name, f"does not support {capability}"))
                continue
            if not stage.has_credential():
                skipped.append(TriggerReason(name, stage.not_ready_reason() or "no credential"))
                continue
            self._sync_quota(stage)
            state = self.ledger.get(name)
            if state.is_paused(now):
                if state.pause_kind == "quota" and state.reset_at and now >= state.reset_at:
                    self.ledger.enable(name)  # the window rolled: try again
                else:
                    skipped.append(TriggerReason(name, _pause_text(state, now)))
                    continue
            cost = self._unit_cost(stage, capability)
            if state.limit > 0 and not state.has_room(cost):
                skipped.append(TriggerReason(
                    name, f"quota spent ({state.used:g}/{state.limit:g} {stage.unit})"))
                continue
            usable.append(stage)
        return usable, skipped

    @staticmethod
    def _unit_cost(stage: Stage, capability: str) -> float:
        return stage.capability.search_units if capability == "search" else stage.capability.extract_units

    # --- the walk ----------------------------------------------------------------------
    def run_search(self, query: str, limit: int) -> RouteResult:
        return self._walk("search", lambda stage: stage.search(query, limit))

    def run_extract(self, urls: list[str]) -> RouteResult:
        return self._walk("extract", lambda stage: stage.extract(urls), urls=urls)

    def _walk(self, capability: str, call: Callable[[Stage], Any], urls: Optional[list[str]] = None) -> RouteResult:
        usable, skipped = self.candidates(capability)
        result = RouteResult()
        result.attempts.extend(RouteAttempt(s.stage, False, f"skipped: {s.reason}") for s in skipped)

        for stage in usable:
            outcome = self._attempt(stage, capability, call, result)
            if outcome is not None:
                result.payload = outcome
                result.stage = stage.name
                return result

        # Nothing configured could serve: the anonymous tier, if the operator allows it.
        last_resort = self.stages.get(KEYLESS_NAME)
        if (self.settings.keyless_last_resort and last_resort is not None
                and KEYLESS_NAME not in [s.stage for s in result.attempts]):
            served = self._attempt(last_resort, capability, call, result)
            if served is not None:
                result.payload = served
                result.stage = KEYLESS_NAME
                logger.warning("quotarouter: every configured stage is out; %s served by the keyless tier", capability)
                return result
        return result

    def _attempt(self, stage: Stage, capability: str, call: Callable[[Stage], Any],
                 result: RouteResult) -> Any:
        """Run one stage. Returns its payload on success, None on any kind of failure.

        Every failure path records why in `result.attempts` and parks the stage — the caller
        never has to inspect the reason, only whether it got a payload.
        """
        try:
            outcome = call(stage)
        except errors.StageError as exc:
            self._pause(stage, exc.kind, str(exc), retry_after=exc.retry_after)
            result.attempts.append(RouteAttempt(stage.name, False, f"{exc.kind}: {exc}"))
            return None
        except Exception as exc:  # an adapter bug must not kill the whole call
            self._pause(stage, types.TRANSIENT, f"stage crashed: {exc}")
            result.attempts.append(RouteAttempt(stage.name, False, f"crashed: {exc}"))
            logger.warning("quotarouter: stage %s crashed", stage.name, exc_info=True)
            return None

        # An adapter may return a bare payload (extract list) or an Outcome (search envelope).
        if not isinstance(outcome, Outcome):
            outcome = Outcome(payload=outcome)

        if not outcome.ok:
            failure = outcome.failure or types.Failure(types.UNKNOWN, "no outcome")
            self._pause(stage, failure.kind, failure.message, retry_after=failure.retry_after)
            result.attempts.append(RouteAttempt(stage.name, False, f"{failure.kind}: {failure.message}"))
            return None

        if outcome.payload in (None, [], {}):
            self._pause(stage, types.EMPTY, "stage returned nothing usable")
            result.attempts.append(RouteAttempt(stage.name, False, f"{types.EMPTY}: no content"))
            return None

        # Extract returns ONE ENTRY PER URL, so a per-URL error is not a stage failure — but a
        # batch where every entry errored is (the vendor is blocked or down, not this page) —
        # UNLESS the error is the origin's own answer (403/404/410 from the site): that is a
        # real per-page result the caller must see, not a reason to spend another vendor's credits.
        payload = outcome.payload
        if isinstance(payload, list) and capability == "extract":
            successes = [e for e in payload if isinstance(e, dict) and not e.get("error") and (e.get("content") or "").strip()]
            if not successes and not all(_is_origin_answer(e) for e in payload if isinstance(e, dict)):
                detail = "; ".join(str(e.get("error")) for e in payload[:3] if isinstance(e, dict)) or "no content"
                self._pause(stage, types.BAD_PAGE, detail)
                result.attempts.append(RouteAttempt(stage.name, False, f"{types.BAD_PAGE}: {detail}"))
                return None

        spent = float(outcome.spent or self._unit_cost(stage, capability))
        self.ledger.charge(stage.name, spent, cost_usd=outcome.cost_usd)
        state = self.ledger.note_success(stage.name)
        result.attempts.append(RouteAttempt(stage.name, True, spent=spent, remaining=state.remaining()))
        return outcome.payload

    def _pause(self, stage: Stage, kind: str, message: str, retry_after: Optional[float] = None) -> None:
        state = self.ledger.get(stage.name)
        if kind == types.AUTH:
            until = self.clock() + 24 * 3600
        elif kind == types.QUOTA:
            # Park until the window resets when we know it; otherwise 24h and re-probe then.
            until = state.reset_at if state.reset_at and state.reset_at > self.clock() else self.clock() + 24 * 3600
        else:
            attempt = state.consecutive_failures + 1
            until = self.clock() + ledger_mod.backoff_seconds(kind, attempt, retry_after)
        self.ledger.pause(stage.name, until, kind, message)
        logger.info("quotarouter: %s paused (%s) until %s — %s", stage.name, kind,
                    ledger_mod.iso(until), message[:200])

    # --- reporting ---------------------------------------------------------------------
    def log_route(self, capability: str, result: RouteResult, extra: dict[str, Any] | None = None) -> None:
        """One JSON line per call in `state/web-router/routes.jsonl` — the audit trail."""
        record = {
            "ts": ledger_mod.iso(self.clock()),
            "capability": capability,
            "served_by": result.stage or None,
            "ok": result.ok,
            "chain": [a.as_line() for a in result.attempts],
            **(extra or {}),
        }
        try:
            path = self.settings.log_path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug("quotarouter: could not write route log: %s", exc)


_ORIGIN_ANSWER = (
    "from origin",
    "HTTP 403 from origin",
    "HTTP 404 from origin",
    "HTTP 410 from origin",
)


def _is_origin_answer(entry: dict[str, Any]) -> bool:
    """True when a per-URL error is the SITE's own answer, not the vendor's failure."""
    text = str(entry.get("error") or "")
    return any(marker in text for marker in _ORIGIN_ANSWER)


def _pause_text(state: "ledger_mod.VendorState", now: float) -> str:
    if state.pause_kind == "disabled":
        return f"disabled by operator ({state.pause_reason})"
    left = max(0.0, (state.paused_until or now) - now)
    if left >= 3600:
        return f"paused {left / 3600:.1f}h ({state.pause_kind}: {state.pause_reason})"
    return f"paused {int(left)}s ({state.pause_kind}: {state.pause_reason})"


def _build_default_stages() -> dict[str, Stage]:
    from .stages import build_stages

    return build_stages()


def format_failure(result: RouteResult, capability: str) -> str:
    """The single error the model sees when nothing served."""
    parts = [a.as_line() for a in result.attempts]
    body = "; ".join(parts) if parts else "no stage configured"
    return f"quotarouter: no {capability} provider served this call ({body})"
