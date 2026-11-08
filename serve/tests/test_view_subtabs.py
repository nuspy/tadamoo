"""The pages under a tab: Monitor holds Server LLM, Hardware and LOGS; Settings holds General, Models, Cloud and privacy and the Wallet."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
SUB = (WEB / "subtabs.js").read_text(encoding="utf-8")
APP = (WEB / "app.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class ViewSubtabsTest(unittest.TestCase):
    def test_hardware_logs_and_wallet_are_not_tabs_of_their_own_but_stay_views(self):
        for name in ("hardware", "logs", "wallet"):
            self.assertNotIn(f'id="tab-{name}"', HTML)
            self.assertIn(f'id="view-{name}"', HTML)
            self.assertIn(f'"{name}"', re.search(r"const VIEWS = \[([^\]]*)\]", APP).group(1))
            self.assertIn(f"aria-labelledby=\"sub-{name}\"", HTML)                   # the strip's button names the page
        self.assertIn('const TAB_OF = { hardware: "stats", logs: "stats", security: "stats", wallet: "settings" };', APP)

    def test_the_strip_is_one_element_filled_by_the_script_and_loaded_after_the_app(self):
        self.assertEqual(HTML.count('id="viewsub"'), 1)
        self.assertLess(HTML.index("web/app.js"), HTML.index("web/subtabs.js"))
        for banned in ("innerHTML", "insertAdjacentHTML", "eval("):
            self.assertNotIn(banned, SUB)
        self.assertRegex(SUB, r"stats: \[[^\]]*sub\.server[^\]]*tab\.hardware[^\]]*tab\.logs")
        self.assertRegex(SUB, r"settings: \[[^\]]*sub\.general[^\]]*sub\.models[^\]]*sub\.privacy[^\]]*tab\.wallet")

    def test_every_settings_section_belongs_to_a_page_and_the_old_index_is_gone(self):
        self.assertNotIn("st-index", HTML)
        block = HTML[HTML.index('id="view-settings"'):HTML.index('id="view-logs"')]
        sections = re.findall(r'<section class="sec [^>]*>', block)
        self.assertGreaterEqual(len(sections), 7)
        for tag in sections:
            self.assertRegex(tag, r'data-sub="(general|inference|models|privacy|mobile|memories|personas|websearch|voice|benchmarks)"', tag)

    def test_the_monitor_has_no_tasks_or_changes_panel_and_the_requests_list_is_long(self):
        monitor = HTML[HTML.index('id="view-stats"'):HTML.index('id="view-plans"')]
        self.assertNotIn("task-root", monitor)
        self.assertNotIn("changes-root", monitor)
        from serve.service import REQUESTS_SHOWN
        self.assertGreaterEqual(REQUESTS_SHOWN, 100)

    def test_the_labels_exist_in_both_languages(self):
        for key in ("sub.server", "sub.general", "sub.models", "sub.privacy"):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)


if __name__ == "__main__":
    unittest.main()
