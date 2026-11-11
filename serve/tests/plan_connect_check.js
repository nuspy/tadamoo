"use strict";
// Drags links on the plans page of a running server in a real headless browser, with real mouse and touch input (Input.dispatch*), and asks the server whether
// the edge exists: node plan_connect_check.js <url> <plan id> <plan title> <browser exe>. Prints [{name, ok, error}] as JSON (read by test_plans_page.py).
// Each case drags from a connector (OUTPUT dot) to the INPUT dot of another step; the sources are a plain step, a branch of a decision and a RAG node.
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, PLAN, TITLE, EXE] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const get = async p => (await fetch(URL + p)).json();

const log = (...a) => { if (process.env.PLC_LOG) console.error(...a); };
async function main() {
  log('start');
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "plc-"));
  const proc = spawn(EXE, ["--headless=new", "--lang=en-US", "--disable-gpu", `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, "--window-size=1440,900", "about:blank"], { stdio: "ignore" });
  const out = [];
  try {
    let targets = [];
    for (let i = 0; i < 40 && !targets.length; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch (e) { await sleep(250); } }
    const ws = new WebSocket(targets.find(t => t.type === "page").webSocketDebuggerUrl);
    await new Promise(r => { ws.onopen = r; });
    let id = 0; const waits = new Map();
    ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.method === 'Runtime.exceptionThrown') log('page error', JSON.stringify(m.params.exceptionDetails.exception && m.params.exceptionDetails.exception.description || m.params.exceptionDetails.text).slice(0, 400)); if (m.id && waits.has(m.id)) { waits.get(m.id)(m.result || m.error); waits.delete(m.id); } };
    const send = (method, params = {}) => new Promise((r, rej) => { const n = ++id; const t = setTimeout(() => rej(new Error(`${method} did not answer`)), 60000); waits.set(n, v => { clearTimeout(t); r(v); }); ws.send(JSON.stringify({ id: n, method, params })); });
    const ev = async expr => { log('ev', expr.trim().slice(0, 70).replace(/\s+/g, ' ')); const r = await send("Runtime.evaluate", { expression: `(async () => { const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)]; const wait = ms => new Promise(r => setTimeout(r, ms)); ${expr} })()`, returnByValue: true, awaitPromise: true }); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception && r.exceptionDetails.exception.description)); return r.result.value; };
    log('connected');
    await send("Page.enable"); await send("Runtime.enable"); await send("Emulation.setFocusEmulationEnabled", { enabled: true }); await send("Page.bringToFront");
    await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
    await send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 });
    await send("Page.navigate", { url: URL });
    await sleep(2500);
    log('navigated');
    await ev(`q("#tab-plans").click(); await wait(1500);
      const hit = qa("#plans-root *").filter(e => e.children.length === 0 && e.textContent.includes(${JSON.stringify(TITLE)})).sort((a, b) => a.textContent.length - b.textContent.length)[0];
      (hit.closest("button, a, li, [role=button], [tabindex]") || hit).click(); await wait(1500);
      for (let i = 0; i < 20; i++) { const gb = qa("#plans-root button").find(b => /^(Graph|Grafo)$/.test(b.textContent.trim())); if (gb) gb.click(); await wait(500); if (q("#plans-root .pl-node .pl-port")) break; }
      return JSON.stringify({ nodes: qa('#plans-root .pl-node').length, ports: qa('#plans-root .pl-port').length, buttons: qa('#plans-root button').map(b => b.textContent.trim()).slice(0, 12), svg: !!q('#plans-root .pl-svg') });`).then(v => log('opened', v));
    /* the centre of a connector in the page's pixels: `sel` is the node's selector, `cls` the connector's class; branch rows have their own */
    await ev(`window.__p = []; for (const t of ['pointerdown', 'pointerup', 'pointercancel']) window.addEventListener(t, e => window.__p.push(t + ':' + e.pointerType + ':' + (e.target.getAttribute && e.target.getAttribute('class'))), true); return 1;`);
    log('graph open');
    const centre = (node, cls) => ev(`const g = q('#plans-root .pl-node[data-id="${node}"]'); if (!g) return null; const d = ${JSON.stringify(cls)}; const dot = [...g.querySelectorAll(".pl-port")].find(e => d.split(" ").every(c => e.classList.contains(c))); if (!dot) return null; const r = dot.getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2, top: document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2) && document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2).getAttribute("class") };`);
    const mouse = async (a, b) => {
      await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: a.x, y: a.y });
      await send("Input.dispatchMouseEvent", { type: "mousePressed", x: a.x, y: a.y, button: "left", buttons: 1, clickCount: 1 });
      for (let i = 1; i <= 8; i++) { await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: a.x + (b.x - a.x) * i / 8, y: a.y + (b.y - a.y) * i / 8, button: "left", buttons: 1 }); await sleep(15); }
      await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: b.x, y: b.y, button: "left", buttons: 0, clickCount: 1 });
    };
    /* a finger: pointer events of type touch built by the page itself (headless Chrome does not deliver real touch input), sent to whatever is on top at each point,
       as the browser would; so the connector must really be on top where the finger lands, and the target found where it lifts */
    const touch = async (a, b) => {
      await ev(`const A = ${JSON.stringify(a)}, B = ${JSON.stringify(b)};
        const at = p => document.elementFromPoint(p.x, p.y);
        const fire = (type, target, p) => target.dispatchEvent(new PointerEvent(type, { pointerType: "touch", pointerId: 7, isPrimary: true, bubbles: true, cancelable: true, composed: true, clientX: p.x, clientY: p.y, button: 0, buttons: type === "pointerup" ? 0 : 1 }));
        fire("pointerdown", at(A), A);
        for (let i = 1; i <= 8; i++) { const p = { x: A.x + (B.x - A.x) * i / 8, y: A.y + (B.y - A.y) * i / 8 }; fire("pointermove", at(p) || document.body, p); await wait(15); }
        fire("pointerup", at(B) || document.body, B);
        return 1;`);
    };
    const nodeOf = async n => (await get(`api/plans/${PLAN}`)).plan.nodes[n];
    const has = async (from, to, port) => {
      const n = await nodeOf(from);
      return port ? ((n.routes || {})[port] || []).includes(to) : (n.outputs || []).includes(to);
    };
    const cases = [
      ["a1", "out", "a2", "in", "", "a plain step to the input of another step"],
      ["choose", "out", "t1", "in", "b1", "a branch of a decision to the input of a step"],
      ["kb", "out data", "t2", "in data", "rag", "a RAG node to the data input of an action"],
    ];
    for (const kind of ["mouse", "touch"]) {
      for (const [from, fromCls, to, toCls, port, name] of cases) {
        const label = `${kind}: ${name}`;
        log('case', label);
        try {
          const fromAt = from === "choose" ? await ev(`const g = q('#plans-root .pl-node[data-id="choose"]'); const rows = [...g.querySelectorAll(".pl-pg.branch .pl-port")]; const r = rows[0].getBoundingClientRect(); return { x: r.x + r.width / 2, y: r.y + r.height / 2 };`) : await centre(from, fromCls);
          const toAt = await centre(to, toCls);
          if (!fromAt || !toAt) throw new Error(`no connector: ${JSON.stringify({ fromAt, toAt })}`);
          await ev('window.__p.length = 0; return 1');
          await (kind === "mouse" ? mouse : touch)(fromAt, toAt);
          let ok = false;
          for (let i = 0; i < 20 && !ok; i++) { await sleep(400); ok = await has(from, to, port); }       // the page proposes the change and approves it: two requests
          if (!ok) throw new Error(`the edge ${from} -> ${to}${port ? " (" + port + ")" : ""} is not in the plan; the page says: ${await ev(`return (q('.pl-status') || {}).textContent || ''`)}; the pointer events: ${await ev('return JSON.stringify(window.__p.splice(0))')}`);
          out.push({ name: label, ok: true, error: "" });
        } catch (e) { out.push({ name: label, ok: false, error: e.message }); }
        // the next case starts again from the same page, the edge it made is taken away through the server
        const post = body => fetch(URL + `api/plans/${PLAN}/events`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then(r => r.json()).catch(() => ({}));
        const proposed = await post({ type: "plan:propose_diff", title: "undo", source: "user", diff: { remove_edges: [from === "a1" ? ["a1", "a2"] : from === "choose" ? ["choose", "t1", "b1"] : ["kb", "t2", "rag"]] } });
        if (proposed.proposal_id) await post({ type: "plan:approve", proposal_id: proposed.proposal_id });
        await sleep(800);
        await ev(`const gb = qa("#plans-root button").find(b => /^(Graph|Grafo)$/.test(b.textContent.trim())); if (gb) gb.click(); await wait(500); return 1;`);
      }
    }
    /* the Connections section of a step: choose where a branch (or the RAG node) leads and press Connect; no dragging */
    for (const [from, to, port, name, row] of [["choose", "t2", "b1", "the Connections section of a decision", 0], ["kb", "t1", "rag", "the Connections section of a RAG node", 0]]) {
      const label = `tap: ${name}`;
      try {
        await ev(`q('#plans-root .pl-node[data-id="${from}"]').dispatchEvent(new MouseEvent("click", { bubbles: true })); await wait(600);
          const sel = q("#pl-c-${row}"); if (!sel) throw new Error("no Connections section");
          sel.value = "${to}"; sel.dispatchEvent(new Event("change", { bubbles: true }));
          const go = sel.parentElement.querySelector("button"); go.click(); return 1;`);
        let ok = false;
        for (let i = 0; i < 20 && !ok; i++) { await sleep(400); ok = await has(from, to, port); }
        if (!ok) throw new Error(`the edge ${from} -> ${to} (${port}) is not in the plan`);
        out.push({ name: label, ok: true, error: "" });
      } catch (e) { out.push({ name: label, ok: false, error: e.message }); }
    }
    ws.close();
  } finally { proc.kill(); await sleep(400); try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* temp */ } }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "the run", ok: false, error: String(e && e.message || e) }])); });
