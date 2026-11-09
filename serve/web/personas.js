"use strict";
/* Personalities: Settings > Personalities, where they are made (name, voice, avatar, how they talk, a short profile) and where each one's own
   memory is read and corrected, and the menu in the header of Chat and Code that chooses the one a conversation speaks as. A conversation of
   the agent keeps its choice on the server; the model-only chat keeps it in this browser until a new conversation starts. Everything the server
   returns enters the page as text. */
(function () {
  const host = document.getElementById("pe-root"), sel = document.getElementById("persona"), label = document.getElementById("persona-label");
  if (!host || !sel || !label) return;

  const KEY = "tadamoo.chat.persona", ARM_MS = 4000, SHOWN_LANGS = [["", ""], ["en", "English"], ["it", "Italiano"], ["fr", "Français"], ["de", "Deutsch"], ["es", "Español"], ["pt", "Português"]];
  let list = [], limits = { name: 40, personality: 8000, system_prompt: 8000, profile: 1500, voice: 80, avatar: 200 }, denied = false, pending = "";
  let archives = new Map();                                           // archive id -> name, from the list the Knowledge tab uses
  let S = null, chosen = "", creating = false, detail = null, note = "", noteBad = false, armed = "", armTimer = 0, editing = "", fKind = "", fSection = "", query = "", finding = 0;

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };
  const why = r => (r.status === 0 ? T("pe.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`));
  const when = ts => (ts ? new Date(ts * 1000).toLocaleString(LANG === "it" ? "it-IT" : "en-GB", { day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit" }) : "-");
  const settingsOn = () => document.body.dataset.view === "settings" && !document.getElementById("view-settings").hidden && !document.getElementById("pe-section").hidden;

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  /* ---- the menu in the header of Chat and Code ---- */
  const chat = () => window.agentChat || null;
  const withSessions = () => !!(chat() && chat().active());
  const stored = () => { try { return localStorage.getItem(KEY) || ""; } catch (e) { return ""; } };
  const keepStored = v => { try { if (v) localStorage.setItem(KEY, v); else localStorage.removeItem(KEY); } catch (e) { /* this page only */ } };
  const nameOf = slug => { const p = list.find(x => x.slug === slug); return p ? p.name : slug; };
  function current() {
    if (!withSessions()) return stored();
    const rec = chat().record();
    return rec ? (rec.persona || "") : pending;
  }
  function paintMenu() {
    const now = current(), view = document.body.dataset.view;
    label.hidden = denied || !(view === "chat" || view === "agent") || !(list.length || now);
    if (label.hidden) return;
    const options = [["", T("pe.none")], ...list.map(p => [p.slug, p.name])];
    if (now && !list.some(p => p.slug === now)) options.push([now, T("pe.gone", now)]);
    const sig = JSON.stringify(options);
    if (sel.dataset.sig !== sig) {                                    // rebuilt only when the list changed: a menu being used stays open
      sel.replaceChildren(...options.map(([value, text]) => { const o = el("option", "", text); o.value = value; return o; }));
      sel.dataset.sig = sig;
    }
    if (document.activeElement !== sel) sel.value = now;
    sel.title = now ? T("pe.tip.on", nameOf(now)) : T("pe.tip");
  }
  async function loadList() {
    const r = await api("GET", "api/personas");
    denied = !r.ok;
    if (r.ok) { list = r.data.personas || []; limits = r.data.limits || limits; }
    paintMenu();
    return r;
  }
  sel.onchange = async () => {
    const slug = sel.value;
    document.dispatchEvent(new CustomEvent("tadamoo:persona", { detail: { slug } }));
    if (!withSessions()) { keepStored(slug); paintMenu(); return; }
    const sid = chat().current();
    if (!sid) { pending = slug; paintMenu(); return; }
    const r = await api("POST", `api/agent/sessions/${encodeURIComponent(sid)}/persona`, { persona: slug });
    if (!r.ok) { const s = document.getElementById("chat-status"); if (s) s.textContent = why(r); paintMenu(); return; }
    chat().refresh();
  };
  /* the text a conversation without a harness puts in its system message: the personality and the notes that match the request */
  async function system(goal) {
    const slug = stored();
    if (!slug || withSessions()) return "";
    const r = await api("POST", `api/personas/${encodeURIComponent(slug)}/compose`, { goal: String(goal || "").slice(0, 2000) });
    if (!r.ok) throw new Error(T("pe.compose.fail", why(r)));
    return [r.data.system, r.data.memory].filter(Boolean).join("\n\n");
  }
  /* the model-only chat ended: its lines go to the personality it spoke as, to learn from (a conversation with a harness is learned from by the server) */
  async function learn(lines, conversation) {
    const slug = stored();
    if (!slug || withSessions() || !lines.length) return;
    await api("POST", `api/personas/${encodeURIComponent(slug)}/learn`, { conversation, messages: lines.slice(-40).map(l => ({ role: l.role, text: String(l.text).slice(0, 4000) })) });
  }
  window.personaUi = {
    pending: () => (withSessions() ? sel.value : ""), name: nameOf, paint: paintMenu, system, learn,
    reset() { pending = ""; keepStored(""); paintMenu(); },
    reload: loadList,
  };
  document.addEventListener("tadamoo:conversation", () => { pending = ""; setTimeout(paintMenu, 0); });
  document.addEventListener("tadamoo:view", () => { setTimeout(paintMenu, 0); if (!list.length && !denied) loadList(); });
  document.getElementById("chatmode").addEventListener("change", () => setTimeout(paintMenu, 0));
  document.addEventListener("tadamoo:lang", () => { sel.dataset.sig = ""; paintMenu(); if (S) paint(); });

  /* ---- Settings > Personalities ---- */
  function arm(key) {
    armed = key; clearTimeout(armTimer);
    armTimer = setTimeout(() => { armed = ""; paint(); }, ARM_MS);
    paint();
  }
  const say = (text, bad) => { note = text; noteBad = !!bad; };

  async function open(slug) {
    chosen = slug; creating = false; editing = ""; armed = ""; detail = null;
    if (!slug) { paint(); return; }
    await reloadDetail();
  }
  async function reloadDetail() {
    const q = new URLSearchParams({ q: query, kind: fKind, section: fSection });
    const r = await api("GET", `api/personas/${encodeURIComponent(chosen)}?${q}`);
    if (!r.ok) { say(why(r), true); detail = null; if (r.status === 404) chosen = ""; paint(); return; }
    detail = r.data;
    if (detail.persona.knowledge && detail.persona.knowledge.length) await loadArchives();
    paint();
  }
  async function loadArchives() {
    const r = await api("GET", "api/rag");
    archives = new Map(r.ok ? (r.data.archives || []).filter(a => a && typeof a.id === "string" && typeof a.name === "string" && a.name).map(a => [a.id, a.name]) : []);
  }
  const archiveName = id => archives.get(id) || id;                    // an archive the list does not know keeps its id
  async function refresh() {
    const r = await loadList();
    if (!r.ok) { S = null; say(why(r), true); paint(); return; }
    S = true;
    if (chosen && !list.some(p => p.slug === chosen)) chosen = "";
    if (chosen) await reloadDetail(); else paint();
  }

  function field(id, labelText, help, control) {
    const wrap = el("div", "pe-field"), lab = el("label", "", labelText);
    lab.htmlFor = id; control.id = id;
    wrap.append(lab, control);
    if (help) wrap.append(el("p", "pe-help", help));
    return wrap;
  }
  function textInput(value, max) {
    const i = el("input"); i.type = "text"; i.value = value || ""; i.maxLength = max; i.autocomplete = "off"; i.spellcheck = false;
    return i;
  }
  function area(value, max, rows, counterHost) {
    const a = el("textarea"); a.value = value || ""; a.maxLength = max; a.rows = rows;
    const count = el("p", "pe-help pe-count");
    const show = () => { count.textContent = T("pe.chars", a.value.length, max); };
    a.addEventListener("input", show); show();
    counterHost.push(count);
    return a;
  }
  function languageSelect(value) {
    const s = el("select"), known = SHOWN_LANGS.some(([c]) => c === value);
    (known ? SHOWN_LANGS : [...SHOWN_LANGS, [value, value]]).forEach(([code, name]) => { const o = el("option", "", code ? name : T("pe.lang.any")); o.value = code; o.selected = code === value; s.append(o); });
    return s;
  }
  function editor() {
    const p = creating ? { name: "", voice: { engine: "", voice: "" }, avatar: "", personality: "", system_prompt: "", profile: "", language: "", knowledge: [] } : detail.persona;
    const form = el("form", "pe-form"), counters = [];
    const name = textInput(p.name, limits.name), lang = languageSelect(p.language), engine = textInput(p.voice.engine, limits.voice), voice = textInput(p.voice.voice, limits.voice);
    const avatar = textInput(p.avatar, limits.avatar);
    const system = area(p.system_prompt, limits.system_prompt, 5, counters), talk = area(p.personality, limits.personality, 6, counters), profile = area(p.profile, limits.profile, 4, counters);
    name.required = true;
    const row = el("div", "pe-row");
    row.append(field("pe-f-name", T("pe.f.name"), T("pe.f.name.help"), name), field("pe-f-lang", T("pe.f.language"), "", lang));
    const voices = el("div", "pe-row");
    voices.append(field("pe-f-engine", T("pe.f.engine"), "", engine), field("pe-f-voice", T("pe.f.voice"), T("pe.f.voice.help"), voice));
    form.append(row, voices, field("pe-f-avatar", T("pe.f.avatar"), T("pe.f.avatar.help"), avatar),
      field("pe-f-system", T("pe.f.system"), T("pe.f.system.help"), system), counters[0],
      field("pe-f-talk", T("pe.f.personality"), T("pe.f.personality.help"), talk), counters[1],
      field("pe-f-profile", T("pe.f.profile"), T("pe.f.profile.help"), profile), counters[2]);
    if (p.knowledge && p.knowledge.length) form.append(el("p", "pe-help", T("pe.f.knowledge", p.knowledge.map(archiveName).join(", "))));
    form.onsubmit = async e => {
      e.preventDefault();
      const body = { name: name.value.trim(), language: lang.value, voice: { engine: engine.value.trim(), voice: voice.value.trim() }, avatar: avatar.value.trim(),
        system_prompt: system.value, personality: talk.value, profile: profile.value };
      const r = creating ? await api("POST", "api/personas", body) : await api("POST", `api/personas/${encodeURIComponent(chosen)}/update`, body);
      if (!r.ok) { say(why(r), true); paint(); return; }
      say(T(creating ? "pe.created" : "pe.saved", r.data.persona.name), false);
      chosen = r.data.persona.slug; creating = false;
      await refresh();
    };
    const bar = el("div", "pe-bar");
    bar.append(btn(T(creating ? "pe.create" : "pe.save"), "primary", () => form.requestSubmit()));
    if (creating) bar.append(btn(T("me.cancel"), "ghost", () => { creating = false; say("", false); paint(); }));
    else {
      const total = detail.memory.counts.long + detail.memory.counts.short, sure = armed === "delete";
      bar.append(btn(sure ? T("pe.delete.sure", p.name, total) : T("pe.delete"), sure ? "pe-warn" : "ghost", async () => {
        if (!sure) { arm("delete"); return; }
        armed = "";
        const r = await api("POST", `api/personas/${encodeURIComponent(chosen)}/delete`, { confirm: true });
        say(r.ok ? T("pe.deleted", p.name) : why(r), !r.ok);
        if (r.ok) { chosen = ""; detail = null; }
        await refresh();
      }));
    }
    form.append(bar);
    return form;
  }

  /* ---- the memory of one personality ---- */
  const mpost = async (action, body) => api("POST", `api/personas/${encodeURIComponent(chosen)}/memory/${action}`, body);
  function sectionSelect(kind, value, id) {
    const s = el("select"); s.id = id; s.setAttribute("aria-label", T("me.sec.field")); s.title = T("me.sec.tip");
    detail.memory.sections[kind].forEach(n => { const o = el("option", "", T("me.sec." + n)); o.value = n; o.selected = n === value; s.append(o); });
    return s;
  }
  function noteRow(i) {
    const li = el("li", "me-note" + (i.origin === "inferred" ? " me-guess" : ""));
    if (editing === i.id) {
      const form = el("form", "me-edit"), text = el("textarea"), sec = sectionSelect(i.kind, i.section, "pe-es-" + i.id);
      text.value = i.text; text.maxLength = 400; text.rows = 2; text.setAttribute("aria-label", T("me.note.text"));
      form.onsubmit = async e => {
        e.preventDefault();
        const r = await mpost("item", { id: i.id, text: text.value, section: sec.value });
        say(r.ok ? "" : why(r), !r.ok);
        if (r.ok) editing = "";
        await refresh();
      };
      form.append(text, sec, btn(T("me.save"), "primary", () => form.requestSubmit()), btn(T("me.cancel"), "ghost", () => { editing = ""; paint(); }));
      li.append(form);
      return li;
    }
    const head = el("div", "me-nhead");
    head.append(el("span", "me-tag " + i.kind, T("me.sec." + i.section)), ...(i.source === "conversation" ? [el("span", "me-tag", T("pe.learn.tag"))] : []), el("span", "me-origin", i.origin === "inferred" ? T("me.origin.inferred", Math.round(i.confidence * 100)) : T("me.origin." + i.origin)),
      el("span", "me-when", T("me.seen", when(i.seen), i.mentions || 1)));
    li.append(head, el("p", "me-text", i.text));
    const bar = el("div", "me-nbar");
    if (i.origin === "inferred") bar.append(btn(T("me.confirm"), "", async () => { const r = await mpost("confirm", { id: i.id }); say(r.ok ? "" : why(r), !r.ok); await refresh(); }));
    bar.append(btn(T("me.edit"), "ghost", () => { editing = i.id; paint(); }));
    const sure = armed === "d" + i.id;
    bar.append(btn(sure ? T("me.delete.sure") : T("me.delete"), sure ? "pe-warn" : "ghost", async () => {
      if (!sure) { arm("d" + i.id); return; }
      armed = ""; const r = await mpost("delete", { id: i.id }); say(r.ok ? "" : why(r), !r.ok); await refresh();
    }));
    li.append(bar);
    return li;
  }
  function filterBar() {
    const bar = el("div", "me-filter"), m = detail.memory;
    const kind = el("select"), sec = el("select"), q = el("input");
    kind.id = "pe-fk"; sec.id = "pe-fs"; q.id = "pe-fq";
    const labels = [["pe-fk", T("me.f.kind")], ["pe-fs", T("me.f.section")], ["pe-fq", T("me.f.search")]].map(([id, text]) => { const l = el("label", "sr", text); l.htmlFor = id; return l; });
    [["", T("me.f.all")], ["long", T("me.long.name")], ["short", T("me.short.name")]].forEach(([v, t]) => { const o = el("option", "", t); o.value = v; o.selected = v === fKind; kind.append(o); });
    const none = el("option", "", T("me.f.allsections")); none.value = ""; sec.append(none);
    (fKind ? m.sections[fKind] : [...m.sections.long, ...m.sections.short]).forEach(n => { const o = el("option", "", T("me.sec." + n)); o.value = n; o.selected = n === fSection; sec.append(o); });
    q.type = "search"; q.placeholder = T("me.f.search"); q.value = query; q.autocomplete = "off";
    kind.onchange = () => { fKind = kind.value; fSection = ""; reloadDetail(); };
    sec.onchange = () => { fSection = sec.value; reloadDetail(); };
    q.oninput = () => { clearTimeout(finding); finding = setTimeout(async () => { query = q.value; await reloadDetail(); const again = document.getElementById("pe-fq"); if (again) { again.focus(); again.setSelectionRange(query.length, query.length); } }, 300); };
    bar.append(labels[0], kind, labels[1], sec, labels[2], q);
    return bar;
  }
  function addForm() {
    const form = el("form", "me-add"), kind = el("select"), text = el("input"), tl = el("label", "sr", T("me.add.text"));
    let sec = sectionSelect("long", detail.memory.sections.long[0], "pe-as");
    kind.id = "pe-ak"; text.id = "pe-at"; tl.htmlFor = "pe-at"; kind.setAttribute("aria-label", T("me.kind.field")); kind.title = T("me.kind.tip");
    [["long", T("me.long.name")], ["short", T("me.short.name")]].forEach(([v, t]) => { const o = el("option", "", t); o.value = v; kind.append(o); });
    kind.onchange = () => { const next = sectionSelect(kind.value, detail.memory.sections[kind.value][0], "pe-as"); sec.replaceWith(next); sec = next; };
    text.maxLength = 400; text.placeholder = T("me.add.ph"); text.autocomplete = "off";
    form.onsubmit = async e => {
      e.preventDefault();
      if (!text.value.trim()) return;
      const r = await mpost("item", { kind: kind.value, section: sec.value, text: text.value });
      say(r.ok ? "" : why(r), !r.ok);
      if (r.ok) text.value = "";
      await refresh();
    };
    form.append(kind, sec, tl, text, btn(T("me.add"), "primary", () => form.requestSubmit()));
    return form;
  }
  function learnBlock() {
    const box = el("div", "pe-learn"), L = detail.learned, on = detail.persona.learn !== false, row = el("label", "pe-switch"), cb = el("input");
    cb.type = "checkbox"; cb.id = "pe-learn-on"; cb.checked = on;
    cb.onchange = async () => {
      const r = await api("POST", `api/personas/${encodeURIComponent(chosen)}/update`, { learn: cb.checked });
      say(r.ok ? "" : why(r), !r.ok);
      await refresh();
    };
    row.append(cb, el("span", "", T("pe.learn.on")));
    const sure = armed === "forget", forget = btn(sure ? T("pe.learn.forget.sure", L.notes) : T("pe.learn.forget", L.notes), sure ? "pe-warn" : "ghost", async () => {
      if (!L.notes) return;
      if (!sure) { arm("forget"); return; }
      armed = ""; const r = await mpost("forget-learned", { confirm: true });
      say(r.ok ? T("pe.learn.forgot", r.data.forgotten) : why(r), !r.ok); await refresh();
    });
    forget.disabled = !L.notes;
    const bar = el("div", "pe-bar");
    bar.append(forget);
    box.append(row, el("p", "pe-help", T("pe.learn.help")), el("p", "pe-line", L.conversations ? T("pe.learn.line", L.conversations, when(L.last)) : T("pe.learn.none")), bar);
    return box;
  }
  function memoryBlock() {
    const m = detail.memory, box = el("section", "pe-memory"), total = m.counts.long + m.counts.short;
    box.append(el("h3", "pe-h", T("pe.mem.title")), el("p", "pe-help", T("pe.mem.help")), el("p", "pe-line", T("pe.mem.counts", m.counts.long, m.counts.short)), learnBlock(), filterBar());
    const ul = el("ul", "me-list");
    m.items.forEach(i => ul.append(noteRow(i)));
    box.append(m.items.length ? ul : el("p", "pe-help", T(query || fKind || fSection ? "pe.mem.nomatch" : "pe.mem.none")));
    if (m.more) box.append(el("p", "pe-help", T("pe.mem.more", m.more)));
    box.append(el("h4", "pe-h2", T("me.add.title")), addForm());
    const sure = armed === "clear";
    const clear = btn(sure ? T("me.clear.sure", total) : T("pe.mem.clear", total), sure ? "pe-warn" : "ghost", async () => {
      if (!total) return;
      if (!sure) { arm("clear"); return; }
      armed = ""; const r = await mpost("clear", { confirm: true }); say(r.ok ? "" : why(r), !r.ok); await refresh();
    });
    clear.disabled = !total;
    const bar = el("div", "pe-bar");
    bar.append(clear);
    box.append(bar);
    return box;
  }

  function sideList() {
    const side = el("nav", "pe-side"), ul = el("ul", "pe-names");
    side.setAttribute("aria-label", T("pe.list.title"));
    list.forEach(p => {
      const li = el("li"), b = btn("", "pe-pick" + (p.slug === chosen && !creating ? " on" : ""), () => { query = ""; fKind = ""; fSection = ""; say("", false); open(p.slug); });
      b.setAttribute("aria-current", String(p.slug === chosen && !creating));
      b.append(el("span", "pe-pname", p.name), el("span", "pe-pcount", T("pe.pcount", p.counts.long + p.counts.short)));
      li.append(b); ul.append(li);
    });
    side.append(el("h3", "pe-h2", T("pe.list.title")), list.length ? ul : el("p", "pe-help", T("pe.list.none")),
      btn(T("pe.new"), "primary pe-newbtn", () => { creating = true; chosen = ""; detail = null; say("", false); paint(); }));
    return side;
  }
  function paint() {
    if (!S) { host.replaceChildren(el("p", "pe-help", note || T("pe.loading"))); return; }
    const focus = document.activeElement && document.activeElement.id;
    const status = el("p", "pe-note", note); status.setAttribute("role", "status"); status.dataset.bad = noteBad ? "1" : ""; status.hidden = !note;
    const main = el("div", "pe-main");
    if (creating) main.append(el("h3", "pe-h", T("pe.new")), editor());
    else if (detail) main.append(el("h3", "pe-h", detail.persona.name), editor(), memoryBlock());
    else main.append(el("p", "pe-help", T("pe.pick")));
    const layout = el("div", "pe-layout");
    layout.append(sideList(), main);
    host.replaceChildren(el("p", "pe-intro", T("pe.intro")), status, layout);
    const again = focus && document.getElementById(focus);
    if (again && again !== document.activeElement) again.focus();
  }

  document.addEventListener("tadamoo:sub", e => { if (e.detail === "personas") refresh(); });
  loadList();
})();
