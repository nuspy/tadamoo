"use strict";
/* The skill picker: one multi-select of the installed skills, used where a plan or a plan step is given skills (Plans > the step, Plans > the plan's profile).
   `window.skillPick.create({T, ids, onChange, disabled})` gives `{el, findId, set(ids), get(), refresh()}`; the skills come from GET api/skills.
   What the server says enters the page as text. */
(function (root) {
  /* ---- pure parts ---- */
  const norm = s => String(s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
  const titleOf = (r, lang) => (r.title && (r.title[lang] || r.title.en)) || r.name;

  /* the rows to draw: every installed skill that matches `filter`, then the selected names that are not installed any more; `on` says whether it is picked */
  function rows(skills, selected, filter, lang) {
    const sel = new Set(selected || []), q = norm(filter).trim(), known = new Set();
    const out = (skills || []).filter(r => r.state !== "not_installed").filter(r => { known.add(r.name); return !q || norm(r.name).includes(q) || norm(titleOf(r, lang)).includes(q); })
      .map(r => ({ name: r.name, title: titleOf(r, lang), off: r.state === "disabled", on: sel.has(r.name), missing: false }));
    (selected || []).filter(n => !known.has(n)).forEach(n => out.push({ name: n, title: n, off: false, on: true, missing: true }));
    return out;
  }
  const toggled = (names, name, on) => (on ? (names.includes(name) ? names.slice() : [...names, name]) : names.filter(n => n !== name));
  const api = { rows, toggled };
  root.skillPick = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof document === "undefined" || !document.createElement) return;

  /* ---- the page part ---- */
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  let seq = 0;

  function create(opts) {
    const T = opts.T || (k => k), id = `sp-${++seq}`, lang = typeof LANG !== "undefined" ? LANG : "en";
    let names = (opts.ids || []).slice(), skills = [], filter = "", state = "loading";
    const box = el("div", "rp"), find = el("input", "rp-find"), count = el("p", "rp-count"), list = el("ul", "rp-list"), msg = el("p", "rp-msg");
    find.type = "search"; find.id = `${id}-find`; find.autocomplete = "off"; find.spellcheck = false; find.placeholder = T("sp.filter");
    find.setAttribute("aria-label", T("sp.filter"));
    list.setAttribute("aria-label", opts.label || T("sp.title"));
    find.addEventListener("input", () => { filter = find.value; paint(); });
    box.append(find, count, msg, list);

    function paint() {
      const shown = rows(skills, names, filter, lang);
      list.replaceChildren();
      shown.forEach(r => {
        const li = el("li", "rp-item"), lab = el("label", "rp-row"), tick = el("input"), text = el("span", "rp-text"), badges = el("span", "rp-badges");
        tick.type = "checkbox"; tick.checked = r.on; tick.disabled = !!opts.disabled; tick.value = r.name;
        tick.addEventListener("change", () => { names = toggled(names, r.name, tick.checked); paint(); if (opts.onChange) opts.onChange(names.slice()); });
        text.append(el("span", "rp-name", r.title));
        if (r.missing) badges.append(el("span", "rp-badge", T("sp.missing")));
        else if (r.off) badges.append(el("span", "rp-badge", T("sp.off")));
        lab.append(tick, text, badges);
        li.append(lab);
        list.append(li);
      });
      count.textContent = T("sp.count", names.length);
      msg.textContent = state === "loading" ? T("sp.loading") : state === "offline" ? T("sp.offline") : (!shown.length ? T(filter ? "sp.nomatch" : "sp.none") : "");
      msg.hidden = !msg.textContent;
    }
    async function refresh() {
      state = "loading"; paint();
      try {
        const r = await fetch("api/skills", { cache: "no-store", headers: typeof authHeaders === "function" ? authHeaders() : {} });
        const d = r.ok ? await r.json() : null;
        skills = d && Array.isArray(d.skills) ? d.skills : []; state = d ? "ready" : "offline";
      } catch (e) { skills = []; state = "offline"; }
      paint();
    }
    paint(); refresh();
    return { el: box, findId: find.id, get: () => names.slice(), set(next) { names = (next || []).slice(); paint(); }, refresh };
  }
  api.create = create;
})(typeof window !== "undefined" ? window : globalThis);
