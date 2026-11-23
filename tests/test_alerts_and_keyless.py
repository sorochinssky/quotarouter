"""Alerts and the anonymous tier: the paths that only run when something already went wrong.

An alerting layer is judged on its worst day: a dead webhook, a wrong token format, a cooldown
file someone corrupted. None of that may break the search that triggered it, and none of it may
claim to have been delivered.
"""

from __future__ import annotations

import json

import httpx
import pytest

from quotarouter import alerts, config, http_client
from quotarouter.stages import keyless


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("QUOTAROUTER_ALERT_COOLDOWN", "60")
    for name in ("QUOTAROUTER_ALERT_TELEGRAM", "QUOTAROUTER_ALERT_WEBHOOK",
                 "QUOTAROUTER_ALERT_COMMAND"):
        monkeypatch.delenv(name, raising=False)
    config.reset_settings_for_tests()
    alerts.reset_for_tests()
    yield tmp_path
    config.reset_settings_for_tests()


# --- transports ---------------------------------------------------------------------------
def test_a_dead_webhook_does_not_raise_and_is_not_recorded_as_sent(state, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("no route to host")

    http_client.set_transport(httpx.MockTransport(handler))
    monkeypatch.setenv("QUOTAROUTER_ALERT_WEBHOOK", "https://hooks.example/x")
    assert alerts.notify(alerts.NO_STAGE, "nothing served", now=100.0) is False
    assert alerts._read_state() == {}, "a failed delivery must not start a cooldown"


def test_a_webhook_that_answers_500_is_a_failure(state, monkeypatch):
    http_client.set_transport(httpx.MockTransport(lambda r: httpx.Response(500)))
    monkeypatch.setenv("QUOTAROUTER_ALERT_WEBHOOK", "https://hooks.example/x")
    assert alerts.notify(alerts.NO_STAGE, "nothing served", now=100.0) is False


def test_a_telegram_target_without_a_chat_id_is_refused(state, monkeypatch):
    sent: list = []
    http_client.set_transport(httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200)))
    monkeypatch.setenv("QUOTAROUTER_ALERT_TELEGRAM", "just-a-token")
    assert alerts.notify(alerts.QUOTA, "out of quota", now=100.0) is False
    assert sent == [], "no request at all: a half-configured target is a typo, not an endpoint"


def test_telegram_text_is_truncated_to_the_api_limit(state, monkeypatch):
    seen: dict = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=b'{"ok": true}')

    http_client.set_transport(httpx.MockTransport(handler))
    monkeypatch.setenv("QUOTAROUTER_ALERT_TELEGRAM", "tok:42")
    assert alerts.notify(alerts.QUOTA, "x" * 9000, now=100.0) is True
    assert len(seen["text"]) <= 4000
    assert seen["chat_id"] == "42"


def test_a_failing_command_is_reported_not_swallowed(state, monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_ALERT_COMMAND", "sh -c 'exit 3'")
    assert alerts.notify(alerts.KEYLESS, "served by keyless", now=100.0) is False


def test_the_cooldown_blocks_a_second_message_of_the_same_kind(state, monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_ALERT_COMMAND", "true")
    assert alerts.notify(alerts.QUOTA, "one", now=100.0) is True
    assert alerts.notify(alerts.QUOTA, "two", now=130.0) is False
    assert alerts.notify(alerts.QUOTA, "three", now=200.0) is True, "the window expired"


def test_a_corrupt_cooldown_file_is_ignored(state, monkeypatch):
    (state / "alerts.json").write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setenv("QUOTAROUTER_ALERT_COMMAND", "true")
    assert alerts.notify(alerts.QUOTA, "one", now=100.0) is True


def test_a_nonsense_cooldown_setting_falls_back_to_fifteen_minutes(state, monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_ALERT_COOLDOWN", "soon")
    assert alerts.settings().cooldown == 900.0


def test_nothing_configured_means_nothing_is_sent(state):
    assert alerts.settings().enabled is False
    assert alerts.notify(alerts.QUOTA, "out", now=100.0) is False


# --- the anonymous tier itself ------------------------------------------------------------
def test_the_anonymous_tier_is_unavailable_when_the_operator_disabled_it(state, monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_ALLOW_KEYLESS", "0")
    config.reset_settings_for_tests()
    assert keyless.KeylessStage().has_credential() is False


def test_an_event_stream_without_an_answer_is_a_transient_failure(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"event: ping\ndata: {}\n\n",
                              headers={"content-type": "text/event-stream"})

    http_client.set_transport(httpx.MockTransport(handler))
    outcome = keyless.KeylessStage().search("q", 3)
    assert outcome.payload is None
    assert outcome.failure.kind == "transient"


def test_an_empty_reply_is_empty_not_success(monkeypatch):
    http_client.set_transport(httpx.MockTransport(lambda r: httpx.Response(
        200, content=b'{"result": {"content": [{"type": "text", "text": ""}]}}',
        headers={"content-type": "application/json"})))
    outcome = keyless.KeylessStage().search("q", 3)
    assert outcome.payload is None
    assert outcome.failure.kind == "empty"
