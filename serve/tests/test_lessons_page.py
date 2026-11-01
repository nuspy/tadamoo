"""Settings > Memories > Lessons: the lessons of the project and the global ones, to read, correct, pin, delete and promote into AGENTS.md."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "lessons.js").read_text(encoding="utf-8")
CSS = (WEB / "lessons.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class LessonsPageTest(unittest.TestCase):
    def test_the_section_sits_in_the_memories_tab_with_its_script_style_and_palette_entry(self):
        self.assertRegex(HTML, r'<section class="sec stlessons" data-sub="memories" id="ls-section"')
        self.assertIn('id="ls-root"', HTML)
        self.assertIn("web/lessons.js", HTML)
        self.assertIn("web/lessons.css", HTML)
        self.assertIn('settingsAt("ls-h", "memories")', (WEB / "shell.js").read_text(encoding="utf-8"))
        self.assertIn('e.detail === "memories"', JS)

    def test_it_uses_the_lessons_routes_only(self):
        for route in ("api/lessons?", "api/lessons/", "api/lessons/promote", "api/lessons/append_agents"):
            self.assertIn(route, JS)
        for action in ("edit", "pin", "delete", "add"):
            self.assertIn(f'act("{action}"', JS)
        self.assertNotIn("api/memories", JS)

    def test_promoting_previews_first_and_writes_only_after_a_second_press_with_the_confirmation(self):
        self.assertLess(JS.index('T("ls.promote.go")'), JS.index('"api/lessons/append_agents"'))          # the write is the handler of the preview's own button
        self.assertIn("if (promo && promo.id === l.id) li.append(promotion(l))", JS)
        self.assertEqual(JS.count("api/lessons/append_agents"), 1)
        self.assertIn("confirm: true", JS)
        self.assertIn("ls.promote.help", JS)
        self.assertIn('armed === "d" + l.id', JS)                                       # deleting asks twice too

    def test_each_lesson_can_be_corrected_in_place_in_its_three_fields(self):
        for field in ("situation", "went_wrong", "fix"):
            self.assertIn(f"{field}: a", JS)
        self.assertIn('"ls.empty"', JS)
        self.assertIn('"ls.out.none"', JS)

    def test_the_script_writes_no_markup(self):
        for banned in ("innerHTML", "insertAdjacentHTML", "document.write", "eval(", "outerHTML"):
            self.assertNotIn(banned, JS)
        self.assertIn("authHeaders()", JS)

    def test_it_is_usable_on_a_phone_and_without_hover(self):
        self.assertNotIn(":hover", CSS)
        self.assertIn("min-height: 44px", CSS)
        self.assertIn("@media (max-width: 520px)", CSS)
        self.assertIn("overflow-wrap: anywhere", CSS)
        self.assertNotRegex(CSS, r"(?<![-\w])width:\s*\d{3,}px")                                 # no fixed width that could scroll sideways

    def test_the_texts_exist_in_both_languages(self):
        keys = {k for k in re.findall(r'T\(\s*"(ls\.[a-z.0-9_]+)"', JS) if not k.endswith(".")} | set(re.findall(r'data-t="(ls\.[a-z.]+)"', HTML)) | {"pal.set.lessons"}
        keys |= {f"ls.result.{r}" for r in ("done", "stalled", "abandoned", "stopped", "failed")}
        keys |= {"ls.promote.done", "ls.promote.created", "ls.promote.already", "ls.empty", "ls.empty.global"}      # picked by a condition or passed as a name
        self.assertGreater(len(keys), 40)
        for key in keys:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        for tag in re.findall(r'"(ls\.[a-z.0-9_]+)":', I18N):
            self.assertIn(tag, keys)


if __name__ == "__main__":
    unittest.main()
