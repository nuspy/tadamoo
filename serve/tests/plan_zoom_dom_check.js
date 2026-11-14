"use strict";
// Zoom and pan of the plan diagram (serve/web/planzoom.js) in a real headless browser at three screen sizes, in English and Italian, with real mouse, wheel, key and touch input
// sent through the DevTools protocol: node plan_zoom_dom_check.js <url> <plan title> <browser exe>. Prints [{name, ok, error}] as JSON (read by test_plan_zoom_page.py).
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, TITLE, EXE] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const ALL = [["desktop 1280x800", 1280, 800, false], ["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true]];
const VIEWPORTS = ALL.filter(v => !process.argv[5] || v[0] === process.argv[5]);            // one screen size and one language for each browser: mouse and touch input do not share a session

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "pzc-"));
  const proc = spawn(EXE, ["--headless=new", "--lang=en-US", "--disable-gpu", "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows", "--disable-features=CalculateNativeWinOcclusion", `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, "--window-size=1440,900", "about:blank"], { stdio: "ignore" });
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
    const send1 = (method, params, ms) => new Promise((r, rej) => { const n = ++id; const t = setTimeout(() => { waits.delete(n); rej(new Error(`${method} did not answer`)); }, ms); waits.set(n, v => { clearTimeout(t); r(v); }); ws.send(JSON.stringify({ id: n, method, params })); });
    // the browser sometimes drops an input event it was sent right after a page load: one that is not answered is sent again
    const send = async (method, params = {}) => { if (!method.startsWith("Input.")) return send1(method, params, 60000); for (let k = 0; ; k++) { try { return await send1(method, params, 8000); } catch (e) { if (k >= 3) throw e; } } };
    const ev = async expr => {
      const r = await send("Runtime.evaluate", { expression: `(async () => { const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)]; const wait = ms => new Promise(r => setTimeout(r, ms)); ${expr} })()`, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception && r.exceptionDetails.exception.description));
      return r.result.value;
    };
    await send("Page.enable"); await send("Runtime.enable");
    const waitFor = async (expr, what, tries = 75) => { for (let i = 0; i < tries; i++) { try { if (await ev(`return (${expr});`)) return true; } catch (e) { /* not yet */ } await sleep(200); } throw new Error(`never: ${what}`); };
    const check = async (name, fn, raw) => { try { if (!raw) await ev(`const z = q(".pl-zreset"); if (z) z.click(); const g = q(".pl-graph"); if (g) g.scrollTo(0, 0); return 1;`); if (!raw) await sleep(80); try { await fn(); } catch (first) { await ev(`const z = q(".pl-zreset"); if (z) z.click(); return 1;`); await sleep(400); await fn(); }       // an input event that arrives late on a busy machine gets one more try
          out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message + (pageErrors.length ? " | page errors: " + pageErrors.splice(0).join(" ;; ") : "") }); } const line = `${out[out.length - 1].ok ? "ok  " : "FAIL"} ${name} ${out[out.length - 1].error.slice(0, 1800)}\n`; process.stderr.write(line); if (process.env.PZ_LOG) fs.appendFileSync(process.env.PZ_LOG, line); };
    const mouse = (type, x, y, extra = {}) => send("Input.dispatchMouseEvent", { type, x, y, ...extra });
    const wheel = (x, y, deltaY, ctrl) => mouse("mouseWheel", x, y, { deltaX: 0, deltaY, modifiers: ctrl ? 2 : 0 });
    const touch = (type, points) => send("Input.dispatchTouchEvent", { type, touchPoints: points });
    const zoom = () => ev(`return window.__zoom().zoom`);
    const near = (a, b, tol, what) => { if (Math.abs(a - b) > tol) throw new Error(`${what}: ${a} is not ${b} (+-${tol})`); };
    /* where things are on the screen */
    const geo = () => ev(`const g = q(".pl-graph"), r = g.getBoundingClientRect(), svg = q(".pl-svg"), s = svg.getBoundingClientRect(), n = qa(".pl-node:not(.ghost)").map(e => { const b = e.getBoundingClientRect(); return { id: e.dataset.id, x: b.left, y: b.top, w: b.width, h: b.height }; });
      return { frame: { x: r.left, y: r.top, w: r.width, h: r.height, sl: g.scrollLeft, st: g.scrollTop, sw: g.scrollWidth, sh: g.scrollHeight }, svg: { x: s.left, y: s.top, w: s.width, h: s.height, attr: Number(svg.getAttribute("width")), vb: svg.viewBox.baseVal.width }, nodes: n };`);
    /* a point of the empty background (inside the visible frame, on the SVG, on no node or edge) */
    const emptyPoint = () => ev(`const g = q(".pl-graph").getBoundingClientRect(); const skip = ".pl-node, .pl-pg, .pl-edgehit, .pl-x, .pl-edge, .pl-edgeg, .pl-port, .pl-hit";
      const free = (x, y) => { const e = document.elementFromPoint(x, y); return !!e && !!e.closest(".pl-svg") && !e.closest(skip); };
      for (const around of [[[0, 0], [-18, 0], [18, 0], [0, -18], [0, 18], [-40, 0]], [[0, 0], [-12, 0], [12, 0], [0, -12], [0, 12]], [[0, 0], [-8, 0], [8, 0]]])           // a finger covers more than a point
        for (let y = g.top + g.height - 12; y > g.top + 8; y -= 8) for (let x = g.left + g.width - 12; x > g.left + 8; x -= 8) if (around.every(([dx, dy]) => free(x + dx, y + dy))) return { x, y };
      return null;`);
    const cursorAt = (x, y) => ev(`const e = document.elementFromPoint(${x}, ${y}); return e ? getComputedStyle(e).cursor : ""`);

    for (const lang of ["en", "it"].filter(l => !process.argv[6] || l === process.argv[6])) {
      for (const [vname, w, h, mobile] of VIEWPORTS) {
        const tag = `${vname}, ${lang}`;
        if (process.env.PZ_ONLY && !process.env.PZ_ONLY.split(";").includes(tag)) continue;                    // a debugging aid: one combination, e.g. "desktop 1280x800, en"
        await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
        await send("Emulation.setTouchEmulationEnabled", { enabled: mobile, maxTouchPoints: 5 });
        await send("Page.navigate", { url: URL + `?lang=${lang}` });
        await sleep(1500);
        await ev(`q("#tab-plans").click(); await wait(1200);
          const hit = qa("#plans-root *").filter(e => e.children.length === 0 && e.textContent.includes(${JSON.stringify(TITLE)})).sort((a, b) => a.textContent.length - b.textContent.length)[0];
          (hit.closest("button, a, li, [role=button], [tabindex]") || hit).click(); return 1;`);
        for (let i = 0; i < 20; i++) {
          await ev(`const gb = qa("#plans-root button").find(b => /^(Graph|Grafo)$/.test(b.textContent.trim())); if (gb) gb.click(); return 1;`);
          await sleep(400);
          if (await ev(`const n = q("#plans-root .pl-node"); return !!n && n.getBoundingClientRect().width > 0`)) break;
        }
        await waitFor(`!!q(".pl-zoom") && !!q(".pl-svg")`, "the graph with its zoom bar");
        await check(`${tag}: the start step is drawn (it sits left of the origin), the whole diagram is in view when the plan opens, and a step is deleted and brought back by Undo with its links`, async () => {
          await sleep(400);
          const o = JSON.parse(await ev(`const g = q(".pl-graph").getBoundingClientRect(), svg = q(".pl-svg"), vb = svg.viewBox.baseVal, nodes = qa(".pl-node:not(.ghost)").map(e => { const b = e.getBoundingClientRect(); return { id: e.dataset.id, l: b.left, t: b.top, r: b.right, b: b.bottom, w: b.width }; });
            return JSON.stringify({ g: { l: g.left, t: g.top, r: g.right, b: g.bottom }, vbx: vb.x, vby: vb.y, nodes, edges: qa(".pl-edgeg").length, zoom: Number(svg.getAttribute("width")) / vb.width })`));
          const start = o.nodes.find(n => n.id === "start");
          if (!start) throw new Error("the start step is not in the diagram: " + JSON.stringify(o.nodes.map(n => n.id)));
          if (!(o.vbx < 0)) throw new Error("the drawing does not reach left of the origin: viewBox x " + o.vbx);
          if (start.w < 10) throw new Error("the start step has no size");
          const outside = o.nodes.filter(n => n.l < o.g.l - 1 || n.r > o.g.r + 1 || n.t < o.g.t - 1 || n.b > o.g.b + 1);
          if (outside.length && o.zoom > 0.505) throw new Error("steps outside the frame when the plan opens: " + outside.map(n => n.id).join(",") + " zoom " + o.zoom);
          await ev(`const n = q('.pl-node[data-id="b2"]'); n.dispatchEvent(new MouseEvent("click", { bubbles: true })); return 1;`);
          await waitFor(`!!q(".pl-zdel")`, "the delete button of the selected step");
          const size = await ev(`const b = q(".pl-zdel").getBoundingClientRect(); return JSON.stringify({ w: b.width, h: b.height })`).then(JSON.parse);
          if (size.h < (mobile ? 43.5 : 26) || size.w < (mobile ? 43.5 : 26)) throw new Error("delete button too small " + JSON.stringify(size));       // 44 px for a finger, a little less for a mouse
          if (mobile) await ev(`q(".pl-zdel").click(); return 1;`);
          else await ev(`const n = q('.pl-node[data-id="b2"]'); n.focus(); n.dispatchEvent(new KeyboardEvent("keydown", { key: "Delete", bubbles: true, cancelable: true })); return 1;`);
          await waitFor(`!q('.pl-node[data-id="b2"]') && !!q(".pl-zundo")`, "the step gone and Undo offered", 60);
          await ev(`q(".pl-zundo").click(); return 1;`);
          await waitFor(`!!q('.pl-node[data-id="b2"]')`, "the step back", 60);
          await sleep(300);
          const edges = await ev(`return qa(".pl-edgeg").length`);
          if (edges !== o.edges) throw new Error("links after undo: " + edges + " instead of " + o.edges);
        }, true);
        await ev(`window.__zoom = () => { const g = q(".pl-graph"), svg = q(".pl-svg"), vb = svg.viewBox.baseVal.width; return { zoom: Number(svg.getAttribute("width")) / vb, pct: q(".pl-zreset").textContent }; }; q(".pl-zreset").click(); q(".pl-graph").scrollTo(0, 0); return 1;`);
        await ev(`q(".pl-graph").scrollIntoView({ block: "start" }); window.scrollBy(0, -30); return 1;`);

        if (!mobile) await check(`${tag}: the wheel alone zooms around the pointer, with limits, and the page does not scroll; Ctrl and the wheel do the same`, async () => {
          near(await zoom(), 1, 0.005, "start");
          const p = await emptyPoint(); if (!p) throw new Error("no empty background to point at");
          const before = await geo();
          const under = before.nodes[0];
          const unit = before.svg.w / before.svg.vb, ux = (p.x - before.svg.x) / unit, uy = (p.y - before.svg.y) / unit;          // the layout point under the pointer
          await wheel(p.x, p.y, -300, false);
          await sleep(150);
          const z1 = await zoom();
          if (!(z1 > 1.3 && z1 < 1.8)) throw new Error("zoom after a wheel up: " + z1);
          const after = await geo();
          if (process.env.PZ_DEBUG) process.stderr.write(JSON.stringify({ p, z1, before: { f: before.frame, s: before.svg }, after: { f: after.frame, s: after.svg }, ux, uy }) + "\n");
          near(after.svg.x + ux * z1, p.x, 3, "the point under the pointer moved (x)"); if (after.frame.h === before.frame.h && after.frame.sh > after.frame.h + 20) near(after.svg.y + uy * z1, p.y, 3, "the point under the pointer moved (y)");       // when the frame can scroll that way
          await wheel(p.x, p.y, 300, false); await sleep(150);
          near(await zoom(), 1, 0.01, "back");
          const y0 = await ev(`return window.scrollY`);
          await wheel(p.x, p.y, -300, true); await sleep(150);                           // Ctrl and the wheel: the same
          near(await zoom(), z1, 0.001, "Ctrl and the wheel did not zoom like the wheel");
          await wheel(p.x, p.y, 300, false); await sleep(150);
          near(await zoom(), 1, 0.01, "the wheel alone did not zoom back");
          near(await ev(`return window.scrollY`), y0, 0.5, "the page scrolled while the pointer was over the diagram");
          for (let i = 0; i < 4; i++) { await wheel(p.x, p.y, -2500, true); await sleep(30); }
          await sleep(150);
          near(await zoom(), 3, 0.001, "the upper limit");
          if (!/300%/.test(await ev(`return q(".pl-zreset").textContent`)) || !(await ev(`return q(".pl-zin").disabled`))) throw new Error("the indicator or the plus button at the limit");
          for (let i = 0; i < 4; i++) { await wheel(p.x, p.y, 2500, true); await sleep(30); }
          await sleep(150);
          near(await zoom(), 0.25, 0.001, "the lower limit");
          if (!(await ev(`return q(".pl-zout").disabled`))) throw new Error("the minus button at the limit");
          await ev(`q(".pl-zreset").click(); return 1;`);
        });

        if (!mobile) await check(`${tag}: the middle button pans, the diagram following the pointer; the hand is open over the background and closed while it moves; steps and links keep their own cursors`, async () => {
          await ev(`q(".pl-zin").click(); q(".pl-zin").click(); q(".pl-zin").click(); return 1;`);            // zoomed in so that the frame scrolls
          await sleep(100);
          const p = await emptyPoint(); if (!p) throw new Error("no empty background");
          if (await cursorAt(p.x, p.y) !== "grab") throw new Error("over the background the cursor is " + await cursorAt(p.x, p.y));
          const g0 = await geo();
          if (!(g0.frame.sw > g0.frame.w + 50 && g0.frame.sh > g0.frame.h - 1 || g0.frame.sw > g0.frame.w + 50)) throw new Error("the frame does not scroll: " + JSON.stringify(g0.frame));
          const nodeAt = g0.nodes.find(n => n.x > g0.frame.x && n.x + n.w < g0.frame.x + g0.frame.w && n.y > g0.frame.y && n.y + n.h < g0.frame.y + g0.frame.h);
          if (nodeAt && await cursorAt(nodeAt.x + nodeAt.w / 2, nodeAt.y + 6) !== "move") throw new Error("over a step the cursor is " + await cursorAt(nodeAt.x + nodeAt.w / 2, nodeAt.y + 6));
          await ev(`q(".pl-graph").scrollTo(0, 0); return 1;`); await sleep(60);
          const s0 = (await geo()).frame;
          await mouse("mousePressed", p.x, p.y, { button: "middle", buttons: 4, clickCount: 1 });
          await mouse("mouseMoved", p.x - 60, p.y - 30, { button: "middle", buttons: 4 });
          await sleep(80);
          if (!(await ev(`return q(".pl-graph").classList.contains("pl-panning")`))) throw new Error("no panning class while the middle button is down");
          if (await cursorAt(p.x - 60, p.y - 30) !== "grabbing") throw new Error("while panning the cursor is " + await cursorAt(p.x - 60, p.y - 30));
          const s1 = (await geo()).frame;
          near(s1.sl - s0.sl, 60, 2, "the diagram did not follow the pointer to the left");
          if (s0.sh > s0.h + 40) near(s1.st - s0.st, 30, 2, "the diagram did not follow the pointer upwards");
          await mouse("mouseMoved", p.x, p.y, { button: "middle", buttons: 4 }); await sleep(60);
          near((await geo()).frame.sl, s0.sl, 2, "moving back did not bring it back");
          await mouse("mouseReleased", p.x, p.y, { button: "middle", buttons: 0, clickCount: 1 });
          await sleep(60);
          if (await ev(`return q(".pl-graph").classList.contains("pl-panning")`)) throw new Error("still panning after the button was let go");
          let hand = "";
          const q2 = await emptyPoint();                                          // the diagram has moved: a point of the empty background as it is now
          for (let k = 0; k < 8 && hand !== "grab"; k++) { hand = q2 ? await cursorAt(q2.x, q2.y) : "no empty background"; if (hand !== "grab") await sleep(150); }
          if (hand !== "grab") throw new Error("the open hand did not come back: " + hand);
          await mouse("mouseMoved", p.x - 30, p.y, { buttons: 0 }); await sleep(60);
          near((await geo()).frame.sl, s0.sl, 2, "the diagram moved without a button");
          await ev(`q(".pl-zreset").click(); return 1;`);
        });

        if (!mobile) await check(`${tag}: a step still drags (at any zoom), a link is still picked, and the left button does not pan`, async () => {
          await ev(`q(".pl-zin").click(); return 1;`); await sleep(100);
          const g = await geo();
          const n = g.nodes.find(x => x.id !== "start" && x.x > g.frame.x + 4 && x.x + x.w < g.frame.x + g.frame.w - 80 && x.y > g.frame.y + 4 && x.y + x.h < g.frame.y + g.frame.h - 40) || g.nodes[1];
          const z = await zoom();
          const sx = n.x + n.w / 2, sy = n.y + n.h / 2;
          if (process.env.PZ_DEBUG) process.stderr.write("drag " + JSON.stringify({ n, z, hit: await ev(`const e = document.elementFromPoint(${sx}, ${sy}); return e ? e.tagName + "." + (e.getAttribute("class") || "") + " in " + ((e.closest(".pl-node") || {}).dataset || {}).id : null`) }) + "\n");
          await mouse("mousePressed", sx, sy, { button: "left", buttons: 1, clickCount: 1 });
          for (let i = 1; i <= 6; i++) await mouse("mouseMoved", sx + i * 10, sy + i * 4, { button: "left", buttons: 1 });
          await mouse("mouseReleased", sx + 60, sy + 24, { button: "left", buttons: 0, clickCount: 1 });
          await sleep(500);
          const after = (await geo()).nodes.find(x => x.id === n.id);
          near(after.x - n.x, 60, 6, `the step did not follow the pointer at ${Math.round(z * 100)}%`); near(after.y - n.y, 24, 6, "the step moved wrongly in y");
          if ((await geo()).frame.sl !== g.frame.sl) throw new Error("the left button panned the frame");
          const edge = await ev(`const e = qa(".pl-edgehit").find(x => { const b = x.getBoundingClientRect(); return b.width > 4; }); if (!e) return null; const len = e.getTotalLength(), pt = e.getPointAtLength(len / 2), m = e.getScreenCTM(); return { x: m.a * pt.x + m.e, y: m.d * pt.y + m.f }`);
          if (edge) {
            await ev(`window.__hit = document.elementFromPoint(${edge.x}, ${edge.y}).closest(".pl-edgehit") ? 1 : 0; return 1;`);
            if (await ev(`return window.__hit`)) {
              if (await cursorAt(edge.x, edge.y) !== "pointer") throw new Error("over a link the cursor is " + await cursorAt(edge.x, edge.y));
              await mouse("mousePressed", edge.x, edge.y, { button: "left", buttons: 1, clickCount: 1 }); await mouse("mouseReleased", edge.x, edge.y, { button: "left", buttons: 0, clickCount: 1 });
              await sleep(300);
              if (!(await ev(`return !!q(".pl-edgehit[aria-pressed=true]")`))) throw new Error("the link was not picked");
            }
          }
          await ev(`q(".pl-zreset").click(); return 1;`);
        });

        await check(`${tag}: + - and 0 zoom from the keyboard, and the bar has three 44 px controls with their names`, async () => {
          await ev(`q(".pl-graph").scrollIntoView({ block: "nearest" }); const n = q(".pl-node:not(.ghost)"); n.focus(); return 1;`);
          const key = k => ev(`const t = document.activeElement || q(".pl-graph"); t.dispatchEvent(new KeyboardEvent("keydown", { key: ${JSON.stringify(k)}, bubbles: true, cancelable: true })); return 1;`);
          near(await zoom(), 1, 0.005, "start");
          await key("+"); near(await zoom(), 1.25, 0.01, "+"); await key("="); near(await zoom(), 1.5625, 0.01, "= is plus too");
          await key("-"); near(await zoom(), 1.25, 0.01, "-"); await key("0"); near(await zoom(), 1, 0.005, "0");
          await key("-"); await key("-"); near(await zoom(), 0.64, 0.01, "minus twice"); await key("0");
          const bar = JSON.parse(await ev(`const b = qa(".pl-zb:not(.pl-zdel):not(.pl-zundo)").map(e => { const r = e.getBoundingClientRect(); return { w: r.width, h: r.height, name: e.getAttribute("aria-label") || "", txt: e.textContent }; }); const r = q(".pl-zoom").getBoundingClientRect();
            return JSON.stringify({ b, inside: r.right <= innerWidth + 1 && r.left >= -1, wide: document.documentElement.scrollWidth > innerWidth + 1 })`));
          if (bar.b.length !== 4 || bar.b.some(x => x.w < (mobile ? 43.5 : 26) || x.h < (mobile ? 43.5 : 26) || !x.name)) throw new Error("zoom bar: " + JSON.stringify(bar));
          if (!bar.inside || bar.wide) throw new Error("the bar leaves the screen or the page scrolls sideways");
          const want = lang === "it" ? ["Riduci", "Ingrandisci"] : ["Zoom out", "Zoom in"];
          if (!bar.b[0].name.includes(want[0]) || !bar.b[2].name.includes(want[1]) || bar.b[1].txt !== "100%") throw new Error("names: " + JSON.stringify(bar.b));
          await ev(`q(".pl-zin").click(); return 1;`);
          if ((await ev(`return q(".pl-zreset").textContent`)) !== "125%") throw new Error("the indicator does not follow");
          await ev(`q(".pl-zreset").click(); return 1;`);
          near(await zoom(), 1, 0.005, "the indicator button did not reset");
        });

        if (mobile) await check(`${tag}: two fingers pinch-zoom around their middle and one finger on the background pans, while a finger on a step still drags it`, async () => {
          await ev(`q(".pl-zin").click(); q(".pl-zin").click(); return 1;`); await sleep(100);
          const p = await emptyPoint(); if (!p) throw new Error("no empty background");
          const z0 = await zoom();
          const pair = await ev(`const g = q(".pl-graph").getBoundingClientRect(), skip = ".pl-node, .pl-pg, .pl-edgehit, .pl-x, .pl-edge, .pl-edgeg, .pl-port, .pl-hit";
            const empty = (x, y) => { const e = document.elementFromPoint(x, y); return !!e && !!e.closest(".pl-svg") && !e.closest(skip); };
            for (let y = g.top + g.height - 14; y > g.top + 8; y -= 12) for (let x = g.left + 24; x < g.left + g.width - 90; x += 12) if (empty(x, y) && empty(x + 64, y) && empty(x + 32, y)) return { a: { x, y }, b: { x: x + 64, y } };
            return null;`);
          if (!pair) throw new Error("no two empty points for the fingers");
          const a = { ...pair.a, id: 1 }, b = { ...pair.b, id: 2 };
          await touch("touchStart", [a, b]);
          for (let i = 1; i <= 6; i++) await touch("touchMove", [{ ...a, x: a.x - i * 4, y: a.y }, { ...b, x: b.x + i * 4, y: b.y }]);
          await touch("touchEnd", []);
          await sleep(150);
          const z1 = await zoom();
          if (!(z1 > z0 * 1.3)) throw new Error(`the pinch did not zoom in: ${z0} -> ${z1}`);
          await ev(`q(".pl-graph").scrollTo(0, 0); return 1;`); await sleep(60);
          const s0 = (await geo()).frame;
          const r = await emptyPoint(); if (!r) throw new Error("no empty background after the pinch");
          await touch("touchStart", [{ x: r.x, y: r.y, id: 1 }]);
          for (let i = 1; i <= 6; i++) await touch("touchMove", [{ x: r.x - i * 12, y: r.y, id: 1 }]);
          await touch("touchEnd", []);
          await sleep(150);
          const s1 = (await geo()).frame;
          if (!(s1.sl - s0.sl > 40)) throw new Error(`one finger did not pan the diagram: ${s0.sl} -> ${s1.sl}`);
          await ev(`q(".pl-zreset").click(); return 1;`); await sleep(100);
          const g = await geo();
          const n = g.nodes.find(x => x.x > g.frame.x + 4 && x.x + x.w < g.frame.x + g.frame.w - 4 && x.y > g.frame.y + 4 && x.y + x.h < g.frame.y + g.frame.h - 4);
          if (n) {
            const sl0 = g.frame.sl, sx = n.x + n.w / 2, sy = n.y + n.h / 2;
            await touch("touchStart", [{ x: sx, y: sy, id: 1 }]);
            for (let i = 1; i <= 6; i++) await touch("touchMove", [{ x: sx + i * 8, y: sy + i * 3, id: 1 }]);
            await touch("touchEnd", []);
            await sleep(500);
            const after = (await geo()).nodes.find(x => x.id === n.id);
            if (!(after.x - n.x > 30)) throw new Error(`a finger on a step did not drag it: ${n.x} -> ${after.x}`);
            if ((await geo()).frame.sl !== sl0) throw new Error("dragging a step also panned the frame");
          }
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
