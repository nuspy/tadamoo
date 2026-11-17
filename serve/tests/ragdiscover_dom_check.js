"use strict";
// Runs serve/web/ragdiscover.js on a tiny stand-in for the DOM and a stand-in server and prints [{name, ok, error}] as JSON (read by test_rag_discover_page.py).
// It checks the block "add from a request in words": the search is a job, the list has a check box for each result (the useful ones checked), the search is widened,
// what is checked is added with its depth, and cancelling clears the list.
const fs = require("fs");
const path = require("path");

class Text { constructor(t) { this.text = String(t); this.parent = null; } }
class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.parent = null; this.attrs = {}; this.classes = new Set(); this.value = ""; this.disabled = false; this.checked = false; this.isConnected = true;
  }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(" "); }
  set textContent(v) { this.children = [new Text(v)]; this.children[0].parent = this; }
  get textContent() { return this.children.map(c => (c instanceof Text ? c.text : c.textContent)).join(""); }
  append(...nodes) { nodes.forEach(n => { const x = typeof n === "string" ? new Text(n) : n; x.parent = this; this.children.push(x); }); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  get elements() { return this.children.filter(c => c instanceof El); }
  all() { return this.elements.flatMap(c => [c, ...c.all()]); }
  tag(name) { return this.all().filter(e => e.tagName === name); }
  byId(id) { return this.all().find(e => e.id === id) || null; }
  buttons(text) { return this.tag("BUTTON").filter(b => b.textContent === text); }
}
const document = { createElement: tag => new El(tag) };
const archive = { id: "wind-1", name: "Wind" };
const ITEMS = [
  { id: 1, title: "Wind turbine maintenance guide", url: "https://a.example/guide", snippet: "How to maintain", via: "", relevant: true, reason: "matches" },
  { id: 2, title: "Buy turbines online", url: "https://shop.example/x", snippet: "", via: "", relevant: false, reason: "a shop" },
  { id: 3, title: "Wind notes", url: "https://c.example/n", snippet: "", via: "", relevant: null, reason: "" },
];
const NEW = [{ id: 4, title: "Blade inspection", url: "https://a.example/blades", snippet: "", via: "Wind turbine maintenance guide", relevant: true, reason: "matches" }];
const posted = [];
let results = null, nothingNew = false;
const answer = (status, body) => ({ ok: status < 400, status, json: async () => body });
const fetch = async (url, opts) => {
  const method = (opts && opts.method) || "GET";
  const body = JSON.parse((opts && opts.body) || "{}");
  if (method === "POST") posted.push({ url, body });
  if (url.endsWith("/discover") && method === "POST") { results = { query: body.query, items: ITEMS }; return answer(200, { job: { id: "j1", state: "running", stage: "searching the web" } }); }
  if (url.endsWith("/discover/more")) { results = { items: nothingNew ? [] : NEW }; return answer(200, { job: { id: "j2", state: "running" } }); }
  if (url.endsWith("/discover/import")) { results = {}; return answer(200, { added: body.urls.length, skipped: [], job: { id: "j3", state: "done", result: {} } }); }
  if (/api\/rag\/jobs\/j[12]$/.test(url)) return answer(200, { job: { id: url.slice(-2), state: "done", result: results } });
  if (method === "POST") return answer(200, {});
  return answer(404, {});
};
const T = (k, ...a) => (a.length ? `${k}:${a.join("|")}` : k);
const window = {};
new Function("window", "document", "fetch", "T", "authHeaders", fs.readFileSync(path.join(__dirname, "..", "web", "ragdiscover.js"), "utf8"))(window, document, fetch, T, () => ({}));

const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };
const settle = () => new Promise(r => setTimeout(r, 1700));
let reloads = 0, box = null;
const show = () => { posted.length = 0; box = window.ragDiscover.block(archive, { reload: async () => { reloads += 1; } }); return box; };
const typeQuery = text => { const q = box.byId("rd-query"); q.value = text; q.oninput(); };

t("an empty request is refused and nothing is sent", async () => {
  show();
  box.byId("rd-search").onclick();
  await new Promise(r => setTimeout(r, 40));
  eq(posted.length, 0);
  if (!box.textContent.includes("rdc.empty")) throw new Error("the page did not say to write something");
});
t("a search is a job; the list shows every result, the useful and the unjudged checked and the off-topic one not", async () => {
  show();
  typeQuery("wind turbine maintenance");
  box.byId("rd-search").onclick();
  await settle();
  eq(posted[0], { url: "api/rag/archives/wind-1/discover", body: { query: "wind turbine maintenance", limit: 10 } });
  eq([1, 2, 3].map(n => box.byId("rd-c-" + n).checked), [true, false, true]);
  for (const want of ["Wind turbine maintenance guide", "https://shop.example/x", "rdc.yes", "rdc.no", "rdc.unjudged", "a shop"]) if (!box.textContent.includes(want)) throw new Error("missing " + want);
  eq(box.byId("rd-addsel").textContent, "rdc.addsel:2");
  eq(box.byId("rd-addall").textContent, "rdc.addall:3");
});
t("checking and unchecking changes what Add selected will send", async () => {
  box.byId("rd-c-2").checked = true; box.byId("rd-c-2").onchange();
  box.byId("rd-c-1").checked = false; box.byId("rd-c-1").onchange();
  eq(box.byId("rd-addsel").textContent, "rdc.addsel:2");
  posted.length = 0;
  box.byId("rd-depth").value = "2"; box.byId("rd-depth").oninput();
  box.byId("rd-addsel").onclick();
  await settle();
  eq(posted[0], { url: "api/rag/archives/wind-1/discover/import", body: { urls: ["https://shop.example/x", "https://c.example/n"], depth: 2 } });
  eq(reloads >= 1, true);
  if (!box.textContent.includes("rdc.added:2")) throw new Error("the page did not say how many were added");
  eq(box.byId("rd-addsel"), null);                                                       // the list is gone once added
});
t("widening sends the results found and puts only the new ones after them, checked", async () => {
  show();
  typeQuery("wind");
  box.byId("rd-search").onclick();
  await settle();
  posted.length = 0;
  box.byId("rd-widen").onclick();
  await settle();
  eq(posted[0].url, "api/rag/archives/wind-1/discover/more");
  eq(posted[0].body.items.length, 3);
  eq(box.byId("rd-c-4").checked, true);
  eq(box.byId("rd-addall").textContent, "rdc.addall:4");
});
t("widening that finds nothing new says so and keeps the list", async () => {
  nothingNew = true;
  box.byId("rd-widen").onclick();
  await settle();
  nothingNew = false;
  if (!box.textContent.includes("rdc.nothingnew")) throw new Error("the page did not say nothing new was found");
  eq(box.byId("rd-addall").textContent, "rdc.addall:4");
});
t("Add all sends every address; Cancel clears the list without sending anything", async () => {
  posted.length = 0;
  box.byId("rd-addall").onclick();
  await settle();
  eq(posted[0].body.urls.length, 4);
  show();
  typeQuery("wind");
  box.byId("rd-search").onclick();
  await settle();
  posted.length = 0;
  box.byId("rd-cancel").onclick();
  await new Promise(r => setTimeout(r, 40));
  eq(posted.length, 0);
  eq(box.byId("rd-addsel"), null);
});
t("with nothing checked Add selected is off, and the page's buttons are real buttons with text", async () => {
  show();
  typeQuery("wind");
  box.byId("rd-search").onclick();
  await settle();
  [1, 3].forEach(n => { box.byId("rd-c-" + n).checked = false; box.byId("rd-c-" + n).onchange(); });
  eq(box.byId("rd-addsel").disabled, true);
  eq(box.tag("BUTTON").every(b => b.attrs.type === undefined ? b.type === "button" : true), true);
});

(async () => {
  const out = [];
  for (const [name, fn] of cases) {
    try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: String(e && e.message || e) }); }
  }
  console.log(JSON.stringify(out));
})();
