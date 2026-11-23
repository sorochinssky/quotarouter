"""Keep the audit trail from growing forever.

`routes.jsonl` gets one line per call and nothing truncates it: on a busy agent that is tens
of thousands of lines a year, and `grep`-ing an incident becomes guesswork. Two mechanics, one
knob each:

* **rotate** — when the file passes `QUOTAROUTER_LOG_MAX_BYTES` (8 MB by default) it is renamed
  to `routes.jsonl.1`, replacing the previous generation. One generation is enough: the point is
  to bound the file, not to archive history (a real archive belongs in a log shipper).
  Rotation runs inside the logging path, so nobody has to remember a cron job for it.
* **prune** — drop lines older than `QUOTAROUTER_LOG_KEEP_DAYS` (30 by default). This one is a
  cron/`quotarouter prune` job, because it rewrites the file and should not happen mid-call.

Both are safe under concurrency: rotation is an atomic rename (a writer keeps appending to the
same inode, which is now `routes.jsonl.1`), and prune writes a new file and replaces it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

try:  # POSIX advisory locks; Windows keeps appends atomic-by-size only
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_KEEP_DAYS = 30


_held = threading.local()


@contextmanager
def locked(path: Path, *, exclusive: bool = True) -> Iterator[None]:
    """Hold the journal's lock file.

    Two writers already collided here without anyone noticing: `log_route` appends a line while
    `prune` reads the whole file and replaces it, so every line appended during a prune vanished
    — quietly, in the one file an incident investigation reads. Appends and pruning now share
    this lock; appends are microseconds, so serialising them costs nothing.
    """
    held: dict[str, int] = getattr(_held, "paths", None) or {}
    _held.paths = held
    key = str(path)
    if held.get(key):  # the same thread already holds it: a nested call must not deadlock
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    if fcntl is None:  # pragma: no cover - Windows
        held[key] = 1
        try:
            yield
        finally:
            held[key] = 0
        return
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+")  # noqa: SIM115 - held for the duration of the block
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        held[key] = 1
        try:
            yield
        finally:
            held[key] = 0
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def append(path: Path, line: str, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
    """Append one audit line and rotate if the file grew past its bound."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        maybe_rotate(path, max_bytes)


def maybe_rotate(path: Path, max_bytes: int = DEFAULT_MAX_BYTES) -> bool:
    """Rename the log to `<name>.1` once it exceeds `max_bytes`. Cheap: one stat per call."""
    if max_bytes <= 0:
        return False
    try:
        size = path.stat().st_size
    except OSError:
        return False
    if size < max_bytes:
        return False
    rotated = path.with_name(path.name + ".1")
    try:
        os.replace(path, rotated)
    except OSError as exc:
        logger.debug("quotarouter: could not rotate %s: %s", path, exc)
        return False
    logger.info("quotarouter: rotated %s (%d bytes) to %s", path.name, size, rotated.name)
    return True


def _timestamp(line: str) -> Optional[float]:
    """The `ts` field of one audit line, or None when the line does not carry one."""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    raw = record.get("ts")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def prune(path: Path, days: int = DEFAULT_KEEP_DAYS, *, now: Optional[float] = None) -> tuple[int, int]:
    """Drop audit lines older than `days`. Returns `(kept, dropped)`.

    A line without a parsable timestamp is kept: losing evidence is worse than keeping a line.
    """
    if days <= 0:
        return (0, 0)
    with locked(path):
        return _prune_locked(path, days, now)


def _prune_locked(path: Path, days: int, now: Optional[float]) -> tuple[int, int]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return (0, 0)
    cutoff = (time.time() if now is None else now) - days * 86400
    kept: list[str] = []
    dropped = 0
    for line in lines:
        if not line.strip():
            continue
        stamp = _timestamp(line)
        if stamp is not None and stamp < cutoff:
            dropped += 1
            continue
        kept.append(line)
    if dropped:
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("quotarouter: prune failed for %s: %s", path, exc)
            return (len(lines), 0)
    return (len(kept), dropped)


def prune_directory(state_dir: Path, days: int = DEFAULT_KEEP_DAYS, *,
                    now: Optional[float] = None) -> dict[str, tuple[int, int]]:
    """Prune every audit generation in the state directory (`routes.jsonl`, `routes.jsonl.1`)."""
    report: dict[str, tuple[int, int]] = {}
    for path in sorted(state_dir.glob("routes.jsonl*")):
        if path.is_file():
            report[path.name] = prune(path, days, now=now)
    return report
