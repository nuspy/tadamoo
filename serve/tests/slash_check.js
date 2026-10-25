"use strict";
// Runs serve/web/slash.js against a small stand-in for the page and prints what happened as JSON (read by test_slash_commands.py).
const vm = require("vm"), fs = require("fs"), path = require("path");
const WEB = path.join(__dirname, "..", "web");

class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.hidden = false; this.value = ""; this.className = ""; this._text = ""; this.dataset = {}; this.options = []; this.selectedIndex = 0; this.clicks = 0; this.focused = false; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  get text() { return this.textContent; }
  set textContent(v) { this._text = String(v); this.children = []; }
  append(...n) { n.forEach(x => this.children.push(typeof x === "string" ? Object.assign(new El("#text"), { _text: x }) : x)); }
  replaceChildren(...n) { this.children = []; this._text = ""; this.append(...n); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  dispatchEvent(e) { (this.listeners[e.type] || []).forEach(f => f(e)); return true; }
  click() { this.clicks++; if (this.onclick) this.onclick(); }
  focus() { this.focused = true; }
  scrollIntoView() {}
}
const els = {};
const $ = sel => (els[sel] = els[sel] || new El(sel));
$("#effort").options = [["", "High (default)"], ["medium", "Medium"], ["low", "Low"], ["none", "Off"]].map(([v, t]) => Object.assign(new El("option"), { value: v, _text: t }));
$("#effort").value = "";
Object.defineProperty($("#effort"), "selectedIndex", { get() { return this.options.findIndex(o => o.value === this.value); } });
$("#chatset").hidden = true;
$("#chat-model").textContent = "Qwen3.8-Flash";

const docListeners = {};
const log = { sent: [], notes: [], server: [], perm: [], navigated: [], stopped: 0, fetches: [] };
const agentChat = {
  active: () => agentChat._active, _active: true, current: () => agentChat._sid, _sid: "s1",
  note: (plain, build) => log.notes.push({ plain, text: build ? build().textContent : plain }),
  send: t => log.sent.push(t),
  runOnServer: (n, a) => log.server.push([n, a]),
  stop: () => { log.stopped++; },
  perm: { available: () => true, now: () => "default", set: async m => { log.perm.push(m); return true; }, cycle: () => log.perm.push("cycle") },
  answers: () => ["first answer", "second answer"], sessions: () => [{ id: "a", label: "A" }], hasCopy: () => false,
};
const ctx = {
  console, Event: class { constructor(t) { this.type = t; } }, CustomEvent: class { constructor(t, o) { this.type = t; this.detail = (o || {}).detail; } },
  document: { createElement: tag => new El(tag), addEventListener: (t, f) => { (docListeners[t] = docListeners[t] || []).push(f); }, dispatchEvent: e => { (docListeners[e.type] || []).forEach(f => f(e)); }, querySelectorAll: () => [] },
  location: { hash: "", search: "?lang=en" }, URLSearchParams, localStorage: { getItem() { return null; }, setItem() {} }, setTimeout, navigator: { clipboard: { writeText: async t => { log.copied = t; } } }, Date,
  $, authHeaders: () => ({}), window: { agentChat, chatUi: { setPrompt: t => { $("#prompt").value = t; }, autosize() {} } },
  fetch: async url => { log.fetches.push(url); return { ok: true, json: async () => ({ entries: [
    { kind: "skill", id: "skill:deploy", name: "deploy", state: "approved", description: "Ship it" },
    { kind: "skill", id: "skill:draft", name: "draft", state: "pending", description: "Write drafts" },
    { kind: "skill", id: "skill:old", name: "old", state: "off", description: "" },
    { kind: "mcp", id: "mcp:x", name: "x", state: "approved", description: "" }] }) }; },
};
ctx.window.document = ctx.document;
ctx.document.documentElement = { dataset: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(WEB, "i18n.js"), "utf8") + "\nthis.T = T; this.LANG = LANG;", ctx);
vm.runInContext(fs.readFileSync(path.join(WEB, "slash.js"), "utf8"), ctx);
const slash = ctx.window.slash;
const out = {};
const notes = () => log.notes.splice(0).map(n => n.text);

(async () => {
  out.parse = [slash.parse("/effort low"), slash.parse("/help"), slash.parse("hello"), slash.parse("/usr/bin/ls"), slash.parse("/x   a  b ")];
  out.clear = [slash.run("/clear"), slash.run("/reset"), $("#new-chat").clicks];
  out.effort = [slash.run("/effort low"), $("#effort").value, slash.run("/effort high"), $("#effort").value, slash.run("/effort nonsense"), notes()];
  out.mode = [slash.run("/mode ask"), slash.run("/mode"), slash.run("/mode automatic"), log.perm.splice(0), notes()];
  out.plan = [slash.run("/plan fix the build"), log.perm.splice(0)];
  await new Promise(r => setTimeout(r, 5));
  out.planSent = log.sent.splice(0);
  out.server = ["status", "context", "compact", "tools", "hooks", "mcp", "memory zebra", "permissions"].map(c => slash.run("/" + c)).concat([log.server.splice(0)]);
  out.settingsOpened = $("#cs-open").clicks;
  out.unknown = [slash.run("/nothing at all"), notes()];
  out.ask = [slash.run("/review only src"), slash.run("/init"), slash.run("/research"), slash.run("/research why is the sky blue"), slash.run("/search tides")];
  out.sent = log.sent.splice(0).map(t => t.slice(0, 60) + " … " + t.slice(-30));
  out.notesAfterAsk = notes();
  out.copy = [slash.run("/copy"), slash.run("/copy 2")];
  await new Promise(r => setTimeout(r, 5));
  out.copied = log.copied;
  slash.run("/stop"); out.stopped = log.stopped;
  slash.run("/plans"); out.hash = ctx.location.hash;
  // skills are found when the chat view opens; a command of the same name as a built-in wins
  ctx.document.dispatchEvent({ type: "basalt:view", detail: "chat" });
  await new Promise(r => setTimeout(r, 5));
  out.candidates = slash.candidates("de").map(c => c.name);
  out.skillMenu = slash.candidates("").filter(c => c.kind === "skill").map(c => [c.name, !!c.pending]);
  out.skillRun = [slash.run("/deploy to staging"), slash.run("/draft"), log.sent.splice(0), notes()];
  out.rank = [slash.candidates("rev").map(c => c.name), slash.candidates("sec").map(c => c.name), slash.candidates("zzzz").length];
  // what the process answers is drawn as text
  const answer = (name, data, ok = true) => { ctx.document.dispatchEvent({ type: "basalt:command", detail: { name, ok, data, error: ok ? "" : "boom" } }); return notes(); };
  out.status = answer("status", { mode: "plan", effort: "low", turns: 3, tokens: 1200, window: 32768, share: 0.037, messages: 7, tools: 30, memories: 2, mcp_servers: 1, model_tokens: 9000 });
  out.compact = [answer("compact", { done: true, before: 9000, after: 3000 }), answer("compact", { done: false, reason: "busy" }), answer("compact", { done: false, reason: "short", tokens: 55 })];
  out.mcp = [answer("mcp", { servers: [] }), answer("mcp", { servers: [{ name: "a", tools: 3, error: "" }, { name: "b", tools: 0, error: "no start" }] })];
  out.memory = [answer("memory", { count: 0, query: "", items: [] }), answer("memory", { count: 1, query: "py", items: [{ id: "m", category: "project", content: "uses python" }] })];
  out.permissions = answer("permissions", { mode: "default", rules: [{ pattern: "shell_command:pytest *", action: "allow" }], denied: 2, untrusted_input: false });
  out.failed = answer("status", null, false);
  out.help = (() => { slash.run("/help"); return log.notes.splice(0)[0].text; })();
  out.lang = ctx.LANG;
  console.log(JSON.stringify(out));
})();
