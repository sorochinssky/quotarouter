"""Self-healing pauses: the machine's "no money" is not the operator's "stop".

The router parks a vendor for two very different reasons, and conflating them cost a manual
`quotarouter enable deepseek` after every top-up. These tests pin the distinction and the path
back: a paused stage is still probed (balances are free), and a vendor that answers "I can pay"
returns to the chain on its own.
"""

from __future__ import annotations

import pytest

from quotarouter import cli, config, ledger as ledger_mod, routing, types
from quotarouter.stages.base import Capability, Stage


class Probing(Stage):
    """A stage whose balance the test controls, like deepseek's /user/balance."""

    name = "probe"
    key_env = ""
    unit = "usd"
    capability = Capability(search=True, extract=False, search_units=1.0)

    def __init__(self, balance: float = 0.0) -> None:
        self.balance = balance
        self.search_calls = 0

    def search(self, query, limit):
        self.search_calls += 1
        return types.search_ok([{"title": "t", "url": "https://x/", "content": "c"}])

    def probe_quota(self):
        self.remaining_override = max(0.0, self.balance)
        if self.balance <= 0:
            self.blocked_reason = "probe: balance empty"
            return 0.0, None, "api"
        return self.balance, None, "api"


@pytest.fixture
def clock():
    now = [1_800_000_000.0]
    return now


def make_router(tmp_path, monkeypatch, stage, clock):
    monkeypatch.setenv("QUOTAROUTER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("QUOTAROUTER_ALLOW_KEYLESS", "0")
    monkeypatch.setenv("QUOTAROUTER_SEARCH_ORDER", stage.name)
    config.reset_settings_for_tests()
    return routing.Router(clock=lambda: clock[0], stages={stage.name: stage})


# --- the machine parks, the machine unparks ----------------------------------------------
def test_an_empty_balance_is_a_pause_with_a_deadline_not_a_stop(tmp_path, monkeypatch, clock):
    stage = Probing(balance=0)
    router = make_router(tmp_path, monkeypatch, stage, clock)

    assert not router.run_search("q", 3).ok
    state = router.ledger.get("probe")
    assert state.pause_kind == types.QUOTA
    assert ledger_mod.pause_origin_of(state) == ledger_mod.ORIGIN_AUTO
    assert state.paused_until is not None and state.paused_until != float("inf")
    assert stage.search_calls == 0, "an unfunded vendor must not be dialed"


def test_a_top_up_returns_the_stage_without_anyone_typing_enable(tmp_path, monkeypatch, clock):
    stage = Probing(balance=0)
    router = make_router(tmp_path, monkeypatch, stage, clock)
    assert not router.run_search("q", 3).ok

    stage.balance = 10.0
    clock[0] += 120  # past the probe TTL, well before any retry deadline

    result = router.run_search("q", 3)
    assert result.ok and result.stage == "probe"
    state = router.ledger.get("probe")
    assert not state.is_paused(clock[0])
    assert state.pause_origin == ""


def test_a_second_empty_answer_parks_it_again(tmp_path, monkeypatch, clock):
    """The account going dry twice is two separate events, not one long stop."""
    stage = Probing(balance=0)
    router = make_router(tmp_path, monkeypatch, stage, clock)
    assert not router.run_search("q", 3).ok
    first = router.ledger.get("probe").paused_until

    clock[0] += 3600
    stage.balance = 0.0
    assert not router.run_search("q", 3).ok
    second = router.ledger.get("probe").paused_until
    assert second is not None and first is not None
    assert router.ledger.get("probe").is_paused(clock[0])


# --- the operator stops, and nothing else lifts it ---------------------------------------
def test_an_operator_stop_survives_a_vendor_that_says_it_can_pay(tmp_path, monkeypatch, clock):
    stage = Probing(balance=100.0)
    router = make_router(tmp_path, monkeypatch, stage, clock)
    router.ledger.disable("probe", "stopped while chasing a bug")

    result = router.run_search("q", 3)
    assert not result.ok
    assert stage.search_calls == 0
    state = router.ledger.get("probe")
    assert state.is_paused(clock[0])
    assert ledger_mod.pause_origin_of(state) == ledger_mod.ORIGIN_OPERATOR
    assert state.paused_until == float("inf")


def test_recheck_releases_machine_pauses_and_leaves_operator_stops(tmp_path, monkeypatch, clock):
    machine = Probing(balance=0)
    operator = Probing(balance=50.0)
    operator.name = "stopped"
    monkeypatch.setenv("QUOTAROUTER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("QUOTAROUTER_ALLOW_KEYLESS", "0")
    monkeypatch.setenv("QUOTAROUTER_SEARCH_ORDER", "probe,stopped")
    config.reset_settings_for_tests()
    router = routing.Router(clock=lambda: clock[0],
                            stages={"probe": machine, "stopped": operator})

    assert router.run_search("q", 3).stage == "stopped"
    router.ledger.disable("stopped", "not this one")

    machine.balance = 7.0
    report = router.recheck()

    assert report["returned"] == ["probe"]
    assert not router.ledger.get("probe").is_paused(clock[0])
    assert router.ledger.get("stopped").is_paused(clock[0])
    assert router.ledger.get("stopped").pause_origin == "operator"


# --- the retry grows, and stays bounded ---------------------------------------------------
def test_the_quota_retry_starts_short_and_doubles_up_to_six_hours():
    assert ledger_mod.quota_retry_seconds(1) == 900
    assert ledger_mod.quota_retry_seconds(2) == 1800
    assert ledger_mod.quota_retry_seconds(3) == 3600
    assert ledger_mod.quota_retry_seconds(4) == 7200
    assert ledger_mod.quota_retry_seconds(20) == 21600


def test_a_known_window_end_is_preferred_over_the_retry(tmp_path, monkeypatch, clock):
    stage = Probing(balance=0)
    router = make_router(tmp_path, monkeypatch, stage, clock)
    router.ledger.set_limit("probe", 10.0, source="api", reset_at=clock[0] + 90)

    router._pause(stage, types.QUOTA, "window spent")
    left = router.ledger.get("probe").pause_left(clock[0])
    assert 60 <= left <= 91, left


def test_an_operator_can_turn_the_retry_off(tmp_path, monkeypatch, clock):
    stage = Probing(balance=0)
    router = make_router(tmp_path, monkeypatch, stage, clock)
    monkeypatch.setenv("QUOTAROUTER_QUOTA_RETRY", "0")
    config.reset_settings_for_tests()
    router = make_router(tmp_path, monkeypatch, stage, clock)

    router._pause(stage, types.QUOTA, "balance empty")
    left = router.ledger.get("probe").pause_left(clock[0])
    assert left == pytest.approx(24 * 3600, rel=0.01)


# --- records written before the distinction existed --------------------------------------
def test_a_legacy_machine_pause_is_read_as_a_machine_pause():
    state = ledger_mod._state_from_dict("deepseek", {
        "pause_kind": "disabled",
        "pause_reason": "deepseek: balance empty (is_available=false)",
        "paused_until": float("inf"),
    })
    assert ledger_mod.pause_origin_of(state) == ledger_mod.ORIGIN_AUTO


def test_a_legacy_operator_stop_stays_an_operator_stop():
    state = ledger_mod._state_from_dict("deepseek", {
        "pause_kind": "disabled",
        "pause_reason": "disabled by operator",
        "paused_until": float("inf"),
    })
    assert ledger_mod.pause_origin_of(state) == ledger_mod.ORIGIN_OPERATOR


# --- what the operator sees ---------------------------------------------------------------
def test_status_separates_the_two_kinds_of_pause(tmp_path, monkeypatch, clock):
    auto = ledger_mod.VendorState(
        vendor="a", limit=10.0, paused_until=clock[0] + 300, pause_kind=types.QUOTA,
        pause_reason="balance empty", pause_origin="auto")
    manual = ledger_mod.VendorState(
        vendor="b", paused_until=float("inf"), pause_kind="disabled",
        pause_reason="stopped while chasing a bug", pause_origin="operator")

    assert "auto" in cli._pause(auto, clock[0])
    assert cli._pause(manual, clock[0]).startswith("stopped:")
    assert "auto" not in cli._pause(manual, clock[0])
