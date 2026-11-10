"use strict";
/* Settings > Voice: one card per helper service (diarization, voice activity, embeddings). A card chooses the model (engine, weight, license,
   languages), where it is loaded (device, VRAM or RAM) and says whether it is installed; a model that is not installed offers "Download and
   install" (voicedl.js). Speech to text, speech synthesis and the avatar are not here: they are set in Models > Voice services, the one place
   where the voice functions are chosen and loaded. The models folder is set here; changing it moves nothing. Everything the server says
   enters the page as text. */
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
  const FUNCTIONS = ["stt", "tts", "avatar"];                  // chosen and loaded in Models > Voice services
  let S = null, loadFailed = "";
  const ui = {};                                      // per service: the line under the card
  const st = id => ui[id] || (ui[id] = { msg: "", bad: false });
  let dirMsg = { text: "", bad: false };
  let TEL = null, telMsg = { text: "", bad: false }, telLive = { pending: [], rows: [], down: false }, telTimer = 0;
  const TEL_POLL_MS = 3000;

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

  const dl = VoiceDownload({ models: () => S, ids: ["diarization", "vad", "embedding"], root: host, runId: "vo-run", repaint: () => paint(), refresh: () => refresh(), active });

  /* ---- loading and drawing ---- */
  async function refresh() {
    const r = await api("GET", "api/voice/models");
    if (r.ok) { S = r.data; loadFailed = ""; } else loadFailed = errText(r);
    const t = await api("GET", "api/voice/telephony");
    if (t.ok) TEL = t.data;
    await loadTelLive();
    paint();
    return r.ok;
  }
  function paint() {
    const focus = document.activeElement && host.contains(document.activeElement) ? document.activeElement.id : "";
    if (!S) { host.replaceChildren(el("p", "vo-intro", loadFailed || T("vo.loading"))); return; }
    const list = el("div", "vo-list");
    S.services.filter(svc => !FUNCTIONS.includes(svc.id)).forEach(svc => list.append(card(svc)));
    host.replaceChildren(el("p", "vo-intro", T("vo.intro")), functionsBox(), folderBox(), dl.runBox(), list, telephonyBox());
    host.append(importBox());
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

  /* ---- the voice functions are not set here ---- */
  function functionsBox() {
    const box = el("div", "vo-box");
    box.append(el("p", "vo-help", T("vo.functions.moved")),
      btn(T("vl.open.tab"), "ghost", () => { view("loader"); document.getElementById("sub-voicesvc").click(); }, "vo-open-functions"));
    return box;
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
    const own = svc.models.filter(x => x.selectable !== false);        // a file for the phones is not a model the function loads
    if (!own.length) { box.append(el("p", "vo-help", T("vo.none"))); return box; }
    const m = mdl(svc, svc.selected) || own[0];
    const pickModel = select(own.map(x => [x.id, x.name + (x.size ? " (" + gb(x.size) + ")" : "")]), m.id, async id => {
      const r = await api("POST", "api/voice/select", { service: svc.id, model: id });
      if (r.ok) { S = r.data; s.plan = null; s.ack = s.nc = false; s.msg = ""; } else { s.msg = errText(r); s.bad = true; }
      paint();
    });
    box.append(field("vo-model-" + svc.id, T("vo.model"), pickModel));
    const state = el("div", "vo-state");
    const parts = m.parts && Array.isArray(m.parts.installed) ? m.parts : null;                    // a set of independent voices (Piper): each counts
    const partial = !!parts && parts.installed.length > 0 && parts.installed.length < parts.total;
    state.append(chip(partial ? T("vo.state.partial", String(parts.installed.length), String(parts.total)) : (m.installed ? T("vo.state.installed") : T("vo.state.missing")), m.installed ? "vo-ok" : "vo-off"));
    if (m.noncommercial) state.append(chip(T("vo.noncommercial"), "vo-warn"));
    if (m.verify) state.append(chip(T("vo.verify"), "vo-warn"));
    box.append(state, facts(m), el("p", "vo-help", pick(m.notes)));
    if (m.verify) box.append(el("p", "vo-help", T("vo.verify.help", m.verify_fields.map(f => (VERIFY[f] || (() => f))()).join(", "))));
    for (const x of svc.models.filter(y => y.selectable === false)) {
      const line = el("p", "vo-help vo-phone-file", T("vo.phones.file", x.name, x.installed ? T("vo.state.installed") : T("vo.state.missing")));
      line.append(" " + pick(x.notes) + " " + T("vo.noncommercial") + (x.verify ? ", " + T("vo.verify") : "") + ".");
      box.append(line);
    }
    box.append(placement(svc));
    if (!m.installed || partial) box.append(dl.part(svc, m));
    box.append(note(s.msg, s.bad));
    return box;
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

  /* ---- phone calls: the rules, the calls a program asked for, the recent log ---- */
  const TRANSPORTS = { off: () => T("vo.tel.transport.off"), auto: () => T("vo.tel.transport.auto"), gsm: () => T("vo.tel.transport.gsm"), android: () => T("vo.tel.transport.android") };
  const APPROVALS = { manual: () => T("vo.tel.approval.manual"), auto: () => T("vo.tel.approval.auto") };
  async function serviceGet(path) { return api("POST", "api/voice/service/call", { method: "GET", path }); }
  async function loadTelLive() {
    const [a, l] = await Promise.all([serviceGet("/call/approvals"), serviceGet("/call/audit")]);
    telLive = { pending: a.ok && Array.isArray(a.data.pending) ? a.data.pending : [], rows: l.ok && Array.isArray(l.data.rows) ? l.data.rows : [], down: !a.ok };
  }
  async function saveTel(change) {
    const r = await api("POST", "api/voice/telephony", change);
    if (r.ok) { TEL = r.data; telMsg = { text: change.on_behalf_of !== undefined || change.contacts !== undefined ? T("vo.tel.saved") : "", bad: false }; } else telMsg = { text: errText(r), bad: true };
    paint();
  }
  function telSelect(id, label, options, value, key) {
    return field(id, label, select(options, value, v => saveTel({ [key]: v })));
  }
  function telCheck(id, text, checked, key) {
    const row = el("label", "vo-check"), input = el("input");
    input.type = "checkbox"; input.id = id; input.checked = checked;
    input.onchange = () => saveTel({ [key]: input.checked });
    row.append(input, el("span", "", text));
    return row;
  }
  function telephonyBox() {
    const box = el("section", "vo-card vo-tel"), h = el("h3", "vo-h", T("vo.tel.title"));
    box.id = "vo-tel"; h.id = "vo-tel-h";
    box.setAttribute("aria-labelledby", h.id);
    box.append(h, el("p", "vo-help", T("vo.tel.intro")));
    if (!TEL) { box.append(note(telMsg.text, telMsg.bad)); return box; }
    box.append(telSelect("vo-tel-transport", T("vo.tel.transport"), Object.keys(TRANSPORTS).map(v => [v, TRANSPORTS[v]()]), TEL.transport, "transport"));
    box.append(telSelect("vo-tel-approval", T("vo.tel.approval"), Object.keys(APPROVALS).map(v => [v, APPROVALS[v]()]), TEL.outgoing_approval, "outgoing_approval"),
      el("p", "vo-help", T("vo.tel.approval.help")));
    if (TEL.outgoing_approval === "auto") box.append(note(T("vo.tel.approval.warn"), true));
    box.append(telCheck("vo-tel-contacts-only", T("vo.tel.contacts.only"), TEL.auto_contacts_only, "auto_contacts_only"), el("p", "vo-help", T("vo.tel.contacts.only.help")));
    box.append(telCheck("vo-tel-disclosure", T("vo.tel.disclosure"), TEL.ai_disclosure_on_start, "ai_disclosure_on_start"));
    if (!TEL.ai_disclosure_on_start) box.append(note(T("vo.tel.disclosure.warn"), true));
    box.append(telCheck("vo-tel-answer", T("vo.tel.answer"), TEL.incoming_auto_answer, "incoming_auto_answer"), el("p", "vo-help", T("vo.tel.answer.help")));
    if (TEL.incoming_auto_answer) box.append(note(T("vo.tel.answer.warn"), true));
    box.append(telCheck("vo-tel-phone", T("vo.tel.phone"), TEL.phone_calls, "phone_calls"), el("p", "vo-help", T("vo.tel.phone.help")));
    if (TEL.phone_calls) box.append(note(T("vo.tel.phone.warn"), true));
    const who = el("input"), form = el("div", "vo-form");
    who.type = "text"; who.value = TEL.on_behalf_of; who.maxLength = 80; who.autocomplete = "off";
    form.append(field("vo-tel-behalf", T("vo.tel.behalf"), who), btn(T("vo.tel.save"), "primary", () => saveTel({ on_behalf_of: who.value.trim() }), "vo-tel-save"));
    box.append(form, listsForm(), note(telMsg.text, telMsg.bad), telLiveBox());
    return box;
  }
  const linesOf = area => area.value.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
  function listsForm() {
    const form = el("div", "vo-form vo-lists"), contacts = el("textarea"), prefixes = el("textarea");
    contacts.rows = 4; contacts.value = TEL.contacts.join("\n"); contacts.autocomplete = "off"; contacts.spellcheck = false;
    prefixes.rows = 6; prefixes.value = TEL.premium_prefixes.join("\n"); prefixes.autocomplete = "off"; prefixes.spellcheck = false;
    form.append(field("vo-tel-contacts", T("vo.tel.contacts"), contacts), el("p", "vo-help", T("vo.tel.contacts.help")),
      field("vo-tel-premium", T("vo.tel.premium"), prefixes), el("p", "vo-help", T("vo.tel.premium.help")),
      btn(T("vo.tel.save"), "primary", () => saveTel({ contacts: linesOf(contacts), premium_prefixes: linesOf(prefixes) }), "vo-tel-lists-save"));
    if (!TEL.premium_prefixes.length) form.append(note(T("vo.tel.premium.warn"), true));
    return form;
  }
  const word = (prefix, value) => { const s = T(prefix + value); return s === prefix + value ? String(value || "") : s; };
  function telLiveBox() {
    const wrap = el("div", "vo-tel-live");
    wrap.id = "vo-tel-live";
    if (telLive.down) wrap.append(note(T("vo.tel.down"), false));
    wrap.append(el("h4", "vo-h2", T("vo.tel.pending")));
    if (!telLive.pending.length) wrap.append(el("p", "vo-help", T("vo.tel.pending.none")));
    const list = el("ul", "vo-tel-rows");
    telLive.pending.forEach(p => {
      const li = el("li", "");
      li.append(el("p", "vo-line", T("vo.tel.pending.line", p.number, p.greeting)));
      const bar = el("div", "vo-bar");
      bar.append(btn(T("vo.tel.approve"), "primary", () => decide(p.id, "approve"), "vo-tel-yes-" + p.id), btn(T("vo.tel.deny"), "ghost", () => decide(p.id, "deny"), "vo-tel-no-" + p.id));
      li.append(bar);
      list.append(li);
    });
    wrap.append(list, el("h4", "vo-h2", T("vo.tel.recent")));
    const rows = el("ul", "vo-tel-rows");
    telLive.rows.slice(-8).reverse().forEach(r => {
      const when = String(r.ts || "").replace("T", " ").replace("+00:00", "");
      const what = word("vo.tel.ev.", r.event) + (r.reason ? " (" + r.reason + ")" : r.outcome ? " (" + r.outcome + ")" : "");
      rows.append(el("li", "", T("vo.tel.row", when, word("vo.tel.dir.", r.direction), r.number || "", r.origin || "", what)));
    });
    if (!rows.children.length) wrap.append(el("p", "vo-help", T("vo.tel.recent.none")));
    wrap.append(rows, btn(T("vo.tel.refresh"), "ghost", async () => { await loadTelLive(); repaintTelLive(); }, "vo-tel-refresh"));
    return wrap;
  }
  function repaintTelLive() {
    const old = document.getElementById("vo-tel-live");
    if (old) old.replaceWith(telLiveBox());
  }
  async function decide(id, verdict) {
    const r = await api("POST", "api/voice/service/call", { method: "POST", path: "/call/approvals/" + id + "/" + verdict });
    telMsg = r.ok ? { text: T("vo.tel.decided"), bad: false } : { text: errText(r), bad: true };
    await loadTelLive();
    paint();
  }
  function pollTel() {
    clearTimeout(telTimer);
    if (!active()) return;
    loadTelLive().then(() => { repaintTelLive(); telTimer = setTimeout(pollTel, TEL_POLL_MS); });
  }

  /* ---- import from callAPIcall: the plan, what is personal, the run ---- */
  const SUGGESTED = "C:\\Projects\\callAPIcall";                        // only a suggestion in the field: nothing is read until the button is pressed
  const IM = { source: SUGGESTED, plan: null, pick: {}, busy: "", msg: { text: "", bad: false }, result: null };
  const IM_KIND = { persona: () => T("vo.im.kind.persona"), voice: () => T("vo.im.kind.voice") };
  const IM_WHY = { memory: () => T("vo.im.why.memory"), "video-or-photo": () => T("vo.im.why.video"), "voice-recording": () => T("vo.im.why.voice") };
  const IM_STATUS = { created: () => T("vo.im.st.created"), updated: () => T("vo.im.st.updated"), skipped: () => T("vo.im.st.skipped"),
    needs_confirmation: () => T("vo.im.st.needs"), failed: () => T("vo.im.st.failed") };
  const imPick = id => IM.pick[id] || (IM.pick[id] = { on: null, replace: false, personal: false });
  const imChosen = it => { const p = imPick(it.id); return it.conflict ? p.replace : p.on === null ? !it.problems.length : p.on; };
  const imReady = () => !!IM.plan && IM.plan.items.some(imChosen) && IM.plan.items.every(it => !imChosen(it) || !it.personal || imPick(it.id).personal);
  async function imLook() {
    IM.busy = "look"; IM.msg = { text: "", bad: false }; IM.result = null; paint();
    const r = await api("POST", "api/voice/import/plan", { source: IM.source.trim() });
    IM.busy = "";
    if (r.ok) { IM.plan = r.data; IM.pick = {}; } else { IM.plan = null; IM.msg = { text: errText(r), bad: true }; }
    paint();
  }
  async function imRun() {
    const items = IM.plan.items.filter(imChosen).map(it => ({ id: it.id, replace: !!it.conflict, confirm_personal: !!(it.personal && imPick(it.id).personal) }));
    IM.busy = "run"; IM.msg = { text: "", bad: false }; paint();
    const r = await api("POST", "api/voice/import/run", { source: IM.plan.source, confirm: true, items });
    IM.busy = "";
    if (r.ok) { IM.result = r.data; IM.plan = null; } else IM.msg = { text: errText(r), bad: true };
    paint();
  }
  function imItem(it) {
    const p = imPick(it.id), box = el("li", "vo-im-item"), id = "vo-im-" + it.id.replace(/[^A-Za-z0-9_-]/g, "_");
    const head = el("div", "vo-state");
    head.append(chip(IM_KIND[it.kind] ? IM_KIND[it.kind]() : it.kind), chip(gb(it.bytes || 0)));
    if (it.personal) head.append(chip(T("vo.im.personal"), "vo-warn"));
    box.append(el("h4", "vo-h2", it.name), head);
    if (it.parts) box.append(el("p", "vo-help", T("vo.im.parts", it.parts.clips.count, it.parts.documents.count, it.parts.memory.sessions)));
    (it.skipped || []).forEach(s => box.append(el("p", "vo-help", T("vo.im.skipped", s.name, gb(s.bytes)))));
    it.notes.forEach(n => box.append(el("p", "vo-help", n)));
    if (it.problems.length) {
      const bad = el("ul", "vo-problems");
      bad.setAttribute("role", "alert");
      it.problems.forEach(x => bad.append(el("li", "", x)));
      box.append(bad);
      return box;
    }
    const check = (cid, text, checked, set) => {
      const row = el("label", "vo-check"), input = el("input");
      input.type = "checkbox"; input.id = cid; input.checked = checked;
      input.onchange = () => { set(input.checked); paint(); };
      row.append(input, el("span", "", text));
      return row;
    };
    if (it.conflict) box.append(el("p", "vo-help", T("vo.im.exists")), check(id + "-replace", T("vo.im.replace"), p.replace, v => { p.replace = v; }));
    else box.append(check(id + "-on", T("vo.im.use", it.name), imChosen(it), v => { p.on = v; }));
    if (it.personal && imChosen(it)) {
      box.append(el("p", "vo-help", T("vo.im.personal.why", it.personal_reasons.map(w => (IM_WHY[w] || (() => w))()).join(", "))),
        check(id + "-personal", T("vo.im.personal.ok"), p.personal, v => { p.personal = v; }));
    }
    return box;
  }
  function importBox() {
    const box = el("section", "vo-card vo-im"), h = el("h3", "vo-h", T("vo.im.title"));
    box.id = "vo-im"; h.id = "vo-im-h";
    box.setAttribute("aria-labelledby", h.id);
    box.append(h, el("p", "vo-help", T("vo.im.intro")));
    const input = el("input"), form = el("div", "vo-form");
    input.type = "text"; input.value = IM.source; input.autocomplete = "off"; input.spellcheck = false;
    input.oninput = () => { IM.source = input.value; };
    const look = btn(IM.busy === "look" ? T("vo.im.looking") : T("vo.im.look"), "primary", imLook, "vo-im-look");
    look.disabled = !!IM.busy;
    form.append(field("vo-im-source", T("vo.im.source"), input), look);
    box.append(form, el("p", "vo-help", T("vo.im.suggest", SUGGESTED)), note(IM.msg.text, IM.msg.bad));
    if (IM.plan) {
      if (!IM.plan.items.length) box.append(el("p", "vo-help", T("vo.im.none")));
      const list = el("ul", "vo-im-items");
      IM.plan.items.forEach(it => list.append(imItem(it)));
      const go = btn(IM.busy === "run" ? T("vo.im.running") : T("vo.im.run"), "primary", imRun, "vo-im-run");
      go.disabled = !imReady() || !!IM.busy;
      box.append(list, go);
      if (!imReady() && IM.plan.items.some(imChosen)) box.append(el("p", "vo-help", T("vo.im.need.confirm")));
    }
    if (IM.result) {
      const list = el("ul", "vo-im-items");
      list.id = "vo-im-result";
      IM.result.results.forEach(r => {
        const li = el("li", "vo-im-item");
        li.append(el("h4", "vo-h2", r.name || r.id), chip(IM_STATUS[r.status] ? IM_STATUS[r.status]() : r.status));
        if (r.message) li.append(el("p", "vo-help", r.message));
        (r.warnings || []).forEach(w => li.append(el("p", "vo-help", w)));
        list.append(li);
      });
      box.append(el("h4", "vo-h2", T("vo.im.result")), list);
    }
    return box;
  }

  /* ---- when the tab is shown ---- */
  document.addEventListener("tadamoo:sub", e => {
    if (e.detail !== "voice") { dl.stop(); clearTimeout(telTimer); return; }
    refresh().then(() => { dl.poll(); pollTel(); });
  });
  document.addEventListener("tadamoo:lang", () => { if (S) paint(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && active()) dl.poll(); });
  if (active()) refresh();
})();
