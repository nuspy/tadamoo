"""Chat: old code goes to the model as a plain description, the original is saved, the person's history is untouched, and the session only reads the block."""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tadamoo import side_uses  # noqa: E402
from serve import chatcode  # noqa: E402

PY = "\n".join(["# loads the lines of a file", "import os", "", "def load(path):", "    out = []", "    for line in open(path):", "        out.append(line.strip())",
                "    return out", "", "class Box:", "    def add(self, x):", "        return x"])
MESSAGE = "Can you check this?\n```python\n" + PY + "\n```\nIt fails on empty files."


class Session:
    """A service with a technical session that answers with `reply` and remembers what it was sent."""

    def __init__(self, reply="It reads a file and gives back its lines, and it defines a small box class.", window=8000, fail=False, session=True):
        self.reply, self.window, self.fail, self.session, self.calls = reply, window, fail, session, []

    def side_slot_info(self):
        return {"slot": 1, "context": self.window} if self.session else None

    def load(self):
        pass

    def prepare(self, msgs, tools, kw, max_new):
        return msgs, False, max_new

    def run(self, ids, thinking, tools, max_new, sampling, cancel):
        if self.fail:
            raise RuntimeError("engine gone")
        self.calls.append((sampling, ids))
        yield "event", types.SimpleNamespace(kind="content", text=self.reply)


def count(messages):
    return sum(len(m["content"]) for m in messages) // 3


class CodeAbstract(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"BASALT_DATA": self.data.name})
        self.env.start()
        side_uses.reset()

    def tearDown(self):
        side_uses.reset()
        self.env.stop()
        self.data.cleanup()

    def run_it(self, svc, text=MESSAGE):
        return chatcode.abstract_message(svc, text, 100000, count, Path(self.data.name))

    def test_the_blocks_are_the_fenced_ones_of_enough_lines_or_a_message_that_is_all_source(self):
        self.assertEqual(len(chatcode.blocks_of(MESSAGE, 6)), 1)
        self.assertEqual(chatcode.blocks_of(MESSAGE, 40), [])
        self.assertEqual(chatcode.blocks_of("Run it:\n```\nls -la\n```", 2), [])
        whole = chatcode.blocks_of(PY, 6)
        self.assertEqual((len(whole), whole[0][0], whole[0][1]), (1, 0, len(PY)))
        self.assertEqual(chatcode.blocks_of("A plain question about the weather and the plans for the weekend.\n" * 8, 6), [])

    def test_the_code_becomes_a_description_with_no_code_and_a_pointer_to_the_saved_original(self):
        svc = Session()
        out = self.run_it(svc)
        text = out["text"]
        self.assertTrue(text.startswith("Can you check this?\n") and text.endswith("It fails on empty files."))
        self.assertNotIn("def load", text)
        self.assertNotIn("```", text)
        self.assertIn("It reads a file and gives back its lines", text)
        self.assertEqual((out["blocks"], out["by"]), (1, ["model"]))
        path = Path(self.data.name) / out["saved"][0]
        self.assertTrue(out["saved"][0].startswith("chat_code/") and text.count(out["saved"][0]) == 1)
        self.assertIn("def load", path.read_text(encoding="utf-8"))                       # nothing is lost
        self.assertIn("ask the person to send it again", text)

    def test_the_session_reads_the_block_alone_under_its_own_prompt_in_its_own_slot(self):
        svc = Session()
        self.run_it(svc)
        sampling, ids = svc.calls[0]
        self.assertEqual((sampling.get("cache_slot"), sampling.get("side_use")), ("side", "code_abstract"))
        self.assertEqual([m["role"] for m in ids], ["system", "user"])                    # no conversation, no tools
        self.assertIn("def load", ids[1]["content"])
        self.assertNotIn("Can you check this?", ids[1]["content"])
        self.assertIn("Write no code", ids[0]["content"])

    def test_without_a_session_or_when_it_fails_or_writes_code_the_outline_is_used(self):
        for svc in (Session(session=False), Session(fail=True), Session(reply="```python\ndef load(path):\n    return 1\n```"), Session(reply=PY)):
            out = self.run_it(svc)
            self.assertEqual(out["by"], ["outline"])
            self.assertIn("python code of 12 lines", out["text"])
            self.assertIn("the classes Box", out["text"])
            self.assertIn("the functions load, add", out["text"])
            self.assertIn("loads the lines of a file", out["text"])
            self.assertNotIn("def load", out["text"])
        self.assertEqual(Session(session=False).calls, [])

    def test_the_outline_is_in_italian_for_an_italian_message(self):
        it = "Puoi controllare questo per favore? Non funziona con i file vuoti e non capisco il motivo.\n```python\n" + PY + "\n```"
        out = self.run_it(Session(session=False), it)
        self.assertIn("Codice python di 12 righe", out["text"])
        self.assertIn("rimandarlo", out["text"])

    def test_a_message_with_several_blocks_gets_a_description_for_each_and_the_use_can_be_switched_off(self):
        twice = MESSAGE + "\nand also\n```js\n" + "\n".join(f"const v{i} = {i};" for i in range(8)) + "\n```"
        out = self.run_it(Session(), twice)
        self.assertEqual(out["blocks"], 2)
        self.assertEqual(len(set(out["saved"])), 2)
        side_uses.save("code_abstract", on=False)
        svc = Session()
        self.assertEqual(self.run_it(svc), {"text": ""})
        self.assertEqual(svc.calls, [])

    def test_the_same_code_is_saved_once_and_a_short_block_is_left_as_it_is(self):
        a, b = self.run_it(Session()), self.run_it(Session())
        self.assertEqual(a["saved"], b["saved"])
        self.assertEqual(len(list((Path(self.data.name) / "chat_code").iterdir())), 1)
        self.assertEqual(self.run_it(Session(), "Look:\n```python\nx = 1\ny = 2\nz = 3\n```"), {"text": ""})

    def test_the_use_is_registered_with_its_prompt_and_its_number(self):
        use = side_uses.BY_ID["code_abstract"]
        self.assertTrue(use.needs_session and use.editable and use.default_on)
        self.assertEqual(side_uses.param("code_abstract", "min_lines"), 6)
        self.assertIs(side_uses.prompt_text("code_abstract"), chatcode.CODE_ABSTRACT_PROMPT)


if __name__ == "__main__":
    unittest.main()
