"use strict";
// Drives Settings > Inference > Summarise a text in a real headless browser at three screen sizes: node summarylevels_dom_check.js <url> <browser exe>.
// Prints [{name, ok, error}] as JSON (read by test_summary_levels_page.py). The call to the server is answered by the page's own fetch stub, which records what the
// page sent: the check is that the controls ask for what the person chose, and that what comes back is shown and can be copied and saved.
const { spawn } = require("child_process"), fs = require("fs"), os = require("os");
const [URL, EXE] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];

async function main() {
  const port = 9800 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(os.tmpdir() + "/slc-");
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

    const openPanel = async lang => {
      await send("Page.navigate", { url: URL + "?lang=" + lang });
      await sleep(1500);
      await waitFor(`!!q("#tab-settings") && !!q("#sl-root")`, "the page");
      await ev(`q("#tab-settings").click(); return 1;`);
      await waitFor(`qa("#viewsub button").some(b => /Inference|Inferenza/.test(b.textContent))`, "the sub-tabs");
      await ev(`qa("#viewsub button").find(b => /Inference|Inferenza/.test(b.textContent)).click(); return 1;`);
      await waitFor(`!q("#sl-section").hidden`, "the section");
      await ev(`window.__sent = []; window.__real = window.fetch; window.fetch = (u, o) => {
        if (String(u).includes("api/summarize")) { window.__sent.push(JSON.parse(o.body)); return Promise.resolve(new Response(JSON.stringify(window.__answer || {}), { status: window.__status || 200, headers: { "Content-Type": "application/json" } })); }
        return window.__real(u, o); };
        window.__answer = { level: "executive", summary: "- The committee approved 4,200 euros.", chars_in: 200, chars_out: 38, parts: 1, side: true }; window.__status = 200; return 1;`);
    };
    const fits = what => ev(`const sec = q("#sl-section"); sec.scrollIntoView();
      const small = qa("#sl-section button, #sl-section select, #sl-section input[type=text], #sl-section label.btn").filter(e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0 && b.height < 43.5; }).map(e => (e.textContent.trim().slice(0, 18) || e.id));
      const box = sec.getBoundingClientRect();
      return JSON.stringify({ wide: document.documentElement.scrollWidth > innerWidth + 1, inside: box.left >= -1 && box.right <= innerWidth + 1, small });`).then(v => {
      const f = JSON.parse(v);
      if (f.wide) throw new Error(what + ": the page scrolls sideways");
      if (!f.inside) throw new Error(what + ": the section goes out of the screen");
      if (f.small.length) throw new Error(what + ": controls under 44 px: " + f.small.join(", "));
    });
    const type = text => ev(`const t = q("#sl-text"); t.value = ${JSON.stringify(text)}; t.dispatchEvent(new Event("input")); return 1;`);

    for (const [vname, w, h, mobile] of VIEWPORTS) {
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      await openPanel("en");

      await check(`${vname}: four levels, the button is off with no text, and the panel fits the screen`, async () => {
        const info = JSON.parse(await ev(`return JSON.stringify({ levels: qa("#sl-level option").map(o => o.value), labels: qa("#sl-level option").map(o => o.textContent), go: q("#sl-go").disabled, help: q("#sl-level-help").textContent, result: q("#sl-result").hidden });`));
        if (info.levels.join() !== "integral,medium,high,executive") throw new Error("levels: " + info.levels);
        if (!/Integral/.test(info.labels[0]) || !/Executive/.test(info.labels[3])) throw new Error("labels: " + info.labels);
        if (!info.go) throw new Error("the button is on with no text");
        if (!/half the length/.test(info.help)) throw new Error("help for the default level: " + info.help);
        if (!info.result) throw new Error("a result is shown before any summary");
        await fits("the panel");
      });

      await check(`${vname}: the request carries the text, the level and the language, and the summary is shown with its figures`, async () => {
        await type("The committee met on Monday and approved 4,200 euros for the library.");
        await ev(`const s = q("#sl-level"); s.value = "executive"; s.dispatchEvent(new Event("change")); q("#sl-lang").value = "Hungarian"; return 1;`);
        if (await ev(`return q("#sl-go").disabled;`)) throw new Error("the button is off with a text");
        if (!/sentence/i.test(await ev(`return q("#sl-level-help").textContent;`))) throw new Error("help did not follow the level");
        await ev(`q("#sl-go").click(); return 1;`);
        await waitFor(`!q("#sl-result").hidden`, "the result");
        const sent = JSON.parse(await ev(`return JSON.stringify(window.__sent);`));
        if (sent.length !== 1 || sent[0].level !== "executive" || sent[0].language !== "Hungarian" || !/4,200 euros/.test(sent[0].text)) throw new Error("sent: " + JSON.stringify(sent));
        const shown = JSON.parse(await ev(`return JSON.stringify({ out: q("#sl-out").value, stats: q("#sl-stats").textContent, note: q("#sl-note").textContent, go: q("#sl-go").textContent });`));
        if (shown.out !== "- The committee approved 4,200 euros.") throw new Error("summary: " + shown.out);
        if (!/200 characters became 38 \(19%\), in 1 part\(s\), written in the technical session/.test(shown.stats)) throw new Error("stats: " + shown.stats);
        if (shown.go !== "Summarise") throw new Error("button after: " + shown.go);
        await fits("the panel with the result");
      });

      await check(`${vname}: an empty language is not sent, an error is said, and the stop of the person is said as an error too`, async () => {
        await ev(`window.__sent.length = 0; q("#sl-lang").value = ""; window.__status = 409; window.__answer = { error: { type: "stopped", message: "the summary was stopped" } }; q("#sl-go").click(); return 1;`);
        await waitFor(`/could not be written/.test(q("#sl-note").textContent)`, "the error");
        const sent = JSON.parse(await ev(`return JSON.stringify(window.__sent);`));
        if (sent.length !== 1 || "language" in sent[0]) throw new Error("language sent although empty: " + JSON.stringify(sent));
        if (!/the summary was stopped/.test(await ev(`return q("#sl-note").textContent;`))) throw new Error("the reason is not shown");
        if (!(await ev(`return q("#sl-result").hidden;`))) throw new Error("an old result stays after a failure");
        await ev(`window.__status = 200; return 1;`);
      });

      await check(`${vname}: a file is read into the text, a file over 8 MB is refused, and Clear empties it`, async () => {
        await ev(`const dt = new DataTransfer(); dt.items.add(new File(["# Notes\\nOne line of notes."], "notes.md", { type: "text/markdown" })); const f = q("#sl-file"); f.files = dt.files; f.dispatchEvent(new Event("change")); return 1;`);
        await waitFor(`q("#sl-text").value.startsWith("# Notes")`, "the file text");
        if (!/notes.md read, 26 characters/.test(await ev(`return q("#sl-note").textContent;`))) throw new Error("note: " + await ev(`return q("#sl-note").textContent;`));
        await ev(`const dt = new DataTransfer(); dt.items.add(new File([new ArrayBuffer(9 << 20)], "big.txt")); const f = q("#sl-file"); f.files = dt.files; f.dispatchEvent(new Event("change")); return 1;`);
        await waitFor(`/larger than 8 MB/.test(q("#sl-note").textContent)`, "the refusal");
        if (!(await ev(`return q("#sl-text").value.startsWith("# Notes");`))) throw new Error("the refused file replaced the text");
        await ev(`q("#sl-clear").click(); return 1;`);
        if (await ev(`return q("#sl-text").value !== "" || !q("#sl-go").disabled;`)) throw new Error("Clear did not empty the text");
      });
    }

    await check("Italian: the title, the levels and the button are in Italian", async () => {
      await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
      await openPanel("it");
      const it = JSON.parse(await ev(`return JSON.stringify({ title: q("#sl-h").textContent, labels: qa("#sl-level option").map(o => o.textContent), go: q("#sl-go").textContent });`));
      if (it.title !== "Riassumi un testo" || !/Integrale/.test(it.labels[0]) || !/Executive/.test(it.labels[3]) || it.go !== "Riassumi") throw new Error(JSON.stringify(it));
    });
    if (pageErrors.length) out.push({ name: "page errors", ok: false, error: pageErrors.join(" ;; ") });
    ws.close();
  } finally { proc.kill(); }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "driver", ok: false, error: String(e && e.stack || e) }])); });
