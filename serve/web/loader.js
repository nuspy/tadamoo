"use strict";
/* Loader: the engine settings by hand or from a saved load profile, with the memory they need next to what this PC
   has free now and the speed they should give (both from the server: GET /api/loader, POST /api/loader/plan). Load
   writes the config file and restarts the engine inside the server (POST /api/loader/load). The Settings view
   reuses the profile picker, the prediction and the Reload button. */
(() => {
  const FIELDS = [
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
    { group: "memory", name: "engine.vram-reserve-mib", kind: "int", min: 0, max: 65536, step: 256 },
    { group: "memory", name: "engine.ple-io", kind: "choice", options: ["direct", "mmap", "ram"] },
    { group: "memory", name: "engine.pool-workers", kind: "int", min: 1, max: 256, step: 1 },
    { group: "server", name: "server.port", kind: "int", min: 1, max: 65535, step: 1 },
    { group: "server", name: "server.vision", kind: "choice", options: ["off", "dynamic", "on", "peer", "cpu"] },
  ];
  const fid = name => "lf-" + name.replace(/[^a-z0-9]+/gi, "-");
  const GiBn = 2 ** 30;
  const gib = n => nfmt(n / GiBn, 1);
  let INFO = null, PROF = null, seq = 0, timer = 0, poll = 0, stValues = null;
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

  function defaults() {
    const d = (INFO && INFO.defaults) || {};
    FIELDS.forEach(f => {
      const e = el(f.name);
      if (!e || f.virtual) return;
      const v = d[f.name];
      if (f.kind === "choice") e.querySelector("[data-default]").textContent = T("opt.default", v ?? fallback[f.name] ?? "—");
      else if (f.kind !== "bool") e.placeholder = v == null ? "—" : String(v);
    });
  }

  function readForm() {
    const out = {};
    for (const f of FIELDS) {
      if (f.virtual) continue;
      const e = el(f.name);
      if (f.kind === "bool") { out[f.name] = e.checked; continue; }
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
  function bar(kind, need, have, keep, items, notes) {
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
      ${keep ? `<p class="res-note">${esc(T("pr.keep", gib(keep)))}</p>` : ""}
      ${(notes || []).map(n => `<p class="res-note">${esc(n)}</p>`).join("")}
      ${lis ? `<ul class="res-items">${lis}</ul>` : ""}
    </div>`;
  }
  function when(iso) {
    try { return new Date(iso).toLocaleString(LANG === "it" ? "it-IT" : "en-US", { dateStyle: "medium", timeStyle: "short" }); } catch (e) { return iso; }
  }
  function sideHtml(p, mode) {
    const v = p.vram, r = p.ram, c = p.commit, s = p.speed || {}, h = p.engine_holds || {};
    const holds = (h.ram || h.commit) ? [T("pr.holds", gib((h.commit || 0) + (h.vram || 0)))] : [];
    const vnotes = [T("pr.reserve", gib(v.reserve)), T("pr.slots", nfmt(v.expert_slots), nfmt(v.expert_slots_total),
      nfmt(v.expert_slots / Math.max(1, v.expert_slots_total) * 100, 0))];
    const src = s.source || {};
    const srcText = src.kind === "calibration" ? T(src.exact ? "sp.cal.exact" : "sp.cal", when(src.at)) : T("sp.seed");
    const problems = (p.problems || []).map(x => LANG === "it" ? x.text_it : x.text);
    const editable = INFO && INFO.editable, busy = INFO && INFO.status && ["waiting", "stopping", "starting", "calibrating"].includes(INFO.status.phase);
    const why = !editable ? T("ld.ro." + ((INFO && INFO.reason) || "no_config")) : busy ? T("ld.busy") : problems.length ? T("ld.refused", problems.join(" ")) : "";
    const label = mode === "settings" ? T("ld.reload") : T("ld.load");
    return `<section class="sec ldres" aria-label="${esc(T("pr.title"))}">
        <div class="sh"><b>${esc(T("pr.title"))}</b></div>
        <div class="pad">
          ${bar("vram", v.need + v.reserve, v.available, 0, v.items, vnotes)}
          ${bar("ram", r.need, r.available, r.keep, r.items, holds)}
          ${bar("commit", c.need, c.available, c.keep, [], [T("pr.commit.parts", gib(c.ram_part), gib(c.vram_part))])}
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
  function renderSide(target, p, mode) {
    const box = $(target);
    box.innerHTML = sideHtml(p, mode);
    if (mode === "loader") sticky(p);
    showStatus();
  }
  // on a phone the prediction is far below the fields: a summary bar stays at the bottom while editing
  function sticky(p) {
    const ok = k => !(p.problems || []).some(x => x.code === k || (k === "ram" && x.code === "ple_ram"));
    const part = (k, label) => `<span data-ok="${ok(k)}">${ok(k) ? "✓" : "✕"} ${esc(label)}</span>`;
    const go = $("#ld-side [data-act=load]");
    $("#ld-sticky").innerHTML = `<span class="stk-parts">${part("vram", T("pr.vram"))}${part("ram", T("pr.ram"))}${part("commit", "Commit")}` +
      `<span>~${nfmt((p.speed || {}).decode, 0)} tok/s</span></span>` +
      `<a class="btn ${go && !go.disabled ? "primary" : "ghost"}" href="#ld-side">${esc(go && !go.disabled ? T("ld.load") : T("pr.nofit"))}</a>`;
  }

  async function plan(values) {
    const r = await fetch("api/loader/plan", { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ values }) });
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
  async function load(values, target, mode) {
    const { r, j } = await post("api/loader/load", { values });
    if (r.status === 422 && j.plan) { renderSide(target, j.plan, mode); return; }
    if (!r.ok) { say(T("ld.phase.failed", (j.error && j.error.message) || `HTTP ${r.status}`), "err"); return; }
    if ((j.notes || []).includes("port_on_restart")) say(T("ld.port.note"));
    INFO.status = { phase: "waiting" };
    watch();
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
    if (s.vram_gb != null) parts.push(`VRAM ~${nfmt(s.vram_gb, 1)} GB`);
    if (s.tok_s != null) parts.push(`~${nfmt(s.tok_s)} tok/s`);
    if (s.port && s.port !== 7232) parts.push(`:${s.port}`);
    const tags = [p.builtin ? T("ld.profile.builtin") : "", p.default ? T("ld.profile.isdefault") : ""].filter(Boolean);
    return `${p.name}${tags.length ? ` (${tags.join(", ")})` : ""} — ${parts.join(" · ")}`;
  }
  function headlessCmd(name) {
    return `powershell -ExecutionPolicy Bypass -File Avvia-Basalt.ps1 --config "${name.replace(/"/g, "")}" -Sfondo`;
  }
  function profileBar(target, mode) {
    const box = $(target);
    if (!PROF) { box.replaceChildren(); return; }
    const id = target.slice(1) + "-sel", sel = box.querySelector("select");
    const keep = sel ? sel.value : (mode === "settings" ? "" : PROF.default);
    const editable = INFO && INFO.editable;
    box.innerHTML = `
      <div class="pfrow"><label for="${id}">${esc(T("ld.profile"))}</label>
        <select id="${id}">${mode === "settings" ? `<option value="">${esc(T("ld.config"))}</option>` : ""}${PROF.load.map(p =>
          `<option value="${esc(p.name)}">${esc(profileLabel(p))}</option>`).join("")}</select>
        ${mode === "loader" ? `<button type="button" class="btn" data-pf="use">${esc(T("ld.profile.apply"))}</button>` : ""}</div>
      ${mode === "loader" ? `<div class="pfrow wrap">
        <input type="text" maxlength="60" autocomplete="off" aria-label="${esc(T("ld.profile.name"))}" placeholder="${esc(T("ld.profile.name"))}" data-pf-name>
        <button type="button" class="btn" data-pf="save" ${editable ? "" : "disabled"}>${esc(T("ld.profile.save"))}</button>
        <button type="button" class="btn ghost" data-pf="rename" ${editable ? "" : "disabled"}>${esc(T("ld.profile.rename"))}</button>
        <button type="button" class="btn ghost" data-pf="delete" ${editable ? "" : "disabled"}>${esc(T("ld.profile.delete"))}</button>
        <button type="button" class="btn ghost" data-pf="default" ${editable ? "" : "disabled"}>${esc(T("ld.profile.default"))}</button>
        <a class="btn ghost" href="api/profiles/export?kind=load" download="basalt-load-profiles.json">${esc(T("ld.profile.export"))}</a>
        <label class="btn ghost filebtn">${esc(T("ld.profile.import"))}<input type="file" accept="application/json,.json" data-pf="import" ${editable ? "" : "disabled"}></label>
        <button type="button" class="btn" data-pf="safe">${esc(T("ld.safe"))}</button></div>` : ""}
      <div class="pfrow wrap headless"><span>${esc(T("ld.headless"))}:</span><code data-pf-cmd></code>
        <button type="button" class="btn ghost" data-pf="copy">${esc(T("ld.headless.copy"))}</button></div>
      <p class="setnote">${esc(T("ld.headless.help"))}</p>
      <p class="setstatus pfmsg" role="status" aria-live="polite"></p>`;
    const s = box.querySelector("select");
    s.value = [...s.options].some(o => o.value === keep) ? keep : s.options[0].value;
    const cmd = () => { box.querySelector("[data-pf-cmd]").textContent = headlessCmd(s.value || PROF.default); };
    cmd();
    s.onchange = () => {
      cmd();
      if (mode === "settings") settingsPick(s.value);
    };
  }
  function pfSay(box, text, kind) { const m = box.querySelector(".pfmsg"); if (m) { m.textContent = text; m.dataset.kind = kind || ""; } }
  async function profileAction(box, act, mode) {
    const s = box.querySelector("select"), name = s.value, input = box.querySelector("[data-pf-name]");
    const typed = input ? input.value.trim() : "";
    const prof = PROF.load.find(p => p.name === name);
    const done = async (key, arg) => { await refreshProfiles(); pfSay(box, T(key, arg), "ok"); };
    const fail = j => pfSay(box, (j.error && j.error.message) || "error", "err");
    if (act === "use" && prof) { writeForm(prof.values); syncMtp(); replan(); pfSay(box, T("ld.profile.used", name), "ok"); return; }
    if (act === "safe") {
      writeForm({ ...INFO.safe, "server.port": Number(el("server.port").value) || INFO.safe["server.port"] || 7232 });
      syncMtp(); replan(); $("#ld-safe-why").open = true; pfSay(box, T("ld.safe.done"), "ok"); return;
    }
    if (act === "copy") {
      const t = box.querySelector("[data-pf-cmd]").textContent;
      if (navigator.clipboard) navigator.clipboard.writeText(t).then(() => pfSay(box, T("ld.headless.copied"), "ok"), () => {});
      return;
    }
    let body = null;
    if (act === "save") { if (!typed) { pfSay(box, T("cs.preset.noname"), "err"); input.focus(); return; } body = { action: "save", kind: "load", name: typed, values: readForm() }; }
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
  async function importProfiles(box, file) {
    try {
      const data = JSON.parse(await file.text());
      const { r, j } = await post("api/profiles", { action: "import", data });
      if (!r.ok) throw new Error((j.error && j.error.message) || `HTTP ${r.status}`);
      await refreshProfiles(); pfSay(box, T("ld.profile.imported"), "ok");
    } catch (e) { pfSay(box, e.message, "err"); }
  }
  for (const [target, mode] of [["#ld-prof", "loader"], ["#st-prof", "settings"]]) {
    const box = $(target);
    box.addEventListener("click", e => { const b = e.target.closest("[data-pf]"); if (b && b.tagName === "BUTTON") profileAction(box, b.dataset.pf, mode); });
    box.addEventListener("change", e => { if (e.target.dataset.pf === "import" && e.target.files[0]) { importProfiles(box, e.target.files[0]); e.target.value = ""; } });
  }

  /* ---- the Settings view: a profile or the file, its prediction, Reload ---- */
  async function settingsPick(name) {
    const prof = PROF && PROF.load.find(p => p.name === name);
    stValues = prof ? { ...prof.values } : { ...(INFO.values || {}) };
    try { renderSide("#st-side", await plan(stValues), "settings"); } catch (e) { $("#st-side").textContent = e.message; }
  }

  /* ---- reading ---- */
  async function refreshProfiles() {
    try { PROF = await get("api/profiles"); } catch (e) { PROF = null; }
    profileBar("#ld-prof", "loader");
    profileBar("#st-prof", "settings");
  }
  async function refresh() {
    try {
      INFO = await get("api/loader");
    } catch (e) {
      if (e.locked) askKey();
      return;
    }
    $("#ld-model").textContent = (INFO.model && INFO.model.name) || "";
    defaults();
    const reason = INFO.editable ? "" : T("ld.ro." + (INFO.reason || "no_config"));
    $("#ld-reason").hidden = !reason; $("#ld-reason").textContent = reason;
    $("#ld-cli").hidden = !(INFO.from_command_line || []).length;
    $("#ld-cli").textContent = T("ld.cli", (INFO.from_command_line || []).join(", "));
    if (!formTouched) writeForm(INFO.values);
    $("#ld-safe-list").innerHTML = Object.entries(INFO.safe || {}).map(([k, v]) => {
      const why = (INFO.safe_why || {})[k];
      return `<dt>${esc(T("f." + k))}: <code>${esc(String(v))}</code></dt><dd>${esc(why ? why[LANG] || why.en : "")}</dd>`;
    }).join("");
    renderSide("#ld-side", INFO.plan, "loader");
    await refreshProfiles();
    if (viewOn === "settings") settingsPick(($("#st-prof select") || {}).value || "");
    if (["waiting", "stopping", "starting", "calibrating"].includes((INFO.status || {}).phase)) watch();
  }
  let formTouched = false;

  build();
  $("#ld-form").addEventListener("input", () => { formTouched = true; syncMtp(); replan(); });
  $("#ld-form").addEventListener("change", () => { formTouched = true; syncMtp(); replan(); });
  $("#ld-form").onsubmit = e => e.preventDefault();
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-act]");
    if (!b || b.disabled) return;
    const inSettings = !!b.closest("#st-side");
    if (b.dataset.act === "load") load(inSettings ? stValues : readForm(), inSettings ? "#st-side" : "#ld-side", inSettings ? "settings" : "loader");
    if (b.dataset.act === "calibrate") calibrate();
  });
  document.addEventListener("basalt:view", e => { if (e.detail === "loader" || e.detail === "settings") refresh(); });
  document.addEventListener("basalt:key", () => { if (viewOn === "loader" || viewOn === "settings") refresh(); });
  applyT($("#view-loader")); applyT($("#st-reload"));
  if (viewOn === "loader" || viewOn === "settings") refresh();
})();
