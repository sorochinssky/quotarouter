"""Operational surface: alerting, log retention, the probe daemon, schedule output.

These are the parts an operator leans on after the plugin "works": does it tell me when it
degrades, does the audit trail stay readable, and can I schedule the balance refresh without
inventing shell glue. All offline — transports are mocked, clocks are injected.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import httpx
import pytest

from quotarouter import alerts, cli, config, http_client, retention, routing, types


@pytest.fixture(autouse=True)
def clean_alert_state(monkeypatch):
    """No transports, no cooldown file, no leftover settings between tests."""
    for name in ("ALERT_TELEGRAM", "ALERT_WEBHOOK", "ALERT_COMMAND", "ALERT_COOLDOWN"):
        monkeypatch.delenv(f"QUOTAROUTER_{name}", raising=False)
    config.reset_settings_for_tests()
    alerts.reset_for_tests()
    yield
    alerts.reset_for_tests()
    config.reset_settings_for_tests()


# --- alerts -------------------------------------------------------------------------------
def test_silent_when_no_transport_is_configured():
    assert alerts.settings().enabled is False
    assert alerts.notify(alerts.KEYLESS, "should not be sent") is False


def test_command_transport_runs_and_is_recorded(monkeypatch, tmp_path):
    marker = tmp_path / "sent.txt"
    monkeypatch.setenv("QUOTAROUTER_ALERT_COMMAND", f"sh -c 'echo {{kind}} >> {marker}'")
    assert alerts.notify(alerts.KEYLESS, "served by the keyless tier", now=1000.0) is True
    assert marker.read_text().strip() == "keyless"


def test_cooldown_stops_a_flood_and_force_bypasses_it(monkeypatch, tmp_path):
    marker = tmp_path / "sent.txt"
    monkeypatch.setenv("QUOTAROUTER_ALERT_COMMAND", f"sh -c 'echo {{kind}} >> {marker}'")
    monkeypatch.setenv("QUOTAROUTER_ALERT_COOLDOWN", "900")

    assert alerts.notify(alerts.QUOTA, "out of quota", now=1000.0) is True
    assert alerts.notify(alerts.QUOTA, "out of quota", now=1100.0) is False  # still cooling down
    assert alerts.notify(alerts.QUOTA, "out of quota", now=2000.0) is True   # window passed
    assert alerts.notify(alerts.QUOTA, "forced", now=2010.0, force=True) is True
    assert len(marker.read_text().splitlines()) == 3


def test_a_broken_transport_never_raises(monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_ALERT_COMMAND", "/definitely/not/a/binary {kind}")
    assert alerts.notify(alerts.NO_STAGE, "nothing served", now=1000.0) is False


def test_webhook_transport_posts_json(monkeypatch):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, content=b"{}")

    monkeypatch.setenv("QUOTAROUTER_ALERT_WEBHOOK", "https://hooks.example/quotarouter")
    http_client.set_transport(httpx.MockTransport(handler))
    assert alerts.notify(alerts.KEYLESS, "served by the keyless tier", now=1000.0) is True
    assert seen and seen[0]["text"] == "served by the keyless tier"


def test_telegram_transport_requires_token_and_chat(monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_ALERT_TELEGRAM", "not-a-pair")
    assert alerts.notify(alerts.KEYLESS, "x", now=1000.0) is False


# --- the router alerts on degradation -----------------------------------------------------
def _router(tmp_path: Path) -> routing.Router:
    settings = config.settings(refresh=True)
    return routing.Router(settings=settings)


def test_keyless_answer_alerts(monkeypatch, tmp_path):
    marker = tmp_path / "sent.txt"
    monkeypatch.setenv("QUOTAROUTER_ALERT_COMMAND", f"sh -c 'echo {{kind}} >> {marker}'")
    router = _router(tmp_path)
    result = routing.RouteResult(payload={"data": {"web": []}}, stage=routing.KEYLESS_NAME)
    result.attempts.append(routing.RouteAttempt("exa", False, "quota: HTTP 402", spent=0.007))
    router.log_route("search", result, {"query": "x"})
    kinds = marker.read_text().split()
    assert "keyless" in kinds, kinds


def test_total_failure_alerts_with_no_stage(monkeypatch, tmp_path):
    marker = tmp_path / "sent.txt"
    monkeypatch.setenv("QUOTAROUTER_ALERT_COMMAND", f"sh -c 'echo {{kind}} >> {marker}'")
    router = _router(tmp_path)
    result = routing.RouteResult()
    result.attempts.append(routing.RouteAttempt("exa", False, "auth: HTTP 401"))
    router.log_route("search", result, {"query": "x"})
    assert marker.read_text().split() == ["no-stage"]


def test_quota_park_alerts(monkeypatch, tmp_path):
    marker = tmp_path / "sent.txt"
    monkeypatch.setenv("QUOTAROUTER_ALERT_COMMAND", f"sh -c 'echo {{kind}} >> {marker}'")
    router = _router(tmp_path)
    result = routing.RouteResult(payload={"data": {"web": [{"url": "u"}]}}, stage="firecrawl")
    result.attempts.append(routing.RouteAttempt("exa", False, f"{types.QUOTA}: HTTP 402"))
    result.attempts.append(routing.RouteAttempt("firecrawl", True, spent=2.0))
    router.log_route("search", result, {"query": "x"})
    assert "quota" in marker.read_text().split()


# --- retention ----------------------------------------------------------------------------
def _line(ts: str, served: str = "exa") -> str:
    return json.dumps({"ts": ts, "capability": "search", "served_by": served, "ok": True})


def test_rotation_moves_the_file_aside_once_it_is_big(tmp_path):
    path = tmp_path / "routes.jsonl"
    path.write_text("x" * 2048 + "\n", encoding="utf-8")
    assert retention.maybe_rotate(path, max_bytes=1024) is True
    assert not path.exists()
    assert (tmp_path / "routes.jsonl.1").exists()
    # a second call on a fresh file does nothing
    path.write_text("small\n", encoding="utf-8")
    assert retention.maybe_rotate(path, max_bytes=1024) is False


def test_prune_drops_only_old_lines(tmp_path):
    path = tmp_path / "routes.jsonl"
    now = time.time()
    fresh = retention_iso(now - 3600)
    old = retention_iso(now - 40 * 86400)
    path.write_text("\n".join([_line(old), _line(fresh), "not json at all"]) + "\n", encoding="utf-8")

    kept, dropped = retention.prune(path, days=30, now=now)
    assert (kept, dropped) == (2, 1), (kept, dropped)
    remaining = path.read_text()
    assert fresh in remaining and old not in remaining
    assert "not json at all" in remaining, "a line without a timestamp is evidence: keep it"


def retention_iso(ts: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")


def test_prune_directory_covers_the_rotated_generation(tmp_path):
    now = time.time()
    old = retention_iso(now - 100 * 86400)
    (tmp_path / "routes.jsonl").write_text(_line(old) + "\n", encoding="utf-8")
    (tmp_path / "routes.jsonl.1").write_text(_line(old) + "\n", encoding="utf-8")
    report = retention.prune_directory(tmp_path, days=30, now=now)
    assert report["routes.jsonl"][1] == 1 and report["routes.jsonl.1"][1] == 1


def test_log_route_rotates_by_itself(monkeypatch, tmp_path):
    monkeypatch.setenv("QUOTAROUTER_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("QUOTAROUTER_LOG_MAX_BYTES", "200")
    config.reset_settings_for_tests()
    router = _router(tmp_path)
    for index in range(20):
        router.log_route("search", routing.RouteResult(payload={"data": {"web": []}}, stage="exa"),
                         {"query": f"query {index}"})
    assert (tmp_path / "routes.jsonl.1").exists(), "the logging path must bound the file itself"


# --- daemon -------------------------------------------------------------------------------
def test_daemon_runs_the_requested_number_of_cycles():
    lines: list[str] = []
    sleeps: list[float] = []
    calls: list[int] = []

    def probe() -> dict:
        calls.append(1)
        return {"exa": {"remaining": 9.0}, "firecrawl": {"remaining": None}}

    cli.run_daemon(0.1, 3, probe=probe, sleep=sleeps.append, out=lines.append)
    assert len(calls) == 3
    assert sleeps == [0.1, 0.1]
    assert "firecrawl" in lines[0]


def test_daemon_survives_a_failing_cycle():
    lines: list[str] = []

    def probe() -> dict:
        raise RuntimeError("vendor down")

    cli.run_daemon(0.0, 2, probe=probe, sleep=lambda _s: None, out=lines.append)
    assert len(lines) == 2 and all("failed" in line for line in lines)


# --- schedule -----------------------------------------------------------------------------
def test_schedule_prints_cron_and_systemd(capsys, monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_PROBE_INTERVAL", "21600")
    config.reset_settings_for_tests()
    assert cli.main(["schedule"]) == 0
    out = capsys.readouterr().out
    assert "*/6 * * *" in out
    assert "quotarouter-probe.service" in out
    assert "daemon --once" in out


def test_schedule_json_is_machine_readable(capsys):
    assert cli.main(["schedule", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["interval"] == 21600
    assert "OnUnitActiveSec=21600s" in payload["systemd_timer"]


# --- alert-test ---------------------------------------------------------------------------
def test_alert_test_reports_missing_transport(capsys):
    assert cli.main(["alert-test"]) == 1
    assert "no alert transport configured" in capsys.readouterr().out


def test_alert_test_sends_when_configured(capsys, monkeypatch, tmp_path):
    marker = tmp_path / "sent.txt"
    monkeypatch.setenv("QUOTAROUTER_ALERT_COMMAND", f"sh -c 'echo {{kind}} >> {marker}'")
    assert cli.main(["alert-test", "--kind", "quota"]) == 0
    assert marker.read_text().strip() == "quota"
    assert "sent" in capsys.readouterr().out


# --- parallel fetching --------------------------------------------------------------------
def test_two_urls_are_fetched_concurrently(monkeypatch):
    """A barrier only lets both through if the two fetches overlap."""
    from quotarouter.stages.jina import JinaStage

    barrier = threading.Barrier(2, timeout=3)

    def handler(request: httpx.Request) -> httpx.Response:
        barrier.wait()  # fails the test with BrokenBarrierError if the calls are sequential
        return httpx.Response(200, text="Title: t\n\nMarkdown Content:\n" + ("text " * 40))

    monkeypatch.setenv("QUOTAROUTER_EXTRACT_WORKERS", "2")
    config.reset_settings_for_tests()
    http_client.set_transport(httpx.MockTransport(handler))
    pages = JinaStage().extract(["https://a.example/", "https://b.example/"])
    assert [page["url"] for page in pages] == ["https://a.example/", "https://b.example/"]


def test_workers_one_stays_sequential(monkeypatch):
    from quotarouter.stages.jina import JinaStage

    order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        order.append(str(request.url))
        return httpx.Response(200, text="Title: t\n\nMarkdown Content:\n" + ("text " * 40))

    monkeypatch.setenv("QUOTAROUTER_EXTRACT_WORKERS", "1")
    config.reset_settings_for_tests()
    http_client.set_transport(httpx.MockTransport(handler))
    JinaStage().extract(["https://a.example/", "https://b.example/"])
    assert order == ["https://r.jina.ai/https://a.example/", "https://r.jina.ai/https://b.example/"]


def test_alert_text_cannot_escape_into_shell_syntax(monkeypatch, tmp_path):
    """Vendor error bodies land in alerts; a body with `$(...)` must stay data, not syntax."""
    pwned = tmp_path / "pwned"
    monkeypatch.setenv("QUOTAROUTER_ALERT_COMMAND", "echo {text}")
    hostile = f"quota: $(touch {pwned}) `touch {pwned}` ; touch {pwned}"
    assert alerts.notify(alerts.QUOTA, hostile, now=1000.0) is True
    assert not pwned.exists(), "the alert text was executed as shell code"


def test_alert_text_is_available_as_an_environment_variable(monkeypatch, tmp_path):
    """The documented pattern for shell scripts: read the text from the environment."""
    monkeypatch.setenv("QUOTAROUTER_ALERT_COMMAND",
                       f"sh -c 'printf %s \"$QUOTAROUTER_ALERT_TEXT\" > {tmp_path}/env.txt'")
    text = "degraded $HOME ; rm -rf /tmp/nope"
    assert alerts.notify(alerts.KEYLESS, text, now=1000.0) is True
    assert (tmp_path / "env.txt").read_text() == text


def test_alert_kind_is_substituted_quoted(monkeypatch, tmp_path):
    marker = tmp_path / "kind.txt"
    monkeypatch.setenv("QUOTAROUTER_ALERT_COMMAND", f"sh -c 'echo {{kind}} > {marker}'")
    assert alerts.notify(alerts.QUOTA, "x", now=1000.0) is True
    assert marker.read_text().strip() == "quota"
