"use strict";
/* Settings > General > Long work: whether the older steps of a long piece of work are summarised when the context is nearly full, and at
   what share of the prompt budget. Saved on this PC; a conversation started afterwards uses them. Everything the server says is put in the
   page as text. */
(() => {
  const root = document.getElementById("cx-root");
  if (!root) return;
  let state = null, note = "", noteBad = false;

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
  const why = r => r.status === 0 ? T("cx.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`);

  async function save(values) {
    const r = await call("POST", "api/compaction", values);
    if (r.ok) { state = r.data; note = T("cx.saved"); noteBad = false; } else { note = why(r); noteBad = true; }
    draw();
  }

  function draw() {
    if (!state) return;
    const s = state, L = s.limits.compaction_threshold;
    const on = h("input"); on.type = "checkbox"; on.id = "cx-on"; on.checked = s.compaction; on.setAttribute("aria-describedby", "cx-on-h");
    const pct = h("input"); pct.type = "number"; pct.id = "cx-pct"; pct.min = String(Math.round(L[0] * 100)); pct.max = String(Math.round(L[1] * 100)); pct.step = "1";
    pct.inputMode = "numeric"; pct.value = String(Math.round(s.compaction_threshold * 100)); pct.setAttribute("aria-describedby", "cx-pct-h");
    const help = (id, text) => Object.assign(h("p", "setnote kn-help", text), { id });
    const share = () => Number(pct.value) / 100;
    const saveBtn = h("button", "btn primary", T("cx.save")); saveBtn.type = "button"; saveBtn.onclick = () => save({ compaction: on.checked, compaction_threshold: share() });
    const defaults = h("button", "btn ghost", T("cx.defaults")); defaults.type = "button";
    defaults.onclick = () => save({ compaction: s.defaults.compaction, compaction_threshold: s.defaults.compaction_threshold });
    const status = h("p", "setnote kn-status", note); status.setAttribute("role", "status"); status.hidden = !note; status.dataset.bad = noteBad ? "1" : "";
    root.replaceChildren(
      h("fieldset", "csgroup", h("legend", "", T("cx.title")),
        h("label", "kn-check", on, h("span", "", T("cx.on"))), help("cx-on-h", T("cx.on.help")),
        h("div", "kn-field", Object.assign(h("label", "", T("cx.threshold")), { htmlFor: "cx-pct" }), pct, help("cx-pct-h", T("cx.threshold.help"))),
        h("p", "setnote kn-help", T("cx.cost"))),
      h("div", "kn-actions", saveBtn, defaults), status);
  }

  async function load() {
    const r = await call("GET", "api/compaction");
    if (r.ok) { state = r.data; draw(); } else root.replaceChildren(h("p", "setnote warnnote", why(r)));
  }
  document.addEventListener("tadamoo:view", e => { if (e.detail === "settings") load(); });
  if (document.body.dataset.view === "settings") load();
})();
