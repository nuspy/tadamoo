"""The sampling defaults follow the engine: a reload re-reads the launch options and the model file's `sampling` block, and
a bad value refuses the reload before the running engine is touched."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from serve import loader, supervisor
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.service import Service
from serve.tests.test_models import ROOMY, tiny_basalt
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]


class Precedence(unittest.TestCase):
    def test_launch_options_win_over_the_model_file(self):
        a = Namespace(sampling_launch={"temperature": 0.2})
        cfg = {"sampling": {"temperature": 0.9, "top_p": 0.8}}
        self.assertEqual(supervisor.sampling_defaults_of(a, cfg), {"temperature": 0.2, "top_p": 0.8})

    def test_no_sampling_anywhere_is_empty(self):
        self.assertEqual(supervisor.sampling_defaults_of(Namespace(sampling_launch={}), {}), {})

    def test_a_bad_model_value_is_a_value_error_not_a_system_exit(self):
        with self.assertRaises(ValueError) as e, contextlib.redirect_stdout(io.StringIO()):
            supervisor.sampling_defaults_of(Namespace(sampling_launch={}), {"sampling": {"temperature": -1}})
        self.assertIn("sampling.temperature", str(e.exception))
        self.assertNotIn("[server] error", str(e.exception))


class Reload(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.env = mock.patch.dict(os.environ, {"BASALT_CACHE": str(root), "BASALT_DATA": str(root)})
        self.env.start()
        self.model = tiny_basalt(root / "a.basalt", "Model A")
        self.cfg = root / "basalt_config.json"
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, ["</think>\n\nhello"]), tok, ChatTemplate(ROOT / "serve/chat_template.jinja"))
        self.svc.settings_info = {"config": str(self.cfg), "model": str(self.model), "cli": [], "running": {}}
        self.sup = self.svc.supervisor = supervisor.Supervisor(
            self.svc, ["--mock-engine", "--config", str(self.cfg)], mock=True)
        self.sup.mock_delay = 0.0
        self.system = mock.patch.object(loader, "system_now", return_value=dict(ROOMY))
        self.system.start()

    def tearDown(self):
        self.system.stop()
        self.env.stop()
        self.dir.cleanup()

    def write(self, **server):
        self.cfg.write_text(json.dumps({"server": {"model": str(self.model), **server}}), encoding="utf-8")

    def reload(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.sup.reload(wait=True))

    def test_a_reload_applies_the_config_files_launch_options(self):
        self.write(temperature=0.3)
        self.reload()
        self.assertEqual(self.svc.sampling_defaults, {"temperature": 0.3})
        self.write(temperature=0.9, top_p=0.5)
        self.reload()
        self.assertEqual(self.svc.sampling_defaults, {"temperature": 0.9, "top_p": 0.5})

    def test_a_reload_without_launch_options_clears_the_old_ones(self):
        self.write(temperature=0.3)
        self.reload()
        self.write()
        self.reload()
        self.assertEqual(self.svc.sampling_defaults, {})


class RefusedBeforeStopping(unittest.TestCase):
    def test_a_bad_sampling_value_leaves_the_engine_running(self):
        engine = mock.MagicMock()
        svc = mock.MagicMock(engine=engine, vision=None, sampling_defaults={"temperature": 0.7})
        sup = supervisor.Supervisor.__new__(supervisor.Supervisor)
        sup.svc = svc
        bad = {"engine_bin": "basalt.exe", "args": [], "sampling": {"temperature": -1}}
        a = Namespace(sampling_launch={}, sampling_cli=set())
        with mock.patch.object(supervisor, "engine_config", return_value=bad), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                sup._reload_engine(a, [])
        engine.close.assert_not_called()
        engine.restart.assert_not_called()
        self.assertEqual(svc.sampling_defaults, {"temperature": 0.7})


if __name__ == "__main__":
    unittest.main()
