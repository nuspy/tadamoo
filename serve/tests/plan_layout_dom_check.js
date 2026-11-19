"use strict";
// The Plans page layout in a real headless browser at three screen sizes: a fixed bar and tabs, a fixed pane (the diagram and the inspector) a third of the window high by default and set
// with a divider, one lower part that scrolls, one-line empty fields, compact empty blocks, left-aligned logs: node plan_layout_dom_check.js <url> <plan title> <browser exe>.
// Prints [{name, ok, error}] as JSON (read by test_plan_layout_page.py).
const { spawn } = require("child_process"), fs = require("fs"), os = require("os"), path = require("path");
const [URL, TITLE, EXE] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const VIEWPORTS = [["desktop 1280x800", 1280, 800, false], ["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true]];
const KEY = "tadamoo.plans.pane";

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(path.join(os.tmpdir(), "plc-"));
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
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message + (pageErrors.length ? " | page errors: " + pageErrors.splice(0).join(" ;; ") : "") }); } const line = `${out[out.length - 1].ok ? "ok  " : "FAIL"} ${name} ${out[out.length - 1].error.slice(0, 900)}\n`; process.stderr.write(line); if (process.env.PL_LOG) fs.appendFileSync(process.env.PL_LOG, line); };
    const near = (a, b, tol, what) => { if (Math.abs(a - b) > tol) throw new Error(`${what}: ${a} is not ${b} (+-${tol})`); };
    const open = async (vw, vh, mobile, lang) => {
      await send("Emulation.setDeviceMetricsOverride", { width: vw, height: vh, deviceScaleFactor: 1, mobile });
      await sleep(300);                                   // lets the override settle before the navigate, instead of racing it
      // a navigate right after changing the emulated viewport occasionally lands while the browser is still mid-transition: the page's own
      // scripts then run with nothing set up yet (app.js itself did not finish) and every later script trips on undefined globals. Retried
      // whole, not just waited out longer: a page in that state never recovers on its own.
      let ready = false;
      for (let attempt = 0; attempt < 3 && !ready; attempt++) {
        pageErrors.length = 0;
        await send("Page.navigate", { url: URL + `?lang=${lang}` });
        await sleep(1500);
        try {
          await waitFor(`!!q("#tab-plans") && typeof view === "function"`, "the page, after changing the viewport", 40);
          ready = true;
        } catch (e) { /* one more try, with a fresh navigate */ }
      }
      if (!ready) throw new Error("never: the page, after changing the viewport, after retrying the navigate");
      pageErrors.length = 0;                                // the page is up: anything from an earlier, discarded attempt is not this check's problem
      await ev(`q("#tab-plans").click(); return 1;`);
      // the plan's own link can take a moment to render after the tab switch, especially right after a viewport change: retried, not a single blind attempt
      let clicked = false;
      for (let i = 0; i < 15 && !clicked; i++) {
        clicked = await ev(`const find = () => qa("#plans-root *").filter(e => e.children.length === 0 && e.textContent.includes(${JSON.stringify(TITLE)}) && e.closest("li, button, a")).sort((a, b) => a.textContent.length - b.textContent.length)[0];
          let hit = find(); if (!hit) { const tg = q("#pl-toggle"); if (tg && q(".plcol.shut")) { tg.click(); await wait(400); hit = find(); } }
          if (!hit) return false;
          (hit.closest("button, a, li, [role=button], [tabindex]") || hit).click(); return true;`);
        if (!clicked) await sleep(300);
      }
      if (!clicked) throw new Error("never: the plan's own link, after changing the viewport");
      for (let i = 0; i < 20; i++) {
        await ev(`const gb = qa("#plans-root button").find(b => /^(Graph|Grafo)$/.test(b.textContent.trim())); if (gb) gb.click(); const d = qa("#plans-root .pl-tabs button").find(b => /^(Plan definition|Definizione del piano)$/.test(b.textContent.trim())); if (d) d.click(); return 1;`);
        await sleep(300);
        if (await ev(`return !!q(".pl-split") && !!q(".pl-canvas") && q(".pl-canvas").getBoundingClientRect().height > 0`)) break;
      }
      await sleep(500);
    };
    const geo = () => ev(`const r = s => { const e = q(s); if (!e) return null; const b = e.getBoundingClientRect(); return { x: b.left, y: b.top, w: b.width, h: b.height, b: b.bottom, r: b.right }; };
      return { bar: r("header.bar"), head: r(".plmain > .sh"), tabs: r(".pl-tabs"), canvas: r(".pl-canvas"), split: r(".pl-split"), lower: r(".pl-lower"), detail: r(".pl-detail"), vh: innerHeight, vw: innerWidth,
        scroll: { h: document.documentElement.scrollHeight, w: document.documentElement.scrollWidth, y: scrollY, lower: (q(".pl-lower") || {}).scrollTop } }`).then(v => (typeof v === "string" ? JSON.parse(v) : v));

    for (const lang of ["en", "it"]) {
      for (const [vname, vw, vh, mobile] of VIEWPORTS) {
        const tag = `${vname}, ${lang}`;
        await send("Emulation.setTouchEmulationEnabled", { enabled: mobile, maxTouchPoints: 5 });
        await send("Page.navigate", { url: URL + "?lang=" + lang });
        await sleep(800);
        await ev(`try { localStorage.removeItem(${JSON.stringify(KEY)}); } catch (e) {} return 1;`);
        await open(vw, vh, mobile, lang);

        await check(`${tag}: the pane is a third of the window high by default, and the page itself does not scroll`, async () => {
          const g = await geo();
          if (!g.canvas || !g.split) throw new Error("no pane or splitter");
          near(g.canvas.h, g.vh / 3, 2.5, "the pane height");
          if (g.scroll.h > g.vh + 1) throw new Error("the page scrolls vertically: " + g.scroll.h + " > " + g.vh);
          if (g.scroll.w > g.vw + 1) throw new Error("the page scrolls sideways: " + g.scroll.w + " > " + g.vw);
          const v = await ev(`return q(".pl-split").getAttribute("aria-valuenow")`);
          if (v !== "33") throw new Error("aria-valuenow " + v);
        });

        await check(`${tag}: the bar and the tabs stay while the lower part scrolls, and the lower part does scroll`, async () => {
          const before = await geo();
          await ev(`const l = q(".pl-lower"), s = document.createElement("div"); s.id = "plc-spacer"; s.style.cssText = "height:1600px;flex:none"; l.append(s); l.scrollTop = 700; window.scrollTo(0, 500); return 1;`);
          await sleep(200);
          const after = await geo();
          await ev(`const s = q("#plc-spacer"); if (s) s.remove(); return 1;`);
          if (!(after.scroll.lower > 100)) throw new Error("the lower part did not scroll: " + after.scroll.lower);
          near(after.bar.y, before.bar.y, 0.5, "the top bar moved"); near(after.head.y, before.head.y, 0.5, "the plan header moved"); near(after.tabs.y, before.tabs.y, 0.5, "the tabs moved"); near(after.canvas.y, before.canvas.y, 0.5, "the pane moved");
          if (after.scroll.y !== 0) throw new Error("the page scrolled: " + after.scroll.y);
          const ov = await ev(`return getComputedStyle(q(".pl-lower")).overflowY`);
          if (ov !== "auto" && ov !== "scroll") throw new Error("overflow-y of the lower part " + ov);
        });

        await check(`${tag}: the divider is a separator that drags and answers the arrow keys, and the height is kept`, async () => {
          const before = await geo();
          const attrs = JSON.parse(await ev(`const s = q(".pl-split"); return JSON.stringify({ role: s.getAttribute("role"), ori: s.getAttribute("aria-orientation"), tab: s.tabIndex, label: s.getAttribute("aria-label"), min: s.getAttribute("aria-valuemin"), max: s.getAttribute("aria-valuemax") })`));
          if (attrs.role !== "separator" || attrs.ori !== "horizontal" || attrs.tab !== 0 || !attrs.label || attrs.min !== "15" || attrs.max !== "75") throw new Error("separator attributes " + JSON.stringify(attrs));
          if (mobile && before.split.h < 43.5) throw new Error("the handle is " + before.split.h + " px high on a phone");
          const cx = before.split.x + before.split.w / 2, cy = before.split.y + before.split.h / 2, dy = Math.round(vh * 0.12);
          if (!mobile) {
            await send("Input.dispatchMouseEvent", { type: "mousePressed", x: cx, y: cy, button: "left", buttons: 1, clickCount: 1 });
            for (let i = 1; i <= 6; i++) await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: cx, y: cy + dy * i / 6, button: "left", buttons: 1 });
            await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: cx, y: cy + dy, button: "left", buttons: 0, clickCount: 1 });
          } else {
            await ev(`const s = q(".pl-split"), fire = (t, y) => s.dispatchEvent(new PointerEvent(t, { bubbles: true, pointerId: 7, pointerType: "touch", clientX: ${cx}, clientY: y, button: 0, isPrimary: true }));
              fire("pointerdown", ${cy}); for (let i = 1; i <= 6; i++) fire("pointermove", ${cy} + ${dy} * i / 6); fire("pointerup", ${cy} + ${dy}); return 1;`);
          }
          await sleep(200);
          const moved = await geo();
          near(moved.canvas.h - before.canvas.h, dy, 6, "the pane did not follow the divider");
          const kept = await ev(`try { return localStorage.getItem(${JSON.stringify(KEY)}); } catch (e) { return ""; }`);
          if (!(Number(kept) > 0.36)) throw new Error("the height was not kept: " + kept);
          await open(vw, vh, mobile, lang);                                          // opened again: the same height
          near((await geo()).canvas.h, moved.canvas.h, 3, "the height after opening the plan again");
          const val = () => ev(`return Number(q(".pl-split").getAttribute("aria-valuenow"))`);
          const key = k => ev(`const s = q(".pl-split"); s.focus(); s.dispatchEvent(new KeyboardEvent("keydown", { key: ${JSON.stringify(k)}, bubbles: true, cancelable: true })); return 1;`);
          const v0 = await val();
          await key("ArrowUp"); await key("ArrowUp"); near(await val(), v0 - 4, 0.6, "two ArrowUp");
          await key("ArrowDown"); near(await val(), v0 - 2, 0.6, "ArrowDown");
          await key("Home"); if (await val() !== 15) throw new Error("Home: " + await val());
          await key("End"); if (await val() !== 75) throw new Error("End: " + await val());
          await key("Enter"); if (await val() !== 33) throw new Error("Enter: " + await val());
          near((await geo()).canvas.h, vh / 3, 3, "back to a third");
        });

        await check(`${tag}: the inspector is in the fixed upper part beside the diagram (a phone: at the top of the lower part) with a scroll of its own`, async () => {
          await ev(`q('.pl-node[data-id="b2"], .pl-li .pl-libtn') && (q('.pl-node[data-id="b2"]') || q(".pl-libtn")).dispatchEvent(new MouseEvent("click", { bubbles: true })); return 1;`);
          await waitFor(`!!q(".pl-detail .pl-dform")`, "the inspector filled");
          const f = JSON.parse(await ev(`const d = q(".pl-detail"), c = q(".pl-canvas"), l = q(".pl-lower"); const cr = c.getBoundingClientRect(), dr = d.getBoundingClientRect();
            return JSON.stringify({ inCanvas: c.contains(d), inLower: l.contains(d), dh: dr.height, ch: cr.height, dtop: dr.top, ctop: cr.top, oy: getComputedStyle(d).overflowY, sh: d.scrollHeight, cl: d.clientHeight })`));
          if (!mobile) {
            if (!f.inCanvas) throw new Error("the inspector is not in the fixed pane");
            near(f.dh, f.ch, 1.5, "the inspector is not as high as the pane"); near(f.dtop, f.ctop, 1.5, "the inspector does not start with the pane");
            if (f.oy !== "auto" && f.oy !== "scroll") throw new Error("the inspector's overflow-y is " + f.oy);
            if (!(f.sh > f.cl + 20)) throw new Error("the inspector is not taller than its box, so its own scroll cannot be tested: " + f.sh + " / " + f.cl);
            await ev(`const d = q(".pl-detail"); d.scrollTop = 150; window.scrollTo(0, 300); return 1;`);
            await sleep(150);
            const g = await geo();
            const st = await ev(`return q(".pl-detail").scrollTop`);
            if (!(st > 50)) throw new Error("the inspector did not scroll by itself: " + st);
            if (g.scroll.y !== 0) throw new Error("the page scrolled with the inspector");
            near(g.detail.y, g.canvas.y, 1.5, "the inspector left the pane");
          } else if (!f.inLower) throw new Error("on a phone the inspector is not in the lower part");
        });

        await check(`${tag}: an empty field is one line and grows when it is typed in; a block with nothing to show is one line`, async () => {
          const small = JSON.parse(await ev(`const out = qa(".pl-panel textarea").filter(t => t.offsetParent !== null && !t.value && !t.readOnly).map(t => ({ id: t.id || t.className, h: Math.round(t.getBoundingClientRect().height), rows: t.rows })); return JSON.stringify(out)`));
          if (!small.length) throw new Error("no empty text field to look at");
          const tall = small.filter(t => t.h > 50 || t.rows > 1);
          if (tall.length) throw new Error("empty fields higher than one line: " + JSON.stringify(tall));
          const id = small[0].id.startsWith("pl-") || small[0].id.startsWith("sc") ? small[0].id : "";
          const target = await ev(`const t = qa(".pl-panel textarea").filter(t => t.offsetParent !== null && !t.value && !t.readOnly)[0]; t.focus(); const h0 = t.getBoundingClientRect().height; t.value = "one\\ntwo\\nthree\\nfour\\nfive"; t.dispatchEvent(new Event("input", { bubbles: true })); await wait(100); const h1 = t.getBoundingClientRect().height; t.value = ""; t.dispatchEvent(new Event("input", { bubbles: true })); await wait(100); return JSON.stringify({ h0, h1, h2: t.getBoundingClientRect().height })`).then(JSON.parse);
          if (!(target.h1 > target.h0 * 2)) throw new Error("the field did not grow when typed in: " + JSON.stringify(target));
          if (target.h2 > target.h0 + 2) throw new Error("the field did not shrink when emptied: " + JSON.stringify(target));
          const nothing = JSON.parse(await ev(`return JSON.stringify(qa(".pl-side > .pl-nothing").map(e => Math.round(e.getBoundingClientRect().height)))`));
          if (!nothing.length) throw new Error("no block shown as one line");
          if (nothing.some(h => h > 44)) throw new Error("a block with nothing to show is " + nothing.join(",") + " px high");
        });

        await check(`${tag}: the history lines are left aligned, one line each, and nothing scrolls the page sideways`, async () => {
          const h = JSON.parse(await ev(`const ul = q(".pl-hist"); if (!ul) return JSON.stringify({ none: true }); const box = ul.getBoundingClientRect();
            return JSON.stringify({ items: [...ul.children].slice(0, 6).map(li => { const r = li.getBoundingClientRect(), first = li.firstElementChild.getBoundingClientRect(), cs = getComputedStyle(li); return { h: Math.round(r.height), lh: parseFloat(cs.lineHeight), left: Math.round(first.left - box.left), align: cs.textAlign, ws: cs.whiteSpace, disp: cs.display, mono: /mono|consolas|courier/i.test(cs.fontFamily) }; }) })`));
          if (h.none) throw new Error("no history lines to look at");
          for (const it of h.items) {
            if (it.align !== "left" && it.align !== "start") throw new Error("not left aligned: " + JSON.stringify(it));
            if (it.left > 16) throw new Error("indented: " + JSON.stringify(it));
            if (it.h > it.lh * 1.6) throw new Error("more than one line: " + JSON.stringify(it));
            if (!it.mono) throw new Error("not monospace: " + JSON.stringify(it));
          }
          const g = await geo();
          if (g.scroll.w > g.vw + 1) throw new Error("the page scrolls sideways");
        });
        await check(`${tag}: the plan's settings panel and the Chat's are a readable column, left aligned, the full width on a narrow screen, with no sideways scroll`, async () => {
          const measure = sel => ev(`const e = q(${JSON.stringify(sel)}); if (!e) return null; const b = e.getBoundingClientRect(), p = e.parentElement.getBoundingClientRect(); const wide = qa(${JSON.stringify(sel)} + " select, " + ${JSON.stringify(sel)} + " input[type=text], " + ${JSON.stringify(sel)} + " textarea").map(c => c.getBoundingClientRect().right).reduce((a, c) => Math.max(a, c), 0);
            return JSON.stringify({ w: b.width, left: b.left - p.left, right: b.right, wide, sw: document.documentElement.scrollWidth, vw: innerWidth })`).then(v => v && JSON.parse(v));
          const verdict = (m, what, leftAligned) => {
            if (!m) throw new Error(what + ": panel not found");
            if (mobile) { if (m.w > m.vw + 1) throw new Error(what + " wider than the screen: " + m.w); }
            else if (m.w > 725) throw new Error(what + " is " + Math.round(m.w) + " px wide, a readable column is at most about 720");
            if (leftAligned && m.left > 24) throw new Error(what + " is not left aligned: " + Math.round(m.left) + " px from its column's left");
            if (m.wide > m.right + 1) throw new Error(what + ": a control sticks out of the panel");
            if (m.sw > m.vw + 1) throw new Error(what + " scrolls the page sideways");
          };
          await ev(`const b = qa("#plans-root .pl-btn, #plans-root button").find(b => /^(Inference settings|Impostazioni di inferenza)$/.test(b.textContent.trim())); if (b) b.click(); return 1;`);
          await waitFor(`!!q(".plinfhost .chatset") && q(".plinfhost .chatset").getBoundingClientRect().width > 0`, "the plan's inference settings");
          verdict(await measure(".plinfhost .chatset"), "the plan's inference settings", true);
          await ev(`q("#tab-chat").click(); await wait(500); const b = q("#cs-open"); if (b && q("#chatset").hidden) b.click(); return 1;`);
          await waitFor(`!q("#chatset").hidden && q("#chatset").getBoundingClientRect().width > 0`, "the Chat settings");
          verdict(await measure("#chatset"), "the Chat settings");
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
