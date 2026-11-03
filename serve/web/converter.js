"use strict";
/* Models > Model converter: convert a model to the .basalt format with the options you choose (the expert profile, the drafter, the projector),
   test it in a server of its own, and compare it with the models you have and with the published figures: a star, bars, points or the table.
   A test already made under the same settings is offered for reuse before it is repeated. Everything the server says enters the page as text. */
(() => {
  const root = document.getElementById("ld-converter");
  if (!root) return;
  const C = window.TadamooCharts, MAX_STAR = 4, MAX_PICK = 8;
  const store = {
    get(k, d) { try { const v = localStorage.getItem("basalt.cv." + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem("basalt.cv." + k, JSON.stringify(v)); } catch (e) { /* this page only */ } },
  };
  const esc = C.esc;
  const GIB = 2 ** 30;
  const gib = n => nfmt(n / GIB, 1);
  const on = (el, ev, fn) => el.addEventListener(ev, fn);
  const $ = (s, el = root) => el.querySelector(s);
  const errText = r => (r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`;

  let O = null;                                       // GET /api/bench/overview
  let plan = null, planSeq = 0, planTimer = 0;
  let convJob = null, testJob = null, pollTimer = 0;
  let selected = store.get("selected", []);
  let view = store.get("view", "radar");
  let axes = store.get("axes", ["decode", "gsm8k_pct", "ram_gib", "vram_gib", "load_s", "size_gib"]);
  let pts = store.get("pts", { x: "decode", y: "gsm8k_pct", size: "ram_gib" });
  let group = store.get("group", "all");
  let showPub = store.get("pub", false);
  let colors = store.get("colors", {});                // a model keeps its colour, whatever the others are

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const metricLabel = m => T("cv.m." + m.id);
  const axisLabel = m => T("cv.ma." + m.id);
  const better = m => T(m.better === "lower" ? "cv.better.lower" : "cv.better.higher");
  const ago = at => new Date(at * 1000).toLocaleString(LANG === "it" ? "it-IT" : "en-GB", { dateStyle: "medium", timeStyle: "short" });

  function colorOf(id) {
    if (!colors[id]) {
      const used = new Set(Object.values(colors));
      colors[id] = [...Array(C.COLORS.length).keys()].find(i => !used.has(i)) ?? Object.keys(colors).length % C.COLORS.length;
      store.set("colors", colors);
    }
    return C.COLORS[colors[id]];
  }
  const chosen = () => selected.map(id => (O.models || []).find(m => m.id === id)).filter(Boolean)
    .map(m => ({ ...m, color: colorOf(m.id), shape: C.SHAPES[selected.indexOf(m.id) % C.SHAPES.length] }));

  /* ---- the page ---- */
  function skeleton() {
    root.innerHTML = `
      <p class="setnote cv-intro">${esc(T("cv.intro"))}</p>
      <section class="sec cv-sec" id="cv-convert" aria-labelledby="cv-h1"><div class="sh"><b id="cv-h1">${esc(T("cv.convert.title"))}</b></div><div class="cv-body" id="cv-convert-body"></div></section>
      <section class="sec cv-sec" id="cv-job" aria-live="polite" hidden></section>
      <section class="sec cv-sec" id="cv-models" aria-labelledby="cv-h2"><div class="sh"><b id="cv-h2">${esc(T("cv.models.title"))}</b></div><div class="cv-body" id="cv-models-body"></div></section>
      <section class="sec cv-sec" id="cv-compare" aria-labelledby="cv-h3"><div class="sh"><b id="cv-h3">${esc(T("cv.compare.title"))}</b></div><div class="cv-body" id="cv-compare-body"></div></section>
      <dialog class="cv-dlg" id="cv-dlg" aria-labelledby="cv-dlg-h"><div id="cv-dlg-body"></div></dialog>
      <div class="cv-tip" id="cv-tip" role="tooltip" hidden></div>`;
  }

  /* ---- convert ---- */
  const opt = (v, text, cur) => `<option value="${esc(v)}"${String(cur) === String(v) ? " selected" : ""}>${esc(text)}</option>`;
  function convertForm() {
    const body = $("#cv-convert-body");
    const src = O.convertible || [];
    if (!src.length) { body.innerHTML = `<p class="dim">${esc(T("cv.src.none"))}</p>`; return; }
    const keep = ($("#cv-src") || {}).value || src[0].id;
    body.innerHTML = `<form class="cv-form" id="cv-form" novalidate>
      <div class="cv-field"><label for="cv-src">${esc(T("cv.src"))}</label><select id="cv-src">${src.map(m => opt(m.id, `${m.name} · ${m.quant || "?"} · ${gib(m.size_bytes)} GiB`, keep)).join("")}</select>
        <p class="help">${esc(T("cv.src.h"))}</p></div>
      <div class="cv-field"><label for="cv-name">${esc(T("cv.name"))}</label><input id="cv-name" type="text" maxlength="200" autocomplete="off"><p class="help">${esc(T("cv.name.h"))}</p></div>
      <div class="cv-field"><label for="cv-out">${esc(T("cv.out"))}</label><input id="cv-out" type="text" autocomplete="off" spellcheck="false" value="${esc((O.out_dir || '').replace(/\//g, navigator.platform.startsWith("Win") ? "\\" : "/"))}"><p class="help">${esc(T("cv.out.h"))}</p></div>
      <div class="cv-field"><label for="cv-profile">${esc(T("cv.profile"))}</label>
        <select id="cv-profile">${["auto", "plain", "file", "model"].map(v => opt(v, T("cv.profile." + v), "auto")).join("")}</select>
        <div class="cv-sub" id="cv-profile-file" hidden><label for="cv-profile-path">${esc(T("cv.path"))}</label><input id="cv-profile-path" type="text" spellcheck="false" autocomplete="off"></div>
        <div class="cv-sub" id="cv-profile-model" hidden><label for="cv-profile-pick">${esc(T("cv.profile.pick"))}</label>
          <select id="cv-profile-pick">${(O.profiles || []).map(p => opt(p.path, p.name || p.path, "")).join("") || opt("", T("cv.profile.none"), "")}</select></div>
        <p class="help">${esc(T("cv.profile.h"))}</p></div>
      <div class="cv-field"><label for="cv-mtp">${esc(T("cv.mtp"))}</label>
        <select id="cv-mtp">${["original", "tuned_pack", "tuned_file", "none"].map(v => opt(v, T("cv.mtp." + v), "original")).join("")}</select>
        <div class="cv-sub" id="cv-mtp-pack" hidden><label for="cv-mtp-pick">${esc(T("cv.mtp.pack"))}</label>
          <select id="cv-mtp-pick">${(O.tuned_packs || []).map(p => opt(p.id, p.name || p.id, "")).join("")}</select></div>
        <div class="cv-sub" id="cv-mtp-file" hidden><label for="cv-mtp-path">${esc(T("cv.path"))}</label><input id="cv-mtp-path" type="text" spellcheck="false" autocomplete="off"></div>
        <p class="help">${esc(T("cv.mtp.h"))}</p></div>
      <div class="cv-field"><label for="cv-vision">${esc(T("cv.vision"))}</label>
        <select id="cv-vision">${["auto", "none", "file"].map(v => opt(v, T("cv.vision." + v), "auto")).join("")}</select>
        <div class="cv-sub" id="cv-vision-file" hidden><label for="cv-vision-path">${esc(T("cv.path"))}</label><input id="cv-vision-path" type="text" spellcheck="false" autocomplete="off" list="cv-projectors">
          <datalist id="cv-projectors">${(O.projectors || []).map(p => `<option value="${esc(p.path)}">${esc(p.name)}</option>`).join("")}</datalist></div>
        <p class="help">${esc(T("cv.vision.h"))}</p></div>
      <div class="cv-field"><label for="cv-compat">${esc(T("cv.compat"))}</label>
        <select id="cv-compat">${["auto", "true", "false"].map(v => opt(v, T("cv.compat." + v), "auto")).join("")}</select><p class="help">${esc(T("cv.compat.h"))}</p></div>
      <div class="cv-plan" id="cv-plan" aria-live="polite"></div>
      <div class="cv-actions"><button type="button" class="btn primary" id="cv-go" disabled>${esc(T("cv.go"))}</button></div>
    </form>`;
    syncSubs();
    replan();
  }
  function syncSubs() {
    const show = (id, yes) => { const e = $(id); if (e) e.hidden = !yes; };
    show("#cv-profile-file", $("#cv-profile").value === "file");
    show("#cv-profile-model", $("#cv-profile").value === "model");
    show("#cv-mtp-pack", $("#cv-mtp").value === "tuned_pack");
    show("#cv-mtp-file", $("#cv-mtp").value === "tuned_file");
    show("#cv-vision-file", $("#cv-vision").value === "file");
  }
  function options() {
    const o = {};
    const name = $("#cv-name").value.trim();
    if (name) o.name = name;
    const p = $("#cv-profile").value;
    o.profile = p === "file" ? { mode: p, path: $("#cv-profile-path").value.trim() } : p === "model" ? { mode: p, path: $("#cv-profile-pick").value } : { mode: p };
    const m = $("#cv-mtp").value;
    o.mtp = m === "tuned_pack" ? { mode: m, pack: $("#cv-mtp-pick").value } : m === "tuned_file" ? { mode: m, path: $("#cv-mtp-path").value.trim() } : { mode: m };
    const v = $("#cv-vision").value;
    o.vision = v === "file" ? { mode: v, path: $("#cv-vision-path").value.trim() } : { mode: v };
    const c = $("#cv-compat").value;
    o.compat_bf16 = c === "auto" ? "auto" : c === "true";
    return o;
  }
  function replan(soon) {
    clearTimeout(planTimer);
    planTimer = setTimeout(async () => {
      const box = $("#cv-plan"), go = $("#cv-go");
      if (!box) return;
      const n = ++planSeq;
      const out = $("#cv-out").value.trim();
      const r = await api("POST", "api/models/plan", { id: $("#cv-src").value, out_dir: out || null, options: options() });
      if (n !== planSeq) return;
      if (!r.ok) { plan = null; go.disabled = true; box.innerHTML = `<p class="cv-bad">${esc(errText(r))}</p>`; return; }
      plan = r.data;
      if (!out && plan.out_dir) $("#cv-out").value = plan.out_dir.replace(/\//g, navigator.platform.startsWith("Win") ? "\\" : "/");
      const say = o => (o && (o[LANG] || o.en)) || "";
      box.innerHTML = `<p>${esc(say(plan.summary))}</p>` +
        ((plan.problems || []).length ? `<ul class="cv-bad">${plan.problems.map(p => `<li>${esc(LANG === "it" ? p.text_it : p.text)}</li>`).join("")}</ul>` : "") +
        ((plan.warnings || []).length ? `<ul class="cv-warn">${plan.warnings.map(w => `<li>${esc(say(w))}</li>`).join("")}</ul>` : "") +
        `<p class="dim">${esc(T("cv.steps", plan.steps.map(s => T("cv.step." + s)).join(" → ")))}</p>` +
        `<details><summary>${esc(T("cv.cmd"))}</summary><pre class="cv-cmd">python tools/convert/convert.py ${esc(plan.args.map(a => /\s/.test(a) ? `"${a}"` : a).join(" "))}</pre></details>`;
      go.textContent = T("cv.go.size", gib(plan.estimate_bytes));
      go.disabled = !plan.ok || !!(convJob && convJob.phase === "running");
    }, soon ? 0 : 350);
  }
  function confirmConvert() {
    if (!plan || !plan.ok) return;
    dialog(`<h3 id="cv-dlg-h">${esc(T("cv.confirm.title"))}</h3><p>${esc(T("cv.confirm.body", plan.out_path, gib(plan.estimate_bytes)))}</p>
      <div class="cv-actions"><button type="button" class="btn primary" data-do="convert">${esc(T("cv.go"))}</button><button type="button" class="btn ghost" data-do="close">${esc(T("cv.cancel"))}</button></div>`);
  }
  async function startConvert() {
    const r = await api("POST", "api/models/convert", { id: $("#cv-src").value, out_dir: $("#cv-out").value.trim() || null, token: plan.token, confirm: true, options: options() });
    closeDialog();
    if (r.status === 409 && r.data.plan) { replan(true); return; }
    if (!r.ok) { $("#cv-plan").insertAdjacentHTML("beforeend", `<p class="cv-bad">${esc(errText(r))}</p>`); return; }
    convJob = r.data;
    drawJob();
    poll();
  }

  /* ---- jobs ---- */
  function drawJob() {
    const box = $("#cv-job");
    const c = convJob, t = testJob;
    const parts = [];
    if (c && c.phase && c.phase !== "idle" && c.kind === "convert") {
      const pct = Math.round((c.progress || 0) * 100);
      parts.push(`<div class="cv-body"><b>${esc(T("cv.job.convert", c.name || ""))}</b>
        <p>${esc(c.phase === "running" ? T("cv.job.step", T("cv.step." + (c.step || "convert")), pct) : c.phase === "done" ? T("cv.job.done", c.out_path || "") : c.phase === "failed" ? T("cv.job.failed", c.error || "") : T("cv.job.cancelled"))}</p>
        ${c.phase === "running" ? `<div class="cv-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${pct}%"></i></div>
          <div class="cv-actions"><button type="button" class="btn ghost" data-do="cancel-convert">${esc(T("cv.cancel"))}</button></div>` : ""}
        ${c.phase === "done" && c.out_path ? `<div class="cv-actions"><button type="button" class="btn primary" data-do="test-path" data-path="${esc(c.out_path)}">${esc(T("cv.test.this"))}</button></div>` : ""}</div>`);
    }
    if (t && t.phase && t.phase !== "idle") {
      const pct = Math.round((t.progress || 0) * 100);
      parts.push(`<div class="cv-body"><b>${esc(T("cv.job.test", t.name || ""))}</b>
        <p>${esc(t.phase === "running" ? (t.message ? T("cv.job.testing", t.message, pct) : T("cv.job.testing0", pct)) : t.phase === "done" ? T("cv.job.testdone", (t.stored || []).map(s => T("cv.s." + s)).join(", ") || T("cv.job.none")) : t.phase === "failed" ? T("cv.job.failed", t.error || "") : T("cv.job.cancelled"))}</p>
        ${t.phase === "running" ? `<div class="cv-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><i style="width:${pct}%"></i></div>
          <div class="cv-actions"><button type="button" class="btn ghost" data-do="cancel-test">${esc(T("cv.cancel"))}</button></div>` : ""}</div>`);
    }
    box.hidden = !parts.length;
    box.innerHTML = parts.join("");
    const go = $("#cv-go");
    if (go && plan) go.disabled = !plan.ok || !!(c && c.phase === "running");
  }
  function poll() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      if (document.hidden || root.hidden || root.closest("[hidden]")) { if ((convJob && convJob.phase === "running") || (testJob && testJob.phase === "running")) poll(); return; }
      const [c, t] = await Promise.all([api("GET", "api/models/job"), api("GET", "api/bench/job")]);
      const wasRunning = [convJob, testJob].some(j => j && j.phase === "running");
      if (c.ok) convJob = c.data;
      if (t.ok) testJob = t.data;
      drawJob();
      const running = [convJob, testJob].some(j => j && j.phase === "running");
      if (wasRunning && !running) await refresh();
      if (running) poll();
    }, 1500);
  }

  /* ---- dialogs ---- */
  function dialog(html) { const d = $("#cv-dlg"); $("#cv-dlg-body").innerHTML = html; if (!d.open) d.showModal(); }
  function closeDialog() { const d = $("#cv-dlg"); if (d.open) d.close(); }
  function testDialog(model) {
    const std = O.standard || {};
    const power = O.power_w ? T("cv.power", nfmt(O.power_w, 0)) : "";
    dialog(`<h3 id="cv-dlg-h">${esc(T("cv.test.title", model.name))}</h3>
      <p class="help">${esc(T("cv.test.help", `${std["max-context"] || ""} ${T("cv.tokens")}, KV ${std.kv}, ${T("cv.test.spec", std.spec)}, ${power}`))}</p>
      <fieldset class="cv-field"><legend>${esc(T("cv.test.steps"))}</legend>
        <label class="cv-chk"><input type="checkbox" data-step="memory" checked> ${esc(T("cv.s.memory.h"))}</label>
        <label class="cv-chk"><input type="checkbox" data-step="speed" checked> ${esc(T("cv.s.speed.h"))}</label>
        <div class="cv-sub" id="cv-relax">
          ${[0.5, 0.3, 0.2].map(p => `<label class="cv-chk"><input type="checkbox" data-relax="${p}" checked> ${esc(T("cv.test.relax", p))}</label>`).join("")}
          <label class="cv-chk"><input type="checkbox" id="cv-baseline" checked> ${esc(T("cv.test.baseline"))}</label></div>
        <label class="cv-chk"><input type="checkbox" data-step="image" checked> ${esc(T("cv.s.image.h"))}</label>
        <label class="cv-chk"><input type="checkbox" data-step="quality" checked> ${esc(T("cv.s.quality.h"))}
          <select id="cv-n" aria-label="${esc(T("cv.test.n"))}">${[100, 200, 300].map(n => opt(n, T("cv.test.questions", n), 200)).join("")}</select></label>
      </fieldset>
      <p class="help">${esc(T("cv.test.relax.h"))}</p>
      <p class="cv-bad" id="cv-dlg-err" hidden></p>
      <div class="cv-actions"><button type="button" class="btn primary" data-do="test-start" data-path="${esc(model.path)}">${esc(T("cv.test.start"))}</button><button type="button" class="btn ghost" data-do="close">${esc(T("cv.cancel"))}</button></div>`);
  }
  let testReq = null;
  async function runTest(path, reuse) {
    const d = $("#cv-dlg-body");
    if (!reuse) {
      const steps = [...d.querySelectorAll("[data-step]:checked")].map(e => e.dataset.step);
      const relax = [0, ...[...d.querySelectorAll("[data-relax]:checked")].map(e => Number(e.dataset.relax))];
      testReq = { path, steps, relax: steps.includes("speed") ? relax : [0], n: Number(($("#cv-n") || {}).value) || 200, baseline: !!($("#cv-baseline") || {}).checked };
      if (!steps.length) { fail(T("cv.test.nosteps")); return; }
    }
    const r = await api("POST", "api/bench/run", { ...testReq, reuse: reuse || "ask" });
    if (r.ok) { closeDialog(); testJob = r.data; if (r.data.phase === "done") await refresh(); drawJob(); poll(); return; }
    const err = (r.data && r.data.error) || {};
    if (r.status === 409 && err.code === "reuse") return reuseDialog(path, r.data);
    if (r.status === 409 && err.code === "model_loaded") return loadedDialog(path);
    if (r.status === 422 && r.data.problems) return fail(r.data.problems.map(p => (LANG === "it" ? p.text_it : p.text)).join(" "));
    fail(errText(r));
  }
  function fail(text) { const e = $("#cv-dlg-err"); if (e) { e.hidden = false; e.textContent = text; } else dialog(`<h3>${esc(T("cv.error"))}</h3><p class="cv-bad">${esc(text)}</p><div class="cv-actions"><button type="button" class="btn" data-do="close">${esc(T("cv.close"))}</button></div>`); }
  function reuseDialog(path, body) {
    const rows = Object.entries(body.available).map(([step, v]) => `<div class="cv-reuse"><b>${esc(T("cv.s." + step))}</b>
      <span class="dim">${esc(T("cv.reuse.row", ago(v.at), v.power_w ? T("cv.power", nfmt(v.power_w, 0)) : ""))}</span>
      <div><label><input type="radio" name="cv-r-${step}" value="reuse" checked> ${esc(T("cv.reuse.reuse"))}</label><label><input type="radio" name="cv-r-${step}" value="redo"> ${esc(T("cv.reuse.redo"))}</label></div></div>`).join("");
    dialog(`<h3 id="cv-dlg-h">${esc(T("cv.reuse.title"))}</h3><p>${esc(T("cv.reuse.help"))}</p>${rows}
      <div class="cv-actions"><button type="button" class="btn primary" data-do="reuse-go" data-path="${esc(path)}">${esc(T("cv.reuse.go"))}</button>
      <button type="button" class="btn" data-do="reuse-all" data-path="${esc(path)}">${esc(T("cv.reuse.all"))}</button>
      <button type="button" class="btn" data-do="redo-all" data-path="${esc(path)}">${esc(T("cv.reuse.redoall"))}</button>
      <button type="button" class="btn ghost" data-do="close">${esc(T("cv.cancel"))}</button></div>`);
  }
  function loadedDialog(path) {
    dialog(`<h3 id="cv-dlg-h">${esc(T("cv.loaded.title"))}</h3><p>${esc(T("cv.loaded.body"))}</p>
      <div class="cv-actions"><button type="button" class="btn primary" data-do="unload" data-path="${esc(path)}">${esc(T("cv.unload"))}</button><button type="button" class="btn ghost" data-do="close">${esc(T("cv.cancel"))}</button></div>`);
  }

  /* ---- the list ---- */
  function modelsList() {
    const body = $("#cv-models-body");
    const local = (O.models || []).filter(m => m.kind === "local"), pub = (O.models || []).filter(m => m.kind === "published");
    const row = m => {
      const tests = Object.keys(m.tests || {});
      const last = tests.length ? Math.max(...tests.map(s => m.tests[s].at)) : 0;
      const on = selected.includes(m.id);
      return `<li class="cv-row"><label><input type="checkbox" data-pick="${esc(m.id)}"${on ? " checked" : ""}><span class="cv-sw" style="background:${on ? colorOf(m.id) : "var(--line)"}" aria-hidden="true"></span><span class="cv-name">${esc(m.name)}</span></label>
        <div class="cv-chips"><span class="cv-chip">${esc(m.quant || "?")}</span>
          ${m.kind === "published" ? `<span class="cv-chip">${esc(T("cv.pub.tag"))}</span>` : `<span class="cv-chip" data-on="${last ? 1 : 0}">${esc(last ? T("cv.tested", ago(last)) : T("cv.untested"))}</span>`}
          ${m.kind === "local" ? `<button type="button" class="btn" data-do="test" data-path="${esc(m.path)}">${esc(T("cv.test"))}</button>` : ""}</div></li>`;
    };
    body.innerHTML = `<p class="help">${esc(T("cv.models.h"))}</p>
      ${local.length ? `<ul class="cv-list">${local.map(row).join("")}</ul>` : `<p class="dim">${esc(T("cv.models.none"))}</p>`}
      <details id="cv-pub"${showPub ? " open" : ""}><summary>${esc(T("cv.pub.title"))}</summary><p class="help">${esc(T("cv.pub.note"))}</p><ul class="cv-list">${pub.map(row).join("")}</ul></details>`;
  }

  /* ---- the comparison ---- */
  const metricById = id => (O.metrics || []).find(m => m.id === id);
  const STAR_CHOICES = ["decode", "decode_r05", "prefill", "gsm8k_pct", "top1_pct", "ram_gib", "commit_gib", "vram_gib", "load_s", "size_gib"];
  function legend(ms) {
    return `<ul class="cv-legend" aria-label="${esc(T("cv.legend"))}">${ms.map(m => `<li><svg width="14" height="14" viewBox="-7 -7 14 14" aria-hidden="true">${(window.TadamooCharts.marker || (() => ""))(m.shape, 0, 0, 4.5, m.color, "var(--viz-surface)")}</svg>${esc(m.name)}${m.kind === "published" ? ` <span class="dim">(${esc(T("cv.pub.tag"))})</span>` : ""}</li>`).join("")}</ul>`;
  }
  function compare() {
    const body = $("#cv-compare-body");
    const ms = chosen();
    const tab = (id, text) => `<button type="button" class="btn" data-chart="${id}" aria-pressed="${view === id}">${esc(text)}</button>`;
    let html = `<div class="cv-tabs" role="group" aria-label="${esc(T("cv.chart.pick"))}">${tab("radar", T("cv.chart.radar"))}${tab("bars", T("cv.chart.bars"))}${tab("points", T("cv.chart.points"))}${tab("table", T("cv.chart.table"))}</div>`;
    if (!ms.length) { body.innerHTML = html + `<p class="dim">${esc(T("cv.pickmore"))}</p>`; return; }
    html += legend(ms);
    if (view === "radar") html += radarView(ms);
    else if (view === "bars") html += barsView(ms);
    else if (view === "points") html += pointsView(ms);
    else html += tableView(ms);
    body.innerHTML = html;
  }
  function radarView(ms) {
    const shown = ms.slice(0, MAX_STAR);
    const metrics = axes.map(metricById).filter(Boolean);
    const out = C.radar({ models: shown, metrics, label: axisLabel, better: m => T(m.better === "lower" ? "cv.better.lowshort" : "cv.better.highshort"), title: T("cv.chart.radar") });
    const choices = STAR_CHOICES.map(metricById).filter(Boolean);
    return `<fieldset class="cv-field"><legend>${esc(T("cv.axes"))}</legend><div class="cv-ctl">${choices.map(m => `<label class="cv-chk"><input type="checkbox" data-axis="${m.id}"${axes.includes(m.id) ? " checked" : ""}> ${esc(metricLabel(m))}</label>`).join("")}</div></fieldset>` +
      (ms.length > MAX_STAR ? `<p class="help">${esc(T("cv.radar.max", MAX_STAR))}</p>` : "") +
      (out.enough ? `<div class="cv-chart">${out.svg}</div>` : `<p class="dim">${esc(T("cv.radar.few"))}</p>`) +
      (out.skipped.length ? `<p class="help">${esc(T("cv.radar.skipped", out.skipped.map(metricLabel).join(", ")))}</p>` : "") +
      `<p class="help">${esc(T("cv.radar.note"))}</p>`;
  }
  function barsView(ms) {
    const groups = ["all", "weight", "speed", "quality"];
    const shown = (O.metrics || []).filter(m => (group === "all" || m.group === group) && ms.some(x => typeof (x.figures || {})[m.id] === "number"));
    return `<div class="cv-ctl"><label>${esc(T("cv.group"))}<select id="cv-group">${groups.map(g => opt(g, T("cv.group." + g), group)).join("")}</select></label></div>` +
      (ms.some(m => m.kind === "published") ? `<p class="help">${esc(T("cv.bars.pub"))}</p>` : "") +
      (shown.length ? `<div class="cv-panels">${shown.map(m => `<div class="cv-panel"><h4>${esc(metricLabel(m))} <small>${esc(m.unit ? "(" + m.unit + ") · " : "")}${esc(better(m))}</small></h4>${C.bars({ models: ms, metric: m, label: metricLabel })}</div>`).join("")}</div>` : `<p class="dim">${esc(T("cv.nodata"))}</p>`);
  }
  function pointsView(ms) {
    const x = metricById(pts.x), y = metricById(pts.y), size = pts.size ? metricById(pts.size) : null;
    const pick = (id, cur, none) => `<select id="${id}">${none ? opt("", T("cv.points.none"), cur) : ""}${(O.metrics || []).map(m => opt(m.id, metricLabel(m), cur)).join("")}</select>`;
    const out = x && y ? C.scatter({ models: ms, x, y, size, label: metricLabel }) : { svg: "", missing: ms.length };
    return `<div class="cv-ctl"><label>${esc(T("cv.points.x"))}${pick("cv-px", pts.x)}</label><label>${esc(T("cv.points.y"))}${pick("cv-py", pts.y)}</label><label>${esc(T("cv.points.size"))}${pick("cv-ps", pts.size, true)}</label></div>` +
      (out.svg ? `<div class="cv-chart">${out.svg}</div>` : `<p class="dim">${esc(T("cv.points.empty"))}</p>`) +
      (out.missing ? `<p class="help">${esc(T("cv.points.missing", out.missing))}</p>` : "");
  }
  function tableView(ms) {
    const fmt = (v, m) => typeof v === "number" ? C.format(v, "") : (v === true ? "✓" : "—");
    const rows = [];
    ["weight", "speed", "quality"].forEach(g => {
      const list = (O.metrics || []).filter(m => m.group === g && ms.some(x => (x.figures || {})[m.id] != null));
      if (!list.length) return;
      rows.push(`<tr class="cv-grp"><th colspan="${ms.length + 1}" scope="colgroup">${esc(T("cv.group." + g))}</th></tr>`);
      list.forEach(m => {
        const vals = ms.map(x => (x.figures || {})[m.id]);
        const known = vals.filter(v => typeof v === "number");
        const best = known.length > 1 ? (m.better === "lower" ? Math.min(...known) : Math.max(...known)) : null;
        rows.push(`<tr><th scope="row">${esc(metricLabel(m))}${m.unit ? ` <span class="dim">${esc(m.unit)}</span>` : ""}</th>${ms.map((x, i) => `<td${vals[i] === best && best != null ? ' data-best="1"' : ""}${x.kind === "published" ? ' data-ref="1"' : ""}>${esc(fmt(vals[i], m))}</td>`).join("")}</tr>`);
      });
    });
    return `<div class="cv-table-wrap"><table class="cv-table"><thead><tr><th scope="col">${esc(T("cv.table.metric"))}</th>${ms.map(m => `<th scope="col"><span class="cv-sw" style="background:${m.color}" aria-hidden="true"></span> ${esc(m.name)}${m.kind === "published" ? `<br><span class="dim">${esc(T("cv.pub.tag"))}</span>` : ""}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div><p class="help">${esc(T("cv.table.best"))}</p>`;
  }

  /* ---- data ---- */
  async function refresh() {
    const r = await api("GET", "api/bench/overview");
    if (!r.ok) { $("#cv-convert-body").innerHTML = `<p class="cv-bad">${esc(errText(r))}</p>`; return; }
    O = r.data;
    selected = selected.filter(id => (O.models || []).some(m => m.id === id));
    if (O.job) testJob = O.job;
    const keep = $("#cv-src") && $("#cv-form") ? $("#cv-form") : null;
    if (!keep) convertForm();
    modelsList();
    compare();
    drawJob();
    if ([convJob, testJob].some(j => j && j.phase === "running")) poll();
    else { const c = await api("GET", "api/models/job"); if (c.ok) { convJob = c.data; if (c.data.phase === "running") { drawJob(); poll(); } } }
  }

  /* ---- events ---- */
  on(root, "change", e => {
    const t = e.target;
    if (t.matches("[data-pick]")) {
      const id = t.dataset.pick;
      if (t.checked) { if (selected.length >= MAX_PICK) { t.checked = false; return; } selected.push(id); } else selected = selected.filter(x => x !== id);
      store.set("selected", selected);
      modelsList(); compare();
    } else if (t.matches("[data-axis]")) {
      axes = [...root.querySelectorAll("[data-axis]:checked")].map(e2 => e2.dataset.axis);
      store.set("axes", axes); compare();
    } else if (t.id === "cv-group") { group = t.value; store.set("group", group); compare(); }
    else if (t.id === "cv-px" || t.id === "cv-py" || t.id === "cv-ps") { pts = { x: $("#cv-px").value, y: $("#cv-py").value, size: $("#cv-ps").value }; store.set("pts", pts); compare(); }
    else if (t.closest("#cv-form")) { syncSubs(); replan(); }
    else if (t.closest("#cv-pub")) { /* the details element toggles on its own */ }
  });
  on(root, "input", e => { if (e.target.closest("#cv-form") && e.target.matches("input")) replan(); });
  on(root, "toggle", e => { if (e.target.id === "cv-pub") { showPub = e.target.open; store.set("pub", showPub); } }, true);
  on(root, "click", async e => {
    const v = e.target.closest("[data-chart]");
    if (v) { view = v.dataset.chart; store.set("view", view); compare(); return; }
    const b = e.target.closest("[data-do]");
    if (!b) return;
    const what = b.dataset.do, path = b.dataset.path;
    if (b.id === "cv-go") return;
    if (what === "close") closeDialog();
    else if (what === "convert") await startConvert();
    else if (what === "cancel-convert") { await api("POST", "api/models/cancel", {}); poll(); }
    else if (what === "cancel-test") { await api("POST", "api/bench/cancel", {}); poll(); }
    else if (what === "test") testDialog((O.models || []).find(m => m.path === path) || { name: path, path });
    else if (what === "test-path") testDialog({ name: path.split(/[\\/]/).pop(), path });
    else if (what === "test-start") await runTest(path, null);
    else if (what === "reuse-all") await runTest(path, "reuse");
    else if (what === "redo-all") await runTest(path, "redo");
    else if (what === "reuse-go") {
      const per = {};
      $("#cv-dlg-body").querySelectorAll("[name^=cv-r-]:checked").forEach(r => { per[r.name.slice(5)] = r.value; });
      await runTest(path, per);
    } else if (what === "unload") {
      const r = await api("POST", "api/bench/unload", {});
      if (r.ok || (r.data && r.data.status === "not loaded")) await runTest(path, "ask"); else fail(errText(r));
    }
  });
  on($("#cv-go") ? root : root, "click", e => { if (e.target.id === "cv-go") confirmConvert(); });

  // the hover text: one tooltip for every mark, the same text for the keyboard focus
  const tip = () => $("#cv-tip");
  const showTip = (el, x, y) => {
    const t = tip(); t.textContent = el.dataset.tip; t.hidden = false;
    const r = t.getBoundingClientRect();
    t.style.left = Math.max(8, Math.min(innerWidth - r.width - 8, x + 14)) + "px";
    t.style.top = Math.max(8, Math.min(innerHeight - r.height - 8, y + 14)) + "px";
  };
  on(root, "pointermove", e => { const p = e.target.closest("[data-tip]"); if (p) showTip(p, e.clientX, e.clientY); else tip().hidden = true; });
  on(root, "pointerleave", () => { tip().hidden = true; });
  on(root, "focusin", e => { const p = e.target.closest("[data-tip]"); if (p) { const r = p.getBoundingClientRect(); showTip(p, r.left + r.width / 2, r.top + r.height / 2); } });
  on(root, "focusout", () => { tip().hidden = true; });
  on(document, "keydown", e => { if (e.key === "Escape") { const t = $("#cv-tip"); if (t) t.hidden = true; } });

  skeleton();
  document.addEventListener("basalt:models-sub", e => { if (e.detail === "converter") refresh(); });
  window.addEventListener("hashchange", () => { /* the sub-tab is kept by providers.js */ });
})();
