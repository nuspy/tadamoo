"""Installing an engine from a local folder or archive, starting Strata's server as a managed child (a fake with the same
start-up protocol), relaying the /v1 routes to it, stopping and switching engines, and an engine's saved profiles."""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from serve import engine_host, engine_install, engines, profiles  # noqa: E402
from serve.tests import fake_strata  # noqa: E402
from serve.tests.test_engines import A100, Http, connect  # noqa: E402


def wait(fn, seconds=30.0):
    end = time.time() + seconds
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.1)
    raise AssertionError("timed out")


def alive(pid) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class Base(Http):
    def setUp(self):
        super().setUp()
        self.root = tempfile.TemporaryDirectory()
        self.rootp = Path(self.root.name)
        self.patch = mock.patch.object(engines, "ROOT", self.rootp)
        self.patch.start()
        self.tmp = self.rootp / "files"
        self.tmp.mkdir()

    def tearDown(self):
        engine_host.HOST.stop()
        engine_install._jobs.clear()
        self.patch.stop()
        self.root.cleanup()
        super().tearDown()

    def entry(self, engine="strata"):
        body = self.req("GET", "/api/loader/engines")[1]
        return next(e for e in body["engines"] if e["id"] == engine)

    def get(self, path):
        c = connect(self.port)
        try:
            c.request("GET", path)
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def post_raw(self, path, body):
        c = connect(self.port)
        try:
            c.request("POST", path, body=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            r = c.getresponse()
            return r.status, r.read(), r.getheader("Content-Type")
        finally:
            c.close()

    def start(self, arch="qwen4exp", values=None, **extra):
        fake_strata.install(self.rootp)
        cfg = fake_strata.run_config(self.tmp, arch=arch)
        code, body = self.req("POST", "/api/loader/engines/strata/load", {"model": str(cfg), "values": values or {}, **extra})
        return code, body, cfg

    def running(self):
        wait(lambda: self.entry()["state"] in ("running", "error"))
        self.assertEqual(self.entry()["state"], "running", self.entry().get("error"))

    def installed_job(self):
        return wait(lambda: (self.entry()["install"] or {}).get("status") in ("done", "error") and self.entry()["install"])


class Install(Base):
    def test_without_a_source_nothing_is_downloaded(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("downloaded")):
            code, body = self.req("POST", "/api/loader/engines/strata/install", {})
        self.assertEqual((code, body["error"]["code"]), (501, "not_available_yet"))

    def test_a_missing_or_incomplete_source_says_why(self):
        code, body = self.req("POST", "/api/loader/engines/strata/install", {"source": str(self.tmp / "nope")})
        self.assertEqual((code, body["error"]["code"]), (400, "source_missing"))
        (self.tmp / "empty").mkdir()
        code, body = self.req("POST", "/api/loader/engines/strata/install", {"source": str(self.tmp / "empty")})
        self.assertEqual((code, body["error"]["code"]), (400, "source_incomplete"))
        code, body = self.req("POST", "/api/loader/engines/nothing/install", {"source": str(self.tmp)})
        self.assertEqual(code, 404)

    def test_a_folder_is_copied_with_progress(self):
        src = fake_strata.make_source(self.tmp / "src")
        (src / "serve" / "test_x.py").write_text("x", encoding="utf-8")
        code, body = self.req("POST", "/api/loader/engines/strata/install", {"source": str(src), "remember": True})
        self.assertEqual(code, 202)
        job = self.installed_job()
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["copied"], job["total"])
        dest = self.rootp / "bin" / "strata"
        self.assertTrue((dest / ("strata" + fake_strata.EXE)).is_file())
        self.assertTrue((dest / "serve" / "server.py").is_file())
        self.assertFalse((dest / "serve" / "test_x.py").exists())
        e = self.entry()
        self.assertTrue(e["installed"])
        self.assertEqual(e["state"], "stopped")
        self.assertEqual(e["source"], str(src))

    def test_a_zip_is_unpacked_and_copied(self):
        src = fake_strata.make_source(self.tmp / "src")
        z = self.tmp / "strata.zip"
        with zipfile.ZipFile(z, "w") as zf:
            for f in src.rglob("*"):
                if f.is_file():
                    zf.write(f, "strata-pack/" + f.relative_to(src).as_posix())
        code, _ = self.req("POST", "/api/loader/engines/strata/install", {"source": str(z)})
        self.assertEqual(code, 202)
        self.assertEqual(self.installed_job()["status"], "done")
        self.assertTrue((self.rootp / "bin" / "strata" / "serve" / "server.py").is_file())
        self.assertEqual([p.name for p in (self.rootp / "bin").glob(".install-*")], [])

    def test_a_bad_zip_is_an_error_not_a_crash(self):
        z = self.tmp / "bad.zip"
        z.write_bytes(b"not a zip")
        self.req("POST", "/api/loader/engines/strata/install", {"source": str(z)})
        self.assertEqual(self.installed_job()["error"]["code"], "source_incomplete")
        self.assertFalse(self.entry()["installed"])

    def test_an_installed_engine_answers_installed(self):
        fake_strata.install(self.rootp)
        self.assertEqual(self.req("POST", "/api/loader/engines/strata/install", {})[1]["status"], "installed")


class Activate(Base):
    def test_load_starts_the_child_and_the_v1_routes_are_relayed(self):
        code, body, cfg = self.start(values={"max_context": 8192})
        self.assertEqual(code, 202, body)
        self.running()
        e = self.entry()
        self.assertTrue(e["active"])
        status, raw = self.get("/v1/models")
        self.assertEqual(json.loads(raw)["data"][0]["id"], "qwen3.8-flash-next")
        sent = json.loads(self.get("/v1/status")[1])
        self.assertEqual(sent["args"][sent["args"].index("--max-context") + 1], "8192")
        run = json.loads((Path(os.environ["TADAMOO_DATA"]) / "strata-run" / "run.json").read_text(encoding="utf-8"))
        self.assertNotIn("api_key", run)
        self.assertEqual(run["host"], "127.0.0.1")
        self.assertEqual(Path(run["cwd"]), self.rootp / "bin" / "strata")
        status, raw, kind = self.post_raw("/v1/chat/completions", {"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        msg = json.loads(raw)["choices"][0]["message"]
        self.assertEqual((status, msg["content"], msg["reasoning_content"]), (200, "Hello from Strata", "thinking"))
        status, raw, kind = self.post_raw("/v1/chat/completions", {"stream": True, "messages": []})
        self.assertIn("text/event-stream", kind)
        self.assertEqual([l for l in raw.decode().split("\n") if l][-1], "data: [DONE]")
        self.assertEqual(json.loads(self.post_raw("/v1/messages", {"messages": []})[1])["stop_reason"], "end_turn")

    def test_stop_gives_the_port_back_to_basalt(self):
        self.start()
        self.running()
        pid = engine_host.HOST.proc.pid
        code, body = self.req("POST", "/api/loader/engines/strata/stop", {})
        self.assertEqual((code, body["phase"]), (200, "idle"))
        self.assertEqual(self.entry()["state"], "stopped")
        wait(lambda: not alive(pid))
        self.assertEqual(json.loads(self.get("/v1/models")[1])["data"][0]["owned_by"], "basalt")

    def test_loading_a_basalt_model_stops_the_other_engine(self):
        self.start()
        self.running()
        with mock.patch("serve.loader_api.load", return_value={"status": "saved"}):
            self.assertEqual(self.req("POST", "/api/loader/load", {})[0], 200)
        self.assertFalse(engine_host.HOST.active())
        self.assertEqual(self.entry()["state"], "stopped")

    def test_starting_strata_unloads_basalt_first(self):
        fake_strata.install(self.rootp)
        cfg = fake_strata.run_config(self.tmp)
        calls = []
        self.svc.unload = lambda *a, **k: calls.append("unload") or "unloaded"
        self.req("POST", "/api/loader/engines/strata/load", {"model": str(cfg)})
        self.assertEqual(calls, ["unload"])
        self.running()

    def test_a_basalt_request_in_flight_is_not_cut(self):
        fake_strata.install(self.rootp)
        cfg = fake_strata.run_config(self.tmp)
        self.svc.unload = lambda *a, **k: "busy"
        code, body = self.req("POST", "/api/loader/engines/strata/load", {"model": str(cfg)})
        self.assertEqual((code, body["error"]["code"]), (409, "busy"))
        self.assertFalse(engine_host.HOST.active())

    def test_a_second_load_replaces_the_first(self):
        self.start()
        self.running()
        first = engine_host.HOST.proc.pid
        cfg = fake_strata.run_config(self.tmp, name="strata-b.json")
        self.assertEqual(self.req("POST", "/api/loader/engines/strata/load", {"model": str(cfg)})[0], 202)
        self.running()
        self.assertNotEqual(engine_host.HOST.proc.pid, first)
        wait(lambda: not alive(first))

    def test_the_health_route_stays_basalts(self):
        self.start()
        self.running()
        self.assertEqual(json.loads(self.get("/health")[1])["service"], "basalt")

    def test_the_load_control_follows_the_active_engine(self):
        self.start()
        self.running()
        self.svc.control_token = "control-secret"
        c = connect(self.port)
        try:
            c.request("POST", "/api/control/load", body=b"{}",
                      headers={"Content-Type": "application/json", "X-Tadamoo-Control": "control-secret"})
            r = c.getresponse()
            self.assertEqual((r.status, json.loads(r.read())["status"]), (200, "loaded"))
        finally:
            c.close()


class Refusals(Base):
    def test_not_installed(self):
        cfg = fake_strata.run_config(self.tmp)
        code, body = self.req("POST", "/api/loader/engines/strata/load", {"model": str(cfg)})
        self.assertEqual((code, body["error"]["code"]), (409, "not_installed"))

    def test_a_model_is_needed(self):
        fake_strata.install(self.rootp)
        self.assertEqual(self.req("POST", "/api/loader/engines/strata/load", {})[1]["error"]["code"], "model_missing")
        self.assertEqual(self.req("POST", "/api/loader/engines/strata/load", {"model": str(self.tmp / "no.json")})[1]["error"]["code"],
                         "model_missing")

    def test_the_binary_of_the_family_must_be_there(self):
        fake_strata.install(self.rootp)
        (self.rootp / "bin" / "strata" / ("strata-q36" + fake_strata.EXE)).unlink()
        cfg = fake_strata.run_config(self.tmp, arch="qwen35moe")
        code, body = self.req("POST", "/api/loader/engines/strata/load", {"model": str(cfg)})
        self.assertEqual((code, body["error"]["code"]), (409, "binary_missing"))

    def test_a_control_the_family_lacks_is_refused_with_its_name(self):
        fake_strata.install(self.rootp)
        cfg = fake_strata.run_config(self.tmp, arch="qwen35moe")
        code, body = self.req("POST", "/api/loader/engines/strata/load", {"model": str(cfg), "values": {"kv": "int8"}})
        self.assertEqual((code, body["error"]["code"]), (400, "control_unsupported"))
        self.assertIn("kv", body["error"]["message"])

    def test_a_flag_the_binary_does_not_have_is_refused(self):
        fake_strata.install(self.rootp)
        (self.rootp / "bin" / "strata" / ("strata" + fake_strata.EXE)).write_bytes(b"--max-context --serve")
        cfg = fake_strata.run_config(self.tmp)
        code, body = self.req("POST", "/api/loader/engines/strata/load", {"model": str(cfg), "values": {"spec": 4}})
        self.assertEqual((code, body["error"]["code"]), (400, "flag_unsupported"))
        self.assertIn("--spec", body["error"]["message"])

    def test_the_tokenizer_folder_is_needed(self):
        fake_strata.install(self.rootp)
        cfg = fake_strata.run_config(self.tmp)
        data = json.loads(cfg.read_text(encoding="utf-8"))
        data["tokenizer"] = str(self.tmp / "none")
        cfg.write_text(json.dumps(data), encoding="utf-8")
        code, body = self.req("POST", "/api/loader/engines/strata/load", {"model": str(cfg)})
        self.assertEqual((code, body["error"]["code"]), (400, "tokenizer_missing"))

    def test_a_foreign_file(self):
        fake_strata.install(self.rootp)
        f = self.tmp / "m.bin"
        f.write_bytes(b"x")
        self.assertEqual(self.req("POST", "/api/loader/engines/strata/load", {"model": str(f)})[1]["error"]["code"], "model_unsupported")

    def test_basalt_has_no_load_here(self):
        self.assertEqual(self.req("POST", "/api/loader/engines/basalt/load", {"model": "x"})[0], 404)


class RefusalOnA100(Base):
    fake = A100

    def test_the_hardware_reason_is_the_code(self):
        fake_strata.install(self.rootp)
        cfg = fake_strata.run_config(self.tmp)
        code, body = self.req("POST", "/api/loader/engines/strata/load", {"model": str(cfg)})
        self.assertEqual((code, body["error"]["code"]), (409, "sm"))


class Failures(Base):
    def test_a_server_that_does_not_start_reports_its_last_lines(self):
        with mock.patch.dict(os.environ, {"FAKE_STRATA_FAIL": "start"}):
            self.start()
            wait(lambda: self.entry()["state"] == "error")
        e = self.entry()
        self.assertEqual(e["error"]["code"], "start_failed")
        self.assertIn("tokenizer is missing", e["error"]["message"])
        self.assertFalse(engine_host.HOST.active())

    def test_a_model_that_does_not_load_reports_the_engines_words(self):
        with mock.patch.dict(os.environ, {"FAKE_STRATA_FAIL": "load"}):
            self.start()
            wait(lambda: self.entry()["state"] == "error")
        self.assertEqual(self.entry()["error"]["code"], "load_failed")
        self.assertIn("out of memory", self.entry()["error"]["message"])

    def test_while_it_loads_the_v1_routes_say_so(self):
        with mock.patch.dict(os.environ, {"FAKE_STRATA_LOAD_S": "3"}):
            self.start()
            wait(lambda: self.entry()["state"] == "loading")
            status, raw, _ = self.post_raw("/v1/chat/completions", {"messages": []})
            self.assertEqual(status, 503)
            self.assertIn("loading", json.loads(raw)["error"]["message"])
            self.running()


class Models(Base):
    def test_run_configs_are_listed(self):
        fake_strata.install(self.rootp)
        fake_strata.run_config(self.rootp / "bin" / "strata", name="strata-a.json")
        fake_strata.run_config(self.rootp / "bin" / "strata", name="strata-b.json", arch="deepseek41")
        code, body = self.req("GET", "/api/loader/engines/strata/models")
        self.assertEqual(code, 200)
        self.assertEqual(sorted(m["family"] for m in body["models"]), ["deepseek41", "qwen4exp"])
        self.assertTrue(all(m["ok"] for m in body["models"]))
        self.assertEqual(self.req("GET", "/api/loader/engines/basalt/models")[1]["models"], [])
        self.assertEqual(self.req("GET", "/api/loader/engines/nothing/models")[0], 404)

    def test_a_qwen36_gguf_without_the_mtp_layer_is_listed_as_refused(self):
        found = [{"kind": "gguf", "role": "model", "arch": "qwen35moe", "path": "/m/q36.gguf", "files": ["/m/q36.gguf"],
                  "name": "Q36", "size_bytes": 5}]
        with mock.patch("serve.models.dirs", return_value=["/m"]), mock.patch("serve.models.scan", return_value={"models": found}), \
                mock.patch("serve.models.read_gguf", return_value={"meta": {"general.architecture": "qwen35moe"}}):
            m = engine_host.models("strata")
        self.assertEqual([(x["family"], x["ok"], x["reason"]) for x in m], [("qwen35moe", False, "nextn")])
        with mock.patch("serve.models.dirs", return_value=["/m"]), mock.patch("serve.models.scan", return_value={"models": found}), \
                mock.patch("serve.models.read_gguf", return_value={"meta": {"qwen35moe.nextn_predict_layers": 1}}):
            self.assertTrue(engine_host.models("strata")[0]["ok"])


class EngineProfiles(Base):
    def test_a_profile_remembers_the_engine_and_stays_out_of_basalts_list(self):
        fake_strata.install(self.rootp)
        cfg = fake_strata.run_config(self.tmp)
        code, _ = self.req("POST", "/api/profiles", {"action": "save", "name": "Strata 8k", "engine": "strata",
                                                      "model": str(cfg), "values": {"max_context": 8192}})
        self.assertEqual(code, 200)
        self.assertNotIn("Strata 8k", [p["name"] for p in profiles.listing()["load"]])
        listed = self.req("GET", "/api/loader/engines/strata/models")[1]["profiles"]
        self.assertEqual([(p["name"], p["values"]) for p in listed], [("Strata 8k", {"max_context": 8192})])
        self.assertIsNone(profiles.get_load("Strata 8k"))
        self.assertEqual(self.req("POST", "/api/profiles", {"action": "default", "name": "Strata 8k"})[0], 400)
        self.req("POST", "/api/loader/engines/strata/load", {"profile": "Strata 8k"})
        self.running()
        sent = json.loads(self.get("/v1/status")[1])
        self.assertEqual(sent["args"][sent["args"].index("--max-context") + 1], "8192")

    def test_a_profile_with_an_unsupported_control_is_not_saved(self):
        cfg = fake_strata.run_config(self.tmp)
        code, _ = self.req("POST", "/api/profiles", {"action": "save", "name": "x", "engine": "strata",
                                                      "model": str(cfg), "values": {"no_such": 1}})
        self.assertEqual(code, 400)
        code, _ = self.req("POST", "/api/profiles", {"action": "save", "name": "x", "engine": "strata", "model": "/nope", "values": {}})
        self.assertEqual(code, 400)

    def test_a_basalt_profile_is_saved_as_before(self):
        code, _ = self.req("POST", "/api/profiles", {"action": "save", "name": "plain", "values": {"engine.max-context": 8192}})
        self.assertEqual(code, 200)
        self.assertIn("plain", [p["name"] for p in profiles.listing()["load"]])
        self.assertNotIn("engine", profiles._read()["load"]["plain"])


class Capabilities(unittest.TestCase):
    def test_each_family_reports_only_what_its_binary_has(self):
        f = engines.FAMILIES
        self.assertEqual(f["qwen35moe"]["controls"], ("max_context", "mtp", "parallel"))
        self.assertFalse(f["qwen35moe"]["capabilities"]["expert_cache"])
        self.assertTrue(f["qwen4exp"]["capabilities"]["speed_spec"])
        self.assertFalse(f["deepseek41"]["capabilities"]["speed_spec"])
        self.assertFalse(any(x["capabilities"]["calibrate"] for x in f.values()))
        self.assertTrue(all(isinstance(v, bool) for v in engines.ENGINES["strata"]["capabilities"].values()))

    def test_values_are_checked(self):
        self.assertEqual(engines.check_values("strata", {"max_context": 4096, "kv": "int8", "spec": ""}, "qwen4exp"),
                         {"max_context": 4096, "kv": "int8"})
        for bad in ({"max_context": 0}, {"kv": "fp8"}, {"max_context": True}, {"expert_cache": -1}, {"nope": 1}):
            with self.assertRaises(ValueError, msg=bad):
                engines.check_values("strata", bad, "qwen4exp")

    def test_set_flag(self):
        self.assertEqual(engine_host.set_flag(["--a", "1", "--b", "2"], "--a", 9), ["--b", "2", "--a", "9"])
        self.assertEqual(engine_host.set_flag(["--a", "1"], "--a", None), [])


if __name__ == "__main__":
    unittest.main()
