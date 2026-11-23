"use strict";
// Drives the grammar check in a real headless browser at three screen sizes: node langcheck_dom_check.js <url> <browser exe>. Prints [{name, ok, error}] as JSON
// (read by test_langcheck_page.py). The routes api/language-check* are answered by the page's own fetch stub, which records what the page sent; the conversation
// itself is the real one on the mock-engine server.
const { spawn } = require("child_process"), fs = require("fs"), os = require("os");
const [URL, EXE] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(os.tmpdir() + "/lcc-");
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
    const waitFor = async (expr, what, tries = 160) => { for (let i = 0; i < tries; i++) { if (await ev(`return (${expr});`)) return true; await sleep(250); } throw new Error(`never: ${what}`); };
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message + (pageErrors.length ? " | page errors: " + pageErrors.splice(0).join(" ;; ") : "") }); } };

    // the stub: a list that starts empty, is changed by what the page posts, and keeps what was asked
    const stub = `window.__lc = { sent: [], run: [], offers: [], list: {}, auto: true, fail: false };
      window.__real = window.fetch;
      const view = () => ({ auto_offer: window.__lc.auto, supported: [{ code: "hu", name: "Hungarian" }, { code: "de", name: "German" }, { code: "it", name: "Italian" }],
        providers: [{ id: "p1", name: "Hungarian editor", model: "hu-editor", is_local: true, runs_here: true }],
        languages: Object.fromEntries(Object.entries(window.__lc.list).map(([c, r]) => [c, { ...r, provider_name: "Hungarian editor", ok: true }])) });
      const reply = (o, s = 200) => Promise.resolve(new Response(JSON.stringify(o), { status: s, headers: { "Content-Type": "application/json" } }));
      window.fetch = (u, o) => {
        const url = String(u), body = o && o.body ? JSON.parse(o.body) : null;
        if (url.includes("api/language-check/offer")) { window.__lc.offers.push(body); return reply({ language: /Hungarian/.test(body.request) && window.__lc.list.hu && window.__lc.auto ? "hu" : "" }); }
        if (url.includes("api/language-check/run")) { window.__lc.run.push(body); return window.__lc.fail ? reply({ error: { message: "cannot reach Hungarian editor" } }, 400) : reply({ text: "Corrected: " + body.text, changed: 1, parts: 1, kept: 0, language: body.language }); }
        if (url.includes("api/language-check")) {
          if (body) { window.__lc.sent.push(body); if (body.set) window.__lc.list[body.set.language] = { provider: body.set.provider, model: body.set.model }; if (body.remove) delete window.__lc.list[body.remove]; if ("auto_offer" in body) window.__lc.auto = body.auto_offer; }
          return reply(view());
        }
        return window.__real(u, o);
      };`;
    const open = async (lang, tab) => {
      await send("Page.navigate", { url: URL + "?lang=" + lang });
      await sleep(1500);
      await waitFor(`!!q("#tab-settings") && !!q("#lk-root") && !!window.langCheck`, "the page");
      await ev(stub + " return 1;");
      if (tab === "settings") {
        await ev(`q("#tab-settings").click(); return 1;`);
        await waitFor(`qa("#viewsub button").some(b => /Inference|Inferenza/.test(b.textContent))`, "the sub-tabs");
        await ev(`qa("#viewsub button").find(b => /Inference|Inferenza/.test(b.textContent)).click(); return 1;`);
        await waitFor(`!q("#lk-section").hidden`, "the section");
        await ev(`document.dispatchEvent(new CustomEvent("tadamoo:view", { detail: "settings" })); return 1;`);
        await waitFor(`q("#lk-lang").options.length > 0`, "the list loaded");
      }
    };
    const fits = (what, sel) => ev(`const el = q(${JSON.stringify(sel)}); el.scrollIntoView();
      const small = qa(${JSON.stringify(sel + " button, " + sel + " select, " + sel + " input[type=text]")}).filter(e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0 && b.height < 43.5; }).map(e => (e.textContent.trim().slice(0, 18) || e.id));
      const box = el.getBoundingClientRect();
      return JSON.stringify({ wide: document.documentElement.scrollWidth > innerWidth + 1, inside: box.left >= -1 && box.right <= innerWidth + 1, small });`).then(v => {
      const f = JSON.parse(v);
      if (f.wide) throw new Error(what + ": the page scrolls sideways");
      if (!f.inside) throw new Error(what + ": it goes out of the screen");
      if (f.small.length) throw new Error(what + ": controls under 44 px: " + f.small.join(", "));
    });

    for (const [vname, w, h, mobile] of VIEWPORTS) {
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      await open("en", "settings");

      await check(`${vname}: the list starts empty, offers the languages and the local providers, and fits the screen`, async () => {
        const info = JSON.parse(await ev(`return JSON.stringify({ empty: !q("#lk-empty").hidden, langs: qa("#lk-lang option").map(o => o.value), provs: qa("#lk-prov option").map(o => o.value), add: q("#lk-add").disabled, auto: q("#lk-auto").checked });`));
        if (!info.empty || info.langs.join() !== "hu,de,it" || info.provs.join() !== "p1" || info.add || !info.auto) throw new Error(JSON.stringify(info));
        await fits("the section", "#lk-section");
      });

      await check(`${vname}: adding a language sends it and lists it with its model, removing it takes it off, the switch is sent`, async () => {
        await ev(`q("#lk-lang").value = "hu"; q("#lk-model").value = "hu-editor-q4"; q("#lk-add").click(); return 1;`);
        await waitFor(`qa("#lk-list li").length === 1`, "the row");
        const sent = JSON.parse(await ev(`return JSON.stringify(window.__lc.sent[0]);`));
        if (sent.set.language !== "hu" || sent.set.provider !== "p1" || sent.set.model !== "hu-editor-q4") throw new Error(JSON.stringify(sent));
        const row = await ev(`return q("#lk-list li").textContent;`);
        if (!/Hungarian/.test(row) || !/Hungarian editor/.test(row)) throw new Error("row: " + row);
        await fits("the section with a row", "#lk-section");
        await ev(`q("#lk-auto").click(); return 1;`);
        await waitFor(`window.__lc.auto === false`, "the switch");
        await ev(`q("#lk-auto").click(); return 1;`);
        await waitFor(`window.__lc.auto === true`, "the switch back");
        await ev(`qa("#lk-list li button")[0].click(); return 1;`);
        await waitFor(`qa("#lk-list li").length === 0 && !q("#lk-empty").hidden`, "the row removed");
        await ev(`q("#lk-lang").value = "hu"; q("#lk-model").value = ""; q("#lk-add").click(); return 1;`);          // left in the list for the chat checks
        await waitFor(`qa("#lk-list li").length === 1`, "the row again");
      });

      await check(`${vname}: a real answer in the Chat gets the Check grammar button; a translation request gets the offer, a plain request does not`, async () => {
        await ev(`q("#tab-chat").click(); return 1;`);
        await waitFor(`!!q("#prompt")`, "the composer");
        const ask = async text => {
          await ev(`const p = q("#prompt"); p.value = ${JSON.stringify(text)}; p.dispatchEvent(new Event("input")); q("#send").click(); return 1;`);
        };
        await waitFor(`!!q("#amsgs") && !q("#amsgs").hidden`, "the conversation view");
        const base = await ev(`return qa("#amsgs .aend .lkbtn").length;`);                      // answers of an earlier visit to the same conversation are there already
        await ask("Write one sentence about the sea.");
        await waitFor(`qa("#amsgs .aend .lkbtn").length === ${base + 1}`, "the button under the first answer");
        if (await ev(`return qa(".lkpanel").some(p => !p.hidden);`)) throw new Error("an offer for a plain request");
        await ask("Translate this into Hungarian: good morning");
        await waitFor(`qa("#amsgs .aend .lkbtn").length === ${base + 2}`, "the button under the second answer");
        await waitFor(`qa(".lkpanel").some(p => !p.hidden)`, "the offer");
        const offer = JSON.parse(await ev(`const p = qa(".lkpanel").find(p => !p.hidden); return JSON.stringify({ text: p.querySelector(".lknote").textContent, lang: p.querySelector("select").value });`));
        if (!/translation into Hungarian/.test(offer.text) || offer.lang !== "hu") throw new Error(JSON.stringify(offer));
        const offers = JSON.parse(await ev(`return JSON.stringify(window.__lc.offers);`));
        if (offers.length !== 2 || !/Translate this into Hungarian/.test(offers[1].request)) throw new Error("offers asked: " + JSON.stringify(offers));
        await fits("the offer", ".lkpanel:not([hidden])");
      });

      await check(`${vname}: Check sends the answer text and the language, shows the corrected text to copy, and an error is said`, async () => {
        await ev(`qa(".lkpanel").find(p => !p.hidden).querySelector(".btn.primary").click(); return 1;`);
        await waitFor(`qa(".lkpanel").some(p => !p.hidden && p.querySelector("textarea"))`, "the corrected text");
        const run = JSON.parse(await ev(`return JSON.stringify(window.__lc.run);`));
        if (run.length !== 1 || run[0].language !== "hu" || !run[0].text.length) throw new Error(JSON.stringify(run));
        const shown = JSON.parse(await ev(`const p = qa(".lkpanel").find(p => !p.hidden); return JSON.stringify({ area: p.querySelector("textarea").value, note: p.querySelector(".lknote").textContent, ro: p.querySelector("textarea").readOnly });`));
        if (shown.area !== "Corrected: " + run[0].text || !shown.ro || !/1 of 1 part\(s\) corrected/.test(shown.note)) throw new Error(JSON.stringify(shown));
        await ev(`window.__lc.fail = true; qa(".lkpanel").find(p => !p.hidden).querySelector(".btn.primary").click(); return 1;`);
        await waitFor(`qa(".lkpanel").some(p => !p.hidden && /cannot reach Hungarian editor/.test(p.querySelector(".lknote").textContent))`, "the error");
        await ev(`window.__lc.fail = false; return 1;`);
      });
    }

    await check("Italian: the button and the settings title are in Italian", async () => {
      await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
      await open("it", "settings");
      const t = await ev(`return q("#lk-h").textContent + "|" + q("#lk-add").textContent;`);
      if (t !== "Verifica della lingua|Aggiungi o cambia") throw new Error(t);
    });
    if (pageErrors.length) out.push({ name: "page errors", ok: false, error: pageErrors.join(" ;; ") });
    ws.close();
  } finally { proc.kill(); }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "driver", ok: false, error: String(e && e.stack || e) }])); });
