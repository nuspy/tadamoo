"""The "Train a personalised model" section: the markup that loads it, the requests it makes are routes the voice proxy lets through, a training starts only from the
dialog with confirm: true on the plan's token, both languages have every string, the page puts nothing the server says into HTML, and the styles are touch-sized and
mobile first. The layout itself is not rendered here: the page is static."""
import re
import unittest
from pathlib import Path

from serve import voice_proxy

WEB = Path(__file__).resolve().parent.parent / "web"
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


if __name__ == "__main__":
    unittest.main()
