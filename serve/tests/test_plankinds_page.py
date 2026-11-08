"""The kinds of step on the Plans page: the pure part runs in node, the wiring is read from the files, and every kind and field of the server's table has its words in both languages."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from tadamoo.planner import nodes

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
NODE = shutil.which("node")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
KINDS_JS = (WEB / "plankinds.js").read_text(encoding="utf-8")
PLANS = (WEB / "plans.js").read_text(encoding="utf-8")
CSS = (WEB / "plans.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


@unittest.skipUnless(NODE, "node is needed to run the form logic")
class KindsLogicTest(unittest.TestCase):
    def test_every_case_passes(self):
        out = subprocess.run([NODE, str(HERE / "plankinds_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 8)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])


@unittest.skipUnless(NODE, "node is needed to run the palette")
class PaletteOnStandInDomTest(unittest.TestCase):
    def test_the_palette_and_the_form_work_with_clicks_drags_and_typed_values(self):
        out = subprocess.run([NODE, str(HERE / "plankinds_dom_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 9)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])


class WordsTest(unittest.TestCase):
    def both(self, key):
        self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_every_kind_has_a_name_a_sentence_and_its_family(self):
        for row in nodes.catalogue():
            for key in (f"pl.type.{row['type']}", f"pl.kind.{row['type']}.d", f"pl.fam.{row['family']}"):
                self.both(key)

    def test_every_field_and_choice_of_the_table_has_its_words(self):
        for row in nodes.catalogue():
            for f in row["fields"]:
                self.both(f"pl.spec.{f['name']}")
                for c in f.get("choices") or []:
                    self.both(f"pl.spec.opt.{c}")
        for unit in ("s", "min", "h", "d"):
            self.both(f"pl.unit.{unit}")

    def test_the_page_texts_exist_in_both_languages(self):
        used = set(re.findall(r'T\("(pl\.(?:pal|fact|judged|spec\.need)[\w.]*)"', PLANS + KINDS_JS)) | {"pl.pal.title", "pl.pal.help"}
        used = {k for k in used if not k.endswith(".")} | {"pl.judged.yes", "pl.judged.no", "pl.judged.unsure"}            # the answer is added to pl.judged. at run time
        self.assertGreaterEqual(len(used), 8)
        for key in sorted(used):
            self.both(key)

    def test_the_palette_in_the_table_and_the_icons_agree(self):
        icons = set(re.findall(r"^    (\w+): \"M", KINDS_JS, re.M))
        self.assertEqual(icons, {row["type"] for row in nodes.catalogue()})


class WiringTest(unittest.TestCase):
    def test_the_script_is_loaded_before_the_plans_page(self):
        self.assertLess(HTML.index("web/plankinds.js"), HTML.index("web/plans.js"))

    def test_a_kind_is_picked_by_tap_or_drop_and_put_on_the_plan_with_the_place_it_was_let_go(self):
        self.assertIn("pick: type => putKind(type, null)", PLANS)
        self.assertIn("putKind(type, at)", PLANS)
        self.assertIn("...(at ? { position: at } : {})", PLANS)

    def test_a_touch_never_drags_so_the_page_scrolls_and_a_drag_can_be_cancelled(self):
        self.assertIn('e.pointerType === "touch"', KINDS_JS)
        self.assertIn('e.key === "Escape"', KINDS_JS)
        self.assertIn("pointercancel", KINDS_JS)

    def test_the_inspector_saves_the_fields_of_the_kind(self):
        self.assertIn("saveNode(n.id, cond, specForm)", PLANS)
        self.assertIn("changes.spec = got.spec", PLANS)

    def test_the_palette_buttons_are_big_enough_and_the_page_does_not_scroll_sideways(self):
        self.assertRegex(CSS, r"\.pk-item \{[^}]*min-height: 44px")
        self.assertRegex(CSS, r"\.pk-item \{[^}]*width: 54px")                       # a small square: the icon and its name under it
        self.assertRegex(CSS, r"\.pk-name \{[^}]*font-size: 9px")
        self.assertRegex(CSS, r"\.pk-fam \{[^}]*border: 1px solid")                   # each family in a small frame whose legend breaks it
        self.assertIn('h("fieldset", "pk-fam"', KINDS_JS)
        self.assertIn('h("legend", "pk-famname"', KINDS_JS)
        self.assertNotRegex(CSS, r"\.pk[\w-]* \{[^}]*min-width:\s*\d{3,}px")
        self.assertIn("@media (max-width: 480px) { .pk-list", CSS)
        self.assertRegex(CSS, r"\.pk \{[^}]*display: flex")

    def test_nothing_the_server_says_is_put_in_the_page_as_html(self):
        self.assertNotIn("innerHTML", KINDS_JS)
