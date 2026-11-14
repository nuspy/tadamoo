"use strict";
// Addresses for a person on another device, in a real headless browser at three screen sizes (serve/web/netaddr.js, docs/remote-devices.md): node netaddr_dom_check.js <url> <browser exe>.
// The page is at a loopback address, so the test says where the person is (NetAddr.force) and renders answers through the page's own Markdown. Prints [{name, ok, error}] as JSON
// (read by test_netaddr_page.py).
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];
const BASE = "http://100.84.1.2:7232", HOST = "100.84.1.2";

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "nad-"));
  const proc = spawn(EXE, ["--headless=new", "--lang=en-US", "--disable-gpu", `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, "--window-size=1440,900", "about:blank"], { stdio: "ignore" });
  const out = [];
  try {
    let targets = [];
    for (let i = 0; i < 40 && !targets.length; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch (e) { await sleep(250); } }
    const ws = new WebSocket(targets.find(t => t.type === "page").webSocketDebuggerUrl);
    await new Promise(r => { ws.onopen = r; });
    let id = 0; const waits = new Map(), pageErrors = [];
    ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      if (m.id && waits.has(m.id)) { waits.get(m.id)(m.result || m.error); waits.delete(m.id); }
      else if (m.method === "Runtime.exceptionThrown") pageErrors.push(((m.params.exceptionDetails.exception || {}).description || m.params.exceptionDetails.text || "").slice(0, 300));
    };
    const send = (method, params = {}) => new Promise((r, rej) => { const n = ++id; const t = setTimeout(() => rej(new Error(`${method} did not answer`)), 60000); waits.set(n, v => { clearTimeout(t); r(v); }); ws.send(JSON.stringify({ id: n, method, params })); });
    const ev = async expr => {
      const r = await send("Runtime.evaluate", { expression: `(async () => { const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)]; const wait = ms => new Promise(r => setTimeout(r, ms)); ${expr} })()`, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception && r.exceptionDetails.exception.description));
      return r.result.value;
    };
    await send("Page.enable"); await send("Runtime.enable");
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message + (pageErrors.length ? " | page errors: " + pageErrors.splice(0).join(" ;; ") : "") }); } process.stderr.write(`${out[out.length - 1].ok ? "ok  " : "FAIL"} ${name} ${out[out.length - 1].error.slice(0, 600)}\n`); };
    // an answer rendered by the page's own Markdown into a box of the page, and what came out
    const render = text => ev(`const box = document.createElement("div"); box.className = "answer"; box.id = "nad-box"; (q("#amsgs") || document.body).append(box);
      window.chatUi.renderMarkdown(box, ${JSON.stringify(text)});
      const links = [...box.querySelectorAll("a")].map(a => a.getAttribute("href")), imgs = [...box.querySelectorAll("img.aimg")].map(i => ({ fid: i.dataset.fid || "", path: i.dataset.path || "", src: i.getAttribute("src") || "" }));
      const files = [...box.querySelectorAll("button.afile")].map(b => ({ kind: b.dataset.kind, id: b.dataset.id || "", path: b.dataset.path || "" })), html = box.innerHTML, code = [...box.querySelectorAll("code, pre")].map(c => c.textContent);
      const wide = document.documentElement.scrollWidth > innerWidth + 1; box.remove();
      return JSON.stringify({ links, imgs, files, html, code, wide })`).then(JSON.parse);

    const ANSWER = [
      "The dev server is at http://localhost:8000/index.html and the API at [the API](http://127.0.0.1:7232/api/status?x=1).",
      "It also says (http://0.0.0.0:5173/) and ws://[::1]:9000/live. Docs: https://example.com/guide and [LAN](http://192.168.1.9:3000/b).",
      "A picture: ![shot](http://127.0.0.1:7232/api/outputs/abcdef012345/0123456789ab/file) and one of the session: ![plot](http://localhost:7232/api/agent/sessions/s1/file?path=out%2Fplot.png&inline=1).",
      "A file: [report](http://localhost:7232/api/outputs/abcdef012345/fedcba987654/file) and a path of the disk: ![disk](C:\\proj\\out\\a.png) and [elsewhere](C:\\other\\b.png).",
      "Code stays: `fetch('http://localhost:3000/x')`",
      "```js\nfetch('http://127.0.0.1:3000/y')\n```",
    ].join("\n\n");

    for (const [vname, w, h, mobile] of VIEWPORTS) {
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      await send("Page.navigate", { url: URL + "?lang=en" });
      await sleep(1500);
      await ev(`for (let i = 0; i < 60 && !(window.NetAddr && window.chatUi && window.chatUi.renderMarkdown); i++) await wait(100); return 1;`);
      await ev(`window.__folder = "C:\\\\proj"; const c = window.agentChat; if (c) { const was = c.record; c.record = () => ({ ...((was && was()) || {}), workspace: window.__folder }); } return 1;`);

      await check(`${vname}: a page at this machine shows every address as it was written`, async () => {
        await ev(`window.NetAddr.force(null); return 1;`);
        const r = await render(ANSWER);
        if (!r.links.includes("http://localhost:8000/index.html") || !r.links.includes("http://127.0.0.1:7232/api/status?x=1")) throw new Error("a local page changed its links: " + JSON.stringify(r.links));
        if (r.imgs.length || r.html.includes(HOST)) throw new Error("a local page rewrote something: " + JSON.stringify(r.imgs));
      });

      await check(`${vname}: a page on another device turns addresses of the server's machine into the address it reaches it at, and keeps the others`, async () => {
        await ev(`window.NetAddr.force({ remote: true, host: ${JSON.stringify(HOST)}, base: ${JSON.stringify(BASE)} }); return 1;`);
        const r = await render(ANSWER);
        const has = href => r.links.includes(href);
        if (!has("http://100.84.1.2:8000/index.html")) throw new Error("the bare dev server address is not rewritten: " + JSON.stringify(r.links));
        if (!has("http://100.84.1.2:7232/api/status?x=1")) throw new Error("the API link is not rewritten: " + JSON.stringify(r.links));
        if (!r.links.some(l => l.startsWith("http://100.84.1.2:5173/")) || !r.html.includes("ws://100.84.1.2:9000/live")) throw new Error("0.0.0.0 and [::1] are not rewritten: " + JSON.stringify(r.links));
        if (!has("https://example.com/guide") || !has("http://192.168.1.9:3000/b")) throw new Error("another host was changed: " + JSON.stringify(r.links));
        if (/localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]/.test(r.links.join(" ") + r.html.replace(/fetch\([^)]*\)/g, ""))) throw new Error("a loopback address is left in the answer: " + r.html.slice(0, 400));
      });

      await check(`${vname}: a picture written as an address of this machine loads from the network address through the page's own key, and a path in the folder is the same`, async () => {
        const r = await render(ANSWER);
        const byFid = r.imgs.find(i => i.fid === "0123456789ab"), byPath = r.imgs.find(i => i.path === "out/plot.png"), byDisk = r.imgs.find(i => i.path === "out/a.png");
        if (!byFid) throw new Error("the output file route did not become tadamoo-file: " + JSON.stringify(r.imgs));
        if (!byPath) throw new Error("the session file route did not become tadamoo-path: " + JSON.stringify(r.imgs));
        if (!byDisk) throw new Error("a path of the disk inside the folder did not become tadamoo-path: " + JSON.stringify(r.imgs));
        if (r.imgs.some(i => /^(https?|file):/.test(i.src))) throw new Error("a picture is hotlinked: " + JSON.stringify(r.imgs));
        if (!r.files.some(f => f.kind === "file" && f.id === "fedcba987654")) throw new Error("the file link is not a button of the page: " + JSON.stringify(r.files));
        if (r.files.some(f => f.path && f.path.includes("b.png")) || r.imgs.some(i => i.path && i.path.includes("other"))) throw new Error("a path outside the folder was made a reference");
        // the pictures are fetched through the page's own address (a relative route with its key), never at an address of this machine
        const asked = await ev(`const urls = []; const real = window.fetch; window.fetch = (u, o) => { urls.push(String(u)); return real.call(window, u, o); };
          const box = document.createElement("div"); (q("#amsgs") || document.body).append(box); window.chatUi.renderMarkdown(box, "![x](http://localhost:7232/api/outputs/abcdef012345/0123456789ab/file)"); await wait(600); window.fetch = real; box.remove();
          return JSON.stringify(urls.filter(u => /outputs|file/.test(u)))`).then(JSON.parse);
        if (asked.some(u => /^https?:\/\/(localhost|127\.|0\.0\.0\.0)/.test(u))) throw new Error("the picture was fetched at an address of this machine: " + JSON.stringify(asked));
      });

      await check(`${vname}: code is left as it is, and a long rewritten address does not push the page sideways`, async () => {
        const r = await render(ANSWER + "\n\nA long one: http://localhost:8000/" + "very-long-path-segment/".repeat(12) + "end");
        if (!r.code.some(c => c.includes("http://localhost:3000/x")) || !r.code.some(c => c.includes("http://127.0.0.1:3000/y"))) throw new Error("code was rewritten: " + JSON.stringify(r.code));
        if (r.wide) throw new Error("the page scrolls sideways");
        await ev(`window.NetAddr.force(null); return 1;`);
      });
      if (pageErrors.length) out.push({ name: `${vname}: no script error`, ok: false, error: pageErrors.splice(0).join(" ;; ") });
    }
  } finally {
    try { proc.kill(); } catch (e) { /* gone */ }
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* in use */ }
  }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "script", ok: false, error: String(e && e.stack || e) }])); });
