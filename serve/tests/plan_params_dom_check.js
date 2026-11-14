"use strict";
// The Parameters tab, Start plan and Stop plan in a real headless browser: node plan_params_dom_check.js <url> <plan id> <plan title> <browser exe> <screen name> <language>.
// Prints [{name, ok, error}] as JSON (read by test_plan_params_page.py).
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, PLAN, TITLE, EXE, SCREEN, LANG] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const SCREENS = { "desktop 1280x800": [1280, 800, false], "phone 360x640": [360, 640, true], "phone 390x844": [390, 844, true] };

async function main() {
  const [vw, vh, mobile] = SCREENS[SCREEN];
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "ppc-"));
  const proc = spawn(EXE, ["--headless=new", "--lang=en-US", "--disable-gpu", "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows", "--disable-features=CalculateNativeWinOcclusion",
    `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, "--window-size=1440,900", "about:blank"], { stdio: "ignore" });
  const out = [];
  try {
    let targets = [];
    for (let i = 0; i < 40 && !targets.length; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch (e) { await sleep(250); } }
    const ws = new WebSocket(targets.find(t => t.type === "page").webSocketDebuggerUrl);
    await new Promise(r => { ws.onopen = r; });
    let id = 0; const waits = new Map(), pageErrors = [];
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
    await send("Page.enable"); await send("Runtime.enable");
    const waitFor = async (expr, what, tries = 75) => { for (let i = 0; i < tries; i++) { try { if (await ev(`return (${expr});`)) return true; } catch (e) { /* not yet */ } await sleep(200); } throw new Error(`never: ${what}`); };
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message + (pageErrors.length ? " | page errors: " + pageErrors.splice(0).join(" ;; ") : "") }); } process.stderr.write(`${out[out.length - 1].ok ? "ok  " : "FAIL"} ${name} ${out[out.length - 1].error.slice(0, 600)}\n`); };
    const tag = `${SCREEN}, ${LANG}`;
    const apiGet = p => ev(`const r = await fetch(${JSON.stringify(p)}, { headers: authHeaders() }); return JSON.stringify(await r.json());`).then(JSON.parse);

    await send("Emulation.setDeviceMetricsOverride", { width: vw, height: vh, deviceScaleFactor: 1, mobile });
    await send("Emulation.setTouchEmulationEnabled", { enabled: mobile, maxTouchPoints: 5 });
    await send("Page.navigate", { url: URL + `?lang=${LANG}` });
    await sleep(1500);
    await ev(`q("#tab-plans").click(); await wait(1200);
      const find = () => qa("#plans-root *").filter(e => e.children.length === 0 && e.textContent.includes(${JSON.stringify(TITLE)}) && e.closest("li, button, a")).sort((a, b) => a.textContent.length - b.textContent.length)[0];
      let hit = find(); if (!hit) { const tg = q("#pl-toggle"); if (tg && q(".plcol.shut")) { tg.click(); await wait(400); hit = find(); } }
      if (hit) (hit.closest("button, a, li, [role=button], [tabindex]") || hit).click(); return 1;`);
    await waitFor(`!!q("#pl-tab-params")`, "the plan opened with its tabs");

    await check(`${tag}: the parameters are not steps: the diagram and the list do not show them, the summary does not count them`, async () => {
      await ev(`q("#pl-tab-define").click(); await wait(400); return 1;`);
      const r = JSON.parse(await ev(`const gb = qa("#plans-root button").find(b => /^(Graph|Grafo)$/.test(b.textContent.trim())); if (gb) gb.click(); await wait(500);
        return JSON.stringify({ svgNodes: qa(".pl-node").map(e => e.dataset.id), rows: qa(".pl-list li, .pl-li").length, summary: (q(".pl-summary") || {}).textContent || "" })`));
      if (r.svgNodes.some(i => i.startsWith("p_"))) throw new Error("a parameter is drawn in the diagram: " + r.svgNodes.join(","));
      if (!r.svgNodes.includes("a")) throw new Error("the step is not drawn: " + r.svgNodes.join(","));
      if (!/\b3\b/.test(r.summary)) throw new Error("the summary counts the parameters: " + r.summary);
    });

    await check(`${tag}: the Parameters tab lists them under their headings, each with the widget of its kind and its default`, async () => {
      await ev(`q("#pl-tab-params").click(); await wait(500); return 1;`);
      const r = JSON.parse(await ev(`return JSON.stringify({ groups: qa(".pl-par-group").map(e => e.textContent), rows: qa(".pl-par").length, text: !!q('.pl-par[data-name="brief"] textarea'), num: !!q('.pl-par[data-name="stories"] input[type=number]'),
          two: qa('.pl-par[data-name="ai_label"] .pl-par-two button').length, menu: !!q('.pl-par[data-name="edition"] select'), other: !!q('.pl-par[data-name="edition"] .pl-par-other'),
          def: (q('.pl-par[data-name="stories"] .pl-par-def') || {}).textContent, textH: Math.round(q('.pl-par[data-name="brief"] textarea').getBoundingClientRect().height),
          val: q('.pl-par[data-name="stories"] input').value, sw: document.documentElement.scrollWidth, iw: innerWidth })`));
      if (r.rows !== 4 || r.groups.length !== 2) throw new Error("rows/groups " + JSON.stringify(r));
      if (!r.text || !r.num || r.two !== 2 || !r.menu || !r.other) throw new Error("widgets " + JSON.stringify(r));
      if (r.val !== "3" || !/3/.test(r.def)) throw new Error("default/value " + JSON.stringify(r));
      if (r.textH > 60) throw new Error("an empty text field is higher than one line: " + r.textH);
      if (r.sw > r.iw + 1) throw new Error("the page scrolls sideways");
      const heads = r.groups.join("|");
      if (LANG === "it" ? !/Gruppo A/.test(heads) : !/Group A/.test(heads)) throw new Error("headings not in the language: " + heads);
    });

    await check(`${tag}: a changed parameter is saved with Save and only what changed is sent; the plan keeps the values`, async () => {
      await ev(`const n = q('.pl-par[data-name="stories"] input'); n.value = "7"; n.dispatchEvent(new Event("input", { bubbles: true }));
        const b = qa('.pl-par[data-name="ai_label"] .pl-par-two button')[0]; b.click();
        const s = q('.pl-par[data-name="edition"] select'); s.value = "\\u0000other"; s.dispatchEvent(new Event("change", { bubbles: true }));
        const o = q('.pl-par[data-name="edition"] .pl-par-other'); o.value = "weekend"; o.dispatchEvent(new Event("input", { bubbles: true }));
        const t = q('.pl-par[data-name="brief"] textarea'); t.value = "line one\\nline two"; t.dispatchEvent(new Event("input", { bubbles: true })); return 1;`);
      const c = await ev(`return q(".pl-par-count").textContent`);
      if (!/4/.test(c)) throw new Error("the count of changes: " + c);
      const size = JSON.parse(await ev(`const b = q(".pl-par-foot .btn"); const r = b.getBoundingClientRect(); return JSON.stringify({ w: r.width, h: r.height, off: b.disabled })`));
      if (size.off) throw new Error("Save is disabled with changes");
      if (mobile && size.h < 43.5) throw new Error("Save is too small for a finger: " + size.h);
      await ev(`q(".pl-par-foot .btn").click(); return 1;`);
      await waitFor(`/(saved|salvati)/i.test((q(".pl-status") || {}).textContent || "")`, "the saved message");
      const got = await apiGet(`api/plans/${PLAN}/parameters`);
      const by = Object.fromEntries(got.parameters.map(p => [p.name, p.value]));
      if (by.stories !== 7 || by.edition !== "weekend" || by.brief !== "line one\nline two" || by.ai_label !== "yes") throw new Error("values kept " + JSON.stringify(by));
    });

    await check(`${tag}: Start plan and Stop plan are in the plan's header, big enough, and the step has no Start button of its own`, async () => {
      const st = JSON.parse(await ev(`const a = q("#pl-start-plan"), z = q("#pl-stop-plan"), ra = a.getBoundingClientRect(), rz = z.getBoundingClientRect();
        return JSON.stringify({ a: a.textContent, z: z.textContent, ha: ra.height, hz: rz.height, wa: ra.width, off: (q(".pl-onoff") || {}).dataset.on })`));
      if (mobile && (st.ha < 43.5 || st.hz < 43.5)) throw new Error("the buttons are too small for a finger " + JSON.stringify(st));
      if (LANG === "it" ? st.a !== "Avvia piano" : st.a !== "Start plan") throw new Error("the label: " + st.a);
      await ev(`q("#pl-tab-define").click(); await wait(300); const n = q('.pl-node[data-id="s"]'); n.dispatchEvent(new MouseEvent("click", { bubbles: true })); await wait(500); return 1;`);
      const inner = JSON.parse(await ev(`return JSON.stringify(qa(".pl-detail button").map(b => b.textContent.trim()))`));
      if (inner.some(t => /^(Start plan|Avvia piano|Start the plan here)$/.test(t))) throw new Error("the step has a Start button: " + inner.join("|"));
      await ev(`q("#pl-start-plan").click(); return 1;`);
      await waitFor(`q(".pl-onoff").dataset.on === "1" && q("#pl-start-plan").disabled`, "the plan is on and running");
      const a1 = await apiGet(`api/plans/${PLAN}`);
      if (a1.active !== true || a1.run.state !== "running") throw new Error("the plan did not start: " + JSON.stringify({ a: a1.active, r: a1.run }));
      await ev(`q("#pl-stop-plan").click(); return 1;`);
      await waitFor(`q(".pl-onoff").dataset.on === "0"`, "the plan is off");
      const a2 = await apiGet(`api/plans/${PLAN}`);
      if (a2.active !== false) throw new Error("the plan is still on");
      if (!(await ev(`return q(".pl-stop-plan, #pl-stop-plan").disabled`))) throw new Error("Stop plan is not disabled when the plan is off");
    });
    if (pageErrors.length) out.push({ name: `${tag}: no script error`, ok: false, error: pageErrors.splice(0).join(" ;; ") });
  } finally {
    try { proc.kill(); } catch (e) { /* gone */ }
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch (e) { /* in use */ }
  }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "script", ok: false, error: String(e && e.stack || e) }])); });
