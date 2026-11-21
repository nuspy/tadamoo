"""Restarting the server from a client, over the real server: it answers first, then asks tadamoo.restart to do the restart (mocked here: nothing real is touched).
It also asks the voice service (a different process, started by the desktop app) to restart, best-effort; and a separate route restarts only that one."""
from unittest import mock

from serve import voice_service
from serve.tests.test_api_plans import PlansApiBase
from tadamoo import restart as restart_mod

KEY = "test-restart-key-0001"


class AdminRestartApiTest(PlansApiBase):
    api_key = KEY

    def headers(self):
        return {"Authorization": f"Bearer {KEY}"}

    def test_the_server_answers_then_asks_for_the_restart(self):
        fake = mock.MagicMock(return_value={"way": "relaunch", "command": ["x.exe"]})
        with mock.patch.object(restart_mod, "restart", fake), mock.patch.object(voice_service, "restart", lambda: False):
            code, res = self.call("POST", "/api/admin/restart", {}, headers=self.headers())
        self.assertEqual((code, res["way"]), (200, "relaunch"))
        self.assertEqual(fake.call_count, 1)

    def test_a_service_restart_is_reported_too_and_the_wrong_key_is_refused(self):
        with mock.patch.object(restart_mod, "restart", lambda: {"way": "service", "name": "TaDaMoo"}), \
             mock.patch.object(voice_service, "restart", lambda: False):
            code, res = self.call("POST", "/api/admin/restart", {}, headers=self.headers())
        self.assertEqual((code, res["name"]), (200, "TaDaMoo"))
        with mock.patch.object(restart_mod, "restart") as fake:
            code, res = self.call("POST", "/api/admin/restart", {}, headers={"Authorization": "Bearer wrong"})
        self.assertEqual(code, 401)
        self.assertEqual(fake.call_count, 0)

    def test_the_voice_service_restart_is_asked_too_and_reported(self):
        fake = mock.MagicMock(return_value=True)
        with mock.patch.object(restart_mod, "restart", lambda: {"way": "relaunch", "command": ["x.exe"]}), \
             mock.patch.object(voice_service, "restart", fake):
            code, res = self.call("POST", "/api/admin/restart", {}, headers=self.headers())
        self.assertEqual((code, res["voice_service_restarted"]), (200, True))
        self.assertEqual(fake.call_count, 1)

    def test_the_voice_service_alone_can_be_restarted_without_touching_the_server(self):
        with mock.patch.object(restart_mod, "restart") as server_fake, mock.patch.object(voice_service, "restart", lambda: True):
            code, res = self.call("POST", "/api/admin/voice-service/restart", {}, headers=self.headers())
        self.assertEqual((code, res["restarted"]), (200, True))
        self.assertEqual(server_fake.call_count, 0)

    def test_the_voice_service_restart_route_says_when_there_was_none_running(self):
        with mock.patch.object(voice_service, "restart", lambda: False):
            code, res = self.call("POST", "/api/admin/voice-service/restart", {}, headers=self.headers())
        self.assertEqual((code, res["restarted"]), (200, False))
        with mock.patch.object(voice_service, "restart") as fake:
            code, _ = self.call("POST", "/api/admin/voice-service/restart", {}, headers={"Authorization": "Bearer wrong"})
        self.assertEqual(code, 401)
        self.assertEqual(fake.call_count, 0)
