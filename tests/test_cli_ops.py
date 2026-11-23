"""The CLI surface an operator actually types: status, prune, daemon, schedule, pause columns.

These are the commands a cron job or a human runs while something is already wrong. A traceback
here is worse than a wrong answer, so every degenerate case (a chain entry the file dropped, a call
nobody could serve, an audit log with nothing in it, a pause with no deadline) must end in a printed
sentence and a meaningful exit code.
"""

from __future__ import annotations

import json
import time

import pytest

from quotarouter import cli, config, ledger as ledger_mod, routing, types
from quotarouter.stages.base import Capability, Stage


class Fake(Stage):
    key_env = ""

    def __init__(self, name="exa"):
        self.name = name
        self.capability = Capability(search=True, extract=True, search_units=1.0, extract_units=1.0)
        self.unit = "requests"
        self.funding = "free"
        self.monthly_allowance = None
        self.calls: list[str] = []

    def search(self, query, limit):
        self.calls.append("search")
        return types.search_ok([{"title": "t", "url": "https://exa.example/"}])

    def extract(self, urls):
        self.calls.append("extract")
        return [types.document(url, "t", "body " * 20, source_url=url) for url in urls]


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "chains.toml"
    path.write_text('[chains.broken]\nsearch = ["nope"]\n', encoding="utf-8")
    monkeypatch.setenv("QUOTAROUTER_CHAINS_FILE", str(path))
    monkeypatch.setenv("QUOTAROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("QUOTAROUTER_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("QUOTAROUTER_ALLOW_KEYLESS", "0")
    monkeypatch.setenv("QUOTAROUTER_KEYLESS_LAST_RESORT", "0")
    monkeypatch.delenv("QUOTAROUTER_CHAINS", raising=False)
    config.reset_settings_for_tests()

    stage = Fake()
    settings = config.settings()
    router = routing.Router(stages={"exa": stage},
                            settings=config.Settings(search_order=("exa",), extract_order=("exa",),
                                                     state_dir=tmp_path / "state"),
                            ledger=ledger_mod.Ledger(tmp_path / "state" / "ledger.json"))
    monkeypatch.setattr(cli, "get_router", lambda: router, raising=False)
    return {"router": router, "stage": stage, "path": path, "settings": settings, "dir": tmp_path}


def _args(*argv):
    return cli.build_parser().parse_args(list(argv))


# --- status: a configuration problem is reported, not raised -------------------------------
def test_status_names_the_chain_entry_the_file_dropped(env, capsys):
    assert cli.cmd_status(_args("status")) == 0
    out = capsys.readouterr().out

    assert "dropped" in out, "an operator who wrote a typo must see it before wondering why nothing changed"
    assert "nope" in out
    assert "none (from" in out, "with every entry dropped there is no chain to name, and the source says why"


def test_chains_says_plainly_that_nothing_is_configured(env, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("QUOTAROUTER_CHAINS_FILE", str(tmp_path / "absent.toml"))
    config.reset_settings_for_tests()

    assert cli.cmd_chains(_args("chains")) == 0
    assert "no named chains" in capsys.readouterr().out


# --- call: "nobody could serve it" is exit 1, with the reason ------------------------------
def test_call_reports_why_nobody_could_serve_it(env, monkeypatch, capsys):
    empty = routing.RouteResult()
    monkeypatch.setattr(env["router"], "run_search", lambda query, limit, **options: empty)

    assert cli.cmd_call(_args("call", "search", "q")) == 1
    out = capsys.readouterr().out
    assert "search" in out, "the failure must say which capability failed"


# --- prune ------------------------------------------------------------------------------
def _audit_log(directory, old_line: str, fresh_line: str):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "routes.jsonl"
    path.write_text(old_line + "\n" + fresh_line + "\n", encoding="utf-8")
    return path


def _iso_days_ago(days: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))


def test_prune_reports_what_it_kept_and_dropped(env, capsys):
    # `ts` is an ISO timestamp in the audit log; a line without one is kept on purpose, so a test
    # that wrote an epoch number would be testing the wrong rule.
    old = json.dumps({"ts": _iso_days_ago(10), "capability": "search", "ok": True})
    fresh = json.dumps({"ts": _iso_days_ago(0), "capability": "search", "ok": True})
    _audit_log(env["dir"] / "state", old, fresh)

    assert cli.cmd_prune(_args("prune", "--days", "1")) == 0
    out = capsys.readouterr().out
    assert "kept 1, dropped 1" in out

    assert cli.cmd_prune(_args("prune", "--days", "1", "--json")) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["days"] == 1 and "routes.jsonl" in report["files"]


def test_prune_without_an_audit_log_says_so(env, capsys):
    assert cli.cmd_prune(_args("prune", "--days", "1")) == 0
    assert "no audit log yet" in capsys.readouterr().out


# --- daemon and schedule: print, never install ---------------------------------------------
def test_daemon_defaults_to_the_configured_interval(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "run_daemon",
                        lambda interval, iterations: seen.update(interval=interval,
                                                                 iterations=iterations) or 0)

    assert cli.cmd_daemon(_args("daemon", "--once")) == 0
    assert seen == {"interval": 21600, "iterations": 1}

    assert cli.cmd_daemon(_args("daemon", "--interval", "600")) == 0
    assert seen == {"interval": 600, "iterations": 0}


@pytest.mark.parametrize("interval,expected", [
    (21600, "17 */6 * * *"),   # six hours divides the day: an hour step
    (7200, "17 */2 * * *"),    # two hours does too
    (300, "*/5 * * * *"),      # under an hour: minutes
    (18000, "17 * * * *"),     # five hours does not divide 24: hourly is the honest fallback
])
def test_schedule_prints_a_cron_line_that_matches_the_interval(env, capsys, interval, expected):
    assert cli.cmd_schedule(_args("schedule", "--interval", str(interval))) == 0
    out = capsys.readouterr().out
    assert expected in out
    assert "quotarouter.cli daemon --once" in out, "the unit must call the daemon, not invent one"


def test_schedule_json_carries_the_same_decision(env, capsys):
    assert cli.cmd_schedule(_args("schedule", "--interval", "21600", "--json")) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["interval"] == 21600
    assert report["cron"].startswith("17 */6 * * *")


def test_the_default_env_file_is_not_guessed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("QUOTAROUTER_ENV_FILE", raising=False)
    monkeypatch.delenv("ROUTER_ENV_FILE", raising=False)
    assert cli._default_env_file() == "", "no file to name is better than a path that does not exist"

    monkeypatch.setenv("QUOTAROUTER_ENV_FILE", "/etc/quotarouter.env")
    assert cli._default_env_file() == "/etc/quotarouter.env"


# --- the small formatting decisions --------------------------------------------------------
def test_a_machine_pause_without_a_deadline_is_described_as_such(tmp_path):
    ledger = ledger_mod.Ledger(tmp_path / "ledger.json")
    ledger.pause("exa", float("inf"), types.QUOTA, "balance empty", origin=ledger_mod.ORIGIN_AUTO)
    state = ledger.get("exa")

    text = cli._pause(state, time.time())
    assert "re-probed" in text, "the operator must know this one clears itself without waiting"


def test_an_absent_timestamp_is_a_dash():
    assert cli._iso(None) == "-"
    assert cli._iso(float("inf")) == "-"


def test_an_interrupt_is_exit_130_not_a_traceback(monkeypatch):
    def interrupted(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_status", interrupted)
    monkeypatch.setattr(cli, "build_parser", lambda: _parser_with(interrupted))
    assert cli.main(["status"]) == 130


def _parser_with(func):
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    status = sub.add_parser("status")
    status.set_defaults(func=func)
    return parser
