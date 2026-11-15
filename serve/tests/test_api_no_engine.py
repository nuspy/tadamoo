"""A server with no engine binary (the harness alone): the Loader's Load says so clearly, in both languages' fields, and the health says no engine is installed."""
import unittest

from serve.tests.test_api_plans import PlansApiBase


class NoEngineApiTest(PlansApiBase):
    def setUp(self):
        self.svc.engine_installed = False

    def tearDown(self):
        self.svc.engine_installed = True

    def test_load_says_there_is_no_engine_and_not_an_obscure_error(self):
        code, res = self.call("POST", "/api/loader/load", {"values": {}})
        self.assertEqual(code, 409)
        err = res["error"]
        self.assertEqual(err["code"], "no_engine")
        self.assertIn("No engine is installed", err["message"])
        self.assertIn("nessun motore", err["message_it"])

    def test_the_health_and_the_loader_status_say_no_engine_is_installed(self):
        self.assertIs(self.call("GET", "/health")[1]["engine_installed"], False)
        self.assertIs(self.call("GET", "/api/loader/status")[1]["engine_installed"], False)

    def test_with_an_engine_the_flag_is_true_and_load_is_not_refused_for_it(self):
        self.svc.engine_installed = True
        self.assertIs(self.call("GET", "/health")[1]["engine_installed"], True)
        code, res = self.call("POST", "/api/loader/load", {"values": {}})
        self.assertNotEqual((res.get("error") or {}).get("code"), "no_engine")


if __name__ == "__main__":
    unittest.main()
