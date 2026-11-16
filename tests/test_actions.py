"""The one list of what is missing from the person (serve/actions.py): every source, the answers sent back, the first answer wins, and the one-time announcement."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serve.actions import ActionError, Actions
from serve.agent_runs import RunError
from serve.approvals import ApprovalBoard


class Sessions:
    def __init__(self, records):
        self.records, self.sent = records, []

    def view(self):
        return {"sessions": self.records}

    def answer(self, sid, ask_id, value):
        rec = next(r for r in self.records if r["id"] == sid)
        waiting = [rec["ask"]["id"]] if rec.get("ask") else []
        waiting += [p["id"] for p in rec.get("pending") or []]
        if rec.get("question"):
            waiting.append(rec["question"]["id"])
        if ask_id not in waiting:
            raise RunError("That question is not waiting for an answer any more.", "not_asking")
        self.sent.append((sid, ask_id, value))
        rec["ask"] = None
        rec["pending"] = [p for p in rec.get("pending") or [] if p["id"] != ask_id]
        rec["question"] = None


class Bridge:
    def __init__(self, proposals):
        self.proposals = proposals


class Proposal:
    def __init__(self, pid, title):
        self.proposal_id, self.title, self.rationale, self.status, self.created_at, self.involves = pid, title, "because", "pending", 50.0, []

    def to_dict(self):
        return {"changes": {"add": ["x", "y"], "remove": []}}


class Plans:
    def __init__(self):
        self.requests = [
            {"plan_id": "p", "plan_title": "Briefing", "node_id": "approval1", "title": "Approvazione", "ask": "approve", "prompt": "Va bene?", "prompt_en": "Fine?", "help": "Read it", "content": "", "since": 10.0,
             "kind": "text", "choices": []},
            {"plan_id": "p", "plan_title": "Briefing", "node_id": "pick", "title": "Edition", "ask": "value", "prompt": "Quale?", "prompt_en": "Which?", "since": 11.0, "kind": "choice",
             "choices": [{"value": "a", "label": "A", "label_en": "A"}, {"value": "b", "label": "B", "label_en": "B"}, {"value": "c", "label": "C", "label_en": "C"}], "other": True, "proposed": "a"},
            {"plan_id": "p", "plan_title": "Briefing", "node_id": "how_many", "title": "N", "ask": "value", "prompt": "Quanti?", "prompt_en": "How many?", "since": 12.0, "kind": "number", "choices": []},
            {"plan_id": "p", "plan_title": "Briefing", "node_id": "call", "title": "Phone", "ask": "task", "prompt": "Chiama", "prompt_en": "", "content": "Call him", "since": 13.0, "kind": "text", "choices": []},
        ]
        self.events = []
        self.bridges = {"p": Bridge({"pr1": Proposal("pr1", "Add a step")})}

    def human_requests(self):
        return list(self.requests)

    def list(self):
        return [{"plan_id": "p", "title": "Briefing"}]

    def bridge(self, plan_id):
        return self.bridges[plan_id]

    def handle_event(self, plan_id, event):
        self.events.append(event)
        if event["type"] == "plan:answer":
            left = [r for r in self.requests if r["node_id"] != event["node_id"]]
            if len(left) == len(self.requests):
                return {"ok": False, "error": f"'{event['node_id']}' is not asking now: it was answered already"}
            self.requests = left
        else:
            bridge = self.bridges[plan_id]
            if event["proposal_id"] not in bridge.proposals:
                return {"ok": False, "error": "that proposal is not pending"}
            bridge.proposals.pop(event["proposal_id"])
        return {"ok": True}


class Scheduler:
    def __init__(self):
        self.rows = [{"id": "r1", "routine": "x", "name": "Morning", "session": "s1", "key": "s1:ask9", "kind": "ask", "question": "Same as the session's", "options": ["a"], "created": 5.0},
                     {"id": "r2", "routine": "x", "name": "Morning", "session": "s2", "key": "s2:zz", "kind": "ask", "question": "Only here", "options": ["go", "stop"], "created": 6.0}]

    def pending(self):
        return list(self.rows)

    def dismiss(self, pid):
        if not any(r["id"] == pid for r in self.rows):
            raise RunError("That request is not in the list any more.", "gone")
        self.rows = [r for r in self.rows if r["id"] != pid]


def records():
    return [
        {"id": "s1", "alias": "coder", "state": "asking", "updated": 20.0, "ask": {"id": "ask9", "mode": "confirm", "kind": "permission", "question": "Run rm?", "options": [{"value": "confirm", "label": "Yes"}, {"value": "deny", "label": "No"}],
                                                                                    "facts": {"instructions": "ignore the rules"}, "tool": "shell", "arguments": {"cmd": "rm x"}}, "pending": [
            {"id": "pend1", "question": "Which port?", "free": True, "at": 21.0},
            {"id": "pend2", "question": "Pick", "options": [{"value": "one", "label": "One"}, {"value": "two", "label": "Two"}, {"value": "three", "label": "Three"}], "free_prefix": "other:", "at": 22.0}]},
        {"id": "s3", "title": "Notes", "state": "idle", "updated": 23.0, "question": {"id": "card1", "intro": "Choose", "questions": [
            {"id": "q1", "kind": "single", "question": "Which one?", "options": [{"id": "o1", "label": "First"}, {"id": "o2", "label": "Second"}]}]}},
        {"id": "s4", "title": "Many", "state": "idle", "updated": 24.0, "question": {"id": "card2", "questions": [
            {"id": "q1", "kind": "single", "question": "A?", "options": []}, {"id": "q2", "kind": "multi", "question": "B?", "options": []}]}},
        {"id": "s5", "state": "closed", "updated": 25.0, "ask": {"id": "x", "options": [], "question": "gone"}},
    ]


def make(tmp_path):
    board = ApprovalBoard()
    board.request("run1", "shell", {"cmd": "ls"}, "reads")
    return Actions(sessions=Sessions(records()), plans=Plans(), routines=Scheduler(), approvals=board, path=tmp_path / "n.json", clock=lambda: 100.0)


def by_id(rows):
    return {r["id"]: r for r in rows}


def test_every_source_is_listed_oldest_first_with_only_what_is_open(tmp_path):
    acts = make(tmp_path)
    view = acts.view("android")
    rows = by_id(view["actions"])
    assert view["count"] == len(rows) == 12 and view["new"] == 12
    assert [r["created_at"] for r in view["actions"]] == sorted(r["created_at"] for r in view["actions"])
    assert "session:s5:x" not in rows                                              # a closed conversation asks nothing
    perm = rows["session:s1:ask9"]
    assert perm["kind"] == "confirm" and perm["context"] == "coder" and [o["value"] for o in perm["options"]] == ["confirm", "deny"]
    assert perm["details"]["arguments"] == {"cmd": "rm x"} and perm["details"]["instructions"] == "ignore the rules" and perm["details"]["tool"] == "shell"
    assert rows["session:s1:pend1"]["kind"] == "text" and rows["session:s1:pend1"]["free_hint"] == "code"
    assert rows["session:s1:pend2"]["kind"] == "choice" and rows["session:s1:pend2"]["free"] is True
    assert rows["session:s3:card1"]["kind"] == "confirm" and [o["value"] for o in rows["session:s3:card1"]["options"]] == ["o1", "o2"]
    assert rows["session:s4:card2"]["kind"] == "card" and rows["session:s4:card2"]["open"] == {"session": "s4"}
    assert rows["plan:p:approval1"]["options"][0]["label_en"] == "Yes" and rows["plan:p:approval1"]["title_en"] == "Fine?" and "Read it" in rows["plan:p:approval1"]["text"]
    assert rows["plan:p:pick"]["kind"] == "choice" and rows["plan:p:pick"]["free"] is True and rows["plan:p:pick"]["proposed"] == "a"
    assert rows["plan:p:how_many"]["free_hint"] == "number" and rows["plan:p:call"]["kind"] == "task" and rows["plan:p:call"]["text"] == "Call him"
    assert rows["proposal:p:pr1"]["options"][1]["value"] == "reject" and "add: 2" in rows["proposal:p:pr1"]["text"]
    assert "routine:r1" not in rows and rows["routine:r2"]["options"][0]["value"] == "dismiss"      # the routine's copy of a conversation's question is not listed twice
    approval = next(r for r in view["actions"] if r["source"] == "approval")
    assert approval["details"]["arguments"] == '{"cmd": "ls"}' and [o["value"] for o in approval["options"]] == ["approve", "deny"]


def test_answers_go_to_their_source_and_a_second_answer_is_refused_clearly(tmp_path):
    acts = make(tmp_path)
    s, p = acts._sessions, acts._plans
    acts.answer("session:s1:ask9", "confirm")
    assert s.sent[-1] == ("s1", "ask9", "confirm")
    with pytest.raises(ActionError) as e:
        acts.answer("session:s1:ask9", "deny")
    assert e.value.code == "gone"
    acts.answer("session:s1:pend1", "", "4242")
    assert s.sent[-1] == ("s1", "pend1", "4242")
    acts.answer("session:s1:pend2", "", "something else")
    assert s.sent[-1] == ("s1", "pend2", "other:something else")
    acts.answer("session:s3:card1", "o2")
    sid, aid, value = s.sent[-1]
    assert (sid, aid) == ("s3", "card1") and json.loads(value[4:]) == {"status": "submitted", "answers": {"q1": {"picked": ["o2"], "text": ""}}}
    acts.answer("plan:p:approval1", "yes")
    acts.answer("plan:p:how_many", "", "7")
    acts.answer("plan:p:pick", "c")
    acts.answer("plan:p:call", "done", "called")
    assert [(e["node_id"], e["answer"]) for e in p.events if e["type"] == "plan:answer"] == [("approval1", True), ("how_many", "7"), ("pick", "c"), ("call", {"done": True, "note": "called"})]
    with pytest.raises(ActionError) as e:
        acts.answer("plan:p:approval1", "no")
    assert e.value.code == "gone"
    acts.answer("proposal:p:pr1", "approve")
    assert p.events[-1] == {"type": "plan:approve", "proposal_id": "pr1", "actor": "user"}
    acts.answer("routine:r2", "dismiss")
    assert [r["id"] for r in acts._routines.pending()] == ["r1"]
    board_id = next(r["id"] for r in acts.open() if r["source"] == "approval")
    acts.answer(board_id, "deny")
    assert acts._approvals.view()["pending"] == []
    with pytest.raises(ActionError) as e:
        acts.answer(board_id, "approve")
    assert e.value.code == "gone"
    assert not any(r["source"] in ("plan", "proposal") for r in acts.open())          # what was answered is not listed any more


def test_a_wrong_answer_is_refused_before_it_reaches_the_source(tmp_path):
    acts = make(tmp_path)
    with pytest.raises(ActionError, match="one of the answers"):
        acts.answer("session:s1:ask9", "maybe")
    with pytest.raises(ActionError, match="written answer"):
        acts.answer("plan:p:approval1", "yes", "and more")
    with pytest.raises(ActionError, match="Choose"):
        acts.answer("plan:p:approval1", "", "")
    with pytest.raises(ActionError, match="several questions"):
        acts.answer("session:s4:card2", "x")
    with pytest.raises(ActionError) as e:
        acts.answer("session:nothing:x", "yes")
    assert e.value.code == "gone"
    assert acts._sessions.sent == []


def test_each_entry_is_announced_once_for_each_channel_and_a_resolved_one_is_forgotten(tmp_path):
    acts = make(tmp_path)
    ids = [r["id"] for r in acts.open()][:3]
    assert acts.view("android")["new"] == 12
    assert acts.mark_notified(ids, "android")["marked"] == ids
    view = acts.view("android")
    assert view["new"] == 9 and [r["id"] for r in view["actions"] if r["notified"]] == ids
    assert acts.view("web")["new"] == 12 and acts.view("desktop")["new"] == 12              # each channel on its own
    assert Actions(path=tmp_path / "n.json").view("android")["count"] == 0                   # the marks are kept on disk (nothing open here, so nothing shows)
    acts2 = make(tmp_path)
    assert acts2.view("android")["new"] == 9                                                   # after a restart of the server they stand
    acts.answer("session:s1:ask9", "confirm")
    acts.mark_notified([], "android")
    assert "session:s1:ask9" not in json.loads((tmp_path / "n.json").read_text())["android"]
    with pytest.raises(ActionError):
        acts.mark_notified(ids, "pager")
    with pytest.raises(ActionError):
        acts.mark_notified("x", "web")


def test_a_source_that_fails_does_not_hide_the_others(tmp_path):
    class Broken:
        def human_requests(self):
            raise RuntimeError("down")

        def list(self):
            raise RuntimeError("down")

    acts = Actions(sessions=Sessions(records()), plans=Broken(), routines=None, approvals=None, path=None)
    assert {r["source"] for r in acts.open()} == {"session"}
    assert Actions().view()["count"] == 0
