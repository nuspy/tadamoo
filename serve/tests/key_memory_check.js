"use strict";
// The API key in a real browser: node key_memory_check.js <url> <browser exe> <key>. Prints [{name, ok, error}] as JSON (read by test_key_memory_page.py).
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE, KEY] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  const port = 9400 + Math.floor(Math.random() * 400), dir = fs.mkdtempSync(path.join(os.tmpdir(), "kmc-"));
  const proc = spawn(EXE, ["--headless=new", "--disable-gpu", `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, "--window-size=1280,800", "about:blank"], { stdio: "ignore" });
  const out = [];
  try {
    let targets = [];
    for (let i = 0; i < 60 && !targets.length; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch (e) { await sleep(250); } }
    const ws = new WebSocket(targets.find(t => t.type === "page").webSocketDebuggerUrl);
    await new Promise(r => { ws.onopen = r; });
    let id = 0; const waits = new Map();
    ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.id && waits.has(m.id)) { waits.get(m.id)(m); waits.delete(m.id); } };
    const send = (method, params = {}) => new Promise(res => { const i = ++id; waits.set(i, res); ws.send(JSON.stringify({ id: i, method, params })); });
    const ev = async expr => { const r = (await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true })).result; if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 300)); return r.result.value; };
    const waitFor = async (expr, what) => { for (let i = 0; i < 60; i++) { if (await ev(expr)) return; await sleep(250); } throw new Error("timeout: " + what); };
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true }); } catch (e) { out.push({ name, ok: false, error: String(e.message || e) }); } };
    await send("Page.enable");
    const open = async () => { await send("Page.navigate", { url: URL }); await sleep(1500); await waitFor(`document.readyState === "complete" && !!document.querySelector("#auth")`, "the page"); };

    await open();
    await check("with no key kept the page asks for it", async () => { await waitFor(`!document.querySelector("#auth").hidden`, "the key prompt"); });
    await check("the key typed is kept in this browser (localStorage)", async () => {
      await ev(`(() => { document.querySelector("#key").value = ${JSON.stringify(KEY)}; document.querySelector("#auth").dispatchEvent(new Event("submit", { cancelable: true, bubbles: true })); return 1; })()`);
      await waitFor(`document.querySelector("#auth").hidden`, "connected");
      if ((await ev(`localStorage.getItem("tadamoo.key")`)) !== KEY) throw new Error("the key is not in localStorage");
    });
    await check("a new visit does not ask again", async () => { await open(); await sleep(1500); await waitFor(`document.querySelector("#auth").hidden && !!document.querySelector("#forgetkey") && !document.querySelector("#forgetkey").hidden`, "connected without asking, with the Forget key button"); });
    await check("Forget key takes it out and asks again, also on the next visit", async () => {
      await ev(`document.querySelector("#forgetkey").click(); 1`);
      await waitFor(`!document.querySelector("#auth").hidden`, "the prompt after forgetting");
      if ((await ev(`localStorage.getItem("tadamoo.key") || sessionStorage.getItem("tadamoo.key") || ""`)) !== "") throw new Error("the key is still kept");
      await open(); await waitFor(`!document.querySelector("#auth").hidden`, "the prompt on the next visit");
    });
    ws.close();
  } catch (e) { out.push({ name: "setup", ok: false, error: String(e.message || e) }); }
  finally { proc.kill(); }
  console.log(JSON.stringify(out));
})();
