"""Edge cases that decide whether the files on disk can be trusted.

Every one of these is a "the process was killed" or "someone edited the file" scenario: the
router keeps its whole memory in JSON, so a file it cannot read is a router that silently
believes every vendor is untouched.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from quotarouter import ledger as ledger_mod, retention


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ts))


# --- ledger -------------------------------------------------------------------------------
def test_a_corrupt_ledger_file_does_not_wipe_accounting_silently(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{not json at all", encoding="utf-8")
    ledger = ledger_mod.Ledger(path)
    state = ledger.get("firecrawl")
    assert state.vendor == "firecrawl"
    assert state.used == 0.0  # a fresh state, not a crash on import


def test_a_half_written_ledger_keeps_the_vendors_it_can_read(tmp_path):
    """A killed process can leave one vendor entry valid and the rest broken."""
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"vendors": {
        "firecrawl": {"limit": 1000.0, "used": 45.0, "source": "api"},
        "exa": "this should be a dict",
    }}), encoding="utf-8")
    ledger = ledger_mod.Ledger(path)
    assert ledger.get("firecrawl").remaining() == 955.0
    assert ledger.get("exa").used == 0.0


def test_charging_past_the_limit_does_not_produce_negative_remaining(tmp_path):
    ledger = ledger_mod.Ledger(tmp_path / "ledger.json")
    ledger.set_limit("firecrawl", 10.0)
    ledger.charge("firecrawl", 25.0)
    assert ledger.get("firecrawl").remaining() == 0.0


def test_a_pause_expires_on_its_own_moment(tmp_path):
    ledger = ledger_mod.Ledger(tmp_path / "ledger.json")
    ledger.pause("exa", until=1_000.5, kind="quota", reason="out")
    assert ledger.get("exa").is_paused(1_000.0)
    assert not ledger.get("exa").is_paused(1_001.0)


def test_two_ledger_objects_keep_each_others_writes(tmp_path):
    """The CLI and the gateway are different processes sharing one file."""
    path = tmp_path / "ledger.json"
    first, second = ledger_mod.Ledger(path), ledger_mod.Ledger(path)
    first.charge("firecrawl", 3.0)
    second.charge("firecrawl", 2.0)
    assert ledger_mod.Ledger(path).get("firecrawl").used == 5.0


# --- retention ----------------------------------------------------------------------------
def _line(ts: float) -> str:
    return json.dumps({"ts": iso(ts), "capability": "search", "ok": True}) + "\n"


def test_rotation_keeps_exactly_one_generation(tmp_path):
    path = tmp_path / "routes.jsonl"
    path.write_text(_line(time.time()), encoding="utf-8")
    retention.append(path, _line(time.time()), max_bytes=10)
    assert (tmp_path / "routes.jsonl.1").exists()
    retention.append(path, _line(time.time()), max_bytes=10)
    assert (tmp_path / "routes.jsonl.1").exists()
    generations = [p for p in tmp_path.glob("routes.jsonl.*") if not p.name.endswith(".lock")]
    assert len(generations) == 1, f"only one previous generation, got {generations}"


def test_rotation_is_off_when_the_bound_is_zero(tmp_path):
    path = tmp_path / "routes.jsonl"
    path.write_text(_line(time.time()), encoding="utf-8")
    assert retention.maybe_rotate(path, max_bytes=0) is False
    assert retention.maybe_rotate(tmp_path / "absent.jsonl", max_bytes=10) is False


def test_prune_keeps_a_line_it_cannot_date(tmp_path):
    """A line without a timestamp is evidence of something broken — not garbage to delete."""
    path = tmp_path / "routes.jsonl"
    old = time.time() - 90 * 86400
    path.write_text(_line(old) + "not json\n" + json.dumps({"capability": "search"}) + "\n",
                    encoding="utf-8")
    kept, dropped = retention.prune(path, days=30)
    body = path.read_text()
    assert dropped == 1
    assert "not json" in body and '"capability": "search"' in body
    assert kept == 2


def test_prune_of_missing_file_is_not_an_error(tmp_path):
    assert retention.prune(tmp_path / "nope.jsonl", days=30) == (0, 0)
    assert retention.prune(tmp_path / "nope.jsonl", days=0) == (0, 0)


def test_prune_directory_covers_the_rotated_generation(tmp_path):
    old = time.time() - 90 * 86400
    for name in ("routes.jsonl", "routes.jsonl.1"):
        (tmp_path / name).write_text(_line(old) + _line(time.time()), encoding="utf-8")
    report = retention.prune_directory(tmp_path, days=30)
    assert set(report) == {"routes.jsonl", "routes.jsonl.1"}
    assert all(dropped == 1 for _, dropped in report.values())


def test_a_bad_timestamp_is_not_treated_as_fresh(tmp_path):
    path = tmp_path / "routes.jsonl"
    path.write_text(json.dumps({"ts": "not-a-date"}) + "\n", encoding="utf-8")
    kept, dropped = retention.prune(path, days=30)
    assert kept == 1 and dropped == 0, "an unreadable date keeps the line"


# --- the entry point ----------------------------------------------------------------------
def test_python_dash_m_quotarouter_runs_the_cli(tmp_path):
    """README tells every MCP client to start the server this way, so it must exist."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, "-m", "quotarouter", "route"], cwd=root,
                          capture_output=True, text=True, timeout=90,
                          env={"PATH": "/usr/bin:/bin", "QUOTAROUTER_STATE_DIR": str(tmp_path),
                               "QUOTAROUTER_ENV_FILE": str(tmp_path / "absent.env")})
    assert done.returncode == 0, done.stderr
    assert "search:" in done.stdout
