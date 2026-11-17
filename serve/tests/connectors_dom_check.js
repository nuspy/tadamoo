"use strict";
// Drives the Connectors tab in a real headless browser at three screen sizes: node connectors_dom_check.js <url> <browser exe>.
// Prints [{name, ok, error}] as JSON (read by test_connectors_page.py). One page, actions in sequence on the buttons a person presses, each checked against what the server then holds
// (the state of the skill, and what the harness sees). Only a skill with no models is ever installed here: nothing is downloaded.
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE, FOLDER] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const get = async p => (await fetch(URL + p)).json();
const until = async (fn, what) => { for (let i = 0; i < 80; i++) { if (await fn()) return; await new Promise(r => setTimeout(r, 250)); } throw new Error("never: " + what); };
const post = async (p, body) => (await fetch(URL + p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "cxc-"));
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
    const item = name => `qa("#cx-root .cx-item").find(li => li.querySelector(".cx-name").textContent === ${JSON.stringify(name)})`;
    const press = (name, label) => ev(`const li = ${item(name)}; if (!li) throw new Error("no item ${name}"); const b = [...li.querySelectorAll("button")].find(x => x.textContent.trim() === ${JSON.stringify(label)}); if (!b) throw new Error("no button ${label}"); b.click(); return 1;`);
    const fits = what => ev(`const small = qa("#cx-root button, #cx-root input, #cx-root select").filter(e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0 && b.height < 43.5; }).map(e => e.textContent.trim() || e.id);
      return JSON.stringify({ wide: document.documentElement.scrollWidth > innerWidth + 1, small, shown: !!q("#cx-root") && q("#cx-root").offsetParent !== null });`).then(v => {
      const f = JSON.parse(v);
      if (!f.shown) throw new Error(what + ": the tab is not shown");
      if (f.wide) throw new Error(what + ": the page scrolls sideways");
      if (f.small.length) throw new Error(what + ": controls under 44 px: " + f.small.join(", "));
    });
    const press2 = label => ev(`const b = [...qa("#cx-root .cx-item .cx-acts button")].find(x => x.textContent.trim() === ${JSON.stringify(label)}); if (!b) throw new Error("no button ${label}"); b.click(); return 1;`);
    const capState = async id => ((await get("api/capabilities")).entries.find(e => e.id === id) || {}).state;
    const calls = [];

    for (const [vname, w, h, mobile] of VIEWPORTS) {
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      await send("Page.navigate", { url: URL + "?lang=en" });
      await sleep(1500);
      await post("api/capabilities/settings", { off: [] });                          // each size starts with everything on
      await waitFor(`!!q("#tab-connectors") && typeof view === "function"`, "the page");
      await ev(`q("#tab-connectors").click(); return 1;`);
      await waitFor(`!!${item("alpha-skill")}`, "the skills of the temporary home");

      await check(`${vname}: the tab lists the skills found with their state and fits the screen`, async () => {
        const heads = await ev(`return qa("#cx-root .cx-bh").map(e => e.textContent).join("|")`);
        if (heads !== "Installed (2)|Available to install (0)") throw new Error("bands: " + heads);
        const text = await ev(`return ${item("alpha-skill")}.textContent`);
        if (!/On/.test(text) || !/user/.test(text)) throw new Error("state or source not shown: " + text);
        const scopes = await ev(`return qa("#cx-scope button").map(b => b.textContent).join("|")`);
        if (scopes !== "All 2|Personal 0|External 2") throw new Error("scope counts: " + scopes);
        const stray = await ev(`return /\b(null|undefined|NaN)\b/.test(q("#cx-root").textContent)`);
        if (stray) throw new Error("a value that is not text is shown: " + await ev(`return q("#cx-root").textContent.slice(0, 200)`));
        await fits("connectors tab");
      });

      await check(`${vname}: the filter keeps the skills that hold the words`, async () => {
        await ev(`const i = q("#cx-filter"); i.value = "beta"; i.dispatchEvent(new Event("input", { bubbles: true })); return 1;`);
        await waitFor(`qa("#cx-root .cx-item").length === 1 && !!${item("beta-skill")}`, "only beta");
        await ev(`const i = q("#cx-filter"); i.value = ""; i.dispatchEvent(new Event("input", { bubbles: true })); return 1;`);
        await waitFor(`qa("#cx-root .cx-item").length === 2`, "both again");
      });

      await check(`${vname}: switching a skill off and on changes what the server holds`, async () => {
        await press("alpha-skill", "Switch off");
        await waitFor(`${item("alpha-skill")}.textContent.includes("Off")`, "alpha shown off");
        if ((await capState("skill:alpha-skill")) !== "off") throw new Error("the server still has it on");
        const view = await get("api/connectors?kind=skill");
        if (view.installed.find(i => i.id === "skill:alpha-skill").state !== "disabled") throw new Error("the connectors API does not say disabled");
        if ((await capState("skill:beta-skill")) !== "approved") throw new Error("another skill changed");
        await press("alpha-skill", "Switch on");
        await waitFor(`${item("alpha-skill")}.textContent.includes("On")`, "alpha shown on");
        if ((await capState("skill:alpha-skill")) !== "approved") throw new Error("the server still has it off");
      });

      await check(`${vname}: a click shows the description, a second click closes it, and the scope switch empties the list`, async () => {
        await ev(`${item("beta-skill")}.querySelector(".cx-open").click(); return 1;`);
        await waitFor(`${item("beta-skill")}.querySelector(".cx-desc") && ${item("beta-skill")}.querySelector(".cx-desc").textContent.includes("Does the beta-skill thing")`, "the description");
        await fits("detail");
        await ev(`${item("beta-skill")}.querySelector(".cx-open").click(); return 1;`);
        await waitFor(`!${item("beta-skill")}.querySelector(".cx-desc")`, "closed");
        await ev(`[...qa("#cx-scope button")].find(b => b.textContent.startsWith("Personal")).click(); return 1;`);
        await waitFor(`qa("#cx-root .cx-item").length === 0`, "no personal skill");
        await ev(`[...qa("#cx-scope button")].find(b => b.textContent.startsWith("All")).click(); return 1;`);
        await waitFor(`qa("#cx-root .cx-item").length === 2`, "all again");
      });

      await check(`${vname}: the Providers sub-tab lists the providers, adds a folder, switches it off and removes it, as the server then holds`, async () => {
        await ev(`[...qa("#cx-subtabs button")].find(b => b.textContent === "Providers").click(); return 1;`);
        await waitFor(`qa("#cx-root .cx-item").length >= 0 && !!q("#cx-pr")`, "the providers form");
        const OFFER = path.join(FOLDER, "..", "offered");
        fs.mkdirSync(path.join(OFFER, "gamma-skill"), { recursive: true });
        fs.writeFileSync(path.join(OFFER, "gamma-skill", "SKILL.md"), ["---", "name: gamma-skill", "description: Offered by the provider.", "---", "", "Do it.", ""].join("\n"));
        const rows = await get("api/connectors/providers");
        if (rows.providers.length !== 0) throw new Error("the test starts with no provider: " + JSON.stringify(rows.providers));
        await ev(`const k = q("#cx-pk"); k.value = "folder"; const r = q("#cx-pr"); r.value = ${JSON.stringify(OFFER)}; r.dispatchEvent(new Event("input", { bubbles: true })); q(".cx-provform button[type=submit]").click(); return 1;`);
        await waitFor(`qa("#cx-root .cx-item").length === 1`, "the new provider row");
        if ((await get("api/connectors/providers")).providers.length !== 1) throw new Error("the server did not keep the provider");
        await fits("providers tab");
        await ev(`[...qa("#cx-subtabs button")].find(b => b.textContent === "Skills").click(); return 1;`);
        const offered = `qa("#cx-root .cx-item").find(li => li.querySelector(".cx-name").textContent === "gamma-skill" && li.querySelector(".cx-src").textContent === ${JSON.stringify(OFFER)})`;
        await waitFor(`!!${offered}`, "the skill the provider lists");
        await ev(`${offered}.querySelector(".cx-open").click(); return 1;`);
        await waitFor(`!!q("#cx-review-go")`, "the review button on an item that is not installed");
        await ev(`q("#cx-review-go").click(); return 1;`);
        await waitFor(`!!q("#cx-root .cx-facts") && !!q("#cx-root .cx-verdict")`, "the facts sheet and the verdict");
        if (!/only text for the model/.test(await ev(`return q("#cx-root .cx-review").textContent`))) throw new Error("the sheet does not say the skill is only text");
        await fits("review");
        await waitFor(`!!q("#cx-install-go")`, "the install button after the review");
        await ev(`q("#cx-install-go").click(); return 1;`);                                  // the first press only asks again
        await waitFor(`q("#cx-install-go") && q("#cx-install-go").textContent.startsWith("Press again")`, "the second press asked for");
        if ((await get("api/capabilities")).entries.some(e => e.origin === "tadamoo")) throw new Error("one press already installed it");
        await ev(`q("#cx-install-go").click(); return 1;`);
        await until(async () => (await get("api/capabilities")).entries.some(e => e.id === "skill:gamma-skill" && e.origin === "tadamoo"), "the skill installed by the tab");
        await ev(`[...qa("#cx-subtabs button")].find(b => b.textContent === "Skills").click(); return 1;`);
        await waitFor(`qa("#cx-root .cx-item").some(li => li.querySelector(".cx-src") && li.querySelector(".cx-src").textContent === "tadamoo")`, "the installed row");
        await ev(`const b = qa('#cx-root [id^="cx-uninstall-"]')[0]; if (!b) throw new Error("no uninstall button"); b.click(); return 1;`);
        await waitFor(`qa('#cx-root [id^="cx-uninstall-"]')[0].textContent.startsWith("Press again")`, "the uninstall asks again");
        await ev(`qa('#cx-root [id^="cx-uninstall-"]')[0].click(); return 1;`);
        await until(async () => !(await get("api/capabilities")).entries.some(e => e.origin === "tadamoo"), "the skill removed again");
        await ev(`[...qa("#cx-subtabs button")].find(b => b.textContent === "Providers").click(); return 1;`);
        await waitFor(`!!q("#cx-orig-dir")`, "the originals folder field");
        await fits("providers with originals");
        await ev(`[...qa("#cx-subtabs button")].find(b => b.textContent === "Providers").click(); return 1;`);
        await waitFor(`!!q("#cx-pr")`, "the providers form again");
        await press2("Switch off");
        await waitFor(`qa("#cx-root .cx-item")[0].textContent.includes("Off")`, "provider shown off");
        if ((await get("api/connectors/providers")).providers[0].enabled !== false) throw new Error("the server still has it on");
        await press2("Remove");
        await waitFor(`qa("#cx-root .cx-item").length === 0`, "provider removed");
        if ((await get("api/connectors/providers")).providers.length !== 0) throw new Error("the server still lists it");
        await ev(`[...qa("#cx-subtabs button")].find(b => b.textContent === "Skills").click(); return 1;`);
        await waitFor(`!!${item("alpha-skill")}`, "the skills again");
      });

      await check(`${vname}: a plugin opens its dependency tree in a window of its own, where a part is switched off and on again`, async () => {
        const tag = vname.replace(/\W+/g, "-"), pname = "demo-" + tag;
        const plugin = path.join(FOLDER, "..", "plugins", pname, "skills", "pl-skill-" + tag);
        fs.mkdirSync(plugin, { recursive: true });
        fs.writeFileSync(path.join(plugin, "SKILL.md"), ["---", "name: pl-skill-" + tag, "description: A skill the plugin brings.", "---", "", "Do it.", ""].join("\n"));
        await post("api/capabilities/rescan", {});                                 // the index reads the new plugin
        await ev(`[...qa("#cx-subtabs button")].find(b => b.textContent === "Plugins").click(); return 1;`);
        await waitFor(`!!${item(pname)}`, "the plugin row");
        await ev(`${item(pname)}.querySelector(".cx-open").click(); return 1;`);
        await waitFor(`!!q("#cx-tree-open")`, "the open-tree button");
        await ev(`q("#cx-tree-open").click(); return 1;`);
        await waitFor(`!!q("#cx-treedlg[open]") && qa("#cx-treedlg .cx-leaf").length === 2`, "the tree window with the plugin and its skill");
        await fits("tree window");
        const toggle = `q('#cx-treedlg button[id^="cx-tn-"]')`;
        await ev(`${toggle}.click(); return 1;`);
        await waitFor(`qa("#cx-treedlg .cx-leaf")[1].classList.contains("cx-disabled")`, "the part switched off");
        await ev(`${toggle}.click(); return 1;`);
        await waitFor(`qa("#cx-treedlg .cx-leaf")[1].classList.contains("cx-active")`, "the part switched on again");
        await ev(`q("#cx-tree-close").click(); return 1;`);
        await waitFor(`!q("#cx-treedlg")`, "the window closed");
        fs.rmSync(path.join(FOLDER, "..", "plugins"), { recursive: true, force: true });          // the next size starts without the plugin
        await post("api/capabilities/rescan", {});
        await ev(`[...qa("#cx-subtabs button")].find(b => b.textContent === "Skills").click(); return 1;`);
        await waitFor(`!!${item("alpha-skill")}`, "the skills again");
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
