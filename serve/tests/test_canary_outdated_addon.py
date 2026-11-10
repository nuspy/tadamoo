"""An add-on folder without the page's modules (an old staged copy) is reported as an outdated add-on, not as an import traceback."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve import canary_api


class OutdatedAddon(unittest.TestCase):
    def test_a_missing_module_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "canary").mkdir()
            (root / "canary" / "__init__.py").write_text("", encoding="utf-8")
            with mock.patch("serve.addons.canary_path", return_value=root):
                with self.assertRaises(canary_api.CanaryError) as caught:
                    canary_api.lib("gui.server")
            self.assertEqual(caught.exception.code, "addon_outdated")
            self.assertIn(str(root), str(caught.exception))
            status, body = canary_api.error_view(caught.exception)
            self.assertEqual(status, 400)
            self.assertEqual(body["error"]["code"], "addon_outdated")


if __name__ == "__main__":
    unittest.main()
