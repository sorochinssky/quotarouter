"""E2E tests: the plugin inside a real Hermes.

Marked `e2e` and `live`, so a plain `pytest -q` skips them. Cost of one run: one Exa search
(~$0.007) and one page (Jina is free; Firecrawl costs 1 credit only if Jina fails).

    python -m pytest -m e2e -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import harness  # noqa: E402

pytestmark = [pytest.mark.e2e, pytest.mark.live]


@pytest.fixture(scope="module")
def report() -> harness.RunReport:
    result = harness.run(live=True, keep_home=False)
    (harness.REPO_ROOT / "e2e-report.json").write_text(
        json.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _step(report: harness.RunReport, name: str) -> harness.StepReport:
    for step in report.steps:
        if step.name == name:
            return step
    raise AssertionError(f"step {name} did not run: {[s.name for s in report.steps]}")


def test_hermes_is_installed() -> None:
    if not (harness.HERMES_ROOT / "tools" / "web_tools.py").exists():
        pytest.skip(f"no Hermes installation in {harness.HERMES_ROOT}")


def test_plugin_registers_in_the_hermes_registry(report):
    step = _step(report, "plugin_registry")
    assert step.ok, step.detail


def test_search_goes_through_the_chain(report):
    step = _step(report, "search_tool")
    assert step.ok, step.detail
    assert str(step.detail["backend"]).startswith("quotarouter:"), step.detail
    assert step.detail["results"] >= 1


def test_extract_returns_real_content(report):
    step = _step(report, "extract_tool")
    assert step.ok, step.detail
    # example.com is legitimately short (~160 chars); the threshold is only about emptiness
    assert step.detail["chars"] >= 40


def test_quota_accounting_lands_in_the_isolated_home(report):
    step = _step(report, "state_isolated")
    assert step.ok, step.detail
    assert step.detail["routes"] >= 2, "search and extract must each leave a line in the route log"
    assert step.detail["served_by"], "the route log names no vendor that served a call"


def test_no_keys_on_any_stage_fails_honestly(report):
    step = _step(report, "degradation_without_keys")
    assert step.ok, step.detail


def test_live_profile_untouched(report):
    step = _step(report, "isolation")
    assert step.ok, step.detail


def test_report_says_what_it_cost(report):
    assert report.spent["search"] <= 1 and report.spent["extract"] <= 1, report.spent
