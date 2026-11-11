"use strict";
// Runs serve/web/ragpick.js (the pure part and the page part on a tiny stand-in for the DOM) and prints [{name, ok, error}] as JSON (read by test_ragpick_page.py).
class Text { constructor(t) { this.text = String(t); this.nodeType = 3; } }
class El {
  constructor(tag) {
    this.nodeType = 1; this.tagName = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.classes = new Set(); this.hidden = false; this.disabled = false; this.checked = false; this.value = "";
  }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(" "); }
  set textContent(v) { this.children = [new Text(v)]; }
  get textContent() { return this.children.map(c => (c instanceof El ? c.textContent : c.text)).join(""); }
  append(...nodes) { nodes.forEach(n => this.children.push(typeof n === "string" ? new Text(n) : n)); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  fire(type) { (this.listeners[type] || []).forEach(fn => fn({})); }
  all() { return this.children.filter(c => c instanceof El).flatMap(c => [c, ...c.all()]); }
  withClass(c) { return this.all().filter(e => e.classes.has(c)); }
}
global.window = global;
global.document = { createElement: t => new El(t) };
const CATALOG = { archives: [
  { id: "forest-1", name: "Forest Management", project_name: "Work", cloud_ok: true, others_ok: "allow" },
  { id: "fires-2", name: "Forest Fires", project_name: "Work", cloud_ok: false, others_ok: "deny" },
  { id: "notes-3", name: "Chat notes", project_name: "Chat", cloud_ok: false },
], project: "x" };
global.ragUi = { catalog: async () => CATALOG };
const P = require(require("path").join(__dirname, "..", "web", "ragpick.js"));

const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };
const T = (k, ...a) => (a.length ? `${k}:${a.join("|")}` : k);
const settle = () => new Promise(r => setTimeout(r, 5));

t("rows are filtered by name or project, accents ignored, and a deleted archive that is still picked stays", () => {
  eq(P.rows(CATALOG.archives, [], "forest").map(r => r.id), ["forest-1", "fires-2"]);
  eq(P.rows(CATALOG.archives, [], "CHAT").map(r => r.id), ["notes-3"]);
  eq(P.rows(CATALOG.archives, ["gone-9", "fires-2"], "").filter(r => r.on).map(r => [r.id, r.missing]), [["fires-2", false], ["gone-9", true]]);
  eq(P.rows(CATALOG.archives, [], "").map(r => r.others), ["allow", "deny", "ask_dialog"]);
});
t("toggling keeps the order of picking and never duplicates", () => {
  eq(P.toggled(["a"], "b", true), ["a", "b"]);
  eq(P.toggled(["a", "b"], "a", true), ["a", "b"]);
  eq(P.toggled(["a", "b"], "a", false), ["b"]);
});
t("the picker lists every archive with its name, project and what it allows, as text", async () => {
  const p = P.create({ T, ids: ["fires-2"] });
  await settle();
  eq(p.el.withClass("rp-name").map(e => e.textContent), ["Forest Management", "Forest Fires", "Chat notes"]);
  eq(p.el.withClass("rp-project").map(e => e.textContent), ["Work", "Work", "Chat"]);
  eq(p.el.withClass("rp-badge").slice(0, 2).map(e => e.textContent), ["rp.cloud.on", "rp.others.allow"]);
  eq(p.el.withClass("rp-badge").slice(2, 4).map(e => e.textContent), ["rp.cloud.off", "rp.others.deny"]);
  eq(p.el.withClass("rp-count")[0].textContent, "rp.count:1");
  eq(p.el.all().filter(e => e.tagName === "input" && e.type === "checkbox").map(e => e.checked), [false, true, false]);
});
t("ticking a row tells the owner the new list, and set() redraws", async () => {
  const told = [];
  const p = P.create({ T, ids: [], onChange: ids => told.push(ids) });
  await settle();
  const boxes = () => p.el.all().filter(e => e.type === "checkbox");
  boxes()[0].checked = true; boxes()[0].fire("change");
  boxes()[2].checked = true; boxes()[2].fire("change");
  eq(told, [["forest-1"], ["forest-1", "notes-3"]]);
  p.set(["fires-2"]);
  eq(p.get(), ["fires-2"]);
  eq(boxes().map(e => e.checked), [false, true, false]);
});
t("a picker that is disabled cannot be changed, and one that cannot reach the server says so", async () => {
  const p = P.create({ T, ids: [], disabled: true });
  await settle();
  eq(p.el.all().filter(e => e.type === "checkbox").every(e => e.disabled), true);
  global.ragUi = { catalog: async () => null };
  const off = P.create({ T, ids: ["x-1"] });
  await settle();
  eq(off.el.withClass("rp-msg")[0].textContent, "rp.offline");
  eq(off.el.withClass("rp-badge").map(e => e.textContent), ["rp.missing"]);
  global.ragUi = { catalog: async () => CATALOG };
});
t("an empty catalog says there is no archive", async () => {
  global.ragUi = { catalog: async () => ({ archives: [] }) };
  const p = P.create({ T, ids: [] });
  await settle();
  eq(p.el.withClass("rp-msg")[0].textContent, "rp.none");
  global.ragUi = { catalog: async () => CATALOG };
});

(async () => {
  const out = [];
  for (const [name, fn] of cases) {
    try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message }); }
  }
  console.log(JSON.stringify(out));
})();
