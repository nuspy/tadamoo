"""GET and POST /api/computer over the real server: the setting is kept in the data folder, the screens come from the system's backend (a made-up one here, so no screen is
read), saving is for this PC only, and the conversations are started with the flags only when it is on."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import computer_api, handler
from serve.tests.test_api_plans import PlansApiBase
from tadamoo.computer import ComputerPrefs
from tadamoo.computer.backend import Backend, Monitor, UnsupportedBackend


class TwoScreens(Backend):
    def monitors(self):
        return [Monitor(1, 0, 0, 1920, 1080, True), Monitor(2, 1920, 0, 2560, 1440)]


class ComputerApiTest(PlansApiBase):
    api_key = "test-computer-key"

    def setUp(self):
        self.data = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(handler.userdata, "path", lambda name: Path(self.data.name) / name),
                        mock.patch.object(computer_api, "pick_backend", lambda *a, **k: TwoScreens())]
        for p in self.patches:
            p.start()
        self.headers = {"Authorization": f"Bearer {self.api_key}"}

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.data.cleanup()

    def test_it_is_off_until_the_person_turns_it_on_and_lists_the_screens(self):
        code, res = self.call("GET", "/api/computer", headers=self.headers)
        self.assertEqual(code, 200)
        self.assertEqual((res["enabled"], res["monitor"], res["supported"]), (False, 0, True))
        self.assertEqual([(m["index"], m["width"]) for m in res["monitors"]], [(1, 1920), (2, 2560)])

    def test_the_setting_is_saved_and_a_bad_one_is_refused(self):
        code, res = self.call("POST", "/api/computer", {"enabled": True, "monitor": 2}, headers=self.headers)
        self.assertEqual((code, res["enabled"], res["monitor"]), (200, True, 2))
        self.assertEqual(json.loads((Path(self.data.name) / "computer.json").read_text(encoding="utf-8")), {"enabled": True, "monitor": 2, "backend": "auto"})
        code, res = self.call("POST", "/api/computer", {"monitor": 0}, headers=self.headers)                   # what is left out stays
        self.assertEqual((code, res["enabled"], res["monitor"]), (200, True, 0))
        self.assertEqual(self.call("POST", "/api/computer", {"monitor": 3}, headers=self.headers)[0], 400)
        self.assertEqual(self.call("POST", "/api/computer", {"backend": "banana"}, headers=self.headers)[0], 400)
        code, res = self.call("POST", "/api/computer", {"backend": "native"}, headers=self.headers)
        self.assertEqual((code, res["backend"], res["in_use"]), (200, "native", "native"))
        self.assertEqual(self.call("POST", "/api/computer", {"enabled": "yes"}, headers=self.headers)[0], 400)
        self.assertEqual(self.call("POST", "/api/computer", {"monitor": -1}, headers=self.headers)[0], 400)
        self.assertEqual(self.call("POST", "/api/computer", {"enabled": True})[0], 401)

    def test_it_is_saved_only_from_this_pc(self):
        with mock.patch.object(handler.settings, "is_loopback", lambda address: False):
            code, res = self.call("POST", "/api/computer", {"enabled": True}, headers=self.headers)
        self.assertEqual(code, 403)
        self.assertFalse(ComputerPrefs(Path(self.data.name) / "computer.json").read()["enabled"])

    def test_agent_desktop_is_listed_with_its_version_and_status_and_is_used_unless_native_is_chosen(self):
        tools = Path(self.data.name) / "tools" / "agent-desktop"
        tools.mkdir(parents=True)
        (tools / "agent-desktop.exe").write_bytes(b"x")
        (tools / "install.json").write_text(json.dumps({"version": "1.3.1", "sha256_verified": True}), encoding="utf-8")
        status = {"version": {"version": "1.3.1"}, "status": {"permissions": {"accessibility": {"state": "granted"}}}}
        res = computer_api.view(ComputerPrefs(Path(self.data.name) / "computer.json"), lambda: TwoScreens(), status=lambda *a, **k: status)
        self.assertEqual((res["in_use"], res["agent_desktop"]["found"], res["agent_desktop"]["version"], res["agent_desktop"]["where"]), ("agent-desktop", True, "1.3.1", "installed"))
        self.assertEqual(res["agent_desktop"]["status"]["status"]["permissions"]["accessibility"]["state"], "granted")
        ComputerPrefs(Path(self.data.name) / "computer.json").write(False, 0, "native")
        res = computer_api.view(ComputerPrefs(Path(self.data.name) / "computer.json"), lambda: TwoScreens(), status=lambda *a, **k: status)
        self.assertEqual(res["in_use"], "native")

    def test_installing_is_asked_for_only_from_this_pc_and_a_failure_is_told(self):
        result = {"version": "9.9.9", "path": "x", "sha256": "0" * 64, "sha256_verified": True, "note": ""}
        with mock.patch.object(computer_api.installer, "install", lambda tools, **k: result):
            code, res = self.call("POST", "/api/computer/install", {}, headers=self.headers)
            self.assertEqual((code, res["installed"]["version"]), (200, "9.9.9"))
            with mock.patch.object(handler.settings, "is_loopback", lambda address: False):
                self.assertEqual(self.call("POST", "/api/computer/install", {}, headers=self.headers)[0], 403)
        self.assertEqual(self.call("POST", "/api/computer/install", {})[0], 401)

        def fail(tools, **k):
            raise computer_api.ComputerError("could not download: offline")
        with mock.patch.object(computer_api.installer, "install", fail):
            code, res = self.call("POST", "/api/computer/install", {}, headers=self.headers)
        self.assertEqual((code, res["error"]["type"]), (502, "install_failed"))
        self.assertIn("offline", res["error"]["message"])

    def test_a_system_that_cannot_do_it_says_why(self):
        with mock.patch.object(computer_api, "pick_backend", lambda *a, **k: UnsupportedBackend("this Linux session has no X11 display")):
            code, res = self.call("GET", "/api/computer", headers=self.headers)
        self.assertEqual((res["supported"], res["monitors"]), (False, []))
        self.assertIn("no X11 display", res["reason"])


if __name__ == "__main__":
    unittest.main()
