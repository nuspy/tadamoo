"""The control secret is off unless the person asks for it, and the Loader saves the switch and the chosen text."""
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from serve import control, handler, settings
from serve.config import build_parser
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
SECRET = "my own secret, 123"


class Option(unittest.TestCase):
    def test_it_is_off_by_default_and_on_when_asked(self):
        self.assertFalse(build_parser().parse_args(["--model", "m.basalt"]).control_secret)
        self.assertTrue(build_parser().parse_args(["--model", "m.basalt", "--control-secret"]).control_secret)

    def test_the_chosen_text_is_the_secret_and_without_one_it_is_random(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(control, "token_path", lambda port: Path(d) / f"control-{port}.token"):
            self.assertEqual(control.write_control_token(7000, SECRET), SECRET)
            self.assertEqual((Path(d) / "control-7000.token").read_text(encoding="ascii" if SECRET.isascii() else "utf-8"), SECRET)
            random_one = control.write_control_token(7001)
            self.assertEqual(len(random_one), 48)


class Saved(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "config.json")
        Path(self.path).write_text(json.dumps({"server": {"port": 8080}, "engine": {"kv": "int8"}}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_switch_and_the_text_are_saved_with_the_other_keys_kept_and_the_state_never_holds_the_text(self):
        self.assertEqual(settings.control_secret_state(self.path), {"enabled": False, "set": False})
        settings.save(self.path, {"server.control-secret": True, "server.control-secret-value": SECRET}, settings.CONTROL_FIELDS)
        raw = json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual((raw["server"]["control-secret"], raw["server"]["control-secret-value"], raw["server"]["port"], raw["engine"]["kv"]), (True, SECRET, 8080, "int8"))
        state = settings.control_secret_state(self.path)
        self.assertEqual(state, {"enabled": True, "set": True})
        self.assertNotIn(SECRET, json.dumps(state))

    def test_a_removed_text_is_gone_and_a_bad_one_is_refused(self):
        settings.save(self.path, {"server.control-secret-value": SECRET}, settings.CONTROL_FIELDS)
        settings.save(self.path, {"server.control-secret-value": None}, settings.CONTROL_FIELDS)
        self.assertFalse(settings.control_secret_state(self.path)["set"])
        for bad in ("", "   ", "two\nlines", "caffè", "x" * 201, 5, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                settings.save(self.path, {"server.control-secret-value": bad}, settings.CONTROL_FIELDS)
        with self.assertRaises(ValueError):
            settings.save(self.path, {"server.control-secret": "yes"}, settings.CONTROL_FIELDS)

    def test_the_other_pages_do_not_set_it_and_do_not_read_it_back(self):
        settings.save(self.path, {"server.control-secret": True, "server.control-secret-value": SECRET}, settings.CONTROL_FIELDS)
        for fields in (settings.FIELDS, settings.LOADER_FIELDS):
            self.assertNotIn(SECRET, json.dumps(settings.values_of(settings.read_file(self.path), fields)))
            with self.assertRaises(ValueError):
                settings.save(self.path, {"server.control-secret-value": "x"}, fields)

    def test_the_saved_file_is_one_the_server_accepts(self):
        settings.save(self.path, {"server.control-secret": True, "server.control-secret-value": SECRET}, settings.CONTROL_FIELDS)
        settings.check_whole(settings.read_file(self.path), self.path)


class Route(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tok = ByteTokenizer()
        cls.svc = Service(MockEngine(tok, "</think>\n\nok", max_context=32768), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = str(Path(cls.tmp.name) / "config.json")
        Path(cls.path).write_text("{}", encoding="utf-8")
        cls.svc.settings_info = {"config": cls.path}
        cls.httpd = serve(cls.svc, port=0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def call(self, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.base + "/api/control-secret", data=data, headers={"Content-Type": "application/json"}, method="POST" if payload is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_the_page_saves_the_switch_and_the_text_and_never_gets_the_text_back(self):
        code, state = self.call()
        self.assertEqual((code, state["enabled"], state["set"], state["running"]), (200, False, False, False))
        code, saved = self.call({"enabled": True, "secret": SECRET})
        self.assertEqual((code, saved["enabled"], saved["set"], saved["changed"].count("server.control-secret")), (200, True, True, 1))
        self.assertNotIn(SECRET, json.dumps(saved) + json.dumps(self.call()[1]))
        self.assertEqual(self.call({"secret": None})[1]["set"], False)
        self.assertEqual(self.call({"enabled": False})[1]["enabled"], False)

    def test_a_request_that_means_nothing_or_is_wrong_is_refused(self):
        self.assertEqual(self.call({})[0], 400)
        self.assertEqual(self.call({"enabled": "yes"})[0], 400)
        self.assertEqual(self.call({"secret": "two\nlines"})[0], 400)

    def test_it_answers_only_on_this_pc(self):
        with mock.patch.object(handler.settings, "is_loopback", lambda address: False):
            self.assertEqual(self.call({"enabled": True})[0], 403)


if __name__ == "__main__":
    unittest.main()
