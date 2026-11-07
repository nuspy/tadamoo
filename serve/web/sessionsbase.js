"use strict";
/* Settings > General > Where the conversations are kept: the folder that holds every conversation's files (one folder each), checked and moved
   from here. Everything the server says is put in the page as text. */
(() => {
  const root = document.getElementById("sb-root");
  if (!root) return;
  let info = null, input = null, checked = null, note = "", noteBad = false, busy = false;

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
  const CODES = ["relative", "same", "nested", "not_empty", "active", "verify", "io"];
  const why = r => {
    if (r.status === 0) return T("sb.e.offline");
    const e = r.data && r.data.error;
    return e && CODES.includes(e.code) ? T("sb.e." + e.code) : ((e && e.message) || `HTTP ${r.status}`);
  };
  const size = n => n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB";

  async function load() {
    const r = await call("GET", "api/sessions-base");
    if (r.ok) info = r.data; else { note = why(r); noteBad = true; }
    draw();
  }
  async function check() {
    const path = input.value.trim();
    if (!path || busy) return;
    busy = true; checked = null; note = ""; draw();
    const r = await call("POST", "api/sessions-base/check", { path });
    busy = false;
    if (r.ok) { checked = { ...r.data, path }; if (!r.data.ok) { note = (r.data.problems || []).map(p => CODES.includes(p.code) ? T("sb.e." + p.code) : p.message).join(" "); noteBad = true; } }
    else { note = why(r); noteBad = true; }
    draw();
  }
  async function move() {
    if (!checked || !checked.ok || busy) return;
    busy = true; note = ""; draw();
    const r = await call("POST", "api/sessions-base/move", { path: checked.path });
    busy = false;
    if (r.ok) { note = T("sb.done", r.data.files, r.data.to); noteBad = false; checked = null; await load(); return; }
    note = why(r); noteBad = true; draw();
  }

  function draw() {
    root.textContent = "";
    if (!info) { root.append(h("p", "setnote", note || T("sb.loading"))); return; }
    input = h("input"); input.type = "text"; input.id = "sb-path"; input.autocomplete = "off"; input.spellcheck = false;
    input.setAttribute("aria-describedby", "sb-help sb-msg");
    input.value = (checked && checked.path) || "";
    input.addEventListener("input", () => { checked = null; });
    const label = h("label", "", T("sb.new")); label.htmlFor = "sb-path";
    const test = h("button", "btn", T("sb.check")); test.type = "button"; test.disabled = busy; test.addEventListener("click", check);
    const go = h("button", "btn primary", T("sb.move")); go.type = "button"; go.disabled = busy || !(checked && checked.ok); go.addEventListener("click", move);
    const help = h("p", "setnote kn-help", T("sb.help")); help.id = "sb-help";
    const msg = h("p", "setstatus" + (noteBad ? " bad" : ""), note); msg.id = "sb-msg"; msg.setAttribute("role", "status"); msg.setAttribute("aria-live", "polite");
    root.append(
      h("p", "setnote", T(info.chosen ? "sb.chosen" : "sb.default"), " ", h("code", "", info.current)),
      info.running ? h("p", "setnote", T("sb.running", info.running)) : null,
      h("div", "kn-field", label, h("div", "pfrow wrap", input, test, go), help),
      checked && checked.ok ? h("p", "setnote", T("sb.ready", checked.files, size(checked.bytes)), checked.paths_inside ? " " + T("sb.paths", checked.paths_inside) : "") : null,
      msg);
  }
  document.addEventListener("tadamoo:lang", draw);
  load();
})();
