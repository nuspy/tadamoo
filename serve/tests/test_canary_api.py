"""The exfiltration test page's server side: settings, the config it writes, start and stop with the security monitor, the findings,
the routes, and the tool-call report. The add-on's launcher is a fake: no process starts and nothing touches the network."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from serve import ROOT, addons, canary_api, netwatch_api, userdata
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.netwatch import NetWatch
from serve.service import Service
from serve.tests.test_netwatch import FakeSource, NoTelemetry
from serve.util.text import ByteTokenizer


def _find_addon() -> Path | None:
    for cand in (os.environ.get("CANARY_SRC", ""), str(ROOT.parent / "openweight-canary"), str(ROOT.parent.parent / "openweight-canary")):
        if cand and (Path(cand) / "canary" / "__init__.py").is_file():
            return Path(cand)
    return None


ADDON = _find_addon()


class FakeLauncher:
    """The add-on's launch module without processes: a started test is 'running' until stop() or finish()."""

    def __init__(self):
        self.pid = None
        self.started = []
        self.stopped = 0
        self.status = {"state": "running", "sessions_this_run": 3}

    def start_background(self, config_path, *, extra_args=(), python=None):
        self.pid = 4242
        self.started.append((Path(config_path), python))
        return {"pid": self.pid, "state_dir": "x", "log": "x", "already_running": False}

    def running_pid(self, state_dir):
        return self.pid

    def read_status(self, state_dir):
        return dict(self.status) if self.pid else None

    def stop(self, state_dir, *, timeout=30.0, kill=True, sleep=None):
        self.stopped += 1
        self.pid = None
        return "stopped"

    def finish(self):
        self.pid = None


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.src = FakeSource()
        self.nw = NetWatch(root / "nw", self.src, lambda ip: "", lambda: 1000.0)
        self.fake = FakeLauncher()
        for p in (mock.patch.object(userdata, "path", lambda name: root / "data" / name),
                  mock.patch.object(netwatch_api, "_WATCH", self.nw),
                  mock.patch.object(canary_api, "_LAUNCHER", self.fake),
                  mock.patch.object(canary_api, "WATCH_S", 0.01),
                  mock.patch.object(canary_api, "_HARNESSES", lambda: []),
                  mock.patch.object(addons, "canary_path", lambda: ADDON)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.nw.stop)
        canary_api._WATCHER = None
        self.addCleanup(self._end_watcher)
        self.ctx = canary_api.Context(port=7232, api_key="secret-key-123", model="qwen-test")


    def _end_watcher(self):
        self.fake.finish()
        w = canary_api._WATCHER
        if w is not None:
            w.join(5)
        canary_api._WATCHER = None


class TestSettings(_Base):
    def test_defaults_and_save(self):
        s = canary_api.settings()
        self.assertEqual(s["runtime"], "basalt")
        self.assertIsNone(s["duration_minutes"])
        self.assertFalse(s["keep_running"])
        canary_api.save_settings({"duration_minutes": 90, "reviewer": {"enabled": True, "base_url": "http://r/v1", "model": "judge"}})
        s = canary_api.settings()
        self.assertEqual(s["duration_minutes"], 90)
        self.assertEqual(s["reviewer"]["model"], "judge")
        canary_api.save_settings({"duration_minutes": ""})
        self.assertIsNone(canary_api.settings()["duration_minutes"])

    def test_refused(self):
        for bad in ({"bogus": 1}, {"base_url": "ftp://x"}, {"duration_minutes": -5}, {"duration_minutes": "x"},
                    {"review_every": 1.5}, {"reviewer": {"kind": "other"}}, {"runtime": ""}, [1]):
            with self.assertRaises(canary_api.CanaryError, msg=str(bad)):
                canary_api.save_settings(bad)
        self.assertEqual(canary_api.settings()["runtime"], "basalt")

    def test_effective_defaults_to_the_loaded_model_and_own_endpoint(self):
        e = canary_api.effective(canary_api.settings(), self.ctx)
        self.assertEqual((e["model"], e["base_url"]), ("qwen-test", "http://127.0.0.1:7232/v1"))
        s = canary_api.save_settings({"runtime": "ollama", "model": "m", "base_url": "http://h:1/v1"})
        e = canary_api.effective(s, self.ctx)
        self.assertEqual((e["model"], e["base_url"], e["ours"]), ("m", "http://h:1/v1", False))


class TestHarnesses(_Base):
    def test_parse(self):
        parse = canary_api._parse_harnesses
        self.assertEqual([h["id"] for h in parse('["a","b"]')], ["a", "b"])
        self.assertEqual(parse('{"harnesses":[{"id":"cc","name":"Claude Code","description":"d"}]}')[0]["name"], "Claude Code")
        self.assertEqual([h["id"] for h in parse("cline  Cline style\nroo\n")], ["cline", "roo"])
        self.assertEqual(parse("[1, 2]"), [])

    def test_none_without_the_command(self):
        with mock.patch.object(canary_api, "_HARNESSES", None), mock.patch.object(canary_api, "_HARNESS_CACHE", {}), \
                mock.patch.object(canary_api.subprocess, "run", return_value=mock.Mock(returncode=2, stdout="")):
            self.assertEqual(canary_api.harnesses(), [] if ADDON else [])

    def test_harness_only_written_when_chosen(self):
        s = canary_api.settings()
        self.assertEqual(s["harness"], "")
        canary_api.save_settings({"harness": "cline"})
        self.assertEqual(canary_api.settings()["harness"], "cline")


@unittest.skipUnless(ADDON, "the Openweight Canary add-on is not on this machine")
class TestRun(_Base):
    def test_config_uses_basalt_endpoint_and_monitor(self):
        path = canary_api._write_config(canary_api.settings(), self.ctx)
        cfg = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(cfg["target"]["base_url"], "http://127.0.0.1:7232/v1")
        self.assertEqual(cfg["target"]["model"], "qwen-test")
        self.assertEqual(cfg["monitor"], {"base_url": "http://127.0.0.1:7232", "api_key": "env:" + canary_api.KEY_ENV})
        self.assertEqual(cfg["duration"], "until-stopped")
        self.assertNotIn("secret-key-123", json.dumps(cfg))                # the key itself is never written
        self.assertNotIn("harness", cfg)

    def test_duration_in_seconds(self):
        canary_api.save_settings({"duration_minutes": 2})
        cfg = json.loads(canary_api._write_config(canary_api.settings(), self.ctx).read_text(encoding="utf-8"))
        self.assertEqual(cfg["duration"], "120")

    def test_no_model(self):
        with self.assertRaises(canary_api.CanaryError) as c:
            canary_api.start(canary_api.Context(port=7232))
        self.assertEqual(c.exception.code, "no_model")
        self.assertEqual(self.fake.started, [])

    def test_the_reviewer_may_be_the_model_under_test(self):
        canary_api.save_settings({"reviewer": {"enabled": True, "kind": "openai", "base_url": "http://127.0.0.1:7232/v1", "model": "qwen-test"}})
        out = canary_api.start(self.ctx)
        self.assertTrue(out["started"])
        cfg = json.loads((canary_api._dir() / "canary.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["reviewer"]["model"], "qwen-test")

    def test_the_settings_saved_in_the_page_are_laid_over_the_config_but_never_the_engine(self):
        canary_api._write(canary_api.overlay_path(), {"narrative": {"enabled": True, "turns": 40}, "workspace": {"enabled": True, "files": 12},
                                                       "target": {"model": "not-this-one"}, "state_dir": "elsewhere", "reviewer": None})
        cfg = json.loads(canary_api._write_config(canary_api.settings(), self.ctx).read_text(encoding="utf-8"))
        self.assertEqual(cfg["narrative"]["turns"], 40)
        self.assertTrue(cfg["narrative"]["enabled"])
        self.assertEqual(cfg["workspace"]["files"], 12)
        self.assertEqual(cfg["target"]["model"], "qwen-test")
        self.assertNotEqual(cfg["state_dir"], "elsewhere")

    def test_start_turns_monitor_on_and_stop_restores_it(self):
        self.assertFalse(self.nw.store.settings["enabled"])
        out = canary_api.start(self.ctx)
        self.assertTrue(out["netwatch_turned_on"])
        self.assertTrue(self.nw.store.settings["enabled"])
        self.assertEqual(self.fake.started[0][1], canary_api.sys.executable)
        st = canary_api.status(self.ctx)
        self.assertTrue(st["running"])
        self.assertTrue(st["netwatch"]["by_canary"])
        with self.assertRaises(canary_api.CanaryError) as c:
            canary_api.start(self.ctx)
        self.assertEqual(c.exception.code, "running")
        canary_api.stop()
        self._wait(lambda: not self.nw.store.settings["enabled"])
        st = canary_api.status(self.ctx)
        self.assertFalse(st["running"])
        self.assertEqual(st["state"], "stopped")
        self.assertEqual(self.fake.stopped, 1)

    def test_monitor_left_on_when_the_user_had_it_on(self):
        self.nw.update_settings({"enabled": True})
        self.assertFalse(canary_api.start(self.ctx)["netwatch_turned_on"])
        self.fake.finish()
        canary_api.sync()
        self.assertTrue(self.nw.store.settings["enabled"])

    def test_test_that_ends_by_itself_switches_the_monitor_off(self):
        canary_api.save_settings({"duration_minutes": 1})
        canary_api.start(self.ctx)
        self.fake.finish()                                                # the duration was reached
        self._wait(lambda: not self.nw.store.settings["enabled"])
        self.assertIsNotNone(canary_api._gui()["finished"])

    def test_failed_start_restores_the_monitor(self):
        with mock.patch.object(self.fake, "start_background", side_effect=OSError("no python")):
            with self.assertRaises(canary_api.CanaryError) as c:
                canary_api.start(self.ctx)
        self.assertEqual(c.exception.code, "start_failed")
        self.assertFalse(self.nw.store.settings["enabled"])

    def test_shutdown_ends_the_test_unless_kept(self):
        canary_api.start(self.ctx)
        canary_api.shutdown()
        self.assertEqual(self.fake.stopped, 1)
        self.assertFalse(self.nw.store.settings["enabled"])
        canary_api.save_settings({"keep_running": True})
        canary_api.start(self.ctx)
        canary_api.shutdown()
        self.assertEqual(self.fake.stopped, 1)
        self.assertIsNotNone(self.fake.pid)

    def test_reattach_after_a_restart(self):
        canary_api.start(self.ctx)
        canary_api._WATCHER = None
        canary_api.reattach()
        self.assertTrue(canary_api._WATCHER.is_alive())
        self.fake.finish()
        self._wait(lambda: canary_api._gui().get("finished"))
        canary_api.reattach()                                             # nothing running and already closed: no change

    def test_findings_register_verify_report(self):
        log = canary_api.lib("findings").FindingsLog(canary_api._state_dir() / "findings.jsonl")
        log.append("high", "connection", {"host": "evil.example", "remote_ip": "203.0.113.9", "remote_port": 443, "process": "x", "tags": ["doh_or_tor"]})
        log.append("info", "session", {"note": "ok"})
        out = canary_api.findings({})
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["counts"]["high"], 1)
        self.assertEqual(out["findings"][0]["n"], 2)                      # newest first
        self.assertEqual(canary_api.findings({"severity": "high"})["findings"][0]["n"], 1)
        self.assertTrue(canary_api.verify()["ok"])
        made = canary_api.register("1")
        self.assertTrue(made["manual"])
        self.assertEqual(made["host"], "evil.example")
        self.assertIn("doh_or_tor", made["tags"])
        self.assertEqual(canary_api.findings({})["findings"][1]["registered"], made["id"])
        with self.assertRaises(canary_api.CanaryError):
            canary_api.register("99")
        self.assertFalse(canary_api.report(False)["exists"])
        rep = canary_api.report(True)
        self.assertTrue(rep["exists"])
        self.assertIn("markdown", rep)
        # a changed record is found by the chain check
        p = canary_api._state_dir() / "findings.jsonl"
        p.write_text(p.read_text(encoding="utf-8").replace("evil.example", "good.example"), encoding="utf-8")
        self.assertFalse(canary_api.verify()["ok"])

    def _wait(self, cond, seconds=5.0):
        import time
        end = time.time() + seconds
        while time.time() < end and not cond():
            time.sleep(0.02)
        self.assertTrue(cond())


class TestNoAddon(_Base):
    def test_status_and_routes_without_the_add_on(self):
        with mock.patch.object(addons, "canary_path", lambda: None):
            st = canary_api.status(self.ctx)
            self.assertFalse(st["addon"])
            self.assertEqual(canary_api.route_get("/api/canary", {}, self.ctx)[0], 200)
            for fn in (lambda: canary_api.route_get("/api/canary/findings", {}, self.ctx), lambda: canary_api.route_post("/api/canary/start", {}, self.ctx)):
                with self.assertRaises(canary_api.CanaryError) as c:
                    fn()
                self.assertEqual(c.exception.code, "no_addon")
            self.assertEqual(canary_api.route_post("/api/canary/settings", {"review_every": 4}, self.ctx)[0], 200)


class TestToolCalls(unittest.TestCase):
    def test_tool_call_tag_becomes_active_once_calls_are_reported(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src, now = FakeSource(), [1000.0]
        nw = NetWatch(Path(tmp.name), src, lambda ip: "", lambda: now[0])
        src.conns[10] = [{"proto": "tcp", "laddr": ("192.168.1.5", 50000), "raddr": ("93.184.216.34", 443), "status": "ESTABLISHED"}]
        self.assertFalse(nw.status()["tool_calls_reported"])
        self.assertEqual(nw.note_tool_calls({"ts": 999, "tool": "web_fetch", "target": "https://93.184.216.34/x"}), {"noted": True})
        self.assertTrue(nw.status()["tool_calls_reported"])
        made = nw.poll_once()
        self.assertTrue(made)
        self.assertNotIn("no_agent_call", made[0]["tags"])                # a call names this address
        src.conns[10] = [{"proto": "tcp", "laddr": ("192.168.1.5", 50001), "raddr": ("198.51.100.7", 443), "status": "ESTABLISHED"}]
        made = nw.poll_once()
        self.assertIn("no_agent_call", made[0]["tags"])                   # no call names this one
        for bad in ({}, {"tool": ""}, {"tool": "t", "ts": "x"}, [1]):
            with self.assertRaises(Exception):
                nw.note_tool_calls(bad)
        nw.note_tool_calls({"tool": "t", "ts": 99999999})                # a time in the future is taken as now
        self.assertLessEqual(nw.tool_calls[-1][0], now[0])

    def test_harness_hook(self):
        from tadamoo import outward_calls
        seen = []
        with mock.patch.object(outward_calls, "_observers", []):
            outward_calls.note("web_fetch", {"url": "https://a.example/x"})      # no observer: nothing happens
            outward_calls.add_observer(lambda ts, tool, target: seen.append((tool, target)))
            outward_calls.add_observer(lambda *a: 1 / 0)                         # a failing observer changes nothing
            outward_calls.note("web_fetch", {"url": "https://a.example/x", "n": 3, "q": "hello"})
        self.assertEqual(seen, [("web_fetch", "https://a.example/x hello")])
        self.assertEqual(outward_calls.target_of("not a dict"), "")


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.nw = NetWatch(root / "nw", FakeSource(), lambda ip: "", lambda: 1000.0)
        cls.patches = [mock.patch.object(userdata, "path", lambda name: root / "data" / name), mock.patch.object(netwatch_api, "_WATCH", cls.nw),
                       mock.patch.object(canary_api, "_LAUNCHER", FakeLauncher()), mock.patch.object(addons, "canary_path", lambda: ADDON),
                       mock.patch.object(canary_api, "_HARNESSES", lambda: [{"id": "h", "name": "H", "description": ""}])]
        for p in cls.patches:
            p.start()
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "Hello.", max_context=4096), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.svc.telemetry = NoTelemetry()
        cls.svc.api_key = ""
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def call(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_netwatch_status_alias_and_tool_call_route(self):
        st, a = self.call("GET", "/api/netwatch/status")
        self.assertEqual(st, 200)
        self.assertIn("enabled", a)
        self.assertEqual(self.call("POST", "/api/netwatch/tool-calls", {"tool": "web_fetch", "target": "x"})[0], 200)
        self.assertTrue(self.call("GET", "/api/netwatch/status")[1]["tool_calls_reported"])
        self.assertEqual(self.call("POST", "/api/netwatch/tool-calls", {})[0], 400)

    @unittest.skipUnless(ADDON, "the Openweight Canary add-on is not on this machine")
    def test_the_canary_page_is_served_under_canary_and_runs_the_test_through_this_server(self):
        req = urllib.request.Request(self.base + "/canary/")
        with urllib.request.urlopen(req) as r:
            page = r.read()
            self.assertEqual(r.status, 200)
        self.assertIn(b"Openweight Canary", page)
        self.assertIn(b'"api/', page)
        st, summary = self.call("GET", "/canary/api/summary")
        self.assertEqual(st, 200)
        self.assertIn("by_level", summary)
        self.assertEqual(self.call("POST", "/canary/api/stop", {})[0], 403)                       # no page header: refused
        req = urllib.request.Request(self.base + "/canary/api/start", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json", "X-Canary-GUI": "1"})
        with urllib.request.urlopen(req) as r:
            started = json.loads(r.read())
        self.assertTrue(started["ok"])
        self.assertTrue(started["started"])                                                       # through canary_api.start, with the monitor
        req = urllib.request.Request(self.base + "/canary/api/stop", data=b"{}", method="POST", headers={"X-Canary-GUI": "1"})
        with urllib.request.urlopen(req) as r:
            self.assertTrue(json.loads(r.read())["ok"])

    @unittest.skipUnless(ADDON, "the Openweight Canary add-on is not on this machine")
    def test_canary_routes(self):
        st, body = self.call("GET", "/api/canary")
        self.assertEqual(st, 200)
        self.assertTrue(body["addon"])
        self.assertEqual(body["effective"]["model"], self.svc.model)
        self.assertEqual(body["harnesses"][0]["id"], "h")
        self.assertEqual(self.call("POST", "/api/canary/settings", {"bogus": 1})[0], 400)
        self.assertEqual(self.call("POST", "/api/canary/settings", {"review_every": 5})[1]["settings"]["review_every"], 5)
        st, body = self.call("POST", "/api/canary/start", {})
        self.assertEqual(st, 200, body)
        self.assertEqual(self.call("POST", "/api/canary/start", {})[0], 409)
        self.assertEqual(self.call("POST", "/api/canary/stop", {})[0], 200)
        self.assertEqual(self.call("GET", "/api/canary/findings")[0], 200)
        self.assertEqual(self.call("GET", "/api/canary/verify")[1]["ok"], True)
        self.assertEqual(self.call("GET", "/api/canary/nothing")[0], 404)
        self.assertEqual(self.call("POST", "/api/canary/findings/9/register", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
