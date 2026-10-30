"use strict";
/* Settings > General > Checks before an answer ends: the project's own check commands and when they run, the budget's last report, and the
   proposal about model errors that waiting cannot cure. Everything the server says is put in the page as text. */
(() => {
  const root = document.getElementById("gt-root");
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
  const why = r => r.status === 0 ? T("gt.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`);

  function field(id, label, help, input) {
    input.id = id;
    const l = h("label", "", label);
    l.htmlFor = id;
    return h("div", "kn-field", l, input, h("p", "setnote kn-help", help));
  }
  function check(id, label, help, on) {
    const box = h("input"); box.type = "checkbox"; box.id = id; box.checked = on;
    const l = h("label", "kn-check", box, h("span", "", label));
    return { box, row: h("div", "kn-field", l, h("p", "setnote kn-help", help)) };
  }

  async function save(values) {
    const r = await call("POST", "api/gate", values);
    if (r.ok) { state = r.data; note = T("gt.saved"); noteBad = false; } else { note = why(r); noteBad = true; }
    draw();
  }

  function draw() {
    if (!state) return;
    const s = state;
    const commands = h("textarea"); commands.rows = 4; commands.value = s.verify_commands.join("\n"); commands.spellcheck = false; commands.autocomplete = "off";
    commands.placeholder = "python -m pytest -x -q";
    const mode = h("select", "", s.modes.map(k => Object.assign(h("option", "", T("gt.mode." + k)), { value: k })));
    mode.value = s.verify_gate;
    const rounds = h("input"); rounds.type = "number"; rounds.min = String(s.limits.verify_rounds[0]); rounds.max = String(s.limits.verify_rounds[1]); rounds.step = "1";
    rounds.inputMode = "numeric"; rounds.value = String(s.verify_rounds);
    const grace = check("gt-grace", T("gt.grace"), T("gt.grace.help"), s.budget_grace);
    const classifier = check("gt-class", T("gt.class"), T("gt.class.help"), s.error_classifier);

    const values = () => ({ verify_commands: commands.value, verify_gate: mode.value, verify_rounds: Number(rounds.value),
      budget_grace: grace.box.checked, error_classifier: classifier.box.checked });
    const saveBtn = h("button", "btn primary", T("gt.save")); saveBtn.type = "button"; saveBtn.onclick = () => save(values());
    const defaults = h("button", "btn ghost", T("gt.defaults")); defaults.type = "button";
    defaults.onclick = () => save({ verify_gate: s.defaults.verify_gate, verify_rounds: s.defaults.verify_rounds, budget_grace: s.defaults.budget_grace, error_classifier: s.defaults.error_classifier });
    const status = h("p", "setnote kn-status", note); status.setAttribute("role", "status"); status.hidden = !note; status.dataset.bad = noteBad ? "1" : "";
    const idle = s.verify_gate !== "off" && !s.verify_commands.length ? h("p", "setnote warnnote", T("gt.nocommands")) : null;

    root.replaceChildren(
      h("fieldset", "csgroup", h("legend", "", T("gt.verify.title")), h("p", "setnote kn-help", T("gt.verify.help")),
        field("gt-commands", T("gt.commands"), T("gt.commands.help", s.limits.commands), commands),
        field("gt-mode", T("gt.mode"), T("gt.mode.help"), mode),
        field("gt-rounds", T("gt.rounds"), T("gt.rounds.help"), rounds), idle),
      h("fieldset", "csgroup", h("legend", "", T("gt.end.title")), grace.row, classifier.row),
      h("div", "kn-actions", saveBtn, defaults), status);
  }

  async function load() {
    const r = await call("GET", "api/gate");
    if (r.ok) { state = r.data; draw(); } else root.replaceChildren(h("p", "setnote warnnote", why(r)));
  }
  document.addEventListener("basalt:view", e => { if (e.detail === "settings") load(); });
  if (document.body.dataset.view === "settings") load();
})();
