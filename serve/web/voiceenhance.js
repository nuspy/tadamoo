"use strict";
/* The Avatar acceleration card of Settings > Voice: draw the avatar smaller and with fewer frames, and let this PC restore the size and the smoothness
   before the picture is shown or sent (voice/callbridge/enhance). Every choice the voice service cannot run on this PC is shown disabled with the reason
   in plain text, never left out. The settings are voice.json `avatar.enhance` (GET and POST api/voice/avatar); what this PC can do, the plan and the
   measured cost come from the voice service (GET /avatar/enhance through the proxy). Everything the server says enters the page as text.
   voicetab.js puts the card under the Avatar output card. */
(function () {
  const UP = ["none", "basic", "rtx_video"], IN = ["none", "basic"], FACTORS = [2, 3], ALL_SCALES = [1.5, 2, 3, 4];
  const POLL_MS = 5000;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const clear = node => { while (node.firstChild) node.firstChild.remove(); return node; };
  const msgOf = r => {
    const e = r.data && r.data.error;
    if (typeof e === "string") return e;
    return (e && ((LANG === "it" && e.message_it) || e.message)) || (r.status === 0 ? T("ae.e.offline") : `HTTP ${r.status}`);
  };
  const num = v => (v == null ? T("ae.cost.na") : `${Math.round(v * 10) / 10} ms`);

  let cfg = null, info = null, note = { text: "", bad: false }, timer = null, down = "";

  function providerOf(stage, name) { return info && info.capabilities && info.capabilities[stage] && info.capabilities[stage][name]; }
  function usable(stage, name) {
    if (name === "none") return true;
    const p = providerOf(stage, name);
    return !!(p && p.available);
  }
  function why(stage, name) {
    const p = providerOf(stage, name);
    return p ? (p.reason || T("ae.na", name)) : (down || T("ae.loading"));
  }
  function scalesFor(name) {
    const p = providerOf("upscale", name);
    return p && p.scales && p.scales.length ? p.scales : ALL_SCALES;
  }

  function radios(stage, names, current, group, onPick) {
    const set = el("fieldset", "ae-set"), legend = el("legend", "ae-legend", T(stage === "upscale" ? "ae.up.title" : "ae.in.title"));
    set.append(legend);
    names.forEach(name => {
      const row = el("label", "vt-check ae-opt"), input = el("input");
      input.type = "radio"; input.name = group; input.value = name; input.id = `ae-${group}-${name}`;
      input.checked = current === name;
      input.disabled = !usable(stage, name);
      input.onchange = () => { if (input.checked) onPick(name); };
      row.append(input, el("span", "", T(`ae.${stage === "upscale" ? "up" : "in"}.${name}`)));
      set.append(row);
      if (input.disabled) set.append(Object.assign(el("p", "vt-help ae-why", T("ae.na", why(stage, name))), { id: `ae-${group}-${name}-why` }));
    });
    return set;
  }

  function select(id, labelKey, values, current, disabled, onPick) {
    const wrap = el("div", "vt-field"), label = el("label", "", T(labelKey)), sel = el("select");
    label.htmlFor = id; sel.id = id; sel.disabled = disabled;
    values.forEach(v => { const o = el("option", "", `${v}x`); o.value = String(v); o.selected = Number(v) === Number(current); sel.append(o); });
    sel.onchange = () => onPick(Number(sel.value));
    wrap.append(label, sel);
    return wrap;
  }

  function field(id, labelKey, value, onPick, help, numeric) {
    const wrap = el("div", "vt-field"), label = el("label", "", T(labelKey)), input = el("input");
    label.htmlFor = id; input.id = id; input.type = "text"; input.value = String(value || (numeric ? 0 : ""));
    if (numeric) { input.inputMode = "numeric"; input.autocomplete = "off"; } else { input.autocomplete = "off"; input.spellcheck = false; }
    input.onchange = () => onPick(numeric ? Number(input.value.trim() || 0) : input.value.trim());
    wrap.append(label, input);
    if (help) wrap.append(el("p", "vt-help", help));
    return wrap;
  }

  let parts = null;
  function build(api) {
    const box = el("section", "vt-card"), h = el("h3", "vt-h", T("ae.title"));
    h.id = "vb-h-accel"; box.setAttribute("aria-labelledby", h.id);
    parts = { box, gpu: el("p", "vt-help"), body: el("div", "ae-body"), status: el("div", "ae-status"), note: el("p", "vt-note") };
    parts.note.setAttribute("role", "status"); parts.note.id = "ae-note";
    const refresh = el("button", "btn", T("ae.refresh")); refresh.type = "button"; refresh.id = "ae-refresh";
    refresh.onclick = () => reload(api);
    box.append(h, el("p", "vt-help", T("ae.intro")), parts.gpu, parts.body, parts.status, parts.note, refresh);
    return box;
  }

  async function save(api, change) {
    note = { text: "", bad: false };
    const merged = { ...cfg, ...change };
    if (change.upscale && !scalesFor(merged.upscale).includes(Number(merged.scale))) merged.scale = scalesFor(merged.upscale).includes(2) ? 2 : scalesFor(merged.upscale)[0];
    const body = {};
    Object.keys(merged).forEach(k => { if (merged[k] !== cfg[k]) body[k] = merged[k]; });
    if (!Object.keys(body).length) { paint(api); return; }
    const r = await api("POST", "api/voice/avatar", { enhance: body });
    if (r.ok) { cfg = r.data.enhance; note = { text: T("ae.saved"), bad: false }; await refreshInfo(api); } else note = { text: msgOf(r), bad: true };
    paint(api);
  }

  function paintBody(api) {
    const body = clear(parts.body);
    if (!cfg) return;
    body.append(radios("upscale", UP, cfg.upscale, "up", name => save(api, { upscale: name })),
                select("ae-scale", "ae.scale", scalesFor(cfg.upscale), cfg.scale, cfg.upscale === "none", v => save(api, { scale: v })),
                field("ae-sdk", "ae.sdk", cfg.rtx_video_path, v => save(api, { rtx_video_path: v }), T("ae.sdk.help"), false),
                radios("interpolate", IN, cfg.interpolate, "in", name => save(api, { interpolate: name })),
                select("ae-factor", "ae.factor", FACTORS, cfg.factor, cfg.interpolate === "none", v => save(api, { factor: v })),
                el("p", "ae-legend", T("ae.limits")),
                field("ae-fpscap", "ae.fpscap", cfg.worker_fps_cap, v => save(api, { worker_fps_cap: v }), "", true),
                field("ae-hcap", "ae.hcap", cfg.worker_height_cap, v => save(api, { worker_height_cap: v }), "", true));
  }

  function line(text, id) { const p = el("p", "vt-help", text); if (id) p.id = id; return p; }
  function paintStatus() {
    if (!parts) return;
    const box = clear(parts.status), st = info && info.status, gpu = info && info.capabilities && info.capabilities.gpu;
    parts.gpu.textContent = gpu ? (gpu.vendor === "nvidia" ? T("ae.gpu", gpu.name, gpu.driver || "?") : T("ae.gpu.none", gpu.note || "")) : (down || T("ae.loading"));
    parts.gpu.id = "ae-gpu";
    if (!st) return;
    const p = st.plan;
    if (!p.active) box.append(line(T("ae.plan.off"), "ae-plan"));
    else if (p.worker_height && p.worker_fps) box.append(line(T("ae.plan", p.worker_height, p.worker_fps, p.out_height, p.out_fps), "ae-plan"));
    else if (p.worker_height) box.append(line(T("ae.plan.size", p.worker_height, p.out_height), "ae-plan"));
    else box.append(line(T("ae.plan.rate", p.worker_fps, p.out_fps), "ae-plan"));
    ["upscale", "interpolate"].forEach(stage => {
      const s = st[stage];
      if (s && !s.running) box.append(line(`${T(stage === "upscale" ? "ae.up.title" : "ae.in.title")}: ${T("ae.stage.off", s.reason || "?")}`, `ae-${stage}-off`));
    });
    if (st.restart_needed) box.append(line(T("ae.restart"), "ae-restart"));
    const t = st.timing;
    if (t && t.total_ms != null) {
      box.append(line(T("ae.cost", num(st.upscale && st.upscale.ms), num(st.interpolate && st.interpolate.ms), num(t.convert_ms), num(t.total_ms), num(t.budget_ms)), "ae-cost"),
                 line(T(t.over_budget ? "ae.cost.over" : "ae.cost.ok"), "ae-budget"));
    } else if (p.active) box.append(line(T("ae.cost.none"), "ae-cost"));
  }

  function paint(api) {
    if (!parts) return;
    const active = document.activeElement && document.activeElement.id;
    paintBody(api);
    paintStatus();
    parts.note.textContent = note.text; parts.note.hidden = !note.text; parts.note.dataset.bad = note.bad ? "1" : "0";
    const again = active && document.getElementById(active);
    if (again && parts.box.contains(again) && !again.disabled) again.focus();
  }

  async function refreshInfo(api) {
    const r = await api("POST", "api/voice/service/call", { method: "GET", path: "/avatar/enhance" });
    if (r.ok) { info = r.data; down = ""; } else { info = null; down = msgOf(r); }
  }

  async function reload(api) {
    const [mine] = await Promise.all([api("GET", "api/voice/avatar"), refreshInfo(api)]);
    if (mine.ok) cfg = mine.data.enhance; else note = { text: msgOf(mine), bad: true };
    paint(api);
  }

  function settingsCard(api) {
    if (timer) { clearInterval(timer); timer = null; }
    const box = build(api);
    paint(api);
    reload(api);
    timer = setInterval(async () => {
      if (!box.isConnected) { clearInterval(timer); timer = null; return; }
      if (document.hidden) return;
      await refreshInfo(api);
      paintStatus();
    }, POLL_MS);
    return box;
  }

  window.VoiceEnhance = { settingsCard };
})();
