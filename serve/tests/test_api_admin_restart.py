"""Restarting the server from a client, over the real server: it answers first, then asks tadamoo.restart to do the restart (mocked here: nothing real is touched)."""
from unittest import mock

from serve.tests.test_api_plans import PlansApiBase
from tadamoo import restart as restart_mod

KEY = "test-restart-key-0001"


class AdminRestartApiTest(PlansApiBase):
    api_key = KEY

    def headers(self):
        return {"Authorization": f"Bearer {KEY}"}

    def test_the_server_answers_then_asks_for_the_restart(self):
        fake = mock.MagicMock(return_value={"way": "relaunch", "command": ["x.exe"]})
        with mock.patch.object(restart_mod, "restart", fake):
            code, res = self.call("POST", "/api/admin/restart", {}, headers=self.headers())
        self.assertEqual((code, res["way"]), (200, "relaunch"))
        self.assertEqual(fake.call_count, 1)

    def test_a_service_restart_is_reported_too_and_the_wrong_key_is_refused(self):
        with mock.patch.object(restart_mod, "restart", lambda: {"way": "service", "name": "TaDaMoo"}):
            code, res = self.call("POST", "/api/admin/restart", {}, headers=self.headers())
        self.assertEqual((code, res["name"]), (200, "TaDaMoo"))
        with mock.patch.object(restart_mod, "restart") as fake:
            code, res = self.call("POST", "/api/admin/restart", {}, headers={"Authorization": "Bearer wrong"})
        self.assertEqual(code, 401)
        self.assertEqual(fake.call_count, 0)
