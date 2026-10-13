"""An engine that fails before READY on its first start (as with --listen-first, or the app's first Load): the reason
the engine wrote to its log reaches the user, not an AttributeError of the server's own state (slot_cv exists only
after a READY)."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from serve.engine.errors import EngineStartError
from serve.engine.process import ProcessEngine

REAL_POPEN = subprocess.Popen
REASON = "the image encoder: tensor mm.2.weight has an unexpected type or size"
FAKE = f"""import sys
sys.stderr.write("basalt generate: PCIe probe: 36.2 GB/s host->device\\n")
sys.stderr.write("basalt generate: {REASON}\\n")
sys.stderr.flush()
sys.exit(1)
"""


class FirstStartFails(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        script = root / "fake_engine.py"
        script.write_text(FAKE, encoding="utf-8")
        patch = mock.patch.object(subprocess, "Popen",
                                  lambda cmd, **kw: REAL_POPEN([sys.executable, str(script), *cmd[1:]], **kw))
        patch.start()
        self.addCleanup(patch.stop)
        self.log = str(root / "engine.log")
        self.engine = ProcessEngine(str(root / "basalt.exe"), ["--max-context", "4096"], log=self.log, lazy=True)

    def tearDown(self):
        try:
            if self.engine.log and not isinstance(self.engine.log, int):
                self.engine.log.close()
        except (OSError, AttributeError):
            pass

    def test_the_engine_reason_reaches_the_user(self):
        self.assertNotIn("slot_cv", self.engine.__dict__)          # never READY
        with self.assertRaises(EngineStartError) as cm:
            self.engine.restart(tries=1)
        self.assertIn(REASON, str(cm.exception))
        self.assertNotIn("slot_cv", str(cm.exception))
        self.assertFalse(self.engine.starting)


if __name__ == "__main__":
    unittest.main()
