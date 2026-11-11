"""The client of the VRAM broker: a lease with a stand-in transport, the fallback to the CPU, the keepalive, and the server's real routes."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tadamoo.vram_lease import VramLease, http_transport  # noqa: E402


class Fake:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if path.endswith("/touch") or path.endswith("/release"):
            return 200, {"ok": True}
        return self.answer


GRANT = (200, {"ok": True, "lease": {"id": "0123456789abcdef", "granted_mib": 3000, "hold_s": 60}})


class ClientTests(unittest.TestCase):
    def test_a_granted_lease_is_released_at_the_end_of_the_block(self):
        t = Fake(GRANT)
        with VramLease("docling", 3000, "pdf", transport=t, keepalive=False) as lease:
            self.assertTrue(lease.gpu_ok)
            self.assertEqual((lease.granted_mib, lease.reason), (3000, ""))
        self.assertEqual([c[:2] for c in t.calls], [("POST", "/api/vram/lease"), ("POST", "/api/vram/lease/0123456789abcdef/release")])
        self.assertEqual(t.calls[0][2], {"owner": "docling", "mib": 3000, "purpose": "pdf", "wait_s": 0})
        self.assertFalse(lease.gpu_ok)

    def test_a_refusal_means_the_cpu_and_says_why(self):
        for reason in ("unavailable", "no_engine", "busy", "not_enough"):
            t = Fake((409, {"ok": False, "reason": reason, "message": "m", "message_it": "mi"}))
            with VramLease("a", 10, transport=t) as lease:
                self.assertEqual((lease.gpu_ok, lease.granted_mib, lease.reason, lease.message_it), (False, 0, reason, "mi"))
            self.assertEqual(len(t.calls), 1)                                  # nothing to release

    def test_no_server_is_the_cpu_not_an_error(self):
        def down(method, path, body):
            raise ConnectionRefusedError("no server")
        with VramLease("a", 10, transport=down) as lease:
            self.assertEqual((lease.gpu_ok, lease.reason), (False, "no_engine"))
        with VramLease("a", 10, transport=http_transport("http://127.0.0.1:1", timeout=2)) as lease:      # nothing listens there
            self.assertFalse(lease.gpu_ok)

    def test_the_hold_is_kept_while_the_block_runs(self):
        t = Fake((200, {"ok": True, "lease": {"id": "0123456789abcdef", "granted_mib": 5, "hold_s": 3}}))
        with VramLease("a", 5, hold_s=3, transport=t) as lease:
            time.sleep(2.3)
        self.assertGreaterEqual(sum(1 for c in t.calls if c[1].endswith("/touch")), 1)
        self.assertFalse(lease.gpu_ok)

    def test_a_lease_the_server_forgot_is_lost(self):
        def call(method, path, body):
            return (404, {"ok": False, "reason": "not_found"}) if path.endswith("/touch") else GRANT
        lease = VramLease("a", 10, transport=call, keepalive=False).acquire()
        self.assertTrue(lease.gpu_ok)
        self.assertFalse(lease.touch())
        self.assertTrue(lease.lost and not lease.gpu_ok)


class ServerTests(unittest.TestCase):
    """The same client against the server's routes."""

    def test_against_a_real_server(self):
        from serve.engine.mock import MockEngine
        from serve.frontend import ChatTemplate
        from serve.handler import serve
        from serve.service import Service
        from serve.tests.test_vram_broker import FakeService
        from serve.util.text import ByteTokenizer
        from serve.vram_broker import BrokerPrefs, VramBroker
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"TADAMOO_DATA": d + "/data", "TADAMOO_CACHE": d + "/cache"}):
            tok = ByteTokenizer()
            svc = Service(MockEngine(tok, "ok"), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
            engine = FakeService()
            svc.vram_broker = VramBroker(engine, probe=lambda: engine.free, start_reserve=lambda: 1500, prefs=BrokerPrefs(Path(d) / "b.json"), timer=False)
            httpd = serve(svc, port=0)
            try:
                url = f"http://127.0.0.1:{httpd.server_address[1]}"
                with VramLease("avatar", 3000, "test", base_url=url, keepalive=False) as lease:
                    self.assertTrue(lease.gpu_ok)
                    with VramLease("other", 100, base_url=url, keepalive=False) as second:
                        self.assertEqual((second.gpu_ok, second.reason), (False, "busy"))
                self.assertEqual(engine.calls, [4500, None])
            finally:
                httpd.shutdown()
                httpd.server_close()


if __name__ == "__main__":
    unittest.main()
