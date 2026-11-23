"use strict";
// Opens Models > Providers in a real headless browser at three screen sizes and checks the row "Requests from outside through the OpenAI and Anthropic interfaces":
// node providers_row_dom_check.js <url> <browser exe>. Prints [{name, ok, error}] as JSON (read by test_providers_external_row.py).
const { spawn } = require("child_process"), fs = require("fs"), os = require("os");
const [URL, EXE] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(os.tmpdir() + "/prc-");
  const proc = spawn(EXE, ["--headless=new", "--lang=en-US", "--disable-gpu", `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, "--window-size=1440,900", "about:blank"], { stdio: "ignore" });
  const out = [];
  try {
    let targets = [];
    for (let i = 0; i < 40 && !targets.length; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch (e) { await sleep(250); } }
    const ws = new WebSocket(targets.find(t => t.type === "page").webSocketDebuggerUrl);
    await new Promise(r => { ws.onopen = r; });
    let id = 0; const waits = new Map(); const pageErrors = [];
    ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      if (m.id && waits.has(m.id)) { waits.get(m.id)(m.result || m.error); waits.delete(m.id); }
      else if (m.method === "Runtime.exceptionThrown") pageErrors.push(((m.params.exceptionDetails.exception || {}).description || m.params.exceptionDetails.text || "").slice(0, 300));
    };
    const send = (method, params = {}) => new Promise((r, rej) => { const n = ++id; const t = setTimeout(() => rej(new Error(`${method} did not answer`)), 60000); waits.set(n, v => { clearTimeout(t); r(v); }); ws.send(JSON.stringify({ id: n, method, params })); });
    const ev = async expr => {
      const r = await send("Runtime.evaluate", { expression: `(async () => { const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)]; ${expr} })()`, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception && r.exceptionDetails.exception.description));
      return r.result.value;
    };
    await send("Page.enable"); await send("Runtime.enable"); await send("Emulation.setFocusEmulationEnabled", { enabled: true }); await send("Page.bringToFront");
    const waitFor = async (expr, what, tries = 120) => { for (let i = 0; i < tries; i++) { if (await ev(`return (${expr});`)) return true; await sleep(250); } throw new Error(`never: ${what}`); };
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message + (pageErrors.length ? " | page errors: " + pageErrors.splice(0).join(" ;; ") : "") }); } };

    for (const [vname, w, h, mobile] of VIEWPORTS) {
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      await send("Page.navigate", { url: URL + "?lang=en" });
      await sleep(1500);
      await check(`${vname}: the row sits between the functions table and the council, says who it is for, and fits the screen`, async () => {
        await waitFor(`!!q("#tab-loader") && !!q("#sub-providers")`, "the page");
        await ev(`q("#tab-loader").click(); return 1;`);
        await waitFor(`!!q("#sub-providers")`, "the models tab");
        await ev(`q("#sub-providers").click(); return 1;`);
        await waitFor(`!!q(".pv-external")`, "the row");
        const info = JSON.parse(await ev(`const row = q(".pv-external"); const kids = [...row.parentElement.children];
          const at = kids.indexOf(row);
          return JSON.stringify({ title: row.querySelector(".sh b").textContent, who: row.querySelector(".pv-external-who").textContent, help: row.querySelector(".setnote").textContent,
            after: kids[at - 1] && kids[at - 1].className, before: kids[at + 1] && kids[at + 1].className, wide: document.documentElement.scrollWidth > innerWidth + 1,
            inside: row.getBoundingClientRect().right <= innerWidth + 1 && row.getBoundingClientRect().left >= -1 });`));
        if (!/OpenAI and Anthropic/.test(info.title)) throw new Error("title: " + info.title);
        if (!info.who || !info.help) throw new Error("the row has no text: " + JSON.stringify(info));
        if (info.wide) throw new Error("the page scrolls sideways");
        if (!info.inside) throw new Error("the row goes out of the screen");
        if (/pv-external/.test(String(info.after))) throw new Error("order: " + JSON.stringify(info));
      });
    }
    if (pageErrors.length) out.push({ name: "page errors", ok: false, error: pageErrors.join(" ;; ") });
    ws.close();
  } finally { proc.kill(); }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "driver", ok: false, error: String(e && e.stack || e) }])); });
