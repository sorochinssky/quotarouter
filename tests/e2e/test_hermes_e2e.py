"""E2E-тесты: плагин внутри настоящего Hermes.

Помечены `e2e` и `live`, поэтому не бегут в обычном `pytest -q`. Траты за прогон:
один поиск у Exa (~$0.007) и одна страница (Jina бесплатно, Firecrawl — 1 кредит если Jina
не справится).

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
    raise AssertionError(f"шаг {name} не выполнялся: {[s.name for s in report.steps]}")


def test_hermes_has_the_plugin_installed() -> None:
    if not (harness.HERMES_ROOT / "tools" / "web_tools.py").exists():
        pytest.skip(f"Hermes не установлен в {harness.HERMES_ROOT}")


def test_plugin_registers_in_the_hermes_registry(report):
    step = _step(report, "plugin_registry")
    assert step.ok, step.detail


def test_search_goes_through_the_chain(report):
    step = _step(report, "search_tool")
    assert step.ok, step.detail
    assert str(step.detail["backend"]).startswith("router:"), step.detail
    assert step.detail["results"] >= 1


def test_extract_returns_real_content(report):
    step = _step(report, "extract_tool")
    assert step.ok, step.detail
    # example.com — легально короткая страница (~160 символов); порог только про пустоту
    assert step.detail["chars"] >= 40


def test_quota_accounting_lands_in_the_isolated_home(report):
    step = _step(report, "state_isolated")
    assert step.ok, step.detail
    assert step.detail["routes"] >= 2, "поиск и вытяжка должны оставить по строке в журнале"
    assert step.detail["served_by"], "в журнале нет вендора, который обслужил вызов"


def test_no_keys_on_any_stage_fails_honestly(report):
    step = _step(report, "degradation_without_keys")
    assert step.ok, step.detail


def test_live_profile_untouched(report):
    step = _step(report, "isolation")
    assert step.ok, step.detail


def test_report_says_what_it_cost(report):
    assert report.spent["search"] <= 1 and report.spent["extract"] <= 1, report.spent
