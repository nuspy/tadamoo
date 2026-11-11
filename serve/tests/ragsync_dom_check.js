"use strict";
// Runs serve/web/ragsync.js on a tiny stand-in for the DOM and a stand-in server and prints [{name, ok, error}] as JSON (read by test_rag_sync_page.py).
// It checks the block of an archive's page: what a check shows, the buttons it enables, the jobs it starts and the two presses before anything is removed.
const fs = require("fs");
const path = require("path");

class Text { constructor(t) { this.text = String(t); this.parent = null; } }
class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.parent = null; this.attrs = {}; this.dataset = {}; this.hidden = false; this.classes = new Set(); this.value = ""; this.disabled = false; this.checked = false;
    this.classList = { add: c => this.classes.add(c) };
  }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(" "); }
  set textContent(v) { this.children = [new Text(v)]; this.children[0].parent = this; }
  get textContent() { return this.children.map(c => (c instanceof Text ? c.text : c.textContent)).join(""); }
  append(...nodes) { nodes.forEach(n => { const x = typeof n === "string" ? new Text(n) : n; x.parent = this; this.children.push(x); }); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  focus() { /* not read */ }
  get elements() { return this.children.filter(c => c instanceof El); }
  all() { return this.elements.flatMap(c => [c, ...c.all()]); }
  withClass(cls) { return this.all().filter(e => e.classes.has(cls)); }
  tag(name) { return this.all().filter(e => e.tagName === name); }
  buttons(text) { return this.tag("BUTTON").filter(b => b.textContent === text); }
}
const mount = new El("div");
const listeners = {};
let current = null;
const document = {
  getElementById: id => (id === "rs-root" ? current : null), createElement: tag => new El(tag), activeElement: null, body: { dataset: { view: "rag" } },
  addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
};
const archive = { id: "wiki-1", name: "Company wiki" };
const view = () => ({
  archive: "wiki-1", running: false, unlinked: 0, history: [], attention: {}, last: {}, settings: { keep_versions: 3, keep_days: 90, min_interval_s: 1, max_download_mb: 50, contact: "", respect_robots: true },
  sources: [{ id: "s1", kind: "wikibridge", label: "company wiki: acme", config: {} }],
  links: [{ doc: "d1", name: "Handbook", source: "s1", key: "sources/handbook.md", versions: 2, pinned: false }],
  check: { at: 1700000000, summary: "1 changed, 1 gone", counts: { changed: 1, gone: 1 }, errors: [], items: [
    { id: "s1:sources/handbook.md", doc: "d1", name: "Handbook", state: "changed", detail: "v1 -> v2" },
    { id: "s1:sources/vpn.md", doc: "d2", name: "VPN guide", state: "gone", detail: "not listed" }] },
  coverage: { documents: 2, ok: 1, check: 1, incomplete: 0, unknown: 0, pages: 10, covered: 9, flagged: 0, failed: [], problems: [{ doc: "d1", name: "Handbook", status: "check", problems: ["1 page hold no passage"] }], status: "check" },
});
const asked = [], posted = [];
let jobPolls = 0;
const answer = (status, body) => ({ ok: status < 400, status, json: async () => body });
const fetch = async (url, opts) => {
  const method = (opts && opts.method) || "GET";
  asked.push(`${method} ${url}`);
  const body = JSON.parse((opts && opts.body) || "{}");
  if (method === "POST") posted.push({ url, body });
  if (url === "api/rag/archives/wiki-1/sync") return answer(200, view());
  if (url === "api/routines" && method === "GET") return answer(200, { routines: [{ id: "r1", name: "n", schedule: "daily 03:00", message: "check", deliver_to: { kind: "knowledge", ref: "wiki-1" }, last_run: 0 }] });
  if (url === "api/routines" && method === "POST") return answer(200, { routine: { id: "r2" } });
  if (url === "api/knowledge") return answer(200, { wiki_url: "http://127.0.0.1:8775", wiki_project: "acme" });
  if (method === "POST" && /archives\/wiki-1\/(check|refresh|verify)$|documents\/d1\/rollback$/.test(url)) return answer(200, { job: { id: "j1", state: "running", stage: "fetching" } });
  if (url === "api/rag/jobs/j1") { jobPolls += 1; return answer(200, { job: { id: "j1", state: "done", result: { summary: "1 updated", items: [{ id: "s1:sources/handbook.md", state: "would update", passages: { added: 2, removed: 1, kept: 5 }, examples: { added: ["new text"], removed: ["old text"] } }], ok: true, errors: 0, problems: [], archive: "wiki-1" } } }); }
  if (method === "POST") return answer(200, {});
  if (url.endsWith("/versions")) return answer(200, { versions: [{ n: 1, at: 1700000000, version: "v1", chunks: 4, pages: 0 }] });
  return answer(404, {});
};
const T = (k, ...a) => (a.length ? `${k}:${a.join("|")}` : k);
const window = {};
new Function("window", "document", "fetch", "T", "authHeaders", fs.readFileSync(path.join(__dirname, "..", "web", "ragsync.js"), "utf8"))(window, document, fetch, T, () => ({}));

const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };
const settle = () => new Promise(r => setTimeout(r, 1000));
let reloads = 0;
async function show() {
  asked.length = 0; posted.length = 0; jobPolls = 0;
  current = window.ragSync.block(archive, { reload: () => { reloads += 1; } });
  await new Promise(r => setTimeout(r, 40));
  return current;
}

t("the block shows the last check, what changed, what is gone and the coverage", async () => {
  const box = await show();
  const text = box.textContent;
  for (const want of ["rs.last", "Handbook", "rs.state.changed", "VPN guide", "rs.state.gone", "rs.cov:9|10|1|1|0", "rs.sources.title", "company wiki: acme"]) if (!text.includes(want)) throw new Error("missing " + want);
  eq(box.buttons("rs.update.all:1").length, 1);
  eq(box.buttons("rs.update.all:1")[0].disabled, false);
  eq(box.buttons("rs.check")[0].disabled, false);
});
t("a check is a job: it is started, followed, and the archive page is told to reload", async () => {
  const box = await show();
  box.buttons("rs.check")[0].onclick();
  await settle();
  eq(posted.some(p => p.url === "api/rag/archives/wiki-1/check"), true);
  eq(jobPolls >= 1 && reloads >= 1, true);
});
t("update all sends no selection; update of one sends its id", async () => {
  let box = await show();
  box.buttons("rs.update.all:1")[0].onclick();
  await settle();
  eq(posted.find(p => p.url.endsWith("/refresh")).body, {});
  box = await show();
  box.buttons("rs.update.one")[0].onclick();
  await settle();
  eq(posted.find(p => p.url.endsWith("/refresh")).body, { only: ["s1:sources/handbook.md"] });
});
t("what changed asks for a preview that changes nothing and shows the passages", async () => {
  const box = await show();
  box.buttons("rs.what")[0].onclick();
  await settle();
  eq(posted.find(p => p.url.endsWith("/refresh")).body, { only: ["s1:sources/handbook.md"], dry_run: true, force: [] });
  const text = current.textContent;
  for (const want of ["rs.diff:2|1|5", "+ new text", "- old text"]) if (!text.includes(want)) throw new Error("missing " + want);
});
t("a document that is gone needs two presses to be removed, and keeping it is one", async () => {
  const box = await show();
  box.buttons("rs.gone.remove")[0].onclick();
  await new Promise(r => setTimeout(r, 40));
  eq(posted.some(p => p.url.endsWith("/gone")), false);
  current.buttons("rs.gone.remove.sure")[0].onclick();
  await new Promise(r => setTimeout(r, 40));
  eq(posted.find(p => p.url.endsWith("/gone")), { url: "api/rag/archives/wiki-1/documents/d2/gone", body: { action: "remove" } });
  const again = await show();
  again.buttons("rs.gone.keep")[0].onclick();
  await new Promise(r => setTimeout(r, 40));
  eq(posted.find(p => p.url.endsWith("/gone")).body, { action: "keep" });
});
t("a source is added with the wiki's address and project from the settings", async () => {
  const box = await show();
  const form = box.tag("FORM").find(f => f.tag("SELECT").length);
  const kind = form.tag("SELECT")[0];
  kind.value = "wikibridge"; kind.onchange();
  await new Promise(r => setTimeout(r, 40));
  const inputs = current.tag("INPUT").filter(i => i.attrs.type === undefined && i.type === "text");
  const f = current.tag("FORM").find(x => x.tag("SELECT").length);
  const fields = f.tag("INPUT");
  eq(fields.map(i => i.value), ["http://127.0.0.1:8775", "acme"]);
  f.buttons("rs.source.add")[0].onclick();
  await new Promise(r => setTimeout(r, 40));
  eq(posted.find(p => p.url.endsWith("/sources")).body, { kind: "wikibridge", address: "http://127.0.0.1:8775", project: "acme" });
});
t("a schedule is a routine that delivers to the archive, with check or update as its message", async () => {
  const box = await show();
  const sel = box.tag("SELECT").find(s => s.elements.some(o => o.value === "update"));
  sel.value = "update";
  const row = sel.parent.parent;
  row.buttons("rs.sched.add")[0].onclick();
  await new Promise(r => setTimeout(r, 40));
  const made = posted.find(p => p.url === "api/routines");
  eq([made.body.deliver_to, made.body.message, made.body.schedule], [{ kind: "knowledge", ref: "wiki-1" }, "update", "daily 03:00"]);
  if (!current.textContent.includes("rs.sched.check:daily 03:00")) throw new Error("the routine of this archive is not listed");
});
t("the versions of a document are listed and putting one back takes two presses", async () => {
  const box = await show();
  box.buttons("rs.versions.show")[0].onclick();
  await new Promise(r => setTimeout(r, 40));
  current.buttons("rs.version.restore")[0].onclick();
  await new Promise(r => setTimeout(r, 40));
  eq(posted.some(p => p.url.endsWith("/rollback")), false);
  current.buttons("rs.version.restore.sure")[0].onclick();
  await settle();
  eq(posted.find(p => p.url.endsWith("/rollback")).body, { version: 1 });
});
t("the lines under a document say where it came from and what its reading missed", async () => {
  const lines = window.ragSync.docLines({ origin: { kind: "wikibridge", version: "version 2" }, updated: 1700000000, coverage: { status: "check", problems: ["2 pages empty"] } });
  eq(lines.length, 2);
  eq(lines[1].textContent, "rs.doc.cov:2 pages empty");
  eq(window.ragSync.docLines({}).length, 0);
});

(async () => {
  const out = [];
  for (const [name, fn] of cases) {
    try { await fn(); out.push({ name, ok: true }); } catch (e) { out.push({ name, ok: false, error: String(e && e.message || e) }); }
  }
  console.log(JSON.stringify(out));
  process.exit(0);
})();
