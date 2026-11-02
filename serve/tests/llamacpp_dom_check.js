"use strict";
// Runs serve/web/llamacpp.js on a tiny stand-in for the DOM and a scripted server, and prints [{name, ok, error}] as JSON (read by
// test_llamacpp_page.py). It checks what the page asks of the server: nothing is downloaded until the button under the plan is pressed,
// the confirmation carries the plan's token, and a start sends the values of the form.
const fs = require("fs");
const path = require("path");

class Text { constructor(t) { this.text = String(t); this.nodeType = 3; } }
class El {
  constructor(tag) {
    this.nodeType = 1; this.tagName = tag.toUpperCase(); this.children = []; this.attrs = {}; this.dataset = {}; this.hidden = false; this.style = {};
    this.value = ""; this.checked = false; this.disabled = false; this.classes = new Set(); this.listeners = {};
  }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(" "); }
  set textContent(v) { this.children = [new Text(v)]; }
  get textContent() { return this.children.map(c => (c instanceof Text ? c.text : c.textContent)).join(""); }
  append(...k) { this.children.push(...k); }
  replaceChildren(...k) { this.children = k; if (this.tagName === "SELECT" && k.length) this.value = k[0].value; }   // a select shows its first option
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(n, f) { (this.listeners[n] = this.listeners[n] || []).push(f); }
  all(pred, out = []) { for (const c of this.children) if (c instanceof El) { if (pred(c)) out.push(c); c.all(pred, out); } return out; }
  querySelector(sel) { return this.all(c => c.tagName === sel.toUpperCase())[0] || null; }
  button(text) { return this.all(c => c.tagName === "BUTTON" && c.textContent === text)[0]; }
}

const panel = new El("div");
panel.hidden = false;
const listeners = {};
const doc = {
  getElementById: id => (id === "ld-llamacpp" ? panel : null),
  createElement: t => new El(t),
  createTextNode: t => new Text(t),
  addEventListener: (n, f) => { (listeners[n] = listeners[n] || []).push(f); },
  hidden: false,
};
const STR = {};
for (const m of fs.readFileSync(path.join(__dirname, "..", "web", "i18n.js"), "utf8").matchAll(/"(lc\.[\w.]+)":\s*"((?:[^"\\]|\\.)*)"/g)) {
  if (!(m[1] in STR)) STR[m[1]] = JSON.parse('"' + m[2] + '"');
}
const T = (k, ...a) => (STR[k] === undefined ? k : STR[k].replace(/\{(\d+)\}/g, (_, i) => a[i]));

const calls = [];
const status = (over) => ({ found: true, path: "C:/x/llama-server.exe", source: "saved", version: "version: 6000 (abc)", candidates: [], state: "stopped", health: { code: 0, status: "", ready: false },
  error: null, settings: { port: 8080, ctx: 8192, ngl: 99, parallel: 1, alias: "", autostart: false }, base_url: "http://127.0.0.1:8080/v1", model_name: "", context_per_slot: 8192,
  pid: null, plan: null, job: null, log_tail: "", models: [{ path: "C:/m/small.gguf", name: "small", size_bytes: 1 }], ...over });
const PLAN = { release: "b6100", name: "b6100", published: "2026-10-30T10:00:00Z", files: [{ name: "a.zip", size: 1048576, url: "https://github.com/a.zip", sha256: "" }],
  total_bytes: 1048576, destination: "C:/data/llama.cpp/b6100", installed: "version: 6000", up_to_date: false, token: "tok123", needs_confirmation: true };
globalThis.fetch = async (url, opts = {}) => {
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push({ method: opts.method || "GET", url, body });
  const ok = (data, code = 200) => ({ ok: code < 400, status: code, json: async () => data });
  if (url === "api/llamacpp") return ok(status());
  if (url === "api/llamacpp/update/check") return ok({ plan: PLAN });
  if (url === "api/llamacpp/update/apply") return ok({ job: { status: "running" } }, 202);
  if (url === "api/llamacpp/start") return ok(status({ state: "starting" }), 202);
  return ok({ error: { code: "x", message: "?" } }, 404);
};
globalThis.document = doc;
globalThis.T = T;
globalThis.authHeaders = () => ({});
globalThis.setInterval = () => 1;
globalThis.clearInterval = () => {};

const results = [];
const test = async (name, fn) => { try { await fn(); results.push({ name, ok: true }); } catch (e) { results.push({ name, ok: false, error: String(e && e.stack || e) }); } };
const assert = (c, m) => { if (!c) throw new Error(m); };
const tick = () => new Promise(r => setImmediate(r));

(async () => {
  eval(fs.readFileSync(path.join(__dirname, "..", "web", "llamacpp.js"), "utf8"));
  await tick(); await tick();
  await test("the page is drawn from the status and shows the program found", async () => {
    assert(panel.textContent.includes("C:/x/llama-server.exe"), "path missing");
    assert(panel.textContent.includes("version: 6000"), "version missing");
    assert(calls.some(c => c.url === "api/llamacpp" && c.method === "GET"), "no status request");
  });
  await test("checking for an update shows the plan and downloads nothing", async () => {
    panel.button(STR["lc.update.check"]).onclick();
    await tick(); await tick();
    const text = panel.textContent;
    assert(text.includes("a.zip") && text.includes("C:/data/llama.cpp/b6100") && text.includes("1 MB"), "plan not shown: " + text.slice(0, 200));
    assert(!calls.some(c => c.url === "api/llamacpp/update/apply"), "a download was asked before the confirmation");
    assert(calls.some(c => c.url === "api/llamacpp/update/check" && c.method === "POST"), "no check request");
  });
  await test("not now closes the plan and still downloads nothing", async () => {
    panel.button(STR["lc.plan.no"]).onclick();
    assert(!panel.textContent.includes("a.zip"), "plan still shown");
    assert(!calls.some(c => c.url === "api/llamacpp/update/apply"), "a download was asked");
    panel.button(STR["lc.update.check"]).onclick();
    await tick(); await tick();
  });
  await test("the button under the plan sends the plan's token and the confirmation", async () => {
    panel.button(STR["lc.plan.go"]).onclick();
    await tick(); await tick();
    const apply = calls.filter(c => c.url === "api/llamacpp/update/apply");
    assert(apply.length === 1, "expected one apply, got " + apply.length);
    assert(apply[0].body.token === "tok123" && apply[0].body.confirm === true, JSON.stringify(apply[0].body));
  });
  await test("start sends the values of the form and the chosen model", async () => {
    panel.button(STR["lc.start"]).onclick();
    await tick(); await tick();
    const start = calls.find(c => c.url === "api/llamacpp/start");
    assert(start, "no start request");
    assert(start.body.model === "C:/m/small.gguf" && start.body.port === 8080 && start.body.ctx === 8192 && start.body.parallel === 1 && start.body.autostart === false, JSON.stringify(start.body));
  });
  console.log(JSON.stringify(results));
})();
