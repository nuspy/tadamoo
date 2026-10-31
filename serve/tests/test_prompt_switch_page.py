"""The Prompt analysis switch (off, rules, full) in the Chat toolbar and in Settings > Inference: the script is run in node against a small
stand-in for the page, and the markup, styles and strings are read as text. No browser, no server, no model."""
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from serve.tests.test_web_static import strings

WEB = Path(__file__).resolve().parents[1] / "web"
NODE = shutil.which("node")
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "promptswitch.js").read_text(encoding="utf-8")
CSS = (WEB / "promptswitch.css").read_text(encoding="utf-8")
INTAKE = (WEB / "intake.js").read_text(encoding="utf-8")
AGENTCHAT = (WEB / "agentchat.js").read_text(encoding="utf-8")
SUBTABS = (WEB / "subtabs.js").read_text(encoding="utf-8")

TEXTS = {
    "off": "Your message goes to the model exactly as you wrote it. Nothing is checked or changed.",
    "rules": "Quick checks, no extra model call. It removes invisible characters and extra blank lines, writes the real date next to words like 'tomorrow', "
             "and reminds the model when you listed several items. It costs nothing.",
    "full": "Everything in Rules, and the model also reads your request first to see whether something is missing or risky. It may ask you a short question "
            "before it starts. It adds a few seconds to each message.",
}

STUB = r"""
const fs = require("fs");
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {}; this.className = ""; this.hidden = false; this.disabled = false;
    this.title = ""; this._text = ""; this.id = ""; this.tabIndex = 0; this.handlers = {}; this.parent = null; }
  get nodeType() { return 1; }
  append(...k) { k.forEach(x => { if (x instanceof El) x.parent = this; this.children.push(x); }); }
  replaceChildren(...k) { this.children = []; this.append(...k); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; }
  addEventListener(n, f) { (this.handlers[n] = this.handlers[n] || []).push(f); }
  fire(n, ev) { (this.handlers[n] || []).forEach(f => f(ev)); let p = this.parent; while (p) { (p.handlers[n] || []).forEach(f => f(ev)); p = p.parent; } }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get classes() { return this.className.split(/\s+/); }
  all(out = []) { this.children.forEach(c => { if (c instanceof El) { out.push(c); c.all(out); } }); return out; }
  querySelectorAll(sel) { const c = sel.replace(/^\./, ""); return this.all().filter(e => e.classes.includes(c)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  contains(o) { for (let n = o; n; n = n.parent) if (n === this) return true; return false; }
  focus() { document.activeElement = this; }
}
class Text { constructor(t) { this.textContent = t; this.nodeType = 3; } }
const mk = (id, cls, extra) => { const e = new El("div"); e.id = id; e.className = cls; Object.assign(e.dataset, extra || {}); return e; };
const byId = {};
const hosts = [mk("ps-chat", "ps", { panel: "ps-chat-panel" }), mk("ps-set", "ps", { panel: "ps-set-panel", label: "off" })];
const panels = [mk("ps-chat-panel", "ps-panel"), mk("ps-set-panel", "ps-panel")];
panels.forEach(p => { p.hidden = true; });
[...hosts, ...panels].forEach(e => { byId[e.id] = e; });
const handlers = {};
global.window = global;
global.LANG = "en";
global.document = { activeElement: null, getElementById: id => byId[id] || null, createElement: t => new El(t), createTextNode: t => new Text(t),
  querySelectorAll: sel => (sel === ".ps[data-panel]" ? hosts : []), addEventListener: (n, f) => { (handlers[n] = handlers[n] || []).push(f); } };
global.T = (k, ...a) => k + (a.length ? "(" + a.join("|") + ")" : "");
global.authHeaders = () => ({});
const calls = [];
let server = "off", failNext = false;
global.fetch = async (url, opts) => {
  calls.push({ url, method: opts.method, body: opts.body ? JSON.parse(opts.body) : null });
  if (opts.method === "POST") {
    if (failNext) { failNext = false; return { ok: false, status: 400, json: async () => ({ error: { message: "Choose off, rules or full." } }) }; }
    server = JSON.parse(opts.body).intake;
  }
  return { ok: true, status: 200, json: async () => ({ intake: server, modes: ["off", "rules", "full"] }) };
};
const wait = ms => new Promise(r => setTimeout(r, ms));
const opts = h => h.querySelectorAll(".ps-opt");
const checked = h => opts(h).filter(b => b.getAttribute("aria-checked") === "true").map(b => b.dataset.mode);
const key = (el, k) => { const ev = { key: k, prevented: false, preventDefault() { this.prevented = true; } }; el.fire("keydown", ev); return ev; };
(async () => {
  require("vm").runInThisContext(fs.readFileSync(process.env.SCRIPT, "utf8"));
  await wait(30);
  const [chat, set] = hosts, out = {};
  out.getFirst = calls[0];
  out.modes = opts(chat).map(b => b.dataset.mode);
  out.roles = opts(chat).map(b => b.getAttribute("role"));
  out.group = chat.querySelector(".ps-seg").getAttribute("role");
  out.groupLabel = chat.querySelector(".ps-seg").getAttribute("aria-labelledby") === chat.querySelector(".ps-label").id;
  out.titles = opts(chat).map(b => b.title);
  out.described = opts(chat).map(b => chat.all().some(e => e.id === b.getAttribute("aria-describedby") && e.textContent === b.title));
  out.startChecked = [checked(chat), checked(set)];
  out.tabStops = opts(chat).map(b => b.tabIndex);
  out.settingsLabelHidden = set.querySelector(".ps-label").classes.includes("sr") && !chat.querySelector(".ps-label").classes.includes("sr");
  // arrow keys move and choose; both hosts follow at once; one save, where the person stopped
  opts(chat)[1].focus();
  const ev = key(opts(chat)[1], "ArrowRight");
  out.arrowPrevented = ev.prevented;
  key(document.activeElement, "ArrowRight");                    // full, then wraps to off
  out.afterWrap = [checked(chat), checked(set), document.activeElement.dataset.mode];
  key(document.activeElement, "End");
  out.afterEnd = [checked(chat), checked(set)];
  out.postsBefore = calls.filter(c => c.method === "POST").length;
  await wait(400);
  out.posts = calls.filter(c => c.method === "POST").map(c => c.body);
  out.status = chat.querySelector(".ps-status").textContent;
  // a click in the other host
  opts(set)[0].fire("click", {});
  out.clickBoth = [checked(chat), checked(set)];
  await wait(400);
  out.server = server;
  // the "?" opens the same text in its panel and Escape closes it
  const help = chat.querySelector(".ps-help");
  out.helpLabel = help.getAttribute("aria-label");
  out.helpControls = help.getAttribute("aria-controls");
  help.fire("click", {});
  out.panelOpen = [!panels[0].hidden, help.getAttribute("aria-expanded")];
  out.panelText = panels[0].all().filter(e => e.tag === "dd").map(e => e.textContent);
  out.panelNow = panels[0].all().filter(e => e.tag === "dt" && e.attrs["aria-current"] === "true").map(e => e.textContent);
  help.focus();
  (handlers.keydown || []).forEach(f => f({ key: "Escape" }));
  out.panelAfterEscape = [panels[0].hidden, help.getAttribute("aria-expanded")];
  help.fire("click", {});
  (handlers.click || []).forEach(f => f({ target: byId["ps-set"] }));
  out.outsideClick = panels[0].hidden;
  // a refused save puts the old value back everywhere and says why
  failNext = true;
  opts(set)[2].fire("click", {});
  await wait(400);
  out.rolledBack = [checked(chat), checked(set), chat.querySelector(".ps-status").textContent, chat.querySelector(".ps-status").dataset.bad];
  // a value read elsewhere on the page is drawn in both
  window.PromptSwitch.sync("full");
  out.synced = [checked(chat), checked(set), window.PromptSwitch.mode()];
  process.stdout.write(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


@unittest.skipUnless(NODE, "node is needed to run the script")
class ScriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d) / "stub.js"
            stub.write_text(STUB, encoding="utf-8")
            done = subprocess.run([NODE, str(stub)], capture_output=True, text=True, timeout=60, env={**os.environ, "SCRIPT": str(WEB / "promptswitch.js")})
        assert done.returncode == 0, done.stderr
        cls.out = json.loads(done.stdout)

    def test_it_reads_the_saved_value_and_draws_three_radio_buttons_in_a_labelled_group(self):
        o = self.out
        self.assertEqual(o["getFirst"], {"url": "api/intake", "method": "GET", "body": None})
        self.assertEqual(o["modes"], ["off", "rules", "full"])
        self.assertEqual(o["roles"], ["radio"] * 3)
        self.assertEqual((o["group"], o["groupLabel"]), ("radiogroup", True))
        self.assertEqual(o["startChecked"], [["off"], ["off"]])
        self.assertEqual(o["tabStops"], [0, -1, -1])                               # one tab stop; the arrows move inside
        self.assertTrue(o["settingsLabelHidden"])                                   # the section title already says it

    def test_every_state_has_a_title_and_the_same_text_as_a_description(self):
        self.assertEqual(self.out["titles"], ["ps.off.d", "ps.rules.d", "ps.full.d"])
        self.assertEqual(self.out["described"], [True] * 3)

    def test_arrow_keys_choose_and_both_places_follow_with_one_save_where_the_person_stops(self):
        o = self.out
        self.assertTrue(o["arrowPrevented"])
        self.assertEqual(o["afterWrap"], [["off"], ["off"], "off"])
        self.assertEqual(o["afterEnd"], [["full"], ["full"]])
        self.assertEqual(o["postsBefore"], 0)
        self.assertEqual(o["posts"], [{"intake": "full"}])
        self.assertEqual(o["status"], "kn.saved")

    def test_a_click_in_one_place_is_shown_in_the_other_and_saved(self):
        self.assertEqual(self.out["clickBoth"], [["off"], ["off"]])
        self.assertEqual(self.out["server"], "off")

    def test_the_question_mark_opens_the_same_texts_and_escape_or_a_click_elsewhere_closes_them(self):
        o = self.out
        self.assertEqual((o["helpLabel"], o["helpControls"]), ("ps.help", "ps-chat-panel"))
        self.assertEqual(o["panelOpen"], [True, "true"])
        self.assertEqual(o["panelText"], ["ps.off.d", "ps.rules.d", "ps.full.d"])
        self.assertEqual(o["panelNow"], ["ps.off - ps.current"])
        self.assertEqual(o["panelAfterEscape"], [True, "false"])
        self.assertTrue(o["outsideClick"])

    def test_a_refused_save_puts_the_old_value_back_and_says_why(self):
        self.assertEqual(self.out["rolledBack"], [["off"], ["off"], "Choose off, rules or full.", "1"])

    def test_a_value_read_elsewhere_is_drawn_in_both(self):
        self.assertEqual(self.out["synced"], [["full"], ["full"], "full"])

    def test_the_scripts_parse(self):
        for name in ("promptswitch.js", "intake.js"):
            done = subprocess.run([NODE, "--check", str(WEB / name)], capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)


class WiringTest(unittest.TestCase):
    def test_the_page_has_both_places_the_stylesheet_and_the_script_before_intake(self):
        self.assertIn('href="web/promptswitch.css"', HTML)
        self.assertLess(HTML.index("web/promptswitch.js"), HTML.index("web/intake.js"))
        for ident, panel in (("ps-chat", "ps-chat-panel"), ("ps-set", "ps-set-panel")):
            self.assertRegex(HTML, rf'<div class="ps" id="{ident}" data-panel="{panel}"')
            self.assertRegex(HTML, rf'<div class="ps-panel" id="{panel}" hidden>')

    def test_the_chat_switch_is_in_the_toolbar_shared_by_the_chat_and_the_agent_tab_and_hidden_without_the_harness(self):
        head = HTML[HTML.index('class="sh chathead"'):HTML.index('class="acopy"')]
        self.assertIn('id="ps-chat"', head)
        self.assertLess(head.index('id="ps-chat"'), head.index('id="chatmode-label"'))
        self.assertIn('id="ps-chat-panel"', head)
        self.assertIn('$("#ps-chat").hidden = !on', AGENTCHAT)                       # the model-only chat has no look at a request

    def test_settings_inference_is_a_sub_tab_and_holds_the_switch_first(self):
        self.assertIn('id: "inference"', SUBTABS)
        first = HTML.index('data-sub="inference"')
        self.assertIn('id="ps-section"', HTML[first - 80:first + 120])
        self.assertLess(HTML.index('id="ps-section"'), HTML.index('id="iq-section"'))
        self.assertRegex(HTML, r'<section class="sec stintake" data-sub="inference" id="iq-section"')
        self.assertEqual(len(re.findall(r'data-sub="inference"', HTML)), 3)

    def test_the_old_select_is_gone_and_the_settings_page_keeps_the_switch_in_step(self):
        self.assertNotIn("iq-mode", INTAKE)
        self.assertNotIn('"select"', INTAKE)
        self.assertIn("PromptSwitch.sync(state.intake)", INTAKE)
        self.assertNotIn("intake: mode", INTAKE)                                      # Save sends the suggestions and the size, not the switch

    def test_nothing_the_server_says_enters_the_page_as_markup(self):
        for banned in ("innerHTML", "insertAdjacentHTML", "document.write", "eval(", "outerHTML"):
            self.assertNotIn(banned, JS)

    def test_it_is_keyboard_operable_with_visible_focus_and_30_px_buttons_and_never_scrolls_sideways(self):
        for needle in ("radiogroup", '"radio"', "ArrowRight", "ArrowLeft", "ArrowUp", "ArrowDown", "Home", "End", "aria-describedby", "aria-expanded", "aria-controls", "Escape"):
            self.assertIn(needle, JS)
        self.assertIn("min-height: var(--btn-h)", CSS)
        self.assertIn(":focus-visible", CSS)
        self.assertIn("flex-wrap: wrap", CSS)
        self.assertIn("overflow-wrap: anywhere", CSS)
        self.assertIn("(pointer: coarse)", CSS)
        self.assertNotIn("overflow-x", CSS)
        self.assertNotIn("white-space: nowrap; overflow", CSS)
        self.assertIn("\\2713", CSS)                                                  # the chosen state has a tick, not only a colour
        app = (WEB / "app.css").read_text(encoding="utf-8")
        self.assertIn("--btn-h: 30px", app)


class TextsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise unittest.SkipTest("node is needed to read the translations")
        cls.s = strings()

    def test_the_three_tooltips_are_the_owners_words_in_english(self):
        for mode, text in TEXTS.items():
            self.assertEqual(self.s["en"][f"ps.{mode}.d"], text)

    def test_every_text_exists_in_both_languages_translated_with_the_same_placeholders(self):
        keys = {"ps." + k for k in ("title", "intro", "off", "rules", "full", "off.d", "rules.d", "full.d", "help", "current")} | {"sub.inference", "pal.set.prompt"}
        keys |= set(re.findall(r'data-t="(ps\.[\w.]+)"', HTML)) | set(re.findall(r'\bT\(\s*"(ps\.[\w.]+)"', JS)) - {"ps."}
        for key in sorted(keys):
            self.assertIn(key, self.s["en"], key)
            self.assertIn(key, self.s["it"], key)
            self.assertNotEqual(self.s["en"][key], self.s["it"][key], key)
            self.assertEqual(sorted(re.findall(r"\{\d\}", self.s["en"][key])), sorted(re.findall(r"\{\d\}", self.s["it"][key])), key)
        self.assertEqual(self.s["it"]["ps.title"], "Analisi del prompt")
        self.assertEqual(self.s["en"]["ps.title"], "Prompt analysis")

    def test_the_italian_tooltips_say_the_same_things(self):
        it = self.s["it"]
        self.assertIn("esattamente come l'hai scritto", it["ps.off.d"])
        self.assertIn("senza chiamate extra", it["ps.rules.d"])
        self.assertIn("«domani»", it["ps.rules.d"])
        self.assertIn("breve domanda", it["ps.full.d"])
        self.assertIn("qualche secondo", it["ps.full.d"])

    def test_the_old_look_at_each_request_strings_are_gone(self):
        for key in ("iq.mode", "iq.mode.help", "iq.mode.off", "iq.mode.rules", "iq.mode.full", "iq.intake.title"):
            self.assertNotIn(key, self.s["en"], key)
            self.assertNotIn(key, self.s["it"], key)


if __name__ == "__main__":
    unittest.main()
