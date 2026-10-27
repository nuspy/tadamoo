"""Settings > Phones: Tailscale installed and connected from the page, phones turned on, a key for each phone and its QR code."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "mobile.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


class PhonesPageTest(unittest.TestCase):
    def test_the_page_is_a_part_of_settings_loaded_with_its_style(self):
        self.assertIn('id="mb-section"', HTML)
        self.assertRegex(HTML, r'<section class="sec stmobile" data-sub="mobile"')
        self.assertIn("web/mobile.js", HTML)
        self.assertIn("web/mobile.css", HTML)
        self.assertIn('{ id: "mobile", view: "settings"', (WEB / "subtabs.js").read_text(encoding="utf-8"))

    def test_it_installs_and_connects_tailscale_turns_phones_on_and_makes_and_revokes_keys_through_the_setup_routes(self):
        for route in ("api/remote", "api/remote/tailscale/install", "api/remote/tailscale/up", "api/remote/keys/create", "api/remote/keys/revoke"):
            self.assertIn(route, JS)
        self.assertIn('`api/remote/${cb.checked ? "enable" : "disable"}`', JS)
        self.assertIn("armed === k.id", JS.replace("sure = armed === k.id", "armed === k.id"))               # taking a key away asks twice

    def test_a_key_is_shown_once_and_as_a_qr_code_drawn_on_a_canvas_by_a_vendored_library(self):
        self.assertIn("shown = { key: r.data.key", JS)
        self.assertIn("mb.fresh.once", JS)
        self.assertIn('s.src = "web/vendor/qrcode.js"', JS)
        self.assertNotIn("qrcode.js", HTML)
        self.assertIn("getContext(\"2d\")", JS)
        self.assertIn("JSON.stringify({ v: 1, url:", JS)
        self.assertTrue((WEB / "vendor" / "qrcode.js").is_file() and (WEB / "vendor" / "LICENSE-qrcode-generator.txt").is_file())

    def test_the_script_writes_no_markup_and_the_texts_exist_in_both_languages(self):
        for banned in ("innerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(banned, JS)
        self.assertGreaterEqual(JS.count("authHeaders()"), 1)
        keys = {k for k in re.findall(r'T\("(mb\.[a-z.0-9]+)"', JS) if not k.endswith(".")} | set(re.findall(r'data-t="(mb\.[a-z.]+)"', HTML)) | {"sub.mobile", "pal.set.mobile"}
        self.assertGreater(len(keys), 30)
        for key in keys:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_keys_have_an_end_that_can_be_chosen_and_renewed_and_the_limits_for_phones_are_set_with_an_explanation(self):
        for route in ("api/remote/keys/renew", "api/remote/limits"):
            self.assertIn(route, JS)
        self.assertIn("window.confirm(T(\"mb.keys.neverconfirm\"))", JS)             # the risk of a key that never ends is said before it is made
        dynamic = [f"mb.mode.{m}" for m in ("plan", "read_only", "default", "auto")] + [f"mb.lim.mode.help.{m}" for m in ("plan", "read_only", "default", "auto")]
        for key in dynamic + ["mb.keys.ends", "mb.keys.expired", "mb.keys.noend", "mb.keys.renew", "mb.keys.forever", "mb.lim.rules.help", "mb.lim.mode"]:
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_where_to_listen_is_chosen_from_three_ways_each_explained_in_both_languages(self):
        self.assertIn("api/remote/listen", JS)
        for value in ('"tailscale"', '"0.0.0.0"', '"custom"'):
            self.assertIn(value, JS)
        for key in ("label", "tailscale", "all", "custom", "ip", "help.tailscale", "help.all", "help.custom", "save", "save.tip", "saved"):
            self.assertEqual(I18N.count(f'"mb.listen.{key}":'), 2, key)


if __name__ == "__main__":
    unittest.main()
