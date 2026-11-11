"use strict";
// Runs serve/web/rag.js on a tiny stand-in for the DOM and a stand-in server and prints [{name, ok, error}] as JSON (read by test_rag_page.py).
// It checks the project picker of the Knowledge tab: the project it starts on (the open conversation's folder, else the Chat's own), the archives
// it lists for the picked project, the project a new archive is made in, and the passages the model-only chat asks for.
const fs = require("fs");
const path = require("path");

class Text { constructor(t) { this.text = String(t); this.parent = null; } }
class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.parent = null; this.attrs = {}; this.dataset = {}; this.hidden = false; this.classes = new Set(); this.value = "";
    this.classList = { add: c => this.classes.add(c) };
  }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(" "); }
  set textContent(v) { this.children = [new Text(v)]; this.children[0].parent = this; }
  get textContent() { return this.children.map(c => (c instanceof Text ? c.text : c.textContent)).join(""); }
  append(...nodes) { nodes.forEach(n => { const x = typeof n === "string" ? new Text(n) : n; x.parent = this; this.children.push(x); }); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  focus() { /* the focus is not read */ }
  get elements() { return this.children.filter(c => c instanceof El); }
  all() { return this.elements.flatMap(c => [c, ...c.all()]); }
  withClass(cls) { return this.all().filter(e => e.classes.has(cls)); }
  tag(name) { return this.all().filter(e => e.tagName === name); }
}
const host = new El("div");
const listeners = {};
const document = {
  getElementById: id => (id === "rg-root" ? host : null), createElement: tag => new El(tag), body: { dataset: { view: "rag" } }, activeElement: null,
  addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
};

const CHAT = { key: "aaaaaaaaaaaa", name: "Chat", path: "C:\\Users\\me\\AppData\\chat-workspace", chat: true, archives: 1 };
const WORK = { key: "bbbbbbbbbbbb", name: "Work", path: "D:\\Projects\\Work", chat: false, archives: 2 };
const archives = { [CHAT.key]: [{ id: "chat-1", name: "Chat notes", documents: 1 }], [WORK.key]: [{ id: "w-1", name: "Specs", documents: 3 }, { id: "w-2", name: "Manuals", documents: 2 }] };
const asked = [], posted = [];
const answer = (status, body) => ({ ok: status < 400, status, json: async () => body });
const fetch = async (url, opts) => {
  const method = (opts && opts.method) || "GET";
  asked.push(`${method} ${url}`);
  if (method === "POST") posted.push({ url, body: JSON.parse(opts.body || "{}") });
  if (url === "api/rag/projects") return answer(200, { projects: [CHAT, WORK] });
  if (url.startsWith("api/rag?project=")) {
    const key = decodeURIComponent(url.split("=")[1]);
    return answer(200, { project: key, archives: archives[key] || [], limits: { name: 60, description: 500, document_bytes: 25 << 20 }, formats: [".txt"], pdf: true, embedding: { available: false, model: "" }, extraction: "basic", settings: { docling_python: "", flagged: "keep" }, docling_python: "", jobs: [] });
  }
  if (url === "api/rag/archives") return answer(200, { archive: { id: "new-1" } });
  if (url === "api/rag/recall") return answer(200, { block: "<untrusted_data>passage</untrusted_data>" });
  if (url.startsWith("api/rag/archives/")) return answer(404, {});
  return answer(404, {});
};
const T = (k, ...a) => (a.length ? `${k}:${a.join("|")}` : k);
const window = { agentChat: null };
new Function("window", "document", "fetch", "T", "authHeaders", fs.readFileSync(path.join(__dirname, "..", "web", "rag.js"), "utf8"))(window, document, fetch, T, () => ({}));

const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };
const settle = () => new Promise(r => setTimeout(r, 20));
async function open() {
  asked.length = 0; posted.length = 0;
  for (const fn of listeners["tadamoo:view"] || []) fn({ detail: "rag" });
  await settle();
}
const picker = () => host.tag("SELECT")[0];
const names = () => host.withClass("rg-pname").map(e => e.textContent);

t("without a conversation the tab starts on the Chat's own folder and lists only its archives", async () => {
  await open();
  eq(picker().value, CHAT.key);
  eq(picker().elements.map(o => o.value), [CHAT.key, WORK.key]);
  eq(picker().elements.map(o => o.textContent), ["rg.proj.opt:rg.proj.chat|1", "rg.proj.opt:Work|2"]);
  eq(names(), ["Chat notes"]);
  eq(asked.includes("GET api/rag/projects") && asked.includes(`GET api/rag?project=${CHAT.key}`) && !asked.includes(`GET api/rag?project=${WORK.key}`), true);
});
t("with a conversation open the tab starts on its folder, however the path is spelt", async () => {
  window.agentChat = { record: () => ({ workspace: "d:/projects/work/" }) };
  await open();
  eq(picker().value, WORK.key);
  eq(names(), ["Specs", "Manuals"]);
});
t("choosing another project asks for that project's archives and keeps the choice", async () => {
  window.agentChat = null;
  await open();
  asked.length = 0;
  picker().value = WORK.key;
  picker().onchange();
  await settle();
  eq(names(), ["Specs", "Manuals"]);
  eq(asked.includes(`GET api/rag?project=${WORK.key}`), true);
  window.agentChat = { record: () => ({ workspace: "C:\\Users\\me\\AppData\\chat-workspace" }) };
  await open();
  eq(picker().value, WORK.key);                                                  // the person's choice is not undone by the conversation
});
t("a new archive is made in the picked project", async () => {
  const make = host.withClass("rg-newbtn")[0];
  make.onclick();
  const form = host.tag("FORM")[0];
  form.tag("INPUT")[0].value = "Fresh";
  form.onsubmit({ preventDefault() {} });
  await settle();
  const call = posted.find(p => p.url === "api/rag/archives");
  eq(call.body, { name: "Fresh", description: "", project: WORK.key });
});
t("nothing about personalities is on the page", async () => {
  eq(host.all().filter(e => /personalit|rg\.use/i.test(e.textContent)).length, 0);
  eq(host.tag("INPUT").filter(e => e.type === "checkbox").length <= 1, true);   // only the cloud tick of an open archive, never a list of personalities
});
t("the model-only chat asks for the Chat folder's passages, and the agent's conversation does not", async () => {
  window.agentChat = null;
  asked.length = 0; posted.length = 0;
  eq(await window.ragUi.recall("what keeps the boat upright"), "<untrusted_data>passage</untrusted_data>");
  eq(posted.map(p => p.body), [{ project: CHAT.key, goal: "what keeps the boat upright", attached: [] }]);
  window.agentChat = { active: () => true };
  posted.length = 0;
  eq(await window.ragUi.recall("anything"), "");
  eq(posted.length, 0);
});

(async () => {
  const out = [];
  for (const [name, fn] of cases) {
    try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message }); }
  }
  console.log(JSON.stringify(out));
})();
