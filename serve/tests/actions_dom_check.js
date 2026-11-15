"use strict";
// The Required actions tab and the dialog for a new entry in a real headless browser: node actions_dom_check.js <url> <browser exe> <screen name> <language>.
// The world: a plan with a question and an approval on the server's board, both there before the page opens. Prints [{name, ok, error}] as JSON (read by test_actions_page.py).
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE, SCREEN, LANG] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const SCREENS = { "desktop 1280x800": [1280, 800, false], "phone 360x640": [360, 640, true], "phone 390x844": [390, 844, true] };

async function main() {
  const [vw, vh, mobile] = SCREENS[SCREEN];
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "acc-"));
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
    const jget = p => ev(`const r = await fetch(${JSON.stringify(p)}, { headers: authHeaders() }); return JSON.stringify(await r.json());`).then(JSON.parse);
    const jpost = (p, b) => ev(`const r = await fetch(${JSON.stringify(p)}, { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(${JSON.stringify(b)}) }); return JSON.stringify({ status: r.status, body: await r.json() });`).then(JSON.parse);

    // a headless page reports that it is hidden: for these checks it is the page in front
    await send("Page.addScriptToEvaluateOnNewDocument", { source: `Object.defineProperty(document, "hidden", { get: () => false }); Object.defineProperty(document, "visibilityState", { get: () => "visible" }); document.hasFocus = () => true;` });
    await send("Emulation.setDeviceMetricsOverride", { width: vw, height: vh, deviceScaleFactor: 1, mobile });
    await send("Emulation.setTouchEmulationEnabled", { enabled: mobile, maxTouchPoints: 5 });
    await send("Page.navigate", { url: URL + `?lang=${LANG}#cron` });
    await waitFor(`!!q("#tab-actions") && typeof view === "function"`, "the page");
    await sleep(600);

    await check(`${tag}: what was already waiting when the page opened is not announced by a dialog, and the number is on the tab`, async () => {
      await waitFor(`!!q("#tab-actions .ac-badge")`, "the number on the tab", 60);
      const n = await ev(`return q("#tab-actions .ac-badge").textContent`);
      if (n !== "2") throw new Error("the number on the tab is " + n);
      if (await ev(`return !!q(".ac-dlg[open]")`)) throw new Error("a dialog announced what was already waiting");
    });

    await check(`${tag}: the tab lists the entries with their buttons, big enough, and what caused a security authorization`, async () => {
      await ev(`q("#tab-actions").click(); return 1;`);
      await waitFor(`qa("#ac-root .ac-item").length === 2`, "the two entries");
      const r = JSON.parse(await ev(`const items = qa("#ac-root .ac-item"); const small = qa("#ac-root button, #ac-root input, #ac-root textarea, #ac-root summary").filter(e => { const b = e.getBoundingClientRect(); return b.width > 0 && (b.height < 43.5 || b.width < 43.5); }).map(e => e.textContent.trim().slice(0, 20) + ":" + Math.round(e.getBoundingClientRect().height));
        return JSON.stringify({ src: items.map(e => e.dataset.source), buttons: items.map(e => qa("button.ac-btn", e).map(b => b.textContent.trim())), cause: !!q('#ac-root .ac-item[data-source="approval"] details.ac-cause'),
          fact: (q('#ac-root .ac-item[data-source="approval"] .ac-fact') || {}).textContent || "", small, sw: document.documentElement.scrollWidth, iw: innerWidth, count: document.getElementById("ac-count").textContent })`));
      if (r.src.sort().join() !== "approval,plan") throw new Error("sources " + r.src);
      if (!r.cause || !/shell|Strumento|Tool|ls/.test(r.fact + "")) throw new Error("the cause is not shown: " + JSON.stringify(r));
      const yes = LANG === "it" ? "Sì" : "Yes";
      if (!r.buttons.some(b => b.includes(yes))) throw new Error("buttons " + JSON.stringify(r.buttons));
      if (mobile && r.small.length) throw new Error("controls under 44 px: " + r.small.join(", "));
      if (r.sw > r.iw + 1) throw new Error("the page scrolls sideways");
      if (!/2/.test(r.count)) throw new Error("the count in the heading: " + r.count);
    });

    await check(`${tag}: an answer is sent to its source, the entry goes and the number follows`, async () => {
      const yes = LANG === "it" ? "Sì" : "Yes";
      await ev(`const b = qa('#ac-root .ac-item[data-source="plan"] button.ac-btn').find(b => b.textContent.trim() === ${JSON.stringify(yes)}); b.click(); return 1;`);
      await waitFor(`qa("#ac-root .ac-item").length === 1`, "the entry went");
      const plan = await jget("api/plans/actions-check");
      if (plan.plan.shared_state.good !== true) throw new Error("the plan did not get the answer: " + JSON.stringify(plan.plan.shared_state));
      await waitFor(`q("#tab-actions .ac-badge").textContent === "1"`, "the number on the tab follows");
    });

    await check(`${tag}: an entry answered somewhere else says so and goes, without sending a second answer`, async () => {
      const made = await jpost("api/agent/approvals", { run: "r2", tool: "shell", arguments: { cmd: "pwd" }, effect: "reads" });
      try { await waitFor(`qa("#ac-root .ac-item").length === 2`, "the new entry in the tab", 80); } catch (e) { throw new Error(e.message + " | made " + JSON.stringify(made) + " | api " + JSON.stringify(await jget("api/actions?channel=web")).slice(0, 300) + " | dom " + await ev(`return qa("#ac-root .ac-item").length + " hidden=" + document.hidden + " view=" + document.body.dataset.view`)); }
      const aid = made.body.id;
      const decided = await jpost("api/agent/approvals/" + aid, { decision: "approve" });                       // answered on another screen
      if (decided.status !== 200) throw new Error("the other screen could not answer: " + JSON.stringify(decided));
      const seen = await ev(`const it = qa("#ac-root .ac-item").find(e => e.textContent.includes("pwd")); if (!it) return "no item"; qa("button.ac-btn", it)[1].click();
        for (let i = 0; i < 60; i++) { await wait(50); const e = qa("#ac-root .ac-err").find(x => !x.hidden && x.textContent); if (e) return e.textContent; } return "NONE items=" + qa("#ac-root .ac-item").length + " btns=" + JSON.stringify(qa("#ac-root button.ac-btn").map(b => [b.textContent, b.disabled])) + " errs=" + JSON.stringify(qa("#ac-root .ac-err").map(x => [x.hidden, x.textContent])) + " live=" + (q(".ac-live") || {}).textContent;`);
      if (!/(already answered|già avuto risposta)/.test(seen)) throw new Error("the message that it was answered elsewhere: " + JSON.stringify(seen));
      await waitFor(`qa("#ac-root .ac-item").length === 1`, "the entry goes");
      const st = await jget("api/agent/approvals/" + aid);
      if (st.status !== "approved") throw new Error("the first answer did not win: " + st.status);
    });

    await check(`${tag}: a new entry arrives while another tab is open: a dialog shows it once, with the buttons; it is not shown again after it is closed`, async () => {
      await ev(`q("#tab-cron").click(); return 1;`);
      const made = await jpost("api/agent/approvals", { run: "r3", tool: "write_file", arguments: { path: "x.txt" }, effect: "writes" });
      try { await waitFor(`!!q(".ac-dlg[open] .ac-item")`, "the dialog", 120); } catch (e) { throw new Error(e.message + " | made " + JSON.stringify(made) + " | api " + JSON.stringify((await jget("api/actions?channel=web")).actions.map(a => [a.id, a.notified])) + " | page " + await ev(`return "hidden=" + document.hidden + " vis=" + document.visibilityState + " view=" + document.body.dataset.view + " dlgs=" + qa("dialog").length + " badge=" + ((q("#tab-actions .ac-badge") || {}).textContent)`)); }
      const d = JSON.parse(await ev(`const dlg = q(".ac-dlg[open]"); const small = qa("button, summary", dlg).filter(e => { const b = e.getBoundingClientRect(); return b.height < 43.5; }).map(e => e.textContent.trim()); const r = dlg.getBoundingClientRect();
        return JSON.stringify({ buttons: qa(".ac-item button.ac-btn", dlg).map(b => b.textContent.trim()), small, inside: r.left >= 0 && r.right <= innerWidth + 1 && r.top >= 0 && r.bottom <= innerHeight + 1, text: dlg.textContent })`));
      if (d.buttons.length !== 2 || !/write_file/.test(d.text)) throw new Error("the dialog " + JSON.stringify(d));
      if (mobile && d.small.length) throw new Error("controls under 44 px in the dialog: " + d.small.join(", "));
      if (!d.inside) throw new Error("the dialog does not fit the screen");
      await ev(`qa(".ac-dlg[open] .ac-item button.ac-btn")[1].click(); return 1;`);           // deny
      await waitFor(`!q(".ac-dlg[open]")`, "the dialog closes");
      const st = await jget("api/agent/approvals/" + made.body.id);
      if (st.status !== "denied") throw new Error("the answer of the dialog did not arrive: " + st.status);
      const mine = (await jget("api/actions?channel=web")).actions.filter(a => a.id === "approval:" + made.body.id);
      if (mine.length) throw new Error("an answered entry is still listed");
      const again = await jpost("api/agent/approvals", { run: "r4", tool: "shell", arguments: { cmd: "date" } });
      await ev(`await wait(300); qa(".ac-dlg[open] .ac-item").length; return 1;`);
      try { await waitFor(`!!q(".ac-dlg[open]")`, "a second new entry gets its own dialog", 120); } catch (e) { throw new Error(e.message + " | api " + JSON.stringify((await jget("api/actions?channel=web")).actions.map(a => [a.id, a.notified])) + " | again " + again.body.id + " | page " + await ev(`return "hidden=" + document.hidden + " view=" + document.body.dataset.view + " ac=" + qa(".ac-dlg").length + " items=" + qa(".ac-dlg .ac-item").length`)); }
      await ev(`q(".ac-dlg[open] .ac-dlg-foot .btn").click(); return 1;`);                       // Later
      await waitFor(`!q(".ac-dlg[open]")`, "Later closes it");
      await sleep(9500);                                                                              // a whole poll later: it is not shown again
      if (await ev(`return !!q(".ac-dlg[open]")`)) throw new Error("the entry was announced twice");
      const row = (await jget("api/actions?channel=web")).actions.find(a => a.id === "approval:" + again.body.id);
      if (!row || row.notified !== true) throw new Error("the entry is not marked as announced: " + JSON.stringify(row));
    });
    if (pageErrors.length) out.push({ name: `${tag}: no script error`, ok: false, error: pageErrors.splice(0).join(" ;; ") });
  } finally {
    try { proc.kill(); } catch (e) { /* gone */ }
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* in use */ }
  }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "script", ok: false, error: String(e && e.stack || e) }])); });
