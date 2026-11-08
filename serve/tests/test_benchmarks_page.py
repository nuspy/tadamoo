"""Settings > Benchmarks: the tab's section, its scripts and styles, the routes it uses, the confirmations it asks for, and its texts in both languages."""
import re
import unittest
from pathlib import Path

from tadamoo.bench.analysis import CLASS_ORDER
from tadamoo.bench.registry import SPECS

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "bench.js").read_text(encoding="utf-8")
CSS = (WEB / "bench.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
SHELL = (WEB / "shell.js").read_text(encoding="utf-8")
SUBTABS = (WEB / "subtabs.js").read_text(encoding="utf-8")


class BenchmarksPageTest(unittest.TestCase):
    def test_the_section_is_a_settings_sub_tab_with_its_script_style_and_palette_entry(self):
        self.assertRegex(HTML, r'<section class="sec stbench" data-sub="benchmarks" id="bn-section"')
        self.assertIn('id="bn-root"', HTML)
        self.assertIn("web/bench.js", HTML)
        self.assertIn("web/bench.css", HTML)
        self.assertIn('id: "benchmarks", view: "settings"', SUBTABS)
        self.assertIn('settingsAt("bn-h", "benchmarks")', SHELL)
        self.assertIn('e.detail === "benchmarks"', JS)

    def test_it_uses_the_benchmark_routes_only(self):
        for route in ("api/bench\"", "api/bench/preflight?id=", "api/bench/run?id=", "api/bench/report?id=", "api/bench/\" + action"):
            self.assertIn(route, JS)
        for action in ("install", "update", "remove", "start", "stop", "resume", "ranking", "add_lesson", "prepare_publish", "begin_publish", "finish_publish"):
            self.assertTrue(f'"api/bench/{action}"' in JS or f'act("{action}"' in JS, action)
        self.assertNotIn("api/bench/overview", JS)                      # that route is the model test page's
        self.assertNotIn("api/lessons", JS)                             # lessons are added through the benchmark route
        self.assertEqual(len(re.findall(r"https?://", JS)), 0)          # the page itself reaches nowhere else

    def test_importing_and_removing_ask_first(self):
        self.assertLess(JS.index('T("bn.import.never")'), JS.index('act("install", b.id)'))          # the confirmation panel comes before the action
        self.assertIn('pending = b.id', JS)
        self.assertIn('armed === "rm" + b.id', JS)                                               # removing asks twice
        for text in ("bn.import.source", "bn.import.size", "bn.import.where"):
            self.assertIn(text, JS)

    def test_only_the_served_model_is_shown_and_there_is_no_model_choice(self):
        self.assertIn("bn.start.model", JS)
        self.assertIn("bn.model.only", JS)
        self.assertNotRegex(JS, r'"model"\s*:')                         # the start body names no model
        self.assertNotIn("api_key", JS)

    def test_the_power_limit_is_typed_only_when_it_could_not_be_read(self):
        self.assertIn("power_limit_measured", JS)
        self.assertIn("body.power_limit_w", JS)
        self.assertIn("bn.power.help", JS)

    def test_a_lesson_is_added_only_by_its_own_button(self):
        self.assertEqual(JS.count('"api/bench/add_lesson"'), 1)
        self.assertLess(JS.index('T("bn.les.add")'), JS.index('"api/bench/add_lesson"'))
        self.assertIn("bn.les.help", JS)

    def test_publishing_is_two_calls_with_a_typed_phrase_that_is_never_prefilled(self):
        self.assertEqual(JS.count('"api/bench/begin_publish"'), 1)
        self.assertEqual(JS.count('"api/bench/finish_publish"'), 1)
        self.assertLess(JS.index('"api/bench/begin_publish"'), JS.index('"api/bench/finish_publish"'))
        self.assertIn("inp.value = typed", JS)                           # the field starts from what was typed, which is empty after each begin
        self.assertRegex(JS, r'desk = r\.data; typed = ""')              # a new second step starts with an empty field
        self.assertNotRegex(JS, r"(typed|inp\.value)\s*=\s*desk\.")
        self.assertIn('["paste", "drop"]', JS)
        self.assertIn("phrase: typed", JS)
        self.assertEqual(JS.count("finish,"), 1)                         # the second step is the send button's handler ...
        self.assertEqual(JS.count("finish("), 1)                         # ... and nothing calls it: this is its definition

    def test_buttons_are_disabled_with_the_reason_shown(self):
        for key in ("bn.pub.provisional", "bn.pub.closed", "bn.pub.email", "bn.pub.none", "bn.pub.notdone"):
            self.assertIn(key, JS)
        self.assertIn("!!why.publish", JS)
        self.assertIn("!!why.prepare", JS)
        self.assertIn("R.provisional ?", JS)

    def test_the_ranking_uses_pasted_text_and_never_the_network(self):
        self.assertIn("leaderboard_text", JS)
        self.assertIn("bn.rank.paste", JS)
        self.assertIn("bn.rank.addr", JS)
        self.assertNotIn("window.open", JS)
        self.assertNotRegex(JS, r"fetch\(\s*(boardAddr|addr)")
        self.assertNotIn("href", JS)

    def test_the_script_writes_no_markup(self):
        for banned in ("innerHTML", "insertAdjacentHTML", "document.write", "eval(", "outerHTML", "new Function"):
            self.assertNotIn(banned, JS)
        self.assertIn("authHeaders()", JS)

    def test_a_field_being_typed_in_is_not_repainted_under_the_person(self):
        self.assertIn("FIELDS.test(a.tagName)", JS)
        self.assertGreaterEqual(JS.count("if (guard("), 5)

    def test_it_polls_only_while_visible_and_active(self):
        self.assertIn("visible && active()", JS)
        self.assertIn("clearTimeout(timer)", JS)

    def test_it_is_usable_on_a_phone_and_without_hover(self):
        self.assertNotIn(":hover", CSS)
        self.assertIn("min-height: 44px", CSS)
        self.assertIn("@media (max-width: 520px)", CSS)
        self.assertIn("overflow-wrap: anywhere", CSS)
        self.assertIn("prefers-reduced-motion", CSS)
        self.assertNotRegex(CSS, r"(?<![-\w])width:\s*\d{3,}px")                                 # no fixed width that could scroll sideways
        self.assertIn("var(--warn)", CSS)

    def test_the_texts_exist_in_both_languages(self):
        keys = {k for k in re.findall(r'T\(\s*"(bn\.[a-z0-9._]+)"', JS) if not k.endswith(".")} | set(re.findall(r'data-t="(bn\.[a-z.]+)"', HTML))
        keys |= {"sub.benchmarks", "pal.set.bench", "bn.les.added", "bn.les.confirmed", "bn.power.measured", "bn.power.typed"}      # picked by a condition
        keys |= {f"bn.job.{a}.{s}" for a in ("install", "update", "start") for s in ("running", "done", "failed")}
        keys |= {f"bn.status.{s}" for s in ("queued", "running", "done", "failed", "stopped", "unknown")}
        keys |= {f"bn.board.{k}" for k in ("none", "csv_pr", "email", "closed")}
        keys |= {f"bn.class.{c}" for c in CLASS_ORDER} | {f"bn.change.{c}" for c in CLASS_ORDER}
        keys |= {"bn.scoring.verifier", "bn.scoring.judge", "bn.scoring.both"}
        keys |= {f"bn.start.reasoning.{k}" for k in ("model", "low", "medium", "high")}
        self.assertGreater(len(keys), 150)
        for key in keys:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        for tag in re.findall(r'"(bn\.[a-z0-9._]+)":', I18N):
            self.assertIn(tag, keys)

    def test_the_registry_values_the_page_maps_all_have_texts(self):
        for spec in SPECS.values():
            self.assertIn(spec.leaderboard, ("none", "csv_pr", "email", "closed"))
            self.assertIn(spec.scoring, ("verifier", "judge", "verifier+judge"))
        self.assertEqual(set(re.findall(r'"(bn\.class\.[a-z_]+)":', I18N)), {f"bn.class.{c}" for c in CLASS_ORDER})

    def test_italian_placeholders_match_english(self):
        en, it = I18N.split("\n  it: {", 1) if "\n  it: {" in I18N else I18N.split("\r\n  it: {", 1)
        for key in re.findall(r'"(bn\.[a-z0-9._]+)":', en):
            e = re.search(rf'"{re.escape(key)}": "((?:[^"\\]|\\.)*)"', en).group(1)
            i = re.search(rf'"{re.escape(key)}": "((?:[^"\\]|\\.)*)"', it).group(1)
            self.assertEqual(sorted(re.findall(r"\{\d+\}", e)), sorted(re.findall(r"\{\d+\}", i)), key)


if __name__ == "__main__":
    unittest.main()
