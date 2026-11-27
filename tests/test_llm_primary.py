"""The Primary model, the fall-back when no model is loaded, and the provider form's list of models and test request."""
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from serve.llm_api import LlmApi, LlmError
from tadamoo.client import TadamooError
from tadamoo.llm import client as llm_client
from tadamoo.llm.client import RoutedClient
from tadamoo.llm.credentials import FileCredentials
from tadamoo.llm.store import PRIMARY, Ledger, SecureRegistry
from test_llm_client import MSG, Cloud, Local


class Loader:
    """What `QwenLoader` says about the local engine: loaded or not."""

    def __init__(self, loaded):
        self.loaded, self.ensured = loaded, 0

    def status(self):
        return {"loaded": self.loaded, "loading": False}

    def ensure(self, route=None):
        self.ensured += 1
        if not self.loaded:
            raise TadamooError(409, "the model did not finish loading in time")
        return "loaded"


class Listing:
    """A provider's address on this PC: it lists models like OpenAI or Anthropic and answers one minimal chat request."""

    def __init__(self):
        self.seen, self.fail = [], 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                outer.seen.append(("GET", self.path, {k.lower(): v for k, v in self.headers.items()}, None))
                if outer.fail:
                    return self._send(outer.fail, {"error": {"message": "bad key sk-secret-123456"}})
                if self.path.startswith("/anthropic/v1/models"):
                    return self._send(200, {"data": [{"id": "claude-a", "display_name": "Claude A"}]})
                self._send(200, {"data": [{"id": "model-b"}, {"id": "model-a"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.seen.append(("POST", self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                if outer.fail:
                    return self._send(outer.fail, {"error": {"message": "model not found"}})
                self._send(200, {"choices": [{"message": {"content": "OK"}}], "content": [{"type": "text", "text": "OK"}]})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def world(tmp_path):
    cloud, local = Cloud(), Local()
    reg = SecureRegistry(tmp_path / "p.json", credentials=FileCredentials(tmp_path / "c.json"))
    reg.local_hosts = ()
    yield cloud, local, reg, Ledger(tmp_path / "u.json")
    cloud.close()


@pytest.fixture
def listing():
    fake = Listing()
    yield fake
    fake.close()


def provider(reg, cloud, name, model="m"):
    pid = reg.add({"type": "openai_compat", "name": name, "base_url": cloud.url + "/v1", "api_key": "k" * 12, "model": model})["id"]
    reg.set_consent("relevance", True)
    return pid


# -- the Primary model ---------------------------------------------------------------------------------------

def test_a_function_on_the_primary_model_follows_it_at_the_moment_of_the_call(world):
    cloud, local, reg, ledger = world
    pid = provider(reg, cloud, "Subscription")
    reg.assign_checked("relevance", PRIMARY)
    client = RoutedClient("relevance", reg, local, ledger)
    client.chat_completion(MSG)
    assert len(local.calls) == 1 and cloud.requests == []                    # the Primary model is the local engine: it answers
    reg.set_primary(pid)                                                      # nothing else changes
    out = client.chat_completion(MSG)
    assert out["choices"][0]["message"]["content"] == "Ciao dal cloud" and len(local.calls) == 1 and len(cloud.requests) == 1
    reg.set_primary("")
    client.chat_completion(MSG)
    assert len(local.calls) == 2 and len(cloud.requests) == 1
    assert reg.chosen("relevance") == PRIMARY                                 # the choice itself was never rewritten


def test_a_function_on_the_primary_model_obeys_the_rules_of_the_provider_it_follows(world):
    cloud, local, reg, ledger = world
    pid = reg.add({"type": "openai_compat", "base_url": cloud.url + "/v1", "api_key": "k" * 12, "model": "m"})["id"]
    reg.assign_checked("executor", PRIMARY)
    reg.set_primary(pid)
    ok, why = reg.check("executor", PRIMARY)
    assert not ok and "say yes" in why                                        # personal data, outside this PC, no yes for this function
    RoutedClient("executor", reg, local, ledger).chat_completion(MSG)
    assert cloud.requests == [] and len(local.calls) == 1                     # as with any refused route: the local engine answers
    reg.set_consent("executor", True)
    RoutedClient("executor", reg, local, ledger).chat_completion(MSG)
    assert len(cloud.requests) == 1


def test_removing_the_primary_provider_puts_the_primary_model_back_on_the_local_engine(world):
    cloud, local, reg, ledger = world
    pid = provider(reg, cloud, "Gone soon")
    reg.set_primary(pid)
    assert reg.primary() == pid
    reg.remove(pid)
    assert reg.primary() == ""
    with pytest.raises(KeyError):
        reg.set_primary("ghost")


def test_new_functions_can_default_to_the_primary_model_and_the_local_engine_stays_a_choice(world):
    cloud, local, reg, ledger = world
    pid = provider(reg, cloud, "Subscription")
    reg.set_primary(pid, default_for_new=True)
    assert reg.load()["assignments"].get("relevance") == pid                  # no choice made: follows
    reg.assign_checked("relevance", None)                                     # "From Local Engine" chosen by hand
    assert reg.load()["assignments"].get("relevance") is None and reg.chosen("relevance") == ""
    reg.set_primary(pid, default_for_new=False)
    assert reg.load()["assignments"].get("chat") is None                      # off again: no choice is the local engine


# -- the fall-back -------------------------------------------------------------------------------------------

def test_with_no_model_loaded_a_function_on_the_local_engine_goes_to_the_primary_provider_and_says_so(world, caplog):
    cloud, local, reg, ledger = world
    provider(reg, cloud, "First")
    second = provider(reg, cloud, "Second")
    reg.set_primary(second)
    loader = Loader(loaded=False)
    llm_client._told.clear()
    with caplog.at_level(logging.INFO, logger="tadamoo.llm"):
        for _ in range(2):
            out = RoutedClient("relevance", reg, local, ledger, loader=loader).chat_completion(MSG)
    assert out["choices"][0]["message"]["content"] == "Ciao dal cloud" and local.calls == [] and loader.ensured == 0
    lines = [r.getMessage() for r in caplog.records if "fell back" in r.getMessage()]
    assert lines == ["llm: relevance fell back to Second (no model loaded in the local engine)"]      # once, not at every call
    assert reg.resolve_route("relevance", local_loaded=False) == (second, "fallback") and reg.resolve_route("relevance", True) == (None, "local")


def test_the_fall_back_goes_to_the_first_provider_when_the_primary_is_the_local_engine_and_keeps_the_rules(world):
    cloud, local, reg, ledger = world
    outside = reg.add({"type": "openai_compat", "name": "Outside", "base_url": cloud.url + "/v1", "api_key": "k" * 12, "model": "m"})["id"]
    reg.add({"type": "openai_compat", "name": "Second", "base_url": cloud.url + "/v1", "api_key": "k" * 12, "model": "m"})
    loader = Loader(loaded=False)
    assert reg.fallback_target("planner") is None                              # personal data, both outside this PC, no yes: nothing is allowed
    with pytest.raises(TadamooError) as err:
        RoutedClient("planner", reg, local, ledger, loader=loader).chat_completion(MSG)
    assert "No model is loaded in the local engine" in str(err.value) and "no configured provider may be used" in str(err.value)
    reg.set_consent("planner", True)
    assert reg.fallback_target("planner") == outside                           # the first configured one
    RoutedClient("planner", reg, local, ledger, loader=loader).chat_completion(MSG)
    assert len(cloud.requests) == 1


def test_with_no_provider_at_all_the_error_says_so_in_plain_words(world):
    cloud, local, reg, ledger = world

    class Dead(Local):
        def chat_completion(self, **kw):
            raise TadamooError(0, "cannot reach the server")
    with pytest.raises(TadamooError) as err:
        RoutedClient("chat", reg, Dead(), ledger, loader=Loader(loaded=False)).chat_completion(MSG)
    assert str(err.value).startswith("No model is loaded in the local engine and no provider is configured")


def test_a_model_that_is_loaded_is_used_and_nothing_falls_back(world):
    cloud, local, reg, ledger = world
    provider(reg, cloud, "Unused")
    RoutedClient("relevance", reg, local, ledger, loader=Loader(loaded=True)).chat_completion(MSG)
    assert len(local.calls) == 1 and cloud.requests == []


# -- what the page is told ---------------------------------------------------------------------------------------

def test_the_page_is_told_what_every_function_resolves_to_and_which_ones_fell_back(world):
    cloud, local, reg, ledger = world
    pid = provider(reg, cloud, "Subscription", model="big-1")
    api = LlmApi(reg, ledger)
    state = {"loaded": True, "model": "Qwen-X"}
    api.local_status = lambda: dict(state)
    view = api.overview()
    assert view["primary"]["id"] == "" and view["primary"]["options"] == [{"id": pid, "label": "Subscription: big-1", "type": "openai_compat"}]
    assert next(f for f in view["functions"] if f["id"] == "chat")["now"]["label"] == "Qwen-X" and view["fallbacks"] == []
    api.set_primary({"primary": pid})
    api.registry.assign_checked("relevance", PRIMARY)
    view = api.overview()
    row = next(f for f in view["functions"] if f["id"] == "relevance")
    assert row["provider"] == PRIMARY and row["now"] == {"provider": pid, "via": "primary", "label": "Subscription: big-1", "local_loaded": True}
    state["loaded"] = False
    assert [n["function"] for n in api.overview()["fallbacks"]] == ["eval"]       # a function that handles personal data needs its yes first
    reg.set_consent("chat", True)
    view = api.overview()
    assert "chat" in [n["function"] for n in view["fallbacks"]] and view["fallbacks"][0]["to"] == "Subscription: big-1"
    assert api.answering()["chat"]["label"] == "Subscription: big-1" and api.answering()["chat"]["via"] == "fallback"
    with pytest.raises(LlmError):
        api.set_primary({"primary": "ghost"})


# -- the list of models and the test request ------------------------------------------------------------------------

def test_the_models_are_listed_with_the_address_and_key_of_the_form_or_the_saved_key(tmp_path, listing):
    reg = SecureRegistry(tmp_path / "p.json", credentials=FileCredentials(tmp_path / "c.json"))
    api = LlmApi(reg, Ledger(tmp_path / "u.json"))
    out = api.models({"type": "openai_compat", "base_url": listing.url + "/v1", "api_key": "sk-form-123456"})
    assert [m["id"] for m in out["models"]] == ["model-b", "model-a"]
    method, path, headers, _ = listing.seen[-1]
    assert (method, path) == ("GET", "/v1/models") and headers["authorization"] == "Bearer sk-form-123456"
    assert reg.load()["providers"] == []                                       # nothing was saved
    saved = reg.add({"type": "openai_compat", "base_url": listing.url + "/v1", "api_key": "sk-saved-123456", "model": "m"})["id"]
    api.models({"type": "openai_compat", "id": saved, "api_key": ""})           # the key field left empty: the saved key is used
    assert listing.seen[-1][2]["authorization"] == "Bearer sk-saved-123456"
    out = api.models({"type": "claude", "base_url": listing.url + "/anthropic/v1", "api_key": "sk-ant-123456"})
    assert out["models"] == [{"id": "claude-a", "label": "Claude A"}] and listing.seen[-1][2]["x-api-key"] == "sk-ant-123456"


def test_a_failed_listing_says_why_and_never_repeats_the_key(tmp_path, listing):
    api = LlmApi(SecureRegistry(tmp_path / "p.json", credentials=FileCredentials(tmp_path / "c.json")), Ledger(tmp_path / "u.json"))
    listing.fail = 401
    with pytest.raises(LlmError) as err:
        api.models({"type": "openai_compat", "base_url": listing.url + "/v1", "api_key": "sk-secret-123456"})
    assert "HTTP 401" in str(err.value) and "sk-secret-123456" not in str(err.value) and "***" in str(err.value)
    with pytest.raises(LlmError):
        api.models({"type": "not_a_kind"})


def test_the_form_test_sends_one_small_request_with_the_form_values_and_saves_nothing(tmp_path, listing):
    reg = SecureRegistry(tmp_path / "p.json", credentials=FileCredentials(tmp_path / "c.json"))
    api = LlmApi(reg, Ledger(tmp_path / "u.json"))
    out = api.try_form({"type": "openai_compat", "base_url": listing.url + "/v1", "api_key": "sk-form-123456", "model": "model-a"})
    method, path, headers, body = listing.seen[-1]
    assert out["ok"] is True and isinstance(out["ms"], int)
    assert (method, path) == ("POST", "/v1/chat/completions") and body["model"] == "model-a" and body["max_tokens"] <= 16
    assert headers["authorization"] == "Bearer sk-form-123456" and reg.load()["providers"] == []
    listing.fail = 404
    bad = api.try_form({"type": "openai_compat", "base_url": listing.url + "/v1", "api_key": "sk-form-123456", "model": "nope"})
    assert bad["ok"] is False and bad["error"] == "HTTP 404: model not found"
    anth = api.try_form({"type": "claude", "base_url": listing.url + "/anthropic/v1", "api_key": "sk-ant-123456", "model": "claude-a"})
    assert anth["ok"] is False                                                  # (the fake answers 404 now) the provider's own words come back
    listing.fail = 0
    assert api.try_form({"type": "claude", "base_url": listing.url + "/anthropic/v1", "api_key": "sk-ant-123456", "model": "claude-a"})["ok"] is True
    assert listing.seen[-1][1] == "/anthropic/v1/messages" and listing.seen[-1][2]["x-api-key"] == "sk-ant-123456"


def test_the_primary_model_and_the_order_survive_a_new_registry_on_the_same_file(tmp_path):
    """The choice is written to the file at once, so a later start of the server (or any other session) reads the same Primary and order."""
    cred = FileCredentials(tmp_path / "c.json")
    reg = SecureRegistry(tmp_path / "p.json", credentials=cred)
    ids = [reg.add({"type": "openai_compat", "name": n, "base_url": "http://127.0.0.1:9/v1", "api_key": "k" * 12, "model": "m"})["id"] for n in ("A", "B", "C")]
    reg.set_primary(ids[1], default_for_new=True)
    reg.reorder([ids[2], ids[0], ids[1]])
    again = SecureRegistry(tmp_path / "p.json", credentials=cred)
    assert again.primary() == ids[1] and again.default_primary() is True
    assert [p["id"] for p in again.load()["providers"]] == [ids[2], ids[0], ids[1]]
    again.reorder([ids[0], ids[1], ids[2]])
    assert again.primary() == ids[1]                                        # a new order does not move the Primary
