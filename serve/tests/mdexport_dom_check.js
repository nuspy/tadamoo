"use strict";
// Drives the "save a Markdown file" window in a real headless browser at three screen sizes: node mdexport_dom_check.js <url> <browser exe>.
// Prints [{name, ok, error}] as JSON (read by test_mdexport_page.py). The window is opened the way the chat opens it (window.mdExport.ask) with two recording calls in place
// of the file saving, and each press is the button or the choice a person makes; what the window asks of its owner is checked.
const { spawn } = require("child_process"), fs = require("fs"), os = require("os");
const [URL, EXE] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(os.tmpdir() + "/mxc-");
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
      const r = await send("Runtime.evaluate", { expression: `(async () => { const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)]; const wait = ms => new Promise(r => setTimeout(r, ms)); ${expr} })()`, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception && r.exceptionDetails.exception.description));
      return r.result.value;
    };
    await send("Page.enable"); await send("Runtime.enable"); await send("Emulation.setFocusEmulationEnabled", { enabled: true }); await send("Page.bringToFront");
    const waitFor = async (expr, what, tries = 120) => { for (let i = 0; i < tries; i++) { if (await ev(`return (${expr});`)) return true; await sleep(250); } throw new Error(`never: ${what}`); };
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message + (pageErrors.length ? " | page errors: " + pageErrors.splice(0).join(" ;; ") : "") }); } };
    const dlg = `q("dialog.mx-dlg[open]")`;
    const openIt = async () => {
      await ev(`window.__mx = []; window.mdExport.ask({ name: "Report.md", original: async () => window.__mx.push(["original"]), converted: async (mode, format) => window.__mx.push([mode, format]), fail: e => window.__mx.push(["fail", String(e)]) }); return 1;`);
      await waitFor(`!!${dlg}`, "the window");
    };
    const log = async () => JSON.parse(await ev(`return JSON.stringify(window.__mx);`));
    const press = label => ev(`const b = qa("dialog.mx-dlg .im-actions button").find(x => x.textContent.trim() === ${JSON.stringify(label)}); if (!b) throw new Error("no button ${label}"); b.click(); return 1;`);
    const pick = mode => ev(`const r = q('dialog.mx-dlg input[value="${mode}"]'); r.click(); return 1;`);
    const fits = what => ev(`const d = ${dlg}; if (!d) return JSON.stringify({ shown: false });
      const small = qa("dialog.mx-dlg button, dialog.mx-dlg select, dialog.mx-dlg input[type=radio]").filter(e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0 && Math.max(b.height, e.closest("label") ? e.closest("label").getBoundingClientRect().height : 0) < 43.5; }).map(e => e.textContent.trim().slice(0, 20) || e.name);
      const box = d.getBoundingClientRect();
      return JSON.stringify({ shown: true, wide: document.documentElement.scrollWidth > innerWidth + 1, inside: box.left >= -1 && box.right <= innerWidth + 1, small });`).then(v => {
      const f = JSON.parse(v);
      if (!f.shown) throw new Error(what + ": the window is not open");
      if (f.wide) throw new Error(what + ": the page scrolls sideways");
      if (!f.inside) throw new Error(what + ": the window goes out of the screen");
      if (f.small.length) throw new Error(what + ": controls under 44 px: " + f.small.join(", "));
    });

    for (const [vname, w, h, mobile] of VIEWPORTS) {
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      await send("Page.navigate", { url: URL + "?lang=en" });
      await sleep(1500);
      await waitFor(`!!window.mdExport && typeof authHeaders === "function"`, "the page and its script");
      await ev(`localStorage.removeItem("tadamoo.mdexport"); return 1;`);

      await check(`${vname}: the window offers the three ways, the formats of this PC, starts on the Markdown alone and fits the screen`, async () => {
        await openIt();
        const info = JSON.parse(await ev(`return JSON.stringify({ title: q("#mx-title").textContent, modes: qa('dialog.mx-dlg input[name="mx-mode"]').map(r => [r.value, r.checked, r.closest("label").textContent.trim()]),
          formats: qa("#mx-format option").map(o => o.value), formatOff: q("#mx-format").disabled });`));
        if (info.title !== "Save Report.md") throw new Error("title: " + info.title);
        if (info.modes.map(m => m[0]).join() !== "md,converted,both") throw new Error("modes: " + JSON.stringify(info.modes));
        if (!info.modes[0][1] || info.modes[1][1] || info.modes[2][1]) throw new Error("it starts on the Markdown alone: " + JSON.stringify(info.modes));
        if (!/^Only the Markdown file$/.test(info.modes[0][2]) || !/^Only the converted file$/.test(info.modes[1][2]) || !/one zip/.test(info.modes[2][2])) throw new Error("texts: " + JSON.stringify(info.modes));
        for (const f of ["docx", "odt", "html"]) if (!info.formats.includes(f)) throw new Error("formats: " + info.formats);
        if (!info.formatOff) throw new Error("the format is off while only the Markdown is chosen");
        await fits("the window");
        await press("Cancel");
        if ((await log()).length) throw new Error("cancel did something: " + JSON.stringify(await log()));
      });

      await check(`${vname}: only the Markdown saves the file as it is`, async () => {
        await openIt(); await press("Save");
        const l = await log();
        if (JSON.stringify(l) !== JSON.stringify([["original"]])) throw new Error(JSON.stringify(l));
        if (await ev(`return !!${dlg};`)) throw new Error("the window stays open");
      });

      await check(`${vname}: only the converted one asks for the chosen format, and the choice is remembered`, async () => {
        await openIt(); await pick("converted");
        if (await ev(`return q("#mx-format").disabled;`)) throw new Error("the format is still off");
        await ev(`const s = q("#mx-format"); s.value = "odt"; s.dispatchEvent(new Event("change")); return 1;`);
        await press("Save");
        const l = await log();
        if (JSON.stringify(l) !== JSON.stringify([["converted", "odt"]])) throw new Error(JSON.stringify(l));
        await openIt();
        const again = JSON.parse(await ev(`return JSON.stringify({ mode: q('dialog.mx-dlg input[name="mx-mode"]:checked').value, format: q("#mx-format").value });`));
        if (again.mode !== "converted" || again.format !== "odt") throw new Error("not remembered: " + JSON.stringify(again));
        await press("Cancel");
      });

      await check(`${vname}: both asks for the Markdown and the converted file together`, async () => {
        await openIt(); await pick("both");
        await ev(`const s = q("#mx-format"); s.value = "docx"; s.dispatchEvent(new Event("change")); return 1;`);
        await press("Save");
        const l = await log();
        if (JSON.stringify(l) !== JSON.stringify([["both", "docx"]])) throw new Error(JSON.stringify(l));
      });

      await check(`${vname}: a server that cannot convert saves the file as it always did, with no window`, async () => {
        await ev(`window.__real = window.fetch; window.fetch = (u, o) => String(u).includes("api/export/formats") ? Promise.resolve(new Response('{"formats": []}', { status: 200 })) : window.__real(u, o); window.mdExport.__reset && window.mdExport.__reset(); return 1;`);
        // the formats are asked once per page: a fresh page asks again
        await send("Page.navigate", { url: URL + "?lang=en" }); await sleep(1500);
        await waitFor(`!!window.mdExport && typeof authHeaders === "function"`, "the page again");
        await ev(`const real = window.fetch; window.fetch = (u, o) => String(u).includes("api/export/formats") ? Promise.resolve(new Response('{"formats": []}', { status: 200 })) : real(u, o); window.__mx = []; await window.mdExport.ask({ name: "a.md", original: async () => window.__mx.push(["original"]), converted: async () => 0 }); return 1;`);
        const l = await log();
        if (JSON.stringify(l) !== JSON.stringify([["original"]])) throw new Error(JSON.stringify(l));
        if (await ev(`return !!q("dialog.mx-dlg");`)) throw new Error("a window opened");
      });
    }
    if (pageErrors.length) out.push({ name: "page errors", ok: false, error: pageErrors.join(" ;; ") });
    ws.close();
  } finally { proc.kill(); }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "driver", ok: false, error: String(e && e.stack || e) }])); });
