"""Stage contract.

A stage is one vendor adapter. It must be:
  * dumb — no routing, no ledger, no retries; one attempt in, one `Outcome` out;
  * honest — an empty answer is `EMPTY`, a blocked page is `BAD_PAGE`, an exhausted
    allowance raises `QuotaError`; never a silent `success` with nothing in it;
  * cheap to probe — `has_credential()` reads the environment only, never the network.

Adding a vendor = one subclass + one line in `stages/__init__.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .. import env_source, errors, types
from ..types import Outcome


@dataclass(frozen=True)
class Capability:
    """Static description of what a stage can do and what it costs."""

    search: bool = True
    extract: bool = False
    # Units charged to the ledger per successful call. Money-cost vendors set `usd_per_call`
    # instead and report the real cost in the response.
    search_units: float = 1.0
    extract_units: float = 1.0


class Stage:
    """Base class for vendor adapters."""

    name: str = ""
    display_name: str = ""
    # Env variable holding the credential; empty means the vendor needs none (keyless tier).
    key_env: str = ""
    unit: str = "requests"
    monthly_allowance: Optional[float] = None  # units per window; None = not a metered quota
    capability = Capability()

    # --- identity ----------------------------------------------------------------------
    def not_ready_reason(self) -> str:
        """Why this stage cannot be dialed, or "" when it can. Shown by `router route`."""
        if self.key_env and not self._env(self.key_env):
            return f"no {self.key_env} set"
        return ""

    def has_credential(self) -> bool:
        """Cheap, offline check that this stage can be dialed at all: is its key present?

        No network, ever — Hermes calls this on every `hermes tools` paint and at tool
        registration. Vendors with no key env (the anonymous tier, Jina's keyless Reader)
        override this.
        """
        if not self.key_env:
            return True
        return bool(self._env(self.key_env))

    @staticmethod
    def _env(name: str) -> str:
        """Key lookup through `env_source`: os.environ, an optional dotenv, host resolvers.

        The core never imports a host: an integration registers its own resolver instead.
        """
        return env_source.resolve(name)

    def describe(self) -> str:
        return f"{self.display_name or self.name} ({self.name})"

    # --- capability --------------------------------------------------------------------
    def supports_search(self) -> bool:
        return self.capability.search

    def supports_extract(self) -> bool:
        return self.capability.extract

    # --- quota -------------------------------------------------------------------------
    def probe_quota(self) -> Optional[tuple[float, Optional[float], str]]:
        """Ask the vendor for the real allowance: `(limit, reset_epoch, source)`.

        Return None when the router must keep its local estimate. Must not raise:
        a probe failure means "keep the estimate", never "fail the search".
        """
        return None

    # --- work --------------------------------------------------------------------------
    def search(self, query: str, limit: int) -> Outcome:
        raise NotImplementedError

    def extract(self, urls: list[str]) -> list[dict[str, Any]]:
        raise NotImplementedError

    # --- helpers for subclasses --------------------------------------------------------
    @staticmethod
    def _raise_for_status(status: int, body: str, headers: dict[str, str] | None = None) -> None:
        if status >= 400:
            raise errors.classify(status, body, headers)

    @staticmethod
    def _outcome(payload: Any, *, spent: float = 0.0, cost_usd: float = 0.0,
                 status: Optional[int] = None) -> Outcome:
        return Outcome(payload=payload, spent=spent, cost_usd=cost_usd, http_status=status)

    @staticmethod
    def _fail(kind: str, message: str, *, status: Optional[int] = None,
              retry_after: Optional[float] = None) -> Outcome:
        return Outcome(failure=types.Failure(kind=kind, message=message, retry_after=retry_after), http_status=status)
