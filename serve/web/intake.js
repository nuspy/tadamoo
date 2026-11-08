"use strict";
/* Settings > Inference > Questions and suggestions: whether the next-message suggestions are offered, and up to how long a conversation the
   extra model calls are made. The look at a request is the Prompt analysis switch (promptswitch.js), which this page keeps in step. Saved on
   this PC; a conversation started afterwards uses them. Everything the server says is put in the page as text. */
(() => {
  const root = document.getElementById("iq-root");
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
  const why = r => r.status === 0 ? T("kn.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`);

  async function save(values) {
    const r = await call("POST", "api/intake", values);
    if (r.ok) { state = r.data; note = T("kn.saved"); noteBad = false; if (window.PromptSwitch) window.PromptSwitch.sync(state.intake); if (window.OptimizePrompt) window.OptimizePrompt.sync(state); } else { note = why(r); noteBad = true; }
    draw();
  }

  function draw() {
    if (!state) return;
    const s = state, L = s.limits.side_call_tokens;
    const chips = h("input"); chips.type = "checkbox"; chips.id = "iq-chips"; chips.checked = s.suggestions; chips.setAttribute("aria-describedby", "iq-chips-h");
    const retry = h("input"); retry.type = "checkbox"; retry.id = "iq-retry"; retry.checked = s.empty_retry; retry.setAttribute("aria-describedby", "iq-retry-h");
    const size = h("input"); size.type = "number"; size.id = "iq-size"; size.min = String(L[0]); size.max = String(L[1]); size.step = "500"; size.inputMode = "numeric";
    size.value = String(s.side_call_tokens); size.setAttribute("aria-describedby", "iq-size-h");
    const label = (id, text) => Object.assign(h("label", "", text), { htmlFor: id });
    const values = () => ({ suggestions: chips.checked, empty_retry: retry.checked, side_call_tokens: Number(size.value) });
    const saveBtn = h("button", "btn primary", T("kn.save")); saveBtn.type = "button"; saveBtn.onclick = () => save(values());
    const defaults = h("button", "btn ghost", T("iq.defaults")); defaults.type = "button";
    defaults.title = T("iq.defaults.tip");
    defaults.onclick = () => save({ intake: s.defaults.intake, suggestions: s.defaults.suggestions, optimize_prompt: s.defaults.optimize_prompt, empty_retry: s.defaults.empty_retry, side_call_tokens: s.defaults.side_call_tokens });
    const status = h("p", "setnote kn-status", note); status.setAttribute("role", "status"); status.hidden = !note; status.dataset.bad = noteBad ? "1" : "";
    const help = (id, text) => Object.assign(h("p", "setnote kn-help", text), { id });
    const tokens = window.OptimizePrompt ? window.OptimizePrompt.sideTokens() : 0;
    const sideNote = h("p", "setnote kn-help", tokens > 0 ? T("iq.side.on", tokens.toLocaleString(LANG)) : T("iq.side.off"));
    sideNote.id = "iq-side"; sideNote.setAttribute("role", "status");
    root.replaceChildren(
      h("fieldset", "csgroup", h("legend", "", T("iq.side.title")), sideNote),
      h("fieldset", "csgroup", h("legend", "", T("iq.sugg.title")),
        h("label", "kn-check", chips, h("span", "", T("iq.sugg.on"))), help("iq-chips-h", T("iq.sugg.help"))),
      h("fieldset", "csgroup", h("legend", "", T("iq.retry.title")),
        h("label", "kn-check", retry, h("span", "", T("iq.retry.on"))), help("iq-retry-h", T("iq.retry.help"))),
      h("fieldset", "csgroup", h("legend", "", T("iq.size.title")),
        h("div", "kn-field", label("iq-size", T("iq.size")), size, help("iq-size-h", T("iq.size.help")))),
      h("div", "kn-actions", saveBtn, defaults), status);
  }

  async function load() {
    const r = await call("GET", "api/intake");
    if (r.ok) { state = r.data; if (window.PromptSwitch) window.PromptSwitch.sync(state.intake); if (window.OptimizePrompt) window.OptimizePrompt.sync(state); draw(); } else root.replaceChildren(h("p", "setnote warnnote", why(r)));
  }
  document.addEventListener("basalt:side", () => { if (state) draw(); });
  document.addEventListener("tadamoo:view", e => { if (e.detail === "settings") load(); });
  if (document.body.dataset.view === "settings") load();
})();
