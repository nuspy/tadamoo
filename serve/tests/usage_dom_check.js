"use strict";
// Opens Models > Usage in a real headless browser at three screen sizes, with calls written at 30 minutes, 5 hours and 3 days ago, and drives the fixed periods and a period given by date and time:
// node usage_dom_check.js <url> <browser exe>. Prints [{name, ok, error}] as JSON (read by test_usage_page.py).
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
      const calls = async () => JSON.parse(await ev(`await new Promise(r => setTimeout(r, 900)); const t = qa("#ld-usage .us-tile-v").map(x => x.textContent.replace(/\D/g, "")); return JSON.stringify({ calls: t[0] || "none", total: t[4] || "", none: !!q("#ld-usage .us-tiles") === false });`));
      await check(`${vname}: the tab shows the periods, the totals and the tables and fits the screen`, async () => {
        await waitFor(`!!q("#tab-loader") && !!q("#sub-usage")`, "the page");
        await ev(`q("#tab-loader").click(); return 1;`);
        await waitFor(`!!q("#sub-usage")`, "the models tab");
        await ev(`q("#sub-usage").click(); return 1;`);
        await waitFor(`!!q("#ld-usage .us-tiles")`, "the totals");
        const info = JSON.parse(await ev(`return JSON.stringify({ presets: qa("#ld-usage .us-presets button").map(b => b.textContent), from: !!q("#us-from"), to: !!q("#us-to"),
          rows: qa("#ld-usage .us-table tbody th").map(x => x.textContent), wide: document.documentElement.scrollWidth > innerWidth + 1, hidden: q("#ld-providers").hidden && q("#ld-qwen").hidden });`));
        if (info.presets.join("|") !== "Last hour|Last 12 hours|Last day|Last week|Last month|All") throw new Error("presets: " + info.presets);
        if (!info.from || !info.to) throw new Error("no date and time fields");
        if (!info.rows.some(x => /Cloud/.test(x)) || !info.rows.some(x => /Local engine/.test(x))) throw new Error("rows: " + JSON.stringify(info.rows));
        if (info.wide) throw new Error("the page scrolls sideways");
        if (!info.hidden) throw new Error("the other panels are still shown");
      });
      await check(`${vname}: each fixed period counts the calls inside it`, async () => {
        const expect = { "Last hour": "1", "Last 12 hours": "2", "Last day": "2", "Last week": "3", "Last month": "3", "All": "3" };
        for (const [label, n] of Object.entries(expect)) {
          await ev(`[...qa("#ld-usage .us-presets button")].find(b => b.textContent === ${JSON.stringify(label)}).click(); return 1;`);
          const got = await calls();
          if (got.calls !== n) throw new Error(`${label}: ${got.calls} calls, expected ${n}`);
        }
      });
      await check(`${vname}: a period given by date and time counts the calls inside it, and a start after the end is refused`, async () => {
        await ev(`const f = x => { const d = new Date(x * 1000 - new Date().getTimezoneOffset() * 60000); return d.toISOString().slice(0, 16); };
          const now = Date.now() / 1000; const set = (id, v) => { const e = q(id); e.value = v; e.dispatchEvent(new Event("input", { bubbles: true })); };
          set("#us-from", f(now - 4 * 86400)); set("#us-to", f(now - 2 * 86400)); [...qa("#ld-usage button")].find(b => b.textContent === "Show this period").click(); return 1;`);
        const got = await calls();
        if (got.calls !== "1") throw new Error("period of the third day: " + JSON.stringify(got));
        await ev(`const f = x => { const d = new Date(x * 1000 - new Date().getTimezoneOffset() * 60000); return d.toISOString().slice(0, 16); };
          const now = Date.now() / 1000; q("#us-from").value = f(now); q("#us-to").value = f(now - 3600); [...qa("#ld-usage button")].find(b => b.textContent === "Show this period").click(); return 1;`);
        await waitFor(`/before the end/.test(q("#ld-usage").textContent)`, "the refusal");
      });
    }
    if (pageErrors.length) out.push({ name: "page errors", ok: false, error: pageErrors.join(" ;; ") });
    ws.close();
  } finally { proc.kill(); }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "driver", ok: false, error: String(e && e.stack || e) }])); });
