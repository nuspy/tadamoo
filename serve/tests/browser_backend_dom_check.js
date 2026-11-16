"use strict";
// Settings > Wallet > Browser in a real headless browser: node browser_backend_dom_check.js <url> <browser exe> <screen name> <language>. Prints [{name, ok, error}] as JSON (read by test_browser_backend_page.py).
// The two selectors (what drives the browser; who does the vault, sign-in and payment steps): they show, they save, and they are still there after the page is reloaded.
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
    await waitFor(`!!q("#sub-wallet") || typeof view === "function"`, "the page");
    await sleep(800);
    await ev(`const b = q("#sub-wallet"); if (b) b.click(); return 1;`);
    await waitFor(`!!q("#wl-br-backend")`, "the browser section");
    const pick = (sel, value) => ev(`const e = q(${JSON.stringify(sel)}); e.value = ${JSON.stringify(value)}; e.dispatchEvent(new Event("change", { bubbles: true })); return 1;`);
    const reload = async () => {
      await send("Page.reload", {});
      await sleep(1500);
      await waitFor(`!!q("#sub-wallet") || typeof view === "function"`, "the page again");
      await ev(`const b = q("#sub-wallet"); if (b) b.click(); return 1;`);
      await waitFor(`!!q("#wl-br-backend")`, "the browser section again");
    };
    const state = () => ev(`return JSON.stringify({ backend: q("#wl-br-backend").value, sw: document.documentElement.scrollWidth, iw: innerWidth, small: qa("#wl-br-backend").filter(e => e.getBoundingClientRect().height < 44).length });`).then(JSON.parse);
    const saved = () => jget("api/wallet").then(w => w.browser);

    await check(`${tag}: the selector shows the default (agent-browser), big enough, with an explanation and no sideways scroll`, async () => {
      const s = await state();
      if (s.backend !== "agent-browser") throw new Error("default is " + JSON.stringify(s));
      if (s.small) throw new Error("the selector is under 44 px");
      if (s.sw > s.iw + 1) throw new Error(`the page scrolls sideways (${s.sw} > ${s.iw})`);
      const text = await ev(`return q("#wl-br-backend").closest(".wl-pick").textContent;`);
      if (text.length < 80) throw new Error("the choice has no explanation");
    });
    await check(`${tag}: the choice is saved and is still there after the page is reloaded, and back`, async () => {
      await pick("#wl-br-backend", "tadamoo");
      await waitFor(`q("#wl-br-backend").value === "tadamoo"`, "the choice");
      await sleep(500);
      const b = await saved();
      if (b.backend !== "tadamoo") throw new Error("saved " + JSON.stringify(b));
      await reload();
      const s = await state();
      if (s.backend !== "tadamoo") throw new Error("after reload " + JSON.stringify(s));
      await pick("#wl-br-backend", "agent-browser");
      await sleep(500);
      const again = await saved();
      if (again.backend !== "agent-browser") throw new Error("saved back " + JSON.stringify(again));
      await reload();
      if ((await state()).backend !== "agent-browser") throw new Error("after the second reload");
    });
    if (pageErrors.length) out.push({ name: `${tag}: no script error`, ok: false, error: pageErrors.splice(0).join(" ;; ") });
  } finally {
    try { proc.kill(); } catch (e) { /* gone */ }
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* in use */ }
  }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "script", ok: false, error: String(e && e.stack || e) }])); });
