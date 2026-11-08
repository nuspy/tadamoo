"""Models > Voice services and the Voice functions dialog: the markup, the scripts and styles, the routes the page uses, the confirmations and
the translations. The page is static here; the layout is checked in a browser by hand."""
import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "voiceload.js").read_text(encoding="utf-8")
VOICE_JS = (WEB / "voice.js").read_text(encoding="utf-8")
PROVIDERS = (WEB / "providers.js").read_text(encoding="utf-8")
ENGINES = (WEB / "engines.js").read_text(encoding="utf-8")
CSS = (WEB / "voice.css").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")
HANDLER = (WEB.parent / "handler.py").read_text(encoding="utf-8")


class VoiceLoaderPageTest(unittest.TestCase):
    def test_the_tab_the_button_and_the_panel_are_in_the_models_view(self):
        view = HTML.split('id="view-loader"')[1].split('id="view-wallet"')[0]
        self.assertIn('id="sub-voicesvc" aria-selected="false" aria-controls="ld-voicesvc" data-t="vl.sub"', view)
        self.assertRegex(view, r'<div class="vl" id="ld-voicesvc" role="tabpanel" aria-labelledby="sub-voicesvc" hidden></div>')
        self.assertIn('id="vl-open" data-t="vl.open"', view)
        self.assertRegex(view, r'id="vl-reload" data-t="vl.reload" hidden')
        self.assertLess(view.index('id="vl-head"'), view.index('class="subtabs"'))          # the buttons are above the tabs, whichever tab is shown
        self.assertIn('<script defer src="web/voiceload.js"></script>', HTML)

    def test_the_other_models_tabs_hide_the_panel_and_this_tab_hides_theirs(self):
        self.assertIn('vc = which === "voicesvc"', PROVIDERS)
        self.assertIn('vcPanel.hidden = !vc', PROVIDERS)
        self.assertIn("qwen.hidden = prov || cv || lc || vc", PROVIDERS)
        self.assertIn('vcTabBtn.onclick = () => sub("voicesvc")', PROVIDERS)
        self.assertIn('["providers", "converter", "llamacpp", "voicesvc"].includes(saved)', PROVIDERS)
        self.assertIn('"llamacpp", "voicesvc"];', ENGINES)
        self.assertIn('"ld-llamacpp", "ld-voicesvc"', ENGINES)

    def test_every_route_the_page_calls_exists_in_the_server(self):
        for route in ("api/voice/functions", "api/voice/models", "api/voice/functions/plan", "api/voice/functions/apply", "api/voice/functions/reload-llm",
                      "api/loader/status"):
            self.assertIn(f'"{route}"', JS, route)
        for route in ("/api/voice/functions", "/api/voice/functions/plan", "/api/voice/functions/apply", "/api/voice/functions/reload-llm", "/api/voice/enabled"):
            self.assertIn(f'"{route}"', HANDLER, route)
        self.assertIn('"api/voice/enabled"', VOICE_JS)
        self.assertIn("authHeaders()", JS)
        self.assertNotRegex(JS, r'fetch\(\s*["`]/')

    def test_what_the_server_says_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            self.assertNotIn(banned, JS)

    def test_nothing_is_applied_or_reloaded_without_the_confirmation_dialog(self):
        self.assertEqual(JS.count('"api/voice/functions/apply"'), 1)
        self.assertEqual(JS.count('"api/voice/functions/reload-llm"'), 1)
        apply_call = JS.split('"api/voice/functions/apply"')[0].rsplit("confirmDialog(", 1)[1]
        self.assertIn("async () =>", apply_call)                                             # the call is inside the confirm dialog's action
        self.assertIn("{ desired: draft, confirm: true }", JS)
        reload_call = JS.split('"api/voice/functions/reload-llm"')[0].rsplit("confirmDialog(", 1)[1]
        self.assertIn("async () =>", reload_call)
        self.assertIn("{ confirm: true }", JS)
        self.assertIn('document.getElementById(id + "-cancel").focus()', JS)                # the safe choice has the focus
        self.assertIn("apply.disabled = !plan.changes || plan.fits === false || !S.service.running", JS)

    def test_the_dialogs_are_real_dialogs_with_focus_returned_and_escape(self):
        self.assertEqual(JS.count("showModal()"), 1)
        self.assertIn('el("dialog", "vl-dlg")', JS)
        self.assertIn('d.setAttribute("aria-labelledby", id + "-h")', JS)
        self.assertIn("(d._from && d._from.offsetParent ? d._from : openBtn).focus()", JS)                                                # native dialogs close on Escape and trap the focus
        self.assertIn("first.focus()", JS)
        self.assertIn("aria-live", JS)

    def test_the_confirmations_say_what_loads_what_unloads_and_that_the_model_reloads(self):
        for key in ("vl.p.loads", "vl.p.unloads", "vl.c.reload", "vl.c.noreload", "vl.r.done", "vl.r.reload"):
            self.assertIn(f'T("{key}"', JS, key)
        self.assertRegex(I18N, r'"vl\.c\.reload": "The model is unloaded now[^"]*loads again[^"]*reserve')
        self.assertIn("using all the free VRAM ({0}) minus the Loader's reserve ({1})", I18N)
        self.assertIn("usando tutta la VRAM libera ({0}) meno la riserva del Loader ({1})", I18N)

    def test_the_change_services_button_shows_only_when_something_is_pending(self):
        self.assertIn("reloadBtn.hidden = !(S && S.pending_reload)", JS)
        self.assertIn('btn(T("vl.reload"), "primary", () => confirmReload(), "vl-reload-tab")', JS)

    def test_settings_voice_shows_the_same_on_off_state_and_links_to_the_tab(self):
        self.assertIn("FUNCTIONS.includes(svc.id)", VOICE_JS)
        self.assertIn("input.checked = !!svc.enabled", VOICE_JS)
        self.assertIn('document.getElementById("sub-voicesvc").click()', VOICE_JS)

    def test_the_text_keys_exist_twice_in_the_translations(self):
        keys = set(re.findall(r'\bT\(\s*"([\w.]+)"', JS)) | set(re.findall(r'"(vl\.ph\.[\w]+)"', JS)) | set(re.findall(r'data-t="(vl\.[\w.]+)"', HTML))
        keys |= set(re.findall(r'\bT\(\s*"(vl\.[\w.]+)"', VOICE_JS))
        self.assertGreater(len(keys), 40)
        for key in sorted(keys):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)
        defined = set(re.findall(r'"(vl\.[\w.]+)":', I18N))
        self.assertEqual(sorted(k for k in defined if k not in keys), [])                    # nothing is translated that no page uses

    def test_the_styles_are_mobile_first_with_touch_sized_targets(self):
        self.assertIn(".vl-head .btn, .vl .btn, .vl-dlg .btn { min-height: 44px; min-width: 44px;", CSS)
        self.assertIn(".vl-field select { width: 100%; min-height: 44px;", CSS)
        self.assertIn(".vl-check { display: flex; align-items: center; gap: 12px; min-height: 44px;", CSS)
        self.assertIn("env(safe-area-inset-bottom)", CSS)
        self.assertIn("width: min(680px, calc(100vw - 32px))", CSS)
        self.assertIn("@media (max-width: 520px)", CSS)
        self.assertIn("prefers-reduced-motion", CSS)
        self.assertNotIn(":hover", CSS)
        self.assertNotRegex(CSS, r"[^\w-]width:\s*\d{4,}px")

    def test_no_emoji_icons(self):
        for text in (JS, CSS):
            self.assertIsNone(re.search("[\U0001F300-\U0001FAFF☀-➿]", text))


if __name__ == "__main__":
    unittest.main()
