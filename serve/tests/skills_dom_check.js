"use strict";
// Drives Settings > Skills and the /install-skill command in a real headless browser at three screen sizes: node skills_dom_check.js <url> <browser exe> <folder with two skills>.
// Prints [{name, ok, error}] as JSON (read by test_skills_page.py). One page, actions in sequence on the buttons a person presses, each checked against what the server then holds
// (the state of the skill, and what the harness sees). Only a skill with no models is ever installed here: nothing is downloaded.
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE, FIXTURE] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const get = async p => (await fetch(URL + p)).json();
const post = async (p, body) => (await fetch(URL + p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "skc-"));
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
    // the card of a skill, found by the name it shows; the button of a card, by its words
    const card = name => `qa("#sk-root .sk-sec .sk-item").find(li => li.querySelector(".sk-name") && li.querySelector(".sk-name").textContent === ${JSON.stringify(name)})`;
    const press = (name, label) => ev(`const li = ${card(name)}; if (!li) throw new Error("no card ${name}"); const b = [...li.querySelectorAll("button")].find(x => x.textContent.trim() === ${JSON.stringify(label)}); if (!b) throw new Error("no button ${label} in ${name}"); b.scrollIntoView(); b.click(); return 1;`);
    const stateOf = name => ev(`const li = ${card(name)}; return li ? li.dataset.state : "";`);
    /* no sideways scroll, and every control of the tab that is shown is at least 44 px high */
    const fits = what => ev(`const small = qa("#sk-root button, #sk-root input, #sk-root summary").filter(e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0 && b.height < 43.5; }).map(e => e.textContent.trim().slice(0, 20) || e.id);
      return JSON.stringify({ wide: document.documentElement.scrollWidth > innerWidth + 1, small, shown: !!q("#sk-root") && q("#sk-root").offsetParent !== null });`).then(v => {
      const f = JSON.parse(v);
      if (!f.shown) throw new Error(what + ": the tab is not shown");
      if (f.wide) throw new Error(what + ": the page scrolls sideways");
      if (f.small.length) throw new Error(what + ": controls under 44 px: " + f.small.join(", "));
    });
    const openTab = async () => { await waitFor(`!!q("#tab-settings") && typeof view === "function"`, "the page"); await ev(`q("#tab-settings").click(); return 1;`); await waitFor(`!!q("#sub-skills")`, "the Skills sub-tab"); await ev(`q("#sub-skills").click(); return 1;`); await waitFor(`qa("#sk-root .sk-item").length > 3`, "the list of skills"); };
    const capState = async id => ((await get("api/capabilities")).entries.find(e => e.id === id) || {}).state;

    for (const [vname, w, h, mobile] of VIEWPORTS) {
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      await send("Page.navigate", { url: URL + "?lang=en" });
      await sleep(1500);
      const names = ["alpha-skill", "beta-skill"];
      for (const n of names) await post("api/skills/remove", { name: n });                  // each size starts from nothing installed of these
      await openTab();

      await check(`${vname}: the tab lists what ships with its state, what it needs and what it downloads, and fits the screen`, async () => {
        await waitFor(`${card("Video modification")}`, "a card");
        const info = await ev(`const li = ${card("Video modification")}; return JSON.stringify({ text: li.textContent, state: li.dataset.state });`).then(JSON.parse);
        if (info.state !== "not_installed" || !/Not installed/.test(info.text)) throw new Error("state not shown: " + info.text);
        if (!/Downloads about 60 GB of models/.test(info.text)) throw new Error("the size is not said before installing: " + info.text);
        const pics = await ev(`return ${card("Picture generation")}.textContent`);
        if (!/Downloads about 10\.5 GB of models/.test(pics)) throw new Error("the size of the pictures skill is not said: " + pics);
        const heads = await ev(`return qa("#sk-root .sk-sec h3").map(e => e.textContent).join("|")`);
        if (!/Installed \(0\)\|Available to install \(\d+\)/.test(heads)) throw new Error("groups: " + heads);
        const stray = await ev(`return /\\b(null|undefined|NaN)\\b/.test(q("#sk-root").textContent)`);
        if (stray) throw new Error("a value that is not text is shown on the page: " + await ev(`return q("#sk-root").textContent.slice(0, 200)`));
        await fits("skills tab");
      });

      await check(`${vname}: an address is read first, shows what it holds, and only the chosen skill is installed`, async () => {
        await ev(`const i = q("#sk-address-tab"); i.focus(); i.value = ${JSON.stringify(FIXTURE)}; i.dispatchEvent(new Event("input", { bubbles: true })); i.form.querySelector("button[type=submit]").click(); return 1;`);
        await waitFor(`qa("#sk-root .sk-found .sk-item").length === 2`, "the two skills of the folder");
        if ((await get("api/skills")).skills.some(r => names.includes(r.name))) throw new Error("something was installed before the button");
        await fits("found list");
        await ev(`const li = qa("#sk-root .sk-found .sk-item").find(x => x.textContent.includes("alpha-skill")); [...li.querySelectorAll("button")].find(b => b.textContent.trim() === "Install").click(); return 1;`);
        await waitFor(`!!${card("alpha-skill")}`, "alpha-skill among the installed", 120);
        if (await stateOf("alpha-skill") !== "installed") throw new Error("state " + await stateOf("alpha-skill"));
        if ((await get("api/skills")).skills.some(r => r.name === "beta-skill")) throw new Error("beta-skill was installed too");
        if (await capState("skill:alpha-skill") !== "approved") throw new Error("the harness does not see it: " + await capState("skill:alpha-skill"));
      });

      await check(`${vname}: turn off and turn on change what the harness sees and say it in words`, async () => {
        await press("alpha-skill", "Turn off");
        await waitFor(`${card("alpha-skill")} && ${card("alpha-skill")}.dataset.state === "disabled"`, "disabled");
        if (!/Installed, off/.test(await ev(`return ${card("alpha-skill")}.textContent`))) throw new Error("the state is not in words");
        if (await capState("skill:alpha-skill") !== "off") throw new Error("the harness still sees it");
        if (!/the assistant does not see it/.test(await ev(`return q("#sk-root .sk-msg").textContent`))) throw new Error("no message");
        await fits("after turn off");
        await press("alpha-skill", "Turn on");
        await waitFor(`${card("alpha-skill")}.dataset.state === "installed"`, "on again");
        if (await capState("skill:alpha-skill") !== "approved") throw new Error("not seen again");
      });

      await check(`${vname}: remove says what happens to the models, and removes only on the second press`, async () => {
        await press("alpha-skill", "Remove");
        await waitFor(`!!${card("alpha-skill")}.querySelector(".sk-ask")`, "the question");
        const ask = await ev(`return ${card("alpha-skill")}.querySelector(".sk-ask").textContent`);
        if (!/no models of its own/.test(ask)) throw new Error("the models are not mentioned: " + ask);
        if (await stateOf("alpha-skill") !== "installed") throw new Error("removed before the answer");
        await fits("remove question");
        await ev(`const b = [...${card("alpha-skill")}.querySelectorAll("button")].find(x => x.textContent.trim() === "Yes, remove it"); b.click(); return 1;`);
        await waitFor(`!${card("alpha-skill")}`, "gone from the list");
        if ((await get("api/skills")).skills.some(r => r.name === "alpha-skill")) throw new Error("still installed");
        if (await capState("skill:alpha-skill")) throw new Error("the harness still sees it");
      });

      await check(`${vname}: /install-skill with a conversation puts the panel in it, shows what a skill weighs and installs only on the button`, async () => {
        await ev(`q("#tab-chat").click(); return 1;`);
        await waitFor(`!!q("#prompt") && q("#prompt").offsetParent !== null`, "the message box");
        await ev(`const s = q("#chatmode"); s.value = "agent"; s.dispatchEvent(new Event("change", { bubbles: true })); const p = q("#prompt"); p.value = "/install-skill video-modification"; p.dispatchEvent(new Event("input", { bubbles: true })); q("#send").click(); return 1;`);
        await waitFor(`!!q("#amsgs .sk-chat .sk-found")`, "the preview in the conversation");
        const t = await ev(`return q("#amsgs .sk-chat .sk-found").textContent`);
        if (!/Install “Video modification”\?/.test(t) || !/about 60 GB/.test(t)) throw new Error("the preview does not say what it is: " + t);
        if ((await get("api/skills")).skills.find(r => r.name === "video-modification").state !== "not_installed") throw new Error("installed before the button");
        const wide = await ev(`return document.documentElement.scrollWidth > innerWidth + 1`);
        if (wide) throw new Error("the page scrolls sideways with the panel in the conversation");
        await ev(`[...q("#amsgs .sk-chat .sk-found").querySelectorAll("button")].find(b => b.textContent.trim() === "Cancel").click(); return 1;`);
        await waitFor(`!q("#amsgs .sk-chat .sk-found")`, "the preview closed");
      });

      await check(`${vname}: /install-skill with no conversation opens Settings > Skills, and a skill with no models is installed from its preview`, async () => {
        const about = (await get("api/skills")).skills.find(r => r.name === "gamma-skill");
        if (about.has_models || about.state !== "not_installed") throw new Error("the test skill must have no models and not be installed: " + JSON.stringify(about));
        await ev(`q("#tab-chat").click(); const s = q("#chatmode"); s.value = "direct"; s.dispatchEvent(new Event("change", { bubbles: true })); await wait(300); const p = q("#prompt"); p.value = "/install-skill gamma-skill"; p.dispatchEvent(new Event("input", { bubbles: true })); q("#send").click(); return 1;`);
        await waitFor(`q("#sub-skills") && q("#sub-skills").getAttribute("aria-selected") === "true" && !!q("#sk-root .sk-found")`, "the Skills tab with the preview", 40);
        const t = await ev(`return q("#sk-root .sk-found").textContent`);
        if (!/Install “Gamma skill”\?/.test(t) || !/Nothing else to download/.test(t)) throw new Error("preview: " + t);
        await fits("preview in the tab");
        await ev(`[...q("#sk-root .sk-found").querySelectorAll("button")].find(b => b.textContent.trim() === "Install").click(); return 1;`);
        await waitFor(`!!${card("Gamma skill")} && ${card("Gamma skill")}.dataset.state === "installed"`, "installed", 120);
        if (await capState("skill:gamma-skill") !== "approved") throw new Error("the harness does not see it");
        await post("api/skills/remove", { name: "gamma-skill" });
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
