"""The inference settings are each conversation's own: saved with it, started from the machine default, taken from a plan by the conversations that carry
the plan out, changed per step of a plan (the thinking level) and for all steps at once; the summary call asks for no thinking."""
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "serve" / "tests"))

from serve import inference, profiles
from serve.agent_runs import RunError
from serve.agent_sessions import SessionManager
from tadamoo.planner import PlanDAG, PlanError, PlanProfile
from tadamoo.planner.dag import node_from_dict
from tadamoo.scheduler import SessionRoutineRunner
from test_e2e_mock_server import answer, start_server  # noqa: F401  (fixture)
from test_planner_starts import n, plan
from serve.plans import PlanService


@pytest.fixture(autouse=True)
def own_data_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("TADAMOO_DATA", str(tmp_path / "data"))


@pytest.fixture
def mgr(tmp_path):
    made = SessionManager(tmp_path / "j", "http://127.0.0.1:1", command=None, tick_s=0.1)
    yield made
    made.shutdown()


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return str(root)


def save_machine_default(settings):
    profiles.change({"action": "save", "kind": "inference", "name": profiles.MACHINE_NAME, "settings": settings})


# -- the machine default and a new conversation ---------------------------------------------------------------------------------------------

def test_a_new_conversation_starts_from_the_machine_default_and_keeps_its_own_copy(mgr, ws):
    assert inference.machine_default() == {}
    plain = mgr.create(ws)
    assert plain["inference"] == {} and plain["context_policy"] == "compact" and plain["compaction_threshold"] == 0.8 and plain["context_threshold"] == 0.8
    save_machine_default({"effort": "low", "policy": "compact", "threshold": "70", "temperature": "0.4", "top_p": "0.8", "max_tokens": "2048", "max_turns": "12",
                          "stop": "END"})
    made = mgr.create(ws)
    assert made["effort"] == "low" and made["compaction"] is True and made["compaction_threshold"] == 0.7 and made["context_threshold"] == 0.7
    assert (made["temperature"], made["max_tokens"], made["max_turns"]) == (0.4, 2048, 12)
    assert made["sampling"] == {"top_p": 0.8, "stop": ["END"]} and made["inference"]["effort"] == "low"
    cmd = mgr._command(made["id"], made, mgr.history_dir(made["id"]))
    assert cmd[cmd.index("--effort") + 1] == "low" and cmd[cmd.index("--compaction-threshold") + 1] == "0.7" and cmd[cmd.index("--context-threshold") + 1] == "0.7"
    assert cmd[cmd.index("--temperature") + 1] == "0.4" and cmd[cmd.index("--max-tokens") + 1] == "2048" and cmd[cmd.index("--max-turns") + 1] == "12"
    assert json.loads(cmd[cmd.index("--sampling") + 1]) == {"top_p": 0.8, "stop": ["END"]}
    save_machine_default({"effort": "high"})                                      # the default changes later: the conversation that exists is not touched
    assert mgr.get(made["id"])["effort"] == "low" and mgr.get(made["id"])["inference"]["effort"] == "low"
    assert mgr.create(ws)["effort"] == "high"


def test_settings_given_to_a_conversation_win_over_the_default_and_a_plain_argument_wins_over_both(mgr, ws):
    save_machine_default({"effort": "low", "threshold": "70"})
    given = mgr.create(ws, inference_settings={"effort": "medium", "policy": "stop", "temperature": "1"})
    assert given["effort"] == "medium" and given["compaction"] is False and given["temperature"] == 1.0
    assert mgr.create(ws, effort="none", context_threshold=0.6)["effort"] == "none"
    assert mgr.create(ws, max_turns=5)["max_turns"] == 5
    with pytest.raises(RunError) as bad:
        mgr.create(ws, inference_settings={"policy": "never"})
    assert bad.value.code == "bad_inference"


def test_a_conversation_made_before_the_settings_were_kept_shows_what_it_runs_with(tmp_path, ws):
    first = SessionManager(tmp_path / "old", "http://127.0.0.1:1", command=None)
    rec = first.create(ws, effort="low", max_turns=9, context_threshold=0.75)
    first.shutdown()
    saved = json.loads((tmp_path / "old" / "sessions" / "agent_sessions.json").read_text(encoding="utf-8"))
    for item in saved["sessions"]:
        item.pop("inference")
    (tmp_path / "old" / "sessions" / "agent_sessions.json").write_text(json.dumps(saved), encoding="utf-8")
    again = SessionManager(tmp_path / "old", "http://127.0.0.1:1", command=None)
    try:
        shown = again.get(rec["id"])["inference"]
        assert shown["effort"] == "low" and shown["max_turns"] == "9" and shown["threshold"] == "75" and shown["policy"] == "compact"
    finally:
        again.shutdown()


# -- changing the settings of a conversation -----------------------------------------------------------------------------------------------

def test_the_settings_of_an_open_conversation_are_saved_and_the_running_process_hears_what_changed(mgr, ws):
    sid = mgr.create(ws)["id"]
    sent = []
    mgr._tell_if_running = lambda s, cmd: sent.append(cmd) or True
    out = mgr.set_inference(sid, {"effort": "high", "policy": "compact", "threshold": "60", "temperature": "0.2", "top_k": "10", "max_turns": "7"})
    assert out["inference"]["threshold"] == "60" and out["effort"] == "high" and out["compaction_threshold"] == 0.6 and out["context_threshold"] == 0.6
    assert out["temperature"] == 0.2 and out["max_turns"] == 7 and out["sampling"] == {"top_k": 10}
    settings = next(c for c in sent if c["cmd"] == "settings")
    assert settings["temperature"] == 0.2 and settings["max_turns"] == 7 and settings["sampling"] == {"top_k": 10}
    assert settings["fold"] == {"on": True, "threshold": 0.6, "mask": 0.6}
    assert "max_tokens" not in settings and "end_turn_bias" not in settings                  # only what changed is told
    assert {"cmd": "effort", "level": "high"} in sent
    sent.clear()
    mgr.set_inference(sid, {"effort": "high", "policy": "compact", "threshold": "60", "temperature": "0.2", "top_k": "10", "max_turns": "7"})
    assert sent == []                                                                          # the same settings again tell nothing
    with pytest.raises(RunError) as bad:
        mgr.set_inference(sid, {"temperature": "9"})
    assert bad.value.code == "bad_inference" and mgr.get(sid)["temperature"] == 0.2          # a wrong value changes nothing
    mgr.set_effort(sid, "none")
    assert mgr.get(sid)["inference"]["effort"] == "none" and mgr.get(sid)["effort"] == "none"
    mgr.set_settings(sid, max_turns=3)
    assert mgr.get(sid)["inference"]["max_turns"] == "3"


def test_the_pages_make_a_conversation_with_the_panel_and_save_it_back_whole(tmp_path, monkeypatch, start_server, ws):
    import urllib.error
    import urllib.request
    url = start_server([answer("x")])

    def call(method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")
    code, made = call("POST", "/api/agent/sessions", {"workspace": ws, "inference": {"effort": "low", "policy": "compact", "threshold": "70", "top_p": "0.9"}})
    assert code == 200 and made["session"]["inference"]["threshold"] == "70" and made["session"]["compaction_threshold"] == 0.7 and made["session"]["sampling"] == {"top_p": 0.9}
    sid = made["session"]["id"]
    code, saved = call("POST", f"/api/agent/sessions/{sid}/inference", {"settings": {"effort": "high", "policy": "stop", "temperature": "0.5"}})
    assert code == 200 and saved["session"]["effort"] == "high" and saved["session"]["compaction"] is False and saved["session"]["sampling"] == {}
    assert call("GET", f"/api/agent/sessions/{sid}/history")[1]["session"]["inference"]["policy"] == "stop"
    code, bad = call("POST", f"/api/agent/sessions/{sid}/inference", {"settings": {"policy": "never"}})
    assert code == 400 and bad["error"]["code"] == "bad_inference"
    assert call("POST", "/api/agent/sessions", {"workspace": ws})[1]["session"]["inference"] == {}                       # none given: the machine default, which is empty here
    assert call("GET", "/api/compaction")[0] == 404                                                                   # no global setting for it any more


# -- the harness side ---------------------------------------------------------------------------------------------------------------------

def test_the_process_takes_the_sampling_and_the_folding_while_it_runs():
    from tadamoo.session import _valid_settings
    assert _valid_settings({"sampling": {"top_p": 0.9}}) and _valid_settings({"fold": {"on": True, "threshold": 0.7, "mask": 0.7}})
    assert not _valid_settings({"sampling": [1]}) and not _valid_settings({"fold": {"on": "yes", "threshold": 0.7, "mask": 0.7}}) and not _valid_settings({})

    class Agent:
        sampling = {"temperature": 0.3, "top_p": 0.5}

        class context:
            mask_at = 0.7
            from tadamoo.compaction import Compaction
            compaction = Compaction(None, True, 0.85)
    from tadamoo.session import Session
    fake = Session.__new__(Session)
    fake.agent = Agent
    Agent.context.compaction._last_after = 5000
    Session.set_sampling(fake, {"top_k": 8, "stop": ["x"]})
    assert Agent.sampling == {"temperature": 0.3, "top_k": 8, "stop": ["x"]}                  # what was given replaces the earlier extras; the temperature stays
    Session.set_folding(fake, False, 0.6, 0.55)
    assert Agent.context.mask_at == 0.55 and Agent.context.compaction.enabled is False and Agent.context.compaction.threshold == 0.6
    assert Agent.context.compaction._last_after == 0


def test_the_size_a_compaction_left_is_forgotten_when_the_budget_changes():
    from tadamoo.compaction import Compaction

    class Ctx:
        budget_tokens, mask_at = 100_000, 0.8

        def estimate_tokens(self, messages):
            return 88_000
    comp = Compaction(None, True, 0.85)
    comp._last_after = 80_000
    ctx = Ctx()
    assert comp.due(ctx, []) is False                                                          # it left 80 thousand: not again before 90
    comp.forget_last()
    assert comp.due(ctx, []) is True and comp.start_tokens(ctx) == 85_000


def test_the_summary_is_asked_for_with_no_thinking_whatever_the_route_says():
    from tadamoo.llm import client_for

    class Here:
        base_url, api_key = "", None
    assert client_for("compaction", Here(), level="none").level == "none"
    assert client_for("compaction", Here()).level == "inherit"
    source = (Path(__file__).resolve().parents[1] / "tadamoo" / "session.py").read_text(encoding="utf-8")
    assert 'client_for("compaction", here, level="none")' in source


# -- the thinking level of a step ---------------------------------------------------------------------------------------------------------

def test_an_action_step_may_carry_a_thinking_level_and_nothing_else_may():
    dag = PlanDAG.from_dict(plan("p", n("a"), n("d", "decision", deps=["a"], spec={"branches": []})))
    dag.update_node("a", effort="high")
    assert dag.nodes["a"].effort == "high" and node_from_dict(json.loads(json.dumps(dag.to_dict()["nodes"]["a"]))).effort == "high"
    assert "effort" not in dag.to_dict()["nodes"]["d"]
    dag.update_node("a", effort="")
    assert dag.nodes["a"].effort is None and "effort" not in dag.to_dict()["nodes"]["a"]
    for bad in ("extreme", 3):
        with pytest.raises(PlanError):
            dag.update_node("a", effort=bad)
    with pytest.raises(PlanError, match="action"):
        dag.update_node("d", effort="low")


def test_one_level_for_all_the_steps_skips_what_is_done_and_what_is_not_an_action(tmp_path):
    svc = PlanService(tmp_path / "planner")
    svc.launcher, svc.run_active = (lambda *a: None), (lambda plan_id: False)
    svc.create({"plan": plan("p", n("a"), n("b", deps=["a"]), n("c", deps=["b"]), n("q", "input", deps=["c"], assignee="user", spec={"prompt": "ok?", "into": "ok"}))})
    svc.handle_event("p", {"type": "plan:update_node", "node_id": "c", "changes": {"effort": "low"}})
    svc.handle_event("p", {"type": "plan:mark", "node_id": "a", "status": "in_progress"})
    svc.handle_event("p", {"type": "plan:mark", "node_id": "a", "status": "completed"})
    out = svc.handle_event("p", {"type": "plan:set_reasoning", "level": "high"})
    assert out["ok"] and out["level"] == "high" and sorted(out["changed"]) == ["b", "c"]
    nodes = svc.bridge("p").dag.nodes
    assert [nodes[i].effort for i in "abc"] == [None, "high", "high"] and getattr(nodes["q"], "effort", None) is None
    assert svc.node_effort("p", "b") == "high" and svc.node_effort("p", "a") == "" and svc.node_effort("p", "nothing") == "" and svc.node_effort("none", "b") == ""
    again = svc.handle_event("p", {"type": "plan:set_reasoning", "level": ""})
    assert sorted(again["changed"]) == ["b", "c"] and svc.node_effort("p", "b") == ""
    assert svc.handle_event("p", {"type": "plan:set_reasoning", "level": "extreme"})["ok"] is False


def test_a_conversation_working_on_a_step_thinks_as_the_step_says_then_as_the_plan_says(mgr, ws):
    levels = {("p", "x"): "high"}
    mgr.plan_effort = lambda plan_id, node_id: levels.get((plan_id, node_id), "")
    sid = mgr.create(ws, inference_settings={"effort": "low"})["id"]
    sent = []
    mgr._tell_if_running = lambda s, cmd: sent.append(cmd) or True

    def tool(name, args, call_id, output="Step x started."):
        mgr._watch(sid, {"event": "agent", "data": {"type": "tool_start", "name": name, "arguments": args, "call_id": call_id, "effect": "pure"}})
        mgr._watch(sid, {"event": "agent", "data": {"type": "tool_end", "name": name, "call_id": call_id, "status": "completed", "output": output}})
    tool("plan_step_start", {"plan_id": "p", "node_id": "x"}, "1")
    assert sent == [{"cmd": "effort", "level": "high"}]
    tool("plan_mark", {"plan_id": "p", "node_id": "x", "status": "completed"}, "2", "ok")
    assert sent[-1] == {"cmd": "effort", "level": "low"} and mgr.get(sid)["effort"] == "low"      # the record keeps the plan's level
    tool("plan_step_start", {"plan_id": "p", "node_id": "y"}, "3", "Step y started.")
    assert len(sent) == 2                                                                      # a step with no level of its own: nothing to tell


# -- a plan's own settings -----------------------------------------------------------------------------------------------------------

def test_a_plan_keeps_its_settings_in_its_profile_and_the_conversations_that_carry_it_out_take_them(tmp_path, ws):
    assert PlanProfile.from_dict({"inference": {"effort": "low", "policy": "compact", "threshold": "75"}}).inference["threshold"] == "75"
    assert PlanProfile.from_dict(None).inference == {} and PlanProfile.from_dict(PlanProfile().to_dict()).to_dict()["inference"] == {}
    for bad in ({"inference": []}, {"inference": {"policy": "never"}}, {"inference": {"temperature": "99"}}):
        with pytest.raises(ValueError):
            PlanProfile.from_dict(bad)
    svc = PlanService(tmp_path / "planner")
    svc.create({"plan": plan("p", n("a")), "profile": {"inference": {"effort": "medium", "policy": "compact", "threshold": "65", "temperature": "0.1"}}})
    svc.create({"plan": plan("q", n("a"))})
    assert svc.inference_of("p")["threshold"] == "65" and svc.inference_of("q") is None and svc.inference_of("missing") is None
    made = []
    manager = SessionManager(tmp_path / "j", "http://127.0.0.1:1", command=None)
    try:
        real = manager.create
        manager.create = lambda *a, **k: (made.append(real(*a, **k)), made[-1])[1]
        runner = SessionRoutineRunner(manager, plan_inference=svc.inference_of)
        runner.manager.send = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop here"))
        for plan_id in ("p", "q", ""):
            routine = {"id": "r", "name": "Plan", "prompt": "go", "workspace": ws, "link": {"plan": plan_id} if plan_id else {}}
            try:
                runner.run(routine, threading.Event())
            except RuntimeError:
                pass
        with_plan, without, no_plan = made
        assert with_plan["effort"] == "medium" and with_plan["context_threshold"] == 0.65 and with_plan["temperature"] == 0.1
        assert without["effort"] == "" and without["inference"] == {}                           # no settings of its own: the machine default
        assert no_plan["max_turns"] == -1                                                       # an ordinary routine: its own, empty means no limit
    finally:
        manager.shutdown()


# -- the pages --------------------------------------------------------------------------------------------------------------------------

WEB = Path(__file__).resolve().parents[1] / "serve" / "web"
NEW_KEYS = ("cs.active.machine", "cs.active.plan", "cs.active.session", "cs.active.new", "cs.active.new.model", "cs.panel.saved", "cs.panel.failed", "cs.effort.high",
            "cs.effort.medium", "cs.effort.low", "cs.effort.none", "cs.effort.help", "inf.title", "inf.help", "inf.open", "inf.close", "pl.inf.open", "pl.inf.close",
            "pl.inf.tip", "pl.effort.all", "pl.effort.all.label", "pl.effort.all.tip", "pl.effort.all.done", "pl.effort.inherit", "pl.effort.none", "pl.effort.low",
            "pl.effort.medium", "pl.effort.high", "pl.f.effort", "pl.f.effort.help")


def test_the_one_panel_is_shown_for_a_conversation_a_plan_and_the_machine_default_and_every_text_is_in_both_languages():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    chatset = (WEB / "chatset.js").read_text(encoding="utf-8")
    plans = (WEB / "plans.js").read_text(encoding="utf-8")
    infdefault = (WEB / "infdefault.js").read_text(encoding="utf-8")
    css = (WEB / "app.css").read_text(encoding="utf-8")
    i18n = (WEB / "i18n.js").read_text(encoding="utf-8")
    assert 'id="cs-effort"' in html and 'id="inf-section"' in html and 'id="inf-host"' in html and 'src="web/infdefault.js"' in html
    assert html.index('src="web/chatset.js"') < html.index('src="web/infdefault.js"')                       # it uses the panel
    for group in ("cs-visuals", "cs-perm", "cs-cp", "cs-bc"):
        assert f'class="csgroup csconv" id="{group}"' in html                                               # what only a conversation has is hidden for a plan
    assert '.chatset[data-scope="plan"] .csconv' in css and '.chatset[data-scope="machine"] .csconv' in css
    assert "return { body, system, policy, threshold, structured, showReasoning, autoContinue, patterns, read, useSession, fresh, edit, endEdit, editing" in chatset
    assert 'kind: "plan"' in plans and "chatSettings.edit(ui.infHost" in plans and '"plan:set_reasoning"' in plans and 'id: "pl-effort-all"' in plans and 'id: "pl-d-effort"' in plans
    assert 'kind: "machine"' in infdefault and '"Machine Default"' in infdefault and "innerHTML" not in infdefault
    assert "min-height: 44px" in css[css.index(".plinf-l select"):css.index(".pl-bar .plinf-l")]                       # a target to touch on a phone
    for key in NEW_KEYS:
        assert i18n.count(f'"{key}":') == 2, key                                                            # once in English and once in Italian
    assert '"cx.title":' not in i18n and "compaction.js" not in html                                       # the global setting is gone
