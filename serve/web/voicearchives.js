"use strict";
/* The knowledge archives a voice function draws on: a checklist (Settings > Voice, a planned meeting) and the question the voice service puts to the person when
   something an archive says is about to reach other people (a dialog that opens on any page). Archives are stored by id and shown by name; everything the server says
   enters the page as text. The list is `GET api/rag/catalog`. */
(() => {
  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  async function call(method, path, payload) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(payload ? { "Content-Type": "application/json" } : {}), ...(typeof authHeaders === "function" ? authHeaders() : {}) }, body: payload ? JSON.stringify(payload) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  let catalog = null, loading = null;
  const load = () => loading || (loading = call("GET", "api/rag/catalog").then(r => { catalog = r.ok ? (r.data.archives || []) : []; loading = null; return catalog; }));

  /* picker({id, label, help, ids, onSave, inherit}) -> a fieldset. `ids` are the archives chosen now (null: the item follows the settings); `onSave(ids)` is called
     after each change and may return a promise. `inherit` (a text) adds a first box "follow the settings" that stands for null. */
  function picker(opt) {
    const box = h("fieldset", "va-pick");
    box.id = opt.id;
    const list = h("div", "va-list");
    let chosen = opt.ids == null ? null : [...opt.ids];
    const save = async ids => { chosen = ids; try { await opt.onSave(ids); } catch (e) { /* the page that owns the save shows its error */ } };
    function draw() {
      list.textContent = "";
      if (opt.inherit) {
        const i = h("input"); i.type = "checkbox"; i.checked = chosen == null; i.id = opt.id + "-inherit";
        i.onchange = () => { chosen = i.checked ? null : []; save(chosen); draw(); };
        list.append(h("label", "va-row", i, h("span", "", opt.inherit)));
      }
      const off = !!opt.inherit && chosen == null;
      if (!catalog) { list.append(h("p", "help", T("va.loading"))); return; }
      if (!catalog.length) { list.append(h("p", "help", T("va.none"))); return; }
      catalog.forEach(a => {
        const i = h("input"); i.type = "checkbox"; i.value = a.id; i.checked = !!chosen && chosen.includes(a.id); i.disabled = off;
        i.onchange = () => { const now = new Set(chosen || []); if (i.checked) now.add(a.id); else now.delete(a.id); save([...now]); };
        const where = a.project_name ? " - " + a.project_name : "";
        list.append(h("label", "va-row", i, h("span", "", a.name + where)));
      });
      const known = new Set(catalog.map(a => a.id)), gone = (chosen || []).filter(x => !known.has(x));
      if (gone.length) list.append(h("p", "help", T("va.gone", gone.length)));
    }
    box.append(h("legend", "", opt.label), h("p", "help", opt.help || ""), list);
    draw();
    load().then(draw);
    box.refresh = ids => { chosen = ids == null ? null : [...ids]; draw(); };
    return box;
  }
  window.VoiceArchives = { picker };

  /* The question: a dialog on whatever page is open, with the four answers. */
  let shown = "", timer = 0;
  const dlg = h("dialog", "va-dialog");
  dlg.id = "va-card"; dlg.setAttribute("aria-labelledby", "va-card-h");
  dlg.addEventListener("cancel", e => e.preventDefault());
  document.body.append(dlg);
  function question(rows) {
    const q = rows && rows[0];
    if (!q) { shown = ""; if (dlg.open) dlg.close(); return; }
    if (q.id === shown && dlg.open) return;
    shown = q.id;
    const answer = async a => { await call("POST", "api/voice/consent/answer", { id: q.id, answer: a }); shown = ""; dlg.close(); poll(); };
    const btns = [["once", "va.q.once", "btn primary"], ["meeting", "va.q.meeting", "btn"], ["always", "va.q.always", "btn"], ["no", "va.q.no", "btn"]].map(([a, key, cls]) => {
      const b = h("button", cls + " va-btn", T(key)); b.type = "button"; b.onclick = () => answer(a); return b;
    });
    dlg.textContent = "";
    dlg.append(h("form", "va-form",
      Object.assign(h("h2", "", T("va.q.title", q.archive)), { id: "va-card-h" }),
      h("p", "", T("va.q.what." + q.how)),
      h("blockquote", "va-preview", q.preview || ""),
      q.fallback ? h("p", "help", T("va.q.fallback")) : q.via === "voice" ? h("p", "help", T("va.q.voice")) : null,
      h("p", "help", T("va.q.help")),
      h("div", "va-actions", ...btns)));
    if (!dlg.open) dlg.showModal();
    btns[0].focus();
  }
  async function poll() {
    clearTimeout(timer);
    if (document.visibilityState === "visible") {
      const r = await call("GET", "api/voice/consent");
      if (r.ok) question(r.data.questions || []);
    }
    timer = setTimeout(poll, 2000);
  }
  document.addEventListener("tadamoo:lang", () => { shown = ""; poll(); });
  poll();
})();
