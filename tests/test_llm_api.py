"""The Providers page's routes: functions and providers, keys write-only, what is refused, and the server's own page only."""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tadamoo.llm.credentials import FileCredentials
from tadamoo.llm.store import Ledger, SecureRegistry
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


def test_other_programs_are_a_function_of_the_list_that_needs_no_consent_and_stays_on_the_loaded_model_until_chosen(api):
    call, reg = api
    pid = call("POST", "/api/llm/providers", {"type": "openai", "api_key": "k" * 12, "model": "m"})[1]["provider"]["id"]
    call("POST", "/api/llm/primary", {"provider": pid, "default_for_new": True})
    row = next(f for f in call("GET", "/api/llm")[1]["functions"] if f["id"] == "external")
    assert (row["label"], row["needs_consent"], row["provider"], row["now"]["via"]) == ("Other programs (OpenAI and Anthropic interfaces)", False, "", "local")
    code, view = call("POST", "/api/llm/assign", {"function": "external", "provider": pid})
    assert code == 200 and next(f for f in view["functions"] if f["id"] == "external")["provider"] == pid
    code, view = call("POST", "/api/llm/assign", {"function": "external", "provider": ""})
    assert next(f for f in view["functions"] if f["id"] == "external")["now"]["via"] == "local"


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


def test_an_unknown_llm_route_is_not_found(api):
    call, reg = api
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


def test_the_council_roles_and_rounds_are_saved_with_their_cost_and_bad_ones_are_refused(api):
    call, reg = api
    view = call("GET", "/api/llm")[1]["council"]
    assert [r["id"] for r in view["settings"]["roles"]] == ["proposer", "critic", "engineer"] and view["estimate"]["calls_max"] == 12 and view["ready"] is True
    assert all(r["route"] == "" and r["enabled"] for r in view["settings"]["roles"]) and view["settings"]["council_auto"] == "ask" and view["settings"]["max_calls"] == 0
    assert "council:proposer" not in {f["id"] for f in call("GET", "/api/llm")[1]["functions"]} and not any(f["id"].startswith("council_") for f in call("GET", "/api/llm")[1]["functions"])
    roles = [{"name": "lawyer", "job": "Say what the law allows."}, {"name": "accountant", "job": "Say what it costs."}, {"name": "skeptic", "job": "Doubt.", "enabled": False}]
    code, body = call("POST", "/api/llm/council", {"rounds": 2, "roles": roles, "moderator": {"name": "judge", "job": "Decide."}, "max_calls": 5, "council_auto": "auto"})
    got = body["council"]
    assert code == 200 and got["settings"]["roles"][0]["name"] == "lawyer" and got["settings"]["roles"][2]["enabled"] is False and got["settings"]["max_calls"] == 5
    assert got["estimate"]["calls_max"] == 3 and got["estimate"]["notes"] and got["settings"]["council_auto"] == "auto"      # two advisers, 3 calls a round, a cap of 5: one round
    view = call("GET", "/api/llm")[1]["council"]["settings"]
    assert view["moderator"]["name"] == "judge" and view["moderator"]["id"] == "moderator"
    code, err = call("POST", "/api/llm/council", {"rounds": 99})
    assert code == 400 and err["error"]["code"] == "bad_council" and call("GET", "/api/llm")[1]["council"]["settings"]["rounds"] == 2
    assert call("POST", "/api/llm/council", {"max_calls": 2})[0] == 400 and call("POST", "/api/llm/council", {"council_auto": "x"})[0] == 400
    call("POST", "/api/llm/council", {"rounds": 3})                                                  # a part left out stays as it was
    assert call("GET", "/api/llm")[1]["council"]["settings"]["roles"][1]["name"] == "accountant"


def test_each_role_has_its_own_route_and_its_own_yes_for_personal_data_and_both_leave_with_the_role(api):
    call, reg = api
    here = call("POST", "/api/llm/providers", {"type": "basalt"})[1]["provider"]["id"]
    cloud = call("POST", "/api/llm/providers", {"type": "openai", "api_key": "k" * 12, "model": "m"})[1]["provider"]["id"]
    roles = [{"name": "a", "job": "j", "route": here}, {"name": "b", "job": "j", "route": cloud}, {"name": "c", "job": "j"}]
    code, err = call("POST", "/api/llm/council", {"roles": roles})
    assert code == 409 and err["error"]["code"] == "not_allowed" and "say yes" in err["error"]["message"] and "b:" in err["error"]["message"]
    assert reg.load().get("assignments") == {} and call("GET", "/api/llm")[1]["council"]["settings"]["roles"][0]["name"] == "proposer"       # nothing was saved
    roles[1]["consent"] = True
    code, body = call("POST", "/api/llm/council", {"roles": roles, "moderator": {"name": "m", "job": "j", "route": here}})
    got = body["council"]
    assert code == 200 and [r["route"] for r in got["settings"]["roles"]] == [here, cloud, ""] and [r["consent"] for r in got["settings"]["roles"]] == [False, True, False]
    assert got["settings"]["moderator"]["route"] == here and got["outside"] == ["council:b"] and got["estimate"]["outside_calls_max"] == 3
    assert reg.load()["assignments"] == {"council:a": here, "council:b": cloud, "council:moderator": here} and reg.consent() == {"council:b": True}
    roles = [{**r} for r in got["settings"]["roles"]]
    roles[1]["consent"] = False                                                                      # taking the yes back takes the role off the outside provider
    got = call("POST", "/api/llm/council", {"roles": roles})[1]["council"]
    assert got["settings"]["roles"][1]["route"] == "" and got["outside"] == [] and got["settings"]["roles"][0]["route"] == here
    roles = [{**r} for r in got["settings"]["roles"]]
    got = call("POST", "/api/llm/council", {"roles": roles[:2]})[1]["council"]                       # a role that is removed takes its route away
    assert [r["id"] for r in got["settings"]["roles"]] == ["a", "b"] and "council:c" not in reg.load()["assignments"]
    assert call("POST", "/api/llm/council", {"roles": [{"name": "x", "job": "j", "route": "ghost"}, roles[0]]})[0] == 409
    assert call("POST", "/api/llm/council", {"roles": [{"name": "x", "job": "j", "consent": "yes"}, roles[0]]})[0] == 400
    assert call("POST", "/api/llm/assign", {"function": "council:a", "provider": here})[0] == 400        # the Council section is the one place for it


def test_a_provider_that_is_removed_leaves_its_roles_on_the_loaded_model(api):
    call, reg = api
    here = call("POST", "/api/llm/providers", {"type": "basalt"})[1]["provider"]["id"]
    call("POST", "/api/llm/council", {"roles": [{"name": "a", "job": "j", "route": here}, {"name": "b", "job": "j"}]})
    call("POST", f"/api/llm/providers/{here}/remove", {})
    assert call("GET", "/api/llm")[1]["council"]["settings"]["roles"][0]["route"] == ""


def test_the_council_of_the_old_shape_is_moved_when_the_page_first_asks_for_it(api):
    call, reg = api
    here = call("POST", "/api/llm/providers", {"type": "basalt"})[1]["provider"]["id"]
    data = reg.load()
    data["council"] = {"rounds": 2, "roles": [{"name": "lawyer", "job": "Law."}, {"name": "accountant", "job": "Cost."}, {"name": "skeptic", "job": "Doubt."}],
                       "moderator": {"name": "judge", "job": "Decide."}}
    data["assignments"] = {"council_2": here}
    reg.save(data)
    view = call("GET", "/api/llm")[1]["council"]["settings"]
    assert [r["id"] for r in view["roles"]] == ["lawyer", "accountant", "skeptic"] and view["roles"][1]["route"] == here and view["rounds"] == 2


def test_the_providers_page_sets_the_order_the_providers_are_tried_in(api):
    call, reg = api
    ids = [call("POST", "/api/llm/providers", {"type": "openai", "name": n, "api_key": "k" * 12, "model": "m"})[1]["provider"]["id"] for n in ("A", "B", "C")]
    assert [p["id"] for p in call("GET", "/api/llm")[1]["providers"]] == ids
    code, body = call("POST", "/api/llm/providers/order", {"ids": [ids[2], ids[0], ids[1]]})
    assert code == 200 and body["order"] == [ids[2], ids[0], ids[1]]
    assert [p["id"] for p in call("GET", "/api/llm")[1]["providers"]] == [ids[2], ids[0], ids[1]]
    for bad in ({"ids": ids[:2]}, {"ids": ids + ids}, {"ids": "abc"}, {}):
        code, body = call("POST", "/api/llm/providers/order", bad)
        assert code == 400 and body["error"]["code"] == "bad_order"
    assert [p["id"] for p in call("GET", "/api/llm")[1]["providers"]] == [ids[2], ids[0], ids[1]]
