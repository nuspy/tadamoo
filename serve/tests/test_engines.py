"""The engine registry, hardware families, the tabs each PC shows, the memory pool modes, GET /api/loader/engines and the
install stub; and a golden check that the Blackwell Loader state is what it was before engines existed."""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from serve import engines, hardware, loader, supervisor  # noqa: E402
from serve.handler import serve  # noqa: E402
from serve.tests.test_loader import CONFIG, F, ROOMY, make_service  # noqa: E402

GiB = 1 << 30
GOLDEN = Path(__file__).with_name("golden") / "loader_blackwell.json"
WEB = Path(__file__).resolve().parents[1] / "web"


def nv(name, cc, **extra):
    return {"TADAMOO_FAKE_GPU": json.dumps({"name": name, "cc": cc, **extra})}


def accel(*items):
    return {"TADAMOO_FAKE_GPU": "null", "TADAMOO_FAKE_ACCEL": json.dumps(list(items))}


RTX5090 = nv("NVIDIA GeForce RTX 5090", [12, 0], vram_total=32 * GiB, vram_free=31 * GiB)
RTX4090 = nv("NVIDIA GeForce RTX 4090 Laptop GPU", [8, 9])
RTX3090 = nv("NVIDIA GeForce RTX 3090", [8, 6])
RTX2080 = nv("NVIDIA GeForce RTX 2080", [7, 5])
A100 = nv("NVIDIA A100", [8, 0])
H100 = nv("NVIDIA H100", [9, 0])
GB10 = nv("NVIDIA GB10", [12, 1])
NO_GPU = {"TADAMOO_FAKE_GPU": "null"}
MI300X = accel({"vendor": "amd", "name": "AMD Instinct MI300X"})
MI300A = accel({"vendor": "amd", "name": "AMD Instinct MI300A"})
RX7900 = accel({"vendor": "amd", "name": "AMD Radeon RX 7900 XTX"})
STRIX = accel({"vendor": "amd", "name": "AMD Radeon(TM) 8060S Graphics"})
ARC = accel({"vendor": "intel", "name": "Intel(R) Arc(TM) A770 Graphics"})
APPLE = accel({"vendor": "apple", "name": "Apple M3 Max"})
LAPTOP_APU = accel({"vendor": "amd", "name": "AMD Radeon(TM) 780M Graphics"}, {"vendor": "intel", "name": "Intel(R) UHD Graphics"})


def machine(env):
    with mock.patch.dict(os.environ, {"TADAMOO_FAKE_SYSTEM_OK": "1", **env}):
        return hardware.machine()


def tab_ids(env, show_all=False):
    return [t["id"] for t in engines.visible_tabs(machine(env), show_all)]


class Families(unittest.TestCase):
    def test_nvidia_architectures_by_compute_capability(self):
        for env, family in ((RTX5090, "nvidia_blackwell"), (GB10, "nvidia_blackwell"), (RTX4090, "nvidia_ada"),
                            (RTX3090, "nvidia_ampere"), (A100, "nvidia_ampere"), (RTX2080, "nvidia_turing"),
                            (H100, "nvidia_other")):
            m = machine(env)
            self.assertEqual(m["gpus"][0]["family"], family, env)
            self.assertIn(family, m["families"])
        self.assertEqual(hardware.nvidia_family(None), None)
        self.assertEqual(hardware.nvidia_family([10, 0]), "nvidia_blackwell")

    def test_other_vendors(self):
        for env, family, unified in ((MI300X, "amd", False), (RX7900, "amd", False), (MI300A, "amd", True),
                                     (STRIX, "amd", True), (ARC, "intel", False), (APPLE, "apple", True)):
            m = machine(env)
            self.assertEqual([g["family"] for g in m["gpus"]], [family], env)
            self.assertIs(m["gpus"][0]["unified_memory"], unified, env)
            self.assertIs(m["unified_memory"], unified, env)
            self.assertEqual("unified_memory" in m["families"], unified, env)

    def test_integrated_graphics_are_not_compute_targets(self):
        m = machine(LAPTOP_APU)
        self.assertEqual((m["gpus"], m["families"], m["unified_memory"]), ([], [], None))

    def test_unified_memory_of_a_nvidia_machine(self):
        self.assertIs(machine(GB10)["unified_memory"], True)
        self.assertIs(machine(RTX5090)["unified_memory"], False)
        self.assertIn("unified_memory", machine(GB10)["families"])

    def test_nothing_is_guessed(self):
        none = machine(NO_GPU)
        self.assertEqual((none["gpus"], none["families"], none["unified_memory"]), ([], [], None))
        self.assertEqual(hardware.classify_accel("amd", None), (None, None))
        self.assertEqual(hardware.classify_accel("amd", "AMD Radeon(TM) Graphics"), (None, None))
        self.assertEqual(hardware.classify_accel("intel", "Intel(R) Iris(R) Xe Graphics"), (None, None))
        self.assertEqual(machine({"TADAMOO_FAKE_GPU": json.dumps({"name": "Mystery"})})["gpus"][0]["family"], None)

    def test_a_mixed_machine_is_not_unified(self):
        env = {**RTX4090, "TADAMOO_FAKE_ACCEL": json.dumps({"vendor": "amd", "name": "AMD Radeon(TM) 8060S Graphics"})}
        m = machine(env)
        self.assertEqual(m["families"], ["amd", "nvidia_ada"])
        self.assertIs(m["unified_memory"], False)

    def test_the_blackwell_flag_of_gpu_is_unchanged(self):
        self.assertEqual(hardware.BLACKWELL_MAJORS, (10, 12))
        with mock.patch.dict(os.environ, {"TADAMOO_FAKE_SYSTEM_OK": "1", **RTX5090}):
            g = hardware.gpu()
            self.assertIs(g["blackwell"], True)
            self.assertEqual(sorted(g), ["blackwell", "cc", "display_active", "display_mode", "name", "processes",
                                         "vram_free", "vram_total"])
        for env, expect in ((RTX4090, False), (RTX3090, False), (RTX2080, False), (H100, False), (GB10, True)):
            with mock.patch.dict(os.environ, {"TADAMOO_FAKE_SYSTEM_OK": "1", **env}):
                self.assertIs(hardware.gpu()["blackwell"], expect, env)
        with mock.patch.dict(os.environ, {"TADAMOO_FAKE_SYSTEM_OK": "1", **NO_GPU}):
            self.assertIsNone(hardware.gpu()["blackwell"])

    def test_the_old_fakes_still_work_and_skip_real_detection(self):
        with mock.patch.dict(os.environ, {"TADAMOO_FAKE_SYSTEM_OK": "1", **RTX5090}), \
                mock.patch.object(hardware, "_real_accel", side_effect=AssertionError("read the real adapters")):
            self.assertEqual(hardware.machine()["families"], ["nvidia_blackwell"])
        with mock.patch.dict(os.environ, RTX5090, clear=False):
            os.environ.pop("TADAMOO_FAKE_SYSTEM_OK", None)
            self.assertNotEqual(hardware._fake("TADAMOO_FAKE_GPU"), json.loads(RTX5090["TADAMOO_FAKE_GPU"]))


class Registry(unittest.TestCase):
    def test_the_two_engines(self):
        b, s = engines.ENGINES["basalt"], engines.ENGINES["strata"]
        self.assertEqual((b["dir"], b["families"], b["models"]), ("", ("nvidia_blackwell",), ("qwen4exp",)))
        self.assertEqual(s["dir"], "strata")
        self.assertEqual(sorted(s["models"]), ["deepseek41", "qwen35moe", "qwen4exp"])
        self.assertEqual(set(s["families"]), {"nvidia_turing", "nvidia_ampere", "nvidia_ada", "nvidia_blackwell", "amd",
                                              "intel", "apple"})
        self.assertEqual(set(b["capabilities"]), set(engines.CAPABILITIES))
        self.assertTrue(all(v is True for v in b["capabilities"].values()))
        self.assertEqual(set(s["capabilities"]), set(engines.CAPABILITIES))
        self.assertTrue(all(isinstance(v, bool) for v in s["capabilities"].values()))
        self.assertFalse(s["capabilities"]["calibrate"])
        self.assertTrue(s["capabilities"]["context_kv"])

    def test_installed_means_the_binary_is_in_its_folder(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(engines, "ROOT", Path(d)):
            self.assertFalse(engines.installed("strata"))
            self.assertFalse(engines.installed("basalt"))
            exe = "strata.exe" if os.name == "nt" else "strata"
            (Path(d) / "bin" / "strata").mkdir(parents=True)
            (Path(d) / "bin" / "strata" / exe).write_bytes(b"x")
            self.assertTrue(engines.installed("strata"))
            (Path(d) / "bin" / ("basalt.exe" if os.name == "nt" else "basalt")).write_bytes(b"x")
            self.assertTrue(engines.installed("basalt"))

    def test_compatibility_with_a_reason(self):
        ok, code, _ = engines.compatible("basalt", machine(RTX5090))
        self.assertEqual((ok, code), (True, None))
        for env, why in ((RTX3090, "family"), (NO_GPU, "no_gpu"), (ARC, "family")):
            ok, code, text = engines.compatible("basalt", machine(env))
            self.assertEqual((ok, code), (False, why), env)
            self.assertTrue(text)
        self.assertEqual(engines.compatible("strata", machine(RTX3090))[0], True)
        self.assertEqual(engines.compatible("strata", machine(RTX5090))[0], True)
        self.assertEqual(engines.compatible("strata", machine(A100))[:2], (False, "sm"))
        self.assertEqual(engines.compatible("strata", machine(H100))[:2], (False, "family"))
        self.assertEqual(engines.compatible("strata", machine(APPLE))[0], True)
        self.assertEqual(engines.compatible("strata", machine(MI300X))[0], True)
        self.assertEqual(engines.compatible("strata", machine(ARC))[0], True)


class Visibility(unittest.TestCase):
    def test_a_5090_sees_every_nvidia_tab(self):
        m = machine(RTX5090)
        self.assertTrue({"nvidia_blackwell", "nvidia_ada", "nvidia_ampere", "nvidia_turing"} <= engines.seen_families(m))
        self.assertEqual(tab_ids(RTX5090), ["qwen", "rtx3040"])
        self.assertEqual(engines.visible_engines(m), ["basalt", "strata"])

    def test_a_3090_sees_only_its_own_tab_and_older_ones(self):
        self.assertEqual(engines.seen_families(machine(RTX3090)), {"nvidia_ampere", "nvidia_turing"})
        self.assertEqual(tab_ids(RTX3090), ["qwen", "rtx3040"])
        tabs = {t["id"]: t for t in engines.visible_tabs(machine(RTX3090))}
        self.assertFalse(tabs["qwen"]["compatible"])
        self.assertEqual(tabs["qwen"]["reason_code"], "family")
        self.assertTrue(tabs["rtx3040"]["compatible"])

    def test_30_and_40_series_share_one_tab(self):
        self.assertEqual(tab_ids(RTX4090), tab_ids(RTX3090))
        self.assertEqual(len([t for t in engines.TABS if t["label"] == "RTX 30/40"]), 1)
        self.assertEqual(engines.seen_families(machine(RTX4090)), {"nvidia_ada", "nvidia_ampere", "nvidia_turing"})
        self.assertEqual(tab_ids(RTX2080), ["qwen", "rtx3040"])

    def test_other_hardware_shows_only_its_tabs(self):
        self.assertEqual(tab_ids(APPLE), ["qwen", "mac", "unified"])
        self.assertEqual(tab_ids(STRIX), ["qwen", "amd", "unified"])
        self.assertEqual(tab_ids(MI300X), ["qwen", "amd"])
        self.assertEqual(tab_ids(ARC), ["qwen", "intel"])
        self.assertEqual(tab_ids(GB10), ["qwen", "rtx3040", "unified"])
        self.assertEqual(tab_ids(NO_GPU), ["qwen"])
        self.assertNotIn("unified", tab_ids(RTX5090))

    def test_show_all_lists_every_tab_and_marks_the_ones_that_do_not_fit(self):
        tabs = {t["id"]: t for t in engines.visible_tabs(machine(RTX5090), show_all=True)}
        self.assertEqual(list(tabs), [t["id"] for t in engines.TABS])
        self.assertTrue(tabs["qwen"]["compatible"] and tabs["rtx3040"]["compatible"])
        for tid in ("mac", "amd", "intel", "unified"):
            self.assertFalse(tabs[tid]["compatible"], tid)
            self.assertEqual(tabs[tid]["reason_code"], "hardware", tid)

    def test_the_qwen_tab_is_always_listed_and_not_hideable(self):
        self.assertTrue(next(t for t in engines.TABS if t["id"] == "qwen")["builtin"])
        for env in (RTX5090, RTX3090, APPLE, NO_GPU, MI300X):
            self.assertEqual(tab_ids(env)[0], "qwen")


class MemoryPool(unittest.TestCase):
    def test_single_pool_only_on_unified_memory(self):
        for env in (APPLE, STRIX, MI300A, GB10):
            m = machine(env)
            self.assertEqual(engines.memory_pools(m), ["split", "single"], env)
            self.assertEqual(engines.memory_pool("single", m), "single")
            self.assertEqual(engines.memory_pool(None, m), "split")

    def test_never_offered_on_a_discrete_gpu_or_when_unknown(self):
        for env in (RTX5090, RTX4090, RTX3090, RTX2080, MI300X, RX7900, ARC, NO_GPU, LAPTOP_APU):
            m = machine(env)
            self.assertEqual(engines.memory_pools(m), ["split"], env)
            self.assertEqual(engines.memory_pool("split", m), "split")
            with self.assertRaises(ValueError):
                engines.memory_pool("single", m)
        with self.assertRaises(ValueError):
            engines.memory_pool("wide", machine(APPLE))


def connect(port: int) -> http.client.HTTPConnection:
    """A connection to the local server; a connect attempt the loopback drops is retried on a new socket."""
    for _ in range(8):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        try:
            c.connect()
            c.sock.settimeout(30)
            return c
        except OSError:
            c.close()
    raise OSError(f"127.0.0.1:{port} does not accept connections")


class Http(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"TADAMOO_CACHE": self.dir.name, "TADAMOO_DATA": self.dir.name,
                                                "TADAMOO_FAKE_SYSTEM_OK": "1", **self.fake})
        self.env.start()
        self.no_engines = mock.patch.object(engines, "ROOT", Path(self.dir.name))     # no engine installed on this PC leaks in
        self.no_engines.start()
        self.path = os.path.join(self.dir.name, "tadamoo_config.json")
        Path(self.path).write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
        self.svc = make_service()
        self.svc.settings_info = {"config": self.path, "model": None, "cli": [], "running": {"engine.max-context": 4096}}
        self.sup = self.svc.supervisor = supervisor.Supervisor(self.svc, ["--mock-engine", "--config", self.path], mock=True)
        self.system = mock.patch.object(loader, "system_now", return_value=dict(ROOMY))
        self.system.start()
        for _ in range(20):             # a port the loopback cannot reach is replaced
            self.httpd = serve(self.svc, port=0)
            self.port = self.httpd.server_address[1]
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
                break
            except OSError:
                self.httpd.shutdown()
                self.httpd.server_close()

    fake = RTX5090

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.system.stop()
        self.no_engines.stop()
        self.env.stop()
        self.dir.cleanup()

    def req(self, method, path, body=None):
        c = connect(self.port)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                c.request(method, path, body=None if body is None else json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"} if body is not None else {})
                r = c.getresponse()
                raw = r.read()
            return r.status, json.loads(raw) if raw[:1] in (b"{", b"[") else raw
        finally:
            c.close()


class Route(Http):
    def test_the_list_for_a_5090(self):
        code, body = self.req("GET", "/api/loader/engines")
        self.assertEqual(code, 200)
        by_id = {e["id"]: e for e in body["engines"]}
        self.assertEqual(sorted(by_id), ["basalt", "strata"])
        for e in by_id.values():
            for key in ("id", "label", "installed", "compatible", "reason", "families", "models", "capabilities"):
                self.assertIn(key, e)
        self.assertTrue(by_id["basalt"]["compatible"] and by_id["strata"]["compatible"])
        self.assertIsNone(by_id["basalt"]["reason"])
        self.assertFalse(by_id["strata"]["installed"])
        self.assertEqual([t["id"] for t in body["tabs"]], ["qwen", "rtx3040"])
        self.assertEqual(body["hardware"]["families"], ["nvidia_blackwell"])
        self.assertEqual(body["memory_pools"], ["split"])

    def test_all_engines(self):
        code, body = self.req("GET", "/api/loader/engines?all=1")
        self.assertEqual(code, 200)
        self.assertEqual([t["id"] for t in body["tabs"]], [t["id"] for t in engines.TABS])

    def test_install_is_a_stub_that_downloads_nothing(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("downloaded")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("started a process")):
            code, body = self.req("POST", "/api/loader/engines/strata/install", {})
        self.assertEqual(code, 501)
        self.assertEqual(body["error"]["code"], "not_available_yet")
        self.assertIn("not available yet", body["error"]["message"])
        code, body = self.req("POST", "/api/loader/engines/nothing/install", {})
        self.assertEqual(code, 404)

    def test_the_other_loader_posts_still_refuse_unknown_paths(self):
        self.assertEqual(self.req("POST", "/api/loader/engines/strata", {})[0], 404)
        self.assertEqual(self.req("POST", "/api/loader/nothing", {})[0], 404)


class RouteOn3090(Http):
    fake = RTX3090

    def test_the_list(self):
        body = self.req("GET", "/api/loader/engines")[1]
        by_id = {e["id"]: e for e in body["engines"]}
        self.assertFalse(by_id["basalt"]["compatible"])
        self.assertEqual(by_id["basalt"]["reason_code"], "family")
        self.assertTrue(by_id["strata"]["compatible"])


class RouteOnApple(Http):
    fake = APPLE

    def test_the_list(self):
        body = self.req("GET", "/api/loader/engines")[1]
        self.assertEqual([t["id"] for t in body["tabs"]], ["qwen", "mac", "unified"])
        self.assertEqual(body["memory_pools"], ["split", "single"])


def golden_state() -> dict:
    """The Loader's plan and safe settings for fixed readings."""
    values = {"engine.kv": "int8", "engine.max-context": 65536, "engine.spec": 7}
    out = {"plan_safe": loader.plan(loader.SAFE, F, ROOMY), "plan_values": loader.plan(values, F, ROOMY),
           "safe": loader.safe(F, ROOMY)}
    return json.loads(json.dumps(out, default=str))


class Golden(Http):
    keys = ("values", "safe", "safe_why", "plan", "gpu", "gpu_warning", "reserve", "unsupported", "fields", "defaults",
            "editable", "reason", "limits")

    def state(self):
        code, body = self.req("GET", "/api/loader")
        self.assertEqual(code, 200)
        out = {k: body[k] for k in self.keys}
        out["limits"] = {k: v for k, v in out["limits"].items() if k not in ("gpus", "cpu_threads", "windows")}
        out["offline"] = golden_state()
        return json.loads(json.dumps(out, default=str))

    def test_the_blackwell_loader_state_is_what_it_was(self):
        state = self.state()
        self.assertEqual(state, json.loads(GOLDEN.read_text(encoding="utf-8")))
        self.assertIsNone(state["gpu_warning"]["en"])


class WebTabs(unittest.TestCase):
    page = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "engines.js").read_text(encoding="utf-8")

    def test_the_page_loads_the_engine_tabs_and_keeps_the_loader_markup(self):
        self.assertIn('src="web/engines.js"', self.page)
        self.assertIn('href="web/engines.css"', self.page)
        for ident in ("sub-providers", "sub-qwen", "sub-converter", "ld-providers", "ld-qwen", "ld-converter", "ld-gpu", "ld-form"):
            self.assertIn(f'id="{ident}"', self.page)
        self.assertLess(self.page.index("web/providers.js"), self.page.index("web/engines.js"))

    def test_the_script_reads_the_route_and_the_stub(self):
        self.assertIn('"api/loader/engines"', self.js)
        self.assertIn("/install", self.js)
        self.assertIn("authHeaders()", self.js)
        self.assertIn("basalt:models-sub", self.js)

    def test_every_engine_key_is_in_both_languages(self):
        import re
        from serve.tests.test_web_static import NODE, strings
        if not NODE:
            self.skipTest("node is needed to read the strings")
        strings = strings()
        used = {k for k in re.findall(r'T\(\s*"(eng\.[\w.]+)"', self.js) if not k.endswith(".")}
        for name in ("max_context", "kv", "spec", "mtp", "expert_cache", "vram_reserve", "parallel", "tokenizer"):
            used |= {f"eng.c.{name}", f"eng.c.{name}.help"}
        used |= {f"eng.fam.{f}" for f in engines.FAMILIES}
        used |= {f"eng.state.{x}" for x in ("stopped", "starting", "loading", "running", "error", "installed", "not")}
        used |= {"eng.model.bad.nextn", "eng.model.bad.family"}
        used |= {f"eng.e.{c}" for c in (
            "start_failed", "start_timeout", "load_failed", "flag_unsupported", "control_unsupported", "in_use", "copy_failed",
            "source_incomplete", "source_missing", "config_unreadable", "model_unsupported", "spawn_failed", "not_installed",
            "busy", "binary_missing", "tokenizer_missing", "model_missing", "family_mismatch", "profile_missing",
            "install_running")}
        for tid in ("rtx3040", "mac", "amd", "intel", "unified"):
            used |= {f"eng.tab.{tid}", f"eng.desc.{tid}"}
        used |= {f"eng.why.{c}" for c in ("no_gpu", "family", "sm", "hardware")}
        used |= {"eng.e.not_available_yet", "eng.e.unknown_engine"}
        for lang in ("en", "it"):
            self.assertEqual(sorted(k for k in used if k not in strings[lang]), [], lang)
        for key in ("eng.install", "eng.install.help", "eng.all", "eng.all.help", "eng.mem.single", "eng.desc.rtx3040"):
            self.assertNotEqual(strings["en"][key], strings["it"][key], key)
        for lang in ("en", "it"):
            for key, text in strings[lang].items():
                if key.startswith("eng."):
                    self.assertNotIn("planner", text.lower(), key)


if __name__ == "__main__":
    unittest.main()
