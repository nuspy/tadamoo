"use strict";
// Settings > Network in a real headless browser: node network_dom_check.js <url> <browser exe> <screen name> <language>. The server runs with a config file, in standby, so the page can save.
// Prints [{name, ok, error}] as JSON (read by test_network_page.py).
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE, SCREEN, LANG] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const SCREENS = { "desktop 1280x800": [1280, 800, false], "phone 360x640": [360, 640, true], "phone 390x844": [390, 844, true] };

async function main() {
  const [vw, vh, mobile] = SCREENS[SCREEN];
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "nwc-"));
  const proc = spawn(EXE, ["--headless=new", "--lang=en-US", "--disable-gpu", "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows", "--disable-features=CalculateNativeWinOcclusion",
    `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, "--window-size=1440,900", "about:blank"], { stdio: "ignore" });
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
      const r = await send("Runtime.evaluate", { expression: `(async () => { const q = (s, r) => (r || document).querySelector(s), qa = (s, r) => [...(r || document).querySelectorAll(s)]; const wait = ms => new Promise(r => setTimeout(r, ms)); ${expr} })()`, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception && r.exceptionDetails.exception.description));
      return r.result.value;
    };
    await send("Page.enable"); await send("Runtime.enable");
    const waitFor = async (expr, what, tries = 100) => { for (let i = 0; i < tries; i++) { try { if (await ev(`return (${expr});`)) return true; } catch (e) { /* not yet */ } await sleep(250); } throw new Error(`never: ${what}`); };
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message + (pageErrors.length ? " | page errors: " + pageErrors.splice(0).join(" ;; ") : "") }); } process.stderr.write(`${out[out.length - 1].ok ? "ok  " : "FAIL"} ${name} ${out[out.length - 1].error.slice(0, 600)}\n`); };
    const tag = `${SCREEN}, ${LANG}`;
    const set = (sel, value) => ev(`const e = q(${JSON.stringify(sel)}); e.value = ${JSON.stringify(value)}; e.dispatchEvent(new Event("input", { bubbles: true })); return 1;`);
    const jget = p => ev(`const r = await fetch(${JSON.stringify(p)}, { headers: authHeaders() }); return JSON.stringify(await r.json());`).then(JSON.parse);

    await send("Emulation.setDeviceMetricsOverride", { width: vw, height: vh, deviceScaleFactor: 1, mobile });
    await send("Emulation.setTouchEmulationEnabled", { enabled: mobile, maxTouchPoints: 5 });
    await send("Page.navigate", { url: URL + `?lang=${LANG}#settings` });
    await waitFor(`!!q("#sub-network") || typeof view === "function"`, "the page");
    await sleep(800);
    await ev(`const b = q("#sub-network"); if (b) b.click(); return 1;`);
    await waitFor(`!!q("#nw-host")`, "the network form");

    await check(`${tag}: the form shows what the server runs with, big enough, and does not scroll the page sideways`, async () => {
      const r = JSON.parse(await ev(`const small = qa("#nw-root button, #nw-root input, #nw-root select, #nw-root textarea").filter(e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height < 43.5; }).map(e => (e.id || e.textContent).slice(0, 20) + ":" + Math.round(e.getBoundingClientRect().height));
        return JSON.stringify({ host: q("#nw-host").value, port: q("#nw-port").value, pub: q("#nw-public").value, small, sw: document.documentElement.scrollWidth, iw: innerWidth, shown: q("#nw-section").offsetParent !== null })`));
      if (!r.shown) throw new Error("the section is not shown");
      if (r.host !== "127.0.0.1" || !(Number(r.port) > 0) || r.pub !== "") throw new Error("values " + JSON.stringify(r));
      if (mobile && r.small.length) throw new Error("controls under 44 px: " + r.small.join(", "));
      if (r.sw > r.iw + 1) throw new Error("the page scrolls sideways");
    });

    await check(`${tag}: Save writes the values to the config file, says they wait for a restart, and Prova says what the Host rule and the address do`, async () => {
      await set("#nw-host", "0.0.0.0");
      await set("#nw-public", "tadamoo.invalid");
      await set("#nw-allowed", "mypc.example.ts.net\nother.example.org");
      await ev(`qa("#nw-root button").find(b => b.textContent.trim() === ${JSON.stringify(LANG === "it" ? "Salva" : "Save")}).click(); return 1;`);
      await waitFor(`/(Saved|Salvato)/.test((q(".nw-status") || {}).textContent || "")`, "the saved message");
      const v = await jget("api/network");
      if (v.saved.host !== "0.0.0.0" || v.saved.public_name !== "tadamoo.invalid" || v.saved.allowed_hosts.join() !== "mypc.example.ts.net,other.example.org") throw new Error("saved " + JSON.stringify(v.saved));
      if (v.pending.length < 3) throw new Error("pending " + JSON.stringify(v.pending));
      await waitFor(`!q(".nw-pending").hidden`, "the notice that the values wait for a restart");
      await ev(`qa("#nw-root button").find(b => b.textContent.trim() === ${JSON.stringify(LANG === "it" ? "Prova" : "Test")}).click(); return 1;`);
      await waitFor(`qa("#nw-results .nw-res").length >= 3`, "the results of the test", 120);
      const res = JSON.parse(await ev(`return JSON.stringify(qa("#nw-results .nw-res").map(e => [e.className, e.textContent]))`));
      const joined = res.map(r => r.join(" ")).join(" | ");
      if (!/(started again|riavvio)/.test(joined)) throw new Error("the Host rule result does not say it passes after a restart: " + joined);
      if (!res.some(r => /ok/.test(r[0]))) throw new Error("no check passed: " + joined);
      if (!/tadamoo\.invalid/.test(joined)) throw new Error("the address is not named: " + joined);
    });

    await check(`${tag}: the guided set-up fills the fields for a reverse proxy and walks to the end`, async () => {
      await ev(`qa("#nw-root button").find(b => b.textContent.trim() === ${JSON.stringify(LANG === "it" ? "Imposta passo per passo" : "Set up step by step")}).click(); return 1;`);
      await waitFor(`!q(".nw-wiz").hidden`, "the guide opens");
      const next = () => ev(`qa(".nw-wiz button").find(b => /^(Next|Avanti)$/.test(b.textContent.trim())).click(); return 1;`);
      await next();                                                                                         // nothing chosen: it says so and stays
      if (!(await ev(`return /(Choose one|Scegline)/.test(q(".nw-status").textContent)`))) throw new Error("no message when nothing is chosen");
      await ev(`q("#nw-wiz-proxy").click(); return 1;`);
      await next();
      const r = JSON.parse(await ev(`return JSON.stringify({ host: q("#nw-host").value, scheme: q("#nw-scheme").value, step: q(".nw-wiz-h").textContent, text: q(".nw-wiz-body p").textContent })`));
      if (r.host !== "127.0.0.1" || r.scheme !== "https" || !/2/.test(r.step) || !/X-Forwarded-Proto/.test(r.text)) throw new Error("the guide " + JSON.stringify(r));
      await next();
      if (!/3/.test(await ev(`return q(".nw-wiz-h").textContent`))) throw new Error("it did not reach the last step");
      const small = JSON.parse(await ev(`return JSON.stringify(qa(".nw-wiz button, .nw-wiz-opt").filter(e => e.getBoundingClientRect().height < 43.5).map(e => e.textContent.trim()))`));
      if (mobile && small.length) throw new Error("guide controls under 44 px: " + small.join(", "));
    });
    if (pageErrors.length) out.push({ name: `${tag}: no script error`, ok: false, error: pageErrors.splice(0).join(" ;; ") });
  } finally {
    try { proc.kill(); } catch (e) { /* gone */ }
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* in use */ }
  }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "script", ok: false, error: String(e && e.stack || e) }])); });
