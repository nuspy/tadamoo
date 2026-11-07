"""The REA switch: what it registers and removes, the tool detection, and that an installer runs only on request. No network, no real winget."""
from __future__ import annotations

import fnmatch
import glob
import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import rea_api


class FakeHost(rea_api.Host):
    """A machine with no network. `results` answers commands (a tuple, a function of the command, or a list of them used one after the other);
    paths under `real_root` are looked up on the real disk, where the downloads and the unpacking of the tests are made."""
    platform = "win32"

    def __init__(self, files=(), on_path=None, env=None, results=None, payloads=None, releases=None):
        self.files = set(files)
        self.on_path = dict(on_path or {})
        self.env = dict(env or {})
        self.results = results or {}
        self.payloads = dict(payloads or {})
        self.releases = dict(releases or {})
        self.real_root = ""
        self.ran, self.slept, self.fetched, self.refreshed = [], [], [], 0
        self.on_path_files = []

    def environ(self):
        return dict(self.env)

    def which(self, name):
        return self.on_path.get(name)

    def _real(self, path):
        return bool(self.real_root) and os.path.normpath(path).startswith(os.path.normpath(self.real_root))

    def glob(self, pattern):
        fake = sorted(f for f in self.files if fnmatch.fnmatch(f, pattern.replace("\\", "/")))
        return fake + (sorted(glob.glob(pattern)) if self._real(pattern) else [])

    def exists(self, path):
        return path.replace("\\", "/") in self.files or (self._real(path) and os.path.exists(path))

    def run(self, argv, timeout):
        self.ran.append(list(argv))
        for key, value in self.results.items():
            if key in " ".join(argv):
                if isinstance(value, list):
                    value = value.pop(0) if len(value) > 1 else value[0]
                return value(argv) if callable(value) else value
        return 0, ""

    def sleep(self, seconds):
        self.slept.append(seconds)

    def refresh_path(self):
        self.refreshed += 1

    def find_in_path(self, pattern):
        return sorted(f for f in self.on_path_files if fnmatch.fnmatch(os.path.basename(f), pattern))

    def fetch(self, url, dest):
        self.fetched.append(url)
        if url not in self.payloads:
            raise OSError("network error: no route")
        Path(dest).write_bytes(self.payloads[url])
        return hashlib.sha256(self.payloads[url]).hexdigest()

    def http_json(self, url):
        self.fetched.append(url)
        if url not in self.releases:
            raise OSError("network error: no route")
        return self.releases[url]


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
        self.host.real_root = str(self.data)
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

    def test_a_missing_tool_shows_its_exact_command_and_its_steps(self):
        self.host.on_path["winget"] = "C:/w/winget.exe"
        views = {v["id"]: v for v in rea_api.tools_view()}
        self.assertEqual(views["adb"]["command"][:4], ["winget", "install", "--id", "Google.PlatformTools"])
        self.assertEqual(views["adb"]["steps"], [" ".join(views["adb"]["command"])])
        self.assertEqual(views["wabt"]["command"], [])                      # it comes from a GitHub release: no command, one step
        self.assertTrue(views["wabt"]["installable"])
        self.assertIn("github.com/WebAssembly/wabt", views["wabt"]["steps"][0])
        self.assertIn("jadx-headless-mcp-0.7.1-all.jar", views["jadx"]["steps"][0])
        self.assertIn("SHA-256", views["jadx"]["steps"][0])

    def test_a_program_that_exists_only_for_linux_is_not_offered_on_windows(self):
        views = {v["id"]: v for v in rea_api.tools_view()}
        self.assertEqual((views["unblob"]["supported"], views["unblob"]["installable"], views["unblob"]["only"]), (False, False, ["linux"]))
        self.host.platform = "linux"
        self.host.on_path["python"] = "/usr/bin/python"
        views = {v["id"]: v for v in rea_api.tools_view()}
        self.assertTrue(views["unblob"]["supported"])
        self.assertEqual(views["unblob"]["command"], ["/usr/bin/python", "-m", "pip", "install", "--user", "unblob"])

    def test_a_program_on_the_path_gives_its_folder(self):
        self.host.files |= {"D:/jdk17/bin/java.exe", "D:/wb/bin/wasm-validate.exe"}
        self.host.on_path.update({"java": "D:/jdk17/bin/java.exe", "wasm-validate": "D:/wb/bin/wasm-validate.exe"})
        views = {v["id"]: v for v in rea_api.tools_view()}
        self.assertEqual(views["jdk"]["path"].replace("\\", "/"), "D:/jdk17")
        self.assertEqual(views["wabt"]["path"].replace("\\", "/"), "D:/wb/bin")

    def test_the_android_sdk_variable_is_a_place_to_look_for_adb(self):
        self.host.env["ANDROID_HOME"] = "E:/Dev/android-sdk"
        self.host.files.add("E:/Dev/android-sdk/platform-tools/adb.exe")
        self.host.env["REA_ADB_PATH"] = ""
        views = {v["id"]: v for v in rea_api.tools_view()}
        self.assertFalse(views["adb"]["detected"])                          # the pattern is expanded by the real environment, not the fake one

    def test_nothing_runs_while_only_looking(self):
        self.host.on_path["winget"] = "C:/w/winget.exe"
        rea_api.status()
        rea_api.tools_view()
        self.assertFalse([a for a in self.host.ran if "install" in a])
        self.assertFalse(self.host.fetched)


class InstallTests(Base):
    def setUp(self):
        super().setUp()
        self.host.on_path["winget"] = "C:/w/winget.exe"
        self.host.on_path["python"] = "C:/py/python.exe"
        self.host.files.update({"C:/Tools/Java21/jdk-21.0.1+12", "C:/Tools/Java21/jdk-21.0.1+12/bin/java.exe"})   # Java is there unless a test removes it
        self.host.results["find_spec"] = (1, "")                             # pwntools is not importable until pip installs it

    def installs(self):
        return [a for a in self.host.ran if "install" in a]

    def appears(self, *paths, out="Successfully installed\n", on_path=None):
        def run(argv):
            self.host.files.update(paths)
            self.host.on_path.update(on_path or {})
            return 0, out
        return run

    def results(self, res):
        return {r["tool"]: r for r in res["results"]}

    def test_one_tool_runs_its_own_command_and_shows_the_output(self):
        self.host.results["Google.PlatformTools"] = self.appears("C:/p/adb.exe", on_path={"adb": "C:/p/adb.exe"})
        res = rea_api.install("adb", wait=True)
        r = res["results"][0]
        self.assertEqual((r["ok"], r["code"], r["attempts"], r["method"], r["output"]), (True, 0, 1, "winget", "Successfully installed\n"))
        self.assertEqual(self.installs(), [r["command"]])
        self.assertGreaterEqual(self.host.refreshed, 1)                      # the PATH an installer changed reaches the server

    def test_a_failure_that_is_not_the_network_is_reported_once_and_not_hidden(self):
        self.host.results["Google.Chrome"] = (1, "No package found")
        r = rea_api.install("chrome", wait=True)["results"][0]
        self.assertEqual((r["ok"], r["code"], r["output"], r["attempts"]), (False, 1, "No package found", 1))
        self.assertEqual(self.host.slept, [])

    def test_a_network_failure_is_tried_again_and_the_second_try_can_succeed(self):
        self.host.results["Google.Chrome"] = [(1, "Download failed: timed out"), self.appears("C:/c/chrome.exe", on_path={"chrome": "C:/c/chrome.exe"})]
        r = rea_api.install("chrome", wait=True)["results"][0]
        self.assertEqual((r["ok"], r["attempts"]), (True, 2))
        self.assertEqual(self.host.slept, [5])

    def test_it_gives_up_after_three_network_failures(self):
        self.host.results["Google.Chrome"] = (1, "could not resolve host")
        r = rea_api.install("chrome", wait=True)["results"][0]
        self.assertEqual((r["ok"], r["attempts"]), (False, 3))
        self.assertEqual(self.host.slept, [5, 20])

    def test_when_one_way_fails_the_next_one_is_tried(self):
        self.host.files.clear()
        self.host.results["Microsoft.OpenJDK.21"] = (1, "No package found")
        self.host.results["Temurin"] = self.appears("C:/Tools/Java21/jdk-21.0.1+12", "C:/Tools/Java21/jdk-21.0.1+12/bin/java.exe")
        r = rea_api.install("jdk", wait=True)["results"][0]
        self.assertEqual((r["ok"], r["attempts"]), (True, 2))
        self.assertIn("EclipseAdoptium.Temurin.21.JDK", r["command"])

    def test_a_program_is_installed_after_what_it_needs(self):
        self.host.files.clear()
        self.host.results["Microsoft.OpenJDK.21"] = self.appears("C:/Tools/Java21/jdk-21.0.1+12", "C:/Tools/Java21/jdk-21.0.1+12/bin/java.exe")
        self.host.results["Ghidra"] = self.appears("C:/Tools/Ghidra/ghidra_12.1.4_PUBLIC", "C:/Tools/Ghidra/ghidra_12.1.4_PUBLIC/ghidraRun.bat")
        res = rea_api.install("ghidra", wait=True)
        self.assertEqual(res["plan"], ["jdk", "ghidra"])
        self.assertEqual([r["tool"] for r in res["results"]], ["jdk", "ghidra"])
        self.assertTrue(all(r["ok"] for r in res["results"]))
        self.assertIn("Microsoft.OpenJDK.21", " ".join(self.installs()[0]))
        self.assertIn("NationalSecurityAgency.Ghidra", " ".join(self.installs()[1]))

    def test_what_needs_a_program_that_could_not_be_installed_is_skipped_and_nothing_of_it_runs(self):
        self.host.files.clear()
        self.host.results["OpenJDK"] = (1, "No package found")
        self.host.results["Temurin"] = (1, "No package found")
        res = rea_api.install("ghidra", wait=True)
        got = self.results(res)
        self.assertFalse(got["jdk"]["ok"])
        self.assertEqual((got["ghidra"]["ok"], got["ghidra"]["skipped"], got["ghidra"]["output"]), (False, "needs", "jdk"))
        self.assertFalse([a for a in self.installs() if "Ghidra" in " ".join(a)])

    def test_install_all_takes_what_is_missing_here_in_order_and_leaves_out_the_rest(self):
        self.host.files.clear()
        self.host.on_path["adb"] = "C:/p/adb.exe"
        self.host.files.add("C:/p/adb.exe")
        res = rea_api.install(None, True, wait=True)
        plan = res["plan"]
        self.assertNotIn("adb", plan)                                       # found
        self.assertNotIn("unblob", plan)                                    # Linux only
        for tool in ("jdk", "ghidra", "jadx", "apktool", "chrome", "tshark", "wabt", "pwntools", "rizin"):
            self.assertIn(tool, plan)
        self.assertLess(plan.index("jdk"), min(plan.index(t) for t in ("ghidra", "jadx", "apktool")))

    def test_the_jadx_jar_is_downloaded_and_checked_against_its_sha256(self):
        jar = b"PK jar bytes"
        with mock.patch.object(rea_api, "JADX_SHA256", hashlib.sha256(jar).hexdigest()):
            self.host.payloads[rea_api.JADX_URL] = jar
            r = rea_api.install("jadx", wait=True)["results"][0]
        self.assertTrue(r["ok"], r)
        self.assertEqual((self.data / "rea-tools" / "jadx-headless-mcp-0.7.1-all.jar").read_bytes(), jar)

    def test_a_download_with_the_wrong_sha256_is_refused_and_not_kept(self):
        self.host.payloads[rea_api.JADX_URL] = b"something else"
        r = rea_api.install("jadx", wait=True)["results"][0]
        self.assertEqual((r["ok"], r["code"], r["attempts"]), (False, 65, 1))
        self.assertIn("SHA-256 mismatch", r["output"])
        self.assertFalse(list((self.data / "rea-tools").glob("*")) if (self.data / "rea-tools").exists() else [])

    def test_a_jar_already_on_the_path_gets_a_launcher_and_nothing_is_downloaded(self):
        self.host.on_path_files = ["E:/Program Files/REA_Components/APKTool/apktool_3.0.3.jar"]
        views = {v["id"]: v for v in rea_api.tools_view()}
        self.assertIn("nothing is downloaded", views["apktool"]["steps"][0])
        r = rea_api.install("apktool", wait=True)["results"][0]
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["attempts"], 1)
        self.assertFalse(self.host.fetched)
        bat = (self.data / "rea-tools" / "apktool" / "apktool.bat").read_text(errors="replace")
        self.assertIn("apktool_3.0.3.jar", bat)
        self.assertIn("-jar", bat)

    def test_apktool_comes_from_its_release_with_a_launcher_that_finds_java(self):
        jar = b"PK apktool"
        url = "https://github.com/iBotPeaches/Apktool/releases/download/v9.9.9/apktool_9.9.9.jar"
        self.host.releases["https://api.github.com/repos/iBotPeaches/Apktool/releases/latest"] = {
            "tag_name": "v9.9.9", "assets": [{"name": "apktool_9.9.9.jar", "browser_download_url": url, "digest": "sha256:" + hashlib.sha256(jar).hexdigest()}]}
        self.host.payloads[url] = jar
        r = rea_api.install("apktool", wait=True)["results"][0]
        self.assertTrue(r["ok"], r)
        folder = self.data / "rea-tools" / "apktool"
        self.assertEqual((folder / "apktool.jar").read_bytes(), jar)
        bat = (folder / "apktool.bat").read_text()
        self.assertIn("JAVA_HOME", bat)
        self.assertIn("-jar", bat)

    def test_a_release_archive_is_unpacked_without_repeating_its_top_folder(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for name in ("wabt-1.0.42/bin/wasm-validate.exe", "wabt-1.0.42/bin/wasm2wat.exe"):
                info = tarfile.TarInfo(name)
                info.size = 3
                t.addfile(info, io.BytesIO(b"exe"))
        data = buf.getvalue()
        url = "https://github.com/WebAssembly/wabt/releases/download/1.0.42/wabt-1.0.42-windows-x64.tar.gz"
        self.host.releases["https://api.github.com/repos/WebAssembly/wabt/releases/tags/1.0.42"] = {
            "tag_name": "1.0.42", "assets": [{"name": "wabt-1.0.42-windows-x64.tar.gz", "browser_download_url": url, "digest": "sha256:" + hashlib.sha256(data).hexdigest()}]}
        self.host.payloads[url] = data
        r = rea_api.install("wabt", wait=True)["results"][0]
        self.assertTrue(r["ok"], r)
        self.assertTrue((self.data / "rea-tools" / "wabt-1.0.42" / "bin" / "wasm-validate.exe").is_file())

    def test_an_archive_that_writes_outside_its_folder_is_refused(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            info = tarfile.TarInfo("../evil.txt")
            info.size = 1
            t.addfile(info, io.BytesIO(b"x"))
        data = buf.getvalue()
        url = "https://x/wabt-1.0.42-windows-x64.tar.gz"
        self.host.releases["https://api.github.com/repos/WebAssembly/wabt/releases/tags/1.0.42"] = {
            "tag_name": "1.0.42", "assets": [{"name": "wabt-1.0.42-windows-x64.tar.gz", "browser_download_url": url, "digest": "sha256:" + hashlib.sha256(data).hexdigest()}]}
        self.host.payloads[url] = data
        r = rea_api.install("wabt", wait=True)["results"][0]
        self.assertFalse(r["ok"])
        self.assertFalse((self.data / "rea-tools" / "evil.txt").exists())

    def test_without_network_a_github_way_fails_with_a_clear_reason_and_is_tried_again(self):
        r = rea_api.install("rizin", wait=True)["results"][0]                 # winget answers (0, "") and installs nothing: then GitHub, no route
        self.assertFalse(r["ok"])
        self.assertIn("network error", r["output"])
        self.assertEqual(self.host.slept, [5, 20])

    def test_a_tool_that_is_found_is_not_installed_again(self):
        self.host.on_path["adb"] = "C:/p/adb.exe"
        self.host.files.add("C:/p/adb.exe")
        r = rea_api.install("adb", wait=True)["results"][0]
        self.assertEqual((r["ok"], r["skipped"]), (True, "present"))
        self.assertFalse(self.installs())

    def test_a_program_for_another_system_is_skipped(self):
        r = rea_api.install("unblob", wait=True)["results"][0]
        self.assertEqual((r["ok"], r["skipped"]), (False, "platform"))
        self.assertFalse(self.installs())

    def test_an_unknown_tool_runs_nothing(self):
        for bad in ("calc", None, 5, "../x"):
            with self.assertRaises(rea_api.ReaError):
                rea_api.install(bad)
        self.assertFalse(self.installs())

    def test_the_pip_command_uses_the_user_site(self):
        def pip(argv):
            self.host.results["find_spec"] = (0, "")
            return 0, "Successfully installed pwntools"
        self.host.results["pwntools"] = pip
        r = rea_api.install("pwntools", wait=True)["results"][0]
        self.assertTrue(r["ok"])
        self.assertEqual(r["command"], ["C:/py/python.exe", "-m", "pip", "install", "--user", "pwntools"])

    def test_after_an_install_the_registered_server_learns_the_new_path(self):
        rea_api.enable()
        self.assertNotIn("REA_ADB_PATH", {e["name"] for e in self.store().get("mcp:rea")["env"]})
        self.host.results["Google.PlatformTools"] = self.appears("C:/p/adb.exe", on_path={"adb": "C:/p/adb.exe"})
        rea_api.install("adb", wait=True)
        self.assertEqual({e["name"]: e["value"] for e in self.store().get("mcp:rea")["env"]}["REA_ADB_PATH"], "C:/p/adb.exe")

    def test_the_installation_runs_in_the_background_and_the_status_follows_it(self):
        gate = __import__("threading").Event()

        def slow(argv):
            gate.wait(5)
            self.host.files.add("C:/p/adb.exe")
            self.host.on_path["adb"] = "C:/p/adb.exe"
            return 0, "ok"
        self.host.results["Google.PlatformTools"] = slow
        started = rea_api.install("adb")
        self.assertTrue(started["started"])
        self.assertTrue(rea_api.status()["install"]["running"])
        self.assertEqual(rea_api.status()["install"]["plan"], ["adb"])
        gate.set()
        rea_api._job_thread.join(5)
        done = rea_api.status()["install"]
        self.assertFalse(done["running"])
        self.assertTrue(done["results"][0]["ok"])

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
        self.host.on_path["adb"] = "C:/p/adb.exe"
        self.host.files.add("C:/p/adb.exe")
        code, obj = rea_api.route_post("/api/rea/install", {"tool": "adb"})
        rea_api._job_thread.join(5)
        self.assertEqual((code, obj["started"], obj["plan"]), (200, True, ["adb"]))
        self.assertEqual(rea_api.route_post("/api/rea/other", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
