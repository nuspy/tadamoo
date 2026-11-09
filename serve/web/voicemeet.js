"use strict";
/* The Meetings card of the Voice tab and the Meetings card of Settings > Voice. The card starts the bot in a meeting (the address and a tick that says
   the person has the right to bring an assistant), shows what it is doing (announcement, consent, minutes so far), lists past meetings with their minutes
   and a Delete that removes the transcript and the minutes, opens the copilot and shows the calendar. The rules of consent are the voice service's:
   this page only asks. Every request goes through the server's list (serve/voice_proxy.py) and everything the server says enters the page as text.
   voicetab.js mounts the card and calls update() on its poll. */
(function () {
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick, id) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; if (id) b.id = id; return b; };
  const clear = node => { while (node.firstChild) node.firstChild.remove(); return node; };
  const sig = v => JSON.stringify(v);
  const warn = text => { const n = el("p", "vt-note", text); n.dataset.bad = "1"; n.setAttribute("role", "alert"); return n; };
  const msgOf = r => {
    const e = r.data && r.data.error;
    if (typeof e === "string") return e;
    return (e && ((LANG === "it" && e.message_it) || e.message)) || (r.status === 0 ? T("vm.e.offline") : `HTTP ${r.status}`);
  };
  const ACTIVE = ["joining", "waiting_admit", "in_call", "leaving"];
  const LIST_EVERY_MS = 10000, CAL_EVERY_MS = 30000;

  let ctx = null, card = null, D = { speak: false, consent_mode: "window", consent_window_s: 45, on_behalf_of: "" };
  let st = { state: "idle" }, cop = { enabled: false, active: false, note: "" }, meetings = [], cal = null, events = [], opened = {}, askDelete = "";
  let joinMsg = { text: "", bad: false }, copMsg = "", rowMsg = {}, calMsg = "", lastList = 0, lastCal = 0, busy = false;
  const ui = {}, sigs = {};
  const changed = (id, v) => { const s = sig(v); if (sigs[id] === s) return false; sigs[id] = s; return true; };

  // ---------------------------------------------------------------- the form
  function field(id, label, input, help) {
    const w = el("div", "vm-field"), l = el("label", "", label);
    l.htmlFor = id; input.id = id;
    w.append(l, input);
    if (help) w.append(el("p", "vt-help", help));
    return w;
  }
  function check(id, label, help, onchange) {
    const w = el("div", "vm-field"), row = el("label", "vt-check"), i = el("input");
    i.type = "checkbox"; i.id = id; i.onchange = onchange;
    row.append(i, el("span", "", label));
    w.append(row);
    if (help) w.append(el("p", "vt-help", help));
    return [w, i];
  }
  function buildForm() {
    const form = el("fieldset", "vm-form"), lg = el("legend", "vt-legend", T("vm.join.title"));
    const url = el("input"); url.type = "url"; url.inputMode = "url"; url.autocomplete = "off"; url.spellcheck = false; url.placeholder = "https://meet.google.com/...";
    url.oninput = syncJoin;
    const [rw, rights] = check("vm-rights", T("vm.rights"), T("vm.rights.help"), syncJoin);
    const [sw, speak] = check("vm-speak", T("vm.speak"), T("vm.speak.help"), syncJoin);
    const mode = el("select");
    for (const m of ["window", "explicit"]) { const o = el("option", "", T("vm.mode." + m)); o.value = m; mode.append(o); }
    const win = el("input"); win.type = "number"; win.inputMode = "numeric"; win.min = "20"; win.max = "300"; win.step = "5";
    const join = btn(T("vm.join"), "primary", doJoin, "vm-join"), leave = btn(T("vm.leave"), "ghost", doLeave, "vm-leave");
    const row = el("div", "vm-actions"); row.append(join, leave);
    const msg = el("p", "vt-note"); msg.id = "vm-join-msg"; msg.hidden = true;
    form.append(lg, field("vm-url", T("vm.url"), url), rw, sw, field("vm-mode", T("vm.mode"), mode, T("vm.mode.help")), field("vm-window", T("vm.window"), win, T("vm.window.help")), row, msg);
    Object.assign(ui, { url, rights, speak, mode, win, join, leave, joinMsg: msg });
    return form;
  }
  function syncJoin() {
    if (!ui.join) return;
    const idle = !ACTIVE.includes(st.state), up = ctx.live().up;
    const w = Number(ui.win.value);
    const why = !up ? T("vm.off.service") : !D.on_behalf_of ? T("vm.off.behalf") : "";
    ui.join.disabled = !idle || !up || !D.on_behalf_of || !ui.url.value.trim() || !ui.rights.checked || !(w >= 20 && w <= 300);
    ui.leave.disabled = idle;
    ui.url.disabled = ui.rights.disabled = ui.speak.disabled = ui.mode.disabled = ui.win.disabled = !idle;
    ui.joinMsg.textContent = joinMsg.text || (idle ? why : "");
    ui.joinMsg.hidden = !ui.joinMsg.textContent;
    ui.joinMsg.dataset.bad = joinMsg.bad || (!joinMsg.text && why) ? "1" : "";
    ui.joinMsg.setAttribute("role", ui.joinMsg.dataset.bad ? "alert" : "status");
  }
  async function doJoin() {
    if (busy) return;
    busy = true; joinMsg = { text: T("vm.joining"), bad: false }; syncJoin();
    const body = { url: ui.url.value.trim(), rights_confirmed: ui.rights.checked === true, speak: ui.speak.checked, consent_mode: ui.mode.value, consent_window_s: Number(ui.win.value) };
    const r = await ctx.svc("POST", "/meetbot/join", body);
    busy = false;
    if (r.ok) { joinMsg = { text: T("vm.joined"), bad: false }; ui.rights.checked = false; await refreshStatus(); } else joinMsg = { text: msgOf(r), bad: true };
    syncJoin();
  }
  async function doLeave() {
    const r = await ctx.svc("POST", "/meetbot/leave");
    joinMsg = r.ok ? { text: T("vm.leaving"), bad: false } : { text: msgOf(r), bad: true };
    await refreshStatus(); syncJoin();
  }

  // ---------------------------------------------------------------- what the bot is doing
  function paintLive() {
    const s = ui.live;
    if (!changed("live", st)) return;
    clear(s);
    s.hidden = st.state === "idle" && !st.end_reason;
    if (s.hidden) return;
    const chips = el("div", "vt-chips");
    chips.append(chip(T("vm.state." + st.state), ACTIVE.includes(st.state) ? "vt-ok" : st.state === "error" ? "vt-warn" : ""));
    if (st.platform) chips.append(chip(st.platform));
    if (st.consent) {
      chips.append(chip(T("vm.consent." + st.consent.state), st.consent.state === "granted" ? "vt-ok" : st.consent.state === "ended" ? "vt-warn" : ""));
      if (st.consent.state === "waiting") chips.append(chip(T("vm.consent.left", st.consent.remaining_s)));
    }
    if (st.announced) chips.append(chip(T("vm.announced", [st.announced.chat ? T("vm.chat") : "", st.announced.voice ? T("vm.voice") : ""].filter(Boolean).join(" + ") || "-")));
    if (st.participants) chips.append(chip(T("vm.people", st.participants)));
    s.append(chips);
    if (st.speak) s.append(el("p", "vt-help", st.speak.on ? (st.speak.blocked ? T("vm.speak.blocked", st.speak.blocked) : T("vm.speak.on")) : T("vm.speak.off")));
    if (st.consent && st.consent.state === "ended") s.append(el("p", "vt-note", T("vm.ended." + (st.consent.outcome || "objection"))));
    if (st.error) s.append(warn(st.error));
    const tail = st.transcript_tail || [];
    if (tail.length) {
      const ol = el("ol", "vm-tail");
      for (const x of tail) ol.append(el("li", "", `${x.speaker || "?"}: ${x.text || ""}`));
      s.append(el("h4", "vm-sub", T("vm.tail")), ol);
    } else if (st.consent && st.consent.state === "waiting") s.append(el("p", "vt-help", T("vm.tail.wait")));
  }
  const chip = (text, cls) => el("span", "vt-chip " + (cls || ""), text);

  // ---------------------------------------------------------------- past meetings
  const when = iso => (iso || "").replace("T", " ").slice(0, 16);
  async function openMeeting(id) {
    if (opened[id]) { delete opened[id]; lastList = 0; return paintList(true); }
    const r = await ctx.svc("GET", `/meetings/${id}`);
    opened[id] = r.ok ? { meta: r.data, tr: null } : null;
    rowMsg[id] = r.ok ? "" : msgOf(r);
    paintList(true);
  }
  async function showTranscript(id) {
    const r = await ctx.svc("GET", `/meetings/${id}/transcript`);
    if (opened[id]) opened[id].tr = r.ok ? (r.data.segments || []) : [];
    rowMsg[id] = r.ok ? "" : msgOf(r);
    paintList(true);
  }
  async function removeMeeting(id) {
    if (askDelete !== id) { askDelete = id; paintList(true); setTimeout(() => { if (askDelete === id) { askDelete = ""; paintList(true); } }, 6000); return; }
    askDelete = "";
    const r = await ctx.svc("DELETE", `/meetings/${id}`);
    rowMsg[id] = r.ok ? "" : msgOf(r);
    if (r.ok) delete opened[id];
    await refreshList(); paintList(true);
  }
  function minutes(m, id) {
    const box = el("div", "vm-min"), a = m.meta.analysis;
    if (m.meta.analysis_state === "analyzing" || m.meta.analysis_state === "pending") box.append(el("p", "vt-help", T("vm.min.wait")));
    if (m.meta.analysis_state === "error") box.append(el("p", "vt-note", T("vm.min.error", m.meta.analysis_error || "")));
    if (a) {
      box.append(el("h4", "vm-sub", T("vm.min.summary")), el("p", "vm-text", a.summary || ""));
      for (const h of a.highlights || []) box.append(el("h5", "vm-sub2", h.title || ""), el("p", "vm-text", h.summary || ""));
      if ((a.agreements || []).length) {
        box.append(el("h4", "vm-sub", T("vm.min.agreements")));
        const ul = el("ul", "vm-list"); for (const x of a.agreements) ul.append(el("li", "", x.detail ? `${x.title}: ${x.detail}` : x.title)); box.append(ul);
      }
      if ((a.tasks || []).length) {
        box.append(el("h4", "vm-sub", T("vm.min.tasks")));
        const by = {}; for (const t of a.tasks) (by[t.assignee || "?"] = by[t.assignee || "?"] || []).push(t.task);
        const ul = el("ul", "vm-list");
        for (const [who, list] of Object.entries(by)) ul.append(el("li", "", `${who}: ${list.join("; ")}`));
        box.append(ul);
      }
    }
    if (m.tr) {
      const ol = el("ol", "vm-tail");
      for (const x of m.tr.slice(0, 400)) ol.append(el("li", "", `${x.speaker || "?"}: ${x.text || ""}`));
      box.append(el("h4", "vm-sub", T("vm.min.transcript")), m.tr.length ? ol : el("p", "vt-help", T("vm.min.none")));
    } else if (!m.meta.deleted) box.append(btn(T("vm.min.show"), "ghost", () => showTranscript(id)));
    return box;
  }
  function paintList(force) {
    const s = ui.list;
    if (!force && !changed("list", [meetings, opened, askDelete, rowMsg])) return;
    sigs.list = sig([meetings, opened, askDelete, rowMsg]);
    clear(s);
    if (!meetings.length) { s.append(el("p", "vt-help", T("vm.past.none"))); return; }
    const ul = el("ul", "vm-meets");
    for (const m of meetings) {
      const li = el("li", "vm-meet"), head = el("div", "vm-meet-h");
      head.append(el("strong", "", m.title || m.id), chip(m.deleted ? T("vm.past.deleted", m.end_reason || "") : T("vm.an." + (m.analysis_state || "none")), m.deleted ? "vt-warn" : m.analysis_state === "done" ? "vt-ok" : ""));
      li.append(head, el("p", "vt-help", [when(m.created), m.platform, m.segments != null ? T("vm.past.lines", m.segments) : "", (m.participants || []).join(", ")].filter(Boolean).join(" - ")));
      const row = el("div", "vm-actions");
      if (!m.deleted) row.append(btn(opened[m.id] ? T("vm.past.close") : T("vm.past.open"), "ghost", () => openMeeting(m.id)));
      row.append(btn(askDelete === m.id ? T("vm.past.confirm") : T("vm.past.delete"), askDelete === m.id ? "danger" : "ghost", () => removeMeeting(m.id)));
      li.append(row);
      if (rowMsg[m.id]) li.append(warn(rowMsg[m.id]));
      if (opened[m.id]) li.append(minutes(opened[m.id], m.id));
      ul.append(li);
    }
    s.append(ul);
  }
  async function refreshList() {
    const r = await ctx.svc("GET", "/meetings/list");
    if (r.ok) meetings = r.data.meetings || [];
    lastList = Date.now();
  }

  // ---------------------------------------------------------------- the copilot
  function paintCopilot() {
    if (!changed("cop", [cop, copMsg])) return;
    const s = clear(ui.cop);
    s.append(chip(cop.active ? T("vm.cop.on") : T("vm.cop.off"), cop.active ? "vt-ok" : ""),
             btn(cop.active ? T("vm.cop.close") : T("vm.cop.open"), "ghost", toggleCopilot, "vm-cop-toggle"));
    ui.cop.dataset.on = cop.active ? "1" : "";
    const note = copMsg || cop.note;
    if (note) { const n = el("p", "vt-note", note); n.dataset.bad = "1"; n.setAttribute("role", "alert"); s.append(n); }
    if (cop.hide_from_capture === false) s.append(warn(T("vm.cop.visible")));
  }
  async function toggleCopilot() {
    const r = await ctx.svc("POST", "/copilot/toggle");
    copMsg = r.ok ? "" : msgOf(r);
    if (r.ok) cop = { ...cop, active: !!r.data.active, note: r.data.note || "" };
    await refreshStatus(); paintCopilot();
  }

  // ---------------------------------------------------------------- the calendar
  function paintCalendar() {
    if (!changed("cal", [cal, events, calMsg])) return;
    const s = clear(ui.cal);
    if (!cal) { s.append(el("p", "vt-help", T("vm.loading"))); return; }
    s.append(el("p", "vt-help", cal.autojoin ? T("vm.cal.autojoin.on") : T("vm.cal.autojoin.off")));
    for (const name of ["google", "microsoft"]) {
      const p = cal[name] || {}, box = el("div", "vm-prov"), row = el("div", "vt-chips");
      row.append(el("strong", "", T("vm.cal." + name)), chip(p.connected ? T("vm.cal.connected") : p.configured ? T("vm.cal.notconnected") : T("vm.cal.inert"), p.connected ? "vt-ok" : ""));
      if (p.configured) row.append(btn(p.connected ? T("vm.cal.disconnect") : T("vm.cal.connect"), "ghost", () => calAct(name, p.connected ? "disconnect" : "connect")));
      box.append(row);
      if (!p.configured) box.append(el("p", "vt-help", T("vm.cal.inert.help", p.hint || "")));
      if (p.flow_error) box.append(warn(p.flow_error));
      s.append(box);
    }
    if (calMsg) s.append(warn(calMsg));
    if (events.length) {
      const ul = el("ul", "vm-meets");
      for (const e of events) {
        const li = el("li", "vm-meet"), row = el("label", "vt-check"), i = el("input");
        i.type = "checkbox"; i.checked = !!e.autojoin; i.disabled = !e.meeting_url;
        i.onchange = async () => { const r = await ctx.svc("POST", `/calendar/events/${e.id}/autojoin`, { enabled: i.checked }); calMsg = r.ok ? "" : msgOf(r); lastCal = 0; };
        row.append(i, el("span", "", `${when(e.start)} - ${e.title || ""}${e.meeting_url ? "" : " (" + T("vm.cal.nolink") + ")"}`));
        li.append(row); ul.append(li);
      }
      s.append(el("h4", "vm-sub", T("vm.cal.events")), ul);
    }
  }
  async function calAct(name, what) {
    const r = await ctx.svc("POST", `/calendar/${name}/${what}`);
    const d = r.data || {};
    calMsg = r.ok ? (d.error ? d.error : d.user_code ? T("vm.cal.code", d.verification_uri || "", d.user_code) : "") : msgOf(r);
    lastCal = 0; await refreshCal();
  }
  async function refreshCal() {
    const r = await ctx.svc("GET", "/calendar/status");
    cal = r.ok ? r.data : null;
    const connected = cal && ["google", "microsoft"].some(n => cal[n] && cal[n].connected);
    if (connected) { const e = await ctx.svc("GET", "/calendar/events"); events = e.ok ? (e.data.events || []) : []; } else events = [];
    lastCal = Date.now();
  }

  // ---------------------------------------------------------------- the poll
  async function refreshStatus() {
    const r = await ctx.svc("GET", "/meetbot/status");
    if (r.ok) {
      st = r.data;
      if (st.copilot) cop = st.copilot;
    } else st = { state: "idle" };
  }
  async function loadDefaults() {
    const r = await ctx.api("GET", "api/voice/meetings");
    if (!r.ok) return;
    D = r.data;
    if (!ui.url) return;
    if (!ACTIVE.includes(st.state)) { ui.speak.checked = !!D.speak; ui.mode.value = D.consent_mode; ui.win.value = D.consent_window_s; }
    syncJoin();
  }
  async function update() {
    if (!card || !ctx) return;
    const up = ctx.live().up;
    if (up) {
      await refreshStatus();
      if (Date.now() - lastList > LIST_EVERY_MS || (st.state === "ended" && changed("endseen", st.session_id))) await refreshList();
      if (Date.now() - lastCal > CAL_EVERY_MS) await refreshCal();
    } else st = { state: "idle" };
    syncJoin(); paintLive(); paintList(false); paintCopilot(); paintCalendar();
  }
  function mount(section, context) {
    ctx = context; card = section;
    const intro = el("p", "vt-help", T("vm.intro"));
    ui.live = el("div", "vm-live"); ui.live.setAttribute("aria-live", "polite"); ui.live.hidden = true;
    ui.list = el("div", "vm-past"); ui.cop = el("div", "vm-cop"); ui.cal = el("div", "vm-cal");
    const sect = (key, node) => { const w = el("div", "vm-sect"); w.append(el("h4", "vm-sub", T(key)), node); return w; };
    card.append(intro, buildForm(), ui.live, sect("vm.past", ui.list), sect("vm.cop", ui.cop), sect("vm.cal", ui.cal));
    Object.keys(sigs).forEach(k => delete sigs[k]);
    loadDefaults(); update();
  }

  // ---------------------------------------------------------------- Settings > Voice > Meetings
  function settingsCard(api) {
    const box = el("section", "vt-card vm-settings"), h = el("h3", "vt-h", T("vm.set.title"));
    h.id = "vm-set-h"; box.setAttribute("aria-labelledby", h.id);
    const msg = el("p", "vt-note"); msg.hidden = true; msg.setAttribute("role", "status");
    box.append(h, el("p", "vt-help", T("vm.set.intro")));
    let cur = null;
    const save = async change => {
      const r = await api("POST", "api/voice/meetings", change);
      if (r.ok) { cur = r.data; D = r.data; msg.textContent = T("vm.set.saved"); msg.dataset.bad = ""; } else { msg.textContent = msgOf(r); msg.dataset.bad = "1"; }
      msg.hidden = false; msg.setAttribute("role", r.ok ? "status" : "alert");
      if (r.ok) fill();
    };
    const text = (key, id, max) => { const i = el("input"); i.type = "text"; i.maxLength = max; i.autocomplete = "off"; i.onchange = () => save({ [key]: i.value }); return [i, id]; };
    const inputs = {};
    const add = (key, label, help, make) => { const [node, input] = make(); inputs[key] = input; box.append(field("vm-set-" + key, label, node, help)); };
    for (const [key, max, label, help] of [["on_behalf_of", 80, "vm.set.behalf", "vm.set.behalf.help"], ["display_name", 40, "vm.set.name", "vm.set.name.help"],
                                           ["virtual_mic", 80, "vm.set.mic", "vm.set.mic.help"], ["wake_phrase", 40, "vm.set.wake", "vm.set.wake.help"]]) {
      add(key, T(label), T(help), () => { const [i] = text(key, "", max); return [i, i]; });
    }
    const boolBox = (key, label, help) => {
      const [w, i] = check("vm-set-" + key, T(label), T(help), () => save({ [key]: i.checked }));
      inputs[key] = i; box.append(w);
    };
    boolBox("speak", "vm.set.speak", "vm.set.speak.help");
    boolBox("camera", "vm.set.camera", "vm.set.camera.help");
    boolBox("proactive", "vm.set.proactive", "vm.set.proactive.help");
    const mode = el("select");
    for (const m of ["window", "explicit"]) { const o = el("option", "", T("vm.mode." + m)); o.value = m; mode.append(o); }
    mode.onchange = () => save({ consent_mode: mode.value });
    inputs.consent_mode = mode; box.append(field("vm-set-consent_mode", T("vm.mode"), mode, T("vm.mode.help")));
    const win = el("input"); win.type = "number"; win.inputMode = "numeric"; win.min = "20"; win.max = "300"; win.step = "5";
    win.onchange = () => save({ consent_window_s: Number(win.value) });
    inputs.consent_window_s = win; box.append(field("vm-set-consent_window_s", T("vm.window"), win, T("vm.window.help")));
    boolBox("calendar_autojoin", "vm.set.autojoin", "vm.set.autojoin.help");
    const reps = el("input"); reps.type = "number"; reps.inputMode = "numeric"; reps.min = "1"; reps.max = "6";
    reps.onchange = () => save({ copilot_replies: Number(reps.value) });
    inputs.copilot_replies = reps; box.append(field("vm-set-copilot_replies", T("vm.set.replies"), reps));
    boolBox("copilot_audio_reply", "vm.set.audio", "vm.set.audio.help");
    boolBox("copilot_hide_from_capture", "vm.set.hide", "vm.set.hide.help");
    box.append(msg);
    function fill() {
      if (!cur) return;
      for (const [key, input] of Object.entries(inputs)) {
        if (document.activeElement === input) continue;
        if (input.type === "checkbox") input.checked = !!cur[key]; else input.value = cur[key] == null ? "" : cur[key];
      }
    }
    api("GET", "api/voice/meetings").then(r => { if (r.ok) { cur = r.data; D = r.data; fill(); } else { msg.textContent = msgOf(r); msg.dataset.bad = "1"; msg.hidden = false; } });
    return box;
  }

  window.VoiceMeet = { mount, update, settingsCard };
})();
