"""The paths that only run when something else already went wrong.

A quota router earns its keep in the bad cases: a ledger file half-written by a killed process, a
vendor that answers with HTML instead of JSON, a probe that raises, an audit log that cannot be
written. None of these may take a call down, and all of them must leave a reason behind.
"""

from __future__ import annotations

import json
import os
import time

import httpx
import pytest

from quotarouter import alerts, config, errors, http_client, ledger as ledger_mod, retention, routing
from quotarouter import types
from quotarouter.stages import deepseek, firecrawl, jina, keenable, keyless, tavily
from quotarouter.stages.base import Capability, Stage


# --- the ledger survives a file somebody else broke ---------------------------------------
def test_a_ledger_without_a_vendor_map_starts_fresh(tmp_path, caplog):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(["not", "a", "map"]), encoding="utf-8")

    ledger = ledger_mod.Ledger(path)

    assert ledger.get("exa").used == 0.0
    assert ledger.get("exa").limit == 0.0


def test_one_unreadable_entry_does_not_discard_the_others(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"vendors": {
        "exa": {"used": 3.0, "limit": 10.0, "unit": "usd"},
        "broken": "not a record",
    }}), encoding="utf-8")

    ledger = ledger_mod.Ledger(path)

    assert ledger.get("exa").used == 3.0, "a charge that happened must survive a neighbour's typo"
    assert ledger.get("broken").used == 0.0


def test_a_nested_transaction_does_not_deadlock(tmp_path):
    ledger = ledger_mod.Ledger(tmp_path / "ledger.json")

    with ledger._transaction():
        with ledger._transaction():
            ledger.charge("exa", 1.0)

    assert ledger.get("exa").used == 1.0


def test_a_failed_save_removes_its_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    ledger = ledger_mod.Ledger(path)

    def refuse(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ledger_mod.os, "replace", refuse)
    with pytest.raises(OSError):
        ledger.charge("exa", 1.0)

    leftovers = [name for name in os.listdir(tmp_path) if name.startswith(".ledger")]
    assert leftovers == [], "a half-written temp file must not be left for the next process to read"


def test_reset_all_clears_every_vendor(tmp_path):
    ledger = ledger_mod.Ledger(tmp_path / "ledger.json")
    ledger.charge("exa", 2.0)
    ledger.set_limit("exa", 10.0)

    ledger.reset_all()

    assert ledger.get("exa").used == 0.0


def test_a_pause_that_has_already_expired_has_no_time_left(tmp_path):
    ledger = ledger_mod.Ledger(tmp_path / "ledger.json")
    ledger.pause("exa", time.time() - 60, types.QUOTA, "expired")
    assert ledger.get("exa").pause_left() == 0.0


def test_a_disabled_vendor_written_by_something_else_is_an_operator_stop(tmp_path):
    state = ledger_mod.VendorState(vendor="exa")
    state.pause_kind = "disabled"
    state.pause_reason = "operator: leaving this vendor"

    assert ledger_mod.pause_origin_of(state) == ledger_mod.ORIGIN_OPERATOR

    state.pause_reason = "usage anomaly"
    assert ledger_mod.pause_origin_of(state) == ledger_mod.ORIGIN_AUTO


# --- rotation and pruning ----------------------------------------------------------------
def test_a_log_that_cannot_be_rotated_is_reported_not_raised(tmp_path):
    path = tmp_path / "routes.jsonl"
    path.write_text("x" * 100, encoding="utf-8")

    assert retention.maybe_rotate(path, max_bytes=10) in (True, False)
    assert path.exists() or (tmp_path / "routes.jsonl.1").exists()


def test_pruning_keeps_a_blank_line_and_a_line_without_a_time(tmp_path):
    path = tmp_path / "routes.jsonl"
    path.write_text("\n" + json.dumps({"capability": "search"}) + "\n", encoding="utf-8")

    kept, dropped = retention.prune(path, days=1)

    assert (kept, dropped) == (1, 0), "an audit line without a timestamp is evidence, not noise"


def test_a_prune_that_cannot_write_leaves_the_file_alone(tmp_path, monkeypatch):
    path = tmp_path / "routes.jsonl"
    original = json.dumps({"ts": "2020-01-01T00:00:00Z", "ok": True}) + "\n"
    path.write_text(original, encoding="utf-8")

    monkeypatch.setattr(retention.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
    kept, dropped = retention.prune(path, days=1)

    assert (kept, dropped) == (1, 0)
    assert path.read_text(encoding="utf-8") == original


def test_alert_state_that_cannot_be_written_does_not_fail_a_call(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))

    alerts._write_state({"keyless": time.time()})  # must not raise


# --- the shared http client --------------------------------------------------------------
def test_the_client_is_created_once_and_reused():
    http_client.set_transport(None)
    first = http_client.client()
    assert http_client.client() is first


def test_a_header_object_that_cannot_be_mapped_is_empty():
    assert http_client.header_map(object()) == {}


def test_an_unmapped_status_lands_on_a_transient_error():
    error = errors.classify(418, "short and stout")
    assert isinstance(error, errors.TransientError)


# --- probes that raise are reported, not fatal -------------------------------------------
class Boom(Stage):
    key_env = ""

    def __init__(self):
        self.name = "boom"
        self.unit = "requests"
        self.funding = "free"
        self.monthly_allowance = None
        self.capability = Capability(search=True, extract=False, search_units=1.0)

    def probe_quota(self):
        raise RuntimeError("vendor endpoint changed")

    def search(self, query, limit):  # pragma: no cover - never reached
        raise AssertionError("should not be dialed")


def test_a_probe_that_raises_is_one_line_in_the_report(tmp_path, monkeypatch):
    router = routing.Router(stages={"boom": Boom()},
                            settings=config.Settings(search_order=("boom",), state_dir=tmp_path),
                            ledger=ledger_mod.Ledger(tmp_path / "ledger.json"))

    def explode(*args, **kwargs):
        raise RuntimeError("vendor endpoint changed")

    monkeypatch.setattr(router, "_sync_quota", explode)
    report = router.probe_all(force=True)

    assert "probe raised" in report["boom"]["error"]


def test_a_result_with_no_attempts_has_no_skips():
    assert routing.RouteResult().skipped() == []


def test_an_unknown_capability_is_refused_before_anything_else(tmp_path):
    router = routing.Router(settings=config.Settings(state_dir=tmp_path),
                            ledger=ledger_mod.Ledger(tmp_path / "ledger.json"))
    with pytest.raises(ValueError, match="capability must be"):
        router.resolve("summarise")


def test_forgetting_the_shared_router_is_safe_to_call(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_STATE_DIR", str(tmp_path))
    config.reset_settings_for_tests()
    first = routing.shared_router()
    routing.reset_shared_router()
    assert routing.shared_router() is not first


# --- a pause whose window rolled is released by the next call ----------------------------
class Flaky(Stage):
    key_env = ""

    def __init__(self):
        self.name = "flaky"
        self.unit = "requests"
        self.funding = "free"
        self.monthly_allowance = None
        self.capability = Capability(search=True, extract=False, search_units=1.0)
        self.calls = 0

    def search(self, query, limit):
        self.calls += 1
        return types.search_ok([{"title": "t", "url": "https://flaky.example/"}])

    def extract(self, urls):  # pragma: no cover - search only
        return []


def test_a_quota_pause_whose_window_rolled_is_released(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_KEYLESS_LAST_RESORT", "0")
    stage = Flaky()
    ledger = ledger_mod.Ledger(tmp_path / "ledger.json")
    ledger.set_limit("flaky", 10.0)
    ledger.pause("flaky", time.time() - 1, types.QUOTA, "month ended",
                 origin=ledger_mod.ORIGIN_AUTO)
    router = routing.Router(stages={"flaky": stage},
                            settings=config.Settings(search_order=("flaky",), state_dir=tmp_path),
                            ledger=ledger)

    usable, skipped = router.candidates("search")

    assert [s.name for s in usable] == ["flaky"], skipped
    assert not ledger.get("flaky").is_paused(time.time())


# --- extraction: per-page problems and the keyless last resort ---------------------------
class TwoVendors(Stage):
    key_env = ""

    def __init__(self, name, pages):
        self.name = name
        self.unit = "requests"
        self.funding = "free"
        self.monthly_allowance = None
        self.capability = Capability(search=False, extract=True, extract_units=1.0)
        self.pages = pages

    def search(self, query, limit):  # pragma: no cover - extract only
        raise AssertionError("extract only")

    def extract(self, urls):
        return [self.pages(url) for url in urls]


def test_the_keyless_tier_takes_only_the_pages_nobody_else_delivered(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_KEYLESS_LAST_RESORT", "1")
    monkeypatch.setenv("QUOTAROUTER_ALLOW_KEYLESS", "1")
    paid = TwoVendors("paid", lambda url: types.document(url, "t", "body " * 20, source_url=url))
    anonymous = TwoVendors("keyless", lambda url: types.document(url, "k", "anon " * 20,
                                                                 source_url=url))
    settings = config.Settings(search_order=(), extract_order=("paid",), state_dir=tmp_path)
    router = routing.Router(stages={"paid": paid, "keyless": anonymous}, settings=settings,
                            ledger=ledger_mod.Ledger(tmp_path / "ledger.json"))

    result = router.run_extract(["https://a", "https://b"])

    assert result.ok
    assert router.stages["keyless"].__class__ is TwoVendors
    assert {page["url"] for page in result.payload} == {"https://a", "https://b"}


def test_a_stage_that_returns_nothing_is_paused_as_empty(tmp_path):
    class Nothing(TwoVendors):
        def extract(self, urls):
            return []

    nothing = Nothing("nothing", lambda url: None)
    router = routing.Router(stages={"nothing": nothing},
                            settings=config.Settings(search_order=(), extract_order=("nothing",),
                                                     state_dir=tmp_path),
                            ledger=ledger_mod.Ledger(tmp_path / "ledger.json"))

    result = router.run_extract(["https://a"])

    assert not result.ok
    assert router.ledger.get("nothing").pause_kind == types.EMPTY


def test_a_budget_ceiling_reached_the_audit_line(tmp_path):
    stage = TwoVendors("paid", lambda url: types.document(url, "t", "body " * 20, source_url=url))
    router = routing.Router(stages={"paid": stage},
                            settings=config.Settings(search_order=(), extract_order=("paid",),
                                                     state_dir=tmp_path),
                            ledger=ledger_mod.Ledger(tmp_path / "ledger.json"))

    result = router.run_extract(["https://a"], max_cost_usd=5.0)
    router.log_route("extract", result, {"urls": ["https://a"]})

    record = json.loads((router.settings.log_path).read_text(encoding="utf-8").splitlines()[-1])
    assert record["max_cost_usd"] == 5.0


def test_a_log_that_cannot_be_written_does_not_fail_the_call(tmp_path, monkeypatch):
    stage = TwoVendors("paid", lambda url: types.document(url, "t", "body " * 20, source_url=url))
    router = routing.Router(stages={"paid": stage},
                            settings=config.Settings(search_order=(), extract_order=("paid",),
                                                     state_dir=tmp_path),
                            ledger=ledger_mod.Ledger(tmp_path / "ledger.json"))
    result = router.run_extract(["https://a"])

    monkeypatch.setattr(routing.retention, "append",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    router.log_route("extract", result, {"urls": ["https://a"]})  # must not raise


# --- vendor payloads we did not expect ----------------------------------------------------
def _json_vendor(stage_factory, key_env, keys, body, status=200, content_type="text/html"):
    keys(**{key_env: "k"})
    stage = stage_factory()

    def handler(request):
        return httpx.Response(status, content=body.encode("utf-8"),
                              headers={"content-type": content_type})

    return stage, handler


def test_a_vendor_that_answers_with_html_is_a_failure_not_a_crash(use_transport, keys):
    """Vendors differ: some raise, some return a failed outcome with a reason. Both are honest,
    neither may look like a result — a page of maintenance HTML is not an answer."""
    for factory, key in ((tavily.TavilyStage, "TAVILY_API_KEY"),
                         (keenable.KeenableStage, "KEENABLE_API_KEY"),
                         (firecrawl.FirecrawlStage, "FIRECRAWL_API_KEY"),
                         (deepseek.DeepSeekStage, "DEEPSEEK_API_KEY")):
        stage, handler = _json_vendor(factory, key, keys, "<html>maintenance</html>")
        use_transport(handler)
        try:
            outcome = stage.search("q", 3)
        except errors.StageError as exc:
            assert exc.kind, f"{stage.name} raised without a kind"
        else:
            assert not outcome.ok and outcome.failure is not None, f"{stage.name} accepted HTML"
        use_transport(lambda request: httpx.Response(200, json={}))


def test_tavily_with_no_results_is_an_empty_failure(use_transport, keys):
    keys(TAVILY_API_KEY="k")
    use_transport(lambda request: httpx.Response(200, json={"results": []}))

    outcome = tavily.TavilyStage().search("q", 3)

    assert not outcome.ok
    assert outcome.failure is not None and outcome.failure.kind == types.EMPTY


def test_keenable_fetch_failure_is_one_page_not_the_batch(use_transport, keys):
    keys(KEENABLE_API_KEY="k")

    def handler(request):
        if request.url.path.endswith("/v1/fetch"):
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, json={})

    use_transport(handler)
    pages = keenable.KeenableStage().extract(["https://a"])

    assert len(pages) == 1 and pages[0].get("error")


def test_keenable_empty_page_is_an_error_not_an_empty_document(use_transport, keys):
    keys(KEENABLE_API_KEY="k")
    use_transport(lambda request: httpx.Response(200, json={"content": "   "}))

    pages = keenable.KeenableStage().extract(["https://a"])

    assert pages[0].get("error") and "empty" in pages[0]["error"]


def test_keenable_has_no_public_quota_endpoint_and_says_so():
    assert keenable.KeenableStage().probe_quota() is None


def test_firecrawl_survives_an_unexpected_error_in_one_page(use_transport, keys):
    keys(FIRECRAWL_API_KEY="k")

    def handler(request):
        return httpx.Response(200, json={"data": {"markdown": None}})

    use_transport(handler)
    stage = firecrawl.FirecrawlStage()
    pages = stage.extract(["https://a"])
    assert isinstance(pages[0], dict) and pages[0].get("error")


def test_jina_reports_an_http_error_without_an_envelope(use_transport):
    use_transport(lambda request: httpx.Response(503, content=b"unavailable"))

    pages = jina.JinaStage().extract(["https://a"])

    assert pages[0].get("error") and "503" in pages[0]["error"]


def test_deepseek_ignores_a_balance_it_cannot_read(use_transport, keys):
    keys(DEEPSEEK_API_KEY="k")
    use_transport(lambda request: httpx.Response(200, json={
        "is_available": True,
        "balance_infos": [{"total_balance": "not a number"}, {"total_balance": "3.5"}],
    }))

    quota = deepseek.DeepSeekStage().probe_quota()

    assert quota is not None, "one unreadable entry must not hide a readable one"


def test_keyless_reports_an_empty_reply(use_transport):
    use_transport(lambda request: httpx.Response(200, json={"result": {"content": []}}))

    outcome = keyless.KeylessStage().search("q", 3)

    assert not outcome.ok
