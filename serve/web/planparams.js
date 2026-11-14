"use strict";
/* The Parameters tab of a plan: the plan's values the person sets, each with its default, grouped under the headings the plan gives them. A parameter is drawn by its kind: a text is a field
   that is one line when empty and grows, a number is a number field, a choice of two is two buttons, a longer choice is a menu with "Other, I write" when the plan allows it. Nothing
   is sent until Save, and only what changed. Everything the server says is put in the page as text. */
(() => {
  const lang = () => (typeof LANG === "string" ? LANG : "en");
  const pick = (r, name, l) => ((l === "en" ? r[name + "_en"] || r[name] : r[name] || r[name + "_en"]) || "");
  const labelOf = (c, l) => (l === "en" ? c.label_en || c.label : c.label || c.label_en) || c.value;
  const same = (a, b) => String(a === null || a === undefined ? "" : a) === String(b === null || b === undefined ? "" : b);
  const h = (tag, cls, ...kids) => { const e = document.createElement(tag); if (cls) e.className = cls; kids.flat().forEach(k => e.append(k)); return e; };

  /* one field: {el, read(), set(value)} */
  function field(p, l, T, onChange) {
    const id = "pl-par-" + p.node_id, cur = p.value;
    if (p.kind === "choice" && p.choices.length === 2) {
      const row = h("div", "pl-par-two"); row.setAttribute("role", "group");
      let value = String(cur);
      const buttons = p.choices.map(c => {
        const b = h("button", "btn pl-btn", labelOf(c, l)); b.type = "button"; b.dataset.value = c.value;
        b.addEventListener("click", () => { value = c.value; paint(); onChange(); });
        return b;
      });
      const paint = () => buttons.forEach(b => b.setAttribute("aria-pressed", String(b.dataset.value === value)));
      row.append(...buttons); paint();
      return { el: row, read: () => value, set: v => { value = String(v); paint(); } };
    }
    if (p.kind === "choice") {
      const sel = h("select", "pl-par-select"); sel.id = id;
      const values = p.choices.map(c => c.value);
      p.choices.forEach(c => { const o = h("option", "", labelOf(c, l)); o.value = c.value; sel.append(o); });
      let other = null;
      if (p.other) {
        const o = h("option", "", T("pl.par.other")); o.value = "\u0000other"; sel.append(o);
        other = h("input", "pl-par-other"); other.type = "text"; other.setAttribute("aria-label", T("pl.par.other"));
      }
      const apply = v => {
        const known = values.includes(String(v));
        sel.value = known ? String(v) : (other ? "\u0000other" : String(v));
        if (other) { other.hidden = known; other.value = known ? "" : String(v); }
      };
      sel.addEventListener("change", () => { if (other) other.hidden = sel.value !== "\u0000other"; onChange(); if (other && !other.hidden) other.focus(); });
      if (other) other.addEventListener("input", onChange);
      apply(cur);
      const box = h("div", "pl-par-choice", sel, other || "");
      return { el: box, read: () => (other && sel.value === "\u0000other" ? other.value : sel.value), set: apply };
    }
    if (p.kind === "number") {
      const inp = h("input", "pl-par-num"); inp.type = "number"; inp.id = id; inp.step = "any"; inp.inputMode = "decimal"; inp.value = String(cur === null || cur === undefined ? "" : cur);
      inp.addEventListener("input", onChange);
      return { el: inp, read: () => inp.value, set: v => { inp.value = String(v === null || v === undefined ? "" : v); } };
    }
    const ta = h("textarea", "pl-par-text"); ta.id = id; ta.rows = 1; ta.value = String(cur === null || cur === undefined ? "" : cur);
    if (ta.value.includes("\n") || ta.value.length > 90) ta.rows = Math.min(8, ta.value.split("\n").length + 1);
    ta.addEventListener("input", onChange);
    return { el: ta, read: () => ta.value, set: v => { ta.value = String(v === null || v === undefined ? "" : v); ta.dispatchEvent(new Event("input")); } };
  }

  /* draws the tab into `host`; `opts.save(values)` posts {name: value} and answers {ok, error?}; `opts.T` is the page's translator */
  function draw(host, params, opts) {
    const T = opts.T, l = lang(), fields = [];
    host.replaceChildren();
    host.append(h("p", "help pad", T("pl.par.help")));
    if (!params.length) { host.append(h("p", "dim pad", T("pl.par.none"))); return; }
    const count = h("span", "pl-par-count", T("pl.par.unchanged")), msg = h("p", "pl-par-msg"); msg.setAttribute("role", "status");
    const dirty = () => fields.filter(x => !same(x.f.read(), x.p.value));
    const changed = () => { const n = dirty().length; count.textContent = n ? T("pl.par.changed", n) : T("pl.par.unchanged"); save.disabled = !n; };
    const save = h("button", "btn primary pl-btn", T("pl.par.save")); save.type = "button"; save.disabled = true;
    const body = h("div", "pl-par-list");
    let heading = null;
    params.forEach(p => {
      const g = pick(p, "group", l);
      if (g !== heading) { heading = g; body.append(h("h3", "pl-par-group", g || T("pl.par.group.none"))); }
      const f = field(p, l, T, changed);
      const row = h("div", "pl-par"), lab = h("label", "pl-par-q", pick(p, "prompt", l) || p.title);
      if (f.el.id) lab.htmlFor = f.el.id;
      row.dataset.name = p.name;
      row.append(lab);
      const help = pick(p, "help", l);
      if (help) row.append(h("p", "pl-par-help", help));
      const reset = h("button", "btn pl-btn pl-par-reset", T("pl.par.reset")); reset.type = "button";
      reset.addEventListener("click", () => { f.set(p.default); changed(); });
      row.append(f.el, h("p", "pl-par-def", T("pl.par.default", String(p.default) === "" ? T("pl.par.empty") : String(p.default).slice(0, 80)), reset));
      body.append(row);
      fields.push({ p, f });
    });
    save.addEventListener("click", async () => {
      const values = {};
      dirty().forEach(({ p, f }) => { values[p.name] = f.read(); });
      save.disabled = true;
      const r = await opts.save(values);
      msg.classList.toggle("bad", !r.ok);
      msg.textContent = r.ok ? T("pl.par.saved") : (r.error || "");
      if (!r.ok) changed();
    });
    host.append(body, h("div", "pl-par-foot", save, count, msg));
  }
  window.PlanParams = { draw };
})();
