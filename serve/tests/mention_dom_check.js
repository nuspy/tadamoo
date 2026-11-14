"use strict";
// The "/" menu and the "@" list of the message box in a real headless browser at three screen sizes, in the Chat and in Agentic coding (they share the box):
// node mention_dom_check.js <url> <browser exe> <project folder>. Real key events (Up, Down, Right, Tab, Enter, Escape) and text typed into the box.
// Prints [{name, ok, error}] as JSON (read by test_mention_page.py).
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, EXE, FOLDER] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const get = async p => (await fetch(URL + p)).json();
const post = async (p, body) => (await fetch(URL + p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];
const VK = { ArrowDown: 40, ArrowUp: 38, ArrowRight: 39, Tab: 9, Enter: 13, Escape: 27 };

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "mnc-"));
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
    const waitFor = async (expr, what, tries = 100) => { for (let i = 0; i < tries; i++) { if (await ev(`return (${expr});`)) return true; await sleep(200); } throw new Error(`never: ${what}`); };
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message + (pageErrors.length ? " | page errors: " + pageErrors.splice(0).join(" ;; ") : "") }); } };
    const key = async k => { const p = { key: k, code: k, windowsVirtualKeyCode: VK[k], nativeVirtualKeyCode: VK[k] }; await send("Input.dispatchKeyEvent", { type: k === "Enter" ? "keyDown" : "rawKeyDown", ...p, ...(k === "Enter" ? { text: "\r" } : {}) }); await send("Input.dispatchKeyEvent", { type: "keyUp", ...p }); await sleep(60); };
    const typeText = async text => { await ev(`const p = q("#prompt"); p.focus(); return 1;`); await send("Input.insertText", { text }); await sleep(120); };
    const reset = () => ev(`const p = q("#prompt"); p.value = ""; p.dispatchEvent(new Event("input", { bubbles: true })); p.focus(); window.mentionUi.clear(); return 1;`);
    const shown = (menu) => `(() => { const m = q(${JSON.stringify(menu)}); return !!m && !m.hidden; })()`;
    const fits = what => ev(`const m = [q("#slash-menu"), q("#mention-menu")].find(x => x && !x.hidden); const items = m ? [...m.querySelectorAll("[role=option]")] : [];
      const r = m ? m.getBoundingClientRect() : null;
      return JSON.stringify({ wide: document.documentElement.scrollWidth > innerWidth + 1, small: items.filter(e => e.getBoundingClientRect().height < 43.5).length, outside: !!r && (r.left < -1 || r.right > innerWidth + 1), n: items.length });`).then(v => {
      const f = JSON.parse(v);
      if (f.wide) throw new Error(what + ": the page scrolls sideways");
      if (f.small) throw new Error(what + ": " + f.small + " list items under 44 px");
      if (f.outside) throw new Error(what + ": the list leaves the screen");
    });
    const active = () => ev(`const p = q("#prompt"); const o = document.getElementById(p.getAttribute("aria-activedescendant") || ""); return o ? o.textContent : ""`);

    // the world: fourteen conversations in the Chat, used in order
    const made = [];
    for (let i = 0; i < 14; i++) made.push((await post("api/agent/sessions", { title: `Conversation number ${String(i).padStart(2, "0")}`, mode: "default" })).session.id);
    await post("api/agent/projects", { path: FOLDER });

    for (const [vname, w, h, mobile] of VIEWPORTS) {
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      for (const [where, tab] of [["Chat", "#tab-chat"], ["Agentic coding", "#tab-agent"]]) {
        await send("Page.navigate", { url: URL + "?lang=en" });
        await sleep(1500);
        await ev(`try { localStorage.setItem("tadamoo.agent.project", ${JSON.stringify(FOLDER)}); localStorage.setItem("tadamoo.chatmode", "agent"); } catch (e) {} return 1;`);
        await send("Page.navigate", { url: URL + "?lang=en" });
        await waitFor(`!!q(${JSON.stringify(tab)}) && typeof view === "function" && !!window.slash && !!window.mentionUi`, "the page");
        await ev(`q(${JSON.stringify(tab)}).click(); return 1;`);
        await waitFor(`q("#prompt") && q("#prompt").offsetParent !== null`, "the message box");
        const label = await ev(`return q(${JSON.stringify(tab)}).textContent.trim()`);
        const tag = `${vname}, ${where}`;

        await check(`${tag}: the tab is called ${where}`, async () => { if (label !== where) throw new Error("the tab says " + label); });

        await check(`${tag}: "/" opens the list of commands and letters after it filter it`, async () => {
          await reset(); await typeText("/");
          await waitFor(shown("#slash-menu"), "the commands list");
          const all = await ev(`return qa("#slash-menu [role=option] b").map(b => b.textContent)`);
          for (const c of ["/install-skill", "/tasks", "/status", "/help", "/reload-skills"]) if (!all.includes(c)) throw new Error("no " + c + " in " + all.slice(0, 8).join(" "));
          const roles = await ev(`const p = q("#prompt"); return JSON.stringify([p.getAttribute("role"), p.getAttribute("aria-expanded"), p.getAttribute("aria-controls"), !!p.getAttribute("aria-activedescendant"), q("#slash-menu").getAttribute("role")])`);
          if (roles !== JSON.stringify(["combobox", "true", "slash-menu", true, "listbox"])) throw new Error("roles: " + roles);
          await fits("commands list");
          await typeText("ins");
          const some = await ev(`return qa("#slash-menu [role=option] b").map(b => b.textContent)`);
          if (some[0] !== "/install-skill" || some.length >= all.length) throw new Error("not filtered: " + some.join(" "));
          await typeText("zzzz");
          await waitFor(`!${shown("#slash-menu")}`, "no match closes the list");
        });

        await check(`${tag}: Up and Down move through the commands, Tab, Enter and Right choose, Escape closes`, async () => {
          await reset(); await typeText("/");
          await waitFor(shown("#slash-menu"), "the list");
          const first = await active();
          await key("ArrowDown"); const second = await active();
          if (first === second) throw new Error("Down did not move");
          await key("ArrowUp"); if (await active() !== first) throw new Error("Up did not come back");
          await key("ArrowUp"); const wrapped = await active();
          if (wrapped === first) throw new Error("Up from the first does not go to the last");
          await key("ArrowDown");
          for (const k of ["Tab", "ArrowRight", "Enter"]) {
            await reset(); await typeText("/instal");
            await waitFor(shown("#slash-menu"), "the filtered list");
            await key(k);
            const v = await ev(`return q("#prompt").value`);
            if (v !== "/install-skill ") throw new Error(k + " left " + JSON.stringify(v));
            if (await ev(`return !q("#slash-menu").hidden`)) throw new Error("the list stayed open after " + k);
          }
          await reset(); await typeText("/");
          await key("Escape");
          if (await ev(`return !q("#slash-menu").hidden`)) throw new Error("Escape did not close");
          await reset();
        });

        await check(`${tag}: "@" opens the other conversations, ten at a time, newest use first, and the next ten come when the list is scrolled`, async () => {
          await reset(); await typeText("@");
          await waitFor(`${shown("#mention-menu")} && qa("#mention-menu [role=option]").length === 10`, "ten conversations");
          const names = await ev(`return qa("#mention-menu .mn-name").map(b => b.textContent)`);
          if (names[0] !== "@Conversation number 13" || names[9] !== "@Conversation number 04") throw new Error("order: " + names.join("|"));
          const roles = await ev(`const p = q("#prompt"); return JSON.stringify([p.getAttribute("aria-controls"), p.getAttribute("aria-expanded"), q("#mention-menu").getAttribute("role"), !!p.getAttribute("aria-activedescendant")])`);
          if (roles !== JSON.stringify(["mention-menu", "true", "listbox", true])) throw new Error("roles: " + roles);
          await fits("the @ list");
          await ev(`const m = q("#mention-menu"); m.scrollTop = m.scrollHeight; return 1;`);
          await waitFor(`qa("#mention-menu [role=option]").length === 14`, "the next page by scrolling");
          await reset(); await typeText("@");
          await waitFor(`qa("#mention-menu [role=option]").length === 10`, "ten again");
          for (let i = 0; i < 10; i++) await key("ArrowDown");                          // past the tenth: the following ones are fetched
          await waitFor(`qa("#mention-menu [role=option]").length === 14`, "the next page by the arrow");
          if (!/number 03/.test(await active())) throw new Error("the eleventh is not under the cursor: " + await active());
        });

        await check(`${tag}: letters after "@" filter by name and the choice becomes a mention with the conversation's id`, async () => {
          await reset(); await typeText("@");
          await waitFor(shown("#mention-menu"), "the list");
          await typeText("07");
          await waitFor(`qa("#mention-menu [role=option]").length === 1 && /number 07/.test(qa("#mention-menu .mn-name")[0].textContent)`, "filtered to one");
          await key("Tab");
          const v = await ev(`return q("#prompt").value`);
          if (v !== "@Conversation number 07 ") throw new Error("text: " + JSON.stringify(v));
          const m = await ev(`return JSON.stringify([q("#mention-chips").hidden, qa("#mention-chips .mn-chip-t").map(x => x.textContent), window.mentionUi.collect(q("#prompt").value)])`).then(JSON.parse);
          if (m[0] || m[1][0] !== "@Conversation number 07" || m[2][0] !== made[7]) throw new Error("chip/ids: " + JSON.stringify(m));
          if (await ev(`return !q("#mention-menu").hidden`)) throw new Error("the list stayed open");
          // a second one in the middle of the text, chosen with Enter and then with Right
          await typeText("ask ");
          await typeText("@04");
          await waitFor(`qa("#mention-menu [role=option]").length === 1`, "filtered to 04");
          await key("Enter");
          await typeText("and ");
          await typeText("@11");
          await waitFor(`qa("#mention-menu [role=option]").length === 1`, "filtered to 11");
          await key("ArrowRight");
          const ids = await ev(`return JSON.stringify(window.mentionUi.collect(q("#prompt").value))`).then(JSON.parse);
          if (JSON.stringify(ids) !== JSON.stringify([made[7], made[4], made[11]])) throw new Error("ids: " + JSON.stringify(ids));
          await fits("with chips");
          const small = await ev(`return qa("#mention-chips button, #mention-chips .mn-chip").filter(e => e.getBoundingClientRect().height < 43.5).length`);
          if (small) throw new Error("chips under 44 px");
          // the text of a mention taken out of the message takes it off the list that is sent; the chip's cross takes out both
          await ev(`const p = q("#prompt"); p.value = p.value.replace("@Conversation number 04", ""); return 1;`);
          const left = await ev(`return JSON.stringify(window.mentionUi.collect(q("#prompt").value))`).then(JSON.parse);
          if (JSON.stringify(left) !== JSON.stringify([made[7], made[11]])) throw new Error("not dropped: " + JSON.stringify(left));
          await ev(`q("#mention-chips .mn-x").click(); return 1;`);
          if ((await ev(`return q("#prompt").value`)).includes("number 07")) throw new Error("the cross left the text");
          await reset();
        });

        await check(`${tag}: a word with an "@" inside is no mention, nothing matching leaves Enter to send, Escape closes`, async () => {
          await reset(); await typeText("mail me@host");
          if (await ev(`return !q("#mention-menu").hidden`)) throw new Error("an address opened the list");
          await reset(); await typeText("@zzzzqq");
          await waitFor(`${shown("#mention-menu")} && /No other conversation/.test(q("#mention-menu").textContent)`, "the empty answer");
          await key("Escape");
          if (await ev(`return !q("#mention-menu").hidden`)) throw new Error("Escape did not close");
          await reset();
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
