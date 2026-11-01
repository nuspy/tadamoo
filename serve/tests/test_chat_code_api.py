"""POST /api/chat/code-abstract against the mock engine: no technical session, so the description is the outline, and the original is saved."""
import os
import tempfile
from pathlib import Path
from unittest import mock

from harness import side_uses
from serve.tests import test_api_plans as base
from serve.tests.test_chatcode import MESSAGE


class ChatCodeApiTest(base.PlansApiBase):
    @classmethod
    def setUpClass(cls):
        cls.data = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {"BASALT_DATA": cls.data.name})
        cls.env.start()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.env.stop()
        cls.data.cleanup()

    def setUp(self):
        side_uses.reset()

    def test_a_message_with_code_comes_back_with_a_description_and_its_original_saved(self):
        code, out = self.call("POST", "/api/chat/code-abstract", {"text": MESSAGE})
        self.assertEqual(code, 200)
        self.assertEqual(out["by"], ["outline"])
        self.assertNotIn("def load", out["text"])
        self.assertIn("def load", (Path(self.data.name) / out["saved"][0]).read_text(encoding="utf-8"))

    def test_a_message_without_code_and_a_bad_request(self):
        self.assertEqual(self.call("POST", "/api/chat/code-abstract", {"text": "Just a question about the weather."}), (200, {"text": ""}))
        self.assertEqual(self.call("POST", "/api/chat/code-abstract", {"text": ""})[0], 400)
        self.assertEqual(self.call("POST", "/api/chat/code-abstract", {})[0], 400)
