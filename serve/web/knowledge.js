"use strict";
/* Settings > Memories > Project notes, wiki and reference documents: how strict the notes added to a request are, the address of the wiki
   (wikibridge) and the folder of reference documents that the Chat and the Agent use. Everything the server says is put in the page as text. */
(() => {
  const root = document.getElementById("kn-root");
  if (!root) return;
  let state = null, note = "", noteBad = false, check = "";

  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  async function call(method, path, payload) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(payload ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: payload ? JSON.stringify(payload) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const why = r => r.status === 0 ? T("kn.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`);

  function field(id, label, help, input) {
    input.id = id;
    const l = h("label", "", label);
    l.htmlFor = id;
    return h("div", "kn-field", l, input, h("p", "setnote kn-help", help));
  }
  function number(id, value, lo, hi, step) {
    const i = h("input"); i.type = "number"; i.min = String(lo); i.max = String(hi); i.step = String(step); i.value = String(value); i.inputMode = "decimal";
    return i;
  }
  function text(value, ph) {
    const i = h("input"); i.type = "text"; i.value = value || ""; i.placeholder = ph || ""; i.spellcheck = false; i.autocomplete = "off";
    return i;
  }

  async function save(values) {
    const r = await call("POST", "api/knowledge", values);
    if (r.ok) { state = r.data; note = T("kn.saved"); noteBad = false; } else { note = why(r); noteBad = true; }
    draw();
  }

  function draw() {
    if (!state) return;
    const s = state, L = s.limits;
    const minScore = number("kn-score", s.memory_min_score, L.memory_min_score[0], L.memory_min_score[1], 0.1);
    const notes = number("kn-notes", s.memory_notes, L.memory_notes[0], L.memory_notes[1], 1);
    const budget = number("kn-budget", s.memory_budget_tokens, L.memory_budget_tokens[0], L.memory_budget_tokens[1], 50);
    const wikiOn = h("input"); wikiOn.type = "checkbox"; wikiOn.checked = s.wiki_enabled;
    const wikiUrl = text(s.wiki_url, "http://127.0.0.1:8775");
    const wikiProject = text(s.wiki_project, "");
    const folder = text(s.reference_folder, "D:\\docs");
    const strategy = h("select", "", s.strategies.map(k => Object.assign(h("option", "", T("kn.strategy." + k)), { value: k })));
    strategy.value = s.reference_strategy;
    const result = h("p", "setnote kn-check", check);
    result.setAttribute("role", "status");

    const values = () => ({ memory_min_score: Number(minScore.value), memory_notes: Number(notes.value), memory_budget_tokens: Number(budget.value),
      wiki_enabled: wikiOn.checked, wiki_url: wikiUrl.value.trim(), wiki_project: wikiProject.value.trim(), reference_folder: folder.value.trim(), reference_strategy: strategy.value });
    const saveBtn = h("button", "btn primary", T("kn.save")); saveBtn.type = "button"; saveBtn.onclick = () => save(values());
    const defaults = h("button", "btn ghost", T("kn.defaults")); defaults.type = "button";
    defaults.onclick = () => save({ memory_min_score: s.defaults.memory_min_score, memory_notes: s.defaults.memory_notes, memory_budget_tokens: s.defaults.memory_budget_tokens });
    const test = h("button", "btn", T("kn.wiki.check")); test.type = "button";
    test.onclick = async () => {
      check = T("kn.wiki.checking"); draw();
      const r = await call("POST", "api/knowledge/wiki-check", { wiki_url: wikiUrl.value.trim() });
      if (!r.ok) check = why(r);
      else if (r.data.status === "ok") check = T("kn.wiki.ok", r.data.address, (r.data.projects || []).length);
      else check = T("kn.wiki.bad", r.data.address, r.data.error || r.data.status);
      draw();
    };
    const status = h("p", "setnote kn-status", note); status.setAttribute("role", "status"); status.hidden = !note; status.dataset.bad = noteBad ? "1" : "";
    const wikiRow = h("label", "kn-check", wikiOn, h("span", "", T("kn.wiki.on")));

    root.replaceChildren(
      h("fieldset", "csgroup", h("legend", "", T("kn.notes.title")), h("p", "setnote kn-help", T("kn.notes.help")),
        field("kn-score", T("kn.score"), T("kn.score.help"), minScore), field("kn-notes", T("kn.count"), T("kn.count.help"), notes),
        field("kn-budget", T("kn.budget"), T("kn.budget.help"), budget)),
      h("fieldset", "csgroup", h("legend", "", T("kn.wiki.title")), h("p", "setnote kn-help", T("kn.wiki.help")), wikiRow,
        field("kn-wurl", T("kn.wiki.url"), T("kn.wiki.url.help"), wikiUrl), field("kn-wproj", T("kn.wiki.project"), T("kn.wiki.project.help"), wikiProject),
        h("div", "kn-actions", test), result),
      h("fieldset", "csgroup", h("legend", "", T("kn.ref.title")), h("p", "setnote kn-help", T("kn.ref.help")),
        field("kn-folder", T("kn.ref.folder"), T("kn.ref.folder.help"), folder), field("kn-strategy", T("kn.ref.strategy"), T("kn.ref.strategy.help"), strategy),
        s.reference_folder && !s.reference_found ? h("p", "setnote warnnote", T("kn.ref.missing", s.reference_folder)) : null),
      h("div", "kn-actions", saveBtn, defaults), status);
  }

  async function load() {
    const r = await call("GET", "api/knowledge");
    if (r.ok) { state = r.data; draw(); } else root.replaceChildren(h("p", "setnote warnnote", why(r)));
  }
  document.addEventListener("tadamoo:view", e => { if (e.detail === "settings") load(); });
  if (document.body.dataset.view === "settings") load();
})();
