"""The Providers page's routes: functions and providers, keys write-only, what is refused, and the server's own page only."""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness.llm.credentials import FileCredentials
from harness.llm.store import Ledger, SecureRegistry
from serve import handler
from serve.llm_api import LlmApi
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)


class Api:
    def __init__(self, url):
        self.url = url

    def __call__(self, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")


@pytest.fixture
def api(tmp_path, monkeypatch, start_server):  # noqa: F811
    reg = SecureRegistry(tmp_path / "p.json", credentials=FileCredentials(tmp_path / "c.json"))
    reg.local_hosts = ()
    monkeypatch.setattr(handler, "_LLM", LlmApi(reg, Ledger(tmp_path / "u.json")))
    return Api(start_server([answer("x")])), reg


def test_the_page_sees_every_function_and_no_key(api):
    call, reg = api
    code, body = call("POST", "/api/llm/providers", {"type": "openai", "api_key": "sk-secret-123456", "model": "gpt-x", "daily_token_cap": 5000})
    assert code == 200 and body["provider"]["has_key"] is True and body["provider"]["daily_token_cap"] == 5000 and "api_key" not in body["provider"]
    code, view = call("GET", "/api/llm")
    assert code == 200 and "sk-secret" not in json.dumps(view)
    assert {f["id"] for f in view["functions"]} >= {"chat", "executor", "router", "triage"}
    assert next(f for f in view["functions"] if f["id"] == "executor")["provider"] == "" and view["providers"][0]["is_local"] is False
    assert {t["type"] for t in view["types"]} >= {"basalt", "jev", "openai", "claude"}


def test_a_function_is_pointed_at_a_provider_only_if_the_rules_allow_it(api):
    call, reg = api
    pid = call("POST", "/api/llm/providers", {"type": "openai", "api_key": "k" * 12, "model": "m"})[1]["provider"]["id"]
    code, body = call("POST", "/api/llm/assign", {"function": "executor", "provider": pid})
    assert code == 409 and body["error"]["code"] == "not_allowed" and "say yes" in body["error"]["message"]
    assert call("POST", "/api/llm/consent", {"function": "executor", "allowed": True})[0] == 200
    code, view = call("POST", "/api/llm/assign", {"function": "executor", "provider": pid})
    assert code == 200 and next(f for f in view["functions"] if f["id"] == "executor")["provider"] == pid
    code, view = call("POST", "/api/llm/consent", {"function": "executor", "allowed": False})        # taking the yes back takes the function off it
    assert next(f for f in view["functions"] if f["id"] == "executor")["provider"] == ""
    assert call("POST", "/api/llm/assign", {"function": "nope", "provider": pid})[0] == 400
    assert call("POST", "/api/llm/assign", {"function": "relevance", "provider": "ghost"})[0] == 409


def test_a_key_is_changed_only_when_a_new_one_is_sent(api):
    call, reg = api
    pid = call("POST", "/api/llm/providers", {"type": "openai", "api_key": "sk-first-123456", "model": "m"})[1]["provider"]["id"]
    call("POST", f"/api/llm/providers/{pid}", {"name": "My OpenAI", "api_key": ""})              # an empty field leaves the key alone
    assert reg.load()["providers"][0]["api_key"] == "sk-first-123456" and reg.load()["providers"][0]["name"] == "My OpenAI"
    call("POST", f"/api/llm/providers/{pid}", {"api_key": "sk-second-123456", "daily_token_cap": 0})
    assert reg.load()["providers"][0]["api_key"] == "sk-second-123456"
    assert call("POST", f"/api/llm/providers/{pid}/remove", {})[0] == 200 and call("GET", "/api/llm")[1]["providers"] == []
    assert call("POST", f"/api/llm/providers/{pid}/remove", {})[0] == 409
    assert call("POST", "/api/llm/providers", {"type": "not_a_type"})[1]["error"]["code"] == "bad_type"
    assert call("POST", "/api/llm/providers", {"type": "openai", "daily_token_cap": "many"})[1]["error"]["code"] == "bad_cap"


def test_a_provider_can_be_tried_and_this_server_always_answers(api):
    call, reg = api
    this = call("POST", "/api/llm/providers", {"type": "basalt"})[1]["provider"]["id"]
    assert call("POST", f"/api/llm/providers/{this}/test", {})[1]["ok"] is True
    jev = call("POST", "/api/llm/providers", {"type": "jev", "api_key": "k" * 12})[1]["provider"]["id"]
    assert call("POST", f"/api/llm/providers/{jev}/test", {})[1]["ok"] is True
    nokey = call("POST", "/api/llm/providers", {"type": "jev"})[1]["provider"]["id"]
    assert call("POST", f"/api/llm/providers/{nokey}/test", {})[1]["ok"] is False


def test_only_this_pc_and_the_pages_own_origin_can_change_anything(api):
    call, reg = api
    code, body = call("POST", "/api/llm/providers", {"type": "openai"}, headers={"Origin": "http://evil.example"})
    assert code == 403 and reg.load()["providers"] == []
    assert call("POST", "/api/llm/nothing", {})[0] == 404


def test_the_catalog_of_context_patterns_is_served_and_a_preset_keeps_a_checked_choice(api):
    call, reg = api
    code, cat = call("GET", "/api/context/patterns")
    assert code == 200 and cat["order"][0] == "injection" and "mask_old" in cat["patterns"]["pruning"]["strategies"] and cat["defaults"]["pruning"]["on"] is True
    from serve import profiles
    ok = profiles.check_inference({"policy": "stop", "context_patterns": {"summary": {"on": True, "strategy": "incremental"}}})
    assert ok["context_patterns"]["summary"] == {"on": True, "strategy": "incremental"} and ok["context_patterns"]["pruning"]["strategy"] == "mask_old"
    with pytest.raises(ValueError):
        profiles.check_inference({"context_patterns": {"summary": {"strategy": "poetry"}}})
