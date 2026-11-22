"use strict";
// Drives the update window of Settings > Imported in a real headless browser at three screen sizes: node updatemerge_dom_check.js <url> <browser exe> <source folder of a skill> <data folder>.
// Prints [{name, ok, error}] as JSON (read by test_updatemerge_page.py). A skill is imported, edited here and changed at its source; each action is the button a person presses, and
// each result is checked against what the server then holds (the files of the copy). Nothing is downloaded and no model is needed.
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE, SRC, DATA] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const post = async (p, body) => (await fetch(URL + p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];
const LINES = ["One.", "Two.", "Three.", "Four.", "Five.", "Six.", "Seven."];
const md = lines => `---\nname: news-research\ndescription: Reads a thing.\n---\n\n${lines.join("\n")}\n`;
const COPY = path.join(DATA, "capabilities", "imported", "skills", "news-research");
const swap = (lines, from, to) => lines.map(l => (l === from ? to : l));

// the skill is imported from its folder, then edited in the copy and changed at the source: SKILL.md both sides on the same line (a choice), notes.md on different lines (joined), new.md only upstream
async function reset() {
  await post("api/import/skill/delete", { name: "news-research" });
  fs.rmSync(SRC, { recursive: true, force: true }); fs.mkdirSync(SRC, { recursive: true });
  fs.writeFileSync(path.join(SRC, "SKILL.md"), md(LINES)); fs.writeFileSync(path.join(SRC, "notes.md"), LINES.join("\n") + "\n");
  const r = await post("api/import/skill/import", { path: SRC });
  if (!r.imported) throw new Error("the skill was not imported: " + JSON.stringify(r));
  fs.writeFileSync(path.join(COPY, "SKILL.md"), md(swap(LINES, "Four.", "Four, mine.")));
  fs.writeFileSync(path.join(COPY, "notes.md"), swap(LINES, "Two.", "Two, mine.").join("\n") + "\n");
  fs.writeFileSync(path.join(SRC, "SKILL.md"), md(swap(LINES, "Four.", "Four, theirs.")));
  fs.writeFileSync(path.join(SRC, "notes.md"), swap(LINES, "Six.", "Six, theirs.").join("\n") + "\n");
  fs.writeFileSync(path.join(SRC, "new.md"), "only upstream\n");
}
const copyText = name => { try { return fs.readFileSync(path.join(COPY, name), "utf8").replace(/\r\n/g, "\n"); } catch (e) { return null; } };

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "umc-"));
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
    const waitFor = async (expr, what, tries = 120) => { for (let i = 0; i < tries; i++) { if (await ev(`return (${expr});`)) return true; await sleep(250); } throw new Error(`never: ${what}`); };
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message + (pageErrors.length ? " | page errors: " + pageErrors.splice(0).join(" ;; ") : "") }); } };
    const dlg = `q("dialog.um-dlg[open]")`;
    /* no sideways scroll, and every control of the window is at least 44 px high */
    const fits = what => ev(`const d = ${dlg}; if (!d) return JSON.stringify({ shown: false });
      const small = qa("dialog.um-dlg button, dialog.um-dlg textarea, dialog.um-dlg summary, dialog.um-dlg input").filter(e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0 && b.height < 43.5; }).map(e => e.textContent.trim().slice(0, 20) || e.id);
      const box = d.getBoundingClientRect();
      return JSON.stringify({ shown: true, wide: document.documentElement.scrollWidth > innerWidth + 1, inside: box.left >= -1 && box.right <= innerWidth + 1, small });`).then(v => {
      const f = JSON.parse(v);
      if (!f.shown) throw new Error(what + ": the window is not open");
      if (f.wide) throw new Error(what + ": the page scrolls sideways");
      if (!f.inside) throw new Error(what + ": the window goes out of the screen");
      if (f.small.length) throw new Error(what + ": controls under 44 px: " + f.small.join(", "));
    });
    const openList = async () => {
      await waitFor(`!!q("#tab-settings") && typeof view === "function"`, "the page");
      await ev(`q("#tab-settings").click(); return 1;`);
      await waitFor(`!!q("#st-imp-body")`, "the Imported section");
      await ev(`document.dispatchEvent(new CustomEvent("tadamoo:view", { detail: "settings" })); return 1;`);
      await waitFor(`qa("#st-imp-body .im-item").some(li => li.textContent.includes("news-research"))`, "the skill among the imported");
    };
    const openUpdate = async () => {
      await ev(`const li = qa("#st-imp-body .im-item").find(x => x.textContent.includes("news-research")); const b = [...li.querySelectorAll("button")].find(x => x.textContent.trim() === "Re-import"); b.scrollIntoView(); b.click(); return 1;`);
      await waitFor(`!!${dlg} && qa("dialog.um-dlg .um-file").length >= 3`, "the update window with its files");
    };
    const rowOf = file => `qa("dialog.um-dlg .um-file").find(d => d.dataset.path === ${JSON.stringify(file)})`;
    const press = label => ev(`const b = qa("dialog.um-dlg .im-actions button").find(x => x.textContent.trim() === ${JSON.stringify(label)}); if (!b) throw new Error("no button ${label}: " + qa("dialog.um-dlg .im-actions button").map(x => x.textContent.trim()).join("|")); b.click(); return 1;`);

    for (const [vname, w, h, mobile] of VIEWPORTS) {
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      await reset();
      await send("Page.navigate", { url: URL + "?lang=en" });
      await sleep(1500);
      await openList();

      await check(`${vname}: the window says what the update does, file by file, with the three versions, and fits the screen`, async () => {
        await openUpdate();
        const info = JSON.parse(await ev(`return JSON.stringify({ title: q("#um-title").textContent, counts: q(".um-counts").textContent, rows: qa("dialog.um-dlg .um-file").map(d => [d.dataset.path, d.dataset.outcome, d.querySelector(".um-tag").textContent]),
          open: qa("dialog.um-dlg .um-file").filter(d => d.open).map(d => d.dataset.path), words: !!q(".um-words") || /No description in words/.test(q("dialog.um-dlg").textContent) });`));
        if (!/Update news-research/.test(info.title)) throw new Error("title: " + info.title);
        if (!/1 taken from the new version, 0 kept as you changed them, 1 joined inside the file, 1 need your choice/.test(info.counts)) throw new Error("counts: " + info.counts);
        const rows = Object.fromEntries(info.rows.map(r => [r[0], r[1]]));
        if (rows["SKILL.md"] !== "conflict" || rows["notes.md"] !== "merged" || rows["new.md"] !== "upstream") throw new Error("outcomes: " + JSON.stringify(info.rows));
        if (info.open.join() !== "SKILL.md") throw new Error("only the file that needs a choice opens by itself: " + info.open);
        if (!info.words) throw new Error("neither a description nor the note that there is none");
        const notes = await ev(`const d = ${rowOf("notes.md")}; d.open = true; return JSON.stringify([...d.querySelectorAll(".um-pane")].map(p => [p.dataset.version, (p.querySelector(".um-text") || {}).textContent || ""]));`).then(JSON.parse);
        const by = Object.fromEntries(notes);
        if (!/Two, mine\./.test(by.installed) || /Six, theirs\./.test(by.installed)) throw new Error("installed version: " + by.installed);
        if (!/Six, theirs\./.test(by.new) || /Two, mine\./.test(by.new)) throw new Error("new version: " + by.new);
        if (!/Two, mine\./.test(by.merged) || !/Six, theirs\./.test(by.merged)) throw new Error("merged version: " + by.merged);
        if (copyText("notes.md").includes("theirs")) throw new Error("something was written before the person applied");
        await fits("update window");
      });

      await check(`${vname}: a file both sides changed needs a choice, and the merge is applied only then`, async () => {
        const state = JSON.parse(await ev(`const a = q("#um-apply"); return JSON.stringify({ disabled: a.disabled, status: q("dialog.um-dlg .im-status").textContent, merged: ${rowOf("SKILL.md")}.querySelector("button[data-choice=merged]").disabled });`));
        if (!state.disabled || !/Choose for every file/.test(state.status)) throw new Error("apply is open with a conflict: " + JSON.stringify(state));
        if (!state.merged) throw new Error("the merge with markers can be chosen");
        const markers = await ev(`return ${rowOf("SKILL.md")}.querySelector(".um-pane[data-version=merged] .um-text").textContent`);
        if (!/<<<<<<< installed[\s\S]*Four, mine\.[\s\S]*=======[\s\S]*Four, theirs\.[\s\S]*>>>>>>> new/.test(markers)) throw new Error("the conflict is not shown with both sides: " + markers);
        await ev(`${rowOf("SKILL.md")}.querySelector("button[data-choice=ours]").click(); return 1;`);
        await waitFor(`!q("#um-apply").disabled`, "apply open after the choice");
        await fits("after the choice");
        await ev(`q("#um-apply").click(); return 1;`);
        await waitFor(`!${dlg}`, "the window closed after the merge");
        const skill = copyText("SKILL.md"), notes = copyText("notes.md");
        if (!/Four, mine\./.test(skill) || /Four, theirs\./.test(skill)) throw new Error("the choice (mine) was not kept: " + skill);
        if (!/Two, mine\./.test(notes) || !/Six, theirs\./.test(notes)) throw new Error("notes.md was not joined: " + notes);
        if (copyText("new.md") !== "only upstream\n") throw new Error("the new file did not arrive");
        const list = await ev(`return q("#st-imp-body").textContent`);
        if (/edited here/i.test(list.split("news-research")[1] || "")) throw new Error("the copy is still marked as edited after the merge");
      });

      await check(`${vname}: replacing with the new version needs two presses and drops the person's changes`, async () => {
        await reset();
        await send("Page.navigate", { url: URL + "?lang=en" }); await sleep(1500); await openList(); await openUpdate();
        await press("Replace with the new version (my changes are lost)");
        if (!await ev(`return !!${dlg} && q("#um-overwrite").textContent.includes("Press again")`)) throw new Error("replaced on the first press");
        if (!/Four, mine\./.test(copyText("SKILL.md"))) throw new Error("the copy changed on the first press");
        await ev(`q("#um-overwrite").click(); return 1;`);
        await waitFor(`!${dlg}`, "the window closed after the replacement");
        if (!/Four, theirs\./.test(copyText("SKILL.md")) || /mine/.test(copyText("notes.md"))) throw new Error("the new version was not written whole: " + copyText("SKILL.md"));
      });

      await check(`${vname}: cancelling leaves the copy exactly as it was`, async () => {
        await reset();
        await send("Page.navigate", { url: URL + "?lang=en" }); await sleep(1500); await openList();
        const before = copyText("SKILL.md") + copyText("notes.md");
        await openUpdate();
        await press("Cancel the update");
        await waitFor(`!${dlg}`, "the window closed");
        if (copyText("SKILL.md") + copyText("notes.md") !== before) throw new Error("the copy changed after cancelling");
        const left = await ev(`return qa("dialog.um-dlg").length`);
        if (left) throw new Error("the window was not removed");
      });

      await check(`${vname}: the instructions box is there and an empty one does nothing`, async () => {
        await reset();
        await send("Page.navigate", { url: URL + "?lang=en" }); await sleep(1500); await openList(); await openUpdate();
        const t = JSON.parse(await ev(`const ta = q("#um-instr"); const go = [...qa("dialog.um-dlg button")].find(b => b.textContent.trim() === "Redo the merge"); const before = qa("dialog.um-dlg .um-file").length; go.click(); await wait(400); return JSON.stringify({ has: !!ta, label: ta && ta.getAttribute("aria-label"), before, after: qa("dialog.um-dlg .um-file").length });`));
        if (!t.has || !/Tell the assistant/.test(t.label) || t.before !== t.after) throw new Error("instructions: " + JSON.stringify(t));
        await fits("instructions");
        await press("Cancel the update");
        await waitFor(`!${dlg}`, "closed");
      });

      await check(`${vname}: on a phone one version is shown at a time with tabs, on a desktop the three side by side`, async () => {
        await reset();
        await send("Page.navigate", { url: URL + "?lang=en" }); await sleep(1500); await openList(); await openUpdate();
        const lay = JSON.parse(await ev(`const d = ${rowOf("notes.md")}; d.open = true; const tabs = d.querySelector(".um-tabs"); const panes = [...d.querySelectorAll(".um-pane")].filter(p => p.offsetParent !== null).map(p => p.dataset.version);
          return JSON.stringify({ tabs: tabs.offsetParent !== null, panes });`));
        if (mobile && (!lay.tabs || lay.panes.join() !== "merged")) throw new Error("phone layout: " + JSON.stringify(lay));
        if (!mobile && (lay.tabs || lay.panes.length !== 3)) throw new Error("desktop layout: " + JSON.stringify(lay));
        if (mobile) {
          await ev(`const d = ${rowOf("notes.md")}; d.querySelector(".um-tab[data-version=new]").click(); return 1;`);
          const shown = await ev(`return qa("dialog.um-dlg .um-file").filter(d => d.open).flatMap(d => [...d.querySelectorAll(".um-pane")].filter(p => p.offsetParent !== null).map(p => p.dataset.version)).join()`);
          if (!/^new(,new)*$/.test(shown)) throw new Error("the tab did not change the version shown: " + shown);
        }
        await fits("layout");
        await press("Cancel the update");
        await waitFor(`!${dlg}`, "closed");
      });
      if (pageErrors.length) out.push({ name: `${vname}: no script error`, ok: false, error: pageErrors.splice(0).join(" ;; ") });
    }
  } finally {
    try { proc.kill(); } catch (e) { /* gone */ }
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* the browser may still hold it */ }
  }
  console.log(JSON.stringify(out));
  process.exit(0);
}
main().catch(e => { console.log(JSON.stringify([{ name: "the check itself", ok: false, error: String(e && e.stack || e).slice(0, 600) }])); process.exit(0); });
