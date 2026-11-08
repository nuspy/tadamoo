"""Settings > Web search: the page that chooses the search provider and the page reader, keeps the keys and installs the local programs on request."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "websearch.js").read_text(encoding="utf-8")
CSS = (WEB / "websearch.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
SUBTABS = (WEB / "subtabs.js").read_text(encoding="utf-8")
PROVIDERS = ("duckduckgo", "searxng", "brave", "tavily", "exa", "firecrawl", "playwright")


class WebSearchPageTest(unittest.TestCase):
    def test_the_page_is_a_part_of_settings_with_its_script_style_and_tab(self):
        self.assertRegex(HTML, r'<section class="sec stwebsearch" data-sub="websearch" id="ws-section" aria-labelledby="ws-h"')
        self.assertIn('id="ws-root"', HTML)
        self.assertIn('<script defer src="web/websearch.js"></script>', HTML)
        self.assertIn('href="web/websearch.css"', HTML)
        self.assertIn('{ id: "websearch", view: "settings", label: () => T("sub.websearch") }', SUBTABS)
        self.assertIn('settingsAt("ws-h", "websearch")', (WEB / "shell.js").read_text(encoding="utf-8"))

    def test_every_route_of_the_server_is_used_and_the_data_loads_when_the_tab_is_shown(self):
        for route in ('"api/websearch"', '"api/websearch/select"', '"api/websearch/key"', '"api/websearch/test"', 'api/websearch/plan?provider=',
                      '"api/websearch/install"', 'api/websearch/job?id=', '"api/websearch/uninstall"'):
            self.assertIn(route, JS)
        self.assertIn('"tadamoo:sub"', JS)
        self.assertIn('e.detail !== "websearch"', JS)
        self.assertIn("authHeaders()", JS)

    def test_what_the_server_says_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, JS)
        self.assertIn("rel = \"noopener noreferrer\"", JS)
        self.assertIn('a.target = "_blank"', JS)
        self.assertIn("/^https:\\/\\//i", JS)                                       # a link comes from the server: only https

    def test_the_key_field_is_write_only(self):
        self.assertIn('input.type = "password"', JS)
        self.assertIn('input.autocomplete = "off"', JS)
        self.assertIn('input.value = ""', JS)                                        # never filled back
        self.assertRegex(JS, r"async function setKey\([^)]*\) \{\s*input\.value = \"\";")   # cleared before the request is even sent
        self.assertNotRegex(JS, r"\.key\b\s*[,;)]\s*(?!_set)")                       # the overview has no key to show; only key_set is read

    def test_the_install_button_exists_only_after_the_steps_are_shown_and_asks_nothing_by_itself(self):
        self.assertIn("if (s.showing) box.append(stepsBlock(p))", JS)
        steps = JS.split("function stepsBlock", 1)[1].split("async function remove", 1)[0]
        self.assertIn("startInstall", steps)
        self.assertIn('T("ws.safety")', steps)
        self.assertEqual(JS.count('"api/websearch/install"'), 1)
        self.assertEqual(JS.count("startInstall("), 2)                                # its definition and the one button
        self.assertIn("if (!plan.automatic)", steps)                                  # a guide, not an install, for Firecrawl
        self.assertIn('role", "log"', JS)
        self.assertIn('T("ws.job.step"', JS)

    def test_removing_needs_a_second_press(self):
        self.assertIn("if (!s.armed)", JS)
        self.assertEqual(JS.count('"api/websearch/uninstall"'), 1)

    def test_one_radio_group_chooses_the_search_and_one_the_reader(self):
        self.assertIn('radio("ws-search"', JS)
        self.assertEqual(JS.count('radio("ws-reader"'), 2)                            # none at the top, and the cards of the readers
        self.assertIn('{ reader: "none" }', JS)
        self.assertIn("{ search: pid }", JS)
        self.assertIn("{ autostart: cb.checked }", JS)
        self.assertIn("{ mode: { [p.id]: m } }", JS)
        self.assertIn("{ urls: { [pid]: input.value.trim() } }", JS)

    def test_the_texts_exist_once_in_each_language(self):
        keys = {k for k in re.findall(r'\bT\(\s*"(ws\.[a-z.0-9_]+)"', JS) if not k.endswith(".")} | set(re.findall(r'data-t="(ws\.[a-z.]+)"', HTML))
        keys |= {"sub.websearch", "pal.set.websearch"}
        for pid in PROVIDERS:
            keys |= {f"ws.p.{pid}.what", f"ws.p.{pid}.good", f"ws.p.{pid}.weak"}
        for name in ("docker", "git", "node"):
            keys.add(f"ws.need.{name}")
        for kind in ("local", "cloud"):
            keys.add(f"ws.mode.{kind}")
        for name in ("running", "runningdown", "stopped", "none", "up", "down", "npx"):
            self.assertIn(f'"ws.st.{name}"', JS)                                       # the state of a local program is named by its key
            keys.add(f"ws.st.{name}")
        for key in ("ws.cloud.signup", "ws.cloud.instances", "ws.steps.show", "ws.steps.hide", "ws.remove", "ws.remove.sure", "ws.test", "ws.test.running",
                    "ws.what.results", "ws.what.characters", "ws.key.saved", "ws.key.removed", "ws.good", "ws.weak"):
            keys.add(key)
        self.assertGreater(len(keys), 90)
        for key in keys:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        every = set(re.findall(r'"(ws\.[a-z.0-9_]+)":', I18N))
        self.assertLessEqual(len(every - keys), 0, sorted(every - keys))             # no text that no code asks for

    def test_every_provider_has_a_name_and_the_cards_say_what_is_good_and_weak(self):
        for pid in PROVIDERS:
            self.assertRegex(JS, rf"\b{pid}: \"")
        self.assertIn("goodWeak(pid)", JS)
        self.assertIn('T(`ws.p.${pid}.what`)', JS)
        self.assertIn('T(`ws.p.${pid}.${k}`)', JS)

    def test_the_style_is_mobile_first_touch_sized_and_motion_is_reduced(self):
        self.assertIn("min-height: 44px", CSS)
        self.assertIn(":focus-visible", CSS)
        self.assertIn("prefers-reduced-motion: reduce", CSS)
        self.assertRegex(CSS, r"font: 16px")                                         # no zoom on focus in iOS
        self.assertNotRegex(CSS, r"overflow-x:\s*(auto|scroll)")
        self.assertRegex(CSS, r"@media \(min-width: 640px\) \{ \.ws-gw")              # good and weak side by side only on wide screens
        self.assertNotIn("max-width: 360px", CSS)

    def test_state_is_never_shown_by_colour_alone(self):
        self.assertIn('icon(c.key_set ? "ok" : "none")', JS)
        self.assertIn('withIcon("check", "ws-badge ws-badge-on"', JS)
        self.assertIn('b.setAttribute("aria-pressed"', JS)
        self.assertIn('if (p.mode === m) b.append(icon("check"))', JS)
        self.assertIn('s.setAttribute("aria-hidden", "true")', JS)


if __name__ == "__main__":
    unittest.main()
