"use strict";
// Drives the inference settings panel in a real headless browser at three screen sizes: node inference_dom_check.js <url> <browser exe>.
// Prints [{name, ok, error}] as JSON (read by test_inference_page.py). One page, actions in sequence, and each one is checked against what the server then holds:
// the machine default saved from Settings, a plan's own settings and the thinking level of all its steps and of one step from the Plans tab, and the
// settings of an open conversation from the Chat. No model: the conversation is only made, never given a message.
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const get = async p => (await fetch(URL + p)).json();
const post = async (p, body) => (await fetch(URL + p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];

const planOf = (id, title) => ({ plan_id: id, title, target_type: "occasional", nodes: {
  a: { id: "a", type: "action", title: "Step A", assignee: "agent", dependencies: [], outputs: ["b"] },
  b: { id: "b", type: "action", title: "Step B", assignee: "agent", dependencies: ["a"], outputs: [] } } });

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "infc-"));
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
    const waitFor = async (expr, what, tries = 60) => { for (let i = 0; i < tries; i++) { if (await ev(`return (${expr});`)) return true; await sleep(300); } throw new Error(`never: ${what}`); };
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message + (pageErrors.length ? " | page errors: " + pageErrors.splice(0).join(" ;; ") : "") }); } };
    // the number a field of the panel holds is changed the way a person does: the value, then the input and change events
    const type = (sel, value) => ev(`const f = q(${JSON.stringify(sel)}); f.value = ${JSON.stringify(value)}; f.dispatchEvent(new Event("input", { bubbles: true })); f.dispatchEvent(new Event("change", { bubbles: true })); return 1;`);
    /* no sideways scroll, and every control of the panel that is shown is at least 44 px high */
    const fits = what => ev(`const p = q("#chatset"); const small = qa("#chatset button, #chatset select, #chatset input:not([type=radio]):not([type=checkbox]), #chatset textarea").filter(e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0 && b.height < 43.5; }).map(e => e.id || e.className || e.tagName);
      return JSON.stringify({ wide: document.documentElement.scrollWidth > innerWidth + 1, small, shown: !!p && p.offsetParent !== null });`).then(v => {
      const f = JSON.parse(v);
      if (!f.shown) throw new Error(what + ": the panel is not shown");
      if (f.wide) throw new Error(what + ": the page scrolls sideways");
      if (f.small.length) throw new Error(what + ": controls under 44 px: " + f.small.join(", "));
    });
    const panelIn = host => ev(`const p = q("#chatset"); return !!p && !!p.closest(${JSON.stringify(host)}) && !p.hidden;`);

    for (const [vname, w, h, mobile] of VIEWPORTS) {
      const key = vname.replace(/[^a-z0-9]+/g, "-");
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      await send("Page.navigate", { url: URL + "?lang=en" });
      await sleep(1500);

      // -- the machine default, from Settings
      await ev(`q("#tab-settings").click(); return 1;`);
      await check(`${vname}: Settings > General opens the panel on the machine default, without what only a conversation has`, async () => {
        await waitFor(`!!q("#inf-open")`, "the Settings section");
        await ev(`const b = q("#inf-open"); b.scrollIntoView(); b.click(); return 1;`);
        await waitFor(`!!q("#inf-host #chatset") && !q("#chatset").hidden`, "the panel in the section");
        if (await ev(`return q("#chatset").dataset.scope`) !== "machine") throw new Error("not bound to the machine default");
        if (await ev(`return getComputedStyle(q("#cs-perm")).display`) !== "none" || await ev(`return getComputedStyle(q("#cs-cp")).display`) !== "none") throw new Error("a group that only a conversation has is shown");
        if (await ev(`return getComputedStyle(q("#cs-sampling")).display`) === "none") throw new Error("the sampling is hidden");
        await fits("settings");
      });
      await check(`${vname}: a change in the panel is saved as the preset Machine Default`, async () => {
        await type("#cs-temperature", "0.35"); await type("#cs-threshold", "70"); await type("#cs-effort", "low");
        let preset = null;
        for (let i = 0; i < 20 && !preset; i++) { await sleep(300); preset = ((await get("api/profiles")).inference || []).find(p => p.name === "Machine Default" && p.settings.temperature === "0.35"); }
        if (!preset || preset.settings.threshold !== "70" || preset.settings.effort !== "low") throw new Error("not saved: " + JSON.stringify(preset) + " status: " + await ev(`return q("#cs-status").textContent + "|" + q("#chatset").dataset.scope + "|" + q("#cs-temperature").value`) + " presets: " + JSON.stringify((await get("api/profiles")).inference));
      });

      // -- a plan: its own settings, one level for all the steps, one step's own level
      const P = `inf-${key}`;
      await post("api/plans", { plan: planOf(P, "Inference " + vname) });
      await ev(`q("#tab-plans").click(); return 1;`);
      await check(`${vname}: the plan's editor has the same panel, bound to the plan, starting from the machine default`, async () => {
        try { await waitFor(`!!q("#pl-col") && qa("#pl-col button").some(b => b.textContent.includes(${JSON.stringify("Inference " + vname)}))`, "the plan in the list"); }
        catch (e) { throw new Error(e.message + " " + JSON.stringify(((await get("api/plans")).plans || []).map(p => p.plan_id)) + " " + await ev(`return qa("#pl-col button").map(b => b.textContent.trim().slice(0, 30)).join("|") + " view=" + document.body.dataset.view`)); }
        await ev(`qa("#pl-col button").find(b => b.textContent.includes(${JSON.stringify("Inference " + vname)})).click(); return 1;`);
        await waitFor(`!!q("#pl-tab-define")`, "the editor");
        await ev(`q("#pl-tab-define").click(); return 1;`);
        await waitFor(`!!q("#pl-inf-open") && q("#pl-inf-open").offsetParent !== null`, "the button");
        if (await ev(`return !!q("#inf-host #chatset")`)) throw new Error("the panel stayed in Settings");
        await ev(`q("#pl-inf-open").scrollIntoView(); q("#pl-inf-open").click(); return 1;`);
        await waitFor(`!!q("#pl-inf-host #chatset") && !q("#chatset").hidden`, "the panel in the editor");
        if (await ev(`return q("#chatset").dataset.scope`) !== "plan") throw new Error("not bound to the plan");
        if (await ev(`return q("#cs-temperature").value`) !== "0.35") throw new Error("it did not start from the machine default");
        await fits("plans");
      });
      await check(`${vname}: a change is saved in the plan's own profile, and the machine default is not touched`, async () => {
        await type("#cs-temperature", "0.6"); await type("#cs-effort", "medium");
        let plan = null;
        for (let i = 0; i < 20; i++) { await sleep(300); plan = (await get(`api/plans/${P}`)); if ((plan.profile || {}).inference && plan.profile.inference.temperature === "0.6") break; }
        const own = (plan.profile || {}).inference || {};
        if (own.temperature !== "0.6" || own.effort !== "medium" || own.threshold !== "70") throw new Error("not in the plan: " + JSON.stringify(own));
        const machine = ((await get("api/profiles")).inference || []).find(p => p.name === "Machine Default");
        if (machine.settings.temperature !== "0.35") throw new Error("the machine default changed");
      });
      await check(`${vname}: closing the panel puts it back where it was and the editor shows no panel`, async () => {
        await ev(`q("#pl-inf-open").click(); return 1;`);
        await waitFor(`!q("#pl-inf-host #chatset") && !!q("#view-chat #chatset")`, "the panel back in the chat");
      });
      await check(`${vname}: the list at the top sets one thinking level on every step, and "As the plan says" takes it away`, async () => {
        await ev(`const s = q("#pl-effort-all"); s.value = "low"; s.dispatchEvent(new Event("change", { bubbles: true })); return 1;`);
        let nodes = {};
        for (let i = 0; i < 20; i++) { await sleep(300); nodes = (await get(`api/plans/${P}`)).plan.nodes; if (nodes.a.effort === "low" && nodes.b.effort === "low") break; }
        if (nodes.a.effort !== "low" || nodes.b.effort !== "low") throw new Error("not on every step: " + JSON.stringify([nodes.a.effort, nodes.b.effort]));
        if (await ev(`return q("#pl-effort-all").value`) !== "-") throw new Error("the list did not go back to its title");
        await ev(`const s = q("#pl-effort-all"); s.value = ""; s.dispatchEvent(new Event("change", { bubbles: true })); return 1;`);
        for (let i = 0; i < 20; i++) { await sleep(300); nodes = (await get(`api/plans/${P}`)).plan.nodes; if (!nodes.a.effort && !nodes.b.effort) break; }
        if (nodes.a.effort || nodes.b.effort) throw new Error("the plan's own level was not given back: " + JSON.stringify([nodes.a.effort, nodes.b.effort]));
      });
      await check(`${vname}: a step has its own thinking level in its detail`, async () => {
        await ev(`qa(".pl-libtn").find(b => b.textContent.includes("Step B")) ? 1 : 0; return 1;`);
        if (!(await ev(`return !!qa(".pl-libtn").find(b => b.textContent.includes("Step B"))`))) await ev(`q(".pl-modes button:nth-child(2)").click(); return 1;`);      // the list view
        await waitFor(`!!qa(".pl-libtn").find(b => b.textContent.includes("Step B"))`, "the step in the list");
        await ev(`qa(".pl-libtn").find(b => b.textContent.includes("Step B")).click(); return 1;`);
        await waitFor(`!!q("#pl-d-effort")`, "the step's level");
        await ev(`const s = q("#pl-d-effort"); s.scrollIntoView(); s.value = "high"; s.dispatchEvent(new Event("change", { bubbles: true })); s.closest("form").querySelector("button.btn.primary").click(); return 1;`);
        let nodes = {};
        for (let i = 0; i < 20; i++) { await sleep(300); nodes = (await get(`api/plans/${P}`)).plan.nodes; if (nodes.b.effort === "high") break; }
        if (nodes.b.effort !== "high" || nodes.a.effort) throw new Error("only the step was meant to change: " + JSON.stringify([nodes.a.effort, nodes.b.effort]) + " " + await ev(`return (q(".pl-status") || {}).textContent + "|" + (q("#pl-d-effort") || {}).value + "|" + qa(".pl-dform .btn.primary").map(b => b.textContent).join(",")`));
        const box = JSON.parse(await ev(`const e = q("#pl-d-effort"); const b = e.getBoundingClientRect(); return JSON.stringify({ h: b.height, w: document.documentElement.scrollWidth, iw: innerWidth });`));
        if (box.h < 43.5) throw new Error("the step's list is under 44 px: " + box.h);
        if (box.w > box.iw + 1) throw new Error("the page scrolls sideways: " + JSON.stringify(box));
      });

      // -- a conversation: its own settings, shown when it is opened and saved with it
      const made = (await post("api/agent/sessions", { workspace: "", mode: "default", ask_only: true, inference: { effort: "medium", policy: "compact", threshold: "65", temperature: "0.8" } })).session;
      await send("Page.navigate", { url: URL + "?lang=en" });
      await sleep(1500);
      await ev(`q("#tab-chat").click(); return 1;`);
      await waitFor(`!!window.agentChat && window.agentChat.active()`, "conversations in the Chat");
      await ev(`await window.agentChat.open(${JSON.stringify(made.id)}); return 1;`);
      await check(`${vname}: an open conversation shows its own settings, not the machine default`, async () => {
        await waitFor(`!!q("#cs-open")`, "the Chat");
        await ev(`const c = q("#cs-open"); if (q("#chatset").hidden) c.click(); return 1;`);
        try { await waitFor(`q("#chatset").dataset.scope === "session" && q("#cs-temperature").value === "0.8"`, "the conversation's own values", 40); }
        catch (e) { throw new Error(e.message + " " + await ev(`return JSON.stringify({ scope: q("#chatset").dataset.scope, view: document.body.dataset.view, keys: Object.entries(localStorage).filter(([k]) => k.includes("session") || k.includes("chatmode")), temp: q("#cs-temperature").value, mode: q("#chatmode").value, list: await (await fetch("api/agent/sessions")).json().then(j => ({ d: j.default_workspace, s: j.sessions.map(x => [x.id, x.workspace, x.state]) })), conv: qa("#convs li").length, cur: window.agentChat.current(), recTemp: (window.agentChat.record() || {}).temperature, active: window.agentChat.active(), view: document.body.dataset.view, status: (q("#chat-status") || {}).textContent })`)); }
        const v = await ev(`return q("#cs-threshold").value + "|" + q("#cs-effort").value + "|" + q("#effort").value;`);
        if (v !== "65|medium|medium") throw new Error("wrong values: " + v);
        await fits("chat");
      });
      await check(`${vname}: a change in the panel is saved with the conversation, and the plan and the default are not touched`, async () => {
        await type("#cs-temperature", "0.15"); await type("#cs-top_p", "0.9");
        let rec = null;
        for (let i = 0; i < 20; i++) { await sleep(300); rec = (await get(`api/agent/sessions/${made.id}/history`)).session; if (rec.temperature === 0.15) break; }
        if (rec.temperature !== 0.15 || rec.inference.top_p !== "0.9" || (rec.sampling || {}).top_p !== 0.9 || rec.inference.threshold !== "65") throw new Error("not in the conversation: " + JSON.stringify(rec.inference));
        if (((await get(`api/plans/${P}`)).profile.inference || {}).temperature !== "0.6") throw new Error("the plan changed");
        if (((await get("api/profiles")).inference || []).find(p => p.name === "Machine Default").settings.temperature !== "0.35") throw new Error("the machine default changed");
      });
      await check(`${vname}: a new conversation starts from the machine default again`, async () => {
        await ev(`q("#new-chat").click(); return 1;`);
        await waitFor(`q("#chatset").dataset.scope === "new" && q("#cs-temperature").value === "0.35"`, "the default values", 40);
      });
      for (const s of (await get("api/agent/sessions")).sessions) await post(`api/agent/sessions/${s.id}/delete`, {});
      await post(`api/plans/${P}/delete`, {});
      await post("api/profiles", { action: "delete", kind: "inference", name: "Machine Default" });
    }
    ws.close();
  } finally { proc.kill(); await sleep(400); try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* temp */ } }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "the run", ok: false, error: String(e && e.message || e) }])); });
