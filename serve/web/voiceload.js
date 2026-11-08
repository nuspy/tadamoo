"use strict";
/* Models > Voice services and the "Voice functions" dialog: which voice functions (speech to text, speech synthesis, avatar) are in memory,
   the model and placement of each, the VRAM they take, and what is left for the language model after the Loader's reserve.
   The dialog edits a draft, previews the plan (what loads, what unloads, whether it fits) and applies only after a confirmation.
   Adding functions unloads the model and loads it again; removing them leaves the model alone until "Change services in memory" is
   confirmed. Everything the server says enters the page as text. */
(function () {
  const root = document.getElementById("ld-voicesvc");
  const openBtn = document.getElementById("vl-open"), reloadBtn = document.getElementById("vl-reload");
  if (!root || !openBtn || !reloadBtn) return;

  const FUNCS = ["stt", "tts", "avatar"], GIB = 1024, POLL_MS = 1000, PLAN_MS = 250;
  const NAME = { stt: () => T("vo.svc.stt"), tts: () => T("vo.svc.tts"), avatar: () => T("vo.svc.avatar") };
  const DEVICES = ["auto", "cpu", "cuda:0", "cuda:1"];
  const BUSY = ["waiting", "stopping", "starting", "calibrating", "measuring", "voice"];
  const PHASE = { starting: "vl.ph.applying", unloading_llm: "vl.ph.unloading", applying: "vl.ph.applying", loading_llm: "vl.ph.loading", reloading_llm: "vl.ph.loading" };
  let S = null, models = null, loaderPhase = "idle", timer = 0, planTimer = 0, planSeq = 0, loadFailed = "";
  let draft = null, plan = null, planErr = "", dlg = null, dlgBody = null;

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick, id) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; if (id) b.id = id; return b; };
  const gb = mib => mib == null ? T("vl.unknown") : T("vl.gb", (mib / GIB).toFixed(1));
  const errText = r => r.status === 0 ? T("vo.e.offline") : (r.data && r.data.error && (LANG === "it" && r.data.error.message_it ? r.data.error.message_it : r.data.error.message)) || `HTTP ${r.status}`;
  const shown = () => document.body.dataset.view === "loader" && !root.hidden;
  const catalog = f => (models && models.services.find(s => s.id === f)) || { models: [] };
  const entry = (f, id) => catalog(f).models.find(m => m.id === id);
  const jobBusy = () => S && S.job && PHASE[S.job.phase];

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  /* ---- reading ---- */
  async function refresh() {
    const r = await api("GET", "api/voice/functions");
    if (r.ok) { S = r.data; loadFailed = ""; } else loadFailed = errText(r);
    const l = await api("GET", "api/loader/status");
    loaderPhase = l.ok && l.data.phase ? l.data.phase : "idle";
    paint();
    schedule();
    return r.ok;
  }
  function schedule() {
    clearTimeout(timer);
    if (jobBusy() || BUSY.includes(loaderPhase)) timer = setTimeout(refresh, POLL_MS);
  }

  /* ---- the tab ---- */
  function paint() {
    reloadBtn.hidden = !(S && S.pending_reload);
    reloadBtn.disabled = !!(S && (jobBusy() || BUSY.includes(loaderPhase)));
    openBtn.disabled = !S;
    if (!S) { root.replaceChildren(el("p", "vl-intro", loadFailed || T("vl.loading"))); return; }
    root.replaceChildren(el("p", "vl-intro", T("vl.intro")), serviceNote(), summary(), pendingBox(), jobBox(), cards());
    if (dlg && dlg.open) paintDialog();
  }
  function serviceNote() {
    const n = el("p", "vl-note", S.service.running ? "" : T("vl.down", S.service.reason));
    n.setAttribute("role", "alert");
    n.hidden = S.service.running;
    return n;
  }
  function row(dl, k, v) { dl.append(el("dt", "", k), el("dd", "", v)); }
  function summary() {
    const box = el("section", "vl-box"), v = S.vram, dl = el("dl", "vl-facts");
    box.setAttribute("aria-label", T("vl.sum"));
    box.append(el("h3", "vl-h", T("vl.sum")));
    row(dl, T("vl.free"), gb(v.free_mib));
    row(dl, T("vl.reserve"), gb(v.reserve_mib));
    row(dl, T("vl.need"), S.set_unknown.length ? T("vl.need.partial", gb(S.set_need_mib)) : gb(S.set_need_mib));
    row(dl, T("vl.left"), S.left_for_llm_mib == null ? T("vl.unknown") : gb(S.left_for_llm_mib));
    box.append(dl);
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
  function placementText(f) {
    const dev = f.device === "auto" ? T("vo.device.auto") : f.device === "cpu" ? T("vo.device.cpu") : T("vo.device.gpu", f.device.split(":")[1]);
    return `${dev}, ${f.memory === "vram" ? T("vo.memory.vram") : T("vo.memory.ram")}`;
  }
  function card(f) {
    const box = el("section", "vl-card");
    const h = el("h3", "vl-h", NAME[f.id]());
    h.id = "vl-h-" + f.id;
    box.setAttribute("aria-labelledby", h.id);
    const state = f.state === "loaded" ? T("vl.st.loaded") : f.state === "loading" ? T("vl.st.loading") : f.state === "error" ? T("vl.st.error") : T("vl.st.unloaded");
    const chips = el("div", "vl-chips");
    chips.append(chip(f.on ? T("vl.on") : T("vl.off"), f.on ? "vl-ok" : ""), chip(state, f.state === "loaded" ? "vl-ok" : f.state === "error" ? "vl-warn" : ""));
    if (!f.installed) chips.append(chip(T("vl.notinstalled"), "vl-warn"));
    const dl = el("dl", "vl-facts");
    row(dl, T("vl.model"), f.name);
    row(dl, T("vo.engine"), f.engine);
    const m = entry(f.id, f.model);
    row(dl, T("vo.weight"), m && m.size ? T("vl.gb", (m.size / 1e9).toFixed(1)) : T("vo.size.unknown"));
    row(dl, T("vl.place"), placementText(f));
    row(dl, T("vl.measured"), f.measured_mib != null ? gb(f.measured_mib) : T("vl.notmeasured"));
    row(dl, T("vl.estimate"), estimateText(f));
    box.append(h, chips, dl);
    if (f.reason) box.append(el("p", "vl-help", f.reason));
    if (f.error) box.append(el("p", "vl-help vl-bad", f.error));
    return box;
  }
  function estimateText(f) {
    if (f.estimate_source === "cpu") return T("vl.est.cpu");
    const src = f.estimate_source === "measured" ? T("vl.est.measured") : f.estimate_source === "catalog" ? T("vl.est.catalog") : "";
    return f.estimated_mib == null ? T("vl.est.unknown") : gb(f.estimated_mib) + (src ? " (" + src + ")" : "");
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
      (d._from && d._from.offsetParent ? d._from : openBtn).focus();
    });
    return { d, body };
  }
  function openDialog(d, from) {
    d._from = from || document.activeElement;
    if (!d.open) d.showModal();
  }
  function heading(id, text) { const h = el("h2", "vl-dlg-h", text); h.id = id + "-h"; return h; }

  async function openMain() {
    if (!dlg) { const m = makeDialog("vl-dlg"); dlg = m.d; dlgBody = m.body; }
    const r = await api("GET", "api/voice/models");
    if (r.ok) models = r.data;
    if (!S) await refresh();
    if (!S) return;
    draft = {};
    S.functions.forEach(f => { draft[f.id] = { on: f.on, model: f.model, device: f.device, memory: f.memory }; });
    plan = null; planErr = "";
    paintDialog();
    openDialog(dlg, openBtn);
    const first = dlg.querySelector("input, select");
    if (first) first.focus();
    queuePlan(0);
  }
  function queuePlan(wait) {
    clearTimeout(planTimer);
    planTimer = setTimeout(async () => {
      const seq = ++planSeq;
      const r = await api("POST", "api/voice/functions/plan", { desired: draft });
      if (seq !== planSeq) return;
      if (r.ok) { plan = r.data; planErr = ""; } else { plan = null; planErr = errText(r); }
      if (dlg && dlg.open) paintPlan();
    }, wait == null ? PLAN_MS : wait);
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
  function fnFieldset(f) {
    const d = draft[f.id], fs = el("fieldset", "vl-fn"), lg = el("legend", "vl-h2", NAME[f.id]());
    const sw = el("label", "vl-check"), cb = el("input");
    cb.type = "checkbox"; cb.id = "vl-on-" + f.id; cb.checked = d.on;
    cb.onchange = () => { d.on = cb.checked; changed(); };
    sw.append(cb, el("span", "", T("vl.load")));
    const opts = catalog(f.id).models.map(m => [m.id, m.name + (m.size ? " (" + T("vl.gb", (m.size / 1e9).toFixed(1)) + ")" : "") + (m.installed ? "" : " - " + T("vl.notinstalled"))]);
    if (!opts.some(o => o[0] === d.model)) opts.push([d.model, f.name]);
    const model = field("vl-model-" + f.id, T("vl.model"), select(opts, d.model, v => { d.model = v; changed(); }));
    const dev = field("vl-device-" + f.id, T("vo.device"), select(DEVICES.concat(DEVICES.includes(d.device) ? [] : [d.device]).map(x => [x, x === "auto" ? T("vo.device.auto") : x === "cpu" ? T("vo.device.cpu") : T("vo.device.gpu", x.split(":")[1])]), d.device,
      v => { d.device = v; if (v === "cpu") d.memory = "ram"; changed(true); }));
    const mem = field("vl-memory-" + f.id, T("vo.memory"), select([["vram", T("vo.memory.vram")], ["ram", T("vo.memory.ram")]], d.memory,
      v => { d.memory = v; if (v === "vram" && d.device === "cpu") d.device = "auto"; changed(true); }));
    const line = el("p", "vl-help");
    line.id = "vl-est-" + f.id;
    fs.append(lg, sw, model, el("div", "vl-place"), line, el("p", "vl-help vl-bad"));
    fs.querySelector(".vl-place").append(dev, mem);
    fs.lastChild.id = "vl-warn-" + f.id;
    return fs;
  }
  function changed(repaint) {
    if (repaint) paintDialog(); else paintPlan();
    queuePlan();
  }
  function paintDialog() {
    const focus = document.activeElement && dlgBody.contains(document.activeElement) ? document.activeElement.id : "";
    const bar = el("div", "vl-bar");
    const apply = btn(T("vl.apply"), "primary", confirmApply, "vl-apply");
    bar.append(apply, btn(T("vl.close"), "", () => dlg.close(), "vl-close"));
    const sum = el("div", "vl-plan");
    sum.id = "vl-plan";
    sum.setAttribute("aria-live", "polite");
    dlgBody.replaceChildren(heading("vl-dlg", T("vl.dlg.title")), el("p", "vl-help", T("vl.dlg.help")), ...S.functions.map(fnFieldset), sum, bar);
    paintPlan();
    if (focus) { const f = document.getElementById(focus); if (f) f.focus(); }
  }
  function names(items, acts) { return items.filter(i => acts.includes(i.action)).map(i => NAME[i.id]()).join(", "); }
  function paintPlan() {
    const sum = document.getElementById("vl-plan"), apply = document.getElementById("vl-apply");
    if (!sum) return;
    S.functions.forEach(f => {
      const i = plan && plan.items.find(x => x.id === f.id), line = document.getElementById("vl-est-" + f.id), warn = document.getElementById("vl-warn-" + f.id);
      if (!line) return;
      line.textContent = i ? T("vl.estimate") + ": " + estimateText(i) : "";
      warn.textContent = i && i.action === "missing" ? i.reason : "";
      warn.hidden = !warn.textContent;
    });
    sum.replaceChildren();
    if (planErr) { const n = el("p", "vl-note", planErr); n.setAttribute("role", "alert"); sum.append(n); apply.disabled = true; return; }
    if (!plan) { sum.append(el("p", "vl-help", T("vl.planning"))); apply.disabled = true; return; }
    sum.append(el("h3", "vl-h2", T("vl.plan")));
    const loads = names(plan.items, ["load", "reload"]), unloads = names(plan.items, ["unload"]);
    if (!plan.changes) sum.append(el("p", "vl-line", T("vl.nochange")));
    if (loads) sum.append(el("p", "vl-line", T("vl.p.loads", loads)));
    if (unloads) sum.append(el("p", "vl-line", T("vl.p.unloads", unloads)));
    const dl = el("dl", "vl-facts");
    row(dl, T("vl.free"), gb(plan.free_mib));
    row(dl, T("vl.p.need"), plan.unknown.length ? T("vl.need.partial", gb(plan.need_mib)) : gb(plan.need_mib));
    row(dl, T("vl.p.recover"), plan.recover_known ? gb(plan.recover_mib) : T("vl.need.partial", gb(plan.recover_mib)));
    row(dl, T("vl.reserve"), gb(plan.reserve_mib));
    row(dl, T("vl.left"), gb(plan.left_for_llm_mib));
    sum.append(dl);
    if (plan.llm_reload) sum.append(el("p", "vl-line", T("vl.p.reload")));
    else if (plan.reload_suggested) sum.append(el("p", "vl-line", T("vl.p.noreload")));
    if (plan.fits === false) { const n = el("p", "vl-note", T("vl.p.nofit")); n.setAttribute("role", "alert"); sum.append(n); }
    else if (plan.fits === null && plan.need_mib) sum.append(el("p", "vl-help", T("vl.p.unknown")));
    apply.disabled = !plan.changes || plan.fits === false || !S.service.running;
  }

  function confirmDialog(id, title, lines, okText, onOk, from) {
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
    m.body.append(heading(id, title), ...lines.map(t => el("p", "vl-line", t)), msg, bar);
    m.d.addEventListener("close", () => m.d.remove());
    openDialog(m.d, from);
    document.getElementById(id + "-cancel").focus();                       // the safe choice has the focus
  }
  function confirmApply() {
    const lines = [];
    const loads = names(plan.items, ["load", "reload"]), unloads = names(plan.items, ["unload"]);
    if (loads) lines.push(T("vl.p.loads", loads));
    if (unloads) lines.push(T("vl.p.unloads", unloads));
    lines.push(plan.llm_reload ? T("vl.c.reload", gb(plan.left_for_llm_mib)) : plan.reload_suggested ? T("vl.c.noreload") : T("vl.c.noreload2"));
    confirmDialog("vl-confirm", T("vl.c.title"), lines, T("vl.c.ok"), async () => {
      const r = await api("POST", "api/voice/functions/apply", { desired: draft, confirm: true });
      if (!r.ok) return errText(r);
      dlg.close();
      const tab = document.getElementById("sub-voicesvc");
      tab.click();
      tab.focus();
      await refresh();
      return "";
    }, document.getElementById("vl-apply"));
  }
  function confirmReload() {
    const p = S.pending_reload, v = S.vram;
    const lines = [T("vl.r.done", (p.removed || []).map(f => NAME[f] ? NAME[f]() : f).join(", "), gb(p.recovered_mib)),
      T("vl.r.reload", gb(v.free_mib), gb(v.reserve_mib))];
    confirmDialog("vl-rconfirm", T("vl.r.title"), lines, T("vl.r.ok"), async () => {
      const r = await api("POST", "api/voice/functions/reload-llm", { confirm: true });
      if (!r.ok) return errText(r);
      await refresh();
      return "";
    }, reloadBtn.hidden ? openBtn : reloadBtn);
  }

  /* ---- wiring ---- */
  openBtn.onclick = openMain;
  reloadBtn.onclick = confirmReload;
  document.addEventListener("tadamoo:view", e => { if (e.detail === "loader") refresh(); else clearTimeout(timer); });
  document.addEventListener("basalt:models-sub", e => { if (e.detail === "voicesvc") refresh(); });
  document.addEventListener("tadamoo:lang", () => { if (S) paint(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && shown()) refresh(); });
  if (document.body.dataset.view === "loader") refresh();
})();
