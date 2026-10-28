"""What a phone may start, and how long its key lasts: the PC fixes the most a conversation started from a phone may do without asking,
and a key ends on a date chosen when it was made."""
import json
import tempfile
import time
import types
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
from serve.remote import KEY_TTL_DAYS, ApiKeys, RemoteAccess
from serve.service import Service
from serve.tasks import ChangeJournal, TaskJournal
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
DAY = 86400


class KeyLifetimeTest(unittest.TestCase):
    def test_a_key_has_no_end_unless_another_end_or_never_is_chosen(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys = ApiKeys(Path(tmp) / "k.json")
            default, _ = keys.create("A")
            self.assertEqual((KEY_TTL_DAYS, default["expires"]), (0, None))         # no end unless the person chose one
            week, _ = keys.create("B", 7)
            self.assertAlmostEqual(week["expires"], time.time() + 7 * DAY, delta=60)
            never, _ = keys.create("C", 0)
            self.assertIsNone(never["expires"])
            for bad in (-1, "soon", True, 1.5e300, float("inf")):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    keys.create("D", bad)
            self.assertEqual([r["expires"] is None for r in keys.list()], [True, False, True])
            self.assertIsNotNone(keys.create("E", 36500)[0]["expires"])           # a very long end is the person's to choose

    def test_a_key_past_its_end_does_not_work_and_a_renewal_brings_it_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys = ApiKeys(Path(tmp) / "k.json")
            row, key = keys.create("Phone", 30)
            self.assertEqual(keys.verify(key)["id"], row["id"])
            keys._rows[0]["expires"] = time.time() - 5
            self.assertIsNone(keys.verify(key))
            self.assertEqual(keys.expired_label(key), "Phone")
            self.assertIsNone(keys.expired_label("bsk_other"))
            self.assertTrue(keys.list()[0]["expired"])
            renewed = keys.renew(row["id"], 30)
            self.assertFalse(renewed["expired"])
            self.assertEqual(keys.verify(key)["id"], row["id"])
            self.assertIsNone(keys.renew("zzzz", 30))
            self.assertIsNone(keys.renew(row["id"], 0)["expires"])               # renewed to never
            self.assertEqual(ApiKeys(Path(tmp) / "k.json").list()[0]["expires"], None)

    def test_a_key_made_before_expiry_existed_keeps_working_and_shows_no_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "k.json"
            keys = ApiKeys(path)
            row, key = keys.create("Old", 0)
            data = json.loads(path.read_text(encoding="utf-8"))
            for r in data["keys"]:
                r.pop("expires", None)
            path.write_text(json.dumps(data), encoding="utf-8")
            keys = ApiKeys(path)
            self.assertEqual(keys.verify(key)["id"], row["id"])
            self.assertIsNone(keys.list()[0]["expires"])


class PhoneLimitsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.svc.keys = ApiKeys(base / "keys.json", key=bytes(32))
        cls.svc.remote = RemoteAccess(base / "remote.json", 0, lambda ip, port: None)
        cls.patches = [mock.patch.object(handler, "_TASKS", TaskJournal(base / "j")), mock.patch.object(handler, "_CHANGES", ChangeJournal(base / "j")),
                       mock.patch.object(handler, "_PLANS", PlanService(base / "planner"))]
        for p in cls.patches:
            p.start()
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.phone = handler._listen(cls.svc, "127.0.0.1", 0, remote=True)
        cls.phone_base = f"http://127.0.0.1:{cls.phone.server_address[1]}"
        cls.row, cls.key = cls.svc.keys.create("Phone")
        cls.work = base / "work"
        cls.work.mkdir()
        cls.call("POST", "/api/agent/projects", {"path": str(cls.work)})

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.phone.shutdown()
        cls.phone.server_close()
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def setUp(self):
        self.svc.remote._httpd = types.SimpleNamespace(server_address=("100.64.0.1", 1))      # the listener for phones is up
        self.svc.api_key = ""
        self.svc.remote.set_limits("default", [], phone_chooses=False)

    @classmethod
    def call(cls, method, path, payload=None, key=None, outside=False):
        base = cls.phone_base if outside else cls.base
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        if key:
            headers["Authorization"] = "Bearer " + key
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        try:
            with urllib.request.urlopen(urllib.request.Request(base + path, data=data, headers=headers, method=method), timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def phone_post(self, path, payload):
        return self.call("POST", path, payload, key=self.key, outside=True)

    def phone_post_from_away(self, path, payload):
        """A phone is never on this PC; the test's own connection is, so the PC-only check is told otherwise."""
        with mock.patch.object(handler.settings, "is_loopback", lambda address: False):
            return self.phone_post(path, payload)

    def session(self, **extra):
        return {"workspace": str(self.work), "ask_only": True, **extra}

    def test_a_phone_cannot_start_a_conversation_in_a_mode_above_the_limit_the_pc_set(self):
        code, body = self.phone_post("/api/agent/sessions", self.session(mode="auto"))
        self.assertEqual((code, body["error"]["code"]), (403, "phone_mode_limit"))
        self.assertIn("Settings > Phones", body["error"]["message"])
        code, made = self.phone_post("/api/agent/sessions", self.session(mode="default"))
        self.assertEqual(code, 200)
        self.assertEqual(self.phone_post("/api/agent/sessions", self.session(mode="read_only"))[0], 200)
        self.svc.remote.set_limits("read_only", [])
        self.assertEqual(self.phone_post("/api/agent/sessions", self.session(mode="default"))[1]["error"]["code"], "phone_mode_limit")
        self.svc.remote.set_limits("auto", [])
        self.assertEqual(self.phone_post("/api/agent/sessions", self.session(mode="auto"))[0], 200)

    def test_switching_a_conversation_to_automatic_from_a_phone_is_refused_too(self):
        code, made = self.phone_post("/api/agent/sessions", self.session(mode="default", ask_only=False))
        sid = made["session"]["id"]
        code, body = self.phone_post(f"/api/agent/sessions/{sid}/mode", {"mode": "auto"})
        self.assertEqual((code, body["error"]["code"]), (403, "phone_mode_limit"))
        self.assertEqual(self.phone_post(f"/api/agent/sessions/{sid}/mode", {"mode": "read_only"})[0], 200)
        self.assertEqual(self.call("POST", f"/api/agent/sessions/{sid}/mode", {"mode": "auto"})[0], 200)       # this PC may

    def test_a_phone_cannot_attach_permission_rules_and_the_conversation_gets_the_rules_of_the_pc(self):
        code, body = self.phone_post("/api/agent/sessions", self.session(allow=["shell_command:*"]))
        self.assertEqual((code, body["error"]["code"]), (403, "phone_rules"))
        code, made = self.phone_post("/api/agent/sessions", self.session(allow=[]))
        self.assertEqual(code, 200)
        self.assertEqual(made["session"].get("allow", []), [])
        self.svc.remote.set_limits("default", ["shell_command:pytest *"])
        code, made = self.phone_post("/api/agent/sessions", self.session())
        self.assertEqual(made["session"]["allow"], ["shell_command:pytest *"])
        code, local = self.call("POST", "/api/agent/sessions", self.session(allow=["shell_command:git *"]))                       # the PC's own page: its own rules
        self.assertEqual(local["session"]["allow"], ["shell_command:git *"])

    def test_when_the_person_lets_phones_choose_the_phone_picks_its_mode_and_rules(self):
        code, view = self.call("POST", "/api/remote/limits", {"phone_chooses": True, "max_mode": "plan", "rules": []})
        self.assertEqual((code, view["limits"]["phone_chooses"]), (200, True))
        code, made = self.phone_post("/api/agent/sessions", self.session(mode="auto", ask_only=False, allow=["shell_command:git *"]))
        self.assertEqual((code, made["session"]["mode"], made["session"]["allow"]), (200, "auto", ["shell_command:git *"]))
        self.assertEqual(self.phone_post("/api/agent/sessions", self.session(mode="root", ask_only=False))[0], 400)
        self.assertTrue(RemoteAccess(Path(self.tmp.name) / "remote.json", 0, lambda ip, port: None).phone_chooses)

    def test_runs_started_from_a_phone_follow_the_limits_and_routines_are_always_automatic(self):
        run = {"workspace": str(self.work), "goal": "x", "mode": "auto"}
        self.assertEqual(self.phone_post("/api/agent/runs", run)[1]["error"]["code"], "phone_mode_limit")
        self.assertEqual(self.phone_post("/api/agent/runs", {**run, "mode": "default", "allow": ["shell_command:*"]})[1]["error"]["code"], "phone_rules")
        routine = {"name": "n", "prompt": "p", "schedule": "daily 08:30", "workspace": str(self.work)}
        code, made = self.phone_post("/api/routines", routine)
        self.assertEqual(code, 200)
        self.call("POST", f"/api/routines/{made['routine']['id']}/remove", {})

    def test_the_limits_are_set_from_the_pc_only_and_kept(self):
        code, view = self.call("POST", "/api/remote/limits", {"max_mode": "read_only", "rules": ["shell_command:pytest *"]})
        self.assertEqual((code, view["limits"]["max_mode"], view["limits"]["rules"]), (200, "read_only", ["shell_command:pytest *"]))
        self.assertEqual(view["limits"]["modes"], ["plan", "read_only", "default", "auto"])
        self.assertEqual(self.call("POST", "/api/remote/limits", {"max_mode": "root", "rules": []})[0], 400)
        self.assertEqual(self.call("POST", "/api/remote/limits", {"max_mode": "auto", "rules": ["x"] * 40})[0], 200)   # no cap on the number of rules
        self.assertEqual(self.call("POST", "/api/remote/limits", {"max_mode": "read_only", "rules": ["shell_command:pytest *"]})[0], 200)
        self.assertEqual(self.phone_post_from_away("/api/remote/limits", {"max_mode": "auto", "rules": []})[0], 403)
        self.assertEqual(RemoteAccess(Path(self.tmp.name) / "remote.json", 0, lambda ip, port: None).max_mode, "read_only")
        self.assertEqual(self.svc.remote.max_mode, "read_only")

    def test_keys_are_made_with_an_end_listed_with_it_renewed_and_an_expired_one_says_so(self):
        code, made = self.call("POST", "/api/remote/keys/create", {"label": "Tablet"})
        self.assertIsNone(made["row"]["expires"])
        self.assertEqual(made["limits"]["key_days"], KEY_TTL_DAYS)
        code, forever = self.call("POST", "/api/remote/keys/create", {"label": "Forever", "days": 0})
        self.assertIsNone(forever["row"]["expires"])
        self.assertEqual(self.call("POST", "/api/remote/keys/create", {"label": "Bad", "days": -3})[0], 400)
        tablet, key = made["row"]["id"], made["key"]
        self.assertEqual(self.call("GET", "/api/remote/hello", key=key, outside=True)[0], 200)
        next(r for r in self.svc.keys._rows if r["id"] == tablet)["expires"] = time.time() - 1
        code, body = self.call("GET", "/api/remote/hello", key=key, outside=True)
        self.assertEqual((code, body["error"]["code"]), (401, "key_expired"))
        self.assertIn("Tablet", body["error"]["message"])
        self.assertTrue(next(k for k in self.call("GET", "/api/remote")[1]["keys"] if k["id"] == tablet)["expired"])
        code, renewed = self.call("POST", "/api/remote/keys/renew", {"id": tablet, "days": 30})
        self.assertEqual((code, renewed["row"]["expired"]), (200, False))
        self.assertEqual(self.call("GET", "/api/remote/hello", key=key, outside=True)[0], 200)
        self.assertEqual(self.call("POST", "/api/remote/keys/renew", {"id": "nope", "days": 30})[0], 404)
        self.assertEqual(self.phone_post_from_away("/api/remote/keys/renew", {"id": tablet, "days": 0})[0], 403)             # a phone cannot renew its own key

    def test_a_phone_with_a_key_uses_personal_skills_and_answers_install_requests_but_changes_nothing_else_of_the_capabilities(self):
        from serve.capabilities_api import CapabilityApi
        from serve.personal_skills_api import PersonalSkillsApi
        base = Path(self.tmp.name)
        script = "def main(params):\n    return 1\n"
        with mock.patch.object(handler, "_PSKILLS", PersonalSkillsApi(base / "pskills")), mock.patch.object(handler, "_CAPS", CapabilityApi(base / "caps", base / "chat-ws")):
            self.assertEqual(self.phone_get_from_away("/api/personal-skills")[0], 200)
            code, made = self.phone_post_from_away("/api/personal-skills", {"skill": {"name": "Hello", "description": "d", "params": []}, "script": script})
            self.assertEqual(code, 200, made)
            self.assertEqual(self.phone_get_from_away("/api/personal-skills/" + made["skill"]["slug"])[0], 200)
            self.assertEqual(self.phone_post_from_away("/api/personal-skills/" + made["skill"]["slug"] + "/delete", {})[0], 200)
            self.assertEqual(self.phone_get_from_away("/api/capabilities/requests")[0], 200)
            self.assertEqual(self.phone_post_from_away("/api/capabilities/settings", {"off": []})[0], 200)
            self.assertEqual(self.phone_post_from_away("/api/capabilities/requests/reject", {"id": "nope"})[0], 409)       # reached the handler: no such request
            self.assertEqual(self.phone_post_from_away("/api/capabilities/remove", {"id": "x"})[0], 403)
            self.assertEqual(self.phone_post_from_away("/api/capabilities/move", {"id": "x", "node": "y"})[0], 403)

    def phone_get_from_away(self, path):
        with mock.patch.object(handler.settings, "is_loopback", lambda address: False):
            return self.call("GET", path, key=self.key, outside=True)


if __name__ == "__main__":
    unittest.main()
