"""A read of the engine's READY line or of the image encoder's pipe that never returns would hold the request turn
for good. Each has a timeout; a process that stays silent is ended and the read raises."""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from serve.engine import process, vision
from serve.engine.errors import EngineStartError
from serve.engine.process import ProcessEngine
from serve.engine.vision import Vision


class Silent:
    """A pipe whose readline blocks until the test lets go (or the encoder is killed)."""

    def __init__(self):
        self.release = threading.Event()
        self.killed = False

    def readline(self):
        self.release.wait(30)
        return ""

    def kill(self):
        self.killed = True
        self.release.set()


class UntimedReads(unittest.TestCase):
    def test_engine_that_never_says_ready_is_ended(self):
        fake = "import time\nprint('INFO engine=0.0.0', flush=True)\ntime.sleep(600)\n"
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "fake_engine.py"
            script.write_text(fake, encoding="utf-8")
            real = process.subprocess.Popen
            procs = []

            def popen(cmd, **kw):
                procs.append(real([sys.executable, str(script), *cmd[1:]], **kw))
                return procs[-1]
            with mock.patch.object(process.subprocess, "Popen", popen), \
                    mock.patch.object(process, "ENGINE_READY_S", 1.0):
                t0 = time.monotonic()
                with self.assertRaises(EngineStartError) as cm:
                    ProcessEngine("basalt", [])
            self.assertLess(time.monotonic() - t0, 30)
            self.assertIn("did not report READY within 1 s", str(cm.exception))
            procs[0].wait(10)                                   # killed, not left running
            self.assertIsNotNone(procs[0].poll())

    def test_a_load_that_reports_progress_is_not_ended(self):
        fake = ("import time\nprint('INFO engine=0.0.0', flush=True)\n"
                "for i in range(6):\n    print('basalt load: ram', i, flush=True)\n    time.sleep(0.4)\n"
                "print('READY 4096 stop', flush=True)\ntime.sleep(30)\n")
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "fake_engine.py"
            script.write_text(fake, encoding="utf-8")
            real = process.subprocess.Popen
            procs = []

            def popen(cmd, **kw):
                procs.append(real([sys.executable, str(script), *cmd[1:]], **kw))
                return procs[-1]
            with mock.patch.object(process.subprocess, "Popen", popen), \
                    mock.patch.object(process, "ENGINE_READY_S", 1.0):
                engine = ProcessEngine("basalt", [])            # 2.4 s of progress lines, each within 1 s of the last
            try:
                self.assertEqual(engine.max_context, 4096)
            finally:
                procs[0].kill()
                procs[0].wait(10)

    def test_a_load_that_writes_to_the_log_is_not_ended(self):
        logfile = os.path.join(tempfile.gettempdir(), f"basalt-ready-test-{os.getpid()}.log")
        fake = ("import sys, time\nprint('INFO engine=0.0.0', flush=True)\n"
                "for i in range(8):\n    sys.stderr.write('basalt load: ram %d\\n' % i)\n    sys.stderr.flush()\n    time.sleep(0.5)\n"
                "print('READY 4096 stop', flush=True)\ntime.sleep(30)\n")
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "fake_engine.py"
            script.write_text(fake, encoding="utf-8")
            real = process.subprocess.Popen
            procs = []

            def popen(cmd, **kw):
                procs.append(real([sys.executable, str(script), *cmd[1:]], **kw))
                return procs[-1]
            with mock.patch.object(process.subprocess, "Popen", popen), \
                    mock.patch.object(process, "ENGINE_READY_S", 1.5):
                engine = ProcessEngine("basalt", [], log=logfile)     # 4 s of log lines, a line every 0.5 s
            try:
                self.assertEqual(engine.max_context, 4096)
            finally:
                procs[0].kill()
                procs[0].wait(10)
                engine.log.close()
        Path(logfile).unlink(missing_ok=True)

    def test_vision_encode_read_times_out(self):
        silent = Silent()
        v = Vision.__new__(Vision)
        v.dir, v.lock, v.cache, v.cache_bytes, v.pinned = Path(tempfile.mkdtemp(prefix="basalt-vision-test-")), threading.Lock(), {}, 0, {}
        v.stopped = False
        v.proc = SimpleNamespace(stdin=io.StringIO(), stdout=silent, kill=silent.kill,
                                 poll=lambda: 1 if silent.killed else None)
        try:
            with mock.patch.object(vision, "VISION_ENCODE_S", 0.3), \
                    mock.patch.object(Vision, "load", return_value=b""), \
                    mock.patch.object(Vision, "normalize", return_value=b"png"):
                with self.assertRaises(ValueError) as cm:
                    v.encode(["x"])
            self.assertIn("said nothing for", str(cm.exception))
            self.assertTrue(silent.killed)                      # ended: the next request starts a fresh one
            self.assertFalse(v.alive())
            self.assertEqual(list(v.dir.glob("*.img")), [])     # the temporary image is removed
        finally:
            shutil.rmtree(v.dir)

    def test_vision_ready_read_times_out(self):
        silent = Silent()
        proc = SimpleNamespace(stdin=io.StringIO(), stdout=silent, kill=silent.kill, poll=lambda: None)
        v = Vision.__new__(Vision)
        v.spawn = (["basalt-vision"], None, None)
        v.dir = Path(tempfile.mkdtemp(prefix="basalt-vision-test-"))
        try:
            with mock.patch.object(vision.subprocess, "Popen", lambda *a, **k: proc), \
                    mock.patch.object(vision, "VISION_READY_S", 0.3):
                with self.assertRaises(RuntimeError) as cm:
                    v._start()
            self.assertIn("did not start", str(cm.exception))
            self.assertTrue(silent.killed)
            self.assertFalse(v.alive())
        finally:
            shutil.rmtree(v.dir)

    def test_a_vision_answer_in_time_is_unchanged(self):
        v = Vision.__new__(Vision)
        v.proc = SimpleNamespace(stdout=SimpleNamespace(readline=lambda: "OK 7 1 1 1\n"))
        self.assertEqual(v._readline(5.0, "x"), "OK 7 1 1 1\n")


if __name__ == "__main__":
    unittest.main()
