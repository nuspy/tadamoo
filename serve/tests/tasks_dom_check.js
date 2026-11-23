"use strict";
// Drives the Tasks tab in a real headless browser at three screen sizes: node tasks_dom_check.js <url> <browser exe> <folder A> <folder B>.
// Prints [{name, ok, error}] as JSON (read by test_tasks_page.py). One page, actions in sequence on the controls a person uses, each checked against what the server then holds.
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE, DIR_A, DIR_B] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const get = async p => (await fetch(URL + p)).json();
const post = async (p, body) => (await fetch(URL + p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];
const flat = view => view.projects.flatMap(p => p.tasks.map(t => ({ ...t, projectName: p.name })));

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "tkc-"));
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
    const card = title => `qa("#tk-root .tk-item").find(li => li.querySelector(".tk-name") && li.querySelector(".tk-name").textContent === ${JSON.stringify(title)})`;
    const press = (title, label) => ev(`const li = ${card(title)}; if (!li) throw new Error("no card ${title}"); const b = [...li.querySelectorAll("button")].find(x => x.textContent.trim() === ${JSON.stringify(label)}); if (!b) throw new Error("no button ${label} in ${title}"); const d = b.closest("details"); if (d) d.open = true; b.scrollIntoView(); b.click(); return 1;`);
    const fits = what => ev(`const small = qa("#tk-root button, #tk-root input, #tk-root select, #tk-root textarea, #tk-root summary").filter(e => { const b = e.getBoundingClientRect(), box = e.type === "checkbox" && e.closest("label") ? e.closest("label").getBoundingClientRect() : b; return b.width > 0 && b.height > 0 && box.height < 43.5; }).map(e => e.textContent.trim().slice(0, 20) || e.id);
      return JSON.stringify({ wide: document.documentElement.scrollWidth > innerWidth + 1, small, shown: !!q("#tk-root") && q("#tk-root").offsetParent !== null });`).then(v => {
      const f = JSON.parse(v);
      if (!f.shown) throw new Error(what + ": the tab is not shown");
      if (f.wide) throw new Error(what + ": the page scrolls sideways");
      if (f.small.length) throw new Error(what + ": controls under 44 px: " + f.small.join(", "));
    });
    const openTab = async () => { await waitFor(`!!q("#tab-tasks") && typeof view === "function"`, "the page"); await ev(`q("#tab-tasks").click(); return 1;`); await waitFor(`qa("#tk-root .tk-item").length >= 4`, "the tasks"); };
    const titles = async () => flat(await get("api/register")).map(t => t.title);

    // the world: two projects, one with a running job with subtasks and one waiting on the person, the other blocked, and one task a plan commands
    await post("api/agent/projects", { path: DIR_A }); await post("api/agent/projects", { path: DIR_B });
    const job = (await post("api/register/add", { project: DIR_A, title: "Move the site", state: "running", missing: "the DNS change", subtasks: [{ title: "Copy files" }, { title: "Change DNS", state: "waiting", missing: "the registrar login" }] })).id;
    await post("api/register/update", { project: DIR_A, id: job, git_branch: "feat/site-move", commits: ["abcdef1234", "not a commit"] });
    const build = (await post("api/register/add", { project: DIR_B, title: "Fix the build", state: "blocked", description: "fails on CI" })).id;
    const planned = (await post("api/register/add", { project: DIR_B, title: "Make the briefing" })).id;
    await post("api/register/link-plan", { project: DIR_B, id: planned, plan_id: "briefing" });

    for (const [vname, w, h, mobile] of VIEWPORTS) {
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      await send("Page.navigate", { url: URL + "?lang=en" });
      await sleep(1500);
      await openTab();

      await check(`${vname}: the open tasks of every project are a tree with states, notes, a plan line, and what waits for the person stands out`, async () => {
        const view = await ev(`return JSON.stringify(qa("#tk-root .tk-item").map(li => ({ name: li.querySelector(".tk-name").textContent, state: li.dataset.state, text: li.textContent, d: li.style.getPropertyValue("--d") })))`).then(JSON.parse);
        const by = n => view.find(x => x.name === n);
        if (!by("Move the site") || by("Move the site").state !== "running" || !/Still missing: the DNS change/.test(by("Move the site").text)) throw new Error("job: " + JSON.stringify(by("Move the site")));
        if (by("Copy files").d !== "1" || by("Change DNS").state !== "waiting" || !/Waiting for you/.test(by("Change DNS").text)) throw new Error("subtasks: " + JSON.stringify([by("Copy files"), by("Change DNS")]));
        if (!by("Plan “Briefing”") || by("Plan “Briefing”").state !== "waiting" || !/1\/3 steps/.test(by("Plan “Briefing”").text) || /Make the briefing/.test(by("Plan “Briefing”").text)) throw new Error("plan line: " + JSON.stringify(by("Plan “Briefing”")));
        const gitLinks = await ev(`return JSON.stringify(qa("#tk-root a.tk-git").map(a => [a.textContent, a.getAttribute("href"), a.target]))`).then(JSON.parse);
        const gitCodes = await ev(`return JSON.stringify(qa("#tk-root code.tk-git").map(c => c.textContent))`).then(JSON.parse);
        const REPO = "https://github.com/nuspy/tadamoo";                     // the test's folder has this remote when git is there; without git the same words show as plain text
        if (gitLinks.length) {
          if (JSON.stringify(gitLinks) !== JSON.stringify([["feat/site-move", REPO + "/tree/feat/site-move", "_blank"], ["abcdef12", REPO + "/commit/abcdef1234", "_blank"]])) throw new Error("branch and commit links: " + JSON.stringify(gitLinks));
        } else if (JSON.stringify(gitCodes) !== JSON.stringify(["feat/site-move", "abcdef12"])) throw new Error("branch and commit text: " + JSON.stringify(gitCodes));
        const banner = await ev(`return q("#tk-root .tk-banner") ? q("#tk-root .tk-banner").textContent : ""`);
        if (!/2 waiting for you/.test(banner)) throw new Error("banner: " + banner);
        const outline = await ev(`return getComputedStyle(${card("Change DNS")}).borderLeftColor + "|" + getComputedStyle(${card("Copy files")}).borderLeftColor`);
        const [a, b] = outline.split("|");
        if (a === b) throw new Error("a task that waits for the person looks like the others");
        const stray = await ev(`return /\\b(null|undefined|NaN)\\b/.test(q("#tk-root").textContent)`);
        if (stray) throw new Error("a value that is not text is shown");
        await fits("tasks tab");
      });

      await check(`${vname}: filters by project and by state`, async () => {
        await ev(`const s = q("#tk-state"); s.value = "blocked"; s.dispatchEvent(new Event("change", { bubbles: true })); return 1;`);
        await waitFor(`qa("#tk-root .tk-item").length === 1 && qa("#tk-root .tk-name")[0].textContent === "Fix the build"`, "only the blocked task");
        await ev(`const s = q("#tk-state"); s.value = ""; s.dispatchEvent(new Event("change", { bubbles: true })); const p = q("#tk-project"); p.value = ${JSON.stringify(DIR_A)}; p.dispatchEvent(new Event("change", { bubbles: true })); return 1;`);
        await waitFor(`qa("#tk-root .tk-sec").length === 1 && qa("#tk-root .tk-item").length === 3`, "only the first project");
        await ev(`const p = q("#tk-project"); p.value = "all"; p.dispatchEvent(new Event("change", { bubbles: true })); return 1;`);
        await waitFor(`qa("#tk-root .tk-sec").length === 2`, "both projects again");
        await fits("filtered");
      });

      await check(`${vname}: a task is added, edited, given a subtask, moved and deleted, and what the person did is marked theirs`, async () => {
        const proj = (await get("api/register")).projects[0];
        await ev(`const sec = qa("#tk-root .tk-sec")[0]; [...sec.querySelectorAll(".tk-sechead button")].find(b => b.textContent.trim() === "Add a task").click(); return 1;`);
        await waitFor(`!!q("#tk-root .tk-sec .tk-form input")`, "the form");
        await ev(`const i = q("#tk-root .tk-sec .tk-form input"); i.value = "Write the report"; i.dispatchEvent(new Event("input", { bubbles: true })); i.form.querySelector("button[type=submit]").click(); return 1;`);
        await waitFor(`!!${card("Write the report")}`, "the new task");
        let row = flat(await get("api/register")).find(t => t.title === "Write the report");
        if (!row || row.by !== "user" || row.state !== "queued") throw new Error("not stored as the person's: " + JSON.stringify(row));
        await press("Write the report", "Edit");
        await waitFor(`!!${card("Write the report")}.querySelector("form")`, "the edit form");
        await ev(`const f = ${card("Write the report")}.querySelector("form"); const set = (sel, v) => { const e = f.querySelector(sel); e.value = v; e.dispatchEvent(new Event("input", { bubbles: true })); }; set("input", "Write the final report"); set("textarea[id^=tk-e-m-]", "the summary"); const s = f.querySelector("select"); s.value = "waiting"; s.dispatchEvent(new Event("change", { bubbles: true })); f.querySelector("button[type=submit]").click(); return 1;`);
        await waitFor(`!!${card("Write the final report")}`, "the edited title");
        row = flat(await get("api/register")).find(t => t.title === "Write the final report");
        if (!row || row.missing !== "the summary" || row.state !== "waiting" || row.by !== "user") throw new Error("edit not stored: " + JSON.stringify(row));
        if (await ev(`return ${card("Write the final report")}.dataset.state`) !== "waiting") throw new Error("not shown as waiting");
        await press("Write the final report", "Add a subtask");
        await waitFor(`!!${card("Write the final report")}.querySelector("form")`, "the subtask form");
        await ev(`const f = ${card("Write the final report")}.querySelector("form"); const i = f.querySelector("input"); i.value = "Outline"; i.dispatchEvent(new Event("input", { bubbles: true })); f.querySelector("button[type=submit]").click(); return 1;`);
        await waitFor(`!!${card("Outline")} && ${card("Outline")}.style.getPropertyValue("--d") === "1"`, "the subtask under it");
        const topOf = async () => (await get("api/register")).projects.find(x => x.path === proj.path).tasks.filter(x => x.depth === 0).map(x => x.title);
        const before = await topOf();
        await press("Write the final report", "Move up");
        for (let i = 0; i < 40 && (await topOf()).join("|") === before.join("|"); i++) await sleep(250);
        const mine = await topOf();
        if (mine.indexOf("Write the final report") !== before.indexOf("Write the final report") - 1) throw new Error("order not saved: " + before.join("|") + " -> " + mine.join("|"));
        await fits("edited");
        await press("Write the final report", "Delete");
        await waitFor(`!!${card("Write the final report")}.querySelector(".tk-ask")`, "the question");
        if (!/1 subtasks/.test(await ev(`return ${card("Write the final report")}.querySelector(".tk-ask").textContent`))) throw new Error("the question does not say what goes with it");
        await ev(`[...${card("Write the final report")}.querySelectorAll("button")].find(b => b.textContent.trim() === "Yes, delete it").click(); return 1;`);
        await waitFor(`!${card("Write the final report")} && !${card("Outline")}`, "gone");
        if ((await titles()).some(t => /final report|Outline/.test(t))) throw new Error("still stored");
        if (/final report|Outline/.test(fs.readFileSync(path.join(proj.path, ".tadamoo", "tasks.json"), "utf8"))) throw new Error("kept in the file");
      });

      await check(`${vname}: Resume in chat hands the project and the words that make the agent take the task over to the chat`, async () => {
        await ev(`window.__calls = []; window.__real = window.agentChat.startWith; window.agentChat.startWith = async (ws, text) => { window.__calls.push([ws, text]); return true; }; return 1;`);
        await press("Fix the build", "Resume in chat");
        await waitFor(`window.__calls.length === 1`, "the call");
        const [ws, text] = await ev(`return window.__calls[0]`);
        if (ws !== DIR_B || !text.includes(build) || !/task_resume/.test(text)) throw new Error("wrong hand-over: " + ws + " | " + text);
        await press("Plan “Briefing”", "Resume the plan in chat");
        await waitFor(`window.__calls.length === 2`, "the plan call");
        if (!/plan "Briefing" \(briefing\)/.test(await ev(`return window.__calls[1][1]`))) throw new Error("the plan is not named");
        await ev(`window.agentChat.startWith = async () => false; return 1;`);
        await press("Fix the build", "Resume in chat");
        await waitFor(`/could not be started/.test(q("#tk-root .tk-msg").textContent)`, "the message when the chat cannot start");
        await ev(`window.agentChat.startWith = window.__real; return 1;`);
      });
      if (pageErrors.length) out.push({ name: `${vname}: no script error`, ok: false, error: pageErrors.splice(0).join(" ;; ") });
    }
  } finally {
    try { proc.kill(); } catch (e) { /* gone */ }
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* in use */ }
  }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "script", ok: false, error: String(e && e.stack || e) }])); });
