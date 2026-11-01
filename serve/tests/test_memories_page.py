"""Settings > Memories: switches to learn and to use for each memory, a weight for each section, the notes to read, write, correct and delete."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "memories.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class MemoriesPageTest(unittest.TestCase):
    def test_the_page_is_a_part_of_settings_with_its_tab_style_and_palette_entry(self):
        self.assertRegex(HTML, r'<section class="sec stmemories" data-sub="memories" id="me-section"')
        self.assertIn("web/memories.js", HTML)
        self.assertIn("web/memories.css", HTML)
        self.assertIn('{ id: "memories", view: "settings"', (WEB / "subtabs.js").read_text(encoding="utf-8"))
        self.assertIn('settingsAt("me-h", "memories")', (WEB / "shell.js").read_text(encoding="utf-8"))

    def test_it_uses_the_memories_routes_only(self):
        for route in ("api/memories?", "api/memories/settings", "api/memories/item", "api/memories/confirm", "api/memories/delete", "api/memories/clear", "api/memories/export", "api/memories/cards"):
            self.assertIn(route, JS)

    def test_each_memory_has_a_switch_to_learn_and_one_to_use_and_each_section_a_weight(self):
        self.assertIn('"learn_" + kind', JS)
        self.assertIn('"use_" + kind', JS)
        self.assertIn("weights: { [name]: sel.value }", JS)
        self.assertIn("S.weights.forEach", JS)                                          # the levels come from the server, not from the page
        self.assertIn("kind + \"_days\"", JS)

    def test_what_is_risky_is_marked_and_asks_twice(self):
        self.assertIn("me.cloud.risk", JS)
        self.assertIn("cloud = 2", JS)                                                  # two confirmations before the notes may go to another provider
        self.assertIn("saveSettings({ cloud_ok: true })", JS)
        self.assertEqual(JS.count("saveSettings({ cloud_ok: true })"), 1)
        self.assertIn("confirm: true", JS)                                              # clearing needs a second press and the server's confirmation
        self.assertIn('armed === "d" + i.id', JS)

    def test_the_script_writes_no_markup_and_the_texts_exist_in_both_languages(self):
        for banned in ("innerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(banned, JS)
        self.assertGreaterEqual(JS.count("authHeaders()"), 1)
        keys = {k for k in re.findall(r'T\(\s*"(me\.[a-z.0-9_]+)"', JS) if not k.endswith(".")} | set(re.findall(r'data-t="(me\.[a-z.]+)"', HTML)) | {"sub.memories", "pal.set.memories"}
        for kind in ("long", "short"):
            keys |= {f"me.{kind}.title", f"me.{kind}.help", f"me.learn.help.{kind}", f"me.days.{kind}", f"me.days.help.{kind}", f"me.clear.{kind}"}
        for name in ("identity", "place", "people", "companies", "partners", "character", "tastes", "interests", "values", "thinking", "foundations", "emotions",
                     "card", "facts", "searches", "topics", "projects"):
            keys |= {f"me.sec.{name}", f"me.sechelp.{name}"}
        for level in ("off", "low", "normal", "high", "highest"):
            keys.add(f"me.w.{level}")
        for what in ("added", "seen_again", "changed", "removed"):
            keys.add(f"me.log.{what}")
        keys |= {"me.cards.title", "me.cards.help", "me.cards.progress", "me.cards.bycode", "me.cards.on", "me.cards.done", "me.cards.off", "me.cards.stop", "me.cards.start"}
        for origin in ("said", "manual", "confirmed", "inferred"):
            keys.add(f"me.origin.{origin}")
        self.assertGreater(len(keys), 100)
        for key in keys:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)


if __name__ == "__main__":
    unittest.main()
