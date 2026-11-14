"use strict";
// Draws the plans page of a running server in a real headless browser and checks the graph of a decision: node plan_graph_dom_check.js <url> <plan title> <browser exe>
// Prints [{name, ok, error}] as JSON (read by test_plans_page.py). Two viewports: 1440x900 and 360x640.
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, TITLE, EXE] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function inBrowser(width, height, port, body) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "plg-"));
  const proc = spawn(EXE, ["--headless=new", "--lang=en-US", "--disable-gpu", `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, `--window-size=${width},${height}`, "about:blank"], { stdio: "ignore" });
  try {
    let targets = [];
    for (let i = 0; i < 40 && !targets.length; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch (e) { await sleep(250); } }
    const ws = new WebSocket(targets.find(t => t.type === "page").webSocketDebuggerUrl);
    await new Promise(r => { ws.onopen = r; });
    let id = 0; const waits = new Map();
    ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.id && waits.has(m.id)) { waits.get(m.id)(m.result || m.error); waits.delete(m.id); } };
    const send = (method, params = {}) => new Promise(r => { const n = ++id; waits.set(n, r); ws.send(JSON.stringify({ id: n, method, params })); });
    await send("Page.enable");
    await send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width < 600 });
    await send("Page.navigate", { url: URL });
    await sleep(2500);
    const expression = `(async () => { const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)]; const wait = ms => new Promise(r => setTimeout(r, ms)); ${body} })()`;
    const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (process.env.PLG_SHOTS) fs.writeFileSync(path.join(process.env.PLG_SHOTS, `plan-${width}.png`), Buffer.from((await send("Page.captureScreenshot", { format: "png" })).data, "base64"));
    ws.close();
    if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception && r.exceptionDetails.exception.description));
    return r.result.value;
  } finally { proc.kill(); await sleep(400); try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* temp */ } }
}

const MEASURE = `q("#tab-plans").click(); await wait(1500);
const hit = qa("#plans-root *").filter(e => e.children.length === 0 && e.textContent.includes(${JSON.stringify(TITLE)})).sort((a, b) => a.textContent.length - b.textContent.length)[0];
(hit.closest("button, a, li, [role=button], [tabindex]") || hit).click(); await wait(1500);
for (let i = 0; i < 20; i++) {                                           // the plan opens, then the graph is asked for, until it is on the screen
  const gb = qa("#plans-root button").find(b => /^(Graph|Grafo)$/.test(b.textContent.trim())); if (gb) gb.click();
  await wait(500);
  const first = q("#plans-root .pl-node .pl-br"); if (first && first.getBoundingClientRect().width > 0) break;
}
{ const z = q("#plans-root .pl-zreset"); if (z) { z.click(); await wait(300); } }               // the diagram opens fitted to the pane; these checks are about its real size
const sel = '#plans-root .pl-node[data-id="choose"]';
const node = q(sel), labels = qa(sel + " .pl-br");
const rows = labels.map(t => { const b = t.getBoundingClientRect(); return { text: t.textContent.trim(), w: b.width, h: b.height }; });
const dots = qa(sel + " .pl-port.out").map(c => Math.round(c.getBoundingClientRect().width));
const title = node.querySelector(".pl-t"), sub = node.querySelector(".pl-s");
let hid = "", up = labels[0]; while (up && up !== document.body) { const c = getComputedStyle(up); if (!hid && (c.display === "none" || c.visibility === "hidden" || up.getBoundingClientRect().width === 0)) hid = up.tagName + "." + String(up.className.baseVal ?? up.className) + " " + c.display + " " + c.visibility; up = up.parentElement; }
return { hid, rows, dots, titleFont: getComputedStyle(title).fontSize, subFont: getComputedStyle(sub).fontSize, brFont: labels.length ? getComputedStyle(labels[0]).fontSize : "",
  subBottom: sub.getBoundingClientRect().bottom, titleBottom: title.getBoundingClientRect().bottom, firstRowTop: labels.length ? labels[0].getBoundingClientRect().top : 0,
  painted: qa(sel + " .pl-hit").map(e => getComputedStyle(e).fill), pageScroll: document.documentElement.scrollWidth, innerWidth };`;

(async () => {
  const results = [];
  const check = (name, fn) => { try { fn(); results.push({ name, ok: true }); } catch (e) { results.push({ name, ok: false, error: String(e.message || e) }); } };
  const need = (c, m) => { if (!c) throw new Error(m); };
  const squash = s => s.replace(/\s+/g, "");
  for (const [w, h, port] of [[1440, 900, 9371], [360, 640, 9372]]) {
    const m = await inBrowser(w, h, port, MEASURE);
    check(`${w}: every output connector of the decision has its label, and it shows`, () => {
      need(m.rows.length === 5, "rows " + JSON.stringify(m.rows));
      m.rows.forEach(r => need(r.text && r.w > 4 && r.h > 4, "empty or invisible label " + JSON.stringify(r) + " hidden at " + m.hid));
      need(["< 10", "= 10", "> 10", "10 to 30", "else"].every((t, i) => squash(m.rows[i].text) === squash(t)), JSON.stringify(m.rows.map(r => r.text)));
    });
    check(`${w}: the touch areas of the connectors draw nothing`, () => need(m.painted.length >= 5 && m.painted.every(f => f === "none" || f === "rgba(0, 0, 0, 0)"), JSON.stringify(m.painted)));
    check(`${w}: the header (title and status) ends above the first branch row`, () => need(m.subBottom <= m.firstRowTop + 1 && m.titleBottom <= m.firstRowTop, JSON.stringify([m.titleBottom, m.subBottom, m.firstRowTop])));
    check(`${w}: the type and the dots are drawn at their real, small size`, () => {
      need(m.titleFont === "13px" && m.subFont === "11px" && m.brFont === "11px", JSON.stringify([m.titleFont, m.subFont, m.brFont]));
      need(m.dots.length === 5 && m.dots.every(d => d >= 9 && d <= 12), JSON.stringify(m.dots));
    });
    check(`${w}: the page does not scroll sideways`, () => need(m.pageScroll <= m.innerWidth, JSON.stringify([m.pageScroll, m.innerWidth])));
  }
  console.log(JSON.stringify(results));
  process.exit(0);
})().catch(e => { console.log(JSON.stringify([{ name: "browser run", ok: false, error: String(e && e.stack || e) }])); process.exit(0); });
