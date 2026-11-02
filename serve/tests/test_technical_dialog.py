"""The technical session's extended dialog, end to end on the server side: every use, its switch, numbers and prompt over HTTP, what is kept
across a restart, which uses are made without a session, and the choice of where the quick jobs run (this PC's session, a second small model
on this PC, a provider outside it) with the master switch and the consent it needs. The page itself is shown in QUEUE_daytime.md."""
import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from harness import side, side_uses
from harness.llm.client import RoutedClient
from harness.llm.credentials import FileCredentials
from harness.llm.store import Ledger, SecureRegistry
from serve import handler
from serve.llm_api import LlmApi
from serve.tests import test_api_plans as base

WINDOW = 30000


class SmallModel:
    """A second small model on this PC (LM Studio, vLLM or llama-server speak this): records what it is sent."""

    def __init__(self):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append({"path": self.path, "body": body})
                raw = json.dumps({"choices": [{"message": {"role": "assistant", "content": "short answer"}, "finish_reason": "stop"}],
                                  "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Dialog(base.PlansApiBase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"BASALT_DATA": cls.data.name})
        cls.env.start()
        cls.reg_dir = Path(cls.data.name) / "registry"
        cls.reg_dir.mkdir()
        cls.registry = SecureRegistry(cls.reg_dir / "p.json", credentials=FileCredentials(cls.reg_dir / "c.json"))
        cls.llm_patch = mock.patch.object(handler, "_LLM", LlmApi(cls.registry))
        cls.llm_patch.start()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.llm_patch.stop()
        cls.env.stop()
        cls.data.cleanup()

    def setUp(self):
        side_uses.reset()
        side_uses.set_external(False)
        side_uses.set_session(True)
        self.call("POST", "/api/llm/assign", {"function": "technical", "provider": None})
        for pid in [p["id"] for p in self.registry.load()["providers"]]:
            self.registry.remove(pid)
        self.small = SmallModel()
        self.addCleanup(self.small.close)

    def session(self, on=True):
        """The server reports a technical session of 30000 tokens (the scripted engine has none of its own)."""
        patch = mock.patch.object(self.svc, "side_slot_info", create=True, return_value={"slot": 1, "context": WINDOW} if on else None)
        patch.start()
        self.addCleanup(patch.stop)

    def view(self):
        code, v = self.call("GET", "/api/side-uses")
        self.assertEqual(code, 200)
        return v

    def use(self, v, name):
        return next(u for u in v["uses"] if u["id"] == name)

    # -- the uses ----------------------------------------------------------------------------------------

    def test_every_use_is_listed_with_its_texts_and_the_dialog_is_available_only_with_a_session(self):
        v = self.view()
        self.assertGreaterEqual(len(v["uses"]), 18)
        self.assertEqual([u["id"] for u in v["uses"]], [u.id for u in side_uses.USES])
        self.assertEqual((v["session"]["available"], v["session"]["tokens"]), (False, 0))
        self.session()
        v = self.view()
        self.assertEqual((v["session"]["available"], v["session"]["tokens"], v["session"]["local"], v["session"]["on_pc"]), (True, WINDOW, True, False))
        for u in v["uses"]:
            self.assertEqual(u["on"], side_uses.BY_ID[u["id"]].default_on, u["id"])
            self.assertIn("prompt", u)
            self.assertTrue(u["prompt"] or not side_uses.BY_ID[u["id"]].prompt, u["id"])         # a use that has a shipped prompt shows it

    def test_every_use_switches_off_and_on_and_every_number_round_trips_and_is_checked(self):
        for spec in side_uses.USES:
            for wanted in (not spec.default_on, spec.default_on):
                code, v = self.call("POST", "/api/side-uses", {"use": spec.id, "on": wanted})
                self.assertEqual((code, self.use(v, spec.id)["on"], side_uses.enabled(spec.id)), (200, wanted, wanted), spec.id)
            for p in spec.params:
                mid = (p.lo + p.hi) / 2
                value = int(mid) if p.integer else float(mid)
                code, v = self.call("POST", "/api/side-uses", {"use": spec.id, "params": {p.key: value}})
                shown = next(x for x in self.use(v, spec.id)["params"] if x["key"] == p.key)
                self.assertEqual((code, shown["value"]), (200, value), (spec.id, p.key))
                self.assertEqual(side_uses.param(spec.id, p.key), value)
                for bad in (p.hi + 1, p.lo - 1, "7", None, True):
                    self.assertEqual(self.call("POST", "/api/side-uses", {"use": spec.id, "params": {p.key: bad}})[0], 400, (spec.id, p.key, bad))
                self.assertEqual(side_uses.param(spec.id, p.key), value)                                 # a refused number changed nothing
            self.assertEqual(self.call("POST", "/api/side-uses", {"use": spec.id, "params": {"nothing": 1}})[0], 400)
        self.assertEqual(self.call("POST", "/api/side-uses", {"reset": "all"})[0], 200)
        self.assertTrue(all(not u["changed"] for u in self.view()["uses"]))

    def test_only_the_uses_that_answer_in_free_text_take_a_new_prompt_and_restoring_gives_the_shipped_one_back(self):
        for spec in side_uses.USES:
            code, v = self.call("POST", "/api/side-uses", {"use": spec.id, "system": "Use three words."})
            if spec.editable:
                row = self.use(v, spec.id)
                self.assertEqual((code, row["prompt"], row["prompt_changed"]), (200, "Use three words.", True), spec.id)
                self.assertEqual(side_uses.system(spec.id, "shipped"), "Use three words.")
                code, v = self.call("POST", "/api/side-uses", {"reset": spec.id})
                self.assertEqual(self.use(v, spec.id)["prompt"], row["prompt_default"])
            else:
                self.assertEqual(code, 400, spec.id)                                                     # a prompt the harness reads by its form is fixed
                self.assertEqual(side_uses.system(spec.id, "shipped"), "shipped")
        self.assertEqual(self.call("POST", "/api/side-uses", {"use": "chat_abstract", "system": "x" * (side_uses.MAX_SYSTEM_CHARS + 1)})[0], 400)

    def test_what_the_person_set_is_kept_in_a_file_that_another_process_reads_and_a_reset_keeps_the_master_switch(self):
        self.call("POST", "/api/side-uses", {"use": "judge", "on": False})
        self.call("POST", "/api/side-uses", {"use": "triage", "params": {"timeout_s": 77}})
        self.call("POST", "/api/side-uses", {"external": True})
        on_disk = json.loads(side_uses.file_path().read_text(encoding="utf-8"))
        self.assertEqual((on_disk["judge"]["on"], on_disk["triage"]["params"]["timeout_s"], on_disk["_external"]["on"]), (False, 77, True))
        side_uses._cache.update(key=None, values={})                                                    # a process that starts later reads the file
        self.assertEqual((side_uses.enabled("judge"), side_uses.param("triage", "timeout_s"), side_uses.external_on()), (False, 77, True))
        self.call("POST", "/api/side-uses", {"reset": "all"})
        self.assertEqual((side_uses.enabled("judge"), side_uses.external_on()), (True, True))
        self.call("POST", "/api/side-uses", {"external": False})

    def test_a_use_that_needs_the_session_is_not_made_without_one_and_a_switched_off_use_runs_in_an_ordinary_slot(self):
        class Client:
            def __init__(self, tokens):
                self.tokens = tokens

            def side_context(self):
                return self.tokens
        for spec in side_uses.USES:
            self.assertEqual(side.side_extra(Client(WINDOW), spec.id), {} if not spec.default_on else {"cache_slot": "side", "side_use": spec.id}, spec.id)
            self.call("POST", "/api/side-uses", {"use": spec.id, "on": True})                           # the uses that start off are switched on by the person
            self.assertEqual(side.side_extra(Client(0), spec.id), {}, spec.id)                          # no session: no cache_slot
            self.assertEqual(side.side_extra(Client(WINDOW), spec.id), {"cache_slot": "side", "side_use": spec.id}, spec.id)
            self.call("POST", "/api/side-uses", {"use": spec.id, "on": False})
            self.assertEqual(side.side_extra(Client(WINDOW), spec.id), {}, spec.id)                     # off: the call is an ordinary one
            self.assertEqual(side.side_context(Client(WINDOW), spec.id), 0, spec.id)
        self.assertEqual(side.side_extra(Client(WINDOW), "triage", only=True), {})                      # triage is off now
        self.call("POST", "/api/side-uses", {"use": "triage", "on": True})
        self.assertEqual(side.side_extra(Client(WINDOW), "triage", only=True), {"cache_slot": "side_only", "side_use": "triage"})

    def test_the_jobs_running_are_listed_and_stop_reaches_them_over_http(self):
        job = side_uses.job("longform", "a long text")
        job.progress(3, 8)
        self.addCleanup(job.close)
        v = self.view()
        self.assertEqual([(r["use"], r["done"], r["total"], r["label"]) for r in v["running"]], [("longform", 3, 8, "a long text")])
        self.call("POST", "/api/side-uses", {"stop": True})
        self.assertTrue(job.stopped())

    # -- where the quick jobs run ----------------------------------------------------------------------

    def test_a_second_small_model_on_this_pc_is_chosen_in_one_step_kept_and_needs_neither_consent_nor_the_master_switch(self):
        code, out = self.call("POST", "/api/llm/technical", {"type": "lmstudio", "model": "qwen-small", "base_url": self.small.url, "context_window": 4096})
        self.assertEqual(code, 200, out)
        self.assertEqual((out["provider"]["id"], out["provider"]["model"], out["provider"]["context_window"]), ("technical-lmstudio", "qwen-small", 4096))
        v = self.view()
        self.assertEqual((v["route"]["provider"], v["route"]["ok"], v["external"]["on"]), ("technical-lmstudio", True, False))
        self.assertEqual((v["session"]["available"], v["session"]["tokens"], v["session"]["local"], v["session"]["on_pc"]), (True, 4096, False, True))
        row = next(f for f in self.call("GET", "/api/llm")[1]["functions"] if f["id"] == "technical")
        self.assertEqual(row["provider"], "technical-lmstudio")
        again = SecureRegistry(self.reg_dir / "p.json", credentials=FileCredentials(self.reg_dir / "c.json"))        # a restart reads the file
        self.assertEqual(again.load()["assignments"]["technical"], "technical-lmstudio")
        self.assertEqual(again.load()["assignments"].get("executor"), None)                                           # the conversation's own function is untouched
        code, out = self.call("POST", "/api/llm/technical", {"type": "lmstudio", "model": "qwen-small-2", "base_url": self.small.url})
        self.assertEqual((code, out["provider"]["model"], out["provider"]["context_window"]), (200, "qwen-small-2", 8192))      # same provider, updated; window defaults
        self.assertEqual([p["id"] for p in self.registry.load()["providers"]].count("technical-lmstudio"), 1)
        self.call("POST", "/api/llm/assign", {"function": "technical", "provider": None})
        self.assertFalse(self.view()["session"]["available"])                                                           # back to this PC's own session (none here)

    def test_the_quick_jobs_reach_the_small_model_without_the_slot_keys_and_the_window_is_the_one_stated(self):
        self.call("POST", "/api/llm/technical", {"type": "llamacpp", "model": "tiny", "base_url": self.small.url, "context_window": 6000})
        client = RoutedClient("technical", self.registry, mock.Mock(side_context=lambda: 0), Ledger(self.reg_dir / "u.json"))
        self.assertTrue(client.stays_on_pc() and not client.stays_local())
        self.assertEqual(side.side_context(client, "triage"), 6000)
        extra = side.side_extra(client, "triage")
        out = client.chat_completion([{"role": "user", "content": "hi"}], extra=extra, max_tokens=20)
        self.assertEqual(out["choices"][0]["message"]["content"], "short answer")
        sent = self.small.requests[-1]
        self.assertEqual((sent["path"], sent["body"]["model"]), ("/v1/chat/completions", "tiny"))
        self.assertNotIn("cache_slot", sent["body"])
        self.assertNotIn("side_use", sent["body"])
        self.assertGreater(len(side.side_extra_for(client, "compaction", 2000, 500)), 0)                                  # a text that fits is sent
        self.assertEqual(side.side_extra_for(client, "compaction", 200000, 500), {})                                    # one that does not stays out
        executor = RoutedClient("executor", self.registry, mock.Mock(side_context=lambda: 0), Ledger(self.reg_dir / "u.json"))
        self.assertTrue(executor.stays_local())                                                                       # the conversation is not routed there

    def test_llama_server_started_from_the_loader_supplies_the_address_the_model_and_the_window(self):
        from serve import llamacpp
        fake = mock.Mock()
        fake.status.return_value = {"state": "ready", "base_url": self.small.url, "model_name": "loader-model", "context_per_slot": 5000}
        with mock.patch.object(llamacpp, "get", return_value=fake):
            code, out = self.call("POST", "/api/llm/technical", {"type": "llamacpp"})
        self.assertEqual((code, out["provider"]["model"], out["provider"]["context_window"]), (200, "loader-model", 5000))
        fake.status.return_value = {"state": "stopped", "base_url": "", "model_name": "", "context_per_slot": None}
        with mock.patch.object(llamacpp, "get", return_value=fake):
            code, out = self.call("POST", "/api/llm/technical", {"type": "llamacpp"})
        self.assertEqual((code, out["error"]["code"]), (400, "bad_model"))

    def test_a_bad_choice_is_refused_with_a_code_and_changes_nothing(self):
        for body, code in (({"type": "openai", "model": "x"}, "bad_type"), ({"type": "lmstudio"}, "bad_model"), ({"type": "lmstudio", "model": "  "}, "bad_model"),
                           ({"type": "lmstudio", "model": "m", "context_window": "big"}, "bad_context_window"), ({}, "bad_type")):
            status, out = self.call("POST", "/api/llm/technical", body)
            self.assertEqual((status, out["error"]["code"]), (400, code), body)
        self.assertEqual(self.registry.load()["providers"], [])
        status, out = self.call("POST", "/api/llm/technical", {"type": "lmstudio", "model": "m", "context_window": 999999})
        self.assertEqual((status, out["provider"]["context_window"]), (200, WINDOW))                                    # never more than the session's own size

    def test_a_model_on_another_machine_needs_the_master_switch_and_a_cloud_provider_needs_the_consent_too(self):
        code, out = self.call("POST", "/api/llm/technical", {"type": "vllm", "model": "m", "base_url": "http://192.168.1.50:8000/v1"})
        self.assertEqual(code, 200)
        v = self.view()
        self.assertEqual((v["route"]["provider"], v["session"]["available"], v["session"]["on_pc"]), ("technical-vllm", False, False))
        self.call("POST", "/api/side-uses", {"external": True})
        self.assertEqual((self.view()["session"]["available"], self.view()["session"]["tokens"]), (True, 8192))
        self.call("POST", "/api/side-uses", {"external": False})
        code, out = self.call("POST", "/api/llm/providers", {"type": "openai_compat", "base_url": "https://api.example.invalid/v1", "api_key": "k" * 12, "model": "cheap"})
        pid = out["provider"]["id"]
        code, out = self.call("POST", "/api/llm/assign", {"function": "technical", "provider": pid})
        self.assertEqual((code, out["error"]["code"]), (409, "not_allowed"))                                            # no consent yet
        self.call("POST", "/api/llm/consent", {"function": "technical", "allowed": True})
        self.assertEqual(self.call("POST", "/api/llm/assign", {"function": "technical", "provider": pid})[0], 200)
        v = self.view()
        self.assertEqual((v["route"]["provider"], v["session"]["available"]), (pid, False))                           # consent given, the master switch still off
        self.call("POST", "/api/side-uses", {"external": True})
        self.assertTrue(self.view()["session"]["available"])
        self.call("POST", "/api/llm/consent", {"function": "technical", "allowed": False})                             # taking the yes back takes the route away
        self.assertEqual(self.view()["route"]["provider"], "")
        self.call("POST", "/api/side-uses", {"external": False})

    def test_nothing_is_sent_to_an_address_outside_this_pc_while_the_master_switch_is_off(self):
        self.call("POST", "/api/llm/technical", {"type": "lmstudio", "model": "m", "base_url": "http://192.168.1.50:1234/v1"})
        client = RoutedClient("technical", self.registry, mock.Mock(side_context=lambda: 0), Ledger(self.reg_dir / "u.json"))
        self.assertFalse(client.stays_on_pc())
        self.assertEqual(side.side_context(client, "triage"), 0)
        self.assertEqual(side.side_extra_for(client, "compaction", 100, 100), {})
        side_uses.set_external(True)
        self.assertGreater(side.side_context(client, "triage"), 0)

    # -- the switch in the chat header (api/technical-session), end to end with the dialog and the harness ----------

    def test_off_in_the_chat_turns_every_use_off_for_the_dialog_and_the_harness_and_back_on_restores_them(self):
        self.session()
        self.assertEqual(self.call("GET", "/api/technical-session"), (200, {"on": True}))
        self.assertTrue(self.view()["master"])
        self.call("POST", "/api/side-uses", {"use": "judge", "on": False})
        self.assertEqual(self.call("POST", "/api/technical-session", {"on": False}), (200, {"on": False}))
        v = self.view()
        self.assertFalse(v["master"])
        self.assertFalse(any(u["on"] for u in v["uses"]))
        client = mock.Mock(side_context=lambda: WINDOW)
        self.assertTrue(all(side.side_extra(client, u.id) == {} for u in side_uses.USES))        # no call is sent to the session
        self.assertEqual(self.call("POST", "/api/technical-session", {"on": True}), (200, {"on": True}))
        v = self.view()
        self.assertTrue(v["master"])
        self.assertFalse(self.use(v, "judge")["on"])                                              # what the person set for a use was kept
        self.assertTrue(self.use(v, "triage")["on"])

    def test_the_choice_is_kept_in_the_file_survives_a_reset_and_is_read_by_another_process(self):
        self.call("POST", "/api/technical-session", {"on": False})
        self.call("POST", "/api/side-uses", {"reset": "all"})
        self.assertFalse(json.loads(side_uses.file_path().read_text(encoding="utf-8"))["_session"]["on"])
        side_uses._cache.update(key=None, values={})
        self.assertFalse(side_uses.session_on())
        self.assertEqual(self.call("GET", "/api/technical-session"), (200, {"on": False}))

    def test_a_bad_value_is_refused_and_changes_nothing(self):
        for bad in ({"on": "yes"}, {"on": 1}, {}):
            self.assertEqual(self.call("POST", "/api/technical-session", bad)[0], 400)
        self.assertTrue(side_uses.session_on())
