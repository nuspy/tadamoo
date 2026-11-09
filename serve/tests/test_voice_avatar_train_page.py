"""The "Train a personalised model" section: the markup that loads it, the requests it makes are routes the voice proxy lets through, a training starts only from the
dialog with confirm: true on the plan's token, both languages have every string, the page puts nothing the server says into HTML, and the styles are touch-sized and
mobile first. The layout itself is not rendered here: the page is static."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from serve import voice_proxy

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
NODE = shutil.which("node")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "voicetrain.js").read_text(encoding="utf-8")
CSS = (WEB / "voicetrain.css").read_text(encoding="utf-8")
AVATAR_JS = (WEB / "voiceavatar.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")


def dictionary(lang):
    text = I18N.split("\n  it: {")[0] if lang == "en" else I18N.split("\n  it: {")[1]
    return dict(re.findall(r'"(vtr\.[\w.]+)": "((?:[^"\\]|\\.)*)"', text))


class TrainPage(unittest.TestCase):
    def test_the_styles_and_the_script_load_and_the_avatar_card_gives_it_a_part(self):
        self.assertIn('<link rel="stylesheet" href="web/voicetrain.css">', HTML)
        self.assertEqual(HTML.count("web/voicetrain.js"), 1)
        self.assertIn("window.VoiceTrain = {", JS)
        self.assertIn('"train"', AVATAR_JS)
        self.assertIn("window.VoiceTrain.paint(parts.train, ctx)", AVATAR_JS)

    def test_every_request_is_a_route_the_voice_proxy_lets_through(self):
        calls = re.findall(r'ctx\.svc\(\s*"(GET|POST)"\s*,\s*"([^"]+)"', JS)
        self.assertEqual(sorted(set(calls)), [("GET", "/avatar/list"), ("GET", "/avatar/train/status"), ("POST", "/avatar/train/cancel"), ("POST", "/avatar/train/export"),
                                             ("POST", "/avatar/train/plan"), ("POST", "/avatar/train/python"), ("POST", "/avatar/train/setup"), ("POST", "/avatar/train/start")])
        for method, path in calls:
            self.assertIn((method, path), voice_proxy.ROUTES)
        # the installation of a weight is the catalog's own flow on the server's routes: plan, confirm with the licence acknowledged, status
        direct = re.findall(r'ctx\.api\(\s*"(GET|POST)"\s*,\s*"([^"?]+)', JS)
        self.assertEqual(sorted(set(direct)), [("GET", "api/voice/download/plan"), ("GET", "api/voice/download/status"), ("POST", "api/voice/download")])
        self.assertNotRegex(JS, r"fetch\(")

    def test_a_training_starts_only_from_the_dialog_with_confirm_and_the_token_of_the_plan(self):
        self.assertEqual(len(re.findall(r'"/avatar/train/start"', JS)), 1)
        self.assertIn("{ token: plan.token, confirm: true }", JS)
        self.assertIn("go.disabled = starting || !p.ok", JS)                    # a plan with a refusal or a failed check cannot be started
        self.assertIn("showModal", JS)
        self.assertIn("b.onclick = () => ask(engine)", JS)                       # the engine button only asks for the plan

    def test_what_the_server_says_enters_the_page_as_text(self):
        for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "dangerouslySet"):
            self.assertNotIn(banned, JS)

    def test_both_languages_have_every_string_with_the_same_placeholders(self):
        en, it = dictionary("en"), dictionary("it")
        self.assertEqual(sorted(en), sorted(it))
        self.assertGreater(len(en), 40)
        for key in en:
            self.assertEqual(sorted(re.findall(r"\{\d\}", en[key])), sorted(re.findall(r"\{\d\}", it[key])), key)
        used = {k for k in re.findall(r'\bT\(\s*"(vtr\.[\w.]+)"', JS) if not k.endswith(".")}
        self.assertFalse(used - set(en), used - set(en))
        for engine in ("ultralight", "feathertalk"):
            self.assertIn(f"vtr.engine.{engine}.help", en)
        for phase in ("convert", "preprocess", "train", "export"):
            self.assertIn(f"vtr.phase.{phase}", it)
        self.assertIn("Chinese speech only", en["vtr.engine.feathertalk.help"])
        self.assertIn("solo su voce cinese", it["vtr.engine.feathertalk.help"])

    def test_the_dialog_shows_what_the_person_needs_before_confirming(self):
        for part in ("p.video", "p.estimate", "e.note", "p.vram", "p.creates", "p.checks", "p.problems", "p.resume_epoch", "c.ok"):
            self.assertIn(part, JS)
        self.assertIn("c.it : c.en", JS)                                        # the checks in the language of the page

    def test_the_set_up_panel_has_the_whole_flow_for_each_engine(self):
        for part in ("setupPanel(engine)", "s.items", "c.install", "openInstall(c.install)", 'inp.id = "vtr-python-" + engine', "savePython", "loadSetup(engine, true)", "s.environment",
                     "exportAgain", "s.trained", "card.append(b, setupPanel(engine))"):
            self.assertIn(part, JS)
        self.assertIn("confirm: true, ack: i.ack", JS)                          # the weights are installed only with the licence acknowledged
        self.assertIn("!i.ack", JS)
        self.assertIn("(p.noncommercial && !i.nc)", JS)
        self.assertIn("api/voice/download/plan", JS)

    def test_the_page_shows_no_licence_warning_for_ultralight(self):
        words = " ".join(v for v in dictionary("en").values()) + " " + " ".join(dictionary("it").values()) + JS
        for banned in ("lic.verify", "LICENSE file", "README badge", "badge", "to be confirmed", "da confermare"):
            self.assertNotIn(banned, words)

    def test_no_emoji(self):
        for text in (JS, CSS):
            self.assertIsNone(re.search("[\U0001F300-\U0001FAFF☀-➿]", text))

    def test_the_styles_are_mobile_first_with_touch_sized_targets(self):
        self.assertGreaterEqual(CSS.count("min-height: 44px"), 4)
        self.assertIn("@media (min-width: 1000px)", CSS)
        self.assertNotIn("@media (max-width", CSS)
        self.assertNotIn(":hover", CSS)
        self.assertIn("prefers-reduced-motion", CSS)

    # ---- the training video
    def test_the_training_video_requests_are_routes_the_proxy_and_the_relay_let_through(self):
        from serve import voice_avatar
        self.assertIn("/avatar/${name}/training-video", JS)
        for method in ("GET", "DELETE"):
            self.assertIn(f'ctx.svc("{method}", `/avatar/${{name}}/training-video`)', JS)
            status_ok = voice_proxy.PATTERNS and any(m == method and p.fullmatch("/avatar/Ada-2/training-video") for m, p, _c in voice_proxy.PATTERNS)
            self.assertTrue(status_ok, method)
        self.assertTrue(voice_avatar.TRAINING_UPLOAD.fullmatch("/api/voice/avatar/Ada-2/training-video"))
        self.assertIn("x.open(\"POST\", `api/voice/avatar/${encodeURIComponent(name)}/training-video`)", JS)
        self.assertIn('form.append("file", file)', JS)                                         # the field the service reads
        self.assertIn("authHeaders()", JS)
        self.assertNotRegex(JS, r"fetch\(")

    def test_the_section_asks_for_the_video_shows_progress_the_length_and_a_removal_with_consent_text(self):
        for part in ('T("vtr.tv.add")', 'T("vtr.tv.replace")', "x.upload.onprogress", "tvUp.x.abort()", 'id = "vtr-tv-file"', 'id = "vtr-tv-bar"', "T(\"vtr.tv.current\", clock(v.seconds)",
                     "v.in_range === false", 'id = "vtr-tv-remove"', 'id = "vtr-tv-yes"', 'T("vtr.tv.consent")', "videoSection(sec)", "p.video_source === \"training\""):
            self.assertIn(part, JS)
        self.assertIn("video/mp4,video/quicktime,video/webm,video/x-matroska,.mp4,.mov,.webm,.mkv", JS)
        self.assertIn("input.disabled = !!tvUp || mine", JS)                                   # not while it is being sent or the avatar is being trained
        self.assertIn("rm.disabled = mine", JS)
        self.assertIn("avatars = r.data.avatars || []", JS)                                    # an avatar with no clip can get a training video

    def test_the_removal_asks_first_and_the_video_is_never_sent_before_a_file_is_chosen(self):
        self.assertEqual(len(re.findall(r"if \(file\) sendVideo\(file\)", JS)), 1)
        self.assertIn("rm.onclick = () => { tvAsk = true; paintNow(); }", JS)
        self.assertEqual(len(re.findall(r'ctx\.svc\("DELETE"', JS)), 1)
        self.assertIn("yes.onclick = removeVideo", JS)

    def test_the_training_video_strings_exist_in_both_languages_and_say_the_person_must_have_agreed(self):
        en, it = dictionary("en"), dictionary("it")
        tv = {k for k in en if k.startswith(("vtr.tv.", "vtr.video."))}
        self.assertGreaterEqual(len(tv), 20)
        self.assertEqual(tv, {k for k in it if k.startswith(("vtr.tv.", "vtr.video."))})
        self.assertIn("3 to 5 minutes", en["vtr.tv.add"])
        self.assertIn("3-5 minuti", it["vtr.tv.add"])
        self.assertIn("agreed", en["vtr.tv.consent"])
        self.assertIn("consenso", it["vtr.tv.consent"])
        used = {k for k in re.findall(r'\bT\(\s*"(vtr\.tv\.[\w.]+)"', JS)}
        self.assertTrue(used and not used - set(en), used - set(en))
        for key in tv:
            self.assertEqual(sorted(re.findall(r"\{\d\}", en[key])), sorted(re.findall(r"\{\d\}", it[key])), key)

    def test_the_old_linux_only_remark_about_ultralight_is_gone(self):
        for text in (dictionary("en")["vtr.engine.ultralight.help"], dictionary("it")["vtr.engine.ultralight.help"]):
            self.assertNotIn("Linux", text)

    def test_the_training_video_styles_are_touch_sized(self):
        self.assertIn(".vtr-video input[type=\"file\"]::file-selector-button { min-height: 44px; min-width: 44px;", CSS)
        self.assertIn(".vtr-video input[type=\"file\"] { min-height: 44px;", CSS)


@unittest.skipUnless(NODE, "node is needed to run the section on the stand-in DOM")
class TrainingVideoOnAStandInDom(unittest.TestCase):
    def test_a_person_can_add_watch_replace_cancel_and_remove_the_training_video(self):
        out = subprocess.run([NODE, str(HERE / "voicetrain_video_dom_check.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        results = json.loads(out.stdout)
        self.assertGreaterEqual(len(results), 9)
        self.assertEqual([f"{r['name']}: {r['error']}" for r in results if not r["ok"]], [])


if __name__ == "__main__":
    unittest.main()
