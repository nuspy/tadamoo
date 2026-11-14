"use strict";
// The bubbles of messages between conversations in a real headless browser at three screen sizes, in English and in Italian:
// node peer_dom_check.js <url> <browser exe> <project folder> <session ids as JSON: {a, b}>. The conversation "a" has a saved history with what it sent
// (a session_send call), what came from another conversation (a message with its `peer`) and a failed send; live events (peer_in, peer_out, peer_brake, peer_expired)
// are then applied as the page gets them. Prints [{name, ok, error}] as JSON (read by test_peer_page.py).
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE, FOLDER, IDS] = process.argv.slice(2);
const { a } = JSON.parse(IDS);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];
const WORDS = { en: { from: "From Backend", to: "To Backend", stop: "Not sent", question: "Question", info: "Information", task: "Task" },
                it: { from: "Da Backend", to: "A Backend", stop: "Non inviato", question: "Domanda", info: "Informazione", task: "Compito" } };

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "pdc-"));
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
      const r = await send("Runtime.evaluate", { expression: `(async () => { const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)]; ${expr} })()`, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception && r.exceptionDetails.exception.description));
      return r.result.value;
    };
    await send("Page.enable"); await send("Runtime.enable");
    const waitFor = async (expr, what, tries = 100) => { for (let i = 0; i < tries; i++) { if (await ev(`return (${expr});`)) return true; await sleep(200); } throw new Error(`never: ${what}`); };
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message + (pageErrors.length ? " | page errors: " + pageErrors.splice(0).join(" ;; ") : "") }); } process.stderr.write(`${out[out.length - 1].ok ? "ok  " : "FAIL"} ${name} ${out[out.length - 1].error.slice(0, 1800)}
`); };
    const bubbles = () => ev(`return JSON.stringify(qa(".cmsg.peer").map(b => ({ cls: b.className, label: b.querySelector(".peer-who").textContent, kind: (b.querySelector(".peer-kind") || {}).textContent || "", folder: (b.querySelector(".peer-folder") || {}).textContent || "",
      when: !!b.querySelector(".peer-when"), body: b.querySelector(".peer-body").textContent, role: b.getAttribute("role"), aria: b.getAttribute("aria-label") })))`).then(JSON.parse);
    const fits = what => ev(`const root = document.documentElement, r = q(".cmsg.peer") ? qa(".cmsg.peer").map(b => b.getBoundingClientRect()) : [];
      return JSON.stringify({ wide: root.scrollWidth > innerWidth + 1, outside: r.filter(x => x.left < -1 || x.right > innerWidth + 1).length, count: r.length })`).then(v => {
      const f = JSON.parse(v);
      if (f.wide) throw new Error(what + ": the page scrolls sideways");
      if (f.outside) throw new Error(what + ": " + f.outside + " bubbles leave the screen");
      if (!f.count) throw new Error(what + ": no bubbles");
    });

    for (const lang of ["en", "it"]) {
      const W = WORDS[lang];
      for (const [vname, w, h, mobile] of VIEWPORTS) {
        await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
        await send("Page.navigate", { url: URL + `?lang=${lang}` });
        await sleep(1200);
        await ev(`try { localStorage.setItem("tadamoo.agent.project", ${JSON.stringify(FOLDER)}); localStorage.setItem("tadamoo.chatmode", "agent"); } catch (e) {} return 1;`);
        await send("Page.navigate", { url: URL + `?lang=${lang}` });
        await waitFor(`!!q("#tab-agent") && !!window.agentChat && typeof window.agentChat.apply === "function"`, "the page");
        await ev(`q("#tab-agent").click(); return 1;`);
        await waitFor(`q("#prompt") && q("#prompt").offsetParent !== null`, "the message box");
        await ev(`await window.agentChat.open(${JSON.stringify(a)}); return 1;`);
        await waitFor(`qa(".cmsg.peer").length >= 5`, "the saved bubbles");
        const tag = `${vname}, ${lang}`;

        await check(`${tag}: the saved history shows what came, what went and what did not go, each as a bubble of its own in order`, async () => {
          const b = await bubbles();
          if (b.length !== 5) throw new Error("bubbles: " + JSON.stringify(b));
          const [r1, s1, r2, s2, f1] = b;                                                // the message that did not go is shown as sent, and then as not sent
          if (!/peer-in/.test(r1.cls) || r1.label !== W.from || r1.kind !== W.question || r1.folder !== "app" || !r1.when || r1.body !== "Which port does the API use?") throw new Error("received: " + JSON.stringify(r1));
          if (!/peer-out/.test(s1.cls) || s1.label !== W.to || s1.kind !== W.task || s1.body !== "Write the migration for the users table.") throw new Error("sent: " + JSON.stringify(s1));
          if (!/peer-in/.test(r2.cls) || r2.kind !== W.info || r2.body !== "It is 8081.") throw new Error("second received: " + JSON.stringify(r2));
          if (!/peer-out/.test(s2.cls) || s2.label !== W.to.replace("Backend", "Nobody") || s2.body !== "hello") throw new Error("second sent: " + JSON.stringify(s2));
          if (!/peer-stop/.test(f1.cls) || f1.label !== W.stop || !/No session is called/.test(f1.body)) throw new Error("failed: " + JSON.stringify(f1));
          if (b.some(x => x.role !== "group" || !x.aria)) throw new Error("no role or label");
          const asTool = await ev(`return qa(".ablock .atool, .atool").filter(t => /session_send/.test(t.textContent)).length`);
          if (asTool) throw new Error("a session_send card was shown beside the bubble");
          const asUser = await ev(`return qa(".cmsg.user").filter(u => /this is not from the user|message from another session/.test(u.textContent)).length`);
          if (asUser) throw new Error("the framed text was shown as the person's message");
          await fits("saved bubbles");
        });

        await check(`${tag}: live events add the bubbles: received, sent, stopped by a brake, a question that expired`, async () => {
          const before = (await bubbles()).length;
          await ev(`const c = window.agentChat;
            c.apply({ event: "peer_in", from: { id: "ab12", name: "Backend", folder: "app" }, kind: "question", text: "Can you review the diff?", at: Date.now() / 1000 });
            c.apply({ event: "peer_out", to: { id: "ab12", name: "Backend", folder: "app" }, kind: "info", text: "Done, merged." });
            c.apply({ event: "peer_brake", to: { id: "ab12", name: "Backend" }, reason: "chain", kind: "info" });
            c.apply({ event: "peer_brake", to: { id: "ab12", name: "Backend" }, reason: "repeat", kind: "info" });
            c.apply({ event: "peer_brake", to: { id: "ab12", name: "Backend" }, reason: "rate", kind: "info" });
            c.apply({ event: "peer_expired", to: { id: "ab12", name: "Backend" }, text: "Is the build green?" });
            return 1;`);
          await waitFor(`qa(".cmsg.peer").length === ${before + 6}`, "six more bubbles");
          const b = (await bubbles()).slice(before);
          if (!/peer-in/.test(b[0].cls) || b[0].label !== W.from || b[0].body !== "Can you review the diff?") throw new Error("live in: " + JSON.stringify(b[0]));
          if (!/peer-out/.test(b[1].cls) || b[1].label !== W.to || b[1].kind !== W.info) throw new Error("live out: " + JSON.stringify(b[1]));
          const sayings = b.slice(2).map(x => x.body);
          if (lang === "en" && !(/limit is 6/.test(sayings[0]) && /same message/.test(sayings[1]) && /ten minutes/.test(sayings[2]) && /did not answer in 12 hours/.test(sayings[3]) && /Is the build green/.test(sayings[3]))) throw new Error("brakes: " + JSON.stringify(sayings));
          if (lang === "it" && !(/il limite è 6/.test(sayings[0]) && /stesso messaggio/.test(sayings[1]) && /dieci minuti/.test(sayings[2]) && /12 ore/.test(sayings[3]))) throw new Error("freni: " + JSON.stringify(sayings));
          if (b.slice(2).some(x => !/peer-stop/.test(x.cls))) throw new Error("a stop is not a stop bubble");
          await fits("live bubbles");
        });

        await check(`${tag}: the long text wraps, the head wraps, nothing under 12 px, and the bubble is readable`, async () => {
          await ev(`window.agentChat.apply({ event: "peer_in", from: { id: "ab12", name: "A very long conversation name that goes on and on and on and on", folder: "a/very/long/folder/path/that/never/ends/and/never/breaks" }, kind: "collaborate", text: "x".repeat(400) + " " + "word ".repeat(80), at: Date.now() / 1000 }); return 1;`);
          await waitFor(`/very long conversation/.test(qa(".cmsg.peer").pop().textContent)`, "the long bubble");
          await fits("long bubble");
          const f = await ev(`const b = qa(".cmsg.peer").pop(); return JSON.stringify({ body: parseFloat(getComputedStyle(b.querySelector(".peer-body")).fontSize), small: parseFloat(getComputedStyle(b.querySelector(".peer-when")).fontSize) })`).then(JSON.parse);
          if (f.body < 15 || f.small < 12) throw new Error("font sizes " + JSON.stringify(f));
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
main().catch(e => { console.log(JSON.stringify([{ name: "script", ok: false, error: String(e && e.stack || e) }])); });
