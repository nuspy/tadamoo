"use strict";
// Runs serve/web/darkwebset.js on a tiny stand-in for the DOM and a stand-in server and prints [{name, ok, error}] as JSON (read by test_rag_discover_page.py).
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
const root = new El("div");
const document = { createElement: tag => new El(tag), getElementById: id => (id === "dw-root" ? root : null), addEventListener: () => {} };
let state = { package: true, settings: { darkweb: false, search: true, read: true, write: false, send: false }, tor: { installed: false, running: false, socks: "127.0.0.1:9071" }, gpg: true };
const posted = [];
const answer = (status, body) => ({ ok: status < 400, status, json: async () => body });
const fetch = async (url, opts) => {
  const method = (opts && opts.method) || "GET";
  const body = JSON.parse((opts && opts.body) || "{}");
  if (method === "POST") posted.push({ url, body });
  if (method === "GET") return answer(200, state);
  if (url === "api/darkweb") { state = { ...state, settings: { ...state.settings, ...body } }; return answer(200, state); }
  if (url === "api/darkweb/tor/install") { state = { ...state, tor: { ...state.tor, installed: true } }; return answer(200, state); }
  if (url === "api/darkweb/tor/start") { state = { ...state, tor: { ...state.tor, running: true } }; return answer(200, state); }
  return answer(404, {});
};
const T = (k, ...a) => (a.length ? `${k}:${a.join("|")}` : k);
const window = {};
const setup = () => new Function("window", "document", "fetch", "T", "authHeaders", fs.readFileSync(path.join(__dirname, "..", "web", "darkwebset.js"), "utf8"))(window, document, fetch, T, () => ({}));
const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };
const settle = () => new Promise(r => setTimeout(r, 60));
const byId = id => root.byId(id);

t("it starts off: the switch is unchecked, searching and reading are greyed, writing is said to be off", async () => {
  setup();
  await settle();
  eq(byId("dw-on").checked, false);
  eq([byId("dw-search").disabled, byId("dw-read").disabled], [true, true]);
  if (!root.textContent.includes("dw.writeoff")) throw new Error("the page does not say writing is off");
});
t("switching it on sends the switch and frees the two boxes; a box sends its own setting", async () => {
  posted.length = 0;
  byId("dw-on").checked = true; byId("dw-on").onchange();
  await settle();
  eq(posted[0], { url: "api/darkweb", body: { darkweb: true } });
  eq([byId("dw-search").disabled, byId("dw-read").disabled], [false, false]);
  byId("dw-read").checked = false; byId("dw-read").onchange();
  await settle();
  eq(posted[1].body, { read: false });
});
t("Tor is not downloaded by one press: the second press does it, then Start and Stop appear", async () => {
  posted.length = 0;
  const bar = () => byId("dw-bar").tag("BUTTON");
  eq(bar().map(b => b.textContent), ["dw.tor.install"]);
  bar()[0].onclick();
  await settle();
  eq(posted.length, 0);
  eq(bar()[0].textContent, "dw.tor.install.again");
  bar()[0].onclick();
  await settle();
  eq(posted[0].url, "api/darkweb/tor/install");
  eq(bar().map(b => b.textContent), ["dw.tor.start", "dw.tor.stop"]);
  bar()[0].onclick();
  await settle();
  if (!root.textContent.includes("dw.tor.running")) throw new Error("the page does not say Tor is running");
});
t("without the package the page says so and offers nothing", async () => {
  state = { ...state, package: false };
  root.children = [];
  setup();
  await settle();
  if (!root.textContent.includes("dw.nopackage") || byId("dw-on")) throw new Error("the package-less page offers a switch");
});
(async () => {
  const out = [];
  for (const [name, fn] of cases) {
    try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: String(e && e.message || e) }); }
  }
  console.log(JSON.stringify(out));
})();
