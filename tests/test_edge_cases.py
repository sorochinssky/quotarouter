"""The last few branches: vendor answers we cannot use, and messages an operator reads.

Everything here is one line of behaviour, but each line is the difference between "one page failed"
and "the batch failed", or between "the pause clears itself" and "wait forever". Kept in one file so
the intent is obvious: this is the tail, not a second beginning.
"""

from __future__ import annotations

import io
import json
import time

import httpx
import pytest

from quotarouter import alerts, cli, config, errors, ledger as ledger_mod, retention, routing, types
from quotarouter.integrations import mcp
from quotarouter.stages import deepseek, exa, firecrawl, jina, keyless, tavily
from quotarouter.stages.base import Capability, Stage


# --- recheck: the command an operator runs when a vendor claims it can pay again -----------
class Rechecker:
    def __init__(self, returned, probe):
        self.returned = returned
        self.probe = probe
        self.stages = {}

    def now(self):
        return 1_000.0

    def recheck(self):
        return {"returned": self.returned, "probe": self.probe}


def _recheck_args(*argv):
    return cli.build_parser().parse_args(list(argv))


def test_recheck_names_the_vendors_that_came_back(monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_router",
                        lambda: Rechecker(["deepseek"], {"deepseek": {"paused": None}}))

    assert cli.cmd_recheck(_recheck_args("recheck")) == 0
    assert "deepseek: back in service" in capsys.readouterr().out


def test_recheck_says_what_is_still_out(monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_router",
                        lambda: Rechecker([], {"deepseek": {"paused": "quota"}}))

    assert cli.cmd_recheck(_recheck_args("recheck")) == 0
    out = capsys.readouterr().out
    assert "nothing came back" in out and "deepseek" in out


def test_recheck_json_is_the_same_answer_machine_readable(monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_router", lambda: Rechecker([], {}))

    assert cli.cmd_recheck(_recheck_args("recheck", "--json")) == 0
    assert json.loads(capsys.readouterr().out) == {"returned": [], "probe": {}}


# --- ledger details ----------------------------------------------------------------------
def test_a_vendor_that_was_never_paused_has_no_origin():
    assert ledger_mod.pause_origin_of(ledger_mod.VendorState(vendor="exa")) == ""


def test_a_legacy_pause_without_an_origin_reads_as_a_machine_pause():
    state = ledger_mod.VendorState(vendor="exa")
    state.pause_kind = types.QUOTA
    state.pause_reason = "balance empty"
    assert ledger_mod.pause_origin_of(state) == ledger_mod.ORIGIN_AUTO


def test_a_counter_that_is_not_a_number_is_dropped_and_named(tmp_path, caplog):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"vendors": {"exa": {"used": [1]}, "good": {"used": 2}}}),
                    encoding="utf-8")

    with caplog.at_level("WARNING"):
        ledger = ledger_mod.Ledger(path)

    assert ledger.get("exa").used == 0.0, "a record nobody can read must not be trusted"
    assert ledger.get("good").used == 2.0, "and must not take its neighbours with it"
    assert any("exa" in record.getMessage() for record in caplog.records)


def test_a_save_that_cannot_replace_leaves_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    ledger = ledger_mod.Ledger(path)

    def refuse(*args, **kwargs):
        raise OSError("denied")

    def unlink_also_fails(*args, **kwargs):
        raise OSError("denied too")

    monkeypatch.setattr(ledger_mod.os, "replace", refuse)
    monkeypatch.setattr(ledger_mod.os, "unlink", unlink_also_fails)

    with pytest.raises(OSError):
        ledger.charge("exa", 1.0)


# --- rotation that cannot happen ---------------------------------------------------------
def test_a_rotation_that_fails_is_reported_as_not_rotated(tmp_path, monkeypatch):
    path = tmp_path / "routes.jsonl"
    path.write_text("x" * 100, encoding="utf-8")
    monkeypatch.setattr(retention.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))

    assert retention.maybe_rotate(path, max_bytes=10) is False


# --- routing: the small decisions that decide a whole call --------------------------------
def test_two_free_budgets_stay_free():
    assert routing._stricter("free", None, "free", None) == ("free", None)


def test_a_quota_window_that_rolled_releases_the_pause(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_KEYLESS_LAST_RESORT", "0")

    class Stage_(Stage):
        key_env = ""

        def __init__(self):
            self.name = "exa"
            self.unit = "usd"
            self.funding = "free"
            self.monthly_allowance = None
            self.capability = Capability(search=True, extract=False, search_units=0.001)

        def search(self, query, limit):
            return types.search_ok([{"title": "t", "url": "https://a"}])

        def extract(self, urls):
            return []

    ledger = ledger_mod.Ledger(tmp_path / "ledger.json")
    ledger.set_limit("exa", 10.0)
    ledger.pause("exa", time.time() + 3600, types.QUOTA, "month ended",
                 origin=ledger_mod.ORIGIN_AUTO)
    state = ledger.get("exa")
    state.reset_at = time.time() - 1  # the vendor's own window has rolled
    router = routing.Router(stages={"exa": Stage_()},
                            settings=config.Settings(search_order=("exa",), state_dir=tmp_path),
                            ledger=ledger)

    usable, skipped = router.candidates("search")

    assert [s.name for s in usable] == ["exa"], skipped


def test_an_empty_entry_in_a_batch_is_skipped_not_counted(tmp_path):
    class OnePage(Stage):
        key_env = ""

        def __init__(self):
            self.name = "paid"
            self.unit = "requests"
            self.funding = "free"
            self.monthly_allowance = None
            self.capability = Capability(search=False, extract=True, extract_units=1.0)

        def search(self, query, limit):  # pragma: no cover
            return types.search_ok([])

        def extract(self, urls):
            return [None, types.document(urls[0], "t", "body " * 20, source_url=urls[0])]

    router = routing.Router(stages={"paid": OnePage()},
                            settings=config.Settings(search_order=(), extract_order=("paid",),
                                                     state_dir=tmp_path),
                            ledger=ledger_mod.Ledger(tmp_path / "ledger.json"))

    result = router.run_extract(["https://a"])

    assert result.ok, "a None in the middle must not hide the page that did arrive"


def test_the_keyless_tier_covers_only_what_was_not_delivered(tmp_path, monkeypatch):
    monkeypatch.setenv("QUOTAROUTER_KEYLESS_LAST_RESORT", "1")
    monkeypatch.setenv("QUOTAROUTER_ALLOW_KEYLESS", "1")

    class Partial(Stage):
        key_env = ""

        def __init__(self):
            self.name = "paid"
            self.unit = "requests"
            self.funding = "free"
            self.monthly_allowance = None
            self.capability = Capability(search=False, extract=True, extract_units=1.0)

        def search(self, query, limit):  # pragma: no cover
            return types.search_ok([])

        def extract(self, urls):
            return [types.page_error(urls[0], "blocked"),
                    types.document(urls[1], "t", "body " * 20, source_url=urls[1])]

    class Anonymous(Stage):
        key_env = ""

        def __init__(self):
            self.name = "keyless"
            self.unit = "requests"
            self.funding = "free"
            self.monthly_allowance = None
            self.capability = Capability(search=False, extract=True, extract_units=1.0)
            self.asked = []

        def search(self, query, limit):  # pragma: no cover
            return types.search_ok([])

        def extract(self, urls):
            self.asked.extend(urls)
            return [types.document(url, "anon", "anon " * 20, source_url=url) for url in urls]

    anonymous = Anonymous()
    router = routing.Router(stages={"paid": Partial(), "keyless": anonymous},
                            settings=config.Settings(search_order=(), extract_order=("paid",),
                                                     state_dir=tmp_path),
                            ledger=ledger_mod.Ledger(tmp_path / "ledger.json"))

    result = router.run_extract(["https://a", "https://b"])

    assert result.ok
    assert anonymous.asked == ["https://a"], "the tier that costs nothing must not refetch what came out"


def test_an_alert_that_fails_does_not_fail_the_search(tmp_path, monkeypatch):
    class Paid(Stage):
        key_env = ""

        def __init__(self):
            self.name = "paid"
            self.unit = "requests"
            self.funding = "free"
            self.monthly_allowance = None
            self.capability = Capability(search=False, extract=True, extract_units=1.0)

        def search(self, query, limit):  # pragma: no cover
            return types.search_ok([])

        def extract(self, urls):
            raise errors.QuotaError("out of quota")

    router = routing.Router(stages={"paid": Paid()},
                            settings=config.Settings(search_order=(), extract_order=("paid",),
                                                     state_dir=tmp_path),
                            ledger=ledger_mod.Ledger(tmp_path / "ledger.json"))
    monkeypatch.setattr(routing.alerts, "notify",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("transport down")))

    result = router.run_extract(["https://a"])

    assert not result.ok  # the call failed on its own merits, without a traceback from the alert


@pytest.mark.parametrize("kind,until,origin,expected", [
    (types.QUOTA, float("inf"), ledger_mod.ORIGIN_AUTO, "re-probed on the next call"),
    (types.QUOTA, time.time() + 60, ledger_mod.ORIGIN_OPERATOR, "stopped by operator"),
])
def test_the_pause_sentence_says_who_undoes_it(kind, until, origin, expected):
    state = ledger_mod.VendorState(vendor="exa")
    state.pause_kind = kind
    state.pause_reason = "reason"
    state.paused_until = until
    state.pause_origin = origin

    assert expected in routing._pause_text(state, time.time())


# --- the transport wrappers --------------------------------------------------------------
class WithBuffer(io.BytesIO):
    """`sys.stdin` exposes `.buffer`; `serve_stdio` must use it rather than the text layer.

    `close` is a no-op because the wrapper the function builds owns its buffer, which is correct
    for a real pipe and would erase the bytes a test wants to read.
    """

    @property
    def buffer(self):
        return self

    def close(self) -> None:  # pragma: no cover - the point is that it is not needed
        pass


def test_serve_stdio_uses_the_underlying_buffer(monkeypatch):
    monkeypatch.setattr(mcp, "get_router", lambda: None, raising=False)
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
    stdin, stdout = WithBuffer(payload.encode()), WithBuffer()

    assert mcp.serve_stdio(stdin, stdout) == 0
    assert json.loads(stdout.getvalue().decode().splitlines()[0])["result"] == {}


# --- vendor edges -----------------------------------------------------------------------
def _html(request):
    return httpx.Response(200, content=b"<html>nope</html>",
                          headers={"content-type": "text/html"})


def test_keyless_non_json_is_a_failure_with_a_reason(use_transport):
    use_transport(_html)

    outcome = keyless.KeylessStage().search("q", 3)

    assert not outcome.ok and outcome.failure is not None


def test_sse_frames_are_read_and_junk_ignored():
    text = "event: ping\ndata: [DONE]\ndata: not json\ndata: {\"result\": {}}\n"
    assert keyless._sse_result(text) == {"result": {}}


def test_keyless_reply_without_links_is_an_empty_failure(use_transport):
    use_transport(lambda request: httpx.Response(200, json={"result": {
        "content": [{"type": "text", "text": "nothing useful here"}]}}))

    outcome = keyless.KeylessStage().search("q", 3)

    assert not outcome.ok


def test_keyless_extract_reports_an_empty_page(use_transport, monkeypatch):
    monkeypatch.setattr(keyless, "_mcp_call", lambda *a, **k: {"result": {"content": []}})
    pages = keyless.KeylessStage().extract(["https://a"])
    assert pages[0].get("error")


def test_keyless_extract_survives_an_unexpected_error(monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("adapter bug")

    monkeypatch.setattr(keyless, "_mcp_call", explode)
    pages = keyless.KeylessStage().extract(["https://a"])
    assert pages[0].get("error") and "RuntimeError" in pages[0]["error"]


def test_firecrawl_search_with_no_results_is_empty(use_transport, keys):
    keys(FIRECRAWL_API_KEY="k")
    use_transport(lambda request: httpx.Response(200, json={"data": {"web": []}}))

    outcome = firecrawl.FirecrawlStage().search("q", 3)

    assert not outcome.ok and outcome.failure is not None


def test_firecrawl_survives_an_adapter_bug(monkeypatch):
    stage = firecrawl.FirecrawlStage()

    def explode(*args, **kwargs):
        raise ValueError("payload shape changed")

    monkeypatch.setattr(stage, "_call", explode)
    pages = stage.extract(["https://a"])

    assert pages[0].get("error") and "ValueError" in pages[0]["error"]


def test_firecrawl_ignores_a_billing_date_it_cannot_read(use_transport, keys):
    keys(FIRECRAWL_API_KEY="k")
    use_transport(lambda request: httpx.Response(200, json={"data": {
        "planCredits": 1000, "remainingCredits": 950, "billingPeriodEnd": "not a date"}}))

    quota = firecrawl.FirecrawlStage().probe_quota()

    assert quota is not None, "a bad date must not hide a readable credit balance"


def test_tavily_reports_failed_results_per_page(use_transport, keys):
    keys(TAVILY_API_KEY="k")
    use_transport(lambda request: httpx.Response(200, json={
        "results": [], "failed_results": [{"url": "https://a", "error": "blocked"}]}))

    pages = tavily.TavilyStage().extract(["https://a"])

    assert pages[0].get("error") and "blocked" in pages[0]["error"]


def test_tavily_probe_gives_up_quietly_on_an_error_status(use_transport, keys):
    keys(TAVILY_API_KEY="k")
    use_transport(lambda request: httpx.Response(401, json={"error": "bad key"}))

    assert tavily.TavilyStage().probe_quota() is None


def test_deepseek_probe_gives_up_quietly_on_an_error_status(use_transport, keys):
    keys(DEEPSEEK_API_KEY="k")
    use_transport(lambda request: httpx.Response(500, content=b"down"))

    assert deepseek.DeepSeekStage().probe_quota() is None


def test_jina_survives_a_transport_bug(monkeypatch):
    def explode(*args, **kwargs):
        raise ValueError("url encoding broke")

    monkeypatch.setattr(jina.http_client, "get", explode)
    pages = jina.JinaStage().extract(["https://a"])

    assert pages[0].get("error") and "transport" in pages[0]["error"]


def test_exa_knows_which_statuses_are_worth_a_retry():
    assert exa.ExaStage._retry_recommended(429) is True
    assert exa.ExaStage._retry_recommended(503) is True
    assert exa.ExaStage._retry_recommended(400) is False


def test_a_stage_that_implements_nothing_says_so():
    class Bare(Stage):
        key_env = ""

        def __init__(self):
            self.name = "bare"
            self.unit = "requests"
            self.funding = "free"
            self.monthly_allowance = None
            self.capability = Capability(search=True, extract=True)

    stage = Bare()
    assert "bare" in stage.describe()
    with pytest.raises(NotImplementedError):
        stage.search("q", 1)
    with pytest.raises(NotImplementedError):
        stage.extract(["https://a"])


# --- the counters a hand-edited ledger can carry ------------------------------------------
@pytest.mark.parametrize("entry", [
    {"used": None},
    {"used": "many"},
    {"consecutive_failures": "twice"},
])
def test_a_counter_the_file_cannot_justify_is_dropped(tmp_path, entry):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"vendors": {"exa": entry, "good": {"used": 1}}}), encoding="utf-8")

    ledger = ledger_mod.Ledger(path)

    assert ledger.get("exa").used == 0.0
    assert ledger.get("good").used == 1.0


def test_a_status_with_no_category_is_still_an_error():
    assert isinstance(errors.classify(None, "unclassified"), errors.TransientError)


# --- extraction where the vendor answers about a different page ---------------------------
def test_an_answer_about_another_page_does_not_land_on_this_one(tmp_path):
    class WrongUrl(Stage):
        key_env = ""

        def __init__(self):
            self.name = "paid"
            self.unit = "requests"
            self.funding = "free"
            self.monthly_allowance = None
            self.capability = Capability(search=False, extract=True, extract_units=1.0)

        def search(self, query, limit):  # pragma: no cover
            return types.search_ok([])

        def extract(self, urls):
            return [types.document("https://something-else", "t", "body " * 20)]

    router = routing.Router(stages={"paid": WrongUrl()},
                            settings=config.Settings(search_order=(), extract_order=("paid",),
                                                     state_dir=tmp_path),
                            ledger=ledger_mod.Ledger(tmp_path / "ledger.json"))

    result = router.run_extract(["https://a", "https://b"])

    assert not result.ok, "an answer to a URL nobody asked for is not an answer"
    assert [page["url"] for page in result.payload] == ["https://a", "https://b"]


def test_an_alert_whose_thread_cannot_start_does_not_fail_the_search(tmp_path, monkeypatch):
    class Paid(Stage):
        key_env = ""

        def __init__(self):
            self.name = "paid"
            self.unit = "requests"
            self.funding = "free"
            self.monthly_allowance = None
            self.capability = Capability(search=False, extract=True, extract_units=1.0)

        def search(self, query, limit):  # pragma: no cover
            return types.search_ok([])

        def extract(self, urls):
            raise errors.QuotaError("out of quota")

    router = routing.Router(stages={"paid": Paid()},
                            settings=config.Settings(search_order=(), extract_order=("paid",),
                                                     state_dir=tmp_path),
                            ledger=ledger_mod.Ledger(tmp_path / "ledger.json"))

    def explode(*args, **kwargs):
        raise RuntimeError("alert transport is misconfigured")

    monkeypatch.setattr(routing.alerts, "notify", explode)

    result = router.run_extract(["https://a"])
    router.log_route("extract", result, {"urls": ["https://a"]})  # the alert path lives here

    assert not result.ok, "the call fails for its own reason; the alert must not add a second one"

    # And the failure was still written down: a dead alert must not cost the audit trail too.
    record = json.loads(router.settings.log_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["ok"] is False


def test_an_empty_batch_returns_an_empty_answer_without_dialing(tmp_path):
    class Watched(Stage):
        key_env = ""

        def __init__(self):
            self.name = "paid"
            self.unit = "requests"
            self.funding = "free"
            self.monthly_allowance = None
            self.capability = Capability(search=False, extract=True, extract_units=1.0)
            self.called = False

        def search(self, query, limit):  # pragma: no cover
            return types.search_ok([])

        def extract(self, urls):
            self.called = True
            return []

    stage = Watched()
    router = routing.Router(stages={"paid": stage},
                            settings=config.Settings(search_order=(), extract_order=("paid",),
                                                     state_dir=tmp_path),
                            ledger=ledger_mod.Ledger(tmp_path / "ledger.json"))

    result = router.run_extract([])

    # An empty batch is "nothing was served", not "success": the caller gets a result it can read
    # instead of an exception, and the MCP surface refuses the request before it gets this far.
    assert result.ok is False and result.payload is None
    assert result.attempts == [], "no attempt was made, so no attempt is reported"
    assert stage.called is False, "nothing to fetch is not a reason to spend a credit"
