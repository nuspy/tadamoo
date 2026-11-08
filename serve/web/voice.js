"use strict";
/* Settings > Voice: one card per voice service (speech to text, speech synthesis, avatar, diarization, voice activity, embeddings). A card
   chooses the model (engine, weight, license, languages), where it is loaded (device, VRAM or RAM) and says whether it is installed. A model
   that is not installed offers "Download and install", which opens the plan (size, free disk, license) and starts only after the license
   is accepted and the person confirms. The models folder is set here; changing it moves nothing. Everything the server says enters the
   page as text. */
(function () {
  const host = document.getElementById("vo-root");
  if (!host) return;

  const POLL_MS = 1000, GIB = 1e9, MIB = 1e6;
  const SERVICES = {
    stt: () => T("vo.svc.stt"), tts: () => T("vo.svc.tts"), avatar: () => T("vo.svc.avatar"),
    diarization: () => T("vo.svc.diarization"), vad: () => T("vo.svc.vad"), embedding: () => T("vo.svc.embedding"),
  };
  const VERIFY = {
    license: () => T("vo.vf.license"), size: () => T("vo.vf.size"), sha256: () => T("vo.vf.sha256"), source: () => T("vo.vf.source"),
    "license-card": () => T("vo.vf.licensecard"), "face-parse": () => T("vo.vf.faceparse"),
  };
  const DEVICES = ["auto", "cpu", "cuda:0", "cuda:1"];
  const FUNCTIONS = ["stt", "tts", "avatar"];                  // loaded and unloaded together in Models > Voice services
  let S = null, loadFailed = "", timer = 0;
  const ui = {};                                      // per service: the plan shown, the acknowledgements, the line under the card
  const st = id => ui[id] || (ui[id] = { plan: null, planErr: "", checking: false, ack: false, nc: false, msg: "", bad: false });
  let dirMsg = { text: "", bad: false };

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick, id) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; if (id) b.id = id; return b; };
  const pick = o => (o && (LANG === "it" ? o.it : o.en)) || (o && o.en) || "";
  const errText = r => r.status === 0 ? T("vo.e.offline") : (r.data && r.data.error && (LANG === "it" && r.data.error.message_it ? r.data.error.message_it : r.data.error.message)) || `HTTP ${r.status}`;
  const active = () => document.body.dataset.view === "settings" && !document.getElementById("view-settings").hidden && !document.getElementById("vo-section").hidden;
  const gb = n => n >= GIB / 10 ? T("vo.size.gb", (n / GIB).toFixed(1)) : T("vo.size.mb", Math.max(1, Math.round(n / MIB)));
  const mdl = (svc, id) => svc.models.find(m => m.id === id);

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  /* ---- loading and drawing ---- */
  async function refresh() {
    const r = await api("GET", "api/voice/models");
    if (r.ok) { S = r.data; loadFailed = ""; } else loadFailed = errText(r);
    paint();
    return r.ok;
  }
  function paint() {
    const focus = document.activeElement && host.contains(document.activeElement) ? document.activeElement.id : "";
    if (!S) { host.replaceChildren(el("p", "vo-intro", loadFailed || T("vo.loading"))); return; }
    const list = el("div", "vo-list");
    S.services.forEach(svc => list.append(card(svc)));
    host.replaceChildren(el("p", "vo-intro", T("vo.intro")), folderBox(), downloadBox(), list);
    if (focus) { const f = document.getElementById(focus); if (f) f.focus(); }
  }
  function note(text, bad) {
    const n = el("p", "vo-note", text);
    n.setAttribute("role", bad ? "alert" : "status");
    n.dataset.bad = bad ? "1" : "";
    n.hidden = !text;
    return n;
  }
  function field(id, label, control) {
    const row = el("div", "vo-field"), lab = el("label", "", label);
    lab.htmlFor = id; control.id = id;
    row.append(lab, control);
    return row;
  }
  function select(options, value, onchange) {
    const sel = el("select");
    options.forEach(([v, text]) => { const o = el("option", "", text); o.value = v; sel.append(o); });
    sel.value = value;
    sel.onchange = () => onchange(sel.value);
    return sel;
  }

  /* ---- the models folder ---- */
  function folderBox() {
    const box = el("div", "vo-box"), input = el("input");
    input.type = "text"; input.value = S.models_dir; input.autocomplete = "off"; input.spellcheck = false;
    const save = btn(T("vo.dir.save"), "primary", async () => {
      const r = await api("POST", "api/voice/models-dir", { path: input.value.trim() });
      if (r.ok) { S = r.data; dirMsg = { text: T("vo.dir.saved"), bad: false }; } else dirMsg = { text: errText(r), bad: true };
      paint();
    }, "vo-dir-save");
    const form = el("div", "vo-form");
    form.append(field("vo-dir", T("vo.dir.label"), input), save);
    box.append(el("h3", "vo-h", T("vo.dir.title")), form, el("p", "vo-help", T("vo.dir.help")), el("p", "vo-help", T("vo.dir.default", S.default_models_dir)), note(dirMsg.text, dirMsg.bad));
    return box;
  }

  /* ---- a service ---- */
  function chip(text, cls) { return el("span", "vo-chip " + (cls || ""), text); }
  function facts(m) {
    const dl = el("dl", "vo-facts");
    const add = (k, v) => { dl.append(el("dt", "", k), el("dd", "", v)); };
    add(T("vo.engine"), m.engine);
    add(T("vo.weight"), m.size ? gb(m.size) : T("vo.size.unknown"));
    add(T("vo.license"), m.license.name);
    add(T("vo.languages"), m.languages.length ? m.languages.join(", ") : T("vo.lang.none"));
    add(T("vo.env"), pick(m.venv));
    return dl;
  }
  function card(svc) {
    const s = st(svc.id), box = el("section", "vo-card");
    box.id = "vo-card-" + svc.id;
    const h = el("h3", "vo-h", SERVICES[svc.id]());
    h.id = "vo-h-" + svc.id;
    box.setAttribute("aria-labelledby", h.id);
    box.append(h);
    if (!svc.models.length) { box.append(el("p", "vo-help", T("vo.none"))); return box; }
    const m = mdl(svc, svc.selected) || svc.models[0];
    const pickModel = select(svc.models.map(x => [x.id, x.name + (x.size ? " (" + gb(x.size) + ")" : "")]), m.id, async id => {
      const r = await api("POST", "api/voice/select", { service: svc.id, model: id });
      if (r.ok) { S = r.data; s.plan = null; s.ack = s.nc = false; s.msg = ""; } else { s.msg = errText(r); s.bad = true; }
      paint();
    });
    box.append(field("vo-model-" + svc.id, T("vo.model"), pickModel));
    const state = el("div", "vo-state");
    state.append(chip(m.installed ? T("vo.state.installed") : T("vo.state.missing"), m.installed ? "vo-ok" : "vo-off"));
    if (m.noncommercial) state.append(chip(T("vo.noncommercial"), "vo-warn"));
    if (m.verify) state.append(chip(T("vo.verify"), "vo-warn"));
    box.append(state, facts(m), el("p", "vo-help", pick(m.notes)));
    if (m.verify) box.append(el("p", "vo-help", T("vo.verify.help", m.verify_fields.map(f => (VERIFY[f] || (() => f))()).join(", "))));
    box.append(placement(svc));
    if (FUNCTIONS.includes(svc.id)) box.append(enabledRow(svc));
    if (!m.installed) box.append(downloadPart(svc, m));
    box.append(note(s.msg, s.bad));
    return box;
  }
  function enabledRow(svc) {
    const wrap = el("div", "vo-dl"), row = el("label", "vo-check"), input = el("input");
    input.type = "checkbox"; input.id = "vo-on-" + svc.id; input.checked = !!svc.enabled;
    input.onchange = async () => {
      const r = await api("POST", "api/voice/enabled", { service: svc.id, enabled: input.checked });
      const s = st(svc.id);
      if (r.ok) { S = r.data; s.msg = ""; } else { s.msg = errText(r); s.bad = true; }
      paint();
    };
    row.append(input, el("span", "", T("vl.enabled")));
    const go = btn(T("vl.open.tab"), "ghost", () => { view("loader"); document.getElementById("sub-voicesvc").click(); }, "vo-open-" + svc.id);
    wrap.append(row, el("p", "vo-help", T("vl.enabled.help")), go);
    return wrap;
  }
  function placement(svc) {
    const p = svc.placement, row = el("div", "vo-place");
    const devices = DEVICES.includes(p.device) ? DEVICES : DEVICES.concat([p.device]);
    const change = async body => {
      const r = await api("POST", "api/voice/placement", { service: svc.id, device: p.device, memory: p.memory, ...body });
      const s = st(svc.id);
      if (r.ok) { S = r.data; s.msg = ""; } else { s.msg = errText(r); s.bad = true; }
      paint();
    };
    const dev = select(devices.map(d => [d, d === "auto" ? T("vo.device.auto") : d === "cpu" ? T("vo.device.cpu") : T("vo.device.gpu", d.split(":")[1])]), p.device,
      d => change(d === "cpu" ? { device: d, memory: "ram" } : { device: d }));
    const mem = select([["vram", T("vo.memory.vram")], ["ram", T("vo.memory.ram")]], p.memory, v => change(v === "vram" && p.device === "cpu" ? { device: "auto", memory: v } : { memory: v }));
    row.append(field("vo-device-" + svc.id, T("vo.device"), dev), field("vo-memory-" + svc.id, T("vo.memory"), mem));
    return row;
  }

  /* ---- download: plan, license, confirm ---- */
  function downloadPart(svc, m) {
    const s = st(svc.id), box = el("div", "vo-dl");
    const busy = S.download && ["running", "verifying"].includes(S.download.phase);
    if (!m.downloadable) { box.append(el("p", "vo-help", T("vo.dl.unavailable"))); return box; }
    if (!s.plan && !s.checking) {
      const b = btn(T("vo.dl.button"), "primary", () => openPlan(svc, m), "vo-dl-open-" + svc.id);
      b.disabled = busy;
      box.append(b);
      if (busy) box.append(el("p", "vo-help", T("vo.dl.busy")));
      if (s.planErr) box.append(note(s.planErr, true));
      return box;
    }
    if (s.checking) { box.append(el("p", "vo-help", T("vo.dl.checking"))); return box; }
    box.append(planView(svc, m, s));
    return box;
  }
  async function openPlan(svc, m) {
    const s = st(svc.id);
    s.checking = true; s.planErr = ""; s.ack = s.nc = false;
    paint();
    const r = await api("GET", "api/voice/download/plan?id=" + encodeURIComponent(m.id));
    s.checking = false;
    if (r.ok) s.plan = r.data; else s.planErr = errText(r);
    paint();
  }
  function planView(svc, m, s) {
    const p = s.plan, box = el("div", "vo-plan");
    box.append(el("h4", "vo-h2", T("vo.dl.plan", m.name)),
      el("p", "vo-line", T("vo.dl.files", p.files.length, gb(p.remaining_bytes), gb(p.total_bytes))),
      el("p", "vo-line", T("vo.dl.dest", p.folder)));
    if (p.free_bytes != null) box.append(el("p", "vo-line", T("vo.dl.free", gb(p.free_bytes))));
    const lic = el("div", "vo-license");
    lic.append(el("h5", "vo-h3", T("vo.dl.license", p.license.name)), el("p", "vo-line", pick(p.license)));
    box.append(lic);
    if (p.problems.length) {
      const bad = el("div", "vo-problems");
      bad.setAttribute("role", "alert");
      bad.append(el("h5", "vo-h3", T("vo.dl.problems")));
      const ul = el("ul", "");
      p.problems.forEach(x => ul.append(el("li", "", (LANG === "it" ? x.text_it : x.text) || x.text)));
      bad.append(ul);
      box.append(bad);
    }
    const checks = el("div", "vo-checks"), ready = p.ok;
    const check = (id, text, key) => {
      const row = el("label", "vo-check"), input = el("input");
      input.type = "checkbox"; input.id = id; input.checked = s[key]; input.disabled = !ready;
      input.onchange = () => { s[key] = input.checked; paint(); };
      row.append(input, el("span", "", text));
      return row;
    };
    checks.append(check("vo-ack-" + svc.id, T("vo.dl.ack"), "ack"));
    if (p.noncommercial) checks.append(check("vo-nc-" + svc.id, T("vo.dl.nc"), "nc"));
    box.append(checks, el("p", "vo-help", T("vo.dl.nothing")));
    const bar = el("div", "vo-bar");
    const go = btn(T("vo.dl.confirm"), "primary", () => confirm(svc, m), "vo-dl-go-" + svc.id);
    go.disabled = !ready || !s.ack || (p.noncommercial && !s.nc);
    bar.append(go, btn(T("vo.dl.cancel"), "ghost", () => { s.plan = null; s.ack = s.nc = false; paint(); }, "vo-dl-cancel-" + svc.id));
    box.append(bar);
    return box;
  }
  async function confirm(svc, m) {
    const s = st(svc.id), p = s.plan;
    const r = await api("POST", "api/voice/download", { id: m.id, token: p.token, confirm: true, ack: s.ack, noncommercial: s.nc });
    if (r.ok) { s.plan = null; s.ack = s.nc = false; s.msg = ""; S.download = r.data; paint(); poll(); return; }
    s.msg = errText(r); s.bad = true;
    if (r.data && r.data.plan) s.plan = r.data.plan;
    paint();
  }

  /* ---- the running download ---- */
  function downloadBox() {
    const d = S.download || {}, box = el("div", "vo-box vo-run");
    box.id = "vo-run";
    const mine = d.id && S.services.some(s => s.models.some(m => m.id === d.id));
    if (!mine || d.phase === "idle") { box.hidden = true; return box; }
    const pct = d.bytes_total ? Math.floor(100 * d.bytes_done / d.bytes_total) : 0;
    const running = d.phase === "running" || d.phase === "verifying";
    box.append(el("h3", "vo-h", running ? T("vo.dl.running", d.name) : d.name));
    if (running) {
      const bar = el("div", "vo-progress");
      bar.setAttribute("role", "progressbar"); bar.setAttribute("aria-valuemin", "0"); bar.setAttribute("aria-valuemax", "100"); bar.setAttribute("aria-valuenow", String(pct));
      bar.setAttribute("aria-label", d.name);
      const fill = el("div", "vo-fill"); fill.style.width = pct + "%";
      bar.append(fill);
      const line = [d.phase === "verifying" ? T("vo.dl.verifying") : T("vo.dl.pct", pct), d.speed_bps > 1 ? T("vo.dl.speed", (d.speed_bps / 1e6).toFixed(1)) : ""].filter(Boolean).join("  ");
      box.append(bar, el("p", "vo-line", line), btn(T("vo.dl.stop"), "ghost", async () => { await api("POST", "api/voice/download/cancel", {}); poll(); }, "vo-dl-stop"));
    } else if (d.phase === "done") box.append(note(T("vo.dl.done"), false));
    else if (d.phase === "cancelled") box.append(note(T("vo.dl.stopped"), false));
    else if (d.phase === "failed") box.append(note(T("vo.dl.failed", d.error ? (LANG === "it" ? d.error.it : d.error.en) : ""), true));
    return box;
  }
  function paintRun() {
    const old = host.querySelector(".vo-run");
    if (old) old.replaceWith(downloadBox()); else paint();
  }
  async function poll() {
    clearTimeout(timer);
    if (!active() || !S) return;
    const r = await api("GET", "api/voice/download/status");
    if (!r.ok) return;
    const was = S.download && S.download.phase;
    S.download = r.data;
    const running = ["running", "verifying"].includes(r.data.phase);
    if (running) { paintRun(); timer = setTimeout(poll, POLL_MS); return; }
    if (["running", "verifying"].includes(was)) await refresh();
  }

  /* ---- when the tab is shown ---- */
  document.addEventListener("tadamoo:sub", e => {
    if (e.detail !== "voice") { clearTimeout(timer); return; }
    refresh().then(() => poll());
  });
  document.addEventListener("tadamoo:lang", () => { if (S) paint(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && active()) poll(); });
  if (active()) refresh();
})();
