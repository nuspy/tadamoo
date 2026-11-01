"""The card after a task: next steps built by code from the facts of the work, the project's own checks offered once, and the answer read without a
model call when nothing more is wanted. Scripted model, no engine."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from harness import agent_loop as al
from harness import nextsteps as ns
from harness import questions as qs
from harness.project_profile import Check
from harness.session import Session, SessionStore, Task
from harness.stack import AgentConfig, build_agent


@pytest.fixture(autouse=True)
def _switch(tmp_path):
    ns.configure(lambda: True)
    yield
    ns.configure(None)


# -- the switch ---------------------------------------------------------------------------------------------------

def test_the_switch_is_off_where_nothing_configures_it_and_on_by_default_in_a_file(tmp_path):
    ns.configure(None)
    assert ns.enabled() is False
    ns.configure(tmp_path / "next-steps.json")
    assert ns.enabled() is True
    ns.Prefs(tmp_path / "next-steps.json").write(False)
    assert ns.enabled() is False


# -- the facts and the card -----------------------------------------------------------------------------------------

def call(name, path, error=False):
    return al.ToolExecutionResult(name, {"path": path}, "ok", error)


def fake_agent(steps=0, todo=(), gate=None, stop=al.FINAL, written=(), text="Add the report page"):
    turns = [al.AgentTurn(i + 1, "", "", "") for i in range(steps)]
    if turns:
        turns[0].tool_calls = [call("write_file", p) for p in written] + [call("read_file", "x"), call("edit_file", "broken", True)]
    items = [{"content": c, "status": s} for c, s in todo]
    tools = SimpleNamespace(todo=SimpleNamespace(open_items=lambda: [i for i in items if i["status"] != "completed"]))
    return SimpleNamespace(turns=turns, tools=tools, gate=gate, stop_reason=stop, last_user_text=text)


def labels(payload, qid="next"):
    return [o["label"] for q in payload["questions"] if q["id"] == qid for o in q["options"]]


def test_open_items_make_a_card_even_after_a_short_task():
    facts = ns.facts_from(fake_agent(steps=2, todo=[("write the docs", "pending"), ("tag the release", "in_progress"), ("done one", "completed")]))
    assert ns.wanted(facts) and facts.open_items == ["write the docs", "tag the release"]
    payload = ns.build(facts)
    assert labels(payload) == ["Finish the open items", "Nothing more"]
    q = payload["questions"][0]
    assert q["options"][0]["recommended"] is True and "write the docs; tag the release" in q["options"][0]["description"] and q["allow_other"] is True
    assert payload["source"] == "next_steps" and "_meta" in payload and "offer" not in payload


def test_a_small_task_with_nothing_to_follow_up_has_no_card():
    assert not ns.wanted(ns.facts_from(fake_agent(steps=ns.MIN_STEPS - 1, written=["a.py"], gate=None)))      # too small
    clean = ns.facts_from(fake_agent(steps=ns.MIN_STEPS + 3, gate=object()))
    assert not ns.wanted(clean) and ns.build(clean) is None                                                   # large, but nothing points anywhere


def test_files_changed_and_no_check_set_up_offers_to_check_the_work():
    facts = ns.facts_from(fake_agent(steps=ns.MIN_STEPS, written=["a.py", "a.py", "b.py"], gate=None))
    assert facts.check == "missing" and facts.changed == ["a.py", "b.py"]
    assert labels(ns.build(facts)) == ["Check the work", "Nothing more"]
    assert ns.facts_from(fake_agent(steps=ns.MIN_STEPS, written=["a.py"], gate=object())).check == ""


def test_a_failed_check_comes_first_and_carries_the_lesson_that_applies():
    seen = []

    def lessons(problem):
        seen.append(problem)
        return "Lessons from your earlier work (...):\n- (this project) tests fail | Went wrong: x | Fix: run the migration first"
    facts = ns.facts_from(fake_agent(steps=8, todo=[("tidy up", "pending")], gate=object(), stop=al.VERIFY_FAILED, written=["a.py"]), lesson_for=lessons)
    payload = ns.build(facts)
    assert labels(payload) == ["Fix the failing checks", "Finish the open items", "Nothing more"] and seen == ["checks failed"]
    first = payload["questions"][0]["options"][0]
    assert first["recommended"] is True and "run the migration first" in first["description"]


def test_the_card_speaks_the_language_of_the_request():
    payload = ns.build(ns.facts_from(fake_agent(steps=8, todo=[("scrivere i test", "pending")])), "it")
    assert labels(payload) == ["Finire le voci aperte", "Nient'altro"] and "scrivere i test" in payload["questions"][0]["options"][0]["description"]
    assert ns.build(ns.facts_from(fake_agent(steps=8, todo=[("x y", "pending")])), "fr")["questions"][0]["text"] == "What would you like to do next?"


def detected(*pairs):
    return [Check(kind, command, "pyproject.toml") for kind, command in pairs]


def test_the_checks_are_offered_as_a_multiple_choice_with_the_useful_kinds_first():
    checks = detected(("build", "make build"), ("lint", "python -m ruff check ."), ("test", "python -m pytest -x -q"), ("build", ""))
    facts = ns.facts_from(fake_agent(steps=1, written=["a.py"], gate=None), checks=checks)
    payload = ns.build(facts, show_next=False)
    q = payload["questions"][0]
    assert q["id"] == "checks" and q["kind"] == "multi" and q["required"] is False and q["allow_other"] is False
    assert [o["label"] for o in q["options"]] == ["python -m pytest -x -q", "python -m ruff check .", "make build"]
    assert [bool(o.get("recommended")) for o in q["options"]] == [True, True, False]
    assert payload["offer"] == {"question": "checks", "commands": {"o1": "python -m pytest -x -q", "o2": "python -m ruff check .", "o3": "make build"}}
    assert any("gate is off" in n for n in payload["notes"])
    assert not any("gate is off" in n for n in ns.build(ns.facts_from(fake_agent(written=["a"], gate=object()), checks=checks), show_next=False)["notes"])


def test_one_detected_check_is_a_yes_or_no_question():
    facts = ns.facts_from(fake_agent(steps=1, written=["a.py"]), checks=detected(("test", "cargo test")))
    payload = ns.build(facts, show_next=False)
    q = payload["questions"][0]
    assert q["kind"] == "confirm" and "cargo test" in q["text"] and payload["offer"]["commands"] == {"yes": "cargo test"}


def test_both_questions_in_one_card_and_the_answer_round_trip():
    facts = ns.facts_from(fake_agent(steps=8, todo=[("tidy", "pending")], written=["a.py"]), checks=detected(("test", "a b"), ("lint", "c d")))
    payload = ns.build(facts)
    meta = payload.pop("_meta")
    assert [q["id"] for q in payload["questions"]] == ["checks", "next"] and payload["offer"]["commands"] == {"o1": "a b", "o2": "c d"}
    checked = qs.check_answer(payload, {"status": "submitted", "answers": {"checks": {"picked": ["o1"]}, "next": {"picked": ["o3"]}}})
    text = qs.format_answer(payload, checked)
    assert "Run these checks before finishing? -> a b" in text and "Nothing more" in text
    assert ns.settle(meta, text) == meta["closed"]


def test_settle_reads_the_answer_without_a_model():
    facts = ns.facts_from(fake_agent(steps=8, todo=[("tidy", "pending")]))
    payload = ns.build(facts)
    meta = payload.pop("_meta")

    def answered(picked, text=""):
        return qs.format_answer(payload, qs.check_answer(payload, {"status": "submitted", "answers": {"next": {"picked": picked, "text": text}}}))
    assert ns.settle(meta, answered(["o2"])) == "Understood, nothing more for now."
    assert ns.settle(meta, answered(["o1"])) is None                              # an action: the agent works on
    assert ns.settle(meta, answered([], "also update the readme")) is None        # a typed answer: the agent reads it
    assert ns.settle(meta, "a normal message") is None
    only = ns.build(ns.facts_from(fake_agent(written=["a"]), checks=detected(("test", "a b"), ("lint", "c d"))), show_next=False)
    assert ns.settle(only.pop("_meta"), qs.ANSWER_HEADER + "\n1. Run these checks before finishing? -> a b") == "Noted."


# -- the conversation ------------------------------------------------------------------------------------------------

def reply(text="", calls=()):
    message = {"role": "assistant", "content": text}
    if calls:
        message["tool_calls"] = [{"id": f"t{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(calls)]
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if calls else "stop"}]}


class Model:
    def __init__(self, *steps):
        self.steps, self.calls = list(steps), 0

    def chat_completion(self, messages, tools=None, on_delta=None, **kwargs):
        self.calls += 1
        return self.steps.pop(0) if self.steps else reply("ok")

    def context_window(self):
        return None


class Events:
    def __init__(self):
        self.items, self.cond = [], threading.Condition()

    def __call__(self, event):
        with self.cond:
            self.items.append(event)
            self.cond.notify_all()

    def wait(self, kind, timeout=20.0, after=0):
        end = time.monotonic() + timeout
        with self.cond:
            while True:
                for e in self.items[after:]:
                    if e["event"] == kind:
                        return e
                if end - time.monotonic() <= 0:
                    raise AssertionError(f"no {kind} event; got {[e['event'] for e in self.items]}")
                self.cond.wait(0.1)

    def count(self, kind):
        return sum(1 for e in self.items if e["event"] == kind)


def session_with(tmp_path, model, unattended=False, switch=True):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    built = build_agent(AgentConfig(workspace=str(ws), data_dir=str(tmp_path / "data"), use_memory=False, use_checkpoints=False, permission_mode="default", max_turns=8),
                        client=model)
    events = Events()
    session = Session(built, SessionStore(tmp_path / "hist", redact=built.agent.secrets.mask_secrets), events, session_id="s1", sleep=lambda s: None, unattended=unattended)
    session.workspace = str(ws)
    ns.configure(lambda: switch)                      # building an agent sets the switch from its own settings
    return session, events, built


TODO = ("todo_write", {"todos": [{"content": "write the docs", "status": "pending"}]})


def open_item_run():
    # the checklist is still open at the answer, the harness sends the agent back once, and the second answer stands
    return [reply(calls=[TODO]), reply("All done."), reply("All done, really.")]


def test_after_a_final_answer_with_open_items_the_card_is_shown_and_nothing_more_costs_no_model_call(tmp_path):
    model = Model(*open_item_run())
    session, ev, built = session_with(tmp_path, model)
    session.send("m1", "Prepare the release")
    done = ev.wait("done")
    ask = ev.wait("ask")
    assert done["stop_reason"] == al.FINAL and ask["source"] == "next_steps" and "_meta" not in ask
    q = ask["questions"][0]
    assert [o["label"] for o in q["options"]] == ["Finish the open items", "Nothing more"]
    before, n = model.calls, len(ev.items)
    nothing = qs.ANSWER_HEADER + "\n1. " + q["text"] + " -> Nothing more\n" + qs.GO_ON
    session.send("m2", nothing)
    closed = ev.wait("done", after=n)
    assert closed["final"] == "Understood, nothing more for now." and model.calls == before and ev.count("ask") == 1      # no second card either


def test_picking_an_action_goes_back_to_the_model(tmp_path):
    model = Model(*open_item_run())
    session, ev, _ = session_with(tmp_path, model)
    session.send("m1", "Prepare the release")
    q = ev.wait("ask")["questions"][0]
    model.steps = [reply("Docs written."), reply("Docs written.")]       # the open item sends the agent back once more
    before, n = model.calls, len(ev.items)
    session.send("m2", qs.ANSWER_HEADER + "\n1. " + q["text"] + " -> Finish the open items\n" + qs.GO_ON)
    assert ev.wait("done", after=n)["final"] == "Docs written." and model.calls > before


def test_the_card_is_not_shown_when_the_switch_is_off_or_nobody_watches_or_the_task_is_small(tmp_path):
    session, ev, _ = session_with(tmp_path / "a", Model(*open_item_run()), switch=False)
    session.send("m1", "Prepare the release")
    ev.wait("done")
    time.sleep(0.3)
    assert ev.count("ask") == 0
    session, ev, _ = session_with(tmp_path / "b", Model(*open_item_run()), unattended=True)
    session.send("m1", "Prepare the release")
    ev.wait("done")
    time.sleep(0.3)
    assert ev.count("ask") == 0
    session, ev, _ = session_with(tmp_path / "c", Model(reply("Hello!")))
    session.send("m1", "Say hello")
    ev.wait("done")
    time.sleep(0.3)
    assert ev.count("ask") == 0


def finished_task(session, written=("a.py",), steps=2):
    agent = session.agent
    agent.stop_reason, agent.last_user_text = al.FINAL, "Add the report page"
    turn = al.AgentTurn(1, "", "", "done", tool_calls=[call("write_file", p) for p in written])
    agent.turns.extend([turn] * steps)
    return Task("t1", "Add the report page", agent, since=0)


def test_the_projects_checks_are_offered_once_after_the_first_work_on_files(tmp_path):
    session, ev, _ = session_with(tmp_path, Model())
    (tmp_path / "ws" / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths = ['tests']\n", encoding="utf-8")
    session.checks_offer = True
    assert session._next_steps(finished_task(session)) is True
    ask = ev.wait("ask")
    assert ask["offer"]["commands"]["yes"] == "python -m pytest -x -q" and ask["questions"][0]["kind"] == "confirm"
    assert session.checks_offer is False and session._steps_meta is not None
    assert session._next_steps(finished_task(session)) is False                  # not again, and nothing else to say after a small task


def test_no_offer_when_no_file_was_written_or_the_server_has_not_asked_for_it(tmp_path):
    session, ev, _ = session_with(tmp_path, Model())
    (tmp_path / "ws" / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    session.checks_offer = True
    assert session._next_steps(finished_task(session, written=())) is False
    session.checks_offer = False
    assert session._next_steps(finished_task(session)) is False
    assert ev.count("ask") == 0
