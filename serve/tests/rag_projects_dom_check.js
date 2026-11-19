"use strict";
// Drives the projects of the Knowledge tab in a real headless browser at three screen sizes: node rag_projects_dom_check.js <url> <browser exe> <folder for the projects>.
// Prints [{name, ok, error}] as JSON (read by test_rag_projects_page.py). One page, actions in sequence on the buttons a person presses, each checked against what the server then holds.
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE, BASE] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const get = async p => (await fetch(URL + p)).json();
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "rgp-"));
  const proc = spawn(EXE, ["--headless=new", "--lang=en-US", "--disable-gpu", `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, "--window-size=1440,900", "about:blank"], { stdio: "ignore" });
  const out = [];
  try {
    let targets = [];
    for (let i = 0; i < 40 && !targets.length; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch (e) { await sleep(250); } }
    const ws = new WebSocket(targets.find(t => t.type === "page").webSocketDebuggerUrl);
    await new Promise(r => { ws.onopen = r; });
    let id = 0; const waits = new Map();
    const pageErrors = [];
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
    const waitFor = async (expr, what, tries = 200) => { for (let i = 0; i < tries; i++) { if (await ev(`return (${expr});`)) return true; await sleep(250); } throw new Error(`never: ${what}`); };
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message + (pageErrors.length ? " | page errors: " + pageErrors.splice(0).join(" ;; ") : "") }); } };
    const fits = what => ev(`const small = qa("#rg-root button, #rg-root input:not([type=checkbox]), #rg-root select").filter(e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0 && b.height < 43.5; }).map(e => (e.getAttribute("aria-label") || e.textContent.trim() || e.id).slice(0, 40));
      return JSON.stringify({ wide: document.documentElement.scrollWidth > innerWidth + 1, small, shown: !!q("#rg-root") && q("#rg-root").offsetParent !== null });`).then(v => {
      const f = JSON.parse(v);
      if (!f.shown) throw new Error(what + ": the tab is not shown");
      if (f.wide) throw new Error(what + ": the page scrolls sideways");
      if (f.small.length) throw new Error(what + ": controls under 44 px: " + f.small.join(" | "));
    });
    const projectsNow = async () => (await get("api/rag/projects")).projects;
    const click = (sel, text) => ev(`const b = [...qa(${JSON.stringify(sel)})].find(x => x.textContent.trim() === ${JSON.stringify(text)} || (x.getAttribute("aria-label") || "") === ${JSON.stringify(text)}); if (!b) throw new Error("no " + ${JSON.stringify(sel)} + " " + ${JSON.stringify(text)}); b.click(); return 1;`);
    const typeIn = (sel, value) => ev(`const i = q(${JSON.stringify(sel)}); if (!i) throw new Error("no field " + ${JSON.stringify(sel)}); i.value = ${JSON.stringify(value)}; i.dispatchEvent(new Event("input", { bubbles: true })); return 1;`);

    for (const [vname, w, h, mobile] of VIEWPORTS) {
      const key = vname.replace(/[^a-z0-9]+/g, "-"), folder = path.join(BASE, "proj-" + key);
      fs.mkdirSync(folder, { recursive: true });
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      await send("Page.navigate", { url: URL + "?lang=en" });
      await sleep(1500);
      await waitFor(`!!q("#tab-rag") && typeof view === "function"`, "the page");
      await ev(`q("#tab-rag").click(); return 1;`);
      await waitFor(`!!q("#rg-root .rg-projects")`, "the projects column");
      await sleep(800);                                   // the first-run embeddings wizard, if it offers itself, opens asynchronously and would otherwise block every click behind it
      await ev(`const skip = [...qa("dialog[open] button")].find(b => b.textContent.trim() === "Not now"); if (skip) skip.click(); return 1;`);
      await sleep(200);

      await check(`${vname}: the projects are a list in the left column with the archives below, in separate containers, and everything fits the screen`, async () => {
        const text = await ev(`return q("#rg-root .rg-projects").textContent`);
        if (!/Chat/.test(text)) throw new Error("the Chat project is not listed: " + text);
        if ((await ev(`return qa("#rg-root .rg-projects .rg-iconbtn").length`)) !== 0) throw new Error("the Chat's own project must have no rename or delete");
        if ((await ev(`return qa("#rg-root #rg-project").length`)) !== 0) throw new Error("the dropdown is still there");
        if ((await ev(`return qa("#rg-root .rg-box").length`)) < 3) throw new Error("the groups are not in containers of their own");
        const order = await ev(`return [...q("#rg-root .rg-left").children].map(c => c.className.split(" ")[1] || c.className).join("|")`);
        if (order !== "rg-projects|") { if (!/^rg-projects/.test(order) || order.split("|").length !== 2) throw new Error("the left column is not projects then archives: " + order); }
        await fits("knowledge tab");
      });

      await check(`${vname}: a project is made from a folder, renamed, and the server keeps both`, async () => {
        await click("#rg-root .rg-newproj", "New project");
        await waitFor(`!!q("dialog[open] #rg-pj-path")`, "the new project dialog");
        await typeIn("#rg-pj-path", folder);
        await typeIn("#rg-pj-name", "Delta " + key);
        await click("dialog[open] button", "Make the project");
        await waitFor(`qa("#rg-root .rg-projects .rg-pname").some(e => e.textContent === "Delta ${key}")`, "the new project in the list");
        if (!(await projectsNow()).some(p => p.name === "Delta " + key)) throw new Error("the server does not have the project");
        await click("#rg-root .rg-projects .rg-iconbtn", "Rename: Delta " + key);
        await waitFor(`!!q("dialog[open] #rg-pj-rename")`, "the rename dialog");
        await typeIn("#rg-pj-rename", "Renamed " + key);
        await click("dialog[open] button", "Save");
        await waitFor(`qa("#rg-root .rg-projects .rg-pname").some(e => e.textContent === "Renamed ${key}")`, "the new name");
        if (!(await projectsNow()).some(p => p.name === "Renamed " + key)) throw new Error("the server kept the old name");
      });

      await check(`${vname}: deleting a project asks what goes, then its name typed by hand, and removes its archives but not its folder`, async () => {
        await ev(`[...qa("#rg-root .rg-projects .rg-pick")].find(b => b.textContent.includes("Renamed ${key}")).click(); return 1;`);
        await waitFor(`qa("#rg-root .rg-projects .rg-pick").some(b => b.textContent.includes("Renamed ${key}") && b.getAttribute("aria-current") === "true")`, "the project picked");
        const pk = (await projectsNow()).find(p => p.name === "Renamed " + key).key;
        const made = await fetch(URL + "api/rag/archives", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "Notes " + key, project: pk }) });
        if (!made.ok) throw new Error("the test could not make an archive");
        await click("#rg-root .rg-projects .rg-iconbtn", "Delete: Renamed " + key);
        await waitFor(`!!q("dialog[open]") && /archives/.test(q("dialog[open]").textContent)`, "the first dialog, saying what goes");
        if (!/folder and its files stay/.test(await ev(`return q("dialog[open]").textContent`))) throw new Error("the first dialog does not say the files stay");
        await click("dialog[open] button", "Continue");
        await waitFor(`!!q("dialog[open] #rg-pj-typed")`, "the second dialog");
        const off = () => ev(`return [...qa("dialog[open] button")].find(b => b.textContent.startsWith("Delete the project")).disabled`);
        if (!(await off())) throw new Error("the delete button is on before the name is typed");
        await typeIn("#rg-pj-typed", "wrong name");
        if (!(await off())) throw new Error("the delete button is on for a wrong name");
        await typeIn("#rg-pj-typed", "Renamed " + key);
        if (await off()) throw new Error("the delete button is off for the right name");
        await click("dialog[open] button", "Delete the project and its archives");
        await waitFor(`!qa("#rg-root .rg-projects .rg-pname").some(e => e.textContent === "Renamed ${key}")`, "the project gone from the list");
        if ((await projectsNow()).some(p => p.name === "Renamed " + key)) throw new Error("the server still has the project");
        if (((await get("api/rag/catalog")).archives || []).some(a => a.name === "Notes " + key)) throw new Error("its archive was not deleted");
        if (!fs.existsSync(folder)) throw new Error("the folder was deleted");
      });
    }
    await send("Page.navigate", { url: "about:blank" });
  } catch (e) {
    out.push({ name: "run", ok: false, error: String(e && e.message || e) });
  } finally {
    try { proc.kill(); } catch (e) { /* gone */ }
    await sleep(500);
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* busy */ }
  }
  console.log(JSON.stringify(out));
}
main().then(() => process.exit(0));
