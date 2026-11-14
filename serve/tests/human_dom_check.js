"use strict";
// Drives the questions of a plan to the person in a real headless browser, over a tab that is not the Plans one, at three screen sizes:
// node human_dom_check.js <url> <browser exe>. Prints [{name, ok, error}] as JSON (read by test_human_page.py).
// It makes its own plans through the API, answers them with the real buttons, menus and fields, and asks the server what the plans came to.
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const get = async p => (await fetch(URL + p)).json();
const post = async (p, body) => (await fetch(URL + p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];
const BULK = 30;                                   // text questions that have a proposed answer: with the six others, the list must show all 36

const input = (id, deps, spec) => ({ id, type: "input", title: id, assignee: "user", dependencies: deps, outputs: [], spec });
/* a plan with a start that is not taken yet, and 36 questions behind it in three groups: a menu of three labelled choices (the fourth entry is "Other, I write"), a text with an explanation, a number,
   thirty texts with a proposed answer, a yes or no with a text to look at, a question of two answers, and a task for the person */
const listPlan = (id, title) => {
  const g1 = { group: "Cosa e quando", group_en: "What and when" }, g2 = { group: "Altri parametri", group_en: "Other settings" }, g3 = { group: "Approvazioni", group_en: "Approvals" };
  const nodes = {
    i1: input("i1", ["go"], { ...g1, prompt: "Quante notizie vuoi?", prompt_en: "How many stories do you want?", help: "Da tre a nove, di solito sei.", help_en: "From three to nine, usually six.", into: "stories", kind: "choice",
      choices: [{ value: "n3", label: "Tre notizie", label_en: "Three stories" }, { value: "n6", label: "Sei notizie", label_en: "Six stories" }, { value: "n9", label: "Nove notizie", label_en: "Nine stories" }], default: "n6" }),
    i2: input("i2", ["go"], { ...g1, prompt: "Di che cosa parla il telegiornale?", prompt_en: "What is the newscast about?", help: "Una frase, per esempio: politica ungherese.", help_en: "One sentence, for example: Hungarian politics.", into: "topic", kind: "text", default: "politica" }),
    i3: input("i3", ["go"], { ...g1, prompt: "Quante ore indietro cercare?", prompt_en: "How many hours back to look?", into: "hours", kind: "number" }),
  };
  for (let i = 1; i <= BULK; i++) { const k = "b" + String(i).padStart(2, "0"); nodes[k] = input(k, ["go"], { ...g2, prompt: `Parametro ${i}`, prompt_en: `Setting ${i}`, into: k, kind: "text", default: "v" + i }); }
  nodes.i4 = input("i4", ["go"], { ...g3, ask: "approve", prompt: "Il copione va bene?", prompt_en: "Is the script good?", content: "Buonasera, ecco le notizie.\nPrima notizia: il porto riapre.", into: "script_ok" });
  nodes.i5 = input("i5", ["go"], { ...g3, prompt: "Il microfono è attivo?", prompt_en: "Is the microphone on?", into: "mic", kind: "choice", choices: [{ value: "on", label: "Attivo", label_en: "On" }, { value: "off", label: "Non attivo", label_en: "Off" }] });
  nodes.t1 = { id: "t1", type: "action", title: "Controlla il cavo del microfono", description: "Guarda il cavo del microfono e di' com'è.", assignee: "user", dependencies: ["go"], outputs: [], spec: { ...g3 } };
  const ids = Object.keys(nodes);
  nodes.go = { id: "go", type: "start", title: "Start", assignee: "agent", dependencies: [], outputs: ids, spec: { number: 1, launch: "manual" } };
  nodes.after = { id: "after", type: "action", title: "After", assignee: "third_party", dependencies: ids, outputs: [] };
  ids.forEach(i => { nodes[i].outputs = ["after"]; });
  return { plan_id: id, title, target_type: "occasional", nodes };
};
const TOTAL = 3 + BULK + 3;

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
    const item = idd => `q('#hr-card .hr-item[data-id="${idd}"]')`;
    const pressIn = async (idd, label) => { await ev(`const b = [...${item(idd)}.querySelectorAll("button")].find(b => b.textContent.trim() === ${JSON.stringify(label)}); if (!b) throw new Error("no button ${label} in ${idd}"); b.click(); return 1;`); await sleep(150); };
    const setIn = async (idd, selector, value, evt = "input") => { await ev(`const f = ${item(idd)}.querySelector(${JSON.stringify(selector)}); if (!f) throw new Error("no ${selector} in ${idd}"); f.value = ${JSON.stringify(value)}; f.dispatchEvent(new Event(${JSON.stringify(evt)}, { bubbles: true })); return 1;`); await sleep(120); };
    const need = async (what, parts) => { const t = await text(); for (const p of parts) if (!t.includes(p)) throw new Error(`missing '${p}' in ${what}: ${t.slice(0, 300)}`); return t; };
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message }); } if (process.env.HRC_LOG) console.error(out[out.length - 1].ok ? "ok  " : "FAIL", name, out[out.length - 1].error); };
    const shot = async name => { if (process.env.HRC_SHOTS) fs.writeFileSync(path.join(process.env.HRC_SHOTS, name + ".png"), Buffer.from((await send("Page.captureScreenshot", { format: "png" })).data, "base64")); };
    const nodeOf = async (plan, n) => (await get(`api/plans/${plan}`)).plan.nodes[n];
    const left = () => ev(`return q("#hr-card .hr-left").textContent;`);

    for (const [vname, w, h, mobile] of VIEWPORTS) {
      const tag = vname.replace(/[^a-z0-9]+/g, "-");
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      const W = `hr-${tag}-list`;
      await post("api/plans", { plan: listPlan(W, "Newscast " + vname) });
      await send("Page.navigate", { url: URL + "?lang=en" });
      await sleep(1800);
      await ev(`const t = q("#tab-stats"); if (t) t.click(); return 1;`);
      await check(`${vname}: nothing asks until the plan starts, and then one window opens over the Monitor tab; the start is not among the questions`, async () => {
        await sleep(5500);                                                         // longer than one look at the list
        if (await ev(`return !!q("#hr-card[open]");`)) throw new Error("the window opened before the plan started");
        const started = await post(`api/plans/${W}/events`, { type: "plan:start", number: 1, actor: "chat" });
        if (!started.ok) throw new Error("the plan did not start: " + JSON.stringify(started));
        await waitFor(`!!q("#hr-card[open]")`, "the window once the plan started", 40);
        if ((await ev(`return document.body.dataset.view;`)) === "plans") throw new Error("it was meant to be tested over another tab");
        const titles = await ev(`return qa("#hr-card .hr-q").map(e => e.textContent).join("|");`);
        if (/\bStart\b/.test(titles)) throw new Error("the start is among the questions");
      });
      await check(`${vname}: ALL ${TOTAL} questions are in one list, under their headings, with their explanations and the proposed answers; there are no steps`, async () => {
        const n = await ev(`return qa("#hr-card .hr-item").length;`);
        if (n !== TOTAL) throw new Error(`the list has ${n} questions, not ${TOTAL}`);
        await need("the header", [`${TOTAL} questions`, "Newscast " + vname]);
        const heads = await ev(`return JSON.stringify(qa("#hr-card .hr-group").map(e => e.textContent));`);
        if (heads !== JSON.stringify(["What and when", "Other settings", "Approvals"])) throw new Error("the headings: " + heads);
        await need("the first questions", ["How many stories do you want?", "From three to nine, usually six.", "What is the newscast about?", "Setting 30", "Is the script good?", "Is the microphone on?", "Controlla il cavo del microfono"]);
        const menu = await ev(`const s = ${item("i1")}.querySelector("select"); return JSON.stringify({ shown: [...s.options].map(o => o.textContent), values: [...s.options].map(o => o.value), chosen: s.selectedOptions[0].textContent });`);
        const m = JSON.parse(menu);
        if (m.chosen !== "Six stories" || m.shown.join("|") !== "Choose one|Three stories|Six stories|Nine stories|Other, I write" || m.shown.some(t => /n3|n6|n9|__other__/.test(t))) throw new Error("what the menu shows: " + menu);
        const buttons = await ev(`return qa("#hr-card button").map(b => b.textContent.trim()).filter(t => t === "Next" || t === "Back" || t === "Edit").length;`);
        if (buttons) throw new Error("there are still steps: Next, Back or Edit");
        if (parseFloat(await ev(`return getComputedStyle(q(".hr-q")).fontSize;`)) < 18) throw new Error("the question is not large");
        if ((await ev(`return qa("#hr-card .hr-item input.hr-field").filter(i => i.value).length;`)) < BULK) throw new Error("the proposed answers are not in the fields");
      });
      await check(`${vname}: ${mobile ? "full screen" : "a wide panel"}, the list scrolls, the bottom line stays in sight, every target is at least 44 px`, async () => {
        const g = JSON.parse(await ev(`const r = q("#hr-card").getBoundingClientRect(); const l = q("#hr-card .hr-list"); l.scrollTop = l.scrollHeight; await wait(100); const f = q("#hr-card .hr-foot").getBoundingClientRect(); const s = q("#hr-card .hr-send").getBoundingClientRect();
          const small = qa("#hr-card .hr-item button, #hr-card .hr-item input, #hr-card .hr-item select, #hr-card .hr-item textarea, #hr-card .hr-foot button").filter(e => { const b = e.getBoundingClientRect(); return b.height < 44 || b.width < 44; }).map(e => e.textContent.trim() || e.id).slice(0, 5);
          return JSON.stringify({ r: [r.left, r.top, r.width, r.height], foot: [f.top, f.bottom], send: [s.top, s.bottom], scroll: l.scrollHeight > l.clientHeight, sideways: l.scrollWidth > l.clientWidth + 1, small, w: innerWidth, h: innerHeight });`));
        if (mobile && (g.r[0] !== 0 || g.r[1] !== 0 || Math.abs(g.r[2] - g.w) > 1 || Math.abs(g.r[3] - g.h) > 1)) throw new Error("not full screen " + JSON.stringify(g));
        if (!mobile && (g.r[2] > 920 || g.r[2] < 600)) throw new Error("not a wide panel " + JSON.stringify(g));
        if (!g.scroll) throw new Error("the list does not scroll " + JSON.stringify(g));
        if (g.sideways) throw new Error("the list scrolls sideways " + JSON.stringify(g));
        if (g.foot[1] > g.h + 1 || g.send[0] < 0 || g.send[1] > g.h + 1) throw new Error("the bottom line is not in sight " + JSON.stringify(g));
        if (g.small.length) throw new Error("targets under 44 px: " + g.small.join(", "));
        await shot(tag + "-list-bottom");
        await ev(`q("#hr-card .hr-list").scrollTop = 0; return 1;`); await shot(tag + "-list-top");
      });
      await check(`${vname}: the bottom line says how many are not answered; Send with some missing shows which, brings the first into sight and sends nothing`, async () => {
        const t = await left();
        if (t !== "4 not answered yet") throw new Error("the line says: " + t);
        await press("Send all answers");
        const bad = await ev(`return qa("#hr-card .hr-err:not([hidden])").filter(e => e.closest(".hr-item")).map(e => e.closest(".hr-item").dataset.id).join(",");`);
        if (bad !== "i3,i4,i5,t1") throw new Error("the ones shown as not answered: " + bad);
        const seen = await ev(`const e = ${item("i3")}.getBoundingClientRect(), l = q("#hr-card .hr-list").getBoundingClientRect(); return e.top >= l.top - 1 && e.bottom <= l.bottom + 1;`);
        if (!seen) throw new Error("the first one not answered is not in sight");
        for (const n of ["i1", "i3", "i4", "t1"]) if ((await nodeOf(W, n)).status !== "pending") throw new Error(`${n} was sent`);
      });
      await check(`${vname}: choosing an answer never sends; Other opens a text field; Enter goes to the next field; one Send gives the plan all the answers, with values not labels`, async () => {
        await setIn("i1", "select", "__other__", "change");
        await waitFor(`!!${item("i1")}.querySelector(".hr-other")`, "the field of Other");
        await setIn("i1", ".hr-other", "Nine and a half");
        await setIn("i3", "input", "12");
        await pressIn("i4", "Yes, approve");
        if ((await nodeOf(W, "i4")).status !== "pending") throw new Error("choosing Yes sent it");
        await pressIn("i5", "Off");
        await setIn("i2", "input", "Hungarian politics");
        await ev(`const f = ${item("i3")}.querySelector("input"); f.focus(); f.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })); return 1;`);
        await sleep(200);
        if ((await ev(`return document.activeElement && document.activeElement.closest(".hr-item") && document.activeElement.closest(".hr-item").dataset.id;`)) !== "b01") throw new Error("Enter did not go to the next field");
        await setIn("t1", "textarea", "cable is fine");
        await pressIn("t1", "I did it");
        const t = await left();
        if (!/All answered/.test(t)) throw new Error("the line says: " + t);
        for (const n of ["i1", "i3", "i4", "i5", "t1"]) if ((await nodeOf(W, n)).status !== "pending") throw new Error(`${n} was sent before Send`);
        await press("Send all answers");
        let plan = null;
        for (let i = 0; i < 40; i++) { plan = (await get(`api/plans/${W}`)).plan; if (plan.nodes.i4.status === "completed") break; await sleep(300); }
        const s = plan.shared_state, done = Object.values(plan.nodes).filter(x => x.type === "input" || x.id === "t1").every(x => x.status === "completed");
        if (!done || s.stories !== "Nine and a half" || s.topic !== "Hungarian politics" || s.hours !== 12 || s.script_ok !== true || s.mic !== "off" || s.t1_note !== "cable is fine" || s.b01 !== "v1" || s.b30 !== "v30") throw new Error("not answered together: " + JSON.stringify(s).slice(0, 400));
        await waitFor(`!q("#hr-card[open]")`, "the window to close once answered").catch(async e => { throw new Error(e.message + " | " + (await text()).slice(0, 400)); });
      });
      const L = `hr-${tag}-later`;
      await post("api/plans", { plan: listPlan(L, "Later " + vname) });
      await post(`api/plans/${L}/events`, { type: "plan:start", number: 1, actor: "chat" });
      await check(`${vname}: Later closes the window, the questions do not come straight back, and nothing is answered`, async () => {
        await waitFor(`!!q("#hr-card[open]")`, "the window", 40);
        await press("Later");
        await waitFor(`!q("#hr-card[open]")`, "the window to close");
        await sleep(6000);
        if (await ev(`return !!q("#hr-card[open]");`)) throw new Error("it came back at once");
        if ((await nodeOf(L, "i1")).status !== "pending") throw new Error("Later answered it");
      });
      const T1 = `hr-${tag}-task`;
      await post("api/plans", { plan: { plan_id: T1, title: "Task " + vname, target_type: "occasional", nodes: { t: { id: "t", type: "action", title: "Pay the invoice", description: "Pay it and note the number.", assignee: "user", dependencies: [], outputs: [] } } } });
      await check(`${vname}: a step for the person is in the same list: "I can't now" with Send leaves it open and it does not come straight back`, async () => {
        await waitFor(`!!q("#hr-card[open]")`, "the window", 40);
        await need("the task", ["1 question", "Pay the invoice", "Pay it and note the number.", "I did it", "I can't now"]);
        await pressIn("t", "I can't now");
        await need("the note", ["It comes back in 10 minutes."]);
        await press("Send all answers");
        await waitFor(`!q("#hr-card[open]")`, "the window to close");
        await sleep(6000);
        if (await ev(`return !!q("#hr-card[open]");`)) throw new Error("it came back at once");
        if ((await nodeOf(T1, "t")).status !== "pending") throw new Error("Not now closed it");
        const done = await post(`api/plans/${T1}/events`, { type: "plan:answers", answers: { t: { done: true, note: "number 77" } }, actor: "user" });
        if (!done.ok || (await nodeOf(T1, "t")).status !== "completed") throw new Error("done did not close it: " + JSON.stringify(done));
      });
      for (const p of [W, L, T1]) await post(`api/plans/${p}/delete`, {});
    }

    // the same list in Italian
    await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
    const I = "hr-italian";
    await post("api/plans", { plan: listPlan(I, "Telegiornale") });
    await post(`api/plans/${I}/events`, { type: "plan:start", number: 1, actor: "chat" });
    await send("Page.navigate", { url: URL + "?lang=it" });
    await sleep(1800);
    await check("in Italian: the headings, the questions, the explanations, the labels, the count and the buttons are in Italian", async () => {
      await waitFor(`!!q("#hr-card[open]")`, "the window", 40);
      await need("the Italian list", [`${TOTAL} domande`, "Quante notizie vuoi?", "Da tre a nove, di solito sei.", "Parametro 30", "Invia tutte le risposte", "Più tardi", "4 senza risposta"]);
      const heads = await ev(`return JSON.stringify(qa("#hr-card .hr-group").map(e => e.textContent));`);
      if (heads !== JSON.stringify(["Cosa e quando", "Altri parametri", "Approvazioni"])) throw new Error("the headings: " + heads);
      const chosen = await ev(`return q('#hr-card .hr-item[data-id="i1"] select').selectedOptions[0].textContent;`);
      if (chosen !== "Sei notizie") throw new Error("the label: " + chosen);
    });
    await post(`api/plans/${I}/delete`, {});
    ws.close();
  } finally { proc.kill(); await sleep(400); try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* temp */ } }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "the run", ok: false, error: String(e && e.message || e) }])); });
