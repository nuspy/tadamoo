"""Voice in the Chat: the pure parts of serve/web/chatvoice.js in node (what is said of an answer, the parts, the WAV), and the whole flow in a real headless Chrome or Edge
with a fake microphone on a scripted mock-engine server, at three screen sizes: Talk, the words sent, the answer said, errors, and the hands-free conversation. Skipped where there
is no browser or no node."""
import json
import math
import os
import random
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
import urllib.request
import wave
from pathlib import Path

from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util.text import ByteTokenizer

HERE = Path(__file__).resolve().parent


class _NoTelemetry:
    def snapshot(self):
        return {"now": {"gpus": []}, "history": {}, "static": {"gpus": []}}


def fake_microphone(path: Path) -> None:
    """0.8 s of noisy 'speech' and 1.2 s of silence, at 16 kHz, played over and over by the fake microphone (which keeps its own clock): a recording that starts at any
    moment hears speech within two seconds, and the page ends it by itself on the silence that follows."""
    rng = random.Random(7)
    frames = bytearray()
    for i in range(12800):
        v = 0.6 * math.sin(2 * math.pi * 180 * i / 16000) * (0.6 + 0.4 * math.sin(2 * math.pi * 3 * i / 16000)) + 0.1 * (rng.random() - 0.5)
        frames += struct.pack("<h", int(max(-1, min(1, v)) * 32767))
    frames += b"\x00\x00" * 16000 * 5
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(bytes(frames))


class VoiceOfTheChatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = None
        cls.tmp = ""
        candidates = (os.environ.get("TADAMOO_TEST_BROWSER", ""), r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                      shutil.which("google-chrome") or "", shutil.which("chromium") or "")
        cls.browser = next((p for p in candidates if p and os.path.exists(p)), None)
        cls.node = shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("no node")

    def test_the_pure_parts(self):
        out = subprocess.run([self.node, str(HERE / "chatvoice_check.js")], capture_output=True, text=True, timeout=120)
        results = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual([r["name"] + ": " + r.get("error", "") for r in results if not r["ok"]], [])
        self.assertEqual(len(results), 9)

    def test_the_flow_in_a_browser_with_a_fake_microphone(self):
        if not self.browser:
            self.skipTest("no headless browser")
        tmp = tempfile.mkdtemp(prefix="cvc-data-")
        httpd = None
        try:
            os.environ["TADAMOO_DATA"] = tmp + "/data"
            os.environ["TADAMOO_CACHE"] = tmp + "/cache"
            wav = Path(tmp) / "mic.wav"
            fake_microphone(wav)
            tok = ByteTokenizer()
            answers = ["</think>\n\nThe sea is wide and quiet.", "</think>\n\nThe sky is high and `blue`.\n\n```\nx = 1\n```", "</think>\n\nHello to you."]
            svc = Service(MockEngine(tok, answers * 40, max_context=1000000), tok, ChatTemplate.from_file(HERE.parents[1] / "serve" / "chat_template.jinja"))
            svc.telemetry = _NoTelemetry()
            httpd = serve(svc, port=0)
            url = f"http://127.0.0.1:{httpd.server_address[1]}/"
            for _ in range(80):
                try:
                    urllib.request.urlopen(url, timeout=1).read(1)
                    break
                except OSError:
                    time.sleep(0.25)
            self.maxDiff = None
            out = subprocess.run([self.node, str(HERE / "chatvoice_dom_check.js"), url, self.browser, str(wav)], capture_output=True, text=True, timeout=900)
            results = json.loads(out.stdout.strip().splitlines()[-1])
            self.assertEqual([r["name"] + ": " + r["error"] for r in results if not r["ok"]], [])
            self.assertEqual(len(results), 18, results)
        finally:
            if httpd:
                httpd.shutdown()
                httpd.server_close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
