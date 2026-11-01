"""The slot check (tools/check_cache_slots.py): its verdicts on simulated servers, and its probes that need no GPU against the real server
with the scripted engine. The prefix-cache probes need the real model, so against the scripted engine they correctly report no reuse."""
import contextlib
import importlib.util
import io
import json
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location("check_cache_slots", ROOT / "tools" / "check_cache_slots.py")
cs = importlib.util.module_from_spec(spec)
sys.modules["check_cache_slots"] = cs
spec.loader.exec_module(cs)


class Cache:
    """A server's prefix caches by slot: a prompt is cached after it ran in that slot, until another prompt takes the slot."""

    def __init__(self, slots, evict_on_other=False):
        self.held, self.slots, self.evict = {}, slots, evict_on_other

    def run(self, slot, text, tokens):
        cached = tokens if self.held.get(slot) == text else 0
        self.held[slot] = text
        return cached


class FakeLlama:
    def __init__(self, slots=2, caches=True, listing=True):
        self.cache, self.slots, self.caches, self.listing = Cache(slots), slots, caches, listing

    def post(self, path, body):
        slot = body.get("id_slot", 0)
        if slot is not None and not 0 <= slot < self.slots:
            return 400, {"error": {"message": "bad slot"}}
        text, tokens = body["messages"][0]["content"], 1800
        cached = self.cache.run(slot, text, tokens) if self.caches else 0
        return 200, {"usage": {"prompt_tokens": tokens}, "timings": {"cache_n": cached}}

    def get(self, path):
        return (200, [{"id": i} for i in range(self.slots)]) if self.listing and path == "/slots" else (404, {})


class FakeBasalt:
    def __init__(self, side=True, caches=True):
        self.cache, self.side, self.caches = Cache(2), side, caches

    def post(self, path, body):
        kind = body.get("cache_slot")
        if kind not in (None, "main", "side", "side_only"):
            return 400, {"error": {"message": "cache_slot: ..."}}
        if kind == "side_only" and (not self.side or len(body["messages"][0]["content"]) > 20000):
            return 400, {"error": {"message": "this request does not fit the technical session"}}
        served = "side" if kind in ("side", "side_only") and self.side else "main"
        text, tokens = body["messages"][0]["content"], 1800
        cached = self.cache.run(served, text, tokens) if self.caches else 0
        return 200, {"usage": {"prompt_tokens": tokens, "prompt_tokens_details": {"cached_tokens": cached}}, "cache_slot": served}

    def get(self, path):
        return 200, {"concurrency": {"side_slot": {"slot": 1, "context": 30000} if self.side else None}}


def by_probe(results):
    return {r["probe"]: r["status"] for r in results}


def test_a_llama_server_that_keeps_each_slot_passes_every_probe():
    out = by_probe(cs.check("x", "llama", "m", slots=2, transport=FakeLlama(2)))
    assert out == {"accepts_other_key": "PASS", "bad_value": "INFO", "same_slot_reuse": "PASS", "other_slot_kept": "PASS", "side_session": "SKIP", "slots_listing": "PASS"}


def test_a_llama_server_without_a_prompt_cache_fails_the_reuse_probes_and_the_exit_code_says_so():
    results = cs.check("x", "llama", "m", slots=2, transport=FakeLlama(2, caches=False))
    out = by_probe(results)
    assert out["same_slot_reuse"] == "FAIL" and out["other_slot_kept"] == "FAIL"
    assert "0 of 1800" in next(r["detail"] for r in results if r["probe"] == "same_slot_reuse")


def test_one_slot_cannot_show_that_another_does_not_evict_it_and_a_missing_slot_list_is_recorded():
    out = by_probe(cs.check("x", "llama", "m", slots=1, transport=FakeLlama(1, listing=False)))
    assert out["other_slot_kept"] == "SKIP" and out["slots_listing"] == "INFO"


def test_a_slot_count_that_disagrees_with_the_listing_fails():
    assert by_probe(cs.check("x", "llama", "m", slots=4, transport=FakeLlama(2)))["slots_listing"] == "FAIL"


def test_a_basalt_server_with_a_session_serves_side_calls_there_and_refuses_oversized_strict_ones():
    results = cs.check("x", "basalt", "m", transport=FakeBasalt(side=True))
    assert by_probe(results) == {"accepts_other_key": "PASS", "bad_value": "PASS", "same_slot_reuse": "PASS", "other_slot_kept": "PASS",
                                 "side_session": "PASS", "slots_listing": "SKIP"}
    assert "served by the side slot" in next(r["detail"] for r in results if r["probe"] == "other_slot_kept")


def test_a_basalt_server_without_a_session_serves_side_calls_from_an_ordinary_slot():
    out = by_probe(cs.check("x", "basalt", "m", transport=FakeBasalt(side=False)))
    assert out["side_session"] == "PASS" and out["bad_value"] == "PASS"


def test_a_reply_that_says_nothing_about_the_cache_is_info_not_a_failure():
    class Silent(FakeLlama):
        def post(self, path, body):
            return 200, {"usage": {"prompt_tokens": 1800}}
    assert by_probe(cs.check("x", "llama", "m", slots=2, transport=Silent()))["same_slot_reuse"] == "INFO"


def test_both_ways_a_server_reports_cached_tokens_are_read():
    assert cs.cached_tokens({"usage": {"prompt_tokens_details": {"cached_tokens": 7}}}) == 7
    assert cs.cached_tokens({"timings": {"cache_n": 9}}) == 9
    assert cs.cached_tokens({}) is None and cs.cached_tokens(None) is None


def test_the_command_line_prints_the_report_and_exits_with_the_verdict(capsys, monkeypatch):
    monkeypatch.setattr(cs, "Transport", lambda base, key="": FakeLlama(2, caches=False))
    assert cs.main(["--base", "http://x", "--server", "llama", "--model", "m"]) == 1
    printed = capsys.readouterr().out
    assert "FAIL  same_slot_reuse" in printed and "PASS  accepts_other_key" in printed
    monkeypatch.setattr(cs, "Transport", lambda base, key="": FakeLlama(2))
    assert cs.main(["--base", "http://x", "--server", "llama", "--model", "m"]) == 0


@pytest.fixture
def real_server():
    from serve.engine.mock import MockEngine
    from serve.frontend import ChatTemplate
    from serve.handler import serve
    from serve.service import Service
    from serve.util.text import ByteTokenizer
    tok = ByteTokenizer()
    svc = Service(MockEngine(tok, "</think>\nok", max_context=65536), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
    with contextlib.redirect_stdout(io.StringIO()):
        httpd = serve(svc, port=0)
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_against_the_real_server_the_key_handling_probes_pass_and_the_scripted_engine_has_no_cache_to_show(real_server):
    with contextlib.redirect_stdout(io.StringIO()):
        results = cs.check(real_server, "basalt", "qwen3.8-flash-next", paragraphs=3)
    out = by_probe(results)
    assert out["accepts_other_key"] == "PASS"                     # id_slot (llama-server's key) is ignored, not an error
    assert out["bad_value"] == "PASS"                             # a bad cache_slot is a 400
    assert out["side_session"] == "PASS"                          # no session here: 'side' is served by an ordinary slot and 'side_only' is refused
    assert out["same_slot_reuse"] in ("FAIL", "INFO")             # the scripted engine keeps no prefix cache; the real model is what this probe is for
