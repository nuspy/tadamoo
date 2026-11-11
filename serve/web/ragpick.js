"use strict";
/* The archive picker: one multi-select of knowledge archives, by name and project, with what each allows (models outside this PC, other people), used
   wherever a function takes archives: a plan, a plan step, the RAG node, a routine, and the settings of meetings and the copilot.
   `window.ragPick.create({T, ids, onChange, disabled})` gives `{el, set(ids), get(), refresh()}`; the archives come from `window.ragUi.catalog()`
   (GET /api/rag/catalog). What the server says enters the page as text. */
(function (root) {
  /* ---- pure parts ---- */
  const OTHERS = ["ask_dialog", "allow", "deny", "ask_voice"];
  const OTHERS_WORDS = { ask_dialog: "rp.others.ask_dialog", allow: "rp.others.allow", deny: "rp.others.deny", ask_voice: "rp.others.ask_voice" };
  const norm = s => String(s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");

  /* the rows to draw: every archive that matches `filter` (name or project), then the selected ids that no longer exist; `on` says whether it is picked */
  function rows(archives, selected, filter) {
    const sel = new Set(selected || []), q = norm(filter).trim(), known = new Set();
    const out = (archives || []).filter(a => { known.add(a.id); return !q || norm(a.name).includes(q) || norm(a.project_name).includes(q); })
      .map(a => ({ id: a.id, name: a.name, project: a.project_name || "", cloud: !!a.cloud_ok, others: OTHERS.includes(a.others_ok) ? a.others_ok : "ask_dialog", on: sel.has(a.id), missing: false }));
    (selected || []).filter(id => !known.has(id)).forEach(id => out.push({ id, name: id, project: "", cloud: false, others: "ask_dialog", on: true, missing: true }));
    return out;
  }
  /* the ids after one is ticked or unticked, in the order they were picked */
  const toggled = (ids, id, on) => (on ? (ids.includes(id) ? ids.slice() : [...ids, id]) : ids.filter(i => i !== id));
  const api = { OTHERS, rows, toggled };
  root.ragPick = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof document === "undefined" || !document.createElement) return;

  /* ---- the page part ---- */
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  let seq = 0;

  function create(opts) {
    const T = opts.T || (k => k), id = `rp-${++seq}`;
    let ids = (opts.ids || []).slice(), archives = [], filter = "", state = "loading";
    const box = el("div", "rp"), find = el("input", "rp-find"), count = el("p", "rp-count"), list = el("ul", "rp-list"), msg = el("p", "rp-msg");
    find.type = "search"; find.id = `${id}-find`; find.autocomplete = "off"; find.spellcheck = false; find.placeholder = T("rp.filter");
    find.setAttribute("aria-label", T("rp.filter"));
    list.setAttribute("aria-label", opts.label || T("rp.title"));
    find.addEventListener("input", () => { filter = find.value; paint(); });
    box.append(find, count, msg, list);

    function badge(text) { return el("span", "rp-badge", text); }
    function paint() {
      const shown = rows(archives, ids, filter);
      list.replaceChildren();
      shown.forEach(r => {
        const li = el("li", "rp-item"), lab = el("label", "rp-row"), box1 = el("input"), text = el("span", "rp-text"), badges = el("span", "rp-badges");
        box1.type = "checkbox"; box1.checked = r.on; box1.disabled = !!opts.disabled; box1.value = r.id;
        box1.addEventListener("change", () => { ids = toggled(ids, r.id, box1.checked); paint(); if (opts.onChange) opts.onChange(ids.slice()); });
        text.append(el("span", "rp-name", r.name), ...(r.project ? [el("span", "rp-project", r.project)] : []));
        if (r.missing) badges.append(badge(T("rp.missing")));
        else badges.append(badge(r.cloud ? T("rp.cloud.on") : T("rp.cloud.off")), badge(T(OTHERS_WORDS[r.others])));
        lab.append(box1, text, badges);
        li.append(lab);
        list.append(li);
      });
      count.textContent = T("rp.count", ids.length);
      msg.textContent = state === "loading" ? T("rp.loading") : state === "offline" ? T("rp.offline") : (!shown.length ? T(filter ? "rp.nomatch" : "rp.none") : "");
      msg.hidden = !msg.textContent;
    }
    async function refresh() {
      state = "loading"; paint();
      let got = null;
      try { got = root.ragUi && root.ragUi.catalog ? await root.ragUi.catalog() : null; } catch (e) { got = null; }
      archives = got ? got.archives : []; state = got ? "ready" : "offline";
      paint();
    }
    paint(); refresh();
    return { el: box, get: () => ids.slice(), set(next) { ids = (next || []).slice(); paint(); }, refresh };
  }
  api.create = create;
})(typeof window !== "undefined" ? window : globalThis);
