"use strict";
// Runs serve/web/voicearchives.js on a tiny stand-in for the DOM and prints [{name, ok, error}] as JSON (read by test_voice_archives_page.py): the checklist of
// archives (ids stored, names shown, a box that follows the settings) and the dialog of the question about letting other people perceive an archive's content.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

class Text { constructor(t) { this.text = String(t); this.nodeType = 3; } }
class El {
  constructor(tag) { this.nodeType = 1; this.tagName = tag.toUpperCase(); this.children = []; this.attrs = {}; this.dataset = {}; this.className = ""; this.checked = false; this.disabled = false; this.open = false; this.listeners = {}; }
  set textContent(v) { this.children = v === "" ? [] : [new Text(v)]; }
  get textContent() { return this.children.map(c => (c instanceof Text ? c.text : c.textContent)).join(""); }
  append(...nodes) { nodes.forEach(n => this.children.push(typeof n === "string" ? new Text(n) : n)); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  all() { return this.children.filter(c => c instanceof El).flatMap(c => [c, ...c.all()]); }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() {}
}
const body = new El("body");
const doc = { createElement: t => new El(t), createTextNode: t => new Text(t), body, visibilityState: "visible", documentElement: { lang: "en" }, listeners: {}, addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); } };
const calls = [];
const answers = { catalog: { ok: true, archives: [{ id: "boats-1", name: "Boats", project_name: "Chat" }, { id: "rice-2", name: "Kitchen", project_name: "" }] }, questions: [] };
const fetchStub = async (url, opt) => {
  calls.push([opt.method, url, opt.body ? JSON.parse(opt.body) : null]);
  const data = url === "api/rag/catalog" ? answers.catalog : url === "api/voice/consent" ? { questions: answers.questions } : {};
  return { ok: true, status: 200, json: async () => data };
};
const T = (k, ...a) => k + (a.length ? ":" + a.join(",") : "");
const ctx = { document: doc, window: {}, fetch: fetchStub, T, setTimeout: () => 0, clearTimeout: () => {}, console };
ctx.window = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "web", "voicearchives.js"), "utf8"), ctx);

const results = [];
const test = (name, fn) => fn().then(() => results.push({ name, ok: true, error: "" }), e => results.push({ name, ok: false, error: String(e && e.message || e) }));
const eq = (a, b, what) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${what}: ${JSON.stringify(a)} != ${JSON.stringify(b)}`); };
const tick = () => new Promise(r => setImmediate(r));
const boxes = node => node.all().filter(e => e.tagName === "INPUT");

(async () => {
  await tick(); await tick();
  await test("the checklist shows names with their project and stores ids", async () => {
    const saved = [];
    const p = ctx.VoiceArchives.picker({ id: "x", label: "L", help: "H", ids: ["boats-1"], onSave: ids => saved.push(ids) });
    await tick();
    eq(boxes(p).map(b => b.value), ["boats-1", "rice-2"], "boxes");
    eq(boxes(p).map(b => b.checked), [true, false], "checked");
    eq(p.textContent.includes("Boats - Chat") && p.textContent.includes("Kitchen"), true, "names");
    const second = boxes(p)[1]; second.checked = true; second.onchange();
    eq(saved, [["boats-1", "rice-2"]], "saved with the new one");
    const first = boxes(p)[0]; first.checked = false; first.onchange();
    eq(saved[1], ["rice-2"], "saved without the first");
  });
  await test("a box that follows the settings stands for null and disables the list", async () => {
    const saved = [];
    const p = ctx.VoiceArchives.picker({ id: "y", label: "L", ids: null, inherit: "follow", onSave: ids => saved.push(ids) });
    await tick();
    const [inherit, ...rest] = boxes(p);
    eq([inherit.checked, rest.map(b => b.disabled)], [true, [true, true]], "inherited");
    inherit.checked = false; inherit.onchange();
    eq(saved, [[]], "explicit none");
    eq(boxes(p).slice(1).map(b => b.disabled), [false, false], "enabled");
    boxes(p)[0].checked = true; boxes(p)[0].onchange();
    eq(saved[1], null, "back to the settings");
  });
  await test("an archive that was deleted is said, not hidden", async () => {
    const p = ctx.VoiceArchives.picker({ id: "z", label: "L", ids: ["boats-1", "gone-9"], onSave: () => {} });
    await tick();
    eq(p.textContent.includes("va.gone:1"), true, "gone");
  });
  await test("the dialog names the archive, shows the preview and sends the answer", async () => {
    answers.questions = [{ id: "q1", archive: "Boats", preview: "the keel keeps the boat upright", how: "spoken", via: "dialog", fallback: false, function: "meeting" }];
    ctx.document.dispatch = null;
    for (const f of doc.listeners["tadamoo:lang"] || []) f();
    await tick(); await tick();
    const dlg = body.all().find(e => e.tagName === "DIALOG");
    eq(dlg.open, true, "open");
    const text = dlg.textContent;
    eq(["va.q.title:Boats", "the keel keeps the boat upright", "va.q.what.spoken", "va.q.help"].every(s => text.includes(s)), true, "texts: " + text);
    const buttons = dlg.all().filter(e => e.tagName === "BUTTON");
    eq(buttons.map(b => b.textContent), ["va.q.once", "va.q.meeting", "va.q.always", "va.q.no"], "four answers");
    answers.questions = [];
    await buttons[2].onclick(); await tick();
    eq(calls.filter(c => c[1] === "api/voice/consent/answer").pop()[2], { id: "q1", answer: "always" }, "answer sent");
    eq(dlg.open, false, "closed");
  });
  await test("the dialog says when an archive that asks by voice has no private channel", async () => {
    answers.questions = [{ id: "q2", archive: "Kitchen", preview: "x", how: "written", via: "dialog", fallback: true }];
    for (const f of doc.listeners["tadamoo:lang"] || []) f();
    await tick(); await tick();
    const dlg = body.all().find(e => e.tagName === "DIALOG");
    eq(dlg.textContent.includes("va.q.fallback") && dlg.textContent.includes("va.q.what.written"), true, "fallback note");
    answers.questions = [];
  });
  console.log(JSON.stringify(results));
})();
