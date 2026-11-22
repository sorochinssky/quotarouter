"""The review harness is code too, and its failures are silent by construction.

Every test here comes from a run that was written off as "the model could not follow
instructions": one reviewer's five findings were thrown away because its JSON came back
escaped, another's because the answer was cut off mid-object.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "scripts" / "model_review.py"


def load():
    spec = importlib.util.spec_from_file_location("model_review_under_test", PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


harness = load()
BACKSLASH = chr(92)


def escaped(payload: str) -> str:
    """What a model sends when it prints JSON inside a JSON string field."""
    return payload.replace('"', BACKSLASH + '"')


def test_a_plain_answer_parses():
    raw = '{"verdict": "approve", "summary": "ok", "findings": []}'
    assert harness.extract_json(raw)["verdict"] == "approve"


def test_a_fenced_answer_parses():
    raw = 'Here you go:\n```json\n{"verdict": "changes", "summary": "x", "findings": []}\n```'
    assert harness.extract_json(raw)["verdict"] == "changes"


def test_an_escaped_answer_is_unwrapped_instead_of_discarded():
    raw = escaped('{"verdict": "changes", "summary": "two bugs", "findings": '
                  '[{"severity": "major", "file": "quotarouter/ledger.py", "line": 12, '
                  '"title": "lost update", "why": "two writers", "certain": true}]}')
    parsed = harness.extract_json(raw)
    assert parsed["verdict"] == "changes", parsed
    assert [f["title"] for f in parsed["findings"]] == ["lost update"]


def test_a_truncated_answer_keeps_the_findings_that_did_parse():
    raw = ('{"verdict": "changes", "summary": "five defects", "findings": ['
           '{"severity": "critical", "title": "first", "why": "a"}, '
           '{"severity": "major", "title": "second", "why": "b"}, '
           '{"severity": "critical", "title": "third", "why": "and then it ran out of tok')
    parsed = harness.extract_json(raw)
    assert parsed["verdict"] == "changes"
    assert [f["title"] for f in parsed["findings"]] == ["first", "second"]


def test_prose_without_json_is_still_reported_as_unparsed():
    parsed = harness.extract_json("We need to review the code thoroughly. Let's think.")
    assert parsed["verdict"] == "unparsed"
    assert parsed["findings"] == []


def test_unescape_leaves_a_lone_backslash_alone():
    assert harness.unescape("C:" + BACKSLASH + "path") == "C:" + BACKSLASH + "path"


def test_the_answer_records_which_code_state_was_reviewed():
    """A finding against code that has since changed must be identifiable as stale."""
    digest = harness.fingerprint(["quotarouter/ledger.py"])
    assert len(digest) == 12 and digest != harness.fingerprint(["quotarouter/alerts.py"])
