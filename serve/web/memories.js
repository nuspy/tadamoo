"use strict";
/* Settings > Memories: the long-term and the short-term memory the assistant keeps about the person. Each has its own switch to learn and to be used
   in answers; each section of the long-term memory has a weight; the notes can be read, written, corrected, confirmed and deleted. The notes are
   encrypted on this PC and only this page reaches them. Everything shown enters the page as text. */
(function () {
  const host = document.getElementById("me-root");
  if (!host) return;

  const POLL_MS = 5000, ARM_MS = 4000;
  let S = null, note = "", exported = null, fKind = "", fSection = "", query = "", editing = "", armed = "", cloud = 0, timer = 0, armTimer = 0, finding = 0, seen = "";
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick, tip) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; if (tip) b.title = tip; return b; };
  const errText = r => (r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`;
  const when = ts => (ts ? new Date(ts * 1000).toLocaleString(LANG === "it" ? "it-IT" : "en-GB", { day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit" }) : "-");
  const active = () => document.body.dataset.view === "settings" && !document.getElementById("view-settings").hidden && !document.getElementById("me-section").hidden;

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  async function refresh(full) {
    const q = new URLSearchParams({ q: query, kind: fKind, section: fSection });
    const r = await api("GET", "api/memories?" + q);
    if (!r.ok) { note = errText(r); paint(); return; }
    S = r.data;
    const sig = JSON.stringify([S.items.map(i => i.id + i.updated + i.seen), S.log.length && S.log[0].at, S.worker, S.counts, S.cards]);
    if (full || sig !== seen) { seen = sig; paint(); }
  }
  function poll() {
    clearTimeout(timer);
    if (!active() || document.hidden) return;
    timer = setTimeout(async () => { if (!editing) await refresh(false); poll(); }, POLL_MS);
  }
  function arm(key) {
    armed = key; clearTimeout(armTimer);
    armTimer = setTimeout(() => { armed = ""; paint(); }, ARM_MS);
    paint();
  }
  async function saveSettings(change) {
    const r = await api("POST", "api/memories/settings", change);
    note = r.ok ? "" : errText(r);
    if (r.ok) S.settings = r.data.settings;
    paint();
  }

  /* ---- the switches of one memory ---- */
  function check(label, help, on, onchange) {
    const row = el("label", "me-check"), cb = el("input");
    cb.type = "checkbox"; cb.checked = on; cb.onchange = () => onchange(cb.checked);
    const text = el("span", "");
    text.append(el("b", "", label), el("span", "help", help));
    row.append(cb, text);
    return row;
  }
  function memoryBlock(kind) {
    const s = S.settings, box = el("div", "me-block"), n = S.counts[kind];
    box.append(el("h3", "", T(`me.${kind}.title`, n)), el("p", "help", T(`me.${kind}.help`)));
    box.append(check(T("me.learn"), T(`me.learn.help.${kind}`), s["learn_" + kind], v => saveSettings({ ["learn_" + kind]: v })));
    box.append(check(T("me.use"), T("me.use.help"), s["use_" + kind], v => saveSettings({ ["use_" + kind]: v })));
    const days = el("div", "me-days"), input = el("input"), id = "me-days-" + kind;
    input.id = id; input.type = "number"; input.inputMode = "numeric"; input.min = kind === "short" ? "1" : "0"; input.max = "36500"; input.value = s[kind + "_days"];
    input.onchange = () => saveSettings({ [kind + "_days"]: Number(input.value) });
    const lab = el("label", "", T(`me.days.${kind}`)); lab.htmlFor = id;
    days.append(lab, input, el("span", "help", T(`me.days.help.${kind}`)));
    box.append(days);
    box.append(weightsBlock(kind));
    return box;
  }
  function weightsBlock(kind) {
    const wrap = el("div", "me-weights"), s = S.settings;
    wrap.append(el("h4", "", T("me.w.title")), el("p", "help", T("me.w.help")));
    S.sections[kind].forEach(name => {
      const row = el("div", "me-wrow"), sel = el("select"), id = "me-w-" + name, lab = el("label", "", T("me.sec." + name));
      lab.htmlFor = id; sel.id = id;
      S.weights.forEach(w => { const o = el("option", "", T("me.w." + w)); o.value = w; o.selected = s.weights[name] === w; sel.append(o); });
      sel.onchange = () => saveSettings({ weights: { [name]: sel.value } });
      const info = el("span", "help", T("me.sechelp." + name) + (S.inferred.includes(name) ? " " + T("me.guessed") : ""));
      row.append(lab, sel, info);
      wrap.append(row);
    });
    return wrap;
  }

  /* ---- sending the notes away: two steps ---- */
  function cloudBlock() {
    const box = el("div", "me-block me-risk"), on = S.settings.cloud_ok;
    box.append(el("h3", "", T("me.cloud.title")), el("p", "me-riskline", T("me.cloud.risk")), el("p", "help", T("me.cloud.help")));
    if (on) {
      box.append(el("p", "mb-bad", T("me.cloud.on")), btn(T("me.cloud.off"), "", () => { cloud = 0; saveSettings({ cloud_ok: false }); }));
    } else if (cloud === 0) {
      box.append(btn(T("me.cloud.allow"), "", () => { cloud = 1; paint(); }));
    } else if (cloud === 1) {
      box.append(el("p", "", T("me.cloud.step1")), btn(T("me.cloud.go"), "me-warn", () => { cloud = 2; paint(); }), btn(T("me.cancel"), "ghost", () => { cloud = 0; paint(); }));
    } else {
      box.append(el("p", "mb-bad", T("me.cloud.step2")), btn(T("me.cloud.confirm"), "me-warn", () => { cloud = 0; saveSettings({ cloud_ok: true }); }), btn(T("me.cancel"), "ghost", () => { cloud = 0; paint(); }));
    }
    return box;
  }

  /* ---- the notes ---- */
  function originText(i) {
    if (i.origin === "inferred") return T("me.origin.inferred", Math.round(i.confidence * 100));
    return T("me.origin." + i.origin);
  }
  function sectionSelect(kind, value, id) {
    const sel = el("select"); sel.id = id; sel.setAttribute("aria-label", T("me.sec.field")); sel.title = T("me.sec.tip");
    S.sections[kind].forEach(name => { const o = el("option", "", T("me.sec." + name)); o.value = name; o.selected = name === value; sel.append(o); });
    return sel;
  }
  function noteRow(i) {
    const li = el("li", "me-note" + (i.origin === "inferred" ? " me-guess" : ""));
    if (editing === i.id) {
      const form = el("form", "me-edit"), text = el("textarea"), sel = sectionSelect(i.kind, i.section, "me-es-" + i.id);
      text.value = i.text; text.maxLength = 400; text.rows = 2; text.setAttribute("aria-label", T("me.note.text"));
      form.onsubmit = async e => {
        e.preventDefault();
        const r = await api("POST", "api/memories/item", { id: i.id, text: text.value, section: sel.value });
        note = r.ok ? "" : errText(r);
        if (r.ok) editing = "";
        await refresh(true);
      };
      form.append(text, sel, btn(T("me.save"), "primary", () => form.requestSubmit()), btn(T("me.cancel"), "ghost", () => { editing = ""; paint(); }));
      li.append(form);
      return li;
    }
    const head = el("div", "me-nhead");
    head.append(el("span", "me-tag " + i.kind, T("me.sec." + i.section)), el("span", "me-origin", originText(i)),
      el("span", "me-when", T("me.seen", when(i.seen), i.mentions || 1)));
    li.append(head, el("p", "me-text", i.text));
    const bar = el("div", "me-nbar");
    if (i.origin === "inferred") bar.append(btn(T("me.confirm"), "", async () => { const r = await api("POST", "api/memories/confirm", { id: i.id }); note = r.ok ? "" : errText(r); await refresh(true); }, T("me.confirm.tip")));
    bar.append(btn(T("me.edit"), "ghost", () => { editing = i.id; paint(); }));
    const sure = armed === "d" + i.id;
    bar.append(btn(sure ? T("me.delete.sure") : T("me.delete"), sure ? "me-warn" : "ghost", async () => {
      if (!sure) { arm("d" + i.id); return; }
      armed = ""; const r = await api("POST", "api/memories/delete", { id: i.id }); note = r.ok ? "" : errText(r); await refresh(true);
    }));
    li.append(bar);
    return li;
  }
  function filterBar() {
    const bar = el("div", "me-filter");
    const kind = el("select"), kl = el("label", "sr", T("me.f.kind")), sec = el("select"), sl = el("label", "sr", T("me.f.section")), q = el("input"), ql = el("label", "sr", T("me.f.search"));
    kind.id = "me-fk"; sec.id = "me-fs"; q.id = "me-fq"; kl.htmlFor = "me-fk"; sl.htmlFor = "me-fs"; ql.htmlFor = "me-fq";
    [["", T("me.f.all")], ["long", T("me.long.name")], ["short", T("me.short.name")]].forEach(([v, t]) => { const o = el("option", "", t); o.value = v; o.selected = v === fKind; kind.append(o); });
    const none = el("option", "", T("me.f.allsections")); none.value = ""; sec.append(none);
    (fKind ? S.sections[fKind] : [...S.sections.long, ...S.sections.short]).forEach(n => { const o = el("option", "", T("me.sec." + n)); o.value = n; o.selected = n === fSection; sec.append(o); });
    q.type = "search"; q.placeholder = T("me.f.search"); q.value = query; q.autocomplete = "off";
    kind.onchange = () => { fKind = kind.value; fSection = ""; refresh(true); };
    sec.onchange = () => { fSection = sec.value; refresh(true); };
    q.oninput = () => { clearTimeout(finding); finding = setTimeout(async () => { query = q.value; await refresh(true); const again = document.getElementById("me-fq"); if (again) { again.focus(); again.setSelectionRange(query.length, query.length); } }, 300); };
    bar.append(kl, kind, sl, sec, ql, q);
    return bar;
  }
  function addForm() {
    const form = el("form", "me-add"), kind = el("select"), text = el("input"), tl = el("label", "sr", T("me.add.text"));
    let sec = sectionSelect("long", "identity", "me-as");
    kind.id = "me-ak"; text.id = "me-at"; tl.htmlFor = "me-at";
    kind.setAttribute("aria-label", T("me.kind.field")); kind.title = T("me.kind.tip");
    [["long", T("me.long.name")], ["short", T("me.short.name")]].forEach(([v, t]) => { const o = el("option", "", t); o.value = v; kind.append(o); });
    kind.onchange = () => { const next = sectionSelect(kind.value, S.sections[kind.value][0], "me-as"); sec.replaceWith(next); sec = next; };
    text.maxLength = 400; text.placeholder = T("me.add.ph"); text.autocomplete = "off";
    form.onsubmit = async e => {
      e.preventDefault();
      if (!text.value.trim()) return;
      const r = await api("POST", "api/memories/item", { kind: kind.value, section: sec.value, text: text.value });
      note = r.ok ? "" : errText(r);
      if (r.ok) text.value = "";
      await refresh(true);
    };
    form.append(kind, sec, tl, text, btn(T("me.add"), "primary", () => form.requestSubmit()));
    return form;
  }
  function notesBlock() {
    const box = el("div", "me-block me-notes");
    box.append(el("h3", "", T("me.notes.title")), filterBar());
    const list = el("ul", "me-list");
    S.items.forEach(i => list.append(noteRow(i)));
    box.append(S.items.length ? list : el("p", "dim", T(query || fKind || fSection ? "me.notes.nomatch" : "me.notes.none")));
    if (S.more) box.append(el("p", "help", T("me.notes.more", S.more)));
    box.append(el("h4", "", T("me.add.title")), addForm());
    return box;
  }

  /* ---- what was learned lately, and the clearing ---- */
  function logBlock() {
    const box = el("div", "me-block"), w = S.worker;
    box.append(el("h3", "", T("me.log.title")));
    box.append(el("p", "help", w.waiting ? T("me.log.waiting", w.waiting) : T("me.log.none.waiting")));
    if (w.error) box.append(el("p", "mb-bad", T("me.log.error", w.error)));
    const list = el("ul", "me-log");
    S.log.forEach(l => list.append(el("li", "", `${when(l.at)} · ${T("me.log." + l.what.replace(" ", "_"))} · ${T("me.sec." + l.section)}: ${l.text}`)));
    box.append(S.log.length ? list : el("p", "dim", T("me.log.empty")));
    return box;
  }
  /* ---- the cards of the past conversations: how many are written, and the back-fill of the old ones ---- */
  function cardsBlock() {
    const c = S.cards;
    if (!c) return null;
    const box = el("div", "me-block");
    box.append(el("h3", "", T("me.cards.title")), el("p", "help", T("me.cards.help")));
    box.append(el("p", "me-cards", T("me.cards.progress", c.current, c.total) + (c.fallback ? " " + T("me.cards.bycode", c.fallback) : "")));
    box.append(el("p", "help", T(c.backfill ? (c.current >= c.total ? "me.cards.done" : "me.cards.on") : "me.cards.off")));
    if (c.error) box.append(el("p", "mb-bad", T("me.log.error", c.error)));
    box.append(btn(T(c.backfill ? "me.cards.stop" : "me.cards.start"), "ghost", async () => {
      const r = await api("POST", "api/memories/cards", { backfill: !c.backfill });
      note = r.ok ? "" : errText(r);
      await refresh(true);
    }));
    return box;
  }
  /* ---- a copy of the notes as Markdown files, to read or edit outside; the encrypted store stays the master ---- */
  function exportBlock() {
    const box = el("div", "me-block");
    box.append(el("h3", "", T("me.export.title")), el("p", "help", T("me.export.help")));
    box.append(btn(T("me.export.go"), "", async () => {
      const r = await api("POST", "api/memories/export", {});
      exported = r.ok ? r.data : null;
      note = r.ok ? "" : errText(r);
      paint();
    }));
    if (exported) box.append(el("p", "me-exported", T("me.export.done", exported.notes, exported.folder)));
    return box;
  }
  function dangerBlock() {
    const box = el("div", "me-block me-danger");
    box.append(el("h3", "", T("me.clear.title")), el("p", "help", T("me.clear.help")));
    ["long", "short", ""].forEach(kind => {
      const key = "c" + kind, sure = armed === key, count = kind ? S.counts[kind] : S.counts.long + S.counts.short;
      const label = kind ? T(`me.clear.${kind}`, count) : T("me.clear.all", count);
      box.append(btn(sure ? T("me.clear.sure", count) : label, sure ? "me-warn" : "ghost", async () => {
        if (!count) return;
        if (!sure) { arm(key); return; }
        armed = ""; const r = await api("POST", "api/memories/clear", { kind: kind || null, confirm: true });
        note = r.ok ? "" : errText(r);
        await refresh(true);
      }));
    });
    return box;
  }

  function paint() {
    if (!S) { host.replaceChildren(el("p", "dim", note || T("me.loading"))); return; }
    const focus = document.activeElement && document.activeElement.id;
    const cols = el("div", "me-cols");
    cols.append(memoryBlock("long"), memoryBlock("short"));
    host.replaceChildren(el("p", "me-intro", T("me.intro")), ...(note ? [el("p", "mb-bad", note)] : []), cols, cloudBlock(), notesBlock(), logBlock(), ...(S.cards ? [cardsBlock()] : []), exportBlock(), dangerBlock());
    const again = focus && document.getElementById(focus);
    if (again && again !== document.activeElement) again.focus();
  }

  document.addEventListener("tadamoo:sub", e => { if (e.detail === "memories") { refresh(true); poll(); } else clearTimeout(timer); });
  document.addEventListener("tadamoo:lang", () => { if (S) paint(); });
  document.addEventListener("visibilitychange", poll);
})();
