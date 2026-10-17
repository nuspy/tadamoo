"use strict";
/* Loader: the engine settings by hand or from a saved load profile, with the memory they need next to what this PC
   has free now and the speed they should give (both from the server: GET /api/loader, POST /api/loader/plan). Load
   writes the config file and restarts the engine inside the server (POST /api/loader/load). Every value decided when
   the model loads is here, with the load profiles and the default one (★); the Settings view only shows what runs now
   and the profile it matches, and its button opens this view on that profile. */
(() => {
  const FIELDS = [
    { group: "reserve", name: "engine.vram-reserve-mib", kind: "reserve" },
    { group: "context", name: "engine.max-context", kind: "int", min: 256, max: 262144, step: 1024,
      list: [4096, 8192, 16384, 32768, 65536, 131072, 262144] },
    { group: "context", name: "engine.kv", kind: "choice", options: ["fp16", "int8", "k8v4", "q4_0"] },
    { group: "context", name: "server.parallel", kind: "int", min: 1, max: 8, step: 1 },
    { group: "context", name: "server.slot-full-context", kind: "bool" },
    { group: "speed", name: "engine.prefill", kind: "text", list: ["auto", "auto:8192", "auto:16384", "auto:32768", "2048", "512"] },
    { group: "speed", name: "mtp", kind: "bool", virtual: true },
    { group: "speed", name: "engine.spec", kind: "int", min: 2, max: 8, step: 1 },
    { group: "speed", name: "engine.spec-min-p", kind: "float", min: 0, max: 1, step: 0.05 },
    { group: "memory", name: "engine.expert-cache", kind: "text", list: ["auto"] },
    { group: "memory", name: "engine.resident-experts", kind: "bool" },
    { group: "memory", name: "engine.ple-io", kind: "choice", options: ["direct", "mmap", "ram"] },
    { group: "memory", name: "engine.pool-workers", kind: "int", min: 1, max: 256, step: 1 },
    { group: "server", name: "server.port", kind: "int", min: 1, max: 65535, step: 1 },
    { group: "server", name: "server.vision", kind: "choice", options: ["off", "dynamic", "on", "peer", "cpu"] },
  ];
  const fid = name => "lf-" + name.replace(/[^a-z0-9]+/gi, "-");
  const GiBn = 2 ** 30;
  const gib = n => nfmt(n / GiBn, 1);
  let INFO = null, PROF = null, seq = 0, timer = 0, poll = 0;
  const fallback = { "server.vision": "off", "engine.kv": "fp16", "engine.ple-io": "direct" };

  /* ---- the form ---- */
  function build() {
    const box = $("#ld-fields");
    const groups = [...new Set(FIELDS.map(f => f.group))];
    box.replaceChildren(...groups.map(g => {
      const fs = document.createElement("fieldset");
      fs.className = "ldgroup";
      fs.innerHTML = `<legend>${esc(T("ld.g." + g))}</legend><div class="fields"></div>`;
      const grid = fs.querySelector(".fields");
      for (const f of FIELDS.filter(x => x.group === g)) {
        const id = fid(f.name), help = `${id}-h`, key = f.name === "mtp" ? "mtp" : f.name;
        const d = document.createElement("div");
        d.className = "field" + (f.kind === "bool" ? " check" : "");
        d.dataset.name = f.name;
        let input;
        if (f.kind === "reserve") {
          d.className = "field rsv";
          d.innerHTML = reserveHtml("lf", id, help);
          grid.append(d);
          continue;
        }
        if (f.kind === "bool") {
          input = `<input id="${id}" type="checkbox" aria-describedby="${help}">`;
          d.innerHTML = `<label for="${id}">${input}<span>${esc(T("f." + key))}</span></label>`;
        } else if (f.kind === "choice") {
          const opts = f.options.map(o => `<option value="${o}">${o}</option>`).join("");
          d.innerHTML = `<label for="${id}">${esc(T("f." + key))}</label><select id="${id}" aria-describedby="${help}">` +
            `<option value="" data-default></option>${opts}</select>`;
        } else {
          const list = f.list ? `<datalist id="${id}-l">${f.list.map(o => `<option value="${o}"></option>`).join("")}</datalist>` : "";
          const num = f.kind !== "text";
          d.innerHTML = `<label for="${id}">${esc(T("f." + key))}</label>` +
            `<input id="${id}" type="${num ? "number" : "text"}" ${num ? `inputmode="${f.kind === "int" ? "numeric" : "decimal"}" min="${f.min}" max="${f.max}" step="${f.step}"` : 'autocapitalize="off" spellcheck="false"'}` +
            `${f.list ? ` list="${id}-l"` : ""} aria-describedby="${help}">${list}`;
        }
        d.insertAdjacentHTML("beforeend", `<p class="help" id="${help}">${esc(T("h." + key))}</p>`);
        grid.append(d);
      }
      return fs;
    }));
  }
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const el = name => $("#" + fid(name));

  /* ---- the VRAM left free: a slider and a number in GB (GiB, as Windows shows them), engine.vram-reserve-mib ---- */
  function reserveHtml(prefix, id, help) {
    return `<label for="${id}" class="${prefix === "lf" ? "sr" : ""}">${esc(T("rsv.title"))}</label>
      <div class="rsv-row"><input type="range" id="${prefix}-rsv-range" min="0.25" max="32" step="0.25" aria-label="${esc(T("rsv.title"))}" aria-describedby="${help}">
        <span class="rsv-num"><input id="${id}" type="number" inputmode="decimal" min="0.25" max="64" step="0.25" aria-describedby="${help}"><span aria-hidden="true">GB</span></span></div>
      <p class="help" id="${help}">${esc(T("rsv.help"))}</p>
      <div class="rsv-live" id="${prefix}-rsv-live" aria-live="polite"></div>`;
  }
  function reserveMax() {
    const v = INFO && INFO.reserve && INFO.reserve.vram_total;
    return v ? Math.max(4, Math.min(64, Math.floor(v / GiBn))) : 32;
  }
  function syncReserve(prefix, num, from) {
    const range = $(`#${prefix}-rsv-range`);
    if (!range || !num) return;
    range.max = String(reserveMax());
    if (from === "range") num.value = range.value;
    else range.value = num.value === "" ? String((((INFO && INFO.defaults) || {})["engine.vram-reserve-mib"] || 700) / 1024) : num.value;
  }
  const toMiB = raw => { const n = Number(String(raw).replace(",", ".")); return raw === "" ? null : Number.isFinite(n) ? Math.round(n * 1024) : raw; };
  const toGB = mib => mib == null ? "" : String(+(Number(mib) / 1024).toFixed(2));
  function reserveLive(p, target) {
    const box = $(target);
    if (!box || !p) return;
    const v = p.vram, s = p.speed || {}, c = p.commit;
    const cache = ((v.items || []).find(i => i.key === "experts") || {}).bytes || 0;
    const pct = v.expert_slots / Math.max(1, v.expert_slots_total) * 100;
    const auto = ["auto", 0, "0", null, undefined].includes((p.values || {})["engine.expert-cache"]);
    const procs = ((p.gpu || {}).processes || []).map(x => x.name || String(x.pid));
    const counts = procs.reduce((a, n) => (a[n] = (a[n] || 0) + 1, a), {});
    const list = Object.entries(counts).map(([n, k]) => k > 1 ? `${n} ×${k}` : n).join(", ");
    box.innerHTML = `<ul class="rsv-figs">
        <li><span>${esc(T("rsv.experts"))}</span><b>${gib(cache)} GB</b></li>
        <li><span>${esc(T("rsv.slots"))}</span><b>${esc(T("rsv.slots.v", nfmt(v.expert_slots), nfmt(v.expert_slots_total), nfmt(pct, 0)))}</b></li>
        <li><span>${esc(T("rsv.hit"))}</span><b>${nfmt((s.hit_rate || 0) * 100, 1)}%</b></li>
        <li><span>${esc(T("rsv.speed"))}</span><b>${nfmt(s.decode, 0)} · ${nfmt(s.prefill, 0)} tok/s</b></li>
        <li><span>${esc(T("rsv.commit"))}</span><b>${gib(c.need)} GB</b></li></ul>
      <p class="help">${esc(auto ? T("rsv.trade") : T("rsv.fixed"))}</p>
      ${(p.warnings || []).map(x => `<p class="rsv-warn" role="alert">⚠ ${esc(LANG === "it" ? x.text_it : x.text)}</p>`).join("")}
      ${list ? `<p class="help">${esc(T("rsv.procs", list))}</p>` : ""}`;
  }

  function defaults() {
    const d = (INFO && INFO.defaults) || {};
    FIELDS.forEach(f => {
      const e = el(f.name);
      if (!e || f.virtual) return;
      const v = d[f.name];
      if (f.kind === "choice") e.querySelector("[data-default]").textContent = T("opt.default", v ?? fallback[f.name] ?? "—");
      else if (f.kind === "reserve") e.placeholder = toGB(v);
      else if (f.kind !== "bool") e.placeholder = v == null ? "—" : String(v);
    });
  }

  function readForm() {
    const out = {};
    for (const f of FIELDS) {
      if (f.virtual) continue;
      const e = el(f.name);
      if (f.kind === "bool") { out[f.name] = e.checked; continue; }
      if (f.kind === "reserve") { out[f.name] = toMiB(e.value.trim()); continue; }
      const raw = e.value.trim();
      if (raw === "") { out[f.name] = null; continue; }
      if (f.kind === "int" || f.kind === "float") { const n = Number(raw); out[f.name] = Number.isFinite(n) ? n : raw; continue; }
      out[f.name] = /^\d+$/.test(raw) ? Number(raw) : raw;
    }
    if (!el("mtp").checked) out["engine.spec"] = 0;
    else if (out["engine.spec"] == null || out["engine.spec"] < 2) out["engine.spec"] = 7;
    return out;
  }
  function writeForm(values) {
    const v = values || {};
    for (const f of FIELDS) {
      if (f.virtual) continue;
      const e = el(f.name), x = v[f.name];
      if (f.kind === "bool") e.checked = x === true;
      else if (f.kind === "reserve") { e.value = toGB(x); syncReserve("lf", e); }
      else e.value = x == null ? "" : String(x);
    }
    const spec = Number(v["engine.spec"] ?? 0);
    el("mtp").checked = spec >= 2;
    if (spec < 2) el("engine.spec").value = "";
    syncMtp();
  }
  function syncMtp() {
    const on = el("mtp").checked;
    el("engine.spec").disabled = !on;
    el("engine.spec-min-p").disabled = !on;
    const par = Number(el("server.parallel").value || 1);
    el("server.slot-full-context").disabled = par < 2;
  }

  /* ---- the prediction: resources, speed, the Load button ---- */
  function bar(kind, need, have, keep, items, notes, share) {
    const known = typeof have === "number";
    const ok = !known || need <= have - (keep || 0);
    const top = Math.max(need + (keep || 0), known ? have : need, 1);
    const pct = x => `${Math.min(100, x / top * 100).toFixed(1)}%`;
    const lis = (items || []).filter(i => i.bytes > 0).map(i =>
      `<li><span>${esc(T("it." + (kind === "ram" && i.key === "experts" ? "experts_ram" : i.key)))}</span><b>${gib(i.bytes)} GiB</b></li>`).join("");
    return `<div class="res" data-ok="${ok}">
      <div class="res-h"><b>${esc(T("pr." + kind))}</b><span class="fit">${ok ? "✓ " + esc(T("pr.fits")) : "✕ " + esc(T("pr.nofit"))}</span></div>
      <div class="res-n"><span>${esc(T("pr.need", gib(need)))}</span><span>${known ? esc(T("pr.have", gib(have))) : esc(T("pr.unknown"))}</span></div>
      <div class="resbar" aria-hidden="true"><i style="width:${pct(need)}"></i>${known && keep ? `<u style="left:${pct(have - keep)}"></u>` : ""}${known ? `<s style="left:${pct(have)}"></s>` : ""}</div>
      ${keep ? `<p class="res-note">${esc(T("pr.keep", gib(keep), nfmt((share ?? 0.15) * 100, 0) + "%"))}</p>` : ""}
      ${(notes || []).map(n => `<p class="res-note">${esc(n)}</p>`).join("")}
      ${lis ? `<ul class="res-items">${lis}</ul>` : ""}
    </div>`;
  }
  function when(iso) {
    try { return new Date(iso).toLocaleString(LANG === "it" ? "it-IT" : "en-US", { dateStyle: "medium", timeStyle: "short" }); } catch (e) { return iso; }
  }
  function sideHtml(p) {
    const v = p.vram, r = p.ram, c = p.commit, s = p.speed || {}, h = p.engine_holds || {};
    const holds = (h.ram || h.commit) ? [T("pr.holds", gib((h.commit || 0) + (h.vram || 0)))] : [];
    const vnotes = [T("pr.reserve", gib(v.reserve)), T("pr.slots", nfmt(v.expert_slots), nfmt(v.expert_slots_total),
      nfmt(v.expert_slots / Math.max(1, v.expert_slots_total) * 100, 0))];
    const src = s.source || {};
    const srcText = src.kind === "calibration" ? T(src.exact ? "sp.cal.exact" : "sp.cal", when(src.at)) : T("sp.seed");
    const problems = (p.problems || []).map(x => LANG === "it" ? x.text_it : x.text);
    const editable = INFO && INFO.editable, busy = INFO && INFO.status && ["waiting", "stopping", "starting", "calibrating"].includes(INFO.status.phase);
    const why = !editable ? T("ld.ro." + ((INFO && INFO.reason) || "no_config")) : busy ? T("ld.busy") : problems.length ? T("ld.refused", problems.join(" ")) : "";
    const label = T("ld.load");
    return `<section class="sec ldres" aria-label="${esc(T("pr.title"))}">
        <div class="sh"><b>${esc(T("pr.title"))}</b></div>
        <div class="pad">
          ${bar("vram", v.need + v.reserve, v.available, 0, v.items, vnotes)}
          ${bar("ram", r.need, r.available, r.keep, r.items, holds, r.keep_share)}
          ${bar("commit", c.need, c.available, c.keep, [], [T("pr.commit.parts", gib(c.ram_part), gib(c.vram_part))], c.keep_share)}
        </div>
      </section>
      <section class="sec ldspeed" aria-label="${esc(T("sp.title"))}">
        <div class="sh"><b>${esc(T("sp.title"))}</b></div>
        <dl class="kv spd">
          <dt>${esc(T("sp.decode"))}</dt><dd><b>${nfmt(s.decode, 0)}</b> tok/s</dd>
          <dt>${esc(T("sp.reasoning"))}</dt><dd><b>${nfmt(s.decode_reasoning, 0)}</b> tok/s${s.decode_reasoning_visible ? ` · ${esc(T("sp.visible", nfmt(s.decode_reasoning_visible, 0)))}` : ""}</dd>
          <dt>${esc(T("sp.prefill"))}</dt><dd><b>${nfmt(s.prefill, 0)}</b> tok/s · ${nfmt(s.prefill_chunk)}</dd>
        </dl>
        <p class="setnote">${esc(srcText)} · ${esc(T("sp.hit", nfmt((s.hit_rate || 0) * 100, 1)))}</p>
        <div class="setactions"><p class="setstatus">${esc(T("sp.calibrate.help"))}</p>
          <button type="button" class="btn" data-act="calibrate" ${editable && !busy ? "" : "disabled"}>${esc(T("sp.calibrate"))}</button></div>
      </section>
      <div class="sec ldgo">
        <p class="ldwhy" role="status" aria-live="polite" ${why ? "" : "hidden"}>${esc(why)}</p>
        <p class="ldstate setstatus" role="status" aria-live="polite"></p>
        <div class="setactions"><button type="button" class="btn primary" data-act="load" ${why ? "disabled" : ""}>${esc(label)}</button></div>
      </div>`;
  }
  function renderSide(target, p) {
    const box = $(target);
    box.innerHTML = sideHtml(p);
    sticky(p);
    reserveLive(p, "#lf-rsv-live");
    showStatus();
  }
  const modelNow = () => (window.BasaltModels && BasaltModels.selected()) || null;
  // on a phone the prediction is far below the fields: a summary bar stays at the bottom while editing
  function sticky(p) {
    const ok = k => !(p.problems || []).some(x => x.code === k || (k === "ram" && x.code === "ple_ram"));
    const part = (k, label) => `<span data-ok="${ok(k)}">${ok(k) ? "✓" : "✕"} ${esc(label)}</span>`;
    const go = $("#ld-side [data-act=load]");
    $("#ld-sticky").innerHTML = `<span class="stk-parts">${part("vram", T("pr.vram"))}${part("ram", T("pr.ram"))}${part("commit", "Commit")}` +
      `<span>~${nfmt((p.speed || {}).decode, 0)} tok/s</span></span>` +
      `<a class="btn ${go && !go.disabled ? "primary" : "ghost"}" href="#ld-side">${esc(go && !go.disabled ? T("ld.load") : T("pr.nofit"))}</a>`;
  }

  async function plan(values, model) {
    const r = await fetch("api/loader/plan", { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ values, model: model === undefined ? modelNow() : model }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((j.error && j.error.message) || `HTTP ${r.status}`);
    return j;
  }
  function replan() {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const n = ++seq;
      try {
        const p = await plan(readForm());
        if (n === seq) { renderSide("#ld-side", p, "loader"); markField(""); }
      } catch (e) {
        if (n !== seq) return;
        const name = FIELDS.find(f => e.message.startsWith(f.name + ":"));
        markField(name ? name.name : "", e.message);
      }
    }, 200);
  }
  function markField(name, msg) {
    $$("#ld-fields .ferr").forEach(x => x.remove());
    $$("#ld-fields [aria-invalid]").forEach(x => x.removeAttribute("aria-invalid"));
    if (!msg) return;
    const box = name ? $(`#ld-fields .field[data-name="${name}"]`) : null;
    const p = document.createElement("p"); p.className = "ferr"; p.textContent = msg; p.setAttribute("role", "alert");
    if (box) { box.append(p); el(name).setAttribute("aria-invalid", "true"); } else $("#ld-fields").prepend(p);
  }

  /* ---- load, calibrate and their progress ---- */
  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) askKey();
    return { r, j };
  }
  async function load(values, target, mode, model) {
    const m = model || modelNow();
    const { r, j } = await post("api/loader/load", m ? { values, model: m } : { values });
    if (r.status === 422 && j.plan) { renderSide(target, j.plan, mode); return; }
    if (!r.ok) { say(T("ld.phase.failed", (j.error && j.error.message) || `HTTP ${r.status}`), "err"); return; }
    if ((j.notes || []).includes("model_restart")) { say(T("ld.model.relaunch", BasaltModels.name(j.model)), "ok"); waitRestart(); return; }
    if ((j.notes || []).includes("model_on_restart")) { say(T("ld.model.restart", BasaltModels.name(j.model)), "ok"); return; }
    if ((j.notes || []).includes("port_on_restart")) say(T("ld.port.note"));
    INFO.status = { phase: "waiting" };
    watch();
  }
  // the server ends and its owner starts it again with the new model: reload the page once it answers again
  function waitRestart() {
    let down = false;
    const t0 = Date.now();
    const tick = async () => {
      let up = false;
      try { up = (await fetch("health", { cache: "no-store" })).ok; } catch (e) { up = false; }
      if (!up) down = true;
      if (up && (down || Date.now() - t0 > 15000)) { location.hash = "#loader"; location.reload(); return; }
      setTimeout(tick, 1000);
    };
    setTimeout(tick, 800);
  }
  async function calibrate() {
    const { r, j } = await post("api/loader/calibrate", {});
    if (!r.ok) { say(T("ld.phase.failed", (j.error && j.error.message) || `HTTP ${r.status}`), "err"); return; }
    INFO.status = { phase: "calibrating" };
    watch();
  }
  function say(text, kind) {
    $$(".ldstate").forEach(e => { e.textContent = text; e.dataset.kind = kind || ""; });
  }
  function showStatus() {
    const st = (INFO && INFO.status) || {};
    const ph = st.phase || "idle";
    if (ph === "idle") return;
    const text = ph === "calibrating" ? T("ld.phase.calibrating", T("ld.step." + (st.step || "decode")))
      : ph === "failed" ? T("ld.phase.failed", st.error || "") : T("ld.phase." + ph);
    say(text, ph === "failed" ? "err" : ph === "ready" ? "ok" : "");
    $$("[data-act=load], [data-act=calibrate]").forEach(b => { b.disabled = b.disabled || ["waiting", "stopping", "starting", "calibrating"].includes(ph); });
  }
  function watch() {
    clearTimeout(poll);
    const tick = async () => {
      try {
        const st = await get("api/loader/status");
        INFO.status = st;
        showStatus();
        if (["waiting", "stopping", "starting", "calibrating"].includes(st.phase)) { poll = setTimeout(tick, 600); return; }
        await refresh();
        showStatus();
      } catch (e) { poll = setTimeout(tick, 1500); }
    };
    showStatus();
    poll = setTimeout(tick, 300);
  }

  /* ---- profiles ---- */
  function profileLabel(p) {
    const s = p.summary || {};
    const parts = [s.text];
    if (s.model) parts.unshift(s.model + (s.model_missing ? " (?)" : ""));
    if (s.vram_gb != null) parts.push(`VRAM ~${nfmt(s.vram_gb, 1)} GB`);
    if (s.tok_s != null) parts.push(`~${nfmt(s.tok_s)} tok/s`);
    if (s.port && s.port !== 7232) parts.push(`:${s.port}`);
    const tags = [p.builtin ? T("ld.profile.builtin") : "", p.default ? T("ld.profile.isdefault") : ""].filter(Boolean);
    return `${p.name}${tags.length ? ` (${tags.join(", ")})` : ""} — ${parts.join(" · ")}`;
  }
  function headlessCmd(name) {
    return `powershell -ExecutionPolicy Bypass -File Avvia-Basalt.ps1 -server --config "${name.replace(/"/g, "")}"`;
  }
  function profileBar(target) {
    const box = $(target);
    if (!PROF) { box.replaceChildren(); return; }
    const id = target.slice(1) + "-sel", sel = box.querySelector("select");
    const keep = sel ? sel.value : PROF.default;
    const editable = INFO && INFO.editable;
    box.innerHTML = `
      <div class="pfrow"><label for="${id}">${esc(T("ld.profile"))}</label>
        <select id="${id}">${PROF.load.map(p =>
          `<option value="${esc(p.name)}">${esc(profileLabel(p))}</option>`).join("")}</select>
        <button type="button" class="btn" data-pf="use">${esc(T("ld.profile.apply"))}</button></div>
      <div class="pfrow wrap">
        <input type="text" maxlength="60" autocomplete="off" aria-label="${esc(T("ld.profile.name"))}" placeholder="${esc(T("ld.profile.name"))}" data-pf-name>
        <button type="button" class="btn" data-pf="save" ${editable ? "" : "disabled"}>${esc(T("ld.profile.save"))}</button>
        <button type="button" class="btn ghost" data-pf="rename" ${editable ? "" : "disabled"}>${esc(T("ld.profile.rename"))}</button>
        <button type="button" class="btn ghost" data-pf="delete" ${editable ? "" : "disabled"}>${esc(T("ld.profile.delete"))}</button>
        <button type="button" class="btn ghost" data-pf="default" ${editable ? "" : "disabled"}>${esc(T("ld.profile.default"))}</button>
        <a class="btn ghost" href="api/profiles/export?kind=load" download="basalt-load-profiles.json">${esc(T("ld.profile.export"))}</a>
        <label class="btn ghost filebtn">${esc(T("ld.profile.import"))}<input type="file" accept="application/json,.json" data-pf="import" ${editable ? "" : "disabled"}></label>
        <button type="button" class="btn" data-pf="safe">${esc(T("ld.safe"))}</button>
        <button type="button" class="btn" data-pf="machine" ${PROF.load.some(p => p.name === PROF.machine) ? "" : "disabled"}>${esc(T("ld.machine"))}</button>
        <button type="button" class="btn primary" data-pf="wizard">${esc(T("ld.wizard"))}</button></div>
      <div class="pfrow wrap headless"><span>${esc(T("ld.headless"))}:</span><code data-pf-cmd></code>
        <button type="button" class="btn ghost" data-pf="copy">${esc(T("ld.headless.copy"))}</button></div>
      <p class="setnote">${esc(T("ld.headless.help"))}</p>
      <p class="setstatus pfmsg" role="status" aria-live="polite"></p>`;
    const s = box.querySelector("select");
    s.value = [...s.options].some(o => o.value === keep) ? keep : s.options[0].value;
    const cmd = () => { box.querySelector("[data-pf-cmd]").textContent = headlessCmd(s.value || PROF.default); };
    cmd();
    s.onchange = cmd;
  }
  function pfSay(box, text, kind) { const m = box.querySelector(".pfmsg"); if (m) { m.textContent = text; m.dataset.kind = kind || ""; } }
  async function profileAction(box, act) {
    const s = box.querySelector("select"), name = s.value, input = box.querySelector("[data-pf-name]");
    const typed = input ? input.value.trim() : "";
    const prof = PROF.load.find(p => p.name === name);
    const done = async (key, arg) => { await refreshProfiles(); pfSay(box, T(key, arg), "ok"); };
    const fail = j => pfSay(box, (j.error && j.error.message) || "error", "err");
    if (act === "use" && prof) {
      writeForm(prof.values); syncMtp();
      if (prof.model) BasaltModels.select(prof.model);
      replan(); pfSay(box, T("ld.profile.used", name), "ok"); return;
    }
    if (act === "safe") {
      writeForm({ ...INFO.safe, "server.port": Number(el("server.port").value) || INFO.safe["server.port"] || 7232 });
      syncMtp(); replan(); $("#ld-safe-why").open = true; pfSay(box, T("ld.safe.done"), "ok"); return;
    }
    if (act === "machine") { applyMachine(box); return; }
    if (act === "wizard") { if (window.BasaltWizard) BasaltWizard.open(); return; }
    if (act === "copy") {
      const t = box.querySelector("[data-pf-cmd]").textContent;
      if (navigator.clipboard) navigator.clipboard.writeText(t).then(() => pfSay(box, T("ld.headless.copied"), "ok"), () => {});
      return;
    }
    let body = null;
    if (act === "save") { if (!typed) { pfSay(box, T("cs.preset.noname"), "err"); input.focus(); return; } body = { action: "save", kind: "load", name: typed, values: readForm(), model: modelNow() }; }
    if (act === "rename") { if (!typed) { pfSay(box, T("cs.preset.noname"), "err"); input.focus(); return; } body = { action: "rename", kind: "load", name, new_name: typed }; }
    if (act === "delete") body = { action: "delete", kind: "load", name };
    if (act === "default") body = { action: "default", kind: "load", name };
    if (!body) return;
    const { r, j } = await post("api/profiles", body);
    if (!r.ok) { fail(j); return; }
    if (act === "save") { await done("ld.profile.saved", typed); box.querySelector("select").value = typed; }
    if (act === "rename") { await done("ld.profile.renamed", typed); box.querySelector("select").value = typed; }
    if (act === "delete") await done("ld.profile.deleted", name);
    if (act === "default") await done("ld.profile.defaulted", name);
  }
  // "Ripristina Machine Default": the wizard's profile in the form, with its model
  function applyMachine(box) {
    const md = PROF && PROF.load.find(p => p.name === PROF.machine);
    if (!md) { pfSay(box, T("ld.machine.none"), "err"); return; }
    writeForm(md.values); syncMtp();
    if (md.model && window.BasaltModels) BasaltModels.select(md.model);
    const sel = box.querySelector("select"); if (sel) sel.value = md.name;
    formTouched = true;
    replan(); pfSay(box, T("ld.machine.done"), "ok");
  }
  document.addEventListener("basalt:machine", async e => {
    await refreshProfiles();
    if (e.detail && e.detail.apply) { view("loader"); applyMachine($("#ld-prof")); }
  });
  async function importProfiles(box, file) {
    try {
      const data = JSON.parse(await file.text());
      const { r, j } = await post("api/profiles", { action: "import", data });
      if (!r.ok) throw new Error((j.error && j.error.message) || `HTTP ${r.status}`);
      await refreshProfiles(); pfSay(box, T("ld.profile.imported"), "ok");
    } catch (e) { pfSay(box, e.message, "err"); }
  }
  {
    const box = $("#ld-prof");
    box.addEventListener("click", e => { const b = e.target.closest("[data-pf]"); if (b && b.tagName === "BUTTON") profileAction(box, b.dataset.pf); });
    box.addEventListener("change", e => { if (e.target.dataset.pf === "import" && e.target.files[0]) { importProfiles(box, e.target.files[0]); e.target.value = ""; } });
  }

  /* ---- the Settings view: what runs now and the load profile it matches; "Open the Loader" opens this view on
     that profile (the form already holds the config file's values, which are the running ones) ---- */
  const samePath = (a, b) => !!a && !!b && a.replace(/\\/g, "/").toLowerCase() === b.replace(/\\/g, "/").toLowerCase();
  function eff(f, v) {
    const x = v ?? ((INFO && INFO.defaults) || {})[f.name] ?? fallback[f.name] ?? (f.kind === "bool" ? false : null);
    return x == null ? "" : String(x);
  }
  function matchProfile(values) {
    if (!PROF || !values) return null;
    const fields = FIELDS.filter(f => !f.virtual);
    const list = [...PROF.load].sort((a, b) => (b.name === PROF.default) - (a.name === PROF.default));
    return list.find(p => {
      const pv = p.values || {};
      if (p.model && INFO.config_model && !samePath(p.model, INFO.config_model)) return false;
      // a profile without a port keeps the file's (the Loader's Use leaves the field as it is)
      return fields.every(f => (f.name === "server.port" && pv[f.name] == null) || eff(f, pv[f.name]) === eff(f, values[f.name]));
    }) || null;
  }
  const ctxText = n => { const v = Number(n); return Number.isFinite(v) && v >= 1024 && v % 1024 === 0 ? `${nfmt(v / 1024)}K` : nfmt(v); };
  function renderSettings() {
    const run = $("#st-run");
    if (!run || !INFO) return;
    const live = !INFO.standby && INFO.running && Object.keys(INFO.running).length;
    const v = live ? INFO.running : (INFO.values || {});
    const val = name => v[name] ?? ((INFO.defaults || {})[name]) ?? fallback[name];
    const spec = Number(val("engine.spec") || 0), par = Number(val("server.parallel") || 1);
    const parts = [(INFO.model && INFO.model.name) || "", ctxText(val("engine.max-context")), val("engine.kv"),
      spec >= 2 ? `spec ${spec}` : T("st.load.nomtp"), par > 1 ? T("st.load.slots", par) : "",
      T("st.load.free", toGB(val("engine.vram-reserve-mib")))].filter(Boolean);
    run.textContent = live ? T("st.load.running", parts.join(" · ")) : `${T("st.load.none")} ${T("st.load.next", parts.join(" · "))}`;
    const p = matchProfile(INFO.values);
    const prof = p ? T("st.load.profile", p.name + (p.name === PROF.default ? ` (${T("ld.profile.isdefault")})` : "")) : T("st.load.custom");
    $("#st-run-file").textContent = `${prof} · ${INFO.config ? T("st.load.file", INFO.config) : T("ld.ro.no_config")}`;
  }
  function openLoader() {
    const p = matchProfile(INFO && INFO.values);
    view("loader");
    const sel = $("#ld-prof select");
    if (sel && p) { sel.value = p.name; sel.dispatchEvent(new Event("change")); }
    (sel || $("#tab-loader")).focus();
  }
  $("#st-open-loader").addEventListener("click", openLoader);

  /* ---- reading ---- */
  async function refreshProfiles() {
    const m = modelNow();
    try { PROF = await get("api/profiles" + (m ? "?model=" + encodeURIComponent(m) : "")); } catch (e) { PROF = null; }
    profileBar("#ld-prof");
    renderSettings();
  }
  async function refresh() {
    try {
      INFO = await get("api/loader");
    } catch (e) {
      if (e.locked) askKey();
      return;
    }
    $("#ld-model").textContent = (INFO.model && INFO.model.name) || "";
    const gw = INFO.gpu_warning && (INFO.gpu_warning[LANG] || INFO.gpu_warning.en);
    $("#ld-gpu").hidden = !gw; $("#ld-gpu").textContent = gw || "";
    $("#ld-standby").hidden = !INFO.standby;
    if (window.BasaltModels) await BasaltModels.refresh();
    defaults();
    const reason = INFO.editable ? "" : T("ld.ro." + (INFO.reason || "no_config"));
    $("#ld-reason").hidden = !reason; $("#ld-reason").textContent = reason;
    $("#ld-cli").hidden = !(INFO.from_command_line || []).length;
    $("#ld-cli").textContent = T("ld.cli", (INFO.from_command_line || []).join(", "));
    if (!formTouched) writeForm(INFO.values);
    syncReserve("lf", el("engine.vram-reserve-mib"));
    $("#ld-safe-list").innerHTML = Object.entries(INFO.safe || {}).map(([k, v]) => {
      const why = (INFO.safe_why || {})[k];
      return `<dt>${esc(T("f." + k))}: <code>${esc(String(v))}</code></dt><dd>${esc(why ? why[LANG] || why.en : "")}</dd>`;
    }).join("");
    const m = modelNow();
    if (m && !(INFO.plan && INFO.plan.model && INFO.plan.model.path && INFO.plan.model.path.replace(/\\/g, "/").toLowerCase() === m.toLowerCase())) replan();
    else renderSide("#ld-side", INFO.plan, "loader");
    await refreshProfiles();
    if (["waiting", "stopping", "starting", "calibrating"].includes((INFO.status || {}).phase)) watch();
    if (viewOn === "loader") applyStart();
  }
  let formTouched = false;

  /* the first opening: ?profile=, ?model=, ?reserve= (GB) from the desktop app's command line, and ?wizard=1 when this
     PC has no Machine Default yet; a server without a loaded model starts from the default profile, not the file */
  const START = new URLSearchParams(location.search);
  let startDone = false;
  function applyStart() {
    if (startDone || !INFO) return;
    startDone = true;
    const prof = START.get("profile"), model = START.get("model"), reserve = START.get("reserve");
    if (model && window.BasaltModels) BasaltModels.select(model);
    const name = prof || (INFO.standby && PROF ? PROF.default : null);
    const p = name && PROF ? PROF.load.find(x => x.name === name) : null;
    if (p) {
      writeForm(p.values);
      if (p.model && !model && window.BasaltModels) BasaltModels.select(p.model);
      const sel = $("#ld-prof select"); if (sel) sel.value = p.name;
      formTouched = true;
    }
    if (reserve !== null && reserve !== "" && Number.isFinite(Number(reserve))) {
      el("engine.vram-reserve-mib").value = String(Number(reserve)); syncReserve("lf", el("engine.vram-reserve-mib")); formTouched = true;
    }
    if (p || model || reserve) replan();
    if (START.get("wizard") === "1" && window.BasaltWizard) BasaltWizard.offer();
    if (START.get("wizard") === "resume" && window.BasaltWizard) BasaltWizard.resume();
    if ([...START.keys()].length) history.replaceState(null, "", location.pathname + location.hash);
  }

  build();
  const formInput = e => {
    formTouched = true;
    if (e.target.id === "lf-rsv-range") syncReserve("lf", el("engine.vram-reserve-mib"), "range");
    else if (e.target === el("engine.vram-reserve-mib")) syncReserve("lf", e.target);
    syncMtp(); replan();
  };
  $("#ld-form").addEventListener("input", formInput);
  $("#ld-form").addEventListener("change", formInput);
  if (window.BasaltModels) BasaltModels.onChange(() => {
    if (viewOn === "loader") { replan(); refreshProfiles(); }
  });
  $("#ld-form").onsubmit = e => e.preventDefault();
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-act]");
    if (!b || b.disabled) return;
    if (b.dataset.act === "load") load(readForm(), "#ld-side", "loader");
    if (b.dataset.act === "calibrate") calibrate();
  });
  document.addEventListener("basalt:view", e => { if (e.detail === "loader" || e.detail === "settings") refresh(); });
  document.addEventListener("basalt:key", () => { if (viewOn === "loader" || viewOn === "settings") refresh(); });
  applyT($("#view-loader"));
  $("#ld-models").setAttribute("aria-label", T("ld.model"));
  if (viewOn === "loader" || viewOn === "settings") refresh();
})();
