"use strict";
// Drives the questions of a plan to the person in a real headless browser, over a tab that is not the Plans one, at three screen sizes:
// node human_dom_check.js <url> <browser exe>. Prints [{name, ok, error}] as JSON (read by test_human_page.py).
// It makes its own plans through the API, answers them with the real buttons, keys and menus, and asks the server what the plans came to.
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const get = async p => (await fetch(URL + p)).json();
const post = async (p, body) => (await fetch(URL + p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];

const input = (id, deps, spec) => ({ id, type: "input", title: id, assignee: "user", dependencies: deps, outputs: [], spec });
/* a plan with a start that is not taken yet, and four questions behind it: a menu with labels, a text with an explanation, a number, a yes or no with a text to look at */
const wizardPlan = (id, title) => {
  const nodes = {
    go: { id: "go", type: "start", title: "Start", assignee: "agent", dependencies: [], outputs: ["i1", "i2", "i3", "i4"], spec: { number: 1, launch: "manual" } },
    i1: input("i1", ["go"], { prompt: "Quante notizie vuoi?", prompt_en: "How many stories do you want?", help: "Da tre a nove, di solito sei.", help_en: "From three to nine, usually six.", into: "stories", kind: "choice",
      choices: [{ value: "n3", label: "Tre notizie", label_en: "Three stories" }, { value: "n6", label: "Sei notizie", label_en: "Six stories" }, { value: "n9", label: "Nove notizie", label_en: "Nine stories" }], default: "n6" }),
    i2: input("i2", ["go"], { prompt: "Di che cosa parla il telegiornale?", prompt_en: "What is the newscast about?", help: "Una frase, per esempio: politica ungherese.", help_en: "One sentence, for example: Hungarian politics.", into: "topic", kind: "text", default: "politica" }),
    i3: input("i3", ["go"], { prompt: "Quante ore indietro cercare?", prompt_en: "How many hours back to look?", into: "hours", kind: "number", default: 24 }),
    i4: input("i4", ["go"], { ask: "approve", prompt: "Il copione va bene?", prompt_en: "Is the script good?", content: "Buonasera, ecco le notizie.\nPrima notizia: il porto riapre.", into: "script_ok" }),
  };
  nodes.after = { id: "after", type: "action", title: "After", assignee: "user", dependencies: ["i1", "i2", "i3", "i4"], outputs: [] };
  ["i1", "i2", "i3", "i4"].forEach(i => { nodes[i].outputs = ["after"]; });
  return { plan_id: id, title, target_type: "occasional", nodes };
};

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
    const text = () => ev(`return q("#hr-card").textContent;`);
    const button = label => `qa("#hr-card button").find(b => b.textContent.trim() === ${JSON.stringify(label)})`;
    const press = async label => { await waitFor(`!!${button(label)}`, `a button '${label}'`, 20); await ev(`${button(label)}.click(); return 1;`); await sleep(150); };
    const key = (k, target = "#hr-value") => ev(`const t = q(${JSON.stringify(target)}) || q("#hr-card"); t.dispatchEvent(new KeyboardEvent("keydown", { key: ${JSON.stringify(k)}, bubbles: true, cancelable: true })); return 1;`).then(() => sleep(150));
    const setField = value => ev(`const f = q("#hr-value"); f.value = ${JSON.stringify(value)}; f.dispatchEvent(new Event("input", { bubbles: true })); f.dispatchEvent(new Event("change", { bubbles: true })); return 1;`);
    const need = async (what, parts) => { const t = await text(); for (const p of parts) if (!t.includes(p)) throw new Error(`missing '${p}' in ${what}: ${t.slice(0, 400)}`); return t; };
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message }); } if (process.env.HRC_LOG) console.error(out[out.length - 1].ok ? "ok  " : "FAIL", name, out[out.length - 1].error); };
    /* what the window looks like: inside the screen, no sideways scroll, every button, field and menu at least 44 px high and wide */
    const fits = () => ev(`const d = q("#hr-card"); const r = d.getBoundingClientRect(); const small = qa("#hr-card button, #hr-card input, #hr-card select").filter(e => { const b = e.getBoundingClientRect(); return b.height < 44 || b.width < 44; }).map(e => e.textContent.trim() || e.id);
      return JSON.stringify({ inside: r.left >= 0 && r.right <= innerWidth + 1 && r.top >= 0 && r.bottom <= innerHeight + 1, noscroll: d.scrollWidth <= d.clientWidth + 1, small, w: innerWidth, h: innerHeight });`).then(v => {
        const f = JSON.parse(v);
        if (!f.inside) throw new Error("the window is not inside the screen " + v);
        if (!f.noscroll) throw new Error("the window scrolls sideways " + v);
        if (f.small.length) throw new Error("targets under 44 px: " + f.small.join(", "));
      });
    const shot = async name => { if (process.env.HRC_SHOTS) fs.writeFileSync(path.join(process.env.HRC_SHOTS, name + ".png"), Buffer.from((await send("Page.captureScreenshot", { format: "png" })).data, "base64")); };
    const nodeOf = async (plan, n) => (await get(`api/plans/${plan}`)).plan.nodes[n];

    for (const [vname, w, h, mobile] of VIEWPORTS) {
      const tag = vname.replace(/[^a-z0-9]+/g, "-");
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      const W = `hr-${tag}-wizard`;
      await post("api/plans", { plan: wizardPlan(W, "Newscast " + vname) });
      await send("Page.navigate", { url: URL + "?lang=en" });
      await sleep(1800);
      await ev(`const t = q("#tab-stats"); if (t) t.click(); return 1;`);
      await check(`${vname}: the start of the plan is not a question, and nothing asks until it is taken`, async () => {
        await sleep(5500);                                                         // longer than one look at the list
        if (await ev(`return !!q("#hr-card[open]");`)) throw new Error("the window opened before the plan started");
        const started = await post(`api/plans/${W}/events`, { type: "plan:start", number: 1, actor: "chat" });
        if (!started.ok) throw new Error("the plan did not start: " + JSON.stringify(started));
        await waitFor(`!!q("#hr-card[open]")`, "the window once the plan started", 40);
        const t = await need("the first question", ["Question 1 of 4"]);
        if (/Start/.test(t.replace("Newscast", "")) ) throw new Error("the start is among the questions: " + t);
      });
      await check(`${vname}: one question, large, with its explanation, in the person's language; a menu shows labels, never values, with the proposed one chosen`, async () => {
        await need("the first question", ["How many stories do you want?", "From three to nine, usually six.", "Later", "Next", "Back"]);
        const opts = await ev(`return JSON.stringify(qa("#hr-value option").map(o => o.textContent)) + "|" + q("#hr-value").selectedOptions[0].textContent + "|" + qa("#hr-value option").map(o => o.value).join(",");`);
        const [list, chosen] = opts.split("|");
        if (/n3|n6|n9/.test(list) || chosen !== "Six stories") throw new Error("what the menu shows: " + opts);
        if (parseFloat(await ev(`return getComputedStyle(q(".hr-q")).fontSize;`)) < 18) throw new Error("the question is not large");
      });
      await check(`${vname}: it fits the screen and its targets are big enough`, async () => { await fits(); await shot(tag + "-question"); });
      await check(`${vname}: the arrow keys and Enter move along the path; Next refuses an empty answer; Back goes back`, async () => {
        await key("ArrowRight");
        await need("question 2", ["Question 2 of 4", "What is the newscast about?", "One sentence, for example: Hungarian politics."]);
        await key("ArrowLeft", "#hr-card");
        await need("question 1", ["Question 1 of 4"]);
        await press("Next");
        await setField("");
        await press("Next");
        await need("the refusal", ["Question 2 of 4", "Write an answer first."]);
        await setField("Hungarian politics");
        await key("Enter");
        await need("question 3", ["Question 3 of 4", "How many hours back to look?"]);
        await press("Back");
        await need("question 2 again", ["Question 2 of 4"]);
        if ((await ev(`return q("#hr-value").value;`)) !== "Hungarian politics") throw new Error("the answer was not kept when going back");
        await press("Next");
        await setField("many"); await press("Next");
        await need("the number refused", ["The answer is a number."]);
        await setField("12"); await press("Next");
        await need("question 4", ["Question 4 of 4", "Is the script good?", "Prima notizia: il porto riapre."]);
      });
      await check(`${vname}: the summary lists every answer as read, Edit changes one and comes straight back, and nothing is sent yet`, async () => {
        await press("Yes, approve");
        await press("Next");
        const t = await need("the summary", ["Check your answers", "Six stories", "Hungarian politics", "12", "Yes", "Send all answers"]);
        if (/\bn6\b/.test(t)) throw new Error("a value is shown: " + t);
        if ((await ev(`return qa("#hr-card .hr-edit").length;`)) !== 4) throw new Error("not an Edit for each answer");
        await fits(); await shot(tag + "-summary");
        await ev(`qa("#hr-card .hr-edit")[0].click(); return 1;`); await sleep(200);
        await need("question 1 again", ["Question 1 of 4"]);
        await setField("n3"); await press("Next");
        await need("the summary again", ["Check your answers", "Three stories"]);
        for (const n of ["i1", "i2", "i3", "i4"]) if ((await nodeOf(W, n)).status !== "pending") throw new Error(`${n} was sent before Send`);
      });
      await check(`${vname}: one Send gives the plan all the answers, with the values and not the labels`, async () => {
        await press("Send all answers");
        let plan = null;
        for (let i = 0; i < 30; i++) { plan = (await get(`api/plans/${W}`)).plan; if (plan.nodes.i4.status === "completed") break; await sleep(300); }
        const s = plan.shared_state;
        if (!["i1", "i2", "i3", "i4"].every(n => plan.nodes[n].status === "completed") || s.stories !== "n3" || s.topic !== "Hungarian politics" || s.hours !== 12 || s.script_ok !== true) throw new Error("not answered together: " + JSON.stringify(s));
        await waitFor(`!q("#hr-card[open]")`, "the window to close once answered");
      });
      const L = `hr-${tag}-later`;
      await post("api/plans", { plan: wizardPlan(L, "Later " + vname) });
      await post(`api/plans/${L}/events`, { type: "plan:start", number: 1, actor: "chat" });
      await check(`${vname}: Later closes the window, the questions do not come straight back, and nothing is answered`, async () => {
        await waitFor(`!!q("#hr-card[open]")`, "the window", 40);
        await press("Later");
        await waitFor(`!q("#hr-card[open]")`, "the window to close");
        await sleep(6000);
        if (await ev(`return !!q("#hr-card[open]");`)) throw new Error("it came back at once");
        if ((await nodeOf(L, "i1")).status !== "pending") throw new Error("Later answered it");
      });
      for (const p of [W, L]) await post(`api/plans/${p}/delete`, {});
    }

    // the same questions in Italian
    await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    const I = "hr-italian";
    await post("api/plans", { plan: wizardPlan(I, "Telegiornale") });
    await post(`api/plans/${I}/events`, { type: "plan:start", number: 1, actor: "chat" });
    await send("Page.navigate", { url: URL + "?lang=it" });
    await sleep(1800);
    await check("in Italian: the question, its explanation, the labels and the buttons are in Italian", async () => {
      await waitFor(`!!q("#hr-card[open]")`, "the window", 40);
      await need("the Italian question", ["Domanda 1 di 4", "Quante notizie vuoi?", "Da tre a nove, di solito sei.", "Avanti", "Indietro", "Più tardi"]);
      const chosen = await ev(`return q("#hr-value").selectedOptions[0].textContent;`);
      if (chosen !== "Sei notizie") throw new Error("the label: " + chosen);
    });
    await post(`api/plans/${I}/delete`, {});
    ws.close();
  } finally { proc.kill(); await sleep(400); try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* temp */ } }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "the run", ok: false, error: String(e && e.message || e) }])); });
