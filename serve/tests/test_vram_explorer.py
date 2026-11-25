"""The VRAM Explorer: the long VRAM history in the telemetry, the on-demand reader (serve/vram_explorer.py), the imported gputaskman
package and the two routes the Monitor reads. A fake clock and fake collector; the server runs on the mock engine."""
import ast
import importlib
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from serve import vram_explorer
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.service import Service
from serve.util import telemetry
from serve.util.telemetry import LONG_SHOWN, LONG_STEP_S, Telemetry
from serve.util.text import ByteTokenizer
from serve.vram_explorer import IDLE_CLOSE_S, MIN_INTERVAL_S, VramExplorer

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "serve" / "chat_template.jinja"


class Card:
    def __init__(self, index, ok=True):
        self.index, self._ok = index, ok

    def ok(self):
        return self._ok

    def name(self):
        return f"Card {self.index}"

    def read(self):
        return {}


class LongHistory(unittest.TestCase):
    def telemetry(self, indices=(0,)):
        before = set(threading.enumerate())
        tel = Telemetry(gpu_indices=list(indices), gpu_factory=Card)
        for thread in set(threading.enumerate()) - before:
            tel.stop()
            thread.join(5)
        tel.vram_long.clear()
        tel._long_peak.clear()
        tel._long_since = 0.0
        return tel

    def feed(self, tel, at, used, index=0, total=1000):
        with mock.patch.object(telemetry.time, "time", return_value=at):
            tel.record({"gpus": [{"index": index, "mem_used": used, "mem_total": total}]})

    def test_a_bucket_keeps_the_peak_of_its_readings(self):
        tel = self.telemetry()
        t0 = 100_000.0
        self.feed(tel, t0, 100)
        self.feed(tel, t0 + 3, 400)            # inside the window of the next point: its peak is the point's value
        self.feed(tel, t0 + 12, 200)
        with mock.patch.object(telemetry.time, "time", return_value=t0 + 12):
            out = tel.vram_history(300)
        (card,) = out["gpus"]
        self.assertEqual((card["index"], card["name"], card["total"]), (0, "Card 0", 1000))
        self.assertEqual((out["range_s"], out["step_s"], len(card["used"])), (300, LONG_STEP_S, 30))
        self.assertEqual(card["used"][28:], [100, 400])
        self.assertEqual(sum(v is None for v in card["used"]), 28)         # nothing was read before

    def test_longer_ranges_are_fewer_wider_buckets(self):
        tel = self.telemetry()
        self.feed(tel, 100_000.0, 50)
        with mock.patch.object(telemetry.time, "time", return_value=100_000.0):
            out = tel.vram_history(86_400)
        self.assertEqual(len(out["gpus"][0]["used"]), LONG_SHOWN)
        self.assertEqual(out["step_s"], 360)
        self.assertEqual(out["gpus"][0]["used"][-1], 50)

    def test_the_range_is_kept_inside_what_is_stored(self):
        tel = self.telemetry()
        self.feed(tel, 100_000.0, 50)
        self.assertEqual(tel.vram_history(1)["range_s"], LONG_STEP_S)
        self.assertEqual(tel.vram_history(10 ** 9)["range_s"], 86_400)

    def test_each_card_has_its_own_series(self):
        tel = self.telemetry((0, 1))
        with mock.patch.object(telemetry.time, "time", return_value=100_000.0):
            tel.record({"gpus": [{"index": 0, "mem_used": 10, "mem_total": 100}, {"index": 1, "mem_used": 20, "mem_total": 200}]})
            out = tel.vram_history(300)
        self.assertEqual([(g["index"], g["total"], g["used"][-1]) for g in out["gpus"]], [(0, 100, 10), (1, 200, 20)])

    def test_a_card_that_gives_no_number_leaves_no_points(self):
        tel = self.telemetry()
        with mock.patch.object(telemetry.time, "time", return_value=100_000.0):
            tel.record({"gpus": [{"index": 0, "mem_used": None, "mem_total": None}]})
            self.assertEqual(tel.vram_history(300)["gpus"], [])


class FakeCollector:
    made = []

    def __init__(self, index, host=True):
        self.index, self.host, self.samples, self.closed = index, host, 0, False
        FakeCollector.made.append(self)

    def sample(self):
        self.samples += 1
        return self.samples

    def close(self):
        self.closed = True


class FakeAdapter:
    def __init__(self, name, vendor="NVIDIA"):
        self.name, self.vendor = name, vendor


def fake_pieces():
    collector = mock.Mock(Collector=FakeCollector, enumerate_adapters=lambda: [FakeAdapter("B"), FakeAdapter("A")],
                          ordered_adapters=lambda found: sorted(found, key=lambda a: a.name))
    report = mock.Mock(to_json_dict=lambda snapshot, system, error: {"vram_used": snapshot})
    return collector, report


class Reader(unittest.TestCase):
    def setUp(self):
        FakeCollector.made = []
        self.t = 1000.0
        sleep = mock.patch.object(vram_explorer.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        self.reader = VramExplorer(loader=fake_pieces, clock=lambda: self.t)

    def test_the_first_reading_starts_the_rates_and_takes_a_second_one(self):
        out = self.reader.snapshot(0)
        (c,) = FakeCollector.made
        self.assertEqual((c.samples, c.host), (2, False))
        self.assertEqual(out["vram_used"], 2)
        self.assertEqual([g["name"] for g in out["gpus"]], ["A", "B"])
        self.assertEqual(out["index"], 0)

    def test_readings_close_together_share_one_sample(self):
        first = self.reader.snapshot(0)
        self.t += MIN_INTERVAL_S / 2
        self.assertIs(self.reader.snapshot(0), first)
        self.assertEqual(FakeCollector.made[0].samples, 2)
        self.t += MIN_INTERVAL_S
        self.assertEqual(self.reader.snapshot(0)["vram_used"], 3)

    def test_an_idle_collector_is_closed_and_a_new_one_made_when_asked_again(self):
        self.reader.snapshot(0)
        self.t += IDLE_CLOSE_S + 1
        self.reader.snapshot(1)
        self.assertTrue(FakeCollector.made[0].closed)
        self.assertEqual([c.index for c in FakeCollector.made], [0, 1])

    def test_close_releases_every_collector(self):
        self.reader.snapshot(0)
        self.reader.snapshot(1)
        self.reader.close()
        self.assertTrue(all(c.closed for c in FakeCollector.made))

    def test_a_reader_that_cannot_start_answers_with_the_reason(self):
        def broken():
            raise ImportError("no counters")
        out = VramExplorer(loader=broken).snapshot(0)
        self.assertEqual(out["gpus"], [])
        self.assertIn("no counters", out["error"])

    def test_an_adapter_that_does_not_exist_is_an_error_not_a_crash(self):
        class Missing(FakeCollector):
            def __init__(self, index, host=True):
                raise ValueError(f"no GPU {index}")
        collector, report = fake_pieces()
        collector.Collector = Missing
        out = VramExplorer(loader=lambda: (collector, report)).snapshot(5)
        self.assertIn("no GPU 5", out["error"])


class Imported(unittest.TestCase):
    def test_the_collector_and_the_report_load_here(self):
        for name in ("collector", "report"):
            importlib.import_module(f"serve.gputaskman.{name}")

    def test_no_terminal_interface_came_with_it(self):
        found = set()
        for path in (ROOT / "serve" / "gputaskman").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    found |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    found.add(node.module.split(".")[0])
        self.assertNotIn("textual", found)

    def test_the_licence_travels_with_it(self):
        text = (ROOT / "serve" / "gputaskman" / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT License", text)
        self.assertIn("Scott Hanselman", text)
        self.assertIn("gputaskman", (ROOT / "THIRD_PARTY_LICENSES").read_text(encoding="utf-8"))


class Routes(unittest.TestCase):
    def setUp(self):
        tok = ByteTokenizer()
        self.svc = Service(MockEngine(tok, "ok"), tok, ChatTemplate.from_file(TEMPLATE))
        self.httpd = serve(self.svc, port=0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.asked = []

        class Fake:
            def snapshot(me, index):
                self.asked.append(index)
                return {"gpu": "Fake", "index": index, "gpus": [{"index": 0, "name": "Fake"}]}

            def close(me):
                self.closed = True
        self.closed = False
        self.svc._vram_explorer = Fake()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_the_history_route_answers_with_the_range_asked(self):
        code, out = self.get("/api/vram/history?range=3600")
        self.assertEqual(code, 200)
        self.assertEqual(out["range_s"], 3600)
        self.assertIn("gpus", out)

    def test_a_bad_range_falls_back_to_five_minutes(self):
        code, out = self.get("/api/vram/history?range=abc")
        self.assertEqual((code, out["range_s"]), (200, 300))

    def test_the_explorer_route_reads_the_adapter_asked(self):
        code, out = self.get("/api/vram/explorer?gpu=1")
        self.assertEqual((code, out["index"]), (200, 1))
        self.assertEqual(self.get("/api/vram/explorer")[1]["index"], 0)
        self.assertEqual(self.asked, [1, 0])

    def test_closing_the_server_closes_the_explorer(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.assertTrue(self.closed)
        self.httpd = serve(self.svc, port=0)      # tearDown closes this one


if __name__ == "__main__":
    unittest.main()
