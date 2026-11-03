"""The Optimize prompt box in the Chat toolbar and in Settings > Inference, and the note under a rewritten message: the script is run in node against a
small stand-in for the page, and the markup, styles and strings are read as text. No browser, no server, no model."""
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
JS = (WEB / "optimize.js").read_text(encoding="utf-8")
CSS = (WEB / "promptswitch.css").read_text(encoding="utf-8")
INTAKE = (WEB / "intake.js").read_text(encoding="utf-8")
AGENTCHAT = (WEB / "agentchat.js").read_text(encoding="utf-8")
LOADER = (WEB / "loader.js").read_text(encoding="utf-8")
I18N = (WEB / "i18n.js").read_text(encoding="utf-8")

TIP = ("Before your message reaches the model, a quick helper pass rewrites it as a clearer, more complete instruction. The chat still shows what you wrote. "
       "Needs the technical session.")

STUB = r"""
const fs = require("fs");
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {}; this.className = ""; this.hidden = false; this.disabled = false;
    this.title = ""; this._text = ""; this.id = ""; this.tabIndex = 0; this.handlers = {}; this.parent = null; this.checked = false; this.indeterminate = false; this.type = ""; }
  get nodeType() { return 1; }
  append(...k) { k.forEach(x => { if (x instanceof El) x.parent = this; this.children.push(x); }); }
  replaceChildren(...k) { this.children = []; this.append(...k); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] === undefined ? null : this.attrs[k]; }
  addEventListener(n, f) { (this.handlers[n] = this.handlers[n] || []).push(f); }
  fire(n, ev) { (this.handlers[n] || []).forEach(f => f(ev)); }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get classes() { return this.className.split(/\s+/); }
  all(out = []) { this.children.forEach(c => { if (c instanceof El) { out.push(c); c.all(out); } }); return out; }
  querySelectorAll(sel) { const c = sel.replace(/^\./, ""); return this.all().filter(e => e.classes.includes(c)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
}
class Text { constructor(t) { this.textContent = t; this.nodeType = 3; } }
const mk = id => { const e = new El("div"); e.id = id; e.className = "po"; e.dataset.po = ""; return e; };
const hosts = [mk("po-chat"), mk("po-set")];
const handlers = {};
global.window = global;
global.LANG = "en";
global.document = { hidden: false, createElement: t => new El(t), createTextNode: t => new Text(t),
  querySelectorAll: sel => (sel === ".po[data-po]" ? hosts : []), addEventListener: (n, f) => { (handlers[n] = handlers[n] || []).push(f); },
  dispatchEvent: e => { global.dispatched.push(e.type); } };
global.dispatched = [];
global.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = (init || {}).detail; } };
const emit = n => (handlers[n] || []).forEach(f => f({}));
global.T = (k, ...a) => k + (a.length ? "(" + a.join("|") + ")" : "");
global.authHeaders = () => ({ "X-Test": "1" });
const calls = [];
let stored = false, side = null, offline = false, refuse = false;
global.fetch = async (url, opts) => {
  calls.push({ url, method: opts.method, body: opts.body ? JSON.parse(opts.body) : null, auth: opts.headers["X-Test"] });
  const ok = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
  if (url === "v1/status") { if (offline) throw new Error("down"); return ok({ concurrency: { serving: 1, side_slot: side } }); }
  if (opts.method === "POST") {
    if (refuse) { refuse = false; return ok({ error: { message: "needs the session" } }, 400); }
    stored = JSON.parse(opts.body).optimize_prompt;
  }
  return ok({ intake: "off", optimize_prompt: stored });
};
const wait = ms => new Promise(r => setTimeout(r, ms));
const box = h => h.querySelector(".po-in");
const view = h => ({ disabled: box(h).disabled, checked: box(h).checked, mixed: box(h).indeterminate, why: h.querySelector(".po-why").textContent,
  title: box(h).title, labelTitle: h.querySelector(".po-box").title, described: h.querySelector(".sr").textContent, off: h.dataset.off,
  status: h.querySelector(".po-status").textContent, bad: h.querySelector(".po-status").dataset.bad });
const posts = () => calls.filter(c => c.method === "POST").map(c => c.body);
const tick = (h, v) => { box(h).checked = v; box(h).fire("change", {}); };
(async () => {
  require("vm").runInThisContext(fs.readFileSync(process.env.SCRIPT, "utf8"));
  await wait(30);
  const [chat, set] = hosts, out = {};
  out.first = calls.slice(0, 2).map(c => [c.url, c.method, c.auth]);
  out.type = [box(chat).type, box(chat).tabIndex, box(chat).id, chat.querySelector(".po-text").textContent, box(chat).getAttribute("aria-describedby")];
  out.noSession = [view(chat), view(set)];
  tick(chat, true);                                              // a change event while there is no session saves nothing
  await wait(30);
  out.noSessionTick = [posts(), view(chat).checked];
  side = { slot: 1, context: 30000 };                            // the Loader finished a load with the technical session on
  emit("basalt:loader");
  await wait(30);
  out.available = [view(chat), view(set)];
  tick(chat, true);
  out.saving = [view(chat).disabled, view(set).checked];
  await wait(30);
  out.ticked = [view(chat), view(set), posts(), stored];
  tick(set, false);
  await wait(30);
  out.unticked = [view(chat).checked, view(set).checked, posts(), stored];
  refuse = true;                                                 // the server says no (the session went between the check and the save)
  tick(chat, true);
  await wait(30);
  out.refused = [view(chat), view(set).checked, posts().length, calls.filter(c => c.url === "v1/status").length];
  window.OptimizePrompt.sync({ optimize_prompt: true });        // read elsewhere on the page (Settings): drawn in both
  out.synced = [view(chat).checked, view(set).checked, window.OptimizePrompt.value()];
  side = null;                                                   // the session disappears while the box is ticked
  emit("basalt:loader");
  await wait(30);
  const before = posts().length;
  out.gone = [view(chat), view(set), window.OptimizePrompt.value(), window.OptimizePrompt.available()];
  tick(chat, true);                                              // a click cannot switch it on without a session: it can only switch it off
  await wait(30);
  out.goneOff = [posts().slice(before), view(chat), view(set), window.OptimizePrompt.value(), stored];
  window.OptimizePrompt.sync({ optimize_prompt: true });        // saved as on again, then the session returns: the stored value is there
  side = { slot: 1, context: 30000 };
  emit("basalt:loader");
  await wait(30);
  out.back = [view(chat).checked, view(chat).disabled, view(chat).mixed];
  window.OptimizePrompt.sync({ optimize_prompt: false });
  offline = true;
  emit("tadamoo:key");
  await wait(30);
  out.offline = view(chat);
  global.LANG = "it";
  emit("tadamoo:lang");
  out.rebuilt = [chat.dataset.built, view(chat).title];
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
            done = subprocess.run([NODE, str(stub)], capture_output=True, text=True, timeout=60, env={**os.environ, "SCRIPT": str(WEB / "optimize.js")})
        assert done.returncode == 0, done.stderr
        cls.out = json.loads(done.stdout)

    def test_it_reads_the_saved_value_and_the_status_with_the_key_and_draws_a_real_labelled_checkbox(self):
        o = self.out
        self.assertEqual(o["first"], [["api/intake", "GET", "1"], ["v1/status", "GET", "1"]])
        self.assertEqual(o["type"][:4], ["checkbox", 0, "po-chat-in", "po.title"])                      # native: Tab reaches it, Space ticks it
        self.assertEqual(len(o["type"][4].split()), 2)                                                   # described by the tooltip text and the reason

    def test_without_a_technical_session_it_is_disabled_says_why_and_its_tooltip_says_how_to_turn_it_on(self):
        for v in self.out["noSession"]:
            self.assertEqual((v["disabled"], v["checked"], v["mixed"], v["off"]), (True, False, False, "1"))
            self.assertEqual(v["why"], "po.why.off")                                                    # a visible reason, not only a tooltip
            self.assertEqual(v["title"], "po.tip po.how")
            self.assertEqual((v["labelTitle"], v["described"]), ("po.tip po.how", "po.tip po.how"))    # on the label too, and read by a screen reader
        self.assertEqual(self.out["noSessionTick"], [[], False])

    def test_with_a_session_it_is_enabled_shows_no_reason_and_the_tooltip_is_the_plain_text(self):
        for v in self.out["available"]:
            self.assertEqual((v["disabled"], v["checked"], v["why"], v["title"], v["off"]), (False, False, "", "po.tip", ""))

    def test_ticking_in_one_place_is_shown_in_the_other_and_saved_at_once(self):
        o = self.out
        self.assertEqual(o["saving"], [True, True])                                                      # held while the save is in flight
        chat, set_, posts, stored = o["ticked"]
        self.assertEqual((chat["checked"], set_["checked"], chat["disabled"], posts, stored), (True, True, False, [{"optimize_prompt": True}], True))
        self.assertEqual((chat["status"], chat["bad"]), ("kn.saved", ""))
        self.assertEqual(o["unticked"], [False, False, [{"optimize_prompt": True}, {"optimize_prompt": False}], False])

    def test_a_refused_save_puts_the_old_value_back_says_why_and_asks_the_server_again(self):
        chat, other, posts, statuses = self.out["refused"]
        self.assertEqual((chat["checked"], other, chat["status"], chat["bad"]), (False, False, "po.refused", "1"))
        self.assertEqual(posts, 3)
        self.assertEqual(statuses, 3)                                                                    # the page load, the Loader's event, and the refusal

    def test_a_value_read_elsewhere_is_drawn_in_both(self):
        self.assertEqual(self.out["synced"], [True, True, True])

    def test_when_the_session_goes_the_stored_value_stays_and_the_box_shows_a_dash_that_can_only_be_switched_off(self):
        for v in self.out["gone"][:2]:
            self.assertEqual((v["disabled"], v["checked"], v["mixed"], v["why"], v["off"]), (False, False, True, "po.why.kept", ""))     # clickable, to switch it off
        self.assertEqual(self.out["gone"][2:], [True, False])                                           # still on in the settings, not available
        posts, chat, other, value, stored = self.out["goneOff"]
        self.assertEqual(posts, [{"optimize_prompt": False}])                                           # the click switched it off, never on
        self.assertEqual((value, stored, chat["checked"], chat["mixed"], chat["disabled"], other["checked"]), (False, False, False, False, True, False))
        self.assertEqual(chat["why"], "po.why.off")
        self.assertEqual(self.out["back"], [True, False, False])                                        # back with the session, as it was

    def test_an_unreachable_server_is_said_in_words(self):
        v = self.out["offline"]
        self.assertEqual((v["disabled"], v["why"]), (True, "po.why.offline"))
        self.assertIn("po.how", v["title"])

    def test_a_change_of_language_draws_the_box_again(self):
        self.assertEqual(self.out["rebuilt"][0], "it")

    def test_the_script_parses_and_writes_no_markup(self):
        done = subprocess.run([NODE, "--check", str(WEB / "optimize.js")], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        for banned in ("innerHTML", "insertAdjacentHTML", "document.write", "eval(", "outerHTML"):
            self.assertNotIn(banned, JS)


class WiringTest(unittest.TestCase):
    def test_both_places_are_next_to_the_prompt_analysis_switch_and_the_script_comes_after_it_and_before_intake(self):
        head = HTML[HTML.index('class="sh chathead"'):HTML.index('class="acopy"')]
        self.assertIn('<div class="po" id="po-chat" data-po></div>', head)
        self.assertLess(head.index('id="ps-chat"'), head.index('id="po-chat"'))
        self.assertLess(head.index('id="po-chat"'), head.index('id="chatmode-label"'))
        section = HTML[HTML.index('id="ps-section"'):HTML.index('id="iq-section"')]
        self.assertIn('<div class="po" id="po-set" data-po></div>', section)
        self.assertLess(section.index('id="ps-set"'), section.index('id="po-set"'))
        self.assertIn('data-t="po.tip"', section)                                                        # the same words are on the page for touch screens
        self.assertLess(HTML.index("web/promptswitch.js"), HTML.index("web/optimize.js"))
        self.assertLess(HTML.index("web/optimize.js"), HTML.index("web/intake.js"))
        self.assertEqual(len(re.findall(r'data-sub="inference"', HTML)), 3)

    def test_the_model_only_chat_hides_it_and_the_pages_keep_it_in_step(self):
        self.assertIn('$("#po-chat").hidden = !on', AGENTCHAT)
        self.assertIn("window.OptimizePrompt.sync(state)", INTAKE)
        self.assertIn("optimize_prompt: s.defaults.optimize_prompt", INTAKE)                             # the defaults button turns it off
        self.assertIn('new CustomEvent("basalt:loader")', LOADER)                                        # a finished load asks the box to look again
        for event in ('"basalt:loader"', '"tadamoo:view"', '"tadamoo:key"', '"tadamoo:lang"'):
            self.assertIn(event, JS)
        self.assertIn('"v1/status"', JS)
        self.assertIn("side_slot", JS)

    def test_the_settings_card_shows_the_session_read_only_and_follows_its_changes(self):
        self.assertIn('new CustomEvent("basalt:side"', JS)                                               # the box says when the session came or went
        self.assertIn('"basalt:side"', INTAKE)
        self.assertIn("window.OptimizePrompt.sideTokens()", INTAKE)
        self.assertIn("sideTokens: () => sideTokens", JS)
        for key in ("iq.side.title", "iq.side.on", "iq.side.off"):
            self.assertEqual(I18N.count(f'"{key}":'), 2, key)

    def test_it_is_keyboard_operable_with_visible_focus_and_30_px_controls_and_never_scrolls_sideways(self):
        self.assertIn('input.type = "checkbox"', JS)
        self.assertIn(".po .po-in:focus-visible", CSS)
        self.assertIn(".po .po-box { display: inline-flex; align-items: center; gap: 8px; min-height: var(--btn-h)", CSS)
        self.assertIn(".po { display: inline-flex; flex-wrap: wrap", CSS)
        self.assertIn("overflow-wrap: anywhere", CSS[CSS.index(".po .po-why"):])
        self.assertIn("(pointer: coarse)", CSS)
        self.assertIn(".po .po-box { min-height: 44px; }", CSS)
        self.assertNotIn("overflow-x", CSS)
        self.assertIn("--btn-h: 30px", (WEB / "app.css").read_text(encoding="utf-8"))

    def test_the_note_under_a_rewritten_message_is_collapsed_text_with_the_undo(self):
        for needle in ('case "optimizing"', 'wait: true', 'role", "status"', 'case "optimized"', 't: "opt"', 'mk("details", "atool aopt")', 'T("ch.opt.title")', 'T("ch.opt.original")', "optimize: false",
                       'T("ch.opt.rejected."', "m.opt", "textContent = b.text"):
            self.assertIn(needle, AGENTCHAT)
        self.assertNotIn("innerHTML", AGENTCHAT[AGENTCHAT.index("function optimizedNote"):AGENTCHAT.index("function optimizedNote") + 600])
        self.assertIn("userItem(typeof m.raw", AGENTCHAT)                                                 # the bubble is the person's own text


class TextsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not NODE:
            raise unittest.SkipTest("node is needed to read the translations")
        cls.s = strings()

    def test_the_tooltip_is_the_owners_sentence(self):
        self.assertEqual(self.s["en"]["po.tip"], TIP)
        self.assertEqual(self.s["en"]["po.title"], "Optimize prompt")
        self.assertEqual(self.s["it"]["po.title"], "Ottimizza il prompt")
        self.assertIn("sessione tecnica", self.s["it"]["po.tip"])
        self.assertIn("Loader > Technical session", self.s["en"]["po.how"])
        self.assertIn("Loader > Sessione tecnica", self.s["it"]["po.how"])

    def test_the_status_line_is_the_owners_wording(self):
        self.assertEqual(self.s["en"]["ch.opt.working"], "Rewriting the prompt…")
        self.assertEqual(self.s["it"]["ch.opt.working"], "Sto riscrivendo il prompt…")

    def test_every_text_exists_in_both_languages_translated_with_the_same_placeholders(self):
        keys = set(re.findall(r'data-t="(po\.[\w.]+)"', HTML)) | set(re.findall(r'\bT\(\s*"(po\.[\w.]+)"', JS)) | set(re.findall(r'\bT\(\s*"(ch\.opt\.[\w.]+)"', AGENTCHAT))
        keys |= {"po.title", "po.tip", "po.why.off", "po.why.kept", "po.why.offline", "po.how", "po.refused", "ch.opt.title", "ch.opt.tip", "ch.opt.original", "ch.opt.original.tip",
                 "ch.opt.working", "ch.opt.stopped"}
        keys |= {f"ch.opt.rejected.{r}" for r in ("lost", "ratio", "answer", "leak", "cut", "empty")}
        keys -= {"ch.opt.rejected."}
        for key in sorted(keys):
            self.assertIn(key, self.s["en"], key)
            self.assertIn(key, self.s["it"], key)
            self.assertNotEqual(self.s["en"][key], self.s["it"][key], key)
            self.assertEqual(sorted(re.findall(r"\{\d\}", self.s["en"][key])), sorted(re.findall(r"\{\d\}", self.s["it"][key])), key)


if __name__ == "__main__":
    unittest.main()
