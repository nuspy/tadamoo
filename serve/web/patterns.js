"use strict";
/* Context patterns in the Chat settings: six ways to shape what the model reads, each with a checkbox, a strategy to choose
   and a description of four parts (what it does, advantages, drawbacks, when to use it) plus what it does to the server's cache.
   The table is built from the server's catalog (GET api/context/patterns), so a new strategy needs no change here. They apply
   to the Chat's own conversations with the agent; other programs using the server are not affected. The catalog's text is put
   in the page as text. */
(() => {
  const box = document.getElementById("cs-patterns");
  if (!box) return;
  let cat = null, value = {}, onchange = () => {};
  const opened = new Set();                                      // descriptions the person opened: they stay open when the table is redrawn

  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const pick = o => (o && (o[LANG] || o.en)) || "";
  const entry = name => ({ ...((cat && cat.defaults[name]) || { on: false, strategy: "" }), ...(value[name] || {}) });

  function terms(text) {
    const t = pick(text);
    return h("dl", "pt-text", ...["what", "pros", "cons", "when", "cache"].flatMap(k => [h("dt", "", T("pt." + k)), h("dd", "", t[k])]));
  }
  /* one button per row: it opens what the pattern is and what the chosen strategy is */
  function info(name, strategy) {
    const p = cat.patterns[name], id = `pt-${name}`;
    const btn = h("button", "pt-info", "i");
    btn.type = "button"; btn.setAttribute("aria-expanded", String(opened.has(id))); btn.setAttribute("aria-controls", id);
    btn.setAttribute("aria-label", T("pt.about", pick(p.label)));
    const body = h("div", "pt-more", h("h4", "", pick(p.label)), terms(p.text),
      h("h4", "", pick(p.strategies[strategy].label)), terms(p.strategies[strategy].text));
    body.id = id; body.hidden = !opened.has(id);
    btn.onclick = () => {
      body.hidden = !body.hidden; btn.setAttribute("aria-expanded", String(!body.hidden));
      if (body.hidden) opened.delete(id); else opened.add(id);
    };
    return [btn, body];
  }

  function draw() {
    if (!cat) return;
    const rows = cat.order.map(name => {
      const p = cat.patterns[name], cur = entry(name);
      const on = h("input"); on.type = "checkbox"; on.checked = cur.on; on.id = `pt-on-${name}`;
      const many = Object.keys(p.strategies).length > 1;
      const sel = h("select", "", Object.entries(p.strategies).map(([k, s]) => Object.assign(h("option", "", pick(s.label)), { value: k })));
      sel.value = cur.strategy; sel.id = `pt-sel-${name}`; sel.disabled = !cur.on;
      sel.setAttribute("aria-label", T("pt.strategy", pick(p.label)));
      const set = patch => { value = { ...value, [name]: { ...entry(name), ...patch } }; draw(); onchange(); };
      on.onchange = () => set({ on: on.checked });
      sel.onchange = () => set({ strategy: sel.value });
      const [ib, ibody] = info(name, cur.strategy);
      const label = h("label", "pt-name", on, h("span", "", pick(p.label)));
      return h("div", "pt-row", h("div", "pt-line", label, many ? sel : h("span", "dim", pick(p.strategies[cur.strategy].label)), ib), ibody,
        p.exists ? null : h("p", "dim pt-new", T("pt.new")));
    });
    box.replaceChildren(...rows);
  }

  async function load() {
    try {
      const r = await fetch("api/context/patterns", { cache: "no-store", headers: authHeaders() });
      if (r.ok) { cat = await r.json(); draw(); }
    } catch (e) { box.replaceChildren(h("p", "dim", T("pt.offline"))); }
  }

  window.ctxPatterns = {
    read: () => JSON.parse(JSON.stringify(value)),
    write: v => { value = v && typeof v === "object" ? JSON.parse(JSON.stringify(v)) : {}; draw(); },
    onchange: fn => { onchange = fn; },
  };
  load();
})();
