"""CLI surface: the commands an operator (or a cron unit) actually runs.

Covered here because the interesting failures are in what the command *prints*: a status table
that hides a vendor, a probe that reports success when nothing answered, a selftest that says
"ok" for an empty page.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field

import pytest

from quotarouter import cli, config, ledger as ledger_mod, routing, types


@dataclass
class FakeCapability:
    search: bool = True
    extract: bool = True


@dataclass
class FakeState:
    limit: float = 1000.0
    used: float = 250.0
    pause_kind: str = ""
    pause_reason: str = ""
    reset_at: float | None = 1_800_000_000.0
    paused: bool = False
    source: str = "api"

    def remaining(self) -> float:
        return max(self.limit - self.used, 0.0)

    def is_paused(self, now: float) -> bool:
        return self.paused


@dataclass
class FakeStage:
    unit: str = "credits"
    credential: bool = True
    capability: FakeCapability = field(default_factory=FakeCapability)

    def has_credential(self) -> bool:
        return self.credential

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True


@dataclass
class FakeLedger:
    states: dict = field(default_factory=dict)

    def get(self, name: str) -> FakeState:
        return self.states.setdefault(name, FakeState())


@dataclass
class FakeRouter:
    stages: dict = field(default_factory=lambda: {"exa": FakeStage(), "jina": FakeStage("requests")})
    ledger: FakeLedger = field(default_factory=FakeLedger)
    search_result: routing.RouteResult | None = None
    extract_result: routing.RouteResult | None = None
    probe: dict = field(default_factory=dict)

    def now(self) -> float:
        return 1_000.0

    def run_search(self, query: str, limit: int = 5) -> routing.RouteResult:
        return self.search_result or routing.RouteResult()

    def run_extract(self, urls: list[str]) -> routing.RouteResult:
        return self.extract_result or routing.RouteResult()

    def probe_all(self, force: bool = False) -> dict:
        return self.probe

    def candidates(self, capability: str):
        return [], []

    def skips(self, capability: str):
        return []


@pytest.fixture
def fake(monkeypatch):
    router = FakeRouter()
    monkeypatch.setattr(cli, "get_router", lambda: router, raising=False)
    monkeypatch.setattr(config, "settings", lambda refresh=False: config.Settings(
        search_order=("exa", "jina"), extract_order=("jina",)), raising=False)
    return router


def test_status_json_is_machine_readable(fake, capsys):
    assert cli.cmd_status(argparse.Namespace(json=True)) == 0
    report = json.loads(capsys.readouterr().out)
    vendors = {row["vendor"]: row for row in report["vendors"]}
    assert set(vendors) == {"exa", "jina"}
    assert vendors["exa"]["left"] == "750"
    assert vendors["exa"]["unit"] == "credits"


def test_status_shows_an_unmetered_stage_as_unmetered(fake, capsys):
    fake.ledger.get("jina").limit = 0.0
    cli.cmd_status(argparse.Namespace(json=True))
    vendors = {row["vendor"]: row for row in json.loads(capsys.readouterr().out)["vendors"]}
    assert vendors["jina"]["left"] == "unmetered"


def test_status_marks_a_stage_without_a_credential(fake, capsys):
    fake.stages["jina"] = FakeStage(credential=False)
    cli.cmd_status(argparse.Namespace(json=True))
    vendors = {row["vendor"]: row for row in json.loads(capsys.readouterr().out)["vendors"]}
    assert vendors["jina"]["credential"] == "no"


def test_probe_exits_non_zero_when_a_vendor_did_not_answer(fake, capsys):
    fake.probe = {"exa": {"remaining": 5.0, "unit": "usd", "source": "api"},
                  "jina": {"remaining": None, "error": "timeout"}}
    assert cli.cmd_probe(argparse.Namespace(json=False)) == 1
    out = capsys.readouterr().out
    assert "probe failed" in out and "exa" in out


def test_probe_exits_zero_when_every_vendor_answered(fake, capsys):
    fake.probe = {"exa": {"remaining": 5.0, "unit": "usd", "source": "api"}}
    assert cli.cmd_probe(argparse.Namespace(json=True)) == 0
    assert json.loads(capsys.readouterr().out)["exa"]["remaining"] == 5.0


def test_disable_and_enable_survive_a_new_process(tmp_path, monkeypatch):
    """The real ledger, not a fake: the kill switch must outlive the process that set it."""
    monkeypatch.setattr(config, "settings",
                        lambda refresh=False: config.Settings(state_dir=tmp_path), raising=False)
    cli.cmd_disable(argparse.Namespace(vendor="deepseek", reason="balance empty"))
    cli.cmd_enable(argparse.Namespace(vendor="firecrawl", reason=None))
    reopened = ledger_mod.Ledger(config.settings().ledger_path)
    assert reopened.get("deepseek").is_paused(1_000.0)
    assert not reopened.get("firecrawl").is_paused(1_000.0)


def test_reset_forgets_local_accounting_only(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "settings",
                        lambda refresh=False: config.Settings(state_dir=tmp_path), raising=False)
    ledger = ledger_mod.Ledger(config.settings().ledger_path)
    ledger.set_remaining("firecrawl", 42.0)
    cli.cmd_reset(argparse.Namespace(vendor="firecrawl"))
    assert ledger_mod.Ledger(config.settings().ledger_path).get("firecrawl").used == 0.0


def test_selftest_reports_a_failed_search_and_exits_non_zero(fake, capsys):
    fake.search_result = routing.RouteResult()
    fake.extract_result = routing.RouteResult()
    code = cli.cmd_selftest(argparse.Namespace(query="q", url="https://a"))
    out = capsys.readouterr().out
    assert code == 1
    assert "FAILED" in out


def test_selftest_passes_when_both_legs_answer(fake, capsys):
    fake.search_result = routing.RouteResult()
    fake.search_result.stage = "exa"
    fake.search_result.payload = types.search_ok([types.web_hit("https://a", "A", "d", 1)])
    fake.extract_result = routing.RouteResult()
    fake.extract_result.stage = "jina"
    fake.extract_result.payload = [types.document("https://a", "A", "text")]
    code = cli.cmd_selftest(argparse.Namespace(query="q", url="https://a"))
    out = capsys.readouterr().out
    assert code == 0
    assert "search 'q': ok (exa)" in out and "extract https://a: ok" in out


def test_selftest_calls_an_empty_page_a_failure(fake, capsys):
    """`ok` from the chain with an empty body is how a stub page used to pass as success."""
    fake.search_result = routing.RouteResult()
    fake.search_result.stage = "exa"
    fake.search_result.payload = types.search_ok([types.web_hit("https://a", "A", "d", 1)])
    fake.extract_result = routing.RouteResult()
    fake.extract_result.stage = "jina"
    fake.extract_result.payload = [types.document("https://a", "A", "   ")]
    assert cli.cmd_selftest(argparse.Namespace(query="q", url="https://a")) == 1


def test_prune_on_a_state_dir_without_a_log_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "settings",
                        lambda refresh=False: config.Settings(state_dir=tmp_path), raising=False)
    assert cli.cmd_prune(argparse.Namespace(days=30, json=False)) == 0
    assert "no audit log yet" in capsys.readouterr().out


def test_serve_mcp_delegates_to_the_stdio_server(monkeypatch):
    called = {}

    def fake_serve():
        called["served"] = True
        return 0

    import quotarouter.integrations.mcp as mcp_module

    monkeypatch.setattr(mcp_module, "serve_stdio", fake_serve)
    assert cli.cmd_serve_mcp(argparse.Namespace()) == 0
    assert called.get("served") is True
