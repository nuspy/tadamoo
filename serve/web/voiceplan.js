"use strict";
/* The planner of the Meetings card: the meetings the person planned and the ones in their calendar, one list with the choices for each (take part,
   role, personality, camera), a form to add or change one, Join now, and the state of each (coming, running, done with a link to its record, missed).
   The joining at the time is the voice service's: this page only asks, through the server's list (serve/voice_proxy.py), and everything the server
   says enters the page as text. voicemeet.js mounts it and passes `host` (the requests, the personalities, the past meetings). */
(function () {
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick, id) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; if (id) b.id = id; return b; };
  const clear = node => { while (node.firstChild) node.firstChild.remove(); return node; };
  const chip = (text, cls) => el("span", "vt-chip " + (cls || ""), text);
  const warn = text => { const n = el("p", "vt-note", text); n.dataset.bad = "1"; n.setAttribute("role", "alert"); return n; };
  const ROLES = ["listen", "minutes", "speak"], EVERY_MS = 10000;
  const pad = n => String(n).padStart(2, "0");

  let host = null, root = null, plan = { items: [], platforms: [], autojoin: false }, loaded = false, msg = { text: "", bad: false };
  let editing = "", askDelete = "", askJoin = "", askRights = false, lastLoad = 0, busy = false, sig = "";
  const f = {}, ui = {};

  const when = iso => { const d = new Date(iso); return isNaN(d) ? String(iso || "") : d.toLocaleString(LANG === "it" ? "it-IT" : "en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }); };
  const local = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const persona = slug => host.personas().find(p => p.slug === slug);
  const camReason = it => {                                   // why the camera of this item cannot show an avatar, known from the personalities
    if (!it.camera) return "";
    if (!it.persona) return "no_persona";
    const p = persona(it.persona);
    return !p ? "persona_missing" : !p.avatar ? "no_avatar" : "";
  };

  // ---------------------------------------------------------------- the form
  function field(id, label, input, help) {
    const w = el("div", "vm-field"), l = el("label", "", label);
    l.htmlFor = input.id = id;
    w.append(l, input);
    if (help) w.append(el("p", "vt-help", help));
    return w;
  }
  function check(id, label, help) {
    const w = el("div", "vm-field"), row = el("label", "vt-check"), i = el("input");
    i.type = "checkbox"; i.id = id;
    row.append(i, el("span", "", label));
    w.append(row);
    if (help) w.append(el("p", "vt-help", help));
    return [w, i];
  }
  function buildForm() {
    const form = el("fieldset", "vm-form vp-form"), lg = el("legend", "vt-legend", T("vp.add"));
    ui.legend = lg;
    f.title = el("input"); f.title.type = "text"; f.title.maxLength = 120; f.title.autocomplete = "off";
    f.url = el("input"); f.url.type = "url"; f.url.inputMode = "url"; f.url.autocomplete = "off"; f.url.spellcheck = false; f.url.placeholder = "https://meet.google.com/...";
    f.start = el("input"); f.start.type = "datetime-local";
    f.dur = el("input"); f.dur.type = "number"; f.dur.inputMode = "numeric"; f.dur.min = "1"; f.dur.max = "1440"; f.dur.step = "5";
    f.role = el("select");
    for (const r of ROLES) { const o = el("option", "", T("vp.role." + r)); o.value = r; f.role.append(o); }
    f.persona = el("select"); f.persona.onchange = () => { paintHints(); };
    f.notes = el("textarea"); f.notes.maxLength = 500; f.notes.rows = 2;
    const [cw, cam] = check("vp-camera", T("vp.camera"), T("vp.camera.help")), [pw, part] = check("vp-participate", T("vp.participate"), T("vp.participate.help"));
    f.camera = cam; f.participate = part; cam.onchange = paintHints;
    ui.hint = el("p", "vt-help");
    ui.formMsg = el("p", "vt-note"); ui.formMsg.hidden = true;
    ui.reminder = el("p", "vt-help vp-consent", T("vp.consent"));
    f.save = btn(T("vp.save"), "primary", save, "vp-save");
    const urlField = field("vp-url", T("vp.f.url"), f.url);
    ui.urlHelp = el("p", "vt-help");
    urlField.append(ui.urlHelp);
    const row = el("div", "vm-actions"); row.append(f.save, btn(T("vp.cancel"), "ghost", closeForm, "vp-cancel"));
    form.append(lg, field("vp-title", T("vp.f.title"), f.title), urlField, field("vp-start", T("vp.f.start"), f.start),
                field("vp-dur", T("vp.f.dur"), f.dur), field("vp-role", T("vp.f.role"), f.role, T("vp.f.role.help")), field("vp-persona", T("vp.f.persona"), f.persona),
                cw, ui.hint, pw, field("vp-notes", T("vp.f.notes"), f.notes), ui.reminder, row, ui.formMsg);
    form.hidden = true;
    ui.form = form;
    return form;
  }
  function fillPersonas() {
    const keep = f.persona.value;
    clear(f.persona);
    const none = el("option", "", T("vp.persona.none")); none.value = ""; f.persona.append(none);
    for (const p of host.personas()) { const o = el("option", "", p.name || p.slug); o.value = p.slug; f.persona.append(o); }
    if (keep && ![...f.persona.options].some(o => o.value === keep)) { const o = el("option", "", T("vp.persona.missing", keep)); o.value = keep; f.persona.append(o); }
    f.persona.value = keep;
  }
  function paintHints() {                                     // why the camera would stay off, said before the meeting
    const why = camReason({ camera: f.camera.checked, persona: f.persona.value });
    ui.hint.textContent = why ? T("vp.cam." + why) : "";
    ui.hint.hidden = !why;
  }
  function openForm(item) {
    editing = item ? item.id : "";
    fillPersonas();
    const cal = !!item && item.source !== "manual";
    ui.legend.textContent = item ? T("vp.edit") : T("vp.add");
    f.title.value = item ? item.title || "" : "";
    f.url.value = item ? item.url || "" : "";
    const d = item ? new Date(item.start) : new Date(Math.ceil((Date.now() + 600000) / 3600000) * 3600000);
    f.start.value = local(d);
    f.dur.value = item && item.duration_min ? item.duration_min : "";
    f.role.value = item ? item.role : "minutes";
    f.persona.value = item ? item.persona || "" : "";
    f.camera.checked = !!(item && item.camera);
    f.participate.checked = item ? !!item.participate : true;
    f.notes.value = item ? item.notes || "" : "";
    for (const k of ["title", "url", "start", "dur"]) f[k].disabled = cal;
    ui.formMsg.hidden = true;
    ui.form.hidden = false;
    ui.add.hidden = true;
    paintHints();
    f.title.focus();
  }
  function closeForm() { editing = ""; ui.form.hidden = true; ui.add.hidden = false; }
  async function save() {
    if (busy) return;
    const start = new Date(f.start.value);
    const dur = f.dur.value.trim() === "" ? null : Number(f.dur.value);
    const fail = text => { ui.formMsg.textContent = text; ui.formMsg.dataset.bad = "1"; ui.formMsg.setAttribute("role", "alert"); ui.formMsg.hidden = false; };
    if (!f.url.disabled && !f.url.value.trim()) return fail(T("vp.e.url"));
    if (!f.start.disabled && isNaN(start)) return fail(T("vp.e.start"));
    if (dur !== null && !(Number.isInteger(dur) && dur >= 1 && dur <= 1440)) return fail(T("vp.e.dur"));
    const body = { role: f.role.value, persona: f.persona.value, camera: f.camera.checked, participate: f.participate.checked, notes: f.notes.value };
    if (!f.url.disabled) Object.assign(body, { title: f.title.value, url: f.url.value.trim(), start: start.toISOString() });
    if (!f.dur.disabled && (dur !== null || editing)) body.duration_min = dur;
    busy = true; f.save.disabled = true;
    const r = editing ? await host.svc("PUT", `/meetings/plan/${editing}`, body) : await host.svc("POST", "/meetings/plan", body);
    busy = false; f.save.disabled = false;
    if (!r.ok) return fail(host.msgOf(r));
    msg = { text: T("vp.saved"), bad: false };
    closeForm(); await load(); paint();
  }

  // ---------------------------------------------------------------- the list
  async function change(item, patch) {
    const r = await host.svc("PUT", `/meetings/plan/${item.id}`, patch);
    msg = r.ok ? { text: "", bad: false } : { text: host.msgOf(r), bad: true };
    await load(); paint();
  }
  async function remove(item) {
    if (askDelete !== item.id) { askDelete = item.id; paint(); setTimeout(() => { if (askDelete === item.id) { askDelete = ""; paint(); } }, 6000); return; }
    askDelete = "";
    const r = await host.svc("DELETE", `/meetings/plan/${item.id}`);
    msg = r.ok ? { text: "", bad: false } : { text: host.msgOf(r), bad: true };
    await load(); paint();
  }
  async function joinNow(item) {
    if (!askRights) return;
    const r = await host.svc("POST", `/meetings/plan/${item.id}/join-now`, { rights_confirmed: true });
    askJoin = ""; askRights = false;
    msg = r.ok ? { text: T("vp.joined"), bad: false } : { text: host.msgOf(r), bad: true };
    await load(); await host.refreshStatus(); paint();
  }
  function card(it) {
    const li = el("li", "vm-meet vp-item"), head = el("div", "vm-meet-h");
    head.append(el("strong", "", it.title || it.platform), chip(T("vp.st." + it.status), it.status === "done" || it.status === "running" ? "vt-ok" : it.status === "missed" ? "vt-warn" : ""));
    const chips = el("div", "vt-chips");
    chips.append(chip(it.platform), chip(T("vp.role." + it.role)));
    if (it.source !== "manual") chips.append(chip(T("vp.src." + it.source)));
    if (it.persona) chips.append(chip(persona(it.persona) ? persona(it.persona).name || it.persona : T("vp.persona.missing", it.persona)));
    if (it.camera) chips.append(chip(T("vp.camera.on")));
    li.append(head, el("p", "vt-help", [when(it.start), it.duration_min ? T("vp.min", it.duration_min) : ""].filter(Boolean).join(" - ")), chips);
    const why = camReason(it);
    if (why) li.append(el("p", "vt-help", T("vp.cam." + why)));
    if (it.last_note) li.append(el("p", "vt-help", T("vp.note." + it.last_note)));
    if (it.last_error) li.append(warn(T("vp.error", it.last_error)));
    if (it.notes) li.append(el("p", "vm-text", it.notes));
    if (it.status === "missed") li.append(el("p", "vt-help", T("vp.missed.help")));
    if (it.status === "done" || it.status === "running") {
      if (it.session_id && host.hasRecord(it.session_id)) li.append(btn(T("vp.record"), "ghost", () => host.openRecord(it.session_id)));
      else if (it.status === "done") li.append(el("p", "vt-help", T("vp.record.none")));
    }
    const row = el("div", "vm-actions");
    if (it.status === "upcoming") {
      const [w, box] = check("vp-p-" + it.id, T("vp.participate"));
      box.checked = !!it.participate; box.onchange = () => change(it, { participate: box.checked });
      w.classList.add("vp-toggle");
      li.append(w);
    }
    if (it.status !== "running") row.append(btn(T("vp.joinnow"), "ghost", () => { askJoin = askJoin === it.id ? "" : it.id; askRights = false; paint(); }));
    row.append(btn(T("vp.edit"), "ghost", () => openForm(it)));
    if (it.source === "manual") row.append(btn(askDelete === it.id ? T("vp.delete.confirm") : T("vp.delete"), askDelete === it.id ? "danger" : "ghost", () => remove(it)));
    li.append(row);
    if (askJoin === it.id) {
      const box = el("div", "vp-ask"), [w, tick] = check("vp-rights-" + it.id, T("vm.rights"), T("vm.rights.help"));
      const go = btn(T("vp.joinnow.go"), "primary", () => joinNow(it));
      tick.checked = askRights; go.disabled = !askRights;
      tick.onchange = () => { askRights = tick.checked; go.disabled = !askRights; };
      box.append(w, go);
      li.append(box);
    }
    return li;
  }
  function paint() {
    if (!root) return;
    const s = clear(ui.list);
    ui.urlHelp.textContent = T("vp.f.url.help", (plan.platforms || []).join(", ") || "-");
    if (ui.master) {
      ui.master.textContent = plan.autojoin ? T("vp.master.on", plan.lead_s || 60) : T("vp.master.off");
      ui.master.dataset.bad = plan.autojoin ? "" : "1";
    }
    if (msg.text) s.append(msg.bad ? warn(msg.text) : el("p", "vt-help", msg.text));
    if (!loaded) { s.append(el("p", "vt-help", T("vm.loading"))); return; }
    if (!plan.items.length) { s.append(el("p", "vt-help", T("vp.none"))); return; }
    const ul = el("ul", "vm-meets");
    for (const it of plan.items) ul.append(card(it));
    s.append(ul);
  }
  async function load() {
    const r = await host.svc("GET", "/meetings/plan");
    lastLoad = Date.now();
    if (r.ok) { plan = r.data; loaded = true; } else if (!loaded) msg = { text: host.msgOf(r), bad: true };
    return r.ok;
  }

  // ---------------------------------------------------------------- the poll
  async function update() {
    if (!root) return;
    const typing = !ui.form.hidden || askJoin;                // a list is not repainted under a form or a question
    if (host.live().up && Date.now() - lastLoad > EVERY_MS) {
      await load();
      const next = JSON.stringify([plan, msg, askDelete]);
      if (!typing && next !== sig) { sig = next; paint(); }
    } else if (!host.live().up && loaded) { loaded = false; paint(); }
  }
  function mount(section, context) {
    host = context; root = section;
    ui.master = el("p", "vt-help");
    ui.add = btn(T("vp.add"), "primary", () => openForm(null), "vp-add");
    ui.list = el("div", "vp-list");
    ui.list.setAttribute("aria-live", "polite");
    root.append(el("p", "vt-help", T("vp.intro")), ui.master, ui.add, buildForm(), ui.list);
    paint();
    update();
  }
  window.VoicePlan = { mount, update, refresh: async () => { await load(); paint(); } };
})();
