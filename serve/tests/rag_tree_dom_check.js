"use strict";
// Drives the tree of an archive's documents in a real headless browser at three screen sizes: node rag_tree_dom_check.js <url> <browser exe> <folder for the projects>.
// Prints [{name, ok, error}] as JSON (read by test_rag_tree_page.py). Real files in nested folders, a real source, and every action checked against what the server then holds.
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE, BASE] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const get = async p => (await fetch(URL + p)).json();
const post = async (p, body) => { const r = await fetch(URL + p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }); return { ok: r.ok, data: await r.json() }; };
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];
const TEXT = "The wind carries the boat along the shore, and the delta is wide. ".repeat(8);

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "rgt-"));
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
    const fits = what => ev(`const small = qa("#rg-root .rs-tree button, #rg-root .rs-tree select, #rg-root .rs-tree input:not([type=checkbox])").filter(e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0 && b.height < 43.5; }).map(e => (e.getAttribute("aria-label") || e.textContent.trim() || e.id).slice(0, 40));
      return JSON.stringify({ wide: document.documentElement.scrollWidth > innerWidth + 1, small });`).then(v => {
      const f = JSON.parse(v);
      if (f.wide) throw new Error(what + ": the page scrolls sideways");
      if (f.small.length) throw new Error(what + ": controls under 44 px: " + f.small.join(" | "));
    });
    const cb = nid => `q('[id="rs-cb-${nid}"]')`;
    const nodeIds = () => ev(`return qa("#rg-root .rs-node").map(n => n.dataset.node)`);
    const treeNow = async aid => (await get(`api/rag/archives/${aid}/tree`)).tree;
    const find = (roots, nid) => { for (const n of roots) { if (n.id === nid) return n; const f = find(n.children || [], nid); if (f) return f; } return null; };
    const until = async (fn, what, tries = 60) => { for (let i = 0; i < tries; i++) { if (await fn()) return true; await sleep(250); } throw new Error("the server never held: " + what); };
    const jobDone = async jid => { for (let i = 0; i < 120; i++) { const j = (await get(`api/rag/jobs/${jid}`)).job; if (j.state !== "running") return j; await sleep(250); } throw new Error("the job did not end"); };
    const click = (sel, text) => ev(`const b = [...qa(${JSON.stringify(sel)})].find(x => x.textContent.trim() === ${JSON.stringify(text)} || (x.getAttribute("aria-label") || "") === ${JSON.stringify(text)}); if (!b) throw new Error("no " + ${JSON.stringify(sel)} + " " + ${JSON.stringify(text)}); b.click(); return 1;`);
    const typeIn = (sel, value) => ev(`const i = q(${JSON.stringify(sel)}); if (!i) throw new Error("no field " + ${JSON.stringify(sel)}); i.value = ${JSON.stringify(value)}; i.dispatchEvent(new Event("input", { bubbles: true })); i.dispatchEvent(new Event("change", { bubbles: true })); return 1;`);

    for (const [vname, w, h, mobile] of VIEWPORTS) {
      const key = vname.replace(/[^a-z0-9]+/g, "-"), folder = path.join(BASE, "tree-" + key);
      for (const f of ["a.md", "sub/b.md", "sub/deeper/c.md"]) { fs.mkdirSync(path.dirname(path.join(folder, f)), { recursive: true }); fs.writeFileSync(path.join(folder, f), `# ${f}\n\n${TEXT}`); }
      const made = await post("api/rag/projects", { path: folder, name: "Tree " + key });
      const pk = made.data.created;
      const arch = await post("api/rag/archives", { name: "Docs " + key, project: pk });
      const aid = arch.data.archive.id;
      const src = await post(`api/rag/archives/${aid}/sources`, { kind: "folder", path: folder });
      if (!src.ok) throw new Error("the test could not add a source: " + JSON.stringify(src.data));
      const sid = src.data.source.id;
      const first = await post(`api/rag/archives/${aid}/refresh`, {});
      const j = await jobDone(first.data.job.id);
      if (j.state !== "done") throw new Error("the first update failed: " + JSON.stringify(j));

      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      await send("Page.navigate", { url: URL + "?lang=en" });
      await sleep(1500);
      await waitFor(`!!q("#tab-rag") && typeof view === "function"`, "the page");
      await ev(`q("#tab-rag").click(); return 1;`);
      await waitFor(`!!q("#rg-root .rg-projects")`, "the projects column");
      await ev(`[...qa("#rg-root .rg-projects .rg-pick")].find(b => b.textContent.includes("Tree ${key}")).click(); return 1;`);
      await waitFor(`qa("#rg-root .rg-side:not(.rg-projects) .rg-pick").some(b => b.textContent.includes("Docs ${key}"))`, "the archive in the list");
      await ev(`[...qa("#rg-root .rg-side:not(.rg-projects) .rg-pick")].find(b => b.textContent.includes("Docs ${key}")).click(); return 1;`);
      await waitFor(`!!q("#rg-root .rs-tree")`, "the tree");
      const sub = `d:${sid}:sub`, deeper = `d:${sid}:sub/deeper`;

      await check(`${vname}: the tree shows the kind, the source, the directories and the documents, all on, and fits the screen`, async () => {
        const ids = await nodeIds();
        for (const want of ["k:folder", `s:${sid}`, sub, deeper]) if (!ids.includes(want)) throw new Error("no node " + want + " in " + ids.join(","));
        if (ids.filter(i => i.startsWith("x:")).length !== 3) throw new Error("not three documents: " + ids.join(","));
        const states = await ev(`return qa('#rg-root .rs-nrow input[type=checkbox]').map(b => b.checked + ":" + b.indeterminate).join("|")`);
        if (!states.split("|").every(s => s === "true:false")) throw new Error("not everything is on: " + states);
        if (!/Folders on this PC/.test(await ev(`return q("#rg-root .rs-tree").textContent`))) throw new Error("the kind is not named");
        await fits("tree");
      });

      await check(`${vname}: a directory switched off turns off what is below it, its parent shows mixed, and the server holds it`, async () => {
        await ev(`${cb(sub)}.click(); return 1;`);
        await until(async () => find(await treeNow(aid), sub).state === "off", "the directory off");
        const t = await treeNow(aid);
        if (find(t, deeper).state !== "off" || find(t, `s:${sid}`).state !== "mixed") throw new Error("the server does not hold the cascade: " + JSON.stringify([find(t, deeper).state, find(t, `s:${sid}`).state]));
        await waitFor(`${cb(sub)}.checked === false && ${cb(`s:${sid}`)}.indeterminate === true`, "the page showing it");
        if (!(await ev(`return ${cb(deeper)}.disabled`))) throw new Error("a node under an off node can still be switched");
        await ev(`${cb(sub)}.click(); return 1;`);
        await until(async () => find(await treeNow(aid), deeper).state === "on", "everything back on");
        await waitFor(`${cb(sub)}.checked === true && ${cb(deeper)}.checked === true`, "the page showing it");
      });

      await check(`${vname}: a schedule by interval and one by days are set from the dialog, kept by the server, and the periodic routine is made`, async () => {
        await ev(`q('[id="rs-freq-s:${sid}"]').click(); return 1;`);
        await waitFor(`!!q("dialog[open] #rs-f-mode")`, "the schedule dialog");
        await typeIn("dialog[open] #rs-f-mode", "interval");
        await typeIn("dialog[open] #rs-f-every", "2");
        await typeIn("dialog[open] #rs-f-unit", "hours");
        await ev(`[...qa("dialog[open] button")].find(b => b.textContent.trim() === "Save").click(); return 1;`);
        await waitFor(`!q("dialog[open]")`, "the dialog closed");
        let t = await treeNow(aid);
        const f = find(t, `s:${sid}`).freq;
        if (!f || f.mode !== "interval" || f.every !== 2 || f.unit !== "hours") throw new Error("the interval was not kept: " + JSON.stringify(f));
        await waitFor(`q('[id="rs-freq-s:${sid}"]').textContent.includes("every 2 hours")`, "the schedule written beside the node");
        await until(async () => ((await get("api/routines")).routines || []).some(r => r.message === "due" && r.deliver_to && r.deliver_to.ref === aid), "the periodic routine");
        const routines = (await get("api/routines")).routines || [];
        if (!routines.some(r => r.message === "due" && r.deliver_to && r.deliver_to.ref === aid)) throw new Error("the periodic routine was not made; the page says: " + await ev(`return qa("#rg-root .rg-note").map(n => n.textContent).join(" / ")`));
        await ev(`q('[id="rs-freq-${sub}"]').click(); return 1;`);
        await waitFor(`!!q("dialog[open] #rs-f-mode")`, "the second dialog");
        await typeIn("dialog[open] #rs-f-mode", "occurrence");
        await typeIn("dialog[open] #rs-f-kind", "weekday");
        await ev(`q("dialog[open] #rs-f-d0").checked = true; q("dialog[open] #rs-f-d3").checked = true; return 1;`);
        await typeIn("dialog[open] #rs-f-at", "04:30");
        await ev(`[...qa("dialog[open] button")].find(b => b.textContent.trim() === "Save").click(); return 1;`);
        await waitFor(`!q("dialog[open]")`, "the second dialog closed");
        t = await treeNow(aid);
        const g = find(t, sub).freq;
        if (!g || g.mode !== "occurrence" || g.type !== "weekday" || JSON.stringify(g.days) !== "[0,3]" || g.at !== "04:30") throw new Error("the days were not kept: " + JSON.stringify(g));
        await fits("tree with schedules");
      });

      await check(`${vname}: reading a node again runs a job and removing one takes two presses, takes its documents out and keeps them out`, async () => {
        await ev(`q('[id="rs-again-${sub}"]').click(); return 1;`);
        await waitFor(`!q('[id="rs-again-${sub}"]').disabled`, "the read again finished");
        await ev(`q('[id="rs-rm-${deeper}"]').click(); return 1;`);
        await waitFor(`q('[id="rs-rm-${deeper}"]').textContent === "Press again to remove"`, "the second press asked for");
        if (find(await treeNow(aid), deeper) === null) throw new Error("the first press already removed it");
        await ev(`q('[id="rs-rm-${deeper}"]').click(); return 1;`);
        await waitFor(`!q('[id="rs-rm-${deeper}"]')`, "the directory gone from the page");
        if (find(await treeNow(aid), deeper) !== null) throw new Error("the server still has the node");
        const docs = (await get(`api/rag/archives/${aid}`)).archive.documents;
        if (docs !== 2) throw new Error("the archive holds " + docs + " documents, not 2");
        const upd = await post(`api/rag/archives/${aid}/refresh`, {});
        await jobDone(upd.data.job.id);
        if (find(await treeNow(aid), deeper) !== null || (await get(`api/rag/archives/${aid}`)).archive.documents !== 2) throw new Error("what was removed came back");
        if (!fs.existsSync(path.join(folder, "sub", "deeper", "c.md"))) throw new Error("the file was deleted");
      });

      await check(`${vname}: a file is chosen by its real path in the Browse dialog, a folder is followed from it, and a dropped file is copied into the archive`, async () => {
        const extra = path.join(BASE, "extra-" + key);
        fs.mkdirSync(extra, { recursive: true });
        fs.writeFileSync(path.join(extra, "picked.md"), "# picked\n\n" + TEXT);
        const docsNow = async () => (await get(`api/rag/archives/${aid}`)).archive.documents;
        const before = await docsNow();
        const browseTo = async () => {
          await waitFor(`!!q("#rg-browse") && !q("#rg-browse").disabled`, "the Browse button free again");
          await click("#rg-root #rg-browse", "Browse this PC…");
          await waitFor(`!!q("dialog[open] #rg-br-path")`, "the browse dialog; the button is " + await ev(`const b = q("#rg-browse"); return b ? "there, disabled=" + b.disabled : "missing"`) + "; the page says: " + await ev(`return (q("#rg-root .rg-say") || q("#rg-root [role=status]") || { textContent: "" }).textContent.slice(0, 200)`));
          await typeIn("dialog[open] #rg-br-path", extra);
          await click("dialog[open] button", "Go");
          await waitFor(`!!q('dialog[open] [id="rg-br-f-picked.md"]')`, "the file listed");
        };
        await browseTo();
        if (!(await ev(`return q("dialog[open] #rg-br-add").disabled`))) throw new Error("Add is on before a file is chosen");
        await ev(`q('dialog[open] [id="rg-br-f-picked.md"]').click(); return 1;`);
        await click("dialog[open] button", "Add the selected files");
        await until(async () => (await docsNow()) === before + 1, "the picked file added");
        await browseTo();
        await click("dialog[open] button", "Follow this folder");
        await until(async () => ((await get(`api/rag/archives/${aid}/sync`)).sources || []).length === 2, "the folder followed as a source");
        await waitFor(`!!q("#rg-drop")`, "the drop zone");
        await ev(`const z = q("#rg-drop"); const dt = new DataTransfer(); dt.items.add(new File(["Dropped text. ".repeat(40)], "dropped-${key}.txt", { type: "text/plain" })); z.dispatchEvent(new DragEvent("drop", { dataTransfer: dt, bubbles: true, cancelable: true })); return 1;`);
        await until(async () => (await docsNow()) === before + 2, "the dropped file copied");
        if (!(await treeNow(aid)).some(n => n.id === "k:hand")) throw new Error("the files added by hand are not in their node");
        await fits("tree after adding");
      });

      await check(`${vname}: every button and every field on the Knowledge page has a suggestion, in the Browse window too`, async () => {
        await new Promise(r => setTimeout(r, 200));
        const bare = sel => ev(`return qa(${JSON.stringify(sel)}).filter(e => !e.title && e.type !== "hidden" && !e.closest("[hidden]")).map(e => (e.id || e.className || e.tagName) + ":" + (e.textContent || e.value || "").slice(0, 30)).slice(0, 8)`);
        const loose = await bare("#rg-root button, #rg-root input:not([type=checkbox]), #rg-root select, #rg-root textarea");
        if (loose.length) throw new Error("no suggestion on: " + loose.join(" | "));
        await waitFor(`!!q("#rg-browse") && !q("#rg-browse").disabled`, "the Browse button free again");
        await click("#rg-root #rg-browse", "Browse this PC…");
        await waitFor(`!!q("dialog[open] #rg-br-path")`, "the browse dialog");
        await new Promise(r => setTimeout(r, 200));
        const inDialog = await bare("dialog[open] button, dialog[open] input:not([type=checkbox])");
        await ev(`q("dialog[open]").close(); return 1;`);
        if (inDialog.length) throw new Error("no suggestion in the Browse window on: " + inDialog.join(" | "));
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
