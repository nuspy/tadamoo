"use strict";
// Runs serve/web/personas.js on a tiny stand-in for the DOM and a stand-in server, opens a personality and prints [{name, ok, error}] as JSON
// (read by test_personas_page.py). It checks which words the "linked knowledge archives" line shows: the archives' names from the list the
// Knowledge tab uses, the id for an archive the list does not know, and the id when that list cannot be read.
const fs = require("fs");
const path = require("path");

class Text { constructor(t) { this.text = String(t); this.parent = null; } }
class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.parent = null; this.attrs = {}; this.dataset = {}; this.hidden = false; this.classes = new Set(); this.value = ""; }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(" "); }
  set textContent(v) { this.children = [new Text(v)]; this.children[0].parent = this; }
  get textContent() { return this.children.map(c => (c instanceof Text ? c.text : c.textContent)).join(""); }
  append(...nodes) { nodes.forEach(n => { const x = typeof n === "string" ? new Text(n) : n; x.parent = this; this.children.push(x); }); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  replaceWith() { /* not needed here */ }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener() { /* no events are fired on the elements */ }
  focus() { /* the focus is not read */ }
  requestSubmit() { /* not used */ }
  get elements() { return this.children.filter(c => c instanceof El); }
  all() { return this.elements.flatMap(c => [c, ...c.all()]); }
  withClass(cls) { return this.all().filter(e => e.classes.has(cls)); }
}
const byId = {};
const root = new El("div");
["pe-root", "persona", "persona-label", "chatmode"].forEach(id => { byId[id] = new El("div"); });
byId["view-settings"] = new El("div"); byId["pe-section"] = new El("div");
const listeners = {};
const document = {
  getElementById: id => byId[id] || null, createElement: tag => new El(tag), body: { dataset: { view: "settings" } }, activeElement: null,
  addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); }, dispatchEvent: () => true,
};
byId["chatmode"].addEventListener = () => {};

const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };

/* the server: a persona that links three archives, and a Knowledge list that can be made to fail */
const server = { rag: { status: 200, body: { archives: [{ id: "recipes-a1b2c3", name: "Family recipes" }, { id: "manuals-d4e5f6", name: "Manuals" }] } }, asked: [] };
const answer = (status, body) => ({ ok: status < 400, status, json: async () => body });
const persona = knowledge => ({ persona: { slug: "ada", name: "Ada", voice: { engine: "", voice: "" }, avatar: "", personality: "", system_prompt: "", profile: "", language: "", knowledge, learn: true },
  memory: { counts: { long: 0, short: 0 }, items: [], more: 0, sections: { long: ["identity"], short: ["facts"] } }, learned: { notes: 0, conversations: 0, last: 0 } });
let linked = ["recipes-a1b2c3", "gone-000000", "manuals-d4e5f6"];
const fetch = async (url) => {
  server.asked.push(url);
  if (url === "api/personas") return answer(200, { personas: [{ slug: "ada", name: "Ada", counts: { long: 0, short: 0 } }], limits: {} });
  if (url.startsWith("api/personas/ada?")) return answer(200, persona(linked));
  if (url === "api/rag") return answer(server.rag.status, server.rag.body);
  return answer(404, {});
};
const T = (k, ...a) => (a.length ? `${k}:${a.join("|")}` : k);
const window = {};
new Function("window", "document", "fetch", "T", "LANG", "authHeaders", "localStorage", "CustomEvent", fs.readFileSync(path.join(__dirname, "..", "web", "personas.js"), "utf8"))(
  window, document, fetch, T, "en", () => ({}), { getItem: () => "", setItem: () => {}, removeItem: () => {} }, class {});

const settle = () => new Promise(r => setTimeout(r, 20));
async function openAda() {
  server.asked.length = 0;
  for (const fn of listeners["tadamoo:sub"] || []) fn({ detail: "personas" });
  await settle();
  const pick = byId["pe-root"].withClass("pe-pick")[0];
  pick.onclick();
  await settle();
  const line = byId["pe-root"].all().find(e => e.textContent.startsWith("pe.f.knowledge"));
  return line ? line.textContent : null;
}

t("the linked archives are shown by their names, in the order of the personality's list", async () => {
  eq(await openAda(), "pe.f.knowledge:Family recipes, gone-000000, Manuals");
});
t("an id the Knowledge list does not know is shown as it is", async () => {
  linked = ["gone-000000"];
  eq(await openAda(), "pe.f.knowledge:gone-000000");
});
t("when the Knowledge list cannot be read every archive keeps its id", async () => {
  linked = ["recipes-a1b2c3", "manuals-d4e5f6"];
  server.rag = { status: 403, body: {} };
  eq(await openAda(), "pe.f.knowledge:recipes-a1b2c3, manuals-d4e5f6");
});
t("a personality with no archive asks for no list and shows no line", async () => {
  linked = [];
  server.rag = { status: 200, body: { archives: [] } };
  eq(await openAda(), null);
  eq(server.asked.includes("api/rag"), false);
});
t("a name made of markup is text, not markup", async () => {
  linked = ["x-111111"];
  server.rag = { status: 200, body: { archives: [{ id: "x-111111", name: "<b>bold</b>" }] } };
  eq(await openAda(), "pe.f.knowledge:<b>bold</b>");
});

(async () => {
  const results = [];
  for (const [name, fn] of cases) {
    try { await fn(); results.push({ name, ok: true, error: "" }); } catch (e) { results.push({ name, ok: false, error: String(e && e.message || e) }); }
  }
  console.log(JSON.stringify(results));
  process.exit(0);
})();
