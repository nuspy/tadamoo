"""The length of a reply a model writes well: measured at the first load, kept with the model, raised by use, shown by the Loader."""
import io
import contextlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from serve import ROOT, lengthprobe, loader, supervisor, userdata
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.service import Service
from serve.util.text import ByteTokenizer

JS = (ROOT / "serve" / "web" / "loader.js").read_text(encoding="utf-8")
I18N = (ROOT / "serve" / "web" / "i18n.js").read_text(encoding="utf-8")


def reply(words, finish="stop", repeated=False, tokens=None):
    text = " ".join(f"w{i % 977}x{i}" for i in range(words))
    return {"text": text, "tokens": tokens if tokens is not None else int(words * 1.4), "finish": finish, "repeated": repeated, "seconds": 3.0}


class Ladder(unittest.TestCase):
    def run_ladder(self, answers, stop=None, rungs=lengthprobe.RUNGS):
        asked = []

        def ask(prompt):
            asked.append(prompt)
            return answers[len(asked) - 1]
        return lengthprobe.measure(ask, stop, rungs=rungs), asked

    def test_it_climbs_while_the_model_writes_what_is_asked_and_ends_by_itself(self):
        result, asked = self.run_ladder([reply(2000), reply(5000), reply(10000), reply(20000), reply(60000)])
        self.assertEqual(len(asked), 5)
        self.assertTrue(result["complete"] and result["stopped_by"] == "ladder")
        self.assertIn("60000 words", asked[-1])
        s = lengthprobe.summarize(result["rungs"])
        self.assertEqual((s["reliable_words"], s["natural_words"]), (60000, 60000))

    def test_it_stops_at_the_first_try_much_shorter_than_asked_and_keeps_what_the_model_wrote(self):
        result, asked = self.run_ladder([reply(2000), reply(5000), reply(2600)])
        self.assertEqual((len(asked), result["stopped_by"]), (3, "short"))
        s = lengthprobe.summarize(result["rungs"])
        self.assertEqual(s["natural_words"], 5000)

    def test_a_loop_is_not_counted_and_ends_the_ladder(self):
        result, _ = self.run_ladder([reply(2000), {"text": "the same line again and again. " * 3000, "tokens": 30000, "finish": "stop", "repeated": False, "seconds": 9}])
        self.assertEqual(result["stopped_by"], "loop")
        self.assertFalse(result["rungs"][1]["clean"])
        self.assertEqual(lengthprobe.summarize(result["rungs"])["reliable_words"], 2000)

    def test_a_repeat_flagged_by_the_server_is_not_counted_either(self):
        result, _ = self.run_ladder([reply(2000, repeated=True)])
        self.assertEqual((result["stopped_by"], result["rungs"][0]["clean"]), ("loop", False))

    def test_a_cut_by_the_context_counts_as_clean_and_ends_the_ladder(self):
        result, asked = self.run_ladder([reply(2000), reply(4000, finish="length")])
        self.assertEqual((len(asked), result["stopped_by"]), (2, "cap"))
        self.assertEqual(lengthprobe.summarize(result["rungs"])["reliable_words"], 4000)

    def test_stop_ends_it_and_a_cancelled_try_is_not_a_result(self):
        stop = threading.Event()
        stop.set()
        result, asked = self.run_ladder([], stop)
        self.assertEqual((asked, result["stopped_by"], result["rungs"]), ([], "stopped", []))
        result, _ = self.run_ladder([reply(2000), {"text": "x", "tokens": 1, "finish": "cancel", "repeated": False, "seconds": 1}])
        self.assertEqual((len(result["rungs"]), result["stopped_by"]), (1, "stopped"))


class Record(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(userdata, "path", lambda name: Path(self.tmp.name) / name)
        p.start()
        self.addCleanup(p.stop)
        self.facts = {"name": "Qwen3.8-Flash-Next", "path": str(Path(self.tmp.name) / "m.basalt"), "meta_hash": "abc123"}
        Path(self.facts["path"]).write_bytes(b"x" * 10)

    def test_the_record_names_the_model_in_full_and_survives_a_read(self):
        model = lengthprobe.identity(self.facts, quant="IQ3_XXS")
        self.assertEqual((model["name"], model["quant"], model["size_bytes"], model["meta_hash"]), ("Qwen3.8-Flash-Next", "IQ3_XXS", 10, "abc123"))
        self.assertTrue(Path(model["path"]).is_absolute() and model["path"].endswith("m.basalt") and model["modified"])
        result = {"rungs": [lengthprobe.judge(2000, reply(1900)["text"], 2700, "stop", False, 4)], "complete": True, "stopped_by": "ladder"}
        record = lengthprobe.new_record(model, 65536, "0.2.0", result)
        lengthprobe.write("abc123", record)
        back = lengthprobe.read("abc123")
        self.assertEqual((back["model"]["path"], back["context"], back["reliable_tokens"], back["engine"]), (model["path"], 65536, 2700, "0.2.0"))
        self.assertIsNone(lengthprobe.read("nothing"))

    def test_the_limit_rises_only_with_a_reply_that_ended_well(self):
        lengths = lengthprobe.Lengths(lambda: self.facts)
        self.assertIsNone(lengths.attach())
        self.assertEqual(lengths.limit_tokens(), 0)
        self.assertFalse(lengths.note({"finish": "stop", "completion_tokens": 9000}))          # no record yet: nothing to raise
        model = lengthprobe.identity(self.facts)
        lengths.set(lengthprobe.new_record(model, 1, None, {"rungs": [lengthprobe.judge(2000, reply(2000)["text"], 3000, "stop", False, 1)], "complete": True, "stopped_by": "ladder"}))
        self.assertEqual(lengths.limit_tokens(), 3000)
        for done in ({"finish": "length", "completion_tokens": 9000}, {"finish": "stop", "completion_tokens": 9000, "repeated": True},
                     {"finish": "stop", "completion_tokens": 9000, "answer_capped": True}, {"finish": "stop", "completion_tokens": 2500}):
            self.assertFalse(lengths.note(done), done)
        self.assertEqual(lengths.limit_tokens(), 3000)
        self.assertTrue(lengths.note({"finish": "stop", "completion_tokens": 8200}))              # thinking and answer together
        self.assertEqual(lengths.limit_tokens(), 8200)
        lengths.flush()
        self.assertEqual(lengthprobe.read("abc123")["observed_max_tokens"], 8200)
        fresh = lengthprobe.Lengths(lambda: self.facts)                                           # the next load reads it
        fresh.attach()
        self.assertEqual(fresh.limit_tokens(), 8200)


class FirstLoad(unittest.TestCase):
    """The supervisor measures once, keeps the result, and does not repeat it at the next load."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(userdata, "path", lambda name: Path(self.tmp.name) / name)
        p.start()
        self.addCleanup(p.stop)
        tok = ByteTokenizer()
        self.engine = MockEngine(tok, " ".join(f"word{i}" for i in range(300)), max_context=32768)
        self.svc = Service(self.engine, tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        facts = {**loader.DEMO_FACTS, "name": "Mock", "path": str(Path(self.tmp.name) / "m.basalt"), "meta_hash": "feed01"}
        Path(facts["path"]).write_bytes(b"y" * 20)
        self.sup = self.svc.supervisor = supervisor.Supervisor(self.svc, [], mock=False, facts=facts)
        self.sup.auto_calibrate = False
        self.svc.lengths = lengthprobe.Lengths(lambda: self.sup.facts)

    def run_it(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.sup.after_load()
            if self.sup.thread is not None:
                self.sup.thread.join(60)

    def test_a_new_model_is_measured_once_and_the_record_is_kept(self):
        self.run_it()
        self.assertEqual(self.sup.status()["phase"], "ready", self.sup.status())
        record = lengthprobe.read("feed01")
        self.assertIsNotNone(record)
        self.assertEqual(record["rungs"][0]["asked_words"], 2000)
        self.assertEqual(record["stopped_by"], "short")                      # the mock writes 300 words of the 2000 asked
        self.assertEqual(record["model"]["name"], "Mock")
        self.assertGreater(self.svc.lengths.limit_tokens(), 0)
        first = record["measured_at"]
        self.sup.thread = None
        self.run_it()                                                        # a second load finds the record and asks nothing
        self.assertEqual(lengthprobe.read("feed01")["measured_at"], first)
        self.assertEqual(len(self.engine.requests), 1)

    def test_the_setting_turns_the_measurement_off(self):
        self.svc.measure_length = False
        self.run_it()
        self.assertIsNone(lengthprobe.read("feed01"))
        self.assertEqual(self.engine.requests, [])

    def test_the_button_measures_again_and_stop_ends_a_running_one(self):
        self.run_it()
        self.assertFalse(self.sup.stop_length())                             # nothing runs
        self.assertTrue(self.sup.measure_length(wait=True))
        self.assertEqual(len(self.engine.requests), 2)

    def test_the_status_and_the_page_carry_the_numbers(self):
        self.run_it()
        self.assertTrue(self.svc.v1_status()["length"]["known"])
        self.assertGreater(self.svc.v1_status()["length"]["limit_tokens"], 0)
        self.assertEqual(Service(self.engine, ByteTokenizer(), ChatTemplate(ROOT / "serve" / "chat_template.jinja")).v1_status()["length"], {"known": False})
        for key in ("ln.title", "ln.limit", "ln.measure", "ln.stop", "ln.unknown", "ln.help", "ld.step.length", "f.server.measure-length", "h.server.measure-length"):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        self.assertIn('data-act="length"', JS)
        self.assertIn("api/loader/length", JS)


if __name__ == "__main__":
    unittest.main()
