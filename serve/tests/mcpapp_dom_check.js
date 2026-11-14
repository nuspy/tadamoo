"use strict";
// MCP Apps in a real headless browser at three screen sizes, in English and Italian:
// node mcpapp_dom_check.js <url> <browser exe> <project folder> <json: {sid, template, key}>.
// A conversation saved with a tool call that returned the demo app (tools/mcp-app-demo) is opened; the page's backend to the conversation's process is replaced by a fake
// that records what the app asks and answers it. Prints [{name, ok, error}] as JSON (read by test_mcpapp_page.py).
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE, FOLDER, WORLD] = process.argv.slice(2);
const world = JSON.parse(WORLD);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "mad-"));
  const proc = spawn(EXE, ["--headless=new", "--lang=en-US", "--disable-gpu", `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, "--window-size=1440,900", "about:blank"], { stdio: "ignore" });
  const out = [];
  try {
    let targets = [];
    for (let i = 0; i < 40 && !targets.length; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch (e) { await sleep(250); } }
    const ws = new WebSocket(targets.find(t => t.type === "page").webSocketDebuggerUrl);
    await new Promise(r => { ws.onopen = r; });
    let id = 0; const waits = new Map(), contexts = new Map(), pageErrors = [];
    ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      if (m.id && waits.has(m.id)) { waits.get(m.id)(m.result || m.error); waits.delete(m.id); }
      else if (m.method === "Runtime.exceptionThrown") pageErrors.push(((m.params.exceptionDetails.exception || {}).description || m.params.exceptionDetails.text || "").slice(0, 300));
      else if (m.method === "Runtime.executionContextCreated") contexts.set(m.params.context.id, m.params.context);
      else if (m.method === "Runtime.executionContextDestroyed") contexts.delete(m.params.executionContextId);
      else if (m.method === "Runtime.executionContextsCleared") contexts.clear();
    };
    const send = (method, params = {}) => new Promise((r, rej) => { const n = ++id; const t = setTimeout(() => rej(new Error(`${method} did not answer`)), 60000); waits.set(n, v => { clearTimeout(t); r(v); }); ws.send(JSON.stringify({ id: n, method, params })); });
    const run = async (expr, contextId) => {
      const r = await send("Runtime.evaluate", { expression: `(async () => { const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)]; const wait = ms => new Promise(r => setTimeout(r, ms)); ${expr} })()`, awaitPromise: true, returnByValue: true, ...(contextId ? { contextId } : {}) });
      if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception && r.exceptionDetails.exception.description));
      return r.result.value;
    };
    const ev = expr => run(expr);
    /* the app's own document: the context whose address is about:srcdoc */
    const inner = async () => {
      for (const c of contexts.values()) {
        try { const href = await run("return location.href", c.id); if (href === "about:srcdoc") return c.id; } catch (e) { /* a context that went away */ }
      }
      return 0;
    };
    const app = async expr => { const c = await inner(); if (!c) throw new Error("the app's document is not there"); return run(expr, c); };
    await send("Page.enable"); await send("Runtime.enable");
    const waitFor = async (expr, what, tries = 75, ctx) => { for (let i = 0; i < tries; i++) { try { if (await run(`return (${expr});`, ctx)) return true; } catch (e) { /* not yet */ } await sleep(200); } throw new Error(`never: ${what}`); };
    const waitApp = async (expr, what, tries = 75) => { for (let i = 0; i < tries; i++) { try { const c = await inner(); if (c && await run(`return (${expr});`, c)) return true; } catch (e) { /* not yet */ } await sleep(200); } throw new Error(`never (in the app): ${what}`); };
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message + (pageErrors.length ? " | page errors: " + pageErrors.splice(0).join(" ;; ") : "") }); } process.stderr.write(`${out[out.length - 1].ok ? "ok  " : "FAIL"} ${name} ${out[out.length - 1].error.slice(0, 600)}\n`); };
    const fits = what => ev(`const root = document.documentElement; return JSON.stringify({ wide: root.scrollWidth > innerWidth + 1 })`).then(v => { if (JSON.parse(v).wide) throw new Error(what + ": the page scrolls sideways"); });

    const WORDS = { en: { open: "Open", show: "Show" }, it: { open: "Apri", show: "Mostra" } };
    for (const lang of ["en", "it"]) {
      const W = WORDS[lang];
      for (const [vname, w, h, mobile] of VIEWPORTS) {
        const tag = `${vname}, ${lang}`;
        await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
        await send("Page.navigate", { url: URL + `?lang=${lang}` });
        await sleep(1200);
        await ev(`try { localStorage.setItem("tadamoo.agent.project", ${JSON.stringify(FOLDER)}); localStorage.setItem("tadamoo.chatmode", "agent"); } catch (e) {} return 1;`);
        await send("Page.navigate", { url: URL + `?lang=${lang}` });
        await waitFor(`!!q("#tab-agent") && !!window.agentChat && !!window.mcpApps`, "the page");
        await ev(`q("#tab-agent").click(); return 1;`);
        await waitFor(`q("#prompt") && q("#prompt").offsetParent !== null`, "the message box");
        // the page's side of the conversation's process, replaced: what an app asks is recorded and answered
        await ev(`window.__asked = []; window.__links = []; window.__sent = []; window.__confirm = 0;
          window.mcpApps.setBackend(async (sid, op, params) => {
            window.__asked.push({ sid, op, params });
            if (op === "tools/call" && params.name === "compute_series") return { result: { content: [{ type: "text", text: "5 points" }], structuredContent: { series: [1, 5, 2, 8, 3] } } };
            if (op === "tools/call" && params.name === "save_answers") return { result: { content: [{ type: "text", text: "saved " + params.arguments.choice + " (1 so far)" }] } };
            if (op === "tools/call") return { error: { code: -32000, message: "no such tool" } };
            return { result: {} };
          });
          window.open = (u) => { window.__links.push(u); return null; };
          window.agentChat.sendFromApp = async (t, owner) => { window.__sent.push([t, owner]); return true; };
          window.mcpApps.reset();
          return 1;`);
        await ev(`await window.agentChat.open(${JSON.stringify(world.sid)}); return 1;`);
        await waitFor(`qa(".mcpph").length === 1`, "the placeholder of the saved app");

        await check(`${tag}: a saved conversation shows the app as a compact placeholder in the tool's bubble, with no window and no frame until it is opened`, async () => {
          const f = JSON.parse(await ev(`const p = q(".mcpph"), b = p.querySelector("button"), r = b.getBoundingClientRect();
            return JSON.stringify({ text: p.textContent, button: b.textContent, h: r.height, w: r.width, inBubble: !!p.closest(".atool"), frames: qa("iframe").length, wins: qa(".mcpwin").filter(x => !x.hidden).length });`));
          if (!/Survey/.test(f.text) || !/app-demo/.test(f.text)) throw new Error("placeholder: " + f.text);
          if (f.button !== W.open) throw new Error("button says " + f.button);
          if (f.h < 43.5 || f.w < 43.5) throw new Error("button too small " + f.w + "x" + f.h);
          if (!f.inBubble || f.frames || f.wins) throw new Error("bubble/frames/windows: " + JSON.stringify(f));
          await fits("the placeholder");
        });

        await check(`${tag}: opening it shows the window (a panel beside the conversation, full screen on a phone), starts the app in a sandbox of another origin and hands it its input and its result`, async () => {
          await ev(`q(".mcpph button").click(); return 1;`);
          await waitFor(`!!q(".mcpwin") && !q(".mcpwin").hidden`, "the window");
          await waitFor(`window.mcpApps.all()[0].state === "ready"`, "the app ready", 100);
          const f = JSON.parse(await ev(`const win = q(".mcpwin"), r = win.getBoundingClientRect(), chat = q("#amsgs").getBoundingClientRect(), frame = win.querySelector("iframe"), v = window.mcpApps.all()[0];
            return JSON.stringify({ display: win.dataset.display, left: r.left, top: r.top, right: r.right, bottom: r.bottom, chatRight: chat.right, vw: innerWidth, vh: innerHeight, sandbox: frame.getAttribute("sandbox"),
              origin: new URL(frame.src).origin, page: location.origin, appInfo: v.appInfo, caps: v.appCaps, role: win.getAttribute("role") });`));
          if (f.display !== "panel") throw new Error("display " + f.display);
          if (f.sandbox !== "allow-scripts allow-same-origin") throw new Error("sandbox flags " + f.sandbox);
          if (f.origin === f.page) throw new Error("the sandbox has the origin of the page");
          if (!f.appInfo || f.appInfo.name !== "Survey") throw new Error("handshake: " + JSON.stringify(f.appInfo));
          if (mobile) { if (f.left > 0.5 || f.top > 0.5 || Math.abs(f.right - f.vw) > 1 || Math.abs(f.bottom - f.vh) > 1) throw new Error("not full screen: " + JSON.stringify(f)); }
          else if (f.left < f.chatRight - 2) throw new Error("the panel overlaps the conversation: " + JSON.stringify(f));
          await waitApp(`document.getElementById("title").textContent === "Lunch"`, "the input shown");
          await waitApp(`document.querySelectorAll("#chart polyline").length === 1 && document.getElementById("status").textContent.includes("Result received")`, "the result drawn");
          const info = await app(`return document.getElementById("info").textContent`);
          if (!/theme dark/.test(info) || !new RegExp(lang === "it" ? "locale it-IT" : "locale en-US").test(info) || !/mode inline/.test(info) || !/platform (mobile|web)/.test(info) || !/box \{"width"/.test(info)) throw new Error("host context: " + info);
          await fits("the open window");
        });

        await check(`${tag}: the app calls its server's tools, tells the model, sends a message and opens a link, each through the host`, async () => {
          await ev(`window.__asked.length = 0; return 1;`);
          await app(`document.getElementById("n").value = "5"; document.getElementById("update").click(); return 1;`);
          await waitApp(`document.getElementById("status").textContent === "Chart updated" && document.querySelector("#chart polyline").getAttribute("points").split(" ").length === 5`, "the chart updated by a proxied tool call");
          await app(`document.getElementById("tell").click(); return 1;`);
          await waitApp(`document.getElementById("status").textContent.includes("will know")`, "the context accepted");
          await app(`document.getElementById("save").click(); return 1;`);
          await waitApp(`document.getElementById("status").textContent.includes("Saved: saved Yes")`, "the save answered");
          await app(`document.getElementById("say").click(); return 1;`);
          await waitApp(`document.getElementById("status").textContent === "Message sent"`, "the message accepted");
          await app(`document.getElementById("link").click(); return 1;`);
          await waitApp(`document.getElementById("status").textContent === "Link opened"`, "the link accepted");
          const f = JSON.parse(await ev(`return JSON.stringify({ asked: window.__asked.filter(a => a.op !== "log"), links: window.__links, sent: window.__sent, sid: ${JSON.stringify(world.sid)}, server: ${JSON.stringify(world.server)}, app: window.mcpApps.all()[0].rec.app })`));
          const ops = f.asked.map(a => a.op + (a.op === "tools/call" ? ":" + a.params.name : ""));
          if (JSON.stringify(ops) !== JSON.stringify(["tools/call:compute_series", "context", "tools/call:save_answers"])) throw new Error("asked " + JSON.stringify(ops));
          if (f.asked.some(a => a.sid !== f.sid || a.params.server !== f.server || a.params.app !== f.app)) throw new Error("a request did not name its own conversation and server: " + JSON.stringify(f.asked));
          if (f.asked[1].params.content[0].text !== "The person chose: Yes" || f.asked[1].params.structuredContent.choice !== "Yes") throw new Error("context: " + JSON.stringify(f.asked[1].params));
          if (JSON.stringify(f.sent) !== JSON.stringify([["I chose Yes in the survey.", f.sid]])) throw new Error("message: " + JSON.stringify(f.sent));
          if (JSON.stringify(f.links) !== JSON.stringify(["https://modelcontextprotocol.io/extensions/apps/overview"])) throw new Error("links: " + JSON.stringify(f.links));
        });

        await check(`${tag}: a call the server refuses comes back to the app as an error, and an unknown method is refused`, async () => {
          const r = await app(`return await new Promise(res => { const id = 9001; const on = e => { if (e.data && e.data.id === id) { removeEventListener("message", on); res(JSON.stringify(e.data)); } }; addEventListener("message", on); parent.postMessage({ jsonrpc: "2.0", id, method: "tools/call", params: { name: "nothing", arguments: {} } }, "*"); setTimeout(() => res("timeout"), 5000); })`);
          if (!/no such tool/.test(r)) throw new Error("tools/call error: " + r);
          const r2 = await app(`return await new Promise(res => { const id = 9002; const on = e => { if (e.data && e.data.id === id) { removeEventListener("message", on); res(JSON.stringify(e.data)); } }; addEventListener("message", on); parent.postMessage({ jsonrpc: "2.0", id, method: "sampling/createMessage", params: {} }, "*"); setTimeout(() => res("timeout"), 5000); })`);
          if (!/-32601/.test(r2)) throw new Error("unknown method: " + r2);
        });

        await check(`${tag}: the policy holds: the app reaches nothing it did not declare, and the display modes map to the panel and the dialog without restarting the app`, async () => {
          const blocked = await app(`try { await fetch(${JSON.stringify(URL + "api/mcp-apps/config")}); return "reached"; } catch (e) { return "blocked"; }`);
          if (blocked !== "blocked") throw new Error("the app reached the host's own server");
          await ev(`const f = q(".mcpwin iframe"); f.dataset.mark = "same"; window.__frame = f; return 1;`);
          await app(`document.querySelector("input[name=choice][value=No]") && (document.querySelector("input[name=choice][value=No]").checked = true); return 1;`);
          if (!mobile) {                                                                       // the switch icon: panel -> dialog -> panel
            await ev(`q(".mcpwin-switch").click(); return 1;`);
            await waitFor(`q(".mcpwin").dataset.display === "dialog"`, "the dialog");
            await waitApp(`document.getElementById("info").textContent.includes("mode pip")`, "the app told it is pip");
            const d = JSON.parse(await ev(`const r = q(".mcpwin").getBoundingClientRect(), s = getComputedStyle(q(".mcpwin")); return JSON.stringify({ pos: s.position, w: r.width, h: r.height, resize: s.resize, same: q(".mcpwin iframe") === window.__frame })`));
            if (d.pos !== "fixed" || d.resize !== "both" || !d.same) throw new Error("dialog: " + JSON.stringify(d));
          }
          await app(`document.getElementById("mode").click(); return 1;`);                       // the app asks for fullscreen: the dialog, maximised
          await waitFor(`q(".mcpwin").dataset.display === "dialog" && (innerWidth <= 720 || q(".mcpwin").dataset.max === "1")`, "the dialog maximised");
          await waitApp(`document.getElementById("info").textContent.includes("mode fullscreen")`, "the app told it is fullscreen");
          const g = JSON.parse(await ev(`const r = q(".mcpwin").getBoundingClientRect(); return JSON.stringify({ w: r.width, h: r.height, vw: innerWidth, vh: innerHeight, same: q(".mcpwin iframe") === window.__frame })`));
          if (!g.same) throw new Error("the app was restarted by the change of display");
          if (g.w < g.vw - 40 || g.h < g.vh - 40) throw new Error("not maximised: " + JSON.stringify(g));
          await app(`document.getElementById("mode").click(); return 1;`);                       // back to inline: the panel
          await waitFor(`q(".mcpwin").dataset.display === "panel"`, "the panel again");
          await waitApp(`document.getElementById("info").textContent.includes("mode inline") && document.querySelector("input[name=choice]:checked").value === "No"`, "the app still holds what the person chose");
          if (await ev(`return q(".mcpwin iframe") !== window.__frame`)) throw new Error("the app was restarted");
          await fits("the display modes");
        });

        await check(`${tag}: a live tool call shows the app at once, the result arrives later, a cancelled call is told to the app, and the app's own tool calls are marked under it`, async () => {
          await ev(`window.mcpApps.reset(); window.__asked.length = 0; return 1;`);
          const rec = { ...rec0(world), app: "live1", call_id: "L1" };
          await ev(`const c = window.agentChat;
            c.apply({ event: "agent", turn: "t9", data: { type: "tool_start", call_id: "L1", name: "call_capability", arguments: {}, effect: "irreversible" } });
            c.apply(${JSON.stringify(rec)}); return 1;`);
          await waitFor(`!!q(".mcpwin") && !q(".mcpwin").hidden`, "the app shown by itself", 50);
          await waitApp(`document.getElementById("title").textContent === "Live" && document.getElementById("status").textContent !== "Result received"`, "the input, before any result");
          await ev(`window.agentChat.apply({ event: "mcp_app_call", app: "live1", tool: "save_answers", state: "started" }); window.agentChat.apply({ event: "mcp_app_call", app: "live1", tool: "save_answers", state: "done" }); return 1;`);
          await waitFor(`/save_answers/.test(q(".mcpwin-calls").textContent) && !q(".mcpwin-calls").hidden`, "the app's own call marked");
          await ev(`window.agentChat.apply({ event: "mcp_app_result", app: "live1", call_id: "L1", result: { content: [{ type: "text", text: "ok" }], structuredContent: { series: [3, 1, 4, 1, 5, 9] } } }); return 1;`);
          await waitApp(`document.getElementById("status").textContent === "Result received" && document.querySelector("#chart polyline").getAttribute("points").split(" ").length === 6`, "the result delivered later");
          await ev(`window.mcpApps.reset(); return 1;`);
          const rec2 = { ...rec, app: "live2", call_id: "L2" };
          await ev(`window.agentChat.apply({ event: "agent", turn: "t9", data: { type: "tool_start", call_id: "L2", name: "call_capability", arguments: {}, effect: "irreversible" } }); window.agentChat.apply(${JSON.stringify(rec2)}); return 1;`);
          await waitApp(`document.getElementById("title").textContent === "Live"`, "the second app started");
          await ev(`window.agentChat.apply({ event: "mcp_app_cancelled", app: "live2", call_id: "L2", reason: "stopped by the person" }); return 1;`);
          await waitApp(`document.getElementById("status").textContent.includes("stopped by the person")`, "the cancellation delivered");
        });

        await check(`${tag}: closing an app that is off stops it (the app is told first) and opening it again starts it; an app that is on keeps running, hidden`, async () => {
          await ev(`window.mcpApps.reset(); window.agentChat.apply({ event: "agent", turn: "t9", data: { type: "tool_start", call_id: "L3", name: "call_capability", arguments: {}, effect: "irreversible" } });
            window.agentChat.apply(${JSON.stringify({ ...rec0(world), app: "live3", call_id: "L3" })}); return 1;`);
          await waitApp(`document.getElementById("title").textContent === "Live"`, "started");
          await ev(`window.__told = []; const v = window.mcpApps.view("live3"); const orig = v.hostRequest.bind(v); v.hostRequest = (m, p) => { window.__told.push(m + ":" + (p && p.reason)); return orig(m, p); }; q(".mcpwin-close").click(); return 1;`);
          await waitFor(`window.mcpApps.view("live3").state === "idle" && !q(".mcpwin iframe")`, "stopped", 50);
          if (await ev(`return JSON.stringify(window.__told)`) !== JSON.stringify(["ui/resource-teardown:closed"])) throw new Error("teardown: " + await ev(`return JSON.stringify(window.__told)`));
          await ev(`window.mcpApps.view("live3").show("panel"); return 1;`);
          await waitFor(`window.mcpApps.view("live3").state === "ready"`, "started again", 100);
          // an app that is on
          await ev(`(await window.mcpApps.config()).settings.on = [${JSON.stringify(world.key)}]; window.__keep = q(".mcpwin iframe"); q(".mcpwin-close").click(); return 1;`);
          await waitFor(`q(".mcpwin").hidden`, "hidden");
          const f = JSON.parse(await ev(`const v = window.mcpApps.view("live3"); return JSON.stringify({ state: v.state, same: q(".mcpwin iframe") === window.__keep, ph: window.mcpApps.running() })`));
          if (f.state !== "ready" || !f.same) throw new Error("an app that is on was stopped: " + JSON.stringify(f));
          await ev(`window.mcpApps.view("live3").show("dialog"); return 1;`);
          await waitFor(`!q(".mcpwin").hidden && q(".mcpwin").dataset.display === "dialog"`, "shown again");
          if (await ev(`return q(".mcpwin iframe") !== window.__keep`)) throw new Error("it restarted");
          await ev(`window.mcpApps.view("live3").close(); (await window.mcpApps.config()).settings.on = []; window.mcpApps.view("live3").stop("test"); return 1;`);
        });

        await check(`${tag}: the window is touch-friendly and readable: every control is 44 px, it has a name for assistive technology, and nothing scrolls the page sideways`, async () => {
          await ev(`window.mcpApps.reset(); window.agentChat.apply({ event: "agent", turn: "t9", data: { type: "tool_start", call_id: "L4", name: "call_capability", arguments: {}, effect: "irreversible" } });
            window.agentChat.apply(${JSON.stringify({ ...rec0(world), app: "live4", call_id: "L4" })}); return 1;`);
          await waitFor(`!!q(".mcpwin") && !q(".mcpwin").hidden && window.mcpApps.view("live4").state === "ready"`, "ready", 100);
          const f = JSON.parse(await ev(`const win = q(".mcpwin"), small = qa(".mcpwin-b").filter(b => b.offsetParent !== null && (b.getBoundingClientRect().height < 43.5 || b.getBoundingClientRect().width < 43.5)).length,
            labelled = qa(".mcpwin-b").filter(b => b.offsetParent !== null).every(b => b.getAttribute("aria-label")); return JSON.stringify({ small, labelled, name: win.getAttribute("aria-label"), role: win.getAttribute("role") })`));
          if (f.small || !f.labelled || f.name !== "Survey" || !f.role) throw new Error("accessibility: " + JSON.stringify(f));
          await fits("the window");
          await ev(`window.mcpApps.reset(); return 1;`);
        });
        if (pageErrors.length) out.push({ name: `${tag}: no script error`, ok: false, error: pageErrors.splice(0).join(" ;; ") });
      }
    }
  } finally {
    try { proc.kill(); } catch (e) { /* gone */ }
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* in use */ }
  }
  console.log(JSON.stringify(out));
}
function rec0(w) {
  return { event: "mcp_app", app: "x", call_id: "x", server: w.server, server_name: "app-demo", key: w.key, uri: "ui://demo/survey", name: "Survey", tool: { name: "survey" },
           input: { title: "Live", options: ["A", "B"] }, state: "running", template: w.template, ui: { csp: {}, prefersBorder: true }, on: false };
}
main().catch(e => { console.log(JSON.stringify([{ name: "script", ok: false, error: String(e && e.stack || e) }])); });
