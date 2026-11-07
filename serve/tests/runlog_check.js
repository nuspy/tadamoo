"use strict";
// Runs serve/web/runlog.js (the entries of a plan or a run, and the chat that shows them) on a tiny stand-in for the DOM and prints
// [{name, ok, error}] as JSON (read by test_runlog_page.py).
class Text { constructor(t) { this.text = String(t); this.nodeType = 3; } }
class El {
  constructor(tag) {
    this.nodeType = 1; this.tagName = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.classes = new Set(); this.value = ""; this.hidden = false; this.disabled = false;
    this.classList = { add: c => this.classes.add(c), remove: c => this.classes.delete(c), contains: c => this.classes.has(c), toggle: (c, on) => (on ? this.classes.add(c) : this.classes.delete(c)) };
  }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(" "); }
  set textContent(v) { this.children = [new Text(v)]; }
  get textContent() { return this.children.map(c => (c instanceof Text ? c.text : c.textContent)).join(""); }
  append(...nodes) { nodes.forEach(n => this.children.push(typeof n === "string" ? new Text(n) : n)); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  async fire(type, ev) { for (const fn of this.listeners[type] || []) await fn(ev); }
  requestSubmit() { return this.fire("submit", { preventDefault() {} }); }
  all() { return this.children.filter(c => c instanceof El).flatMap(c => [c, ...c.all()]); }
}
global.window = global;
global.document = { createElement: t => new El(t), createTextNode: t => new Text(t), addEventListener() {} };
global.T = k => k;
const RL = require(require("path").join(__dirname, "..", "web", "runlog.js"));
const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };

t("a plan's journal and changes are one conversation in time order, and an automatic change is not said twice", () => {
  const journal = [{ t: 30, kind: "output", text: "risultato: P&L 120" }, { t: 10, kind: "log", text: "checked" }];
  const history = [{ t: 20, actor: "chat", type: "mark", summary: "check done" }, { t: 25, actor: "user", type: "pause", summary: "Weekend" }, { t: 11, actor: "system", type: "auto", summary: "worked out by itself: log" }];
  eq(RL.planEntries(journal, history, a => a.toUpperCase()).map(e => [e.role, e.text]),
    [["assistant", "checked"], ["event", "CHAT: check done"], ["user", "Weekend"], ["assistant", "risultato: P&L 120"]]);
});
t("nothing, or something that is not a list, gives nothing", () => { eq(RL.planEntries(undefined, null, a => a), []); eq(RL.planEntries({}, "x", a => a), []); });
t("only the latest few are kept", () => {
  const many = Array.from({ length: 100 }, (_, i) => ({ t: i + 1, kind: "log", text: "l" + i }));
  const got = RL.planEntries(many, [], a => a);
  eq(got.length, RL.KEEP); eq(got[got.length - 1].text, "l99");
});
t("a run is its conversation when it held one and its summary when it did not", () => {
  const run = { status: "completed", summary: "all done", started: 50 };
  eq(RL.runEntries(run, []).map(e => [e.role, e.text, e.t]), [["assistant", "all done", 50]]);
  eq(RL.runEntries(run, [{ role: "user", content: "go" }, { role: "tool", content: "x" }, { role: "assistant", content: "ok" }, { role: "assistant", content: "" }]).map(e => [e.role, e.text]),
    [["user", "go"], ["assistant", "ok"]]);
});

function chat(send) {
  const host = new El("div");
  const sent = [];
  const c = RL.mount(host, { id: "x", send: send === null ? null : async text => { sent.push(text); return send ? send(text) : ""; } });
  const form = host.all().find(e => e.tagName === "form"), input = host.all().find(e => e.tagName === "textarea");
  return { host, c, form, input, sent, bubbles: () => host.all().filter(e => e.classes.has("sc-msg")), note: () => host.all().find(e => e.classes.has("sc-note")).textContent };
}
t("the bubbles show the entries with their role, and the box is hidden when nobody can be asked", () => {
  const withBox = chat();
  withBox.c.set([{ role: "assistant", kind: "output", text: "P&L 120", t: 100 }, { role: "user", kind: "", text: "ok", t: 0 }]);
  eq(withBox.bubbles().map(b => [b.classes.has("assistant"), b.classes.has("user"), b.classes.has("rl-output")]), [[true, false, true], [false, true, false]]);
  eq(withBox.form.hidden, false);
  const without = chat(null);
  without.c.set([]);
  eq(without.form.hidden, true);
});
t("a message is sent, shown as the person's own and the box is emptied", async () => {
  const x = chat();
  x.c.set([]);
  x.input.value = "  how is it going?  ";
  await x.form.requestSubmit();
  eq(x.sent, ["how is it going?"]); eq(x.input.value, ""); eq(x.bubbles().map(b => b.classes.has("user")), [true]);
});
t("a message that could not be sent keeps its text and says why", async () => {
  const x = chat(() => "That conversation is not there any more.");
  x.c.set([]);
  x.input.value = "hello";
  await x.form.requestSubmit();
  eq(x.input.value, "hello"); eq(x.note(), "That conversation is not there any more."); eq(x.bubbles().length, 0);
});
t("an empty box sends nothing", async () => {
  const x = chat();
  x.input.value = "   ";
  await x.form.requestSubmit();
  eq(x.sent, []);
});

(async () => {
  const results = [];
  for (const [name, fn] of cases) {
    try { await fn(); results.push({ name, ok: true }); } catch (e) { results.push({ name, ok: false, error: String(e.message || e) }); }
  }
  console.log(JSON.stringify(results));
})();
