"""The log of the tokens every model call used: what a line holds, what a period reports, and that nothing of a message is kept."""
import json

from tadamoo.llm.usage import UsageLog, counts


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def test_the_counts_of_an_answer_are_read_in_the_openai_shape_and_a_missing_one_is_zero():
    assert counts({"usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 60}}}) == {"prompt": 100, "completion": 20, "cached": 60}
    for odd in (None, {}, {"usage": None}, {"usage": {"prompt_tokens": True, "completion_tokens": "7"}}, {"usage": {"prompt_tokens": -3}}):
        assert counts(odd) == {"prompt": 0, "completion": 0, "cached": 0}


def test_a_call_is_one_line_with_no_text_of_the_messages_and_a_call_without_tokens_is_none(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl", clock=Clock(1000.0))
    log.record("p1", "Cloud", "glm-x", "chat", 100, 20, 60)
    log.record("p1", "Cloud", "glm-x", "chat", 0, 0)
    log.record("local", "Local engine", "qwen", "", 50, 10, 500, api="/v1/messages")           # cached cannot exceed the prompt
    lines = [json.loads(x) for x in (tmp_path / "u.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2 and lines[0] == {"t": 1000.0, "provider": "p1", "name": "Cloud", "model": "glm-x", "function": "chat", "prompt": 100, "completion": 20, "cached": 60}
    assert lines[1]["cached"] == 50 and lines[1]["api"] == "/v1/messages"


def test_a_period_reports_totals_each_provider_model_and_function_and_the_bars(tmp_path):
    clock = Clock(10_000.0)
    log = UsageLog(tmp_path / "u.jsonl", clock=clock)
    for t, args in ((1_000.0, ("p1", "Cloud", "glm-x", "chat", 100, 20, 10)), (8_000.0, ("p1", "Cloud", "glm-x", "executor", 300, 50, 0)),
                    (9_900.0, ("local", "Local engine", "qwen", "chat", 40, 10, 5)), (9_950.0, ("local", "Local engine", "qwen", "voice", 60, 0, 0))):
        clock.now = t
        log.record(*args)
    clock.now = 10_000.0
    everything = log.report()
    assert everything["totals"] == {"calls": 4, "prompt": 500, "completion": 80, "cached": 15, "total": 580}
    assert [(p["id"], p["calls"], p["total"]) for p in everything["providers"]] == [("p1", 2, 470), ("local", 2, 110)]
    assert {(m["model"], m["total"]) for m in everything["models"]} == {("glm-x", 470), ("qwen", 110)}
    assert {(f["function"], f["total"]) for f in everything["functions"]} == {("chat", 170), ("executor", 350), ("voice", 60)}
    assert everything["first"] == 1_000.0 and sum(sum(b["by_provider"].values()) for b in everything["bars"]) == 580
    recent = log.report(start=9_000.0, end=10_000.0)                                           # the last part of the period: two calls
    assert recent["totals"]["calls"] == 2 and [p["id"] for p in recent["providers"]] == ["local"]
    assert recent["from"] == 9_000.0 and recent["to"] == 10_000.0 and recent["bar_s"] == 60 and len(recent["bars"]) <= 60
    assert log.report(start=20_000.0, end=30_000.0)["totals"]["calls"] == 0                    # a period with nothing in it


def test_the_bar_width_grows_with_the_period_and_never_leaves_more_than_sixty_bars(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl", clock=Clock(10_000_000.0))
    for hours, width in ((1, 60), (12, 900), (24, 3600), (24 * 7, 3 * 3600), (24 * 30, 86400)):
        report = log.report(start=10_000_000.0 - hours * 3600, end=10_000_000.0)
        assert report["bar_s"] == width and (hours * 3600) / report["bar_s"] <= 60, (hours, report["bar_s"])


def test_a_damaged_line_is_skipped_and_a_missing_file_is_an_empty_report(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl", clock=Clock(500.0))
    assert log.report()["totals"]["calls"] == 0
    (tmp_path / "u.jsonl").write_text('{not json\n["x"]\n{"t": "late"}\n' + json.dumps({"t": 400, "provider": "p", "name": "P", "model": "m", "function": "f", "prompt": 3, "completion": 4, "cached": 0}) + "\n", encoding="utf-8")
    assert log.report()["totals"]["total"] == 7


# ---- where a call is written -------------------------------------------------------------------------------------

import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402
from test_llm_client import MSG, Cloud, Local  # noqa: E402,F401

from tadamoo.llm.client import RoutedClient  # noqa: E402
from tadamoo.llm.credentials import FileCredentials  # noqa: E402
from tadamoo.llm.store import Ledger, SecureRegistry  # noqa: E402


@pytest.fixture
def world(tmp_path):
    cloud, local = Cloud(), Local()
    reg = SecureRegistry(tmp_path / "p.json", credentials=FileCredentials(tmp_path / "c.json"))
    reg.local_hosts = ()
    yield cloud, local, reg, Ledger(tmp_path / "l.json"), UsageLog(tmp_path / "usage.jsonl")
    cloud.close()


def test_a_provider_answer_is_written_with_who_the_model_the_function_and_the_counts(world):
    cloud, local, reg, ledger, log = world
    pid = reg.add({"type": "openai_compat", "name": "Cloud", "base_url": cloud.url + "/v1", "api_key": "sk-test-abcdef", "model": "gpt-test"})["id"]
    reg.set_consent("relevance", True)
    reg.assign_checked("relevance", pid)
    RoutedClient("relevance", reg, local, ledger, usage=log).chat_completion(MSG)
    rows = list(log.rows())
    assert len(rows) == 1 and (rows[0]["provider"], rows[0]["name"], rows[0]["model"], rows[0]["function"]) == (pid, "Cloud", "gpt-test", "relevance")
    assert (rows[0]["prompt"], rows[0]["completion"]) == (5, 3)


def test_a_streamed_answer_and_an_anthropic_provider_are_written_too(world):
    cloud, local, reg, ledger, log = world
    pid = reg.add({"type": "openai_compat", "name": "Cloud", "base_url": cloud.url + "/v1", "api_key": "sk-test-abcdef", "model": "gpt-test"})["id"]
    claude = reg.add({"type": "anthropic_compat", "name": "Claude", "base_url": cloud.url + "/v1", "api_key": "sk-ant-abcdef", "model": "claude-test"})["id"]
    reg.set_consent("relevance", True)
    reg.set_consent("memory", True)
    reg.assign_checked("relevance", pid)
    reg.assign_checked("memory", claude)
    RoutedClient("relevance", reg, local, ledger, usage=log).chat_completion(MSG, on_delta=lambda kind, text: None)
    RoutedClient("memory", reg, local, ledger, usage=log).chat_completion(MSG)
    rows = {r["name"]: r for r in log.rows()}
    assert (rows["Cloud"]["prompt"], rows["Cloud"]["completion"]) == (5, 3) and (rows["Claude"]["prompt"], rows["Claude"]["completion"]) == (11, 7)


def test_the_loaded_model_called_through_a_client_is_not_written_there_and_a_client_without_a_log_writes_nothing(world, tmp_path):
    cloud, local, reg, ledger, log = world
    RoutedClient("relevance", reg, local, ledger, usage=log).chat_completion(MSG)                    # the engine's own count is written by the server, once
    assert list(log.rows()) == []
    pid = reg.add({"type": "openai_compat", "name": "Cloud", "base_url": cloud.url + "/v1", "api_key": "sk-test-abcdef", "model": "gpt-test"})["id"]
    reg.set_consent("relevance", True)
    reg.assign_checked("relevance", pid)
    RoutedClient("relevance", reg, local, ledger).chat_completion(MSG)
    assert list(log.rows()) == []


def test_the_server_writes_the_local_engines_calls_and_serves_the_period_and_the_page_is_wired(tmp_path, monkeypatch):
    import re
    import urllib.request

    from serve.tests.test_vram_api import Server

    class T(Server):
        def runTest(self):
            pass

    case = T()
    case.setUp()
    try:
        case.svc.usage_log = UsageLog()                                                              # in the data folder the test server uses
        code, _ = case.req("POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "ciao"}], "max_tokens": 5}, {"X-Tadamoo-Function": "chat"})
        assert code == 200
        code, report = case.req("GET", "/api/usage")
        assert code == 200 and report["totals"]["calls"] == 1 and report["providers"][0]["id"] == "local" and report["functions"][0]["function"] == "chat"
        assert report["models"][0]["model"] and report["totals"]["prompt"] > 0 and report["totals"]["total"] == report["totals"]["prompt"] + report["totals"]["completion"]
        code, empty = case.req("GET", f"/api/usage?from=1&to=2")
        assert code == 200 and empty["totals"]["calls"] == 0
        code, bad = case.req("GET", "/api/usage?from=abc")
        assert code == 200 and bad["totals"]["calls"] == 1                                            # a value that is not a number is no filter
    finally:
        case.tearDown()
    web = Path(__file__).resolve().parents[1] / "serve" / "web"
    i18n = (web / "i18n.js").read_text(encoding="utf-8")
    source = (web / "usage.js").read_text(encoding="utf-8")
    used = {k for k in re.findall(r'T\("(us\.[a-z0-9_.]+)"', source) if not k.endswith(".")} | {"us.sub", "us.title"} | {"us.p." + p for p in ("1h", "12h", "1d", "1w", "1m", "all")}
    assert len(used) > 20
    for key in used:
        assert i18n.count(f'"{key}":') == 2, key
    html = (web / "index.html").read_text(encoding="utf-8")
    assert 'src="web/usage.js"' in html and 'id="ld-usage"' in html and 'id="sub-usage"' in html and 'href="web/usage.css"' in html
    assert "innerHTML" not in source
