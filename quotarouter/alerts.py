"""Tell a human when the chain degrades — with no monitoring stack and no dependencies.

A router that fails silently is worse than one that fails loudly: the agent answers from a
keyless tier for a week and nobody notices the paid key died. This module is the smallest
honest answer: three transports and a cooldown.

Enabled by configuring any ONE of them (all read through `QUOTAROUTER_*`):

    QUOTAROUTER_ALERT_TELEGRAM=<bot token>:<chat id>     # POST to the Bot API
    QUOTAROUTER_ALERT_WEBHOOK=https://example/hook       # POST {"text": ...}
    QUOTAROUTER_ALERT_COMMAND="notify-send quotarouter {kind}"

Nothing configured means nothing sent — the default deployment stays quiet.

Two rules keep it useful instead of noisy:

* **A cooldown per kind** (default 900 s): a vendor that is down must not send one message per
  call. `QUOTAROUTER_ALERT_COOLDOWN` changes it.
* **Delivery never breaks a call.** Every transport failure is logged and swallowed; a search
  must not fail because a webhook is unreachable.

State (`<state>/alerts.json`, `{kind: last_sent_epoch}`) is on disk, so a restart does not
re-open the floodgates.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import config, http_client

logger = logging.getLogger(__name__)

# Alert kinds. `no-stage` is the worst: nothing could serve. `keyless` means the paid chain is
# out and the answer came from an anonymous tier. `quota` means a vendor parked itself.
NO_STAGE = "no-stage"
KEYLESS = "keyless"
QUOTA = "quota"


@dataclass(frozen=True)
class AlertSettings:
    telegram: str = ""
    webhook: str = ""
    command: str = ""
    cooldown: float = 900.0

    @property
    def enabled(self) -> bool:
        return bool(self.telegram or self.webhook or self.command)


def settings() -> AlertSettings:
    """Read alert settings through the same env layer as everything else."""
    cooldown_raw = config.env("ALERT_COOLDOWN") or "900"
    try:
        cooldown = float(cooldown_raw)
    except ValueError:
        cooldown = 900.0
    return AlertSettings(
        telegram=config.env("ALERT_TELEGRAM"),
        webhook=config.env("ALERT_WEBHOOK"),
        command=config.env("ALERT_COMMAND"),
        cooldown=max(0.0, cooldown),
    )


def _state_path() -> Path:
    return config.settings().state_dir / "alerts.json"


def _read_state() -> dict[str, float]:
    path = _state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))}


def _write_state(state: dict[str, float]) -> None:
    path = _state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug("quotarouter: could not store alert state: %s", exc)


def notify(kind: str, text: str, *, now: Optional[float] = None, force: bool = False,
           extra: Optional[dict[str, Any]] = None) -> bool:
    """Send an alert unless the same kind is cooling down. Returns True when it was sent."""
    settings_ = settings()
    if not settings_.enabled:
        return False
    moment = time.time() if now is None else now
    state = _read_state()
    last = state.get(kind)
    if not force and last is not None and moment - last < settings_.cooldown:
        logger.debug("quotarouter: alert %s is cooling down (%.0fs of %.0fs)",
                     kind, moment - last, settings_.cooldown)
        return False

    body = text if not extra else f"{text}\n{json.dumps(extra, ensure_ascii=False, sort_keys=True)}"
    delivered = False
    if settings_.telegram:
        delivered |= _send_telegram(settings_.telegram, body)
    if settings_.webhook:
        delivered |= _send_webhook(settings_.webhook, body)
    if settings_.command:
        delivered |= _send_command(settings_.command, kind, body)
    if delivered:
        state[kind] = moment
        _write_state(state)
    return delivered


# --- transports -------------------------------------------------------------------------
def _send_telegram(target: str, text: str) -> bool:
    """`<token>:<chat id>` — the Bot API expects the token in the path, so keep it out of logs."""
    token, _, chat_id = target.partition(":")
    if not token or not chat_id:
        logger.warning("quotarouter: QUOTAROUTER_ALERT_TELEGRAM must be '<token>:<chat id>'")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    return _post(url, {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True})


def _send_webhook(url: str, text: str) -> bool:
    return _post(url, {"text": text})


def _post(url: str, payload: dict[str, Any]) -> bool:
    try:
        response = http_client.post(url, json=payload, timeout=10.0)
    except Exception as exc:  # a broken transport must never break the caller
        logger.warning("quotarouter: alert transport failed: %s", exc)
        return False
    status = getattr(response, "status_code", 0)
    if status >= 300:
        logger.warning("quotarouter: alert transport answered HTTP %s", status)
        return False
    return True


def _send_command(template: str, kind: str, text: str) -> bool:
    """Run a shell command with `{kind}` and `{text}` substituted.

    Substitute into the whole string before `shlex.split`, so a `{text}` containing spaces
    lands as several arguments: use `{kind}` in the command and keep the long text in the log,
    or wrap it yourself (`sh -c 'curl -d "{text}" …'`).
    """
    argv = template.replace("{kind}", kind).replace("{text}", text.replace("\n", " "))
    try:
        done = subprocess.run(shlex.split(argv), capture_output=True, text=True, timeout=10)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        logger.warning("quotarouter: alert command failed: %s", exc)
        return False
    if done.returncode != 0:
        logger.warning("quotarouter: alert command exited %s: %s", done.returncode,
                       (done.stderr or "").strip()[:200])
        return False
    return True


def reset_for_tests() -> None:
    """Forget the cooldown state (tests only)."""
    try:
        _state_path().unlink()
    except OSError:
        pass
