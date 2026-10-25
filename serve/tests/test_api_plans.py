"""The planner's HTTP API against the real server (scripted mock engine, no GPU, no network)."""
import http.client
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from serve import handler
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.plans import PlanService
from serve.service import Service
from serve.tasks import ChangeJournal, TaskJournal
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]

CARD = {"titolo": "Weekend a Cervinia", "partecipanti": ["Io", "Gianni", "Diana"],
        "profilo_e_vincoli_partecipanti": {"Gianni": "Macchinista FS", "Diana": "Allergica agli ovini"}}
PLAN = {"plan_id": "cervinia", "title": "Weekend a Cervinia", "target_type": "occasional",
        "nodes": {
            "hotel": {"id": "hotel", "type": "action", "title": "Hotel", "description": "", "assignee": "agent",
                      "dependencies": [], "outputs": ["partenza"]},
            "partenza": {"id": "partenza", "type": "merge", "title": "Partenza", "description": "", "assignee": "agent",
                         "dependencies": ["hotel"], "outputs": []}}}
REPLAN = {"diagnosis": "Lo sciopero ferma il treno di Gianni.", "affected_nodes": ["partenza"],
          "alternatives": [{"title": "Parti in auto", "description": "Viaggio in auto",
                            "diff": {"update_nodes": {"partenza": {"title": "Partenza in auto"}}}}],
          "recommended": 0, "human_fallbacks": []}


class _NoTelemetry:
    def snapshot(self):
        return {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}


class PlansApiBase(unittest.TestCase):
    api_key = ""
    script = "</think>\n\nok"

    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.engine = MockEngine(tok, cls.script, max_context=32768)
        cls.svc = Service(cls.engine, tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.svc.telemetry = _NoTelemetry()
        cls.svc.api_key = cls.api_key
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.patches = [mock.patch.object(handler, "_TASKS", TaskJournal(base / "j")),
                       mock.patch.object(handler, "_CHANGES", ChangeJournal(base / "j")),
                       mock.patch.object(handler, "_PLANS", PlanService(base / "planner"))]
        for p in cls.patches:
            p.start()
        cls.httpd = serve(cls.svc, port=0)
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def call(self, method, path, payload=None, headers=None, raw=None):
        data = raw if raw is not None else (None if payload is None else json.dumps(payload).encode("utf-8"))
        hdrs = {"Content-Type": "application/json"} if method == "POST" else {}
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def wait_event(self, plan_id, kind, after=0, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, data = self.call("GET", f"/api/plans/{plan_id}/events?after={after}")
            for e in data["events"]:
                if e["type"] == kind:
                    return e
            time.sleep(0.1)
        self.fail(f"no {kind} event arrived")


class PlansApiTest(PlansApiBase):
    script = "</think>\n\n" + json.dumps(REPLAN)

    def test_create_list_get_and_the_errors(self):
        code, snap = self.call("POST", "/api/plans", {"plan": PLAN, "card": CARD})
        self.assertEqual(code, 201)
        self.assertEqual(snap["plan"]["plan_id"], "cervinia")
        self.assertEqual(snap["ready"], ["hotel"])
        self.assertEqual(snap["layers"], [["hotel"], ["partenza"]])
        self.assertEqual(self.call("POST", "/api/plans", {"plan": PLAN})[0], 400)           # already exists
        self.assertIn("cervinia", [p["plan_id"] for p in self.call("GET", "/api/plans")[1]["plans"]])
        self.assertEqual(self.call("GET", "/api/plans/cervinia")[1]["plan"]["title"], "Weekend a Cervinia")
        self.assertEqual(self.call("GET", "/api/plans/missing")[0], 404)
        self.assertEqual(self.call("GET", "/api/plans/..%2F..%2Fx")[0], 400)
        bad = json.loads(json.dumps(PLAN))
        bad["plan_id"] = "broken"
        bad["nodes"]["hotel"]["outputs"] = []                                               # one-sided edge
        self.assertEqual(self.call("POST", "/api/plans", {"plan": bad})[0], 400)
        self.assertEqual(self.call("POST", "/api/plans", raw=b"[1]")[0], 400)
        self.assertEqual(self.call("POST", "/api/plans", {"plan": {"title": "no id"}})[0], 400)

    def test_the_plan_as_a_mermaid_diagram(self):
        self.call("POST", "/api/plans", {"plan": {**PLAN, "plan_id": "mermaid-one"}, "card": CARD})
        code, body = self.call("GET", "/api/plans/mermaid-one/mermaid")
        self.assertEqual(code, 200)
        self.assertTrue(body["mermaid"].startswith("%% Weekend a Cervinia\nflowchart TD\n"))
        self.assertIn("n_hotel --> n_partenza", body["mermaid"])
        self.assertEqual(self.call("GET", "/api/plans/missing/mermaid")[0], 404)

    def test_events_edit_the_plan_are_saved_and_can_be_followed(self):
        plan = {**PLAN, "plan_id": "edits"}
        self.call("POST", "/api/plans", {"plan": plan, "card": CARD})
        code, res = self.call("POST", "/api/plans/edits/events",
                              {"type": "plan:update_node", "node_id": "hotel", "changes": {"title": "Hotel Cervinia"}})
        self.assertEqual((code, res["ok"]), (200, True))
        code, res = self.call("POST", "/api/plans/edits/events", {"type": "plan:update_node", "node_id": "hotel",
                                                                  "changes": {"status": "completed"}})
        self.assertEqual(code, 400)
        self.assertIn("cannot edit", res["error"])
        self.assertEqual(self.call("POST", "/api/plans/edits/events", {"type": "plan:nope"})[0], 400)
        self.assertEqual(self.call("POST", "/api/plans/ghost/events", {"type": "plan:get"})[0], 404)
        _, ev = self.call("GET", "/api/plans/edits/events?after=0")
        self.assertEqual([e["type"] for e in ev["events"]], ["plan:node_updated"])
        self.assertEqual(self.call("GET", f"/api/plans/edits/events?after={ev['seq']}")[1]["events"], [])
        fresh = PlanService(Path(self.tmp.name) / "planner")                                  # what was saved to disk
        self.assertEqual(fresh.snapshot("edits")["plan"]["nodes"]["hotel"]["title"], "Hotel Cervinia")

    def test_the_event_stream_replays_the_past_and_delivers_the_future(self):
        plan = {**PLAN, "plan_id": "stream"}
        self.call("POST", "/api/plans", {"plan": plan, "card": CARD})
        self.call("POST", "/api/plans/stream/events", {"type": "plan:update_node", "node_id": "hotel", "changes": {"title": "uno"}})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/api/plans/stream/stream?after=0")
        resp = conn.getresponse()
        self.assertEqual((resp.status, resp.getheader("Content-Type")), (200, "text/event-stream"))

        def read_event():
            fields = {}
            while True:
                line = resp.readline().decode("utf-8").rstrip("\n")
                if line == "" and fields:
                    return fields
                if line.startswith(":") or not line:
                    continue
                key, _, value = line.partition(": ")
                fields[key] = value

        first = read_event()
        self.assertEqual((first["id"], first["event"]), ("1", "plan:node_updated"))
        threading.Timer(0.2, lambda: self.call("POST", "/api/plans/stream/events",
                                               {"type": "plan:update_node", "node_id": "hotel", "changes": {"title": "due"}})).start()
        second = read_event()
        self.assertEqual(second["id"], "2")
        self.assertEqual(json.loads(second["data"])["node"]["title"], "due")
        conn.close()

    def test_proposals_are_approved_through_the_api(self):
        plan = {**PLAN, "plan_id": "approve"}
        self.call("POST", "/api/plans", {"plan": plan, "card": CARD})
        _, res = self.call("POST", "/api/plans/approve/events", {
            "type": "plan:propose_diff", "title": "Rinomina", "diff": {"update_nodes": {"partenza": {"title": "Partenza sabato"}}}})
        pid = res["proposal_id"]
        self.assertEqual(self.call("GET", "/api/plans/approve")[1]["proposals"][0]["proposal_id"], pid)
        self.assertEqual(self.call("GET", "/api/plans/approve")[1]["plan"]["nodes"]["partenza"]["title"], "Partenza")
        self.assertTrue(self.call("POST", "/api/plans/approve/events", {"type": "plan:approve", "proposal_id": pid})[1]["ok"])
        self.assertEqual(self.call("GET", "/api/plans/approve")[1]["plan"]["nodes"]["partenza"]["title"], "Partenza sabato")

    def test_triage_without_consent_stays_local_and_a_wake_can_replan_with_the_model(self):
        plan = {**PLAN, "plan_id": "triage"}
        self.call("POST", "/api/plans", {"plan": plan, "card": CARD})
        code, quiet = self.call("POST", "/api/plans/triage/triage", {"events": ["Nuovo museo a Parigi"]})
        self.assertEqual(code, 200)
        self.assertEqual((quiet["wake_system_2"], quiet["source"], quiet["sent_to_cloud"]), (False, "offline", False))
        self.assertEqual(self.call("POST", "/api/plans/triage/triage", {"events": []})[0], 400)
        self.assertEqual(self.call("POST", "/api/plans/triage/triage", {"events": ["x"] * 101})[0], 400)
        self.assertEqual(self.call("POST", "/api/plans/triage/triage", {"events": "Sciopero"})[0], 400)

        code, loud = self.call("POST", "/api/plans/triage/triage", {"events": ["Sciopero dei treni"], "replan": True})
        self.assertEqual((loud["wake_system_2"], loud["replanning"]), (True, True))
        ready = self.wait_event("triage", "plan:replan_ready")
        self.assertEqual(len(ready["proposals"]), 1)
        snap = self.call("GET", "/api/plans/triage")[1]
        self.assertEqual(snap["proposals"][0]["title"], "Parti in auto")
        self.assertEqual(snap["plan"]["nodes"]["partenza"]["title"], "Partenza")             # nothing applied by itself
        self.assertEqual(self.wait_event("triage", "plan:triage")["result"]["source"], "offline")

    def test_a_plan_without_a_card_cannot_be_triaged(self):
        self.call("POST", "/api/plans", {"plan": {**PLAN, "plan_id": "nocard"}})
        code, res = self.call("POST", "/api/plans/nocard/triage", {"events": ["Sciopero"]})
        self.assertEqual(code, 400)
        self.assertIn("no card", res["error"]["message"])

    def test_settings_need_the_own_page_and_never_reveal_the_key(self):
        _, s = self.call("GET", "/api/planner/settings")
        self.assertEqual((s["cloud_triage"], s["pseudonymize"]), (False, True))
        self.assertNotIn("sk-", json.dumps(s))
        with mock.patch.dict("os.environ", {"TYPESAFE_API_KEY": "sk-secret-value"}):
            _, s = self.call("GET", "/api/planner/settings")
            self.assertTrue(s["key_present"])
            self.assertNotIn("sk-secret-value", json.dumps(s))
        self.assertEqual(self.call("POST", "/api/planner/settings", {"cloud_triage": "yes"})[0], 400)
        self.assertEqual(self.call("POST", "/api/planner/settings", {"api_key": "sk-x"})[0], 400)
        self.assertEqual(self.call("POST", "/api/planner/settings", {"card_fields": ["segreto"]})[0], 400)
        self.assertNotIn("profilo_e_vincoli_partecipanti", s["card_fields_default"])               # health and allergies stay home unless chosen
        self.assertEqual(s["card_fields_personal"], ["profilo_e_vincoli_partecipanti"])
        self.assertIsNone(s["card_fields"])
        self.assertEqual(self.call("POST", "/api/planner/settings", {"cloud_triage": True},
                                   headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.call("POST", "/api/planner/settings", {"cloud_triage": True},
                                   headers={"Content-Type": "text/plain"})[0], 415)
        code, s = self.call("POST", "/api/planner/settings", {"cloud_triage": True, "card_fields": ["titolo"]})
        self.assertEqual((code, s["cloud_triage"], s["card_fields"]), (200, True, ["titolo"]))
        try:
            with mock.patch.dict("os.environ", {"TYPESAFE_API_KEY": "sk-secret-value"}):
                gk = handler._PLANS.gatekeeper()
                self.assertTrue(gk.cloud_allowed and gk.card_fields == ["titolo"])
        finally:
            self.call("POST", "/api/planner/settings", {"cloud_triage": False, "card_fields": None})
        with mock.patch.dict("os.environ", {"TYPESAFE_API_KEY": "sk-secret-value"}):
            self.assertFalse(handler._PLANS.gatekeeper().cloud_allowed)                       # a key without consent

    def test_the_mailbox_of_events_and_the_telemetry_are_over_http(self):
        sensor = {"plan_id": "snow-http", "title": "Neve", "target_type": "occasional",
                  "nodes": {"meteo": {"id": "meteo", "type": "sensor", "title": "Neve", "description": "", "assignee": "agent", "dependencies": [], "outputs": [],
                                      "conditions": {"when": "'nev' in text", "set": {"neve": True}}}}}
        self.assertEqual(self.call("POST", "/api/plans", {"plan": sensor})[0], 201)
        code, res = self.call("POST", "/api/plans/events", {"text": "Sole a Cervinia", "source": "meteo"})
        self.assertEqual((code, res["accepted"], res["resolved"].get("snow-http"), res["pump"]), (200, 1, None, False))
        code, res = self.call("POST", "/api/plans/events", {"events": [{"text": "Nevicata a Cervinia", "source": "meteo"}], "actor": "system"})
        self.assertEqual((code, res["resolved"]["snow-http"]), (200, ["meteo"]))
        self.assertEqual(self.call("GET", "/api/plans/snow-http")[1]["plan"]["shared_state"], {"neve": True})
        self.assertEqual(self.call("POST", "/api/plans/events", {"text": ""})[0], 400)
        self.assertEqual(self.call("POST", "/api/plans/events", {"text": "x", "plan_id": "nonesiste"})[0], 404)
        self.assertEqual(self.call("POST", "/api/plans/events", {"text": "x"}, headers={"Origin": "http://evil.example"})[0], 403)
        code, tele = self.call("GET", "/api/plans/telemetry")
        self.assertEqual(code, 200)
        self.assertEqual(set(tele), {"triage", "replanning", "events"})
        self.assertEqual((tele["events"]["pump"], tele["events"]["accepted"] >= 2), (False, True))
        self.assertGreaterEqual(tele["triage"]["calls"], 0)
        self.assertEqual(self.call("POST", "/api/planner/settings", {"gate_mode": "xor"})[0], 400)
        self.assertEqual(self.call("POST", "/api/planner/settings", {"event_pump": "on"})[0], 400)
        self.assertEqual(self.call("POST", "/api/planner/settings", {"sources": [{"kind": "telegram", "secret_entry": "t"}]})[0], 400)
        for reserved in ("events", "telemetry"):
            self.assertEqual(self.call("POST", "/api/plans", {"plan": {"plan_id": reserved, "title": "x", "target_type": "occasional", "nodes": {}}})[0], 400)

    def test_changes_are_recorded_validated_and_cleared(self):
        self.call("POST", "/api/changes", {"clear": True})
        edit = {"file": "src/a.py", "kind": "modified", "added": 2, "removed": 1, "diff": "-a\n+b\n+c\n"}
        code, data = self.call("POST", "/api/changes", {"edits": [edit]})
        self.assertEqual((code, data["edits"][0]["file"]), (200, "src/a.py"))
        self.assertEqual(len(self.call("GET", "/api/changes")[1]["edits"]), 1)
        for bad in ({"edits": [{"kind": "added"}]}, {"edits": [{"file": "a", "kind": "renamed"}]},
                    {"edits": [{"file": "a", "added": "many"}]}, {"edits": "x"}):
            self.assertEqual(self.call("POST", "/api/changes", bad)[0], 400)
        self.assertEqual(self.call("POST", "/api/changes", {"edits": [edit]}, headers={"Origin": "http://evil.example"})[0], 403)
        self.call("POST", "/api/changes", {"clear": True})
        self.assertEqual(self.call("GET", "/api/changes")[1]["edits"], [])


    def test_a_plan_is_archived_not_erased_and_can_be_restored(self):
        plan = {**PLAN, "plan_id": "arch1"}
        code, snap = self.call("POST", "/api/plans", {"plan": plan, "card": CARD, "actor": "chat"})
        self.assertEqual((code, snap["rev"], snap["history"][0]["actor"], snap["history"][0]["type"]), (201, 1, "chat", "create"))
        self.assertEqual(self.call("POST", "/api/plans/arch1/archive", {"actor": "root"})[0], 400)
        self.assertEqual(self.call("POST", "/api/plans/arch1/archive", {}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.call("POST", "/api/plans/arch1/archive", {})[1]["archived"], "arch1")
        self.assertNotIn("arch1", [p["plan_id"] for p in self.call("GET", "/api/plans")[1]["plans"]])
        self.assertIn("arch1", [p["plan_id"] for p in self.call("GET", "/api/plans?archived=1")[1]["plans"]])
        self.assertEqual(self.call("GET", "/api/plans/arch1")[0], 404)
        code, again = self.call("POST", "/api/plans", {"plan": plan})
        self.assertEqual(code, 400)
        self.assertIn("archive", again["error"]["message"])
        self.assertEqual(self.call("POST", "/api/plans/arch1/archive", {})[0], 404)
        code, back = self.call("POST", "/api/plans/arch1/restore", {})
        self.assertEqual(code, 200)
        self.assertEqual([h["type"] for h in back["history"]], ["create", "archive", "restore"])
        self.assertIn("arch1", [p["plan_id"] for p in self.call("GET", "/api/plans")[1]["plans"]])
        self.assertEqual(self.call("POST", "/api/plans/arch1/restore", {})[0], 404)

    def test_edits_carry_the_revision_the_actor_and_the_plans_own_details(self):
        self.call("POST", "/api/plans", {"plan": {**PLAN, "plan_id": "rev1"}, "card": CARD})
        ev = {"type": "plan:update_node", "node_id": "hotel", "changes": {"title": "Hotel Cervino"}}
        self.assertTrue(self.call("POST", "/api/plans/rev1/events", {**ev, "base_rev": 1, "actor": "chat"})[1]["ok"])
        code, late = self.call("POST", "/api/plans/rev1/events", {**ev, "base_rev": 1})
        self.assertEqual((code, late["stale"], late["rev"]), (400, True, 2))
        snap = self.call("GET", "/api/plans/rev1")[1]
        self.assertEqual((snap["rev"], snap["history"][-1]["actor"]), (2, "chat"))
        meta = {"type": "plan:update_meta", "changes": {"title": "Cervinia", "target_type": "constant", "card": {"titolo": "Cervinia"}}}
        self.assertTrue(self.call("POST", "/api/plans/rev1/events", meta)[1]["ok"])
        snap = self.call("GET", "/api/plans/rev1")[1]
        self.assertEqual((snap["plan"]["title"], snap["plan"]["target_type"], snap["card"]["titolo"]), ("Cervinia", "constant", "Cervinia"))
        self.assertEqual(self.call("POST", "/api/plans/rev1/events", {"type": "plan:update_meta", "changes": {"title": ""}})[0], 400)


    def test_plans_carry_what_they_say_about_time_and_money_and_the_comparison_is_over_http(self):
        long_plan = {"plan_id": "long-health", "title": "Health", "target_type": "constant", "nodes": {}}
        short_plan = {"plan_id": "short-ski", "title": "Ski weekend", "target_type": "occasional", "nodes": {}}
        code, snap = self.call("POST", "/api/plans", {"plan": long_plan, "profile": {"goals": ["Heal the knee"], "review_every_days": 7,
                                                                                    "commitments": [{"text": "Physio", "from": "2031-03-01", "to": "2031-03-31"}]}})
        self.assertEqual(code, 201)
        self.assertEqual(snap["profile"]["goals"], ["Heal the knee"])
        self.assertTrue(snap["review_due"])
        self.assertEqual(self.call("POST", "/api/plans", {"plan": short_plan, "profile": {"starts_on": "tomorrow"}})[0], 400)            # a bad profile is refused
        self.call("POST", "/api/plans", {"plan": short_plan, "profile": {"starts_on": "2031-03-10", "ends_on": "2031-03-12"}})
        code, found = self.call("GET", "/api/plans/short-ski/alignment")
        self.assertEqual(code, 200)
        self.assertEqual([c["kind"] for c in found["conflicts"]], ["time"])
        self.assertEqual(found["checks"], [])
        rows = {r["plan_id"]: r for r in self.call("GET", "/api/plans")[1]["plans"]}
        self.assertEqual((rows["short-ski"]["target_type"], len(rows["short-ski"]["conflicts"])), ("occasional", 1))
        self.assertTrue(rows["long-health"]["review_due"])
        code, found = self.call("POST", "/api/plans/short-ski/alignment", {"model": True})                # the mock engine answers "ok": nothing usable, no checks
        self.assertEqual((code, found["checks"]), (200, []))
        self.assertTrue(found["checked_at"])
        self.assertEqual(self.call("GET", "/api/plans/missing/alignment")[0], 404)
        code, res = self.call("POST", "/api/plans/short-ski/events", {"type": "plan:update_meta", "changes": {"profile": {"starts_on": "2031-05-01", "ends_on": "2031-05-02"}}})
        self.assertEqual((code, res["ok"]), (200, True))
        self.assertEqual(self.call("GET", "/api/plans/short-ski/alignment")[1]["conflicts"], [])

class PlansApiKeyedTest(PlansApiBase):
    api_key = "s3cret"
    auth = {"Authorization": "Bearer s3cret"}

    def test_every_planner_route_needs_the_key(self):
        for method, path in (("GET", "/api/plans"), ("GET", "/api/plans/x"), ("GET", "/api/plans/x/events"),
                             ("GET", "/api/plans/x/stream"), ("GET", "/api/planner/settings"), ("POST", "/api/plans"),
                             ("POST", "/api/plans/x/events"), ("POST", "/api/plans/x/triage"), ("POST", "/api/planner/settings"),
                             ("POST", "/api/plans/events"), ("GET", "/api/plans/telemetry"),
                             ("GET", "/api/plans?archived=1"), ("POST", "/api/plans/x/archive"), ("POST", "/api/plans/x/restore"),
                             ("POST", "/api/changes"), ("GET", "/api/changes")):
            self.assertEqual(self.call(method, path, {} if method == "POST" else None)[0], 401, (method, path))
        self.assertEqual(self.call("GET", "/api/plans", headers=self.auth)[0], 200)
        self.assertEqual(self.call("POST", "/api/plans", {"plan": PLAN, "card": CARD}, headers=self.auth)[0], 201)


if __name__ == "__main__":
    unittest.main()
