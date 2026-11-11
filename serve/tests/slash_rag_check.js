"use strict";
// Runs serve/web/slash.js (with serve/web/rag.js) on stand-ins and prints [{name, ok, error}] as JSON (read by test_rag_page.py): what /rag does in the
// model-only chat (the page keeps the attached archives and asks the server to choose) and in a conversation of the agent (the server keeps them).
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
  if (url === "api/rag/choose") {
    const b = JSON.parse(opts.body);
    const hits = ARCH.filter(a => a.name.toLowerCase().includes(b.name.toLowerCase()) && a.project !== CHAT.key);
    if (b.action === "clear") return answer(200, { status: "cleared", ids: [], dropped: b.attached.length });
    if (hits.length > 1) return answer(200, { status: "ambiguous", ids: b.attached, candidates: hits.map(a => ({ id: a.id, name: a.name, project: a.project_name })) });
    if (!hits.length) return answer(200, { status: "none", ids: b.attached });
    if (b.action === "detach") return answer(200, { status: "detached", ids: b.attached.filter(i => i !== hits[0].id), archive: { id: hits[0].id, name: hits[0].name, project: "Work" } });
    return answer(200, { status: "attached", ids: [...b.attached, hits[0].id], archive: { id: hits[0].id, name: hits[0].name, project: "Work" } });
  }
  if (url === "api/rag/recall") return answer(200, { block: "<untrusted_data>x</untrusted_data>" });
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
t("model-only chat: /rag name attaches, and the next recall carries the attached id", async () => {
  model(); window.ragUi.reset();
  eq(await run("/rag forest man"), ["sl.rag.attached:Forest Management"]);
  eq(posted[0].body, { action: "attach", name: "forest man", attached: [], project: CHAT.key });
  posted.length = 0;
  await window.ragUi.recall("felling cycle");
  eq(posted[0].body.attached, ["forest-1"]);
});
t("an ambiguous name attaches nothing and names the candidates", async () => {
  eq(await run("/rag forest"), ["sl.rag.ambiguous:Forest Management (Work); Forest Fires (Work)"]);
  eq(window.ragUi.ids(), ["forest-1"]);
  eq(await run("/rag astronomy"), ["sl.rag.none:astronomy"]);
});
t("/rag lists every archive with its project and what is used here", async () => {
  const [line] = await run("/rag");
  eq(line, "sl.rag.list:Forest Management (Work, sl.rag.on); Forest Fires (Work); Chat notes (sl.rag.this)");
});
t("/rag off name drops one, /rag off drops all", async () => {
  eq(await run("/rag off forest man"), ["sl.rag.detached:Forest Management"]);
  eq(window.ragUi.ids(), []);
  await run("/rag forest man");
  eq(await run("/rag off"), ["sl.rag.cleared"]);
  eq(window.ragUi.ids(), []);
  eq(await run("/rag off"), ["sl.rag.nothing"]);
});
t("a new conversation forgets what was attached", async () => {
  await run("/rag forest man");
  window.ragUi.reset();
  eq(window.ragUi.ids(), []);
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
  eq(posted.some(p => p.url === "api/rag/choose"), false);                      // the model-only chat's route is not used here
  eq(await window.ragUi.recall("x"), "");                                         // and the page adds no passages: the process does
});

(async () => {
  const out = [];
  for (const [name, fn] of cases) {
    try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message }); }
  }
  console.log(JSON.stringify(out));
})();
