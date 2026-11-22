"""Ledger: crash-safe persistence, window roll, pause semantics."""

from __future__ import annotations

import json

import pytest

from quotarouter import ledger as L


def make_ledger(tmp_path):
    return L.Ledger(tmp_path / "ledger.json")


def test_charge_accumulates_and_persists(tmp_path):
    led = make_ledger(tmp_path)
    led.set_limit("firecrawl", 1000, source="api", period="2026-11")
    led.charge("firecrawl", 2)
    led.charge("firecrawl", 3)
    assert led.get("firecrawl").used == 5
    # a fresh Ledger object reads the same file — this is what survives a restart
    assert make_ledger(tmp_path).get("firecrawl").used == 5


def test_window_roll_resets_usage_and_quota_pause(tmp_path):
    led = make_ledger(tmp_path)
    led.set_limit("exa", 10, period="2026-11")
    led.charge("exa", 9.5)
    led.pause("exa", L.utcnow() + 3600, "quota", "spent")
    assert led.get("exa").is_paused()

    led.set_limit("exa", 10, period="2026-12")  # new month
    state = led.get("exa")
    assert state.used == 0.0
    assert not state.is_paused(), "a rolled window clears a quota pause"


def test_throttle_pause_clears_on_success(tmp_path):
    led = make_ledger(tmp_path)
    led.pause("tavily", L.utcnow() + 60, "throttle", "429")
    assert led.get("tavily").is_paused()
    led.note_success("tavily")
    assert not led.get("tavily").is_paused()


def test_quota_pause_survives_success(tmp_path):
    led = make_ledger(tmp_path)
    led.pause("exa", L.utcnow() + 3600, "quota", "402")
    led.note_success("exa")  # a stray success must not un-park an exhausted vendor
    assert led.get("exa").is_paused()


def test_corrupted_ledger_does_not_raise(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{ this is not json", encoding="utf-8")
    led = L.Ledger(path)
    assert led.get("exa").used == 0.0
    led.charge("exa", 1)
    assert json.loads(path.read_text())["vendors"]["exa"]["used"] == 1.0


def test_unknown_fields_from_a_newer_version_are_ignored(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"vendors": {"exa": {"used": 2, "future_field": 1}}}), encoding="utf-8")
    assert L.Ledger(path).get("exa").used == 2


def test_disable_is_forever_until_enable(tmp_path):
    led = make_ledger(tmp_path)
    led.disable("keenable", "no key")
    assert led.get("keenable").is_paused()
    led.enable("keenable")
    assert not led.get("keenable").is_paused()


def test_has_room_requires_a_limit(tmp_path):
    led = make_ledger(tmp_path)
    assert not led.get("jina").has_room(1), "unmetered stages have no limit and are not gated"
