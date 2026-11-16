"use strict";
// Settings > Wallet > Computer control in a real headless browser: node computer_dom_check.js <url> <browser exe> <screen name> <language>. Prints [{name, ok, error}] as JSON (read by test_computer_page.py).
// Nothing is switched on: only what the section shows, its size and the page staying whole are looked at (the saving is tested over the API).
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
    await waitFor(`!!q("#wl-cc-on")`, "the computer control section");

    await check(`${tag}: the section shows the switch and the screens, with targets big enough, and does not scroll the page sideways`, async () => {
      const r = JSON.parse(await ev(`const sec = q("#wl-cc-on").closest("section"); const small = qa("input, select, button", sec).filter(e => { const b = e.getBoundingClientRect(); const l = e.closest("label"); const r = l ? l.getBoundingClientRect() : b; return b.width > 0 && Math.max(b.height, r.height) < 44; }).map(e => e.id || e.tagName);
        return JSON.stringify({ title: q("b", sec).textContent, on: q("#wl-cc-on").checked, options: qa("#wl-cc-screen option").map(o => o.textContent), small, sw: document.documentElement.scrollWidth, iw: innerWidth, disabled: q("#wl-cc-on").disabled });`));
      const title = LANG === "it" ? "Controllo del computer" : "Computer control";
      if (r.title !== title) throw new Error("the title is " + r.title);
      if (!r.on) throw new Error("the switch is off by default");
      if (!r.options.length) throw new Error("no screen to choose");
      if (!r.disabled && r.options[0] !== (LANG === "it" ? "Tutti gli schermi" : "All screens")) throw new Error("the first screen choice is " + r.options[0]);
      if (r.small.length) throw new Error("targets under 44 px: " + r.small.join(", "));
      if (r.sw > r.iw + 1) throw new Error(`the page scrolls sideways (${r.sw} > ${r.iw})`);
    });
    await check(`${tag}: it can be turned off, and turning it on again asks first and, said no, stays off`, async () => {
      await ev(`q("#wl-cc-on").click(); return 1;`);                                       // on by default: turning it off needs no question
      await waitFor(`q("#wl-cc-on").checked === false`, "the switch to go off");
      await sleep(500);
      if ((await jget("api/computer")).enabled) throw new Error("it was not saved as off");
      await ev(`q("#wl-cc-on").click(); return 1;`);
      await waitFor(`!!q("dialog.risk-dialog")`, "the question");
      await ev(`q("dialog.risk-dialog .btn.ghost").click(); return 1;`);
      await waitFor(`!q("dialog.risk-dialog")`, "the question to close");
      if (await ev(`return q("#wl-cc-on").checked;`)) throw new Error("the switch stayed on after No");
      if ((await jget("api/computer")).enabled) throw new Error("it was saved after No");
    });
    if (pageErrors.length) out.push({ name: `${tag}: no script error`, ok: false, error: pageErrors.splice(0).join(" ;; ") });
  } finally {
    try { proc.kill(); } catch (e) { /* gone */ }
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* in use */ }
  }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "script", ok: false, error: String(e && e.stack || e) }])); });
