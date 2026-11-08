"""Every limit on the thinking or the answer that the server holds is set from the Loader page, not only from the command line."""
import re
import unittest

from serve import ROOT, loader, settings
from serve.config import build_parser
from serve.service import Service
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.util.text import ByteTokenizer

LIMITS = ("reasoning-budget-tokens", "answer-room", "measure-length", "thinking-loop-tokens", "max-answer-tokens",
          "passage-loop-tokens", "passage-loop-times", "passage-loop-window")
JS = (ROOT / "serve" / "web" / "loader.js").read_text(encoding="utf-8")
I18N = (ROOT / "serve" / "web" / "i18n.js").read_text(encoding="utf-8")


def service():
    tok = ByteTokenizer()
    return Service(MockEngine(tok, "ok"), tok, ChatTemplate(ROOT / "serve" / "chat_template.jinja"))


class LimitsAreSettings(unittest.TestCase):
    def test_each_limit_is_a_field_of_the_page_with_a_default_and_a_text_in_both_languages(self):
        for name in LIMITS:
            self.assertIn(("server", name), settings.LOADER_FIELDS, name)
            self.assertIn("server." + name, loader.ENGINE_DEFAULTS, name)
            self.assertIn(f'name: "server.{name}"', JS, name)
            self.assertEqual(I18N.count(f'"f.server.{name}":'), 2, name)
            self.assertEqual(I18N.count(f'"h.server.{name}":'), 2, name)
            self.assertTrue(hasattr(build_parser().parse_args([]), name.replace("-", "_")), name)

    def test_the_defaults_never_cut_the_thinking(self):
        a = build_parser().parse_args([])
        self.assertFalse(a.answer_room)
        self.assertEqual((a.thinking_loop_tokens, a.max_answer_tokens), (0, 0))
        self.assertIsNone(a.reasoning_budget_tokens)

    def test_the_values_reach_the_service_at_start_and_at_a_reload(self):
        svc = service()
        a = build_parser().parse_args(["--answer-room", "--thinking-loop-tokens", "300", "--max-answer-tokens", "9000",
                                       "--passage-loop-tokens", "20", "--passage-loop-times", "3", "--passage-loop-window", "800",
                                       "--reasoning-budget-tokens", "5000"])
        svc.apply_limits(a)
        self.assertEqual((svc.answer_room, svc.thinking_loop_tokens, svc.max_answer_tokens, svc.repeat_ngram, svc.reasoning_budget_tokens),
                         (True, 300, 9000, (20, 3, 800), 5000))
        svc.apply_limits(build_parser().parse_args(["--passage-loop-tokens", "0"]))
        self.assertIsNone(svc.repeat_ngram)
        self.assertFalse(svc.answer_room)

    def test_the_page_saves_and_checks_them(self):
        for key, good, bad in (("answer-room", True, "yes"), ("thinking-loop-tokens", 256, -1), ("max-answer-tokens", 4000, "x"),
                               ("passage-loop-tokens", 12, 1.5), ("passage-loop-times", 4, -3), ("passage-loop-window", 2000, None)):
            self.assertEqual(settings.check_value("server", key, good), good, key)
            with self.assertRaises(ValueError, msg=key):
                settings.check_value("server", key, bad)


if __name__ == "__main__":
    unittest.main()
