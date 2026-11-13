"use strict";
// Drives the window of a plan's request to the person in a real headless browser, over a tab that is not the Plans one, at three screen sizes:
// node human_dom_check.js <url> <browser exe>. Prints [{name, ok, error}] as JSON (read by test_human_page.py).
// It makes its own plans through the API, answers them with the real buttons, and asks the server what the plans came to.
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const get = async p => (await fetch(URL + p)).json();
const post = async (p, body) => (await fetch(URL + p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];

const planOf = (id, title, node) => ({ plan_id: id, title, target_type: "occasional", nodes: {
  ask: { id: "ask", type: "input", title: "The question", assignee: "user", dependencies: [], outputs: ["after"], spec: node },
  after: { id: "after", type: "action", title: "After", assignee: "agent", dependencies: ["ask"], outputs: [] } } });

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "hrc-"));
  const proc = spawn(EXE, ["--headless=new", "--lang=en-US", "--disable-gpu", `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, "--window-size=1440,900", "about:blank"], { stdio: "ignore" });
  const out = [];
  try {
    let targets = [];
    for (let i = 0; i < 40 && !targets.length; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch (e) { await sleep(250); } }
    const ws = new WebSocket(targets.find(t => t.type === "page").webSocketDebuggerUrl);
    await new Promise(r => { ws.onopen = r; });
    let id = 0; const waits = new Map();
    ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.id && waits.has(m.id)) { waits.get(m.id)(m.result || m.error); waits.delete(m.id); } };
    const send = (method, params = {}) => new Promise((r, rej) => { const n = ++id; const t = setTimeout(() => rej(new Error(`${method} did not answer`)), 60000); waits.set(n, v => { clearTimeout(t); r(v); }); ws.send(JSON.stringify({ id: n, method, params })); });
    const ev = async expr => {
      const r = await send("Runtime.evaluate", { expression: `(async () => { const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)]; const wait = ms => new Promise(r => setTimeout(r, ms)); ${expr} })()`, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception && r.exceptionDetails.exception.description));
      return r.result.value;
    };
    await send("Page.enable"); await send("Runtime.enable"); await send("Emulation.setFocusEmulationEnabled", { enabled: true }); await send("Page.bringToFront");

    const waitFor = async (expr, what, tries = 60) => { for (let i = 0; i < tries; i++) { if (await ev(`return (${expr});`)) return true; await sleep(300); } throw new Error(`never: ${what}`); };
    const button = label => `qa("#hr-card button").find(b => b.textContent.trim() === ${JSON.stringify(label)})`;
    const press = async label => { await waitFor(`!!${button(label)}`, `a button '${label}'`, 20); await ev(`${button(label)}.click(); return 1;`); await sleep(150); };
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message }); } if (process.env.HRC_LOG) console.error(out[out.length - 1].ok ? "ok  " : "FAIL", name, out[out.length - 1].error); };
    /* what the window looks like: inside the screen, no sideways scroll, every button at least 44 px high and wide */
    const fits = (w, h) => ev(`const d = q("#hr-card"); const r = d.getBoundingClientRect(); const small = qa("#hr-card button, #hr-card input, #hr-card select").filter(e => { const b = e.getBoundingClientRect(); return b.height < 44 || b.width < 44; }).map(e => e.textContent.trim() || e.id);
      return JSON.stringify({ inside: r.left >= 0 && r.right <= innerWidth + 1 && r.top >= 0 && r.bottom <= innerHeight + 1, noscroll: d.scrollWidth <= d.clientWidth + 1, small, w: innerWidth, h: innerHeight });`).then(v => {
        const f = JSON.parse(v);
        if (!f.inside) throw new Error("the window is not inside the screen " + v);
        if (!f.noscroll) throw new Error("the window scrolls sideways " + v);
        if (f.small.length) throw new Error("targets under 44 px: " + f.small.join(", "));
      });

    for (const [vname, w, h, mobile] of VIEWPORTS) {
      const key = vname.replace(/[^a-z0-9]+/g, "-");
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      // an approval, over the Monitor tab
      const A = `hr-${key}-approve`;
      await post("api/plans", { plan: planOf(A, "Newscast " + vname, { ask: "approve", prompt: "Is the script good?", content: "Good evening.\nThe first story is about the harbour.", into: "script_ok" }) });
      await send("Page.navigate", { url: URL + "?lang=en" });
      await sleep(1500);
      await ev(`const t = q("#tab-stats"); if (t) t.click(); return 1;`);
      await check(`${vname}: the window opens over a tab that is not Plans, with the question and what to approve`, async () => {
        await waitFor(`!!q("#hr-card[open]")`, "the window");
        const t = await ev(`return q("#hr-card").textContent + "|" + document.body.dataset.view;`);
        for (const need of ["A plan needs you", "Newscast " + vname, "Is the script good?", "The first story is about the harbour.", "Yes, approve", "No", "Back"]) if (!t.includes(need)) throw new Error(`missing '${need}' in: ${t}`);
        if (t.endsWith("|plans")) throw new Error("it was meant to be tested over another tab");
      });
      await check(`${vname}: it fits the screen and its targets are big enough`, () => fits(w, h));
      await check(`${vname}: Yes asks to confirm, Back returns to the choice, nothing is sent before the confirmation`, async () => {
        await press("Yes, approve");
        await waitFor(`q("#hr-card").textContent.includes("Send this answer to the plan?")`, "the confirmation");
        if ((await get(`api/plans/${A}`)).plan.nodes.ask.status !== "pending") throw new Error("it was sent before the confirmation");
        await press("Back");
        await waitFor(`!!${button("Yes, approve")}`, "the choice again");
      });
      await check(`${vname}: Yes, then Confirm, sets the plan going: the answer is true in the plan`, async () => {
        await press("Yes, approve"); await press("Confirm and send");
        let plan = null;
        for (let i = 0; i < 30; i++) { plan = (await get(`api/plans/${A}`)).plan; if (plan.nodes.ask.status === "completed") break; await sleep(300); }
        if (plan.nodes.ask.status !== "completed" || plan.shared_state.script_ok !== true) throw new Error("not answered: " + JSON.stringify(plan.shared_state));
        await waitFor(`!q("#hr-card[open]")`, "the window to close once answered");
      });
      // a number
      const V = `hr-${key}-value`;
      await post("api/plans", { plan: planOf(V, "Count " + vname, { ask: "value", prompt: "How many stories?", into: "count", kind: "number" }) });
      await check(`${vname}: a value: a wrong one is refused, a right one goes through OK and the confirmation`, async () => {
        await waitFor(`!!q("#hr-card[open] #hr-value")`, "the window with a field", 40);
        await fits(w, h);
        await ev(`const f = q("#hr-value"); f.value = "many"; ${button("OK")}.click(); return 1;`);
        await waitFor(`q("#hr-card").textContent.includes("The answer is a number.")`, "the refusal");
        await ev(`const f = q("#hr-value"); f.value = "12"; f.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); return 1;`);      // Enter is OK
        await waitFor(`q("#hr-card").textContent.includes("Your answer: 12")`, "the confirmation");
        await press("Confirm and send");
        let plan = null;
        for (let i = 0; i < 30; i++) { plan = (await get(`api/plans/${V}`)).plan; if (plan.nodes.ask.status === "completed") break; await sleep(300); }
        if (plan.shared_state.count !== 12) throw new Error("not answered: " + JSON.stringify(plan.shared_state));
      });
      // Back leaves it for later
      const L = `hr-${key}-later`;
      await post("api/plans", { plan: planOf(L, "Later " + vname, { ask: "approve", prompt: "Later?", into: "later" }) });
      await check(`${vname}: Back closes the window and the request does not come straight back`, async () => {
        await waitFor(`!!q("#hr-card[open]")`, "the window", 40);
        await press("Back");
        await waitFor(`!q("#hr-card[open]")`, "the window to close");
        await sleep(6000);                                                             // longer than one look at the list
        if (await ev(`return !!q("#hr-card[open]")`)) throw new Error("it came back at once");
        if ((await get(`api/plans/${L}`)).plan.nodes.ask.status !== "pending") throw new Error("Back answered it");
      });
      for (const p of [A, V, L]) await post(`api/plans/${p}/delete`, {});
    }
    ws.close();
  } finally { proc.kill(); await sleep(400); try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* temp */ } }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "the run", ok: false, error: String(e && e.message || e) }])); });
