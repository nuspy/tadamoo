"use strict";
/* Settings > Voice/Avatar > Phone: the rules for real phone calls the voice service makes or takes (transport, approval, who may be called,
   the disclosure and auto-answer policy) and what is live now (pending approvals, the recent log, who is on the line). Split out of
   voice.js, which keeps only the helper models. The allow-list of numbers here is its own list, not the Contacts tab's: unifying the two
   is a separate question, not done here. Everything the server says enters the page as text. */
(function () {
  const host = document.getElementById("vph-root");
  if (!host) return;

  const POLL_MS = 3000;
  const TRANSPORTS = { off: () => T("vo.tel.transport.off"), auto: () => T("vo.tel.transport.auto"), gsm: () => T("vo.tel.transport.gsm"), android: () => T("vo.tel.transport.android") };
  const APPROVALS = { manual: () => T("vo.tel.approval.manual"), auto: () => T("vo.tel.approval.auto") };
  let TEL = null, telMsg = { text: "", bad: false }, telLive = { pending: [], rows: [], down: false, voice: { active: false } }, telTimer = 0;
  let telVoiceConfirm = { busy: false, msg: "", bad: false, pick: "" }, telContactsCache = null;

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick, id) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; if (id) b.id = id; return b; };
  const errText = r => r.status === 0 ? T("vo.e.offline") : (r.data && r.data.error && (LANG === "it" && r.data.error.message_it ? r.data.error.message_it : r.data.error.message)) || `HTTP ${r.status}`;
  const active = () => document.body.dataset.view === "settings" && !document.getElementById("view-settings").hidden && !document.getElementById("vph-section").hidden;
  const chip = (text, cls) => el("span", "vo-chip " + (cls || ""), text);
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

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  async function serviceGet(path) { return api("POST", "api/voice/service/call", { method: "GET", path }); }

  async function refresh() {
    const t = await api("GET", "api/voice/telephony");
    if (t.ok) { TEL = t.data; telMsg = { text: "", bad: false }; } else telMsg = { text: errText(t), bad: true };
    await loadTelLive();
    paint();
    return t.ok;
  }
  async function loadTelLive() {
    const [a, l, v] = await Promise.all([serviceGet("/call/approvals"), serviceGet("/call/audit"), serviceGet("/call/voice")]);
    telLive = { pending: a.ok && Array.isArray(a.data.pending) ? a.data.pending : [], rows: l.ok && Array.isArray(l.data.rows) ? l.data.rows : [],
      down: !a.ok, voice: v.ok ? v.data : { active: false } };
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

  function paint() {
    const focus = document.activeElement && host.contains(document.activeElement) ? document.activeElement.id : "";
    host.replaceChildren(telephonyBox());
    if (focus) { const f = document.getElementById(focus); if (f) f.focus(); }
  }
  function telephonyBox() {
    const box = el("div", "vo-card vo-tel"), h = el("h3", "vo-h", T("vo.tel.title"));
    box.id = "vo-tel"; h.id = "vo-tel-h";
    box.setAttribute("aria-labelledby", h.id);
    box.append(h, el("p", "vo-help", T("vo.tel.intro")));
    if (!TEL) { box.append(note(telMsg.text, telMsg.bad)); return box; }
    box.append(telSelect("vo-tel-transport", T("vo.tel.transport"), Object.keys(TRANSPORTS).map(v => [v, TRANSPORTS[v]()]), TEL.transport, "transport"));
    box.append(telSelect("vo-tel-approval", T("vo.tel.approval"), Object.keys(APPROVALS).map(v => [v, APPROVALS[v]()]), TEL.outgoing_approval, "outgoing_approval"),
      el("p", "vo-help", T("vo.tel.approval.help")));
    if (window.VoiceArchives) box.append(window.VoiceArchives.picker({ id: "vo-tel-archives", label: T("va.calls.label"), help: T("va.calls.help"), ids: TEL.archives || [],
      onSave: async ids => { const r = await api("POST", "api/voice/telephony", { archives: ids }); if (r.ok) TEL = r.data; } }));       // no repaint: the checklist keeps its focus
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
  function telVoiceBox() {
    const v = telLive.voice || { active: false };
    if (!v.active) return null;
    const box = el("div", "vo-tel-voice");
    const id = v.identity;
    if (id) box.append(el("p", "vo-help", T(id.confident ? "vo.tel.voice.match" : "vo.tel.voice.maybe", id.name)));
    else box.append(el("p", "vo-help", T("vo.tel.voice.none")));
    const row = el("div", "vo-bar");
    const sel = document.createElement("select");
    sel.append(new Option(T("vo.tel.voice.pick"), ""));
    if (telContactsCache) for (const c of telContactsCache) sel.append(new Option(c.name, c.id));
    sel.value = telVoiceConfirm.pick;
    sel.disabled = !telContactsCache || telVoiceConfirm.busy;
    sel.onchange = () => { telVoiceConfirm.pick = sel.value; };
    const go = btn(telVoiceConfirm.busy ? T("vo.tel.voice.busy") : T("vo.tel.voice.confirm"), "", confirmTelVoice, "vo-tel-voice-go");
    go.disabled = telVoiceConfirm.busy || !telVoiceConfirm.pick;
    row.append(sel, go);
    box.append(row, note(telVoiceConfirm.msg, telVoiceConfirm.bad));
    if (telContactsCache === null) loadTelContactsCache();
    return box;
  }
  async function loadTelContactsCache() {
    telContactsCache = [];
    const r = await api("GET", "api/contacts");
    if (r.ok && Array.isArray(r.data.contacts)) telContactsCache = r.data.contacts;
    repaintTelLive();
  }
  async function confirmTelVoice() {
    if (!telVoiceConfirm.pick) return;
    telVoiceConfirm = { ...telVoiceConfirm, busy: true, msg: "", bad: false };
    repaintTelLive();
    const r = await api("POST", "api/voice/service/call", { method: "POST", path: "/call/voice/confirm", body: { contact_id: telVoiceConfirm.pick } });
    telVoiceConfirm = { pick: "", busy: false, msg: r.ok ? T("vo.tel.voice.done") : errText(r), bad: !r.ok };
    repaintTelLive();
  }
  function telLiveBox() {
    const wrap = el("div", "vo-tel-live");
    wrap.id = "vo-tel-live";
    if (telLive.down) wrap.append(note(T("vo.tel.down"), false));
    const vb = telVoiceBox();
    if (vb) wrap.append(vb);
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
  function poll() {
    clearTimeout(telTimer);
    if (!active()) return;
    loadTelLive().then(() => { repaintTelLive(); telTimer = setTimeout(poll, POLL_MS); });
  }

  document.addEventListener("tadamoo:voicesub", e => { if (e.detail === "phone") refresh().then(poll); else clearTimeout(telTimer); });
  document.addEventListener("tadamoo:lang", () => { if (TEL) paint(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && active()) poll(); });
  if (active()) refresh().then(poll);
})();
