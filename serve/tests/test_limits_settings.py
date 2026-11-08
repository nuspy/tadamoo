"""Every limit on the thinking or the answer that the server holds is set from the Loader page, not only from the command line."""
import re
import unittest

from serve import ROOT, loader, settings
from serve.config import build_parser
from serve.service import Service
from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.util.text import ByteTokenizer

LIMITS = ("answer-room", "measure-length", "thinking-loop-tokens",
          "passage-loop-tokens", "passage-loop-times", "passage-loop-window",
          "thinking-passage-loop-tokens", "thinking-passage-loop-times", "thinking-passage-loop-window")
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
        self.assertEqual(a.thinking_loop_tokens, 0)
        svc = service()
        self.assertEqual((svc.reasoning_budget({}), svc.answer_cap({})), (None, 0))

    def test_the_values_reach_the_service_at_start_and_at_a_reload(self):
        svc = service()
        a = build_parser().parse_args(["--answer-room", "--thinking-loop-tokens", "300",
                                       "--passage-loop-tokens", "20", "--passage-loop-times", "3", "--passage-loop-window", "800",])
        svc.apply_limits(a)
        self.assertEqual((svc.answer_room, svc.thinking_loop_tokens, svc.repeat_ngram),
                         (True, 300, (20, 3, 800)))
        svc.apply_limits(build_parser().parse_args(["--passage-loop-tokens", "0"]))
        self.assertIsNone(svc.repeat_ngram)
        self.assertFalse(svc.answer_room)

    def test_the_page_saves_and_checks_them(self):
        for key, good, bad in (("answer-room", True, "yes"), ("thinking-loop-tokens", 256, -1),
                               ("passage-loop-tokens", 12, 1.5), ("passage-loop-times", 4, -3), ("passage-loop-window", 2000, None)):
            self.assertEqual(settings.check_value("server", key, good), good, key)
            with self.assertRaises(ValueError, msg=key):
                settings.check_value("server", key, bad)

    def test_the_removed_fields_are_not_on_the_page_and_a_request_decides_alone(self):
        for name in ("reasoning-budget-tokens", "max-answer-tokens"):
            self.assertNotIn(("server", name), settings.LOADER_FIELDS, name)
            self.assertNotIn("server." + name, loader.ENGINE_DEFAULTS, name)
            self.assertNotIn(f'"server.{name}"', JS, name)
            self.assertNotIn(f'"f.server.{name}"', I18N, name)
            self.assertIn(f"server.{name}", settings.REMOVED_KEYS)
            self.assertFalse(hasattr(service(), name.replace("-", "_")), name)

    def test_a_saved_file_that_still_holds_them_drops_the_keys_on_save(self):
        raw = {"server": {"port": 1, "reasoning-budget-tokens": 4096, "Max_Answer_Tokens": 9000}, "engine": {}}
        out, changed = settings.merge(raw, {"server.port": 2}, settings.LOADER_FIELDS)
        self.assertEqual(out["server"], {"port": 2})
        self.assertEqual(changed, ["server.port"])
        out, _ = settings.merge(raw, {"server.reasoning-budget-tokens": 5}, settings.LOADER_FIELDS)    # a form that still sends one: ignored
        self.assertEqual(out["server"], {"port": 1})
        self.assertNotIn("server.reasoning-budget-tokens", settings.values_of(out, settings.LOADER_FIELDS))

    def test_minus_one_is_no_limit_for_both_request_fields(self):
        svc = service()
        for value in (0, -1):
            self.assertIsNone(svc.reasoning_budget({"reasoning_budget_tokens": value}), value)
            self.assertEqual(svc.answer_cap({"max_answer_tokens": value}), 0, value)
        self.assertEqual((svc.reasoning_budget({"reasoning_budget_tokens": 50}), svc.answer_cap({"max_answer_tokens": 70})), (50, 70))
        for bad in ("x", 1.5, True):
            with self.assertRaises(ValueError, msg=bad):
                svc.answer_cap({"max_answer_tokens": bad})
            with self.assertRaises(ValueError, msg=bad):
                svc.reasoning_budget({"reasoning_budget_tokens": bad})


if __name__ == "__main__":
    unittest.main()
