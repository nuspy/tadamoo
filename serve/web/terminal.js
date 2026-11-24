"use strict";
/* Settings > General > Command line: the prefix that sends a line of the PowerShell module to the harness, the colour of the agent's answer, the server it
   talks to, its key (written, never shown) and the level it may ask for. Everything the server says is put in the page as text. */
(() => {
  const root = document.getElementById("tm-root");
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
  const why = r => r.status === 0 ? T("tm.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`);

  function field(id, label, help, input) {
    input.id = id;
    const l = h("label", "", label);
    l.htmlFor = id;
    return h("div", "kn-field", l, input, h("p", "setnote kn-help", help));
  }

  async function save(values) {
    const r = await call("POST", "api/terminal", values);
    if (r.ok) { state = r.data; note = T("tm.saved"); noteBad = false; } else { note = why(r); noteBad = true; }
    draw();
  }

  function draw() {
    if (!state) return;
    const s = state;
    const prefix = h("input"); prefix.type = "text"; prefix.maxLength = 3; prefix.value = s.prefix; prefix.autocomplete = "off"; prefix.spellcheck = false;
    const color = h("input"); color.type = "color"; color.value = s.color.toLowerCase();
    const sample = h("p", "tm-sample", "│ " + T("tm.sample")); sample.style.color = color.value;
    color.oninput = () => { sample.style.color = color.value; };
    const server = h("input"); server.type = "url"; server.value = s.server; server.autocomplete = "off"; server.spellcheck = false;
    const key = h("input"); key.type = "password"; key.autocomplete = "off"; key.placeholder = s.has_key ? T("tm.key.set") : "";
    const mode = h("select", "", ["", ...s.modes].map(k => Object.assign(h("option", "", T("tm.mode." + (k || "inherit"))), { value: k })));
    mode.value = s.mode;
    const values = () => ({ prefix: prefix.value, color: color.value, server: server.value, mode: mode.value, ...(key.value ? { key: key.value } : {}) });
    const saveBtn = h("button", "btn primary", T("tm.save")); saveBtn.type = "button"; saveBtn.onclick = () => save(values());
    const clearKey = h("button", "btn ghost", T("tm.key.clear")); clearKey.type = "button"; clearKey.hidden = !s.has_key; clearKey.onclick = () => save({ key: "" });
    const defaults = h("button", "btn ghost", T("tm.defaults")); defaults.type = "button";
    defaults.onclick = () => save({ prefix: s.defaults.prefix, color: s.defaults.color, server: s.defaults.server, mode: s.defaults.mode });
    const status = h("p", "setnote kn-status", note); status.setAttribute("role", "status"); status.hidden = !note; status.dataset.bad = noteBad ? "1" : "";
    root.replaceChildren(
      h("p", "setnote kn-help", T("tm.help")),
      field("tm-prefix", T("tm.prefix"), T("tm.prefix.help"), prefix),
      field("tm-color", T("tm.color"), T("tm.color.help"), color), sample,
      field("tm-server", T("tm.server"), T("tm.server.help"), server),
      field("tm-key", T("tm.key"), T("tm.key.help"), key), clearKey,
      field("tm-mode", T("tm.mode"), T("tm.mode.help"), mode),
      h("p", "setnote kn-help", T("tm.file", s.path)),
      h("div", "kn-actions", saveBtn, defaults), status);
  }

  async function load() {
    const r = await call("GET", "api/terminal");
    if (r.ok) { state = r.data; draw(); } else root.replaceChildren(h("p", "setnote warnnote", why(r)));
  }
  document.addEventListener("tadamoo:view", e => { if (e.detail === "settings") load(); });
  if (document.body.dataset.view === "settings") load();
})();
