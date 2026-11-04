"""The REA switch: what it registers and removes, the tool detection, and that an installer runs only on request. No network, no real winget."""
from __future__ import annotations

import fnmatch
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import rea_api


class FakeHost(rea_api.Host):
    platform = "win32"

    def __init__(self, files=(), on_path=None, env=None, results=None):
        self.files = set(files)
        self.on_path = dict(on_path or {})
        self.env = dict(env or {})
        self.results = results or {}
        self.ran = []

    def environ(self):
        return dict(self.env)

    def which(self, name):
        return self.on_path.get(name)

    def glob(self, pattern):
        return sorted(f for f in self.files if fnmatch.fnmatch(f, pattern.replace("\\", "/")))

    def exists(self, path):
        return path.replace("\\", "/") in self.files

    def run(self, argv, timeout):
        self.ran.append(list(argv))
        for key, value in self.results.items():
            if key in " ".join(argv):
                return value
        return 0, ""


def make_addons(root: Path) -> Path:
    """A fake add-on folder: node, REA's entry script and its one skill."""
    (root / "node").mkdir(parents=True)
    (root / "node" / ("node.exe" if os.name == "nt" else "node")).write_text("x")
    pkg = root / "rea-agents" / "node_modules" / "rea-agents"
    (pkg / "scripts").mkdir(parents=True)
    (pkg / "scripts" / "rea.mjs").write_text("// rea")
    skill = pkg / "skills" / rea_api.SKILL_NAME
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: reverse-engineer-anything\ndescription: Reverse engineer things.\n---\nbody\n")
    (skill / "references" / "environments.md").write_text("env")
    (root / "addons.json").write_text(json.dumps({"addons": {"rea-agents": {"version": "6.8.0"}, "node": {"version": "24.22.1"}}}))
    return root


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.data = base / "data"
        self.addons = make_addons(base / "addons")
        patch = mock.patch.dict(os.environ, {"TADAMOO_DATA": str(self.data), "TADAMOO_ADDONS": str(self.addons)})
        patch.start()
        self.addCleanup(patch.stop)
        self.host = FakeHost(results={"--version": (0, "v24.22.1\n")})
        patch = mock.patch.object(rea_api, "HOST", self.host)
        patch.start()
        self.addCleanup(patch.stop)

    def store(self):
        from tadamoo.discovery.installed import InstalledStore
        return InstalledStore(self.data / "capabilities")


class SwitchTests(Base):
    def test_it_is_off_until_asked(self):
        s = rea_api.status()
        self.assertFalse(s["enabled"])
        self.assertTrue(s["addon"]["present"])
        self.assertFalse(s["mcp"]["registered"])
        self.assertFalse(s["skills"]["imported"])
        self.assertEqual(s["addon"]["version"], "6.8.0")

    def test_turning_it_on_registers_the_server_and_the_skill(self):
        s = rea_api.enable()
        self.assertTrue(s["enabled"] and s["mcp"]["registered"] and s["skills"]["imported"])
        rec = self.store().get("mcp:rea")
        self.assertEqual(rec["kind"], "mcp")
        self.assertEqual(rec["command"][1:], [str(self.addons / "rea-agents" / "node_modules" / "rea-agents" / "scripts" / "rea.mjs"), "mcp"])
        self.assertTrue(Path(rec["command"][0]).is_file())
        skill = self.data / "capabilities" / "skills" / rea_api.SKILL_NAME
        self.assertTrue((skill / "SKILL.md").is_file())
        self.assertTrue((skill / "references" / "environments.md").is_file())

    def test_the_capability_index_takes_both_as_approved(self):
        from tadamoo.capabilities import CapabilityIndex
        rea_api.enable()
        index = CapabilityIndex(self.data / "capabilities", self.data / "ws", home=self.data / "home", appdata=self.data / "appdata")
        server, skill = index.get("mcp:rea"), index.get("skill:reverse-engineer-anything")
        self.assertIsNotNone(server)
        self.assertIsNotNone(skill)
        self.assertEqual((server["state"], server["origin"]), ("approved", "tadamoo"))
        self.assertEqual(skill["state"], "approved")

    def test_turning_it_off_removes_exactly_what_was_added(self):
        caps = self.data / "capabilities"
        self.store().put({"id": "mcp:other", "name": "other", "kind": "mcp", "command": ["x"], "source": "me"})
        (caps / "skills" / "mine").mkdir(parents=True)
        (caps / "skills" / "mine" / "SKILL.md").write_text("---\nname: mine\ndescription: d\n---\n")
        rea_api.enable()
        s = rea_api.disable()
        self.assertFalse(s["enabled"] or s["mcp"]["registered"] or s["skills"]["imported"])
        self.assertIsNone(self.store().get("mcp:rea"))
        self.assertIsNotNone(self.store().get("mcp:other"))
        self.assertTrue((caps / "skills" / "mine" / "SKILL.md").is_file())
        self.assertFalse((caps / "skills" / rea_api.SKILL_NAME).exists())

    def test_what_the_person_made_with_the_same_name_is_not_touched(self):
        self.store().put({"id": "mcp:rea", "name": "rea", "kind": "mcp", "command": ["mine"], "source": "me"})
        with self.assertRaises(rea_api.ReaError) as ctx:
            rea_api.enable()
        self.assertEqual(ctx.exception.code, "name_taken")
        rea_api.disable()
        self.assertEqual(self.store().get("mcp:rea")["command"], ["mine"])

    def test_without_the_add_on_it_cannot_be_turned_on(self):
        gone = Path(self.tmp.name) / "nowhere"
        with mock.patch.dict(os.environ, {"TADAMOO_ADDONS": str(gone)}), mock.patch.object(rea_api.addons, "_ROOT", gone), \
                mock.patch.object(rea_api.addons, "_DEV", gone), mock.patch.object(rea_api.addons, "_MAIN_DEV", gone):
            self.assertFalse(rea_api.status()["addon"]["present"])
            with self.assertRaises(rea_api.ReaError) as ctx:
                rea_api.enable()
        self.assertEqual(ctx.exception.code, "no_addon")
        self.assertFalse(rea_api.read_state()["enabled"])

    def test_the_state_is_kept_and_applied_again_at_start(self):
        self.assertFalse(rea_api.reapply())
        rea_api.enable()
        self.store().delete("mcp:rea")                         # lost, for instance by a copy of the data folder
        seen = []
        rea_api.start_if_enabled(lambda: seen.append(1))
        self.assertIsNotNone(self.store().get("mcp:rea"))
        self.assertEqual(seen, [1])

    def test_when_it_is_off_the_start_adds_nothing(self):
        rea_api.start_if_enabled(lambda: self.fail("refreshed"))
        self.assertIsNone(self.store().get("mcp:rea"))

    def test_the_server_gets_the_programs_found_as_variables(self):
        self.host.files |= {"C:/Tools/Ghidra/ghidra_12.1.4_PUBLIC", "C:/Tools/Ghidra/ghidra_12.1.4_PUBLIC/ghidraRun.bat", "C:/p/adb.exe"}
        self.host.on_path["adb"] = "C:/p/adb.exe"
        rea_api.enable()
        env = {e["name"]: e["value"] for e in self.store().get("mcp:rea")["env"]}
        self.assertEqual(env["GHIDRA_INSTALL_DIR"], "C:/Tools/Ghidra/ghidra_12.1.4_PUBLIC")
        self.assertEqual(env["REA_ADB_PATH"], "C:/p/adb.exe")
        self.assertNotIn("REA_TSHARK_COMMAND", env)


class ToolTests(Base):
    def test_detection_gives_path_and_version(self):
        self.host.files |= {"C:/Tools/Ghidra/ghidra_12.1.4_PUBLIC", "C:/Tools/Ghidra/ghidra_12.1.4_PUBLIC/ghidraRun.bat",
                            "C:/Tools/Java21/jdk-21.0.1+12", "C:/Tools/Java21/jdk-21.0.1+12/bin/java.exe"}
        views = {v["id"]: v for v in rea_api.tools_view()}
        self.assertTrue(views["ghidra"]["detected"])
        self.assertEqual(views["ghidra"]["version"], "12.1.4")
        self.assertEqual(views["jdk"]["version"], "21.0.1+12")
        self.assertFalse(views["adb"]["detected"])

    def test_a_variable_already_set_wins(self):
        self.host.env["REA_ADB_PATH"] = "D:/x/adb.exe"
        self.host.files.add("D:/x/adb.exe")
        self.assertEqual({v["id"]: v for v in rea_api.tools_view()}["adb"]["path"], "D:/x/adb.exe")

    def test_a_missing_tool_shows_its_exact_command(self):
        self.host.on_path["winget"] = "C:/w/winget.exe"
        views = {v["id"]: v for v in rea_api.tools_view()}
        self.assertEqual(views["adb"]["command"][:4], ["winget", "install", "--id", "Google.PlatformTools"])
        self.assertEqual(views["wabt"]["command"], [])         # no installer: by hand

    def test_nothing_runs_while_only_looking(self):
        self.host.on_path["winget"] = "C:/w/winget.exe"
        rea_api.status()
        rea_api.tools_view()
        self.assertFalse([a for a in self.host.ran if "install" in a])


class InstallTests(Base):
    def setUp(self):
        super().setUp()
        self.host.on_path["winget"] = "C:/w/winget.exe"
        self.host.on_path["python"] = "C:/py/python.exe"

    def installs(self):
        return [a for a in self.host.ran if "install" in a]

    def test_one_tool_runs_its_own_command_and_shows_the_output(self):
        self.host.results["Google.PlatformTools"] = (0, "Successfully installed\n")
        res = rea_api.install("adb")
        self.assertEqual(len(res["results"]), 1)
        self.assertEqual(res["results"][0]["output"], "Successfully installed\n")
        self.assertEqual(res["results"][0]["code"], 0)
        self.assertEqual(self.installs(), [res["results"][0]["command"]])

    def test_a_failure_is_reported_not_hidden(self):
        self.host.results["Google.Chrome"] = (1, "No package found")
        res = rea_api.install("chrome")
        self.assertEqual((res["results"][0]["code"], res["results"][0]["output"]), (1, "No package found"))

    def test_an_unknown_tool_runs_nothing(self):
        for bad in ("calc", None, 5, "../x"):
            with self.assertRaises(rea_api.ReaError):
                rea_api.install(bad)
        self.assertFalse(self.installs())

    def test_a_tool_with_no_installer_is_skipped(self):
        res = rea_api.install("wabt")
        self.assertEqual(res["results"][0]["skipped"], "manual")
        self.assertFalse(self.installs())

    def test_install_all_runs_only_the_missing_ones_that_have_a_command(self):
        self.host.on_path["adb"] = "C:/p/adb.exe"
        self.host.files.add("C:/p/adb.exe")
        res = rea_api.install(None, True)
        done = {r["tool"] for r in res["results"]}
        self.assertNotIn("adb", done)
        self.assertNotIn("wabt", done)
        self.assertIn("chrome", done)
        self.assertIn("unblob", done)
        self.assertTrue(all(r["command"] for r in res["results"]))

    def test_the_pip_command_uses_the_user_site(self):
        res = rea_api.install("pwntools")
        self.assertEqual(res["results"][0]["command"], ["C:/py/python.exe", "-m", "pip", "install", "--user", "pwntools"])

    def test_after_an_install_the_registered_server_learns_the_new_path(self):
        rea_api.enable()
        self.assertNotIn("REA_ADB_PATH", {e["name"] for e in self.store().get("mcp:rea")["env"]})
        self.host.files.add("C:/p/adb.exe")
        self.host.on_path["adb"] = "C:/p/adb.exe"
        rea_api.install("adb")
        self.assertEqual({e["name"]: e["value"] for e in self.store().get("mcp:rea")["env"]}["REA_ADB_PATH"], "C:/p/adb.exe")

    def test_only_one_installation_at_a_time(self):
        rea_api._install_lock.acquire()
        self.addCleanup(rea_api._install_lock.release)
        with self.assertRaises(rea_api.ReaError) as ctx:
            rea_api.install("adb")
        self.assertEqual(ctx.exception.code, "busy")

    def test_on_other_systems_there_is_no_winget_command(self):
        self.host.platform = "linux"
        self.assertEqual({v["id"]: v for v in rea_api.tools_view()}["adb"]["command"], [])


class RouteTests(Base):
    def test_routes(self):
        self.assertEqual(rea_api.route_get("/api/rea")[0], 200)
        self.assertEqual(rea_api.route_get("/api/rea/x")[0], 404)
        code, obj = rea_api.route_post("/api/rea/enable", {})
        self.assertEqual((code, obj["enabled"]), (200, True))
        code, obj = rea_api.route_post("/api/rea/disable", {})
        self.assertEqual((code, obj["enabled"]), (200, False))
        self.assertEqual(rea_api.route_post("/api/rea/install", {"tool": "nope"})[0], 400)
        self.assertEqual(rea_api.route_post("/api/rea/other", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
