// Drives Edge (the engine of WebView2, which the app uses) over the DevTools protocol and checks the web UI.
//   node tools/web_audit.mjs --url http://127.0.0.1:7232 [--out DIR] [--views a,b] [--sizes 1024x768,3600x1400]
//                            [--interact] [--strict]
// For every view and window size it measures page overflow, controls smaller than 24x24 pixels, console errors and
// the interface zoom, and saves a screenshot. With --interact it also tries Alt+number, Ctrl+K and the palette.
// --strict exits with 1 when anything fails. Needs Node 22+ (WebSocket and fetch are built in) and Edge or Chrome.
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const args = process.argv.slice(2);
const opt = k => { const i = args.indexOf("--" + k); return i >= 0 ? args[i + 1] : undefined; };
const flag = k => args.includes("--" + k);
const url = opt("url");
if (!url) { console.error("usage: node tools/web_audit.mjs --url URL [--out DIR] [--views a,b] [--sizes WxH,...] [--interact] [--strict]"); process.exit(2); }
const out = opt("out") || mkdtempSync(join(tmpdir(), "basalt-audit-"));
const VIEWS = (opt("views") || "chat,agent,plans,stats,loader,hardware,logs,settings").split(",");
const SIZES = (opt("sizes") || "1024x768,1920x1080,3600x1400").split(",").map(s => s.split("x").map(Number));
mkdirSync(out, { recursive: true });

const candidates = [process.env.BASALT_BROWSER,
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
  "C:/Program Files/Google/Chrome/Application/chrome.exe", "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/microsoft-edge"].filter(Boolean);
const browser = candidates.find(existsSync);
if (!browser) { console.error("no Edge or Chrome found (set BASALT_BROWSER)"); process.exit(3); }

const port = 9400 + Math.floor(Math.random() * 400);
const proc = spawn(browser, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${port}`,
  `--user-data-dir=${mkdtempSync(join(tmpdir(), "edge-"))}`, "--no-first-run", "about:blank"], { stdio: "ignore" });
const sleep = ms => new Promise(r => setTimeout(r, ms));
let list;
for (let i = 0; i < 60; i++) {
  try { list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json(); if (list.length) break; } catch (e) { /* not up yet */ }
  await sleep(250);
}
const page = (list || []).find(t => t.type === "page");
if (!page) { proc.kill(); console.error("the browser did not start"); process.exit(3); }
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise(r => (ws.onopen = r));
let id = 0;
const pending = new Map();
const consoleErrors = [];
ws.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  else if (m.method === "Runtime.exceptionThrown") consoleErrors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
  else if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") consoleErrors.push(m.params.args.map(a => a.value ?? a.description).join(" "));
};
const send = (method, params = {}) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })); });
const evaluate = async expression => (await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true })).result.result.value;
await send("Runtime.enable");
await send("Page.enable");

const key = async (k, code, vk, modifiers = 0) => {
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: k, code, windowsVirtualKeyCode: vk, modifiers });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: k, code, windowsVirtualKeyCode: vk, modifiers });
};

const PROBE = `(() => {
  const vw = document.documentElement.clientWidth, vh = innerHeight;
  const res = { vw, scrollW: document.documentElement.scrollWidth, zoom: getComputedStyle(document.body).zoom, over: [], small: [] };
  const clipsViewport = [document.documentElement, document.body].some(e => getComputedStyle(e).overflowY === "hidden");
  res.bodyH = Math.round(document.body.getBoundingClientRect().height);
  res.vscroll = clipsViewport ? Math.max(0, res.bodyH - vh) : 0;   // a hidden viewport must not cut the body off; a scrolling one may be taller
  const name = e => (e.id ? "#" + e.id : e.tagName.toLowerCase() + "." + String(e.className.baseVal ?? e.className).split(" ")[0]);
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none" && !e.closest("[hidden]"); };
  const scrolls = e => { for (let p = e.parentElement; p && p !== document.body; p = p.parentElement) { const o = getComputedStyle(p).overflowX; if (o !== "visible" && p.getBoundingClientRect().right <= vw + 1) return true; } return false; };
  for (const e of document.querySelectorAll("body *")) {
    if (!vis(e) || e.closest("svg") || e.closest("dialog:not([open])")) continue;
    const r = e.getBoundingClientRect();
    if (r.right > vw + 1 && !scrolls(e) && res.over.length < 8) res.over.push(name(e) + " right=" + Math.round(r.right));
  }
  const clipsY = e => { for (let p = e.parentElement; p; p = p.parentElement) { const o = getComputedStyle(p).overflowY; if (o !== "visible" && p !== document.documentElement) return true; } return false; };
  res.tall = [];
  if (res.vscroll > 2) for (const e of document.querySelectorAll("body *")) {
    if (e.closest("svg")) continue;
    const r = e.getBoundingClientRect();
    if (r.bottom > vh + 2 && !clipsY(e) && res.tall.length < 6) res.tall.push(name(e) + " bottom=" + Math.round(r.bottom) + " pos=" + getComputedStyle(e).position + " display=" + getComputedStyle(e).display);
  }
  const z = parseFloat(res.zoom) || 1;
  for (const e of document.querySelectorAll("button, a[href], input:not([type=hidden]), select, textarea, summary, [role=tab]")) {
    if (!vis(e) || e.closest("dialog:not([open])")) continue;
    const r = e.getBoundingClientRect();
    if ((r.width / z < 24 || r.height / z < 24) && !["checkbox", "radio"].includes(e.type) && res.small.length < 10)
      res.small.push(name(e) + " " + Math.round(r.width / z) + "x" + Math.round(r.height / z) + " '" + (e.textContent || e.value || "").trim().slice(0, 14) + "'");
  }
  return res;
})()`;

const report = [];
const failures = [];
const note = (cond, what) => { if (!cond) failures.push(what); };
for (const [w, h] of SIZES) {
  await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url });
  await sleep(2500);
  for (const v of VIEWS) {
    consoleErrors.length = 0;
    await evaluate(`location.hash = "#${v}"`);
    await sleep(1800);
    const r = await evaluate(PROBE);
    const shot = await send("Page.captureScreenshot", { format: "png" });
    writeFileSync(join(out, `${v}-${w}.png`), Buffer.from(shot.result.data, "base64"));
    const row = { view: v, size: `${w}x${h}`, zoom: r.zoom, overflow: r.scrollW > r.vw + 1 ? `scrollWidth ${r.scrollW} > ${r.vw}` : "ok",
      over: r.over, small: r.small, errors: [...new Set(consoleErrors)].slice(0, 4) };
    report.push(row);
    note(row.overflow === "ok", `${v} ${w}: page wider than the window (${row.overflow})`);
    note(!row.over.length, `${v} ${w}: content past the right edge: ${row.over.join(", ")}`);
    note(!row.errors.length, `${v} ${w}: console errors: ${row.errors.join(" | ")}`);
    note(r.vscroll <= 2, `${v} ${w}: the page body is ${r.vscroll}px taller than the window and the window clips it (the zoomed body should fill the window exactly)`);
  }
  if (flag("interact")) {
    await evaluate(`location.hash = "#chat"`);
    await sleep(600);
    await key("2", "Digit2", 50, 1);                                  // Alt+2 = the second tab
    await sleep(500);
    note((await evaluate(`document.body.dataset.view`)) === "agent", `${w}: Alt+2 did not open the second view`);
    await key("k", "KeyK", 75, 2);                                    // Ctrl+K
    await sleep(500);
    const open = await evaluate(`(() => { const d = document.getElementById("palette"); return { open: !!d && d.open, items: document.querySelectorAll("#pal-list .pal-item").length, focus: document.activeElement && document.activeElement.id }; })()`);
    note(open.open && open.items >= 7 && open.focus === "pal-input", `${w}: the palette did not open properly ${JSON.stringify(open)}`);
    await evaluate(`(() => { const i = document.getElementById("pal-input"); i.value = "plan"; i.dispatchEvent(new Event("input")); })()`);
    await sleep(300);
    const shot = await send("Page.captureScreenshot", { format: "png" });
    writeFileSync(join(out, `palette-${w}.png`), Buffer.from(shot.result.data, "base64"));
    const filtered = await evaluate(`[...document.querySelectorAll("#pal-list .pal-item")].map(li => li.textContent)`);
    note(filtered.length >= 1 && filtered.length < open.items, `${w}: typing in the palette did not filter (${filtered.length} of ${open.items})`);
    await key("Enter", "Enter", 13);
    await sleep(500);
    note(!(await evaluate(`document.getElementById("palette").open`)), `${w}: Enter did not close the palette`);
    await key("k", "KeyK", 75, 2);
    await sleep(300);
    await key("Escape", "Escape", 27);
    await sleep(300);
    note(!(await evaluate(`document.getElementById("palette").open`)), `${w}: Escape did not close the palette`);
  }
}
console.log(JSON.stringify({ browser, out, failures, report }, null, 1));
ws.close();
proc.kill();
process.exit(flag("strict") && failures.length ? 1 : 0);
