"use strict";
// Runs the training video part of serve/web/voicetrain.js on a tiny stand-in for the DOM, with a fake XMLHttpRequest and a fake voice proxy, and prints [{name, ok, error}]
// as JSON (read by test_voice_avatar_train_page.py). It drives the section the way a person does: pick a file, watch the progress, see the length, remove it. No server,
// no browser, no video: the stand-in covers only what the script uses.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const WEB = path.join(__dirname, "..", "web");

class Text { constructor(t) { this.text = String(t); this.parent = null; } }
class El {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.parent = null; this.attrs = {}; this.dataset = {}; this.hidden = false; this.className = ""; this.id = ""; this.disabled = false; }
  set textContent(v) { this.children = [new Text(v)]; this.children[0].parent = this; }
  get textContent() { return this.children.map(c => (c instanceof Text ? c.text : c.textContent)).join(""); }
  get firstChild() { return this.children[0] || null; }
  append(...nodes) { nodes.forEach(n => { const x = typeof n === "string" ? new Text(n) : n; if (x.parent) x.parent.children = x.parent.children.filter(c => c !== x); x.parent = this; this.children.push(x); }); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener() {}
  all() { return this.children.filter(c => c instanceof El).flatMap(c => [c, ...c.all()]); }
  byId(id) { return this.all().find(e => e.id === id) || null; }
}
const body = new El("body");
const root = new El("div");

const EN = {};
for (const m of fs.readFileSync(path.join(WEB, "i18n.js"), "utf8").split("\n  it: {")[0].matchAll(/"(vtr\.[\w.]+)": ("(?:[^"\\]|\\.)*")/g)) EN[m[1]] = JSON.parse(m[2]);
const T = (k, ...a) => (EN[k] === undefined ? "MISSING:" + k : EN[k]).replace(/\{(\d+)\}/g, (_, i) => String(a[+i] ?? ""));

const server = { video: null, avatars: [{ name: "ada", display_name: "Ada", clips: [] }], calls: [], status: { state: "idle" }, failUpload: null };
const xhrs = [];
class FakeXHR {
  constructor() { this.headers = {}; this.upload = {}; xhrs.push(this); }
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader(k, v) { this.headers[k] = v; }
  send(form) { this.form = form; }
  abort() { this.aborted = true; this.onabort && this.onabort(); }
}
class FakeForm { constructor() { this.fields = []; } append(k, v) { this.fields.push([k, v]); } }

const ctx = {
  svc: async (method, p, b) => {
    server.calls.push([method, p, b]);
    if (p === "/avatar/list") return { ok: true, status: 200, data: { avatars: server.avatars } };
    if (p === "/avatar/train/status") return { ok: true, status: 200, data: server.status };
    if (p === "/avatar/ada/training-video" && method === "GET") return { ok: true, status: 200, data: { video: server.video, limits: { max_bytes: 2000 << 20, extensions: [".mp4"], seconds: [120, 600] } } };
    if (p === "/avatar/ada/training-video" && method === "DELETE") { server.video = null; return { ok: true, status: 200, data: { ok: true } }; }
    return { ok: false, status: 404, data: {} };
  },
  api: async () => ({ ok: true, status: 200, data: {} }),
};

const sandbox = {
  document: { createElement: t => new El(t), createTextNode: t => new Text(t), body, addEventListener() {}, getElementById: id => body.byId(id) || root.byId(id) },
  window: {}, T, LANG: "en", authHeaders: () => ({ Authorization: "Bearer k" }), XMLHttpRequest: FakeXHR, FormData: FakeForm, Date, JSON, Math, Object, Array, Number, String, Promise,
  setTimeout, console,
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(WEB, "voicetrain.js"), "utf8"), sandbox, { filename: "voicetrain.js" });
const V = sandbox.window.VoiceTrain;
const tick = () => new Promise(r => setImmediate(r));
const settle = async () => { for (let i = 0; i < 6; i++) await tick(); };
const text = () => root.textContent;

const results = [];
async function run(name, fn) {
  try { await fn(); results.push({ name, ok: true, error: "" }); } catch (e) { results.push({ name, ok: false, error: String(e && e.message || e) }); }
}
const need = (cond, what) => { if (!cond) throw new Error(what); };

(async () => {
  await run("an avatar with no clip is listed and its training video is read from the proxy", async () => {
    await V.paint(root, ctx);
    await settle();
    need(root.byId("vtr-tv-file"), "no file input: " + text().slice(0, 300));
    need(server.calls.some(c => c[0] === "GET" && c[1] === "/avatar/ada/training-video"), "the video was not asked for");
    need(text().includes(EN["vtr.tv.none"]), "the none line is missing");
    need(!text().includes("MISSING:"), "a string key is missing: " + text());
  });
  await run("the control says it takes a video of 3 to 5 minutes and that the person must have agreed", async () => {
    need(text().includes("Add a training video (3 to 5 minutes)"), "the add label");
    need(text().includes("agreed"), "the consent text");
    need(text().includes("2000 MB"), "the size cap is not shown");
    const input = root.byId("vtr-tv-file");
    need(input.type === "file" && input.accept.includes(".mkv") && input.accept.includes("video/webm") && !input.disabled, "the picker");
  });
  await run("choosing a file sends it as the multipart field file to the training-video route with the key, and shows progress", async () => {
    root.byId("vtr-tv-file").onchange.call(null);
    need(xhrs.length === 0, "nothing is sent without a file");
    const input = root.byId("vtr-tv-file");
    input.files = [{ name: "me.mp4" }];
    input.onchange();
    need(xhrs.length === 1, "no upload started");
    const x = xhrs[0];
    need(x.method === "POST" && x.url === "api/voice/avatar/ada/training-video", "url: " + x.url);
    need(x.headers.Authorization === "Bearer k", "the key is not sent");
    need(x.form.fields.length === 1 && x.form.fields[0][0] === "file" && x.form.fields[0][1].name === "me.mp4", "the field");
    need(root.byId("vtr-tv-file").disabled, "the picker is not disabled while sending");
    x.upload.onprogress({ lengthComputable: true, loaded: 40, total: 100 });
    need(root.byId("vtr-tv-bar").value === 40 && root.byId("vtr-tv-line").textContent === "Sending 40%", "the progress is not shown");
    need(root.byId("vtr-tv-cancel"), "no cancel button");
  });
  await run("when the service answers the measured length appears and the picker offers a replacement", async () => {
    const x = xhrs[0];
    server.video = { file: "video.mp4", seconds: 252, fps: 25, width: 1280, height: 720, bytes: 210 << 20, in_range: true };
    x.status = 200; x.responseText = JSON.stringify({ ok: true, seconds: 252, in_range: true });
    x.onload();
    await settle();
    need(text().includes("Training video: 4 min long, 25 frames per second, 1280 x 720 pixels, 210 MB."), "the current line: " + text());
    need(text().includes("Training video added: 4 min long."), "the done line");
    need(text().includes("Replace the training video (3 to 5 minutes)"), "the replace label");
    need(!root.byId("vtr-tv-bar") && !root.byId("vtr-tv-file").disabled, "the progress stays or the picker is still disabled");
    need(root.byId("vtr-tv-remove"), "no remove button");
  });
  await run("a length outside 2 to 10 minutes is said to be refused by a plan", async () => {
    server.video = { ...server.video, seconds: 60, in_range: false };
    await V.paint(root, ctx);
    root.byId("vtr-tv-file").files = [{ name: "b.mp4" }];
    root.byId("vtr-tv-file").onchange();
    const x = xhrs[1];
    x.status = 200; x.responseText = JSON.stringify({ ok: true, seconds: 60, in_range: false });
    x.onload();
    await settle();
    need(text().includes(EN["vtr.tv.range"]), "the range warning: " + text());
  });
  await run("an error of the service is shown as text and the old video stays listed", async () => {
    root.byId("vtr-tv-file").files = [{ name: "c.exe" }];
    root.byId("vtr-tv-file").onchange();
    const x = xhrs[2];
    x.status = 415; x.responseText = JSON.stringify({ error: "The training video is an MP4, MOV, WebM or MKV file.", code: "bad_type" });
    x.onload();
    await settle();
    need(text().includes("The training video is an MP4, MOV, WebM or MKV file."), "the message: " + text());
    need(root.byId("vtr-tv-remove"), "the old video is not listed");
  });
  await run("cancelling an upload aborts it and says so", async () => {
    root.byId("vtr-tv-file").files = [{ name: "d.mp4" }];
    root.byId("vtr-tv-file").onchange();
    const x = xhrs[3];
    root.byId("vtr-tv-cancel").onclick();
    need(x.aborted === true && text().includes("Upload cancelled."), "abort: " + text());
    need(!root.byId("vtr-tv-bar"), "the progress stays");
  });
  await run("removal asks first, then deletes through the proxy and the section goes back to none", async () => {
    root.byId("vtr-tv-remove").onclick();
    need(text().includes(EN["vtr.tv.sure"]) && root.byId("vtr-tv-yes") && root.byId("vtr-tv-no"), "no question");
    need(!server.calls.some(c => c[0] === "DELETE"), "deleted without asking");
    root.byId("vtr-tv-no").onclick();
    need(!root.byId("vtr-tv-yes") && root.byId("vtr-tv-remove"), "keep did not go back");
    root.byId("vtr-tv-remove").onclick();
    await root.byId("vtr-tv-yes").onclick();
    await settle();
    need(server.calls.filter(c => c[0] === "DELETE" && c[1] === "/avatar/ada/training-video").length === 1, "the delete call");
    need(text().includes(EN["vtr.tv.none"]) && text().includes("Training video removed."), "after: " + text());
  });
  await run("while this avatar is being trained the video cannot be changed", async () => {
    server.video = { file: "video.mp4", seconds: 252, fps: 25, width: 1280, height: 720, bytes: 100, in_range: true };
    root.byId("vtr-tv-file").files = [{ name: "e.mp4" }];
    root.byId("vtr-tv-file").onchange();
    xhrs[4].status = 200; xhrs[4].responseText = JSON.stringify({ ok: true, seconds: 252, in_range: true });
    xhrs[4].onload();
    await settle();
    need(root.byId("vtr-tv-remove") && !root.byId("vtr-tv-remove").disabled, "not listed before the training");
    server.status = { state: "running", avatar: "ada", engine: "feathertalk", phase: "train", percent: 30, epoch: 1, epochs: 5, log: [] };
    await V.paint(root, ctx);
    await settle();
    need(root.byId("vtr-tv-file").disabled && root.byId("vtr-tv-remove").disabled && text().includes(EN["vtr.tv.busy"]), "not locked: " + text().slice(0, 400));
  });
  console.log(JSON.stringify(results));
})();
