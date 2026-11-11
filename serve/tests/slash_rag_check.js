"use strict";
// Runs serve/web/slash.js (with serve/web/rag.js) on stand-ins and prints [{name, ok, error}] as JSON (read by test_rag_page.py): what /rag does in the
// conversation with tools (the server keeps the attached archives) and in the chat without tools (it has none and says so).
const fs = require("fs");
const path = require("path");

class El {
  constructor() { this.children = []; this.attrs = {}; this.dataset = {}; this.classes = new Set(); this.options = [{ text: "" }]; this.selectedIndex = 0; this.hidden = false; this.value = ""; }
  addEventListener() { /* no events are fired here */ }
  setAttribute(k, v) { this.attrs[k] = v; }
  removeAttribute() { /* not read */ }
  replaceChildren() { /* not read */ }
  append() { /* not read */ }
  set className(v) { this.classes = new Set(String(v).split(/\s+/)); }
  set textContent(v) { this.text = v; }
}
const els = {};
const $ = sel => (els[sel] = els[sel] || new El());
const host = new El();
const document = { getElementById: id => (id === "rg-root" ? host : null), createElement: () => new El(), addEventListener: () => {}, body: { dataset: { view: "chat" } }, activeElement: null, querySelectorAll: () => [] };

const CHAT = { key: "aaaaaaaaaaaa", name: "Chat", path: "C:\\chat", chat: true, archives: 0 };
const ARCH = [{ id: "forest-1", name: "Forest Management", description: "", project: "bbbbbbbbbbbb", project_name: "Work" }, { id: "fires-2", name: "Forest Fires", description: "", project: "bbbbbbbbbbbb", project_name: "Work" },
  { id: "chat-3", name: "Chat notes", description: "", project: CHAT.key, project_name: "Chat" }];
const posted = [];
const answer = (status, body) => ({ ok: status < 400, status, json: async () => body });
const fetch = async (url, opts) => {
  const method = (opts && opts.method) || "GET";
  if (method === "POST") posted.push({ url, body: JSON.parse(opts.body || "{}") });
  if (url === "api/rag/projects") return answer(200, { projects: [CHAT] });
  if (url === "api/rag/catalog") return answer(200, { archives: ARCH });
  return answer(404, {});
};
const T = (k, ...a) => (a.length ? `${k}:${a.join("|")}` : k);
const notes = [];
const window = { agentChat: null };
const Event = class {};
new Function("window", "document", "fetch", "T", "authHeaders", "$", "Event", fs.readFileSync(path.join(__dirname, "..", "web", "rag.js"), "utf8"))(window, document, fetch, T, () => ({}), $, Event);
new Function("window", "document", "fetch", "T", "authHeaders", "$", "Event", "location", fs.readFileSync(path.join(__dirname, "..", "web", "slash.js"), "utf8"))(window, document, fetch, T, () => ({}), $, Event, {});

const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };
const run = async text => { notes.length = 0; posted.length = 0; window.slash.run(text); await new Promise(r => setTimeout(r, 30)); return notes.slice(); };
const model = () => { window.agentChat = { active: () => false, note: p => notes.push(p), knowledgeIds: () => [] }; };

t("/rag and /knowledge are one command that has a description", async () => {
  eq(window.slash.byName("rag") === window.slash.byName("knowledge") && !!window.slash.byName("rag"), true);
  eq(window.slash.candidates("ra").some(c => c.name === "rag"), true);
});
t("the chat without tools has no archives: /rag says so and asks nothing", async () => {
  model();
  eq(await run("/rag forest man"), ["sl.rag.notools"]);
  eq(await run("/rag"), ["sl.rag.notools"]);
  eq(await run("/rag off"), ["sl.rag.notools"]);
  eq(posted.length, 0);
});
t("in a conversation of the agent the server keeps the choice: the page only asks it", async () => {
  const asked = [];
  window.agentChat = { active: () => true, current: () => "s1", note: p => notes.push(p), knowledgeIds: () => ["fires-2"],
    knowledge: async (action, name) => { asked.push([action, name]); return { status: action === "detach" ? "detached" : "attached", archive: { id: "forest-1", name: "Forest Management", project: "Work" }, ids: ["forest-1"] }; } };
  eq(await run("/rag forest man"), ["sl.rag.attached:Forest Management"]);
  eq(await run("/rag off forest"), ["sl.rag.detached:Forest Management"]);
  eq(asked, [["attach", "forest man"], ["detach", "forest"]]);
  const [line] = await run("/rag");
  eq(line.includes("Forest Fires (Work, sl.rag.on)") && !line.includes("Forest Management (Work, sl.rag.on)"), true);
  eq(line, "sl.rag.list:Forest Management (Work); Forest Fires (Work, sl.rag.on); Chat notes (sl.rag.this)");
  eq(posted.some(p => p.url === "api/rag/choose"), false);
});

(async () => {
  const out = [];
  for (const [name, fn] of cases) {
    try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message }); }
  }
  console.log(JSON.stringify(out));
})();
