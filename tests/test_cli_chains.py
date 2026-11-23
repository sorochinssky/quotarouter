"""The CLI surface for chains: what the file gave, what a call would use, and one real call.

`route` and `chains` must never dial a vendor — they exist to answer a question before spending
anything. `call` is the library API in one command, and its exit codes separate "you asked for
something impossible" (2) from "nobody could serve it" (1).
"""

from __future__ import annotations

import json

import pytest

from quotarouter import cli, config, ledger as ledger_mod, routing, types
from quotarouter.stages.base import Capability, Stage


class Fake(Stage):
    key_env = ""

    def __init__(self, name, search=True, extract=False):
        self.name = name
        self.capability = Capability(search=search, extract=extract,
                                     search_units=1.0, extract_units=1.0)
        self.unit = "requests"
        self.funding = "free"
        self.monthly_allowance = None
        self.calls: list[str] = []

    def search(self, query, limit):
        self.calls.append("search")
        return types.search_ok([types.web_hit(f"https://{self.name}.example/", self.name, "d", 1)])

    def extract(self, urls):
        self.calls.append("extract")
        return [types.document(url, self.name, "body " * 20, source_url=url) for url in urls]


CHAIN_FILE = """
[chains.one]
description = "One vendor, no tail"
search = ["exa"]
extract = ["jina"]

[chains.broken]
search = ["nope"]
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "chains.toml"
    path.write_text(CHAIN_FILE, encoding="utf-8")
    monkeypatch.setenv("QUOTAROUTER_CHAINS_FILE", str(path))
    monkeypatch.setenv("QUOTAROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("QUOTAROUTER_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("QUOTAROUTER_ALLOW_KEYLESS", "0")
    monkeypatch.setenv("QUOTAROUTER_KEYLESS_LAST_RESORT", "0")
    monkeypatch.delenv("QUOTAROUTER_CHAINS", raising=False)
    config.reset_settings_for_tests()

    stages = {"exa": Fake("exa"), "jina": Fake("jina", search=False, extract=True),
              "firecrawl": Fake("firecrawl")}
    router = routing.Router(stages=stages, settings=config.settings(),
                            ledger=ledger_mod.Ledger(tmp_path / "state" / "ledger.json"))
    monkeypatch.setattr(cli, "get_router", lambda: router, raising=False)
    return {"router": router, "stages": stages, "path": path}


def _args(*argv):
    return cli.build_parser().parse_args(list(argv))


# --- what the file gave -------------------------------------------------------------------
def test_chains_lists_what_loaded_and_what_did_not(env, capsys):
    assert cli.cmd_chains(_args("chains")) == 0
    out = capsys.readouterr().out

    assert "source: file" in out
    assert "one" in out and "search: exa" in out
    assert "dropped chains.broken" in out, "a dropped entry must be visible, not swallowed"
    assert "per-call overrides: allowed" in out


def test_chains_json_is_machine_readable(env, capsys):
    assert cli.cmd_chains(_args("chains", "--json")) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["chains"]["one"]["search"] == ["exa"]
    assert report["chains"]["one"]["strict"] is True
    assert report["problems"] and "nope" in report["problems"][0]
    assert report["allow_chain_override"] is True


# --- what a call would use, without calling -----------------------------------------------
def test_route_resolves_a_named_chain_without_dialing(env, capsys):
    assert cli.cmd_route(_args("route", "--chain", "one")) == 0
    out = capsys.readouterr().out

    assert "search: named (exa)" in out
    assert "extract: named (jina)" in out
    assert env["stages"]["exa"].calls == [] and env["stages"]["jina"].calls == []


def test_route_refuses_an_unknown_chain_and_says_so(env, capsys):
    assert cli.cmd_route(_args("route", "--chain", "nope")) == 1
    out = capsys.readouterr().out

    assert "refused" in out and "one" in out


def test_route_shows_a_budget(env, capsys):
    cli.cmd_route(_args("route", "--budget", "free"))
    assert "[budget=free]" in capsys.readouterr().out


# --- one real call -------------------------------------------------------------------------
def test_call_pins_one_stage_and_reports_it(env, capsys):
    assert cli.cmd_call(_args("call", "search", "how", "to", "--stages", "firecrawl",
                              "--json")) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["ok"] is True
    assert report["served_by"] == "firecrawl"
    assert report["chain_mode"] == "custom"
    assert report["requested"] == ["firecrawl"]
    assert env["stages"]["exa"].calls == [], "a pinned call must not touch another vendor"


def test_call_reads_a_query_with_spaces(env, capsys):
    cli.cmd_call(_args("call", "search", "two", "words"))
    assert "served by exa" in capsys.readouterr().out


def test_call_rejects_an_impossible_request_with_exit_two(env, capsys):
    assert cli.cmd_call(_args("call", "search", "q", "--stages", "nope")) == 2
    assert "refused" in capsys.readouterr().out
    assert env["stages"]["exa"].calls == []


def test_call_extract_takes_several_urls(env, capsys):
    assert cli.cmd_call(_args("call", "extract", "https://a.example/", "https://b.example/",
                              "--chain", "one", "--json")) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["served_by"] == "jina"
    assert report["delivered"] == {"jina": 2}


def test_call_writes_the_audit_line(env, capsys):
    cli.cmd_call(_args("call", "search", "q", "--stages", "exa"))
    lines = env["router"].settings.log_path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[-1])

    assert record["chain_mode"] == "custom"
    assert record["source"] == "cli"
