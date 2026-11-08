"""The length of a reply a model writes: measured at the first load from the tokens the server counts, kept with the model, raised by use,
shown by the Loader."""
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


def text(n_words):
    return " ".join(f"w{i % 977}x{i}" for i in range(n_words))


def got(tokens, finish="length", body=None, repeated=False, ended_by="", cap=30_000):
    """What `ask` gives back: the text, and the tokens the server counted (not the model's idea of them)."""
    return {"text": body if body is not None else text(int(tokens / 1.4)), "tokens": tokens, "finish": finish, "repeated": repeated,
            "seconds": 5.0, "ended_by": ended_by, "cap": cap}


LOOP = "and then the river turned back to the same bend, and the same field, and the same old mill. " * 400


class Tries(unittest.TestCase):
    def run_tries(self, answers, stop=None):
        asked = []

        def ask(messages, cap):
            asked.append((messages, cap))
            return answers[len(asked) - 1]
        return lengthprobe.measure(ask, stop), asked

    def test_nothing_is_asked_in_words_or_tokens_the_model_would_have_to_count(self):
        for name, prompt in lengthprobe.PROMPTS:
            self.assertNotRegex(prompt, r"\d[\d,.]*\s*(words|tokens|parole|token)")
            self.assertIn("Never conclude", prompt)
        self.assertNotRegex(lengthprobe.CONTINUE, r"\d")

    def test_a_text_that_reaches_the_cap_clean_is_the_answer_and_the_second_text_is_not_asked(self):
        result, asked = self.run_tries([got(30_000)])
        self.assertEqual((len(asked), result["stopped_by"], asked[0][1]), (1, "cap", 30_000))
        r = result["rungs"][0]
        self.assertTrue(r["reached_cap"] and r["clean"] and r["clean_tokens"] == 30_000)
        self.assertEqual([c["clean"] for c in r["checkpoints"]], [True, True, True])
        s = lengthprobe.summarize(result["rungs"])
        self.assertEqual((s["reliable_tokens"], s["bounded"]), (30_000, True))

    def test_a_model_that_closes_its_reply_is_asked_to_go_on_with_its_own_text_before_it(self):
        result, asked = self.run_tries([got(1_300, finish="stop"), got(9_000, finish="stop"), got(19_700, finish="length")])
        self.assertEqual(len(asked), 3)
        first, second, third = (a[0] for a in asked)
        self.assertEqual([m["role"] for m in first], ["user"])
        self.assertEqual([m["role"] for m in second], ["user", "assistant", "user"])
        self.assertEqual(second[-1]["content"], lengthprobe.CONTINUE)
        self.assertEqual(second[1]["content"], text(int(1_300 / 1.4)))
        self.assertEqual(len(third), 5)
        self.assertEqual([a[1] for a in asked], [30_000, 28_700, 19_700])                  # what is left of the cap
        r = result["rungs"][0]
        self.assertEqual((r["tokens"], r["pieces"], r["stops"], r["first_stop_tokens"], r["reached_cap"], r["clean"]), (30_000, 3, [1_300, 10_300], 1_300, True, True))
        self.assertEqual(result["stopped_by"], "cap")
        s = lengthprobe.summarize(result["rungs"])
        self.assertEqual((s["reliable_tokens"], s["first_stop_tokens"], s["bounded"]), (30_000, 1_300, True))

    def test_a_model_that_will_not_go_on_has_shown_where_it_stops_not_where_it_degrades(self):
        result, asked = self.run_tries([got(1_327, finish="stop"), got(60, finish="stop"), got(1_268, finish="stop"), got(40, finish="stop")])
        self.assertEqual((len(asked), result["stopped_by"]), (4, "ladder"))                # both texts were asked to go on
        s = lengthprobe.summarize(result["rungs"])
        self.assertEqual((s["first_stop_tokens"], s["bounded"], s["reached_cap"]), (1_327, False, False))
        self.assertTrue(all(r["natural"] for r in result["rungs"]))

    def test_the_tokens_are_the_servers_not_the_texts(self):
        result, _ = self.run_tries([got(12_345, finish="stop", body="a short text"), got(100, finish="stop"), got(100, finish="stop")])
        self.assertEqual(result["rungs"][0]["tokens"], 12_445)

    def test_a_loop_says_where_it_began_and_what_came_before_counts(self):
        body = text(7_000) + " " + LOOP
        result, _ = self.run_tries([got(14_000, ended_by="loop", body=body), got(3_000, finish="stop"), got(100, finish="stop")])
        r = result["rungs"][0]
        self.assertFalse(r["clean"])
        self.assertTrue(r["looped"])
        self.assertLess(r["clean_tokens"], 14_000 * 0.8)
        self.assertGreater(r["clean_tokens"], 14_000 * 0.2)
        self.assertEqual([c["clean"] for c in r["checkpoints"]], [r["clean_tokens"] >= 10_000, False, False])
        self.assertEqual([c["reached"] for c in r["checkpoints"]], [True, False, False])
        s = lengthprobe.summarize(result["rungs"])
        self.assertEqual((s["reliable_tokens"], s["bounded"]), (r["clean_tokens"], True))

    def test_a_loop_in_the_piece_that_went_on_is_a_loop_of_the_whole_text(self):
        result, asked = self.run_tries([got(8_000, finish="stop"), got(4_000, ended_by="loop", body=LOOP), got(2_000, finish="stop"), got(50, finish="stop")])
        r = result["rungs"][0]
        self.assertEqual((r["tokens"], r["looped"], len(asked[1][0])), (12_000, True, 3))
        self.assertGreater(r["clean_tokens"], 0)

    def test_a_loop_the_server_cut_without_the_text_check_seeing_it_still_counts_as_one(self):
        result, _ = self.run_tries([got(9_000, repeated=True), got(2_000, finish="stop"), got(50, finish="stop")])
        self.assertEqual((result["rungs"][0]["looped"], result["rungs"][0]["clean_tokens"]), (True, 7_000))

    def test_a_cap_the_context_set_lower_is_the_cap_that_counts(self):
        result, _ = self.run_tries([got(18_000, cap=18_100)])
        self.assertTrue(result["rungs"][0]["reached_cap"])
        self.assertEqual([c["at"] for c in result["rungs"][0]["checkpoints"]], [10_000])
        self.assertEqual(result["stopped_by"], "cap")

    def test_stop_ends_it_and_a_stopped_try_is_not_a_result(self):
        stop = threading.Event()
        stop.set()
        result, asked = self.run_tries([], stop)
        self.assertEqual((asked, result["stopped_by"], result["rungs"], result["complete"]), ([], "stopped", [], False))
        result, _ = self.run_tries([got(500, finish="cancel", ended_by="stopped")])
        self.assertEqual((result["rungs"], result["stopped_by"]), ([], "stopped"))

    def test_a_model_that_always_goes_on_is_asked_a_limited_number_of_times(self):
        asked = []

        def ask(messages, cap):
            asked.append(cap)
            return got(200, finish="stop")
        r = lengthprobe.run_text("x", ask, 30_000)
        self.assertEqual((len(asked), r["finish"]), (lengthprobe.MAX_PIECES, "length"))


class Record(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(userdata, "path", lambda name: Path(self.tmp.name) / name)
        p.start()
        self.addCleanup(p.stop)
        self.facts = {"name": "Qwen3.8-Flash-Next", "path": str(Path(self.tmp.name) / "m.basalt"), "meta_hash": "abc123"}
        Path(self.facts["path"]).write_bytes(b"x" * 10)

    def record(self, tokens=30_000):
        model = lengthprobe.identity(self.facts, quant="IQ3_XXS")
        r = lengthprobe.judge("entries", 30_000, text(int(tokens / 1.4)), tokens, "length", False, 4.0)
        r["reached_cap"] = True                                  # the record is bounded: its number is a limit
        return lengthprobe.new_record(model, 65536, "0.2.0", {"rungs": [r], "complete": True, "stopped_by": "cap"}, {"relaxed_drafting": 0.2})

    def test_the_record_names_the_model_in_full_and_survives_a_read(self):
        model = lengthprobe.identity(self.facts, quant="IQ3_XXS")
        self.assertEqual((model["name"], model["quant"], model["size_bytes"], model["meta_hash"]), ("Qwen3.8-Flash-Next", "IQ3_XXS", 10, "abc123"))
        self.assertTrue(Path(model["path"]).is_absolute() and model["path"].endswith("m.basalt") and model["modified"])
        lengthprobe.write("abc123", self.record(12_000))
        back = lengthprobe.read("abc123")
        self.assertEqual((back["model"]["path"], back["context"], back["reliable_tokens"], back["engine"], back["version"]),
                         (model["path"], 65536, 12_000, "0.2.0", lengthprobe.VERSION))
        self.assertIsNone(lengthprobe.read("nothing"))

    def test_a_record_of_an_old_method_is_not_used(self):
        for old in (1, 2):
            lengthprobe.write("abc123", {**self.record(), "version": old})
            self.assertIsNone(lengthprobe.read("abc123"))

    def test_the_record_keeps_the_settings_it_was_measured_with(self):
        lengthprobe.write("abc123", self.record(12_000))
        self.assertEqual(lengthprobe.read("abc123")["settings"], {"relaxed_drafting": 0.2})

    def test_a_number_nothing_bounded_is_not_a_limit_until_a_reply_shows_one(self):
        lengths = lengthprobe.Lengths(lambda: self.facts)
        lengths.set({**self.record(1_327), "reliable_tokens": 1_327, "bounded": False, "observed_max_tokens": 1_327})
        self.assertEqual(lengths.limit_tokens(), 1_327)                 # observed in use
        lengths.set({**self.record(1_327), "reliable_tokens": 1_327, "bounded": False, "observed_max_tokens": 0})
        self.assertEqual((lengths.limit_tokens(), lengths.summary()["bounded"]), (0, False))
        self.assertTrue(lengths.note({"finish": "stop", "completion_tokens": 5_000}))
        self.assertEqual(lengths.limit_tokens(), 5_000)

    def test_the_limit_rises_only_with_a_reply_that_ended_well(self):
        lengths = lengthprobe.Lengths(lambda: self.facts)
        self.assertIsNone(lengths.attach())
        self.assertEqual(lengths.limit_tokens(), 0)
        self.assertFalse(lengths.note({"finish": "stop", "completion_tokens": 9000}))          # no record yet: nothing to raise
        lengths.set(self.record(3_000))
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
        self.assertEqual([r["prompt"] for r in record["rungs"]], ["entries"])                 # the mock goes on whenever it is asked, to the cap
        r = record["rungs"][0]
        self.assertTrue(r["reached_cap"] and r["clean"] and r["pieces"] > 1 and r["tokens"] == lengthprobe.CAP)
        self.assertEqual((record["reliable_tokens"], record["bounded"]), (lengthprobe.CAP, True))
        self.assertGreater(record["first_stop_tokens"], 0)                                    # where it first closed a reply
        self.assertEqual(record["model"]["name"], "Mock")
        self.assertIn("relaxed_drafting", record["settings"])
        self.assertEqual(self.svc.lengths.limit_tokens(), lengthprobe.CAP)
        first, asked = record["measured_at"], len(self.engine.requests)
        self.assertEqual(asked, r["pieces"])
        self.sup.thread = None
        self.run_it()                                                        # a second load finds the record and asks nothing
        self.assertEqual(lengthprobe.read("feed01")["measured_at"], first)
        self.assertEqual(len(self.engine.requests), asked)

    def test_the_setting_turns_the_measurement_off(self):
        self.svc.measure_length = False
        self.run_it()
        self.assertIsNone(lengthprobe.read("feed01"))
        self.assertEqual(self.engine.requests, [])

    def test_the_button_measures_again_and_stop_ends_a_running_one(self):
        self.run_it()
        self.assertFalse(self.sup.stop_length())                             # nothing runs
        before = len(self.engine.requests)
        self.assertTrue(self.sup.measure_length(wait=True))
        self.assertEqual(len(self.engine.requests), 2 * before)

    def test_a_loop_in_the_text_cancels_the_try_where_it_is_found(self):
        tok = ByteTokenizer()
        looping = MockEngine(tok, "Chapter 1. The start of the long story is told in plain words. " + LOOP * 3, max_context=32768)
        svc = Service(looping, tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))
        svc.supervisor = sup = supervisor.Supervisor(svc, [], mock=False, facts=self.sup.facts)
        sup.auto_calibrate = False
        svc.lengths = lengthprobe.Lengths(lambda: sup.facts)
        with contextlib.redirect_stdout(io.StringIO()):
            sup.measure_length(wait=True)
        record = lengthprobe.read("feed01")
        self.assertTrue(all(r["looped"] for r in record["rungs"]), record["rungs"])
        self.assertLess(record["reliable_tokens"], 2_000)
        self.assertFalse(record["reached_cap"])

    def test_the_status_and_the_page_carry_the_numbers(self):
        self.run_it()
        self.assertTrue(self.svc.v1_status()["length"]["known"])
        self.assertGreater(self.svc.v1_status()["length"]["limit_tokens"], 0)
        self.assertEqual(Service(self.engine, ByteTokenizer(), ChatTemplate(ROOT / "serve" / "chat_template.jinja")).v1_status()["length"], {"known": False})
        for key in ("ln.title", "ln.limit", "ln.measure", "ln.stop", "ln.unknown", "ln.help", "ln.try", "ln.cp.ok", "ld.step.length", "f.server.measure-length", "h.server.measure-length"):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        self.assertNotIn("asked_words", JS)
        self.assertIn('data-act="length"', JS)
        self.assertIn("api/loader/length", JS)


if __name__ == "__main__":
    unittest.main()
