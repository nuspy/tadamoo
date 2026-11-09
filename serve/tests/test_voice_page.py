"""Settings > Voice: the page that chooses the voice models, says where they load and installs them on request."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "voice.js").read_text(encoding="utf-8")
CSS = (WEB / "voice.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
SUBTABS = (WEB / "subtabs.js").read_text(encoding="utf-8")
HANDLER = (WEB.parent / "handler.py").read_text(encoding="utf-8")


class VoicePageTest(unittest.TestCase):
    def test_the_page_is_a_part_of_settings_with_its_script_style_and_tab(self):
        self.assertRegex(HTML, r'<section class="sec stvoice" data-sub="voice" id="vo-section" aria-labelledby="vo-h"')
        self.assertIn('id="vo-root"', HTML)
        self.assertIn('<script defer src="web/voice.js"></script>', HTML)
        self.assertIn('href="web/voice.css"', HTML)
        self.assertIn('{ id: "voice", view: "settings", label: () => T("sub.voice") }', SUBTABS)
        self.assertIn('settingsAt("vo-h", "voice")', (WEB / "shell.js").read_text(encoding="utf-8"))

    def test_every_route_of_the_server_is_used_and_the_data_loads_when_the_tab_is_shown(self):
        for route in ('"api/voice/models"', '"api/voice/select"', '"api/voice/placement"', '"api/voice/models-dir"', 'api/voice/download/plan?id=',
                      '"api/voice/download"', '"api/voice/download/status"', '"api/voice/download/cancel"'):
            self.assertIn(route, JS)
        for route in ("/api/voice/models", "/api/voice/download/plan", "/api/voice/download/status", "/api/voice/select", "/api/voice/placement",
                      "/api/voice/models-dir", "/api/voice/download", "/api/voice/download/cancel"):
            self.assertIn(f'"{route}"', HANDLER)
        self.assertIn('"tadamoo:sub"', JS)
        self.assertIn('e.detail !== "voice"', JS)
        self.assertIn("authHeaders()", JS)
        self.assertNotRegex(JS, r'fetch\(\s*["`]/')

    def test_what_the_server_says_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, JS)

    def test_a_download_starts_only_from_the_plan_after_the_acknowledgements(self):
        self.assertEqual(JS.count('"api/voice/download"'), 1)
        self.assertIn("{ id: m.id, token: p.token, confirm: true, ack: s.ack, noncommercial: s.nc }", JS)
        self.assertIn("go.disabled = !ready || !s.ack || (p.noncommercial && !s.nc)", JS)
        self.assertIn("input.disabled = !ready", JS)                                      # a plan with problems cannot be confirmed
        self.assertIn("if (p.noncommercial) checks.append(", JS)
        self.assertEqual(JS.count("openPlan("), 2)                                         # its definition and the one button
        self.assertIn("if (!m.downloadable)", JS)                                          # no button where the files are not fixed
        self.assertIn('role", "progressbar"', JS)

    def test_the_button_exists_only_for_a_model_that_is_not_installed(self):
        self.assertIn("if (!m.installed || partial) box.append(downloadPart(svc, m))", JS)        # partial: a set of voices (Piper) with some still missing
        self.assertIn("const partial = !!parts && parts.installed.length > 0 && parts.installed.length < parts.total", JS)

    def test_the_placement_cannot_ask_for_a_cpu_in_vram(self):
        self.assertIn('d === "cpu" ? { device: d, memory: "ram" }', JS)
        self.assertIn('v === "vram" && p.device === "cpu" ? { device: "auto", memory: v }', JS)

    def test_the_folder_sentence_and_save_button_are_there(self):
        self.assertIn('T("vo.dir.help")', JS)
        self.assertIn('"api/voice/models-dir", { path: input.value.trim() }', JS)
        self.assertIn("Changing the folder moves nothing", I18N)

    def test_the_text_keys_exist_twice_in_the_translations(self):
        keys = set(re.findall(r'\bT\(\s*"([\w.]+)"', JS)) | set(re.findall(r'data-t="(vo\.[\w.]+)"', HTML))
        keys |= {"sub.voice", "pal.set.voice"}
        self.assertGreater(len(keys), 60)
        for key in sorted(keys):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_a_file_for_the_phones_is_listed_but_cannot_be_chosen_as_the_function_model_and_the_avatar_function_has_its_emergency_name(self):
        self.assertIn("svc.models.filter(x => x.selectable !== false)", JS)
        self.assertIn('"vo.phones.file"', I18N)
        self.assertEqual(I18N.count('"vo.svc.avatar": "Avatar video from the PC (emergency, uses VRAM)"'), 1)
        self.assertEqual(I18N.count("\"vo.svc.avatar\": \"Video dell'avatar dal PC (emergenza, usa VRAM)\""), 1)

    def test_the_styles_are_mobile_first_with_touch_sized_targets(self):
        self.assertIn(".vo-root .btn { min-height: 44px; min-width: 44px;", CSS)
        self.assertIn("min-height: 44px", CSS.split(".vo-field input")[1].split("}")[0])
        self.assertIn(".vo-check { display: flex; align-items: center; gap: 12px; min-height: 44px;", CSS)
        self.assertIn("@media (max-width: 520px)", CSS)
        self.assertIn("prefers-reduced-motion", CSS)
        self.assertNotIn(":hover", CSS)                                                    # nothing is reachable by hover only
        self.assertNotRegex(CSS, r"[^\w-]width:\s*\d{4,}px")

    def test_no_emoji_icons(self):
        for text in (JS, CSS):
            self.assertIsNone(re.search("[\U0001F300-\U0001FAFF☀-➿]", text))


if __name__ == "__main__":
    unittest.main()
