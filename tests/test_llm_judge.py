"""Judges: the loaded model, another model, or Jev, with the guards that keep the person's words on this PC."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.llm.credentials import FileCredentials
from harness.llm.judge import Judge, router_judge
from harness.llm.store import Ledger, SecureRegistry
from harness.router import Router


class Words:
    """The loaded model: answers the one word it is told to."""

    def __init__(self, word="steer"):
        self.word, self.asked = word, []

    def chat_completion(self, **kw):
        self.asked.append(kw)
        return {"choices": [{"message": {"role": "assistant", "content": self.word}, "finish_reason": "stop"}], "usage": {}}


class Jev:
    """A service that answers typed questions: records what it was sent."""

    def __init__(self, choice="queue", status=200):
        self.requests, self.choice, self.status = [], choice, status
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                raw = json.dumps({"answers": {"answer": {"type": "choice", "choice": outer.choice}}, "usage": {"input_tokens": 40, "output_tokens": 2}}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


OPTIONS = {"steer": "about the task", "queue": "can wait", "urgent": "cannot wait"}


@pytest.fixture
def world(tmp_path):
    jev = Jev()
    reg = SecureRegistry(tmp_path / "p.json", credentials=FileCredentials(tmp_path / "c.json"))
    reg.local_hosts = ()
    # an https address is required for Jev: the test registers one on a loopback port and the guard is lifted for it below
    yield jev, reg, Ledger(tmp_path / "u.json"), Words()
    jev.close()


def add_jev(reg, jev, https=True):
    pid = reg.add({"type": "jev", "api_key": "jev-key-1234567", "base_url": jev.url})["id"]
    return pid


def test_the_loaded_model_judges_with_one_word_and_the_options_are_in_the_prompt(world):
    jev, reg, ledger, words = world
    words.word = "Urgent."
    judge = Judge("router", reg, words, ledger)
    assert judge.choose("Where does it belong?", OPTIONS, "Task: cena. New message: la caldaia perde") == "urgent"
    assert judge.last_source == "model"
    system = words.asked[0]["messages"][0]["content"]
    assert "steer - about the task" in system and "urgent - cannot wait" in system and words.asked[0]["max_tokens"] <= 8


def test_a_word_that_is_not_an_option_is_no_answer(world):
    jev, reg, ledger, words = world
    words.word = "maybe"
    judge = Judge("router", reg, words, ledger)
    assert judge.choose("q", OPTIONS, "x") is None and judge.last_source == ""


def test_jev_gets_a_short_excerpt_a_typed_choice_and_the_key_and_is_counted(world, monkeypatch):
    jev, reg, ledger, words = world
    pid = add_jev(reg, jev)
    reg.set_consent("router", True)
    reg.assign_checked("router", pid)
    from harness.llm import judge as judge_mod
    monkeypatch.setattr(judge_mod.Judge, "_jev", _allow_http(judge_mod.Judge._jev))        # the fake listens on http, loopback
    judge = Judge("router", reg, words, ledger)
    assert judge.choose("Where does it belong?", OPTIONS, "A" * 5000) == "queue"
    req = jev.requests[0]
    assert req["path"] == "/systemone" and req["auth"] == "Bearer jev-key-1234567"
    q = req["body"]["questions"]["answer"]
    assert q["type"] == "choice" and q["criteria"] == OPTIONS and len(req["body"]["state"]["excerpt"]) == 1500       # an excerpt, not the whole
    assert judge.last_source == "jev" and words.asked == [] and ledger.used(pid) == 42


def _allow_http(original):
    """The service must be reached over https; the test's fake is on plain http, so the scheme check is the one thing relaxed."""
    def run(self, prov, question, options, context, timeout):
        prov = dict(prov, base_url=prov["base_url"].replace("http://", "https://", 1))
        self.http = _Rewrite(self.http)
        return original(self, prov, question, options, context, timeout)
    return run


class _Rewrite:
    def __init__(self, inner):
        self.inner = inner

    def post(self, url, **kw):
        return self.inner.post(url.replace("https://", "http://", 1), **kw)


def test_jev_is_not_asked_without_the_yes_for_personal_data_and_the_loaded_model_judges_instead(world):
    jev, reg, ledger, words = world
    pid = add_jev(reg, jev)
    data = reg.load()
    data["assignments"]["router"] = pid                    # assigned past the checks: the judge checks again
    reg.save(data)
    words.word = "steer"
    judge = Judge("router", reg, words, ledger)
    assert judge.choose("q", OPTIONS, "the person's plan has an allergy") == "steer"
    assert jev.requests == [] and judge.last_source == "model"


def test_jev_that_fails_or_is_over_its_cap_hands_over_to_the_loaded_model(world, monkeypatch):
    jev, reg, ledger, words = world
    pid = add_jev(reg, jev)
    reg.set_consent("router", True)
    reg.assign_checked("router", pid)
    from harness.llm import judge as judge_mod
    monkeypatch.setattr(judge_mod.Judge, "_jev", _allow_http(judge_mod.Judge._jev))
    jev.status = 500
    judge = Judge("router", reg, words, ledger)
    assert judge.choose("q", OPTIONS, "x") == "steer" and judge.last_source == "model"
    jev.status = 200
    reg.set_cap(pid, 10)
    ledger.record(pid, 10)
    n = len(jev.requests)
    assert judge.choose("q", OPTIONS, "x") == "steer" and len(jev.requests) == n          # over its cap: not even asked


def test_jev_over_plain_http_is_never_used(world):
    jev, reg, ledger, words = world
    pid = add_jev(reg, jev)
    reg.set_consent("router", True)
    reg.assign_checked("router", pid)
    judge = Judge("router", reg, words, ledger)
    assert judge.choose("q", OPTIONS, "x") == "steer" and jev.requests == []                # http: the key stays here


def test_only_a_judging_function_can_have_a_judge(world):
    jev, reg, ledger, words = world
    with pytest.raises(ValueError):
        Judge("executor", reg, words, ledger)


def test_the_router_takes_its_judge_from_here(world):
    jev, reg, ledger, words = world
    words.word = "queue"
    router = Router(router_judge(Judge("router", reg, words, ledger)))
    assert router.route("controlla le mail", {"task": "cena", "progress": "ho letto il piano"}) == ("queue", "judge")
    assert "Task in hand: cena" in words.asked[0]["messages"][1]["content"]
