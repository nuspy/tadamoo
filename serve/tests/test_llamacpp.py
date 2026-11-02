"""llama-server in the Loader: finding it, starting and stopping it, its health, and an update that shows what it would download and waits.
Nothing here runs a program or reaches the network: the machine is a fake."""
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from serve import llamacpp
from serve.llamacpp import LlamaCpp, LlamaError


class FakeProc:
    def __init__(self, argv):
        self.argv, self.pid, self.code = argv, 4242, None

    def poll(self):
        return self.code


def zip_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


class FakeHost:
    platform = "win32"
    machine = "amd64"

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.env, self.on_path, self.files, self.saved = {}, None, set(), {}
        self.version_text = "version: 6000 (abc1234)\nbuilt with clang"
        self.runs, self.procs, self.killed = [], [], []
        self.busy_ports, self.health = set(), {}
        self.release, self.fetches, self.release_list = None, 0, None
        self.contents, self.downloads = {}, []

    def environ(self):
        return dict(self.env)

    def which(self, name):
        return self.on_path

    def is_file(self, path):
        return path in self.files

    def data_dir(self):
        return self.tmp

    def load_settings(self):
        return dict(self.saved)

    def save_settings(self, value):
        self.saved = dict(value)

    def run(self, argv, timeout):
        self.runs.append(argv)
        return 0, self.version_text

    def popen(self, argv, log, cwd):
        proc = FakeProc(argv)
        self.procs.append(proc)
        return proc

    def kill(self, proc):
        self.killed.append(proc.pid)
        proc.code = 1

    def port_free(self, port):
        return port not in self.busy_ports

    def http_get(self, port, path, timeout=2.0):
        code, body = self.health.get(port, (0, b""))
        return code, body

    def fetch_json(self, url):
        self.fetches += 1
        if url.endswith("per_page=20") and self.release_list is not None:
            return self.release_list
        if isinstance(self.release, Exception):
            raise self.release
        return self.release

    def download(self, url, dest, progress):
        self.downloads.append(url)
        data = self.contents[url]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        progress(len(data))


def release(tag="b6100", extra=None, win_main=b"", win_rt=b""):
    main = f"llama-{tag}-bin-win-cuda-12.4-x64.zip"
    rt = "cudart-llama-bin-win-cuda-12.4-x64.zip"
    base = "https://github.com/ggml-org/llama.cpp/releases/download/" + tag + "/"
    assets = [{"name": main, "size": len(win_main), "browser_download_url": base + main, "digest": "sha256:" + hashlib.sha256(win_main).hexdigest()},
              {"name": rt, "size": len(win_rt), "browser_download_url": base + rt},
              {"name": f"llama-{tag}-bin-win-cuda-13.1-x64.zip", "size": 1, "browser_download_url": base + "x"},
              {"name": f"llama-{tag}-bin-ubuntu-x64.zip", "size": 9, "browser_download_url": base + "u"},
              {"name": f"llama-{tag}-bin-macos-arm64.zip", "size": 9, "browser_download_url": base + "m"}]
    return {"tag_name": tag, "name": tag, "published_at": "2026-10-30T10:00:00Z", "assets": [*assets, *(extra or [])]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.host = FakeHost(self.tmp.name)
        self.mgr = LlamaCpp(self.host)
        self.exe = str(Path(self.tmp.name) / "tools" / llamacpp.EXE)
        self.model = str(Path(self.tmp.name) / "small.gguf")
        self.host.files |= {self.exe, self.model}

    def tearDown(self):
        self.tmp.cleanup()


class Finding(Base):
    def test_nothing_found_is_said_plainly(self):
        found = self.mgr.find()
        self.assertEqual((found["path"], found["version"], found["candidates"]), (None, "", []))
        self.assertFalse(self.mgr.status()["found"])
        self.assertEqual(self.host.runs, [])                                    # no program, nothing to ask a version of

    def test_the_saved_path_comes_before_the_environment_and_the_path(self):
        other = str(Path(self.tmp.name) / "other" / llamacpp.EXE)
        third = str(Path(self.tmp.name) / "third" / llamacpp.EXE)
        self.host.files |= {other, third}
        self.host.env[llamacpp.ENV_PATH] = other
        self.host.on_path = third
        self.assertEqual([s for s, _ in self.mgr.candidates()], ["env", "path"])
        self.host.saved = {"path": self.exe}
        found = self.mgr.find()
        self.assertEqual((found["path"], found["source"]), (self.exe, "saved"))
        self.assertEqual([c["source"] for c in found["candidates"]], ["saved", "env", "path"])
        self.assertEqual(found["version"], "version: 6000 (abc1234)")

    def test_the_version_is_asked_once_per_program(self):
        self.host.saved = {"path": self.exe}
        for _ in range(3):
            self.mgr.status()
        self.assertEqual(self.host.runs, [[self.exe, "--version"]])

    def test_a_path_is_remembered_only_when_it_is_a_llama_server_file(self):
        out = self.mgr.set_path(self.exe)
        self.assertEqual((out["path"], self.host.saved["path"]), (self.exe, self.exe))
        for bad in (self.model, "C:/nowhere/llama-server.exe", 5):
            with self.assertRaises(LlamaError) as caught:
                self.mgr.set_path(bad)
            self.assertEqual(caught.exception.code, "bad_path")
        self.mgr.set_path("")
        self.assertNotIn("path", self.host.saved)


class Running(Base):
    def setUp(self):
        super().setUp()
        self.host.saved = {"path": self.exe}

    def start(self, **over):
        return self.mgr.start({"model": self.model, **over})

    def test_it_starts_on_loopback_with_only_the_flags_the_page_offers_and_remembers_them(self):
        out = self.start(port=8099, ctx=4096, parallel=2, ngl=20, alias="tech")
        argv = self.host.procs[0].argv
        self.assertEqual(argv[:3], [self.exe, "-m", self.model])
        self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1")
        self.assertEqual(argv[argv.index("--port") + 1], "8099")
        self.assertEqual(argv[argv.index("-c") + 1], "8192")                    # per slot x slots: the server shares the context between them
        self.assertEqual(argv[argv.index("-np") + 1], "2")
        self.assertEqual(argv[argv.index("--alias") + 1], "tech")
        self.assertEqual(out["state"], "starting")
        self.assertEqual(out["base_url"], "http://127.0.0.1:8099/v1")
        self.assertEqual((self.host.saved["port"], self.host.saved["model"], self.host.saved["path"]), (8099, self.model, self.exe))

    def test_health_decides_between_starting_and_ready(self):
        self.start(port=8099)
        self.host.health[8099] = (503, b'{"error": {"message": "Loading model"}}')
        self.assertEqual(self.mgr.status()["state"], "starting")
        self.host.health[8099] = (200, b'{"status": "ok"}')
        status = self.mgr.status()
        self.assertEqual((status["state"], status["health"]["ready"], status["pid"]), ("ready", True, 4242))

    def test_a_bad_request_is_refused_with_the_field_named_and_nothing_starts(self):
        for body, code in (({"model": "nothing.gguf"}, "model_missing"), ({"model": self.exe}, "model_missing"),
                           ({"model": self.model, "port": 80}, "bad_port"), ({"model": self.model, "port": "8080"}, "bad_port"),
                           ({"model": self.model, "ctx": 10}, "bad_ctx"), ({"model": self.model, "parallel": 99}, "bad_parallel"),
                           ({"model": self.model, "ngl": True}, "bad_ngl"), ({"model": self.model, "alias": "a\nb"}, "bad_alias")):
            with self.assertRaises(LlamaError) as caught:
                self.mgr.start(body)
            self.assertEqual((caught.exception.status, caught.exception.code), (400, code), body)
        self.assertEqual(self.host.procs, [])

    def test_no_program_a_busy_port_and_a_second_start_are_refused(self):
        self.host.saved = {}
        with self.assertRaises(LlamaError) as caught:
            self.start()
        self.assertEqual(caught.exception.code, "not_found")
        self.host.saved = {"path": self.exe}
        self.host.busy_ports.add(8080)
        with self.assertRaises(LlamaError) as caught:
            self.start()
        self.assertEqual(caught.exception.code, "port_busy")
        self.host.busy_ports.clear()
        self.start()
        with self.assertRaises(LlamaError) as caught:
            self.start(port=8081)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "running"))
        self.assertEqual(len(self.host.procs), 1)

    def test_stop_ends_the_process_this_server_started_and_only_that(self):
        self.start()
        out = self.mgr.stop()
        self.assertEqual((self.host.killed, out["state"]), ([4242], "stopped"))
        self.host.health[8080] = (200, b'{"status": "ok"}')                       # someone else's llama-server on the port
        self.assertEqual(self.mgr.status()["state"], "external")
        with self.assertRaises(LlamaError) as caught:
            self.mgr.stop()
        self.assertEqual(caught.exception.code, "not_ours")
        self.assertEqual(self.host.killed, [4242])

    def test_another_program_on_the_port_is_not_taken_for_a_llama_server(self):
        self.host.health[8080] = (404, b"<html>not here</html>")
        self.assertEqual(self.mgr.status()["state"], "stopped")
        self.start()                                                              # the port is free for the bind check, and nothing llama-server-like answers
        self.assertEqual(len(self.host.procs), 1)

    def test_a_program_that_exits_is_an_error_with_the_end_of_its_log(self):
        self.start()
        self.host.procs[0].code = 3
        status = self.mgr.status()
        self.assertEqual((status["state"], status["error"]["code"]), ("error", "exited"))
        self.assertIn("exit 3", status["error"]["message"])
        self.assertIsNone(status["pid"])
        self.start()                                                              # a new start clears the error
        self.assertEqual(self.mgr.status()["state"], "starting")

    def test_it_starts_again_with_the_server_only_when_the_person_asked(self):
        self.assertFalse(self.mgr.start_if_saved())
        self.start(autostart=False)
        self.mgr.stop()
        self.assertFalse(self.mgr.start_if_saved())
        self.assertEqual(len(self.host.procs), 1)
        self.start(autostart=True)
        self.mgr.stop()
        self.assertTrue(self.mgr.start_if_saved())
        self.assertEqual(len(self.host.procs), 3)
        self.mgr.stop()
        self.host.files.discard(self.model)
        self.assertFalse(self.mgr.start_if_saved())                               # the file is gone: no start, no error to the server


class Updating(Base):
    def setUp(self):
        super().setUp()
        self.main, self.rt = zip_bytes({"build/bin/" + llamacpp.EXE: "MZ-new", "build/bin/ggml.dll": "x"}), zip_bytes({"cudart64_12.dll": "y"})
        self.host.release = release(win_main=self.main, win_rt=self.rt)
        base = "https://github.com/ggml-org/llama.cpp/releases/download/b6100/"
        self.host.contents = {base + "llama-b6100-bin-win-cuda-12.4-x64.zip": self.main, base + "cudart-llama-bin-win-cuda-12.4-x64.zip": self.rt}

    def test_the_check_shows_release_files_sizes_and_destination_and_downloads_nothing(self):
        plan = self.mgr.update_check()
        self.assertEqual(plan["release"], "b6100")
        self.assertEqual([f["name"] for f in plan["files"]], ["llama-b6100-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip"])
        self.assertEqual(plan["total_bytes"], len(self.main) + len(self.rt))
        self.assertEqual(plan["destination"], str(Path(self.tmp.name) / "llama.cpp" / "b6100"))
        self.assertTrue(plan["needs_confirmation"])
        self.assertEqual((self.host.fetches, self.host.downloads), (1, []))
        self.assertEqual(self.mgr.status()["plan"]["token"], plan["token"])

    def test_when_the_latest_tag_carries_no_binaries_the_newest_release_that_has_one_is_offered(self):
        self.host.release = {"tag_name": "v0.8.0", "assets": [{"name": "nightly-tag.txt", "size": 4, "browser_download_url": "https://github.com/x/nightly-tag.txt"}]}
        self.host.release_list = [{"tag_name": "v0.8.0", "assets": []}, release(win_main=self.main, win_rt=self.rt)]
        plan = self.mgr.update_check()
        self.assertEqual(plan["release"], "b6100")
        self.assertEqual(self.host.downloads, [])

    def test_linux_and_mac_take_their_own_build_and_no_build_is_said(self):
        self.host.platform = "linux"
        self.assertEqual([f["name"] for f in self.mgr.update_check()["files"]], ["llama-b6100-bin-ubuntu-x64.zip"])
        self.host.platform, self.host.machine = "darwin", "arm64"
        self.assertEqual([f["name"] for f in self.mgr.update_check()["files"]], ["llama-b6100-bin-macos-arm64.zip"])
        self.host.release = {"tag_name": "b6100", "assets": []}
        with self.assertRaises(LlamaError) as caught:
            self.mgr.update_check()
        self.assertEqual(caught.exception.code, "no_asset")

    def test_an_unreachable_or_odd_release_list_and_a_download_address_off_github_are_refused(self):
        self.host.release = OSError("no network")
        with self.assertRaises(LlamaError) as caught:
            self.mgr.update_check()
        self.assertEqual((caught.exception.status, caught.exception.code), (502, "release_unreachable"))
        self.host.release = ["not", "a", "release"]
        with self.assertRaises(LlamaError) as caught:
            self.mgr.update_check()
        self.assertEqual(caught.exception.code, "release_unreadable")
        odd = release(win_main=self.main, win_rt=self.rt)
        odd["assets"][0]["browser_download_url"] = "https://evil.example/llama.zip"
        self.host.release = odd
        with self.assertRaises(LlamaError) as caught:
            self.mgr.update_check()
        self.assertEqual(caught.exception.code, "bad_url")
        odd["assets"][0]["browser_download_url"] = "http://github.com/x.zip"        # not https
        with self.assertRaises(LlamaError):
            self.mgr.update_check()

    def test_up_to_date_compares_the_build_numbers(self):
        self.host.saved = {"path": self.exe}
        self.host.version_text = "version: 6100 (abc)"
        self.assertTrue(self.mgr.update_check()["up_to_date"])
        self.mgr._versions.clear()
        self.host.version_text = "version: 6000 (abc)"
        self.assertFalse(self.mgr.update_check()["up_to_date"])

    def test_nothing_is_downloaded_without_the_confirmation_of_the_current_plan(self):
        plan = self.mgr.update_check()
        for token, confirmed, code in ((plan["token"], None, "not_confirmed"), (plan["token"], "yes", "not_confirmed"),
                                       (plan["token"], False, "not_confirmed"), ("0" * 24, True, "plan_changed"), (None, True, "plan_changed")):
            with self.assertRaises(LlamaError) as caught:
                self.mgr.update_apply(token, confirmed, wait=True)
            self.assertEqual(caught.exception.code, code)
        fresh = LlamaCpp(self.host)                                                 # no plan was ever shown to this one
        with self.assertRaises(LlamaError) as caught:
            fresh.update_apply(plan["token"], True, wait=True)
        self.assertEqual(caught.exception.code, "plan_changed")
        self.assertEqual(self.host.downloads, [])

    def test_a_confirmed_plan_downloads_exactly_its_files_and_installs_them(self):
        plan = self.mgr.update_check()
        asked = []

        def fake(url, dest, progress):
            asked.append(url)
            self.host.download(url, dest, progress)
        job = self.mgr.update_apply(plan["token"], True, downloader=fake, wait=True)
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(asked, [f["url"] for f in plan["files"]])
        self.assertEqual(job["done"], plan["total_bytes"])
        dest = Path(plan["destination"])
        installed = next(dest.rglob(llamacpp.EXE))
        self.assertEqual(installed.read_text(), "MZ-new")
        self.assertTrue((dest / "cudart64_12.dll").is_file())
        self.assertFalse(dest.with_name("b6100.download").exists() or dest.with_name("b6100.unpack").exists())
        current = json.loads((Path(self.tmp.name) / "llama.cpp" / "current.json").read_text())
        self.assertEqual((current["release"], current["path"]), ("b6100", str(installed)))
        self.host.files.add(str(installed))
        self.assertIn(("update", str(installed)), self.mgr.candidates())

    def test_a_wrong_size_a_wrong_checksum_or_an_archive_that_escapes_fails_and_leaves_nothing(self):
        plan = self.mgr.update_check()
        cases = {"size_mismatch": lambda u, d, p: (d.parent.mkdir(parents=True, exist_ok=True), d.write_bytes(b"short"), p(5)),
                 "hash_mismatch": lambda u, d, p: (d.parent.mkdir(parents=True, exist_ok=True), d.write_bytes(b"x" * len(self.main)), p(1))}
        for code, bad in cases.items():
            job = self.mgr.update_apply(plan["token"], True, downloader=bad, wait=True)
            self.assertEqual((job["status"], job["error"]["code"]), ("error", code), job)
            self.assertFalse(Path(plan["destination"]).exists())
        evil = zip_bytes({"../escape.txt": "x"})
        self.host.release = release(win_main=evil, win_rt=self.rt)
        plan = self.mgr.update_check()

        def serve_evil(url, dest, progress):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(evil if "cudart" not in url else self.rt)
        job = self.mgr.update_apply(plan["token"], True, downloader=serve_evil, wait=True)
        self.assertEqual((job["status"], job["error"]["code"]), ("error", "bad_archive"))
        self.assertFalse((Path(self.tmp.name) / "llama.cpp" / "escape.txt").exists() or (Path(self.tmp.name) / "escape.txt").exists())

    def test_the_update_waits_while_the_program_runs(self):
        plan = self.mgr.update_check()
        self.host.saved = {"path": self.exe}
        self.mgr.start({"model": self.model})
        with self.assertRaises(LlamaError) as caught:
            self.mgr.update_apply(plan["token"], True, wait=True)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "in_use"))
        self.assertEqual(self.host.downloads, [])

    def test_a_program_an_earlier_update_installed_moves_to_the_new_one_but_the_persons_own_stays(self):
        plan = self.mgr.update_check()
        old = str(Path(self.tmp.name) / "llama.cpp" / "b5000" / llamacpp.EXE)
        self.host.saved = {"path": old}
        self.mgr.update_apply(plan["token"], True, wait=True)
        self.assertTrue(self.host.saved["path"].startswith(str(Path(self.tmp.name) / "llama.cpp" / "b6100")))
        self.host.saved = {"path": self.exe}
        plan = self.mgr.update_check()
        self.mgr.update_apply(plan["token"], True, wait=True)
        self.assertEqual(self.host.saved["path"], self.exe)


class Routes(Base):
    def setUp(self):
        super().setUp()
        from serve import llamacpp_api
        self.api = llamacpp_api
        self.before, llamacpp._MANAGER = llamacpp._MANAGER, self.mgr
        self.host.saved = {"path": self.exe}

    def tearDown(self):
        llamacpp._MANAGER = self.before
        super().tearDown()

    def test_the_routes_map_to_the_manager_and_errors_carry_their_code(self):
        code, out = self.api.route_get("/api/llamacpp")
        self.assertEqual((code, out["found"], out["state"]), (200, True, "stopped"))
        code, out = self.api.route_post("/api/llamacpp/start", {"model": self.model, "port": 8123, "junk": 1})
        self.assertEqual((code, out["state"]), (202, "starting"))
        code, out = self.api.route_post("/api/llamacpp/start", {"model": self.model})
        self.assertEqual((code, out["error"]["code"]), (409, "running"))
        self.assertEqual(self.api.route_post("/api/llamacpp/stop", {})[0], 200)
        code, out = self.api.route_post("/api/llamacpp/update/apply", {"token": "x", "confirm": True})
        self.assertEqual((code, out["error"]["code"]), (409, "plan_changed"))
        self.assertEqual(self.api.route_post("/api/llamacpp/nothing", {})[0], 404)
        self.assertEqual(self.api.route_get("/api/llamacpp/other")[0], 404)


if __name__ == "__main__":
    unittest.main()
