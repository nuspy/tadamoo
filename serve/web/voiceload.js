"use strict";
/* Models > Voice services: the one place where speech to text, speech synthesis and the avatar are set and loaded. Each function is a card of
   its own: the switch, the model, the device and the memory, each written to the voice settings the moment it is chosen; what it takes in
   VRAM; a download for a model that is not installed; and an Apply that loads, replaces or unloads that function alone, after a confirmation.
   The Loader's profiles keep these choices. Adding a function unloads the model and loads it again; removing one leaves the model alone
   until "Change services in memory" is confirmed. Everything the server says enters the page as text. */
(function () {
  const root = document.getElementById("ld-voicesvc");
  const reloadBtn = document.getElementById("vl-reload");
  if (!root || !reloadBtn) return;

  const FUNCS = ["stt", "tts", "avatar"], GIB = 1024, POLL_MS = 1000;
  const NAME = { stt: () => T("vo.svc.stt"), tts: () => T("vo.svc.tts"), avatar: () => T("vo.svc.avatar") };
  const DEVICES = ["auto", "cpu", "cuda:0", "cuda:1"];
  const BUSY = ["waiting", "stopping", "starting", "calibrating", "measuring", "voice"];
  const PHASE = { starting: "vl.ph.applying", unloading_llm: "vl.ph.unloading", applying: "vl.ph.applying", loading_llm: "vl.ph.loading", reloading_llm: "vl.ph.loading" };
  const ACT = { load: "vl.act.load", reload: "vl.act.reload", unload: "vl.act.unload", keep: "vl.act.keep", off: "vl.act.off", missing: "vl.act.missing", on_request: "vl.act.onrequest" };
  const IDLE_MIN = [1, 2, 5, 10, 30, 60];                // minutes an avatar loaded on request may sit unused before it is unloaded
  let S = null, models = null, loaderPhase = "idle", timer = 0, loadFailed = "", idleS = 300;
  const lines = {};                                    // per function: the line under its card

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick, id) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; if (id) b.id = id; return b; };
  const gb = mib => mib == null ? T("vl.unknown") : T("vl.gb", (mib / GIB).toFixed(1));
  const errText = r => r.status === 0 ? T("vo.e.offline") : (r.data && r.data.error && (LANG === "it" && r.data.error.message_it ? r.data.error.message_it : r.data.error.message)) || `HTTP ${r.status}`;
  const shown = () => document.body.dataset.view === "loader" && !root.hidden;
  const catalog = f => (models && models.services.find(s => s.id === f)) || { models: [] };
  const entry = (f, id) => catalog(f).models.find(m => m.id === id);
  const jobBusy = () => S && S.job && PHASE[S.job.phase];
  const busyNow = () => jobBusy() || BUSY.includes(loaderPhase);
  const line = f => lines[f] || (lines[f] = { text: "", bad: false });

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  const dl = VoiceDownload({ models: () => models, ids: FUNCS, root, runId: "vl-run", repaint: () => paint(), refresh: () => refresh(true), active: shown });

  /* ---- reading ---- */
  async function refresh(withModels) {
    if (withModels || !models) {
      const m = await api("GET", "api/voice/models");
      if (m.ok) models = m.data;
    }
    const r = await api("GET", "api/voice/functions");
    if (r.ok) { S = r.data; loadFailed = ""; } else loadFailed = errText(r);
    const a = await api("GET", "api/voice/avatar");
    if (a.ok && a.data.idle_unload_s) idleS = a.data.idle_unload_s;
    const l = await api("GET", "api/loader/status");
    loaderPhase = l.ok && l.data.phase ? l.data.phase : "idle";
    paint();
    schedule();
    if (withModels && models) dl.poll();
    return r.ok;
  }
  function schedule() {
    clearTimeout(timer);
    if (busyNow()) timer = setTimeout(refresh, POLL_MS);
  }
  async function write(f, path, body) {                  // a choice is written to the voice settings at once
    const r = await api("POST", path, body);
    lines[f] = { text: r.ok ? "" : errText(r), bad: !r.ok };
    await refresh(true);
  }

  /* ---- the tab ---- */
  function paint() {
    const focus = document.activeElement && root.contains(document.activeElement) ? document.activeElement.id : "";
    reloadBtn.hidden = !(S && S.pending_reload);
    reloadBtn.disabled = !!(S && busyNow());
    if (!S) { root.replaceChildren(el("p", "vl-intro", loadFailed || T("vl.loading"))); return; }
    root.replaceChildren(el("p", "vl-intro", T("vl.intro")), serviceNote(), summary(), pendingBox(), jobBox(), ...(models ? [dl.runBox()] : []), cards());
    if (focus) { const f = document.getElementById(focus); if (f) f.focus(); }
  }
  function serviceNote() {
    const n = el("p", "vl-note", S.service.running ? "" : T("vl.down", S.service.reason));
    n.setAttribute("role", "alert");
    n.hidden = S.service.running;
    return n;
  }
  function row(dl2, k, v) { dl2.append(el("dt", "", k), el("dd", "", v)); }
  function summary() {
    const box = el("section", "vl-box"), v = S.vram, facts = el("dl", "vl-facts");
    box.setAttribute("aria-label", T("vl.sum"));
    box.append(el("h3", "vl-h", T("vl.sum")));
    row(facts, T("vl.free"), gb(v.free_mib));
    row(facts, T("vl.reserve"), gb(v.reserve_mib));
    row(facts, T("vl.need"), S.set_unknown.length ? T("vl.need.partial", gb(S.set_need_mib)) : gb(S.set_need_mib));
    row(facts, T("vl.left"), S.left_for_llm_mib == null ? T("vl.unknown") : gb(S.left_for_llm_mib));
    box.append(facts);
    return box;
  }
  function pendingBox() {
    const box = el("section", "vl-box vl-pending");
    box.hidden = !S.pending_reload;
    if (!S.pending_reload) return box;
    box.setAttribute("role", "status");
    box.append(el("h3", "vl-h", T("vl.pend.title")), el("p", "vl-line", T("vl.pend.text", gb(S.pending_reload.recovered_mib))),
      btn(T("vl.reload"), "primary", () => confirmReload(), "vl-reload-tab"));
    return box;
  }
  function jobBox() {
    const j = S.job || {}, box = el("section", "vl-box vl-job");
    const loading = BUSY.includes(loaderPhase) && !jobBusy();
    box.hidden = !(jobBusy() || j.phase === "done" || j.phase === "failed" || loading);
    if (box.hidden) return box;
    box.setAttribute("role", "status");
    box.setAttribute("aria-live", "polite");
    const text = jobBusy() ? T(PHASE[j.phase]) : j.phase === "failed" ? T("vl.ph.failed", j.error || "") : loading ? T("vl.ph.loader") : T("vl.ph.done");
    box.append(el("h3", "vl-h", text));
    const report = j.report || {};
    Object.keys(report).forEach(f => {
      const r = report[f];
      if (r.action === "keep") return;
      const state = r.error ? T("vl.rep.error", r.error) : r.action === "unload" ? T("vl.rep.unloaded", gb(r.measured_mib)) : T("vl.rep.loaded", gb(r.measured_mib));
      box.append(el("p", "vl-line", `${NAME[f] ? NAME[f]() : f}: ${state}`));
    });
    return box;
  }
  function chip(text, cls) { return el("span", "vl-chip " + (cls || ""), text); }
  function cards() {
    const list = el("div", "vl-list");
    S.functions.forEach(f => list.append(card(f)));
    return list;
  }
  function field(id, label, control) {
    const w = el("div", "vl-field"), l = el("label", "", label);
    l.htmlFor = id; control.id = id;
    w.append(l, control);
    return w;
  }
  function select(options, value, onchange) {
    const s = el("select");
    options.forEach(([v, text]) => { const o = el("option", "", text); o.value = v; s.append(o); });
    s.value = value;
    s.onchange = () => onchange(s.value);
    return s;
  }
  function estimateText(f) {
    if (f.estimate_source === "cpu") return T("vl.est.cpu");
    const src = f.estimate_source === "measured" ? T("vl.est.measured") : f.estimate_source === "catalog" ? T("vl.est.catalog") : "";
    return f.estimated_mib == null ? T("vl.est.unknown") : gb(f.estimated_mib) + (src ? " (" + src + ")" : "");
  }
  function whenRow(f) {                                  // when the avatar loads, and how long it may sit unused before it is unloaded
    const row = el("div", "vl-place");
    row.append(field("vl-load-" + f.id, T("vl.when"), select([["at_load", T("vl.when.load")], ["on_request", T("vl.when.req")]], f.load || "on_request",
      v => write(f.id, "api/voice/load-mode", { service: f.id, mode: v }))));
    if ((f.load || "on_request") === "on_request") {
      const mins = IDLE_MIN.includes(idleS / 60) ? IDLE_MIN : IDLE_MIN.concat([idleS / 60]).sort((x, y) => x - y);
      row.append(field("vl-idle-" + f.id, T("vl.idle"), select(mins.map(m => [String(m * 60), T("vl.min", String(m))]), String(idleS),
        v => write(f.id, "api/voice/avatar", { idle_unload_s: Number(v) }))));
    }
    return row;
  }
  function card(f) {
    const box = el("section", "vl-card"), h = el("h3", "vl-h", NAME[f.id]());
    h.id = "vl-h-" + f.id;
    box.setAttribute("aria-labelledby", h.id);
    const state = f.state === "loaded" ? T("vl.st.loaded") : f.state === "loading" ? T("vl.st.loading") : f.state === "error" ? T("vl.st.error") : T("vl.st.unloaded");
    const chips = el("div", "vl-chips");
    chips.append(chip(state, f.state === "loaded" ? "vl-ok" : f.state === "error" ? "vl-warn" : ""));
    if (!f.installed) chips.append(chip(T("vl.notinstalled"), "vl-warn"));
    box.append(h, chips);

    // the choices: each one is written to the voice settings as it is made
    const sw = el("label", "vl-check"), cb = el("input");
    cb.type = "checkbox"; cb.id = "vl-on-" + f.id; cb.checked = f.on;
    cb.onchange = () => write(f.id, "api/voice/enabled", { service: f.id, enabled: cb.checked });
    sw.append(cb, el("span", "", T("vl.load")));
    const opts = catalog(f.id).models.filter(x => x.selectable !== false)
      .map(x => [x.id, x.name + (x.size ? " (" + T("vl.gb", (x.size / 1e9).toFixed(1)) + ")" : "") + (x.installed ? "" : " - " + T("vl.notinstalled"))]);
    if (!opts.some(o => o[0] === f.model)) opts.push([f.model, f.name]);
    const place = (device, memory) => write(f.id, "api/voice/placement", { service: f.id, device, memory });
    const model = field("vl-model-" + f.id, T("vl.model"), select(opts, f.model, v => write(f.id, "api/voice/select", { service: f.id, model: v })));
    const device = field("vl-device-" + f.id, T("vo.device"), select(DEVICES.concat(DEVICES.includes(f.device) ? [] : [f.device]).map(x => [x, x === "auto" ? T("vo.device.auto") : x === "cpu" ? T("vo.device.cpu") : T("vo.device.gpu", x.split(":")[1])]), f.device,
      v => place(v, v === "cpu" ? "ram" : f.memory)));
    const memory = field("vl-memory-" + f.id, T("vo.memory"), select([["vram", T("vo.memory.vram")], ["ram", T("vo.memory.ram")]], f.memory,
      v => v === "vram" && f.device === "cpu" ? place("auto", v) : place(f.device, v)));
    const where = el("div", "vl-place");
    where.append(device, memory);
    box.append(sw, model, where);
    if (f.id === "avatar") box.append(whenRow(f));

    const facts = el("dl", "vl-facts"), m = entry(f.id, f.model);
    row(facts, T("vo.engine"), f.engine);
    row(facts, T("vo.weight"), m && m.size ? T("vl.gb", (m.size / 1e9).toFixed(1)) : T("vo.size.unknown"));
    row(facts, T("vl.measured"), f.measured_mib != null ? gb(f.measured_mib) : T("vl.notmeasured"));
    row(facts, T("vl.estimate"), estimateText(f));
    box.append(facts);

    // what the choices mean for memory, and the one button that makes memory match them
    const pending = ["load", "reload", "unload"].includes(f.action);
    if (ACT[f.action]) box.append(el("p", "vl-help" + (f.action === "missing" ? " vl-bad" : ""), T(ACT[f.action])));
    if (f.error) box.append(el("p", "vl-help vl-bad", f.error));
    if (models && m && !f.installed) box.append(dl.part(catalog(f.id), m));
    const apply = btn(T("vl.apply"), "primary", () => confirmApply(f), "vl-apply-" + f.id);
    apply.disabled = !pending || !S.service.running || !!busyNow();
    const bar = el("div", "vl-bar");
    bar.append(apply);
    box.append(bar);
    const u = line(f.id), msg = el("p", "vl-help" + (u.bad ? " vl-bad" : ""), u.text);
    msg.setAttribute("role", u.bad ? "alert" : "status");
    msg.hidden = !u.text;
    box.append(msg);
    return box;
  }

  /* ---- dialogs ---- */
  function makeDialog(id) {
    const d = el("dialog", "vl-dlg");
    d.id = id;
    d.setAttribute("aria-labelledby", id + "-h");
    const body = el("div", "vl-dlg-body");
    d.append(body);
    document.body.append(d);
    d.addEventListener("close", () => {                                    // the control that opened it gets the focus back, unless focus went somewhere on purpose
      if (document.activeElement && document.activeElement !== document.body) return;
      (d._from && d._from.offsetParent ? d._from : document.getElementById("sub-voicesvc")).focus();
    });
    return { d, body };
  }
  function openDialog(d, from) {
    d._from = from || document.activeElement;
    if (!d.open) d.showModal();
  }
  function heading(id, text) { const h = el("h2", "vl-dlg-h", text); h.id = id + "-h"; return h; }
  function names(items, acts) { return items.filter(i => acts.includes(i.action)).map(i => NAME[i.id]()).join(", "); }

  function confirmDialog(id, title, text, okText, onOk, from) {
    const old = document.getElementById(id);
    if (old) old.remove();
    const m = makeDialog(id), bar = el("div", "vl-bar"), msg = el("p", "vl-note");
    msg.setAttribute("role", "alert");
    msg.hidden = true;
    const ok = btn(okText, "primary", async () => {
      ok.disabled = true;
      const err = await onOk();
      if (err) { msg.textContent = err; msg.hidden = false; ok.disabled = false; return; }
      m.d.close();
    }, id + "-ok");
    bar.append(ok, btn(T("vl.c.cancel"), "", () => m.d.close(), id + "-cancel"));
    m.body.append(heading(id, title), ...text.map(t => el("p", "vl-line", t)), msg, bar);
    m.d.addEventListener("close", () => m.d.remove());
    openDialog(m.d, from);
    document.getElementById(id + "-cancel").focus();                       // the safe choice has the focus
  }
  async function confirmApply(f) {
    const u = line(f.id);
    const r = await api("POST", "api/voice/functions/plan", { only: f.id });
    if (!r.ok) { lines[f.id] = { text: errText(r), bad: true }; paint(); return; }
    const p = r.data, text = [];
    const loads = names(p.items, ["load", "reload"]), unloads = names(p.items, ["unload"]);
    if (loads) text.push(T("vl.p.loads", loads));
    if (unloads) text.push(T("vl.p.unloads", unloads));
    text.push(`${T("vl.p.need")}: ${gb(p.need_mib)} · ${T("vl.p.recover")}: ${gb(p.recover_mib)} · ${T("vl.left")}: ${gb(p.left_for_llm_mib)}`);
    if (p.fits === false) text.push(T("vl.p.nofit"));
    else if (p.fits === null && p.need_mib) text.push(T("vl.p.unknown"));
    text.push(p.llm_reload ? T("vl.c.reload", gb(p.left_for_llm_mib)) : p.reload_suggested ? T("vl.c.noreload") : T("vl.c.noreload2"));
    u.text = ""; u.bad = false;
    confirmDialog("vl-confirm", T("vl.c.title", NAME[f.id]()), text, T("vl.c.ok"), async () => {
      const a = await api("POST", "api/voice/functions/apply", { only: f.id, confirm: true });
      if (!a.ok) return errText(a);
      await refresh(true);
      return "";
    }, document.getElementById("vl-apply-" + f.id));
  }
  function confirmReload() {
    const p = S.pending_reload, v = S.vram;
    const text = [T("vl.r.done", (p.removed || []).map(f => NAME[f] ? NAME[f]() : f).join(", "), gb(p.recovered_mib)),
      T("vl.r.reload", gb(v.free_mib), gb(v.reserve_mib))];
    confirmDialog("vl-rconfirm", T("vl.r.title"), text, T("vl.r.ok"), async () => {
      const r = await api("POST", "api/voice/functions/reload-llm", { confirm: true });
      if (!r.ok) return errText(r);
      await refresh();
      return "";
    }, reloadBtn.hidden ? document.getElementById("sub-voicesvc") : reloadBtn);
  }

  /* ---- wiring ---- */
  reloadBtn.onclick = confirmReload;
  document.addEventListener("tadamoo:view", e => { if (e.detail === "loader") refresh(true); else { clearTimeout(timer); dl.stop(); } });
  document.addEventListener("basalt:models-sub", e => { if (e.detail === "voicesvc") refresh(true); });
  document.addEventListener("tadamoo:voice", () => refresh(true));                  // a profile was used: it wrote the voice settings
  document.addEventListener("tadamoo:lang", () => { if (S) paint(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && shown()) refresh(); });
  if (document.body.dataset.view === "loader") refresh(true);
})();
