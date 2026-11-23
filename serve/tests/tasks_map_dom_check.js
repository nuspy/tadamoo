"use strict";
// Drives the Tasks tab as the map of the development in a real headless browser at three screen sizes:
// node tasks_map_dom_check.js <url> <browser exe> <project folder>. Prints [{name, ok, error}] as JSON (read by test_tasks_page.py).
// Groups, the three views (tree, spine, history), closing and reopening, hiding and "Show hidden nodes", deleting for good, the notes and the new states in the edit form,
// only the tasks of one conversation (and the Chat's own button), and the roadmap files; each action checked against what the server then holds.
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE, DIR] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const get = async p => (await fetch(URL + p)).json();
const post = async (p, body) => (await fetch(URL + p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];
const tasksOf = async hidden => (await get("api/register?hidden=" + (hidden ? 1 : 0))).projects.flatMap(p => p.tasks);
const byTitle = async (title, hidden) => (await tasksOf(hidden)).find(t => t.title === title);

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "tkm-"));
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
    const waitFor = async (expr, what, tries = 200) => { for (let i = 0; i < tries; i++) { if (await ev(`return (${expr});`)) return true; await sleep(250); } throw new Error(`never: ${what}`); };
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message + (pageErrors.length ? " | page errors: " + pageErrors.splice(0).join(" ;; ") : "") }); } };
    const fits = what => ev(`const small = qa("#tk-root button, #tk-root select, #tk-root textarea, #tk-root summary, #tk-root input[type=text]").filter(e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0 && b.height < 43.5; }).map(e => e.textContent.trim().slice(0, 20) || e.id);
      const cut = qa("#tk-root .tk-node").filter(e => e.getBoundingClientRect().right > innerWidth + 1).length;
      return JSON.stringify({ wide: document.documentElement.scrollWidth > innerWidth + 1, small, cut });`).then(v => {
      const f = JSON.parse(v);
      if (f.wide) throw new Error(what + ": the page scrolls sideways");
      if (f.cut) throw new Error(what + ": a node goes out of the screen");
      if (f.small.length) throw new Error(what + ": controls under 44 px: " + f.small.join(", "));
    });
    const openTab = async () => { await waitFor(`!!q("#tab-tasks") && typeof view === "function"`, "the page"); await ev(`q("#tab-tasks").click(); return 1;`); await waitFor(`!!q("#tk-view-tree")`, "the tab"); };
    const view = async v => { await ev(`q("#tk-view-${v}").click(); return 1;`); await waitFor(`q("#tk-view-${v}").getAttribute("aria-pressed") === "true"`, "the " + v + " view"); await sleep(400); };
    const card = title => `qa("#tk-root .tk-item").find(li => li.querySelector(".tk-name") && li.querySelector(".tk-name").textContent === ${JSON.stringify(title)})`;
    const press = (scope, label) => ev(`const b = [...${scope}.querySelectorAll("button")].find(x => x.textContent.trim() === ${JSON.stringify(label)}); if (!b) throw new Error("no button ${label} in " + ${scope}.textContent.slice(0, 80)); const d = b.closest("details"); if (d) d.open = true; b.click(); return 1;`);

    // the world: one project with two groups, running / queued / review tasks with notes, and one task already closed
    await post("api/agent/projects", { path: DIR });
    const g1 = (await post("api/register/group-add", { project: DIR, title: "The engine" })).id, g2 = (await post("api/register/group-add", { project: DIR, title: "The phone" })).id;
    await post("api/register/add", { project: DIR, title: "Fuse the experts", state: "running", group: g1, description: "Fused at load", solved: "- read the paper\n- found a trap", decisions: "which format?", risks: "numbers may drift" });
    await post("api/register/add", { project: DIR, title: "Quantizers", group: g1 });
    await post("api/register/add", { project: DIR, title: "Messages while it works", state: "review", group: g2, solved: "buttons done" });
    const study = (await post("api/register/add", { project: DIR, title: "Study the format", group: g1, solved: "finished" })).id;
    await post("api/register/close", { project: DIR, id: study });

    for (const [vname, w, h, mobile] of VIEWPORTS) {
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      await send("Page.navigate", { url: URL + "?lang=en" });
      await sleep(1500);
      await openTab();

      await check(`${vname}: the tree is by group with progress, numbers, notes and the new states, and fits the screen`, async () => {
        await view("tree");
        await waitFor(`qa("#tk-root .tk-item").length >= 3`, "the tasks");
        const info = JSON.parse(await ev(`return JSON.stringify({ groups: qa("#tk-root .tk-ghead").map(g => g.textContent.replace(/\\s+/g, " ").trim()), names: qa("#tk-root .tk-name").map(n => n.textContent), nums: qa("#tk-root .tk-num").map(n => n.textContent),
          states: qa("#tk-root .tk-item").map(li => li.dataset.state), notes: card("Fuse the experts") ? 0 : 0 });`.replace("card(\"Fuse the experts\") ? 0 : 0", "0")));
        if (!/The engine.*2 open, 1 done/.test(info.groups[0]) || !/The phone.*1 open, 0 done/.test(info.groups[1])) throw new Error("groups: " + JSON.stringify(info.groups));
        if (info.names.join("|") !== "Fuse the experts|Quantizers|Messages while it works") throw new Error("names: " + info.names);
        if (info.nums.join() !== "#1,#2,#3") throw new Error("numbers: " + info.nums);
        if (info.states.join() !== "running,queued,review") throw new Error("states: " + info.states);
        const text = await ev(`return ${card("Fuse the experts")}.textContent;`);
        if (!/Done and problems solved/.test(text) || !/read the paper/.test(text) || !/which format\?/.test(text) || !/numbers may drift/.test(text)) throw new Error("the notes are not shown: " + text);
        if (!/Done, to commit/.test(await ev(`return ${card("Messages while it works")}.textContent;`))) throw new Error("the review state is not named");
        await fits("the tree");
      });

      await check(`${vname}: the spine shows the nodes in group order, the closed one among them, and a node's notes in the panel`, async () => {
        await view("spine");
        await waitFor(`qa("#tk-root .tk-node").length === 4`, "four nodes");
        const info = JSON.parse(await ev(`return JSON.stringify({ nodes: qa("#tk-root .tk-node").map(n => [n.querySelector(".tk-ntitle").textContent, n.dataset.state]), seps: qa("#tk-root .tk-sep").map(s => s.textContent), panel: q("#tk-root .tk-panel").textContent });`));
        if (info.nodes.map(n => n[0]).join("|") !== "Fuse the experts|Quantizers|Study the format|Messages while it works") throw new Error("order: " + JSON.stringify(info.nodes));
        if (info.nodes.map(n => n[1]).join() !== "running,queued,done,review") throw new Error("states: " + JSON.stringify(info.nodes));
        if (info.seps.join("|") !== "The engine|The phone" || !/Choose a node/.test(info.panel)) throw new Error(JSON.stringify(info));
        await ev(`qa("#tk-root .tk-node").find(n => /Fuse the experts/.test(n.textContent)).click(); return 1;`);
        await waitFor(`/numbers may drift/.test(q("#tk-root .tk-panel").textContent)`, "the panel");
        const panel = await ev(`return q("#tk-root .tk-panel").textContent;`);
        if (!/Fused at load/.test(panel) || !/found a trap/.test(panel) || !/which format\?/.test(panel) || !/Mark done/.test(panel)) throw new Error("panel: " + panel);
        await ev(`qa("#tk-root .tk-node").find(n => /Study the format/.test(n.textContent)).click(); return 1;`);
        await waitFor(`/Reopen/.test(q("#tk-root .tk-panel").textContent) && /Closed on/.test(q("#tk-root .tk-panel").textContent)`, "the closed node's panel");
        await fits("the spine");
        await ev(`qa("#tk-root .tk-node").find(n => /Study the format/.test(n.textContent)).click(); return 1;`);
      });

      await check(`${vname}: marking a task done puts it in the History and Reopen brings it back`, async () => {
        await view("tree");
        await waitFor(`!!${card("Quantizers")}`, "the task");
        await ev(`${card("Quantizers")}.querySelector(".tk-donebtn").click(); return 1;`);
        await waitFor(`!${card("Quantizers")} && /in the History/.test(q("#tk-root .tk-msg").textContent)`, "the task closed");
        if (await byTitle("Quantizers")) throw new Error("still open on the server");
        await view("history");
        await waitFor(`qa("#tk-root .tk-closedrow").length === 2`, "two closed tasks");
        const names = await ev(`return qa("#tk-root .tk-closedrow .tk-name").map(n => n.textContent).join("|");`);
        if (names !== "Quantizers|Study the format" || !/Closed on/.test(await ev(`return q("#tk-root .tk-closedrow").textContent;`))) throw new Error("history: " + names);
        await fits("the history");
        await ev(`const row = qa("#tk-root .tk-closedrow").find(r => /Quantizers/.test(r.textContent)); [...row.querySelectorAll("button")].find(b => b.textContent.trim() === "Reopen").click(); return 1;`);
        await waitFor(`qa("#tk-root .tk-closedrow").length === 1`, "reopened");
        const back = await byTitle("Quantizers");
        if (!back || back.state !== "queued") throw new Error("not open again on the server");
      });

      await check(`${vname}: hiding a node takes it out of the views, "Show hidden nodes" brings it back marked, Show makes it normal again`, async () => {
        await view("tree");
        await waitFor(`!!${card("Quantizers")}`, "the task");
        await press(card("Quantizers"), "Hide");
        await waitFor(`!${card("Quantizers")}`, "the node hidden");
        const hid = await byTitle("Quantizers", true);
        if (!hid || hid.hidden !== true || (await byTitle("Quantizers", false))) throw new Error("the server does not hide it");
        if (!/Show hidden nodes \(1\)/.test(await ev(`return q("#tk-root .tk-check").textContent;`))) throw new Error("the count of hidden nodes: " + await ev(`return q("#tk-root .tk-check").textContent;`));
        await view("spine");
        if (await ev(`return qa("#tk-root .tk-node").some(n => /Quantizers/.test(n.textContent));`)) throw new Error("a hidden node is in the spine");
        await view("tree");
        await ev(`q("#tk-hidden").click(); return 1;`);
        await waitFor(`!!${card("Quantizers")} && /Hidden/.test(${card("Quantizers")}.textContent)`, "the hidden node shown and marked");
        await press(card("Quantizers"), "Show");
        await waitFor(`!/Hidden/.test(${card("Quantizers")}.textContent)`, "the node normal again");
        if ((await byTitle("Quantizers", false)) === undefined) throw new Error("the server still hides it");
        await ev(`q("#tk-hidden").click(); return 1;`);
        await waitFor(`q("#tk-hidden").checked === false && !!${card("Quantizers")}`, "the checkbox off, the node still there");
      });

      await check(`${vname}: Delete asks first, removes the task for good (not into the History), and the edit form sets the new states and the three notes`, async () => {
        await post("api/register/add", { project: DIR, title: "Throwaway", group: g2 });
        await waitFor(`!!${card("Throwaway")}`, "the new task");
        await press(card("Throwaway"), "Delete");
        await waitFor(`!!${card("Throwaway")}.querySelector(".tk-ask")`, "the question");
        await ev(`[...${card("Throwaway")}.querySelectorAll(".tk-ask button")].find(b => /Yes, delete/.test(b.textContent)).click(); return 1;`);
        await waitFor(`!${card("Throwaway")}`, "deleted");
        const hist = (await get("api/register/history")).projects[0].history.map(r => r.title);
        if (await byTitle("Throwaway") || hist.includes("Throwaway")) throw new Error("it was kept somewhere: " + hist);
        await press(card("Quantizers"), "Edit");
        await waitFor(`!!${card("Quantizers")}.querySelector(".tk-form")`, "the edit form");
        await ev(`const f = ${card("Quantizers")}.querySelector(".tk-form"); const set = (sel, v) => { const e = f.querySelector(sel); e.value = v; e.dispatchEvent(new Event("input", { bubbles: true })); e.dispatchEvent(new Event("change", { bubbles: true })); };
          set("select[id^=tk-e-s-]", "decision"); set("textarea[id^=tk-e-n-solved-]", "- IQ4_NL done"); set("textarea[id^=tk-e-n-decisions-]", "keep IQ3?"); set("textarea[id^=tk-e-n-risks-]", "loses precision"); f.querySelector("button[type=submit]").click(); return 1;`);
        await waitFor(`/Open decision/.test(${card("Quantizers")}.textContent) && /keep IQ3\\?/.test(${card("Quantizers")}.textContent)`, "the saved task");
        const t = await byTitle("Quantizers");
        if (t.state !== "decision" || t.notes.solved !== "- IQ4_NL done" || t.notes.decisions !== "keep IQ3?" || t.notes.risks !== "loses precision") throw new Error(JSON.stringify(t));
        await post("api/register/update", { project: DIR, id: t.id, state: "queued", solved: "", decisions: "", risks: "" });
      });

      await check(`${vname}: a group is added, renamed and deleted from the page, and its tasks stay`, async () => {
        await view("tree");
        await ev(`[...q("#tk-root .tk-sechead").querySelectorAll("button")].find(b => b.textContent.trim() === "Add a group").click(); return 1;`);
        await waitFor(`!!q("#tk-g-new-f")`, "the group form");
        await ev(`const i = q("#tk-g-new-f"); i.value = "Scratch"; i.dispatchEvent(new Event("input", { bubbles: true })); i.form.querySelector("button[type=submit]").click(); return 1;`);
        await waitFor(`qa("#tk-root .tk-gname").some(g => g.textContent === "Scratch")`, "the new group");
        const head = `qa("#tk-root .tk-ghead").find(g => g.querySelector(".tk-gname").textContent === "Scratch")`;
        await press(head, "Rename the group");
        await waitFor(`!!q("[id^=tk-g-n-]")`, "the rename form");
        await ev(`const i = q("[id^=tk-g-n-]"); i.value = "Scratch pad"; i.dispatchEvent(new Event("input", { bubbles: true })); i.form.querySelector("button[type=submit]").click(); return 1;`);
        await waitFor(`qa("#tk-root .tk-gname").some(g => g.textContent === "Scratch pad")`, "the renamed group");
        await press(`qa("#tk-root .tk-ghead").find(g => g.querySelector(".tk-gname").textContent === "Scratch pad")`, "Delete the group (its tasks stay)");
        await waitFor(`!qa("#tk-root .tk-gname").some(g => /Scratch/.test(g.textContent))`, "the group gone");
        if (!(await byTitle("Fuse the experts"))) throw new Error("a task went with the group");
      });

      await check(`${vname}: the roadmap files are written from the register and read back, with a question first`, async () => {
        await press(`q("#tk-root .tk-sec")`, "Write docs/roadmap.json and roadmap.md");
        await waitFor(`/roadmap files are written/.test(q("#tk-root .tk-msg").textContent)`, "the message");
        const file = path.join(DIR, "docs", "roadmap.json");
        if (!fs.existsSync(file)) throw new Error("no file written");
        const data = JSON.parse(fs.readFileSync(file, "utf-8"));
        if (data.schema !== "roadmap-1" || !data.nodes.some(n => n.title === "Fuse the experts" && n.status === "run" && n.solved.includes("found a trap")) || !data.nodes.some(n => n.title === "Study the format" && n.status === "done")) throw new Error("the file: " + JSON.stringify(data.nodes.map(n => [n.title, n.status])));
        if (!fs.existsSync(path.join(DIR, "docs", "roadmap.md"))) throw new Error("no text written");
        await press(`q("#tk-root .tk-sec")`, "Read docs/roadmap.json");
        await waitFor(`/Read docs\\/roadmap.json into the register/.test(q("#tk-root .tk-sec").textContent)`, "the question");
        await ev(`[...q("#tk-root .tk-sec .tk-ask").querySelectorAll("button")].find(b => /Yes, read it/.test(b.textContent)).click(); return 1;`);
        await waitFor(`/roadmap file was read/.test(q("#tk-root .tk-msg").textContent)`, "read back");
        if ((await tasksOf(false)).filter(t => t.title === "Fuse the experts").length !== 1) throw new Error("a task was doubled by reading the file");
      });
    }

    await check("only the tasks of one conversation: the chip, the Chat's own Tasks button, and Show all", async () => {
      await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
      await send("Page.navigate", { url: URL + "?lang=en" });
      await sleep(1500);
      await waitFor(`!!q("#tab-chat") && !!window.tasksUi`, "the page");
      await ev(`q("#tab-chat").click(); return 1;`);
      await waitFor(`!!q("#tasks-open") && !q("#tasks-open").hidden`, "the Tasks button in the chat");
      if (!/Tasks/.test(await ev(`return q("#tasks-open").textContent;`))) throw new Error("the button has no name");
      await ev(`const p = q("#prompt"); p.value = "Hello"; p.dispatchEvent(new Event("input")); q("#send").click(); return 1;`);
      await waitFor(`!!window.agentChat && !!window.agentChat.current()`, "a conversation");
      const sid = await ev(`return window.agentChat.current();`);
      await ev(`q("#tasks-open").click(); return 1;`);
      await waitFor(`document.body.dataset.view === "tasks" && !!q("#tk-session")`, "the Tasks tab with the chip");
      await waitFor(`/carries no task/.test(q("#tk-root").textContent)`, "the empty note");
      await post("api/register/add", { project: DIR, title: "Carried here" });                       // made by the person: no conversation yet
      // a task of this conversation, made the way the agent makes one
      fs.mkdirSync(path.join(DIR, ".tadamoo"), { recursive: true });
      const file = path.join(DIR, ".tadamoo", "tasks.json"), doc = JSON.parse(fs.readFileSync(file, "utf-8"));
      doc.tasks.find(t => t.title === "Carried here").links.session = sid;
      fs.writeFileSync(file, JSON.stringify(doc));
      await ev(`window.tasksUi.refresh(); return 1;`);
      await waitFor(`qa("#tk-root .tk-item").length === 1 && qa("#tk-root .tk-name")[0].textContent === "Carried here"`, "only the conversation's task");
      await ev(`q("#tk-session button").click(); return 1;`);
      await waitFor(`!q("#tk-session") && qa("#tk-root .tk-item").length > 1`, "all tasks again");
    });

    await check("Italian: the views, the hidden-nodes box and the history say it in Italian", async () => {
      await send("Page.navigate", { url: URL + "?lang=it" });
      await sleep(1500);
      await openTab();
      const t = JSON.parse(await ev(`return JSON.stringify({ views: ["tree", "spine", "history"].map(v => q("#tk-view-" + v).textContent.trim()), hidden: q("#tk-root .tk-check").textContent.trim() });`));
      if (t.views.join() !== "Albero,Spina,Cronologia" || !/^Visualizza nodi nascosti \(\d+\)$/.test(t.hidden)) throw new Error(JSON.stringify(t));
    });
    if (pageErrors.length) out.push({ name: "page errors", ok: false, error: pageErrors.join(" ;; ") });
    ws.close();
  } finally { proc.kill(); }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "driver", ok: false, error: String(e && e.stack || e) }])); });
