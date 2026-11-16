"""The one list of what waits for the person, against the real server (scripted mock engine): GET /api/actions, POST /api/actions/<id>/answer, the first answer wins, the count in /api/attention,
and what is announced once on a channel."""
import json
import unittest
import urllib.parse
from unittest import mock

from serve import handler
from serve.tests.test_api_plans import PlansApiBase


class ActionsApiTest(PlansApiBase):
    def setUp(self):
        handler._ACTIONS = None
        handler._APPROVALS = handler.ApprovalBoard()

    def make_plan(self):
        plan = {"plan_id": "listed", "title": "Listed", "target_type": "occasional", "nodes": {
            "ok": {"id": "ok", "type": "input", "title": "Approve", "assignee": "user", "dependencies": [], "outputs": ["go"],
                   "spec": {"ask": "approve", "prompt": "Is it good?", "prompt_en": "Is it fine?", "content": "The text", "into": "good"}},
            "go": {"id": "go", "type": "action", "title": "Go", "assignee": "agent", "dependencies": ["ok"], "outputs": []}}}
        self.assertEqual(self.call("POST", "/api/plans", {"plan": plan})[0], 201)

    def mine(self, channel=""):
        code, body = self.call("GET", "/api/actions" + (f"?channel={channel}" if channel else ""))
        self.assertEqual(code, 200)
        return body, {a["id"]: a for a in body["actions"]}

    def test_a_plan_question_and_a_board_approval_are_listed_answered_once_and_gone(self):
        self.make_plan()
        approval = handler._APPROVALS.request("run7", "shell", {"cmd": "ls"}, "reads")
        body, rows = self.mine("android")
        self.assertIn("plan:listed:ok", rows)
        self.assertEqual((rows["plan:listed:ok"]["kind"], rows["plan:listed:ok"]["title_en"], rows["plan:listed:ok"]["context"]), ("confirm", "Is it fine?", "Listed"))
        self.assertEqual(rows[f"approval:{approval['id']}"]["details"]["tool"], "shell")
        self.assertEqual(body["count"], len(rows))
        attention = self.call("GET", "/api/attention")[1]                                       # the real knowledge store: no archive has news
        self.assertEqual((attention["actions"]["count"], attention["rag"]["t"]), (body["count"], 0.0))
        quoted = urllib.parse.quote("plan:listed:ok", safe="")
        code, res = self.call("POST", f"/api/actions/{quoted}/answer", {"value": "maybe"})
        self.assertEqual((code, res["error"]["code"]), (400, "bad_answer"))
        code, res = self.call("POST", f"/api/actions/{quoted}/answer", {"value": "yes"})
        self.assertEqual((code, res["answered"]), (200, "plan:listed:ok"))
        code, res = self.call("POST", f"/api/actions/{quoted}/answer", {"value": "no"})
        self.assertEqual((code, res["error"]["code"]), (409, "gone"))                         # the first answer won
        self.assertEqual(self.call("GET", "/api/plans/listed")[1]["plan"]["shared_state"], {"good": True})
        code, res = self.call("POST", f"/api/actions/approval%3A{approval['id']}/answer", {"value": "approve"})
        self.assertEqual(code, 200)
        self.assertEqual(handler._APPROVALS.get(approval["id"])["status"], "approved")
        _, rows = self.mine()
        self.assertNotIn("plan:listed:ok", rows)
        self.assertFalse([a for a in rows.values() if a["source"] == "approval"])

    def test_an_entry_is_announced_once_on_a_channel(self):
        approval = handler._APPROVALS.request("run8", "shell", {"cmd": "pwd"})
        entry = f"approval:{approval['id']}"
        _, rows = self.mine("android")
        self.assertFalse(rows[entry]["notified"])
        code, res = self.call("POST", "/api/actions/notified", {"ids": [entry], "channel": "android"})
        self.assertEqual((code, res["marked"]), (200, [entry]))
        self.assertTrue(self.mine("android")[1][entry]["notified"])
        self.assertFalse(self.mine("web")[1][entry]["notified"])
        self.assertEqual(self.call("POST", "/api/actions/notified", {"ids": [entry], "channel": "pager"})[0], 400)
        handler._APPROVALS.decide(approval["id"], False)


if __name__ == "__main__":
    unittest.main()
