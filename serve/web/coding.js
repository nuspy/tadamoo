"use strict";
/* The Agent tab's role selector and the card of project checks. The roles come from the server; the card lists the checks a folder's files
   suggest and what the person accepted. Nothing here runs a command: accepting is a choice saved on the server, and everything the server
   or a project file says enters the page as text. */
(() => {
  const sel = document.getElementById("coderole"), roleLabel = document.getElementById("coderole-label");
  const card = document.getElementById("coding-card"), sum = document.getElementById("coding-sum"), toggle = document.getElementById("coding-toggle");
  const body = document.getElementById("coding-body"), go = document.getElementById("coding-go"), bug = document.getElementById("coding-bug");
  if (!sel || !card) return;
  const ROLE_KEY = "tadamoo.agent.role";
  const store = {
    get(k) { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked: the choice lasts until the page closes */ } },
  };
  let roles = [], codingRole = "", view = null, viewFolder = "", open = false, needDraw = false, note = "", noteBad = false, chosen = "", chosenFor = null, bugText = "";

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
  const why = r => (r.status === 0 ? T("cd.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`));
  const chat = () => window.agentChat || null;
  const inAgent = () => document.body.dataset.view === "agent" && !!chat() && chat().active();
  const record = () => (chat() && chat().record ? chat().record() : null);
  const folder = () => (record() && record().workspace) || (chat() && chat().folder ? chat().folder() : "");

  /* ---- the role ---- */
  const sidNow = () => (chat() ? chat().current() : "");
  const roleNow = () => {
    if (chosen && chosenFor === sidNow() && roles.includes(chosen)) return chosen;        // what the person just picked, until the conversation says it too
    const r = record();
    if (r && roles.includes(r.role)) return r.role;
    return roles.includes(store.get(ROLE_KEY)) ? store.get(ROLE_KEY) : codingRole;
  };
  function paintRoles() {
    if (sel.children.length !== roles.length) {                       // built once: a rebuilt list would close the menu while it is open
      sel.replaceChildren(...roles.map(id => {
        const o = h("option", "", T("cd.role." + id));
        o.value = id; o.title = T("cd.role." + id + ".d");
        return o;
      }));
    }
    if (document.activeElement !== sel) sel.value = roleNow();
    sel.title = T("cd.role." + sel.value + ".d");
  }
  async function setRole(id, quiet) {
    if (!roles.includes(id)) return false;
    chosen = id; chosenFor = sidNow(); store.set(ROLE_KEY, id);
    const sid = chosenFor;
    if (sid) {
      const r = await call("POST", `api/agent/sessions/${sid}/role`, { role: id });
      if (!r.ok) { if (!quiet) note = why(r), noteBad = true; refresh(); return false; }
      if (chat().refresh) chat().refresh();
    }
    refresh();
    return true;
  }
  sel.onchange = () => { setRole(sel.value); };

  /* ---- the card ---- */
  async function load(force) {
    const f = folder();
    if (!f) { view = null; viewFolder = ""; return; }
    if (!force && view && viewFolder === f) return;
    const r = await call("GET", "api/coding?workspace=" + encodeURIComponent(f));
    viewFolder = f;                                                    // a failed look is not repeated until the card is opened again
    needDraw = true;
    if (r.ok) view = r.data; else { view = null; note = why(r); noteBad = true; }
  }
  const accepted = c => c.state === "accepted";
  function summary() {
    if (!view) return "";
    const real = view.checks.filter(c => c.command);
    if (!real.length) return T("cd.card.sum.none");
    return T("cd.card.sum", real.filter(accepted).length, real.length);
  }
  function row(c, n) {
    if (!c.command) return h("div", "cc-row", h("div", "cc-main", h("span", "cc-kind", T("cd.kind." + c.kind)), h("span", "", T("cd.cmake"))), h("div", "cc-meta", T("cd.from", c.source)));
    const on = h("input"); on.type = "checkbox"; on.id = "cc-on-" + n; on.checked = accepted(c); on.dataset.command = c.command;
    const gate = h("input"); gate.type = "checkbox"; gate.id = "cc-gate-" + n; gate.checked = c.gate; gate.disabled = !on.checked;
    on.onchange = () => { gate.disabled = !on.checked; };
    const state = h("span", "cc-state", c.state === "accepted" ? T("cd.state.accepted") : c.state === "changed" ? T("cd.state.changed") : T("cd.state.none"));
    state.dataset.state = c.state;
    return h("div", "cc-row", h("div", "cc-main", h("span", "cc-kind", T("cd.kind." + c.kind)), h("code", "cc-cmd", c.command), state),
      h("div", "cc-meta", T("cd.from", c.source), c.runs ? " - " + T("cd.runs", c.runs) : ""),
      h("div", "cc-checks",
        h("label", "cc-check", on, h("span", "", T("cd.accept"))),
        h("label", "cc-check", gate, h("span", "", T("cd.gate")))));
  }
  function drawBody() {
    if (!view) { body.replaceChildren(h("p", "cc-dim", note || T("cd.card.none"))); return; }
    const rows = view.checks.map(row);
    const own = h("textarea"); own.id = "cc-own"; own.rows = 3; own.value = view.own.join("\n"); own.spellcheck = false; own.autocomplete = "off"; own.placeholder = "python -m pytest tests/test_one.py -q";
    const ownLabel = h("label", "", T("cd.own")); ownLabel.htmlFor = "cc-own";
    const save = h("button", "btn primary", T("cd.save")); save.type = "button";
    save.onclick = async () => {
      const list = [...body.querySelectorAll("input[data-command]")].filter(i => i.checked).map(i => ({ command: i.dataset.command, gate: body.querySelector("#" + i.id.replace("cc-on-", "cc-gate-")).checked }));
      const r = await call("POST", "api/coding", { workspace: viewFolder, accepted: list, own: own.value });
      if (r.ok) { view = r.data; note = T("cd.saved", r.data.told || 0); noteBad = false; } else { note = why(r); noteBad = true; }
      needDraw = true;
      refresh();
    };
    const status = h("p", "cc-status", note); status.setAttribute("role", "status"); status.dataset.bad = noteBad ? "1" : ""; status.hidden = !note;
    const off = view.verify_gate === "off" ? T("cd.gate.off") : T("cd.gate.on", T("cd.gate.mode." + view.verify_gate));
    const empty = view.empty ? h("div", "cc-empty", h("span", "", T("cd.empty")), (() => { const b = h("button", "btn", T("cd.empty.btn")); b.type = "button"; b.onclick = () => setRole("plan"); return b; })()) : null;
    body.replaceChildren(...[h("p", "cc-dim", T("cd.card.help")), empty, ...(rows.length ? rows : [h("p", "cc-dim", T("cd.card.sum.none"))]),
      h("div", "cc-own", ownLabel, own, h("p", "cc-dim", T("cd.own.help", view.limits.own))),
      h("p", "cc-dim", T("cd.readonly", view.read_only.join(", "))), h("p", "cc-dim", off), h("div", "cc-actions", save), status].filter(Boolean));
  }
  toggle.onclick = async () => {
    open = !open;
    if (open) await load(true);
    needDraw = true;
    refresh();
  };

  /* ---- what the page shows now ---- */
  function refresh() {
    const show = inAgent() && roles.length > 0;
    roleLabel.hidden = !show;
    if (show) paintRoles();
    const f = folder();
    card.hidden = !(show && f);
    if (card.hidden) return;
    if (!view || viewFolder !== f) { load(false).then(refresh); }
    sum.textContent = summary();
    toggle.textContent = T(open ? "cd.card.close" : "cd.card.open");
    toggle.setAttribute("aria-expanded", String(open));
    body.hidden = !open;
    if (open && needDraw) { needDraw = false; drawBody(); }                // the list is redrawn when it changes, not on every poll: a box being ticked or typed in stays
    const busy = chat() && chat().busy ? chat().busy() : false;
    go.hidden = !(roleNow() === "plan" && chat().current() && !busy);
    go.textContent = T("cd.go", T("cd.role." + codingRole));
    bug.hidden = !(bugText && roleNow() === codingRole && !busy);
    bug.textContent = T("cd.bug", T("cd.role.debug"));
    document.getElementById("coding-next").hidden = go.hidden && bug.hidden;
  }
  go.onclick = async () => { if (await setRole(codingRole, true)) chat().send(T("cd.go.text")); };
  bug.onclick = async () => { bugText = ""; await setRole("debug"); };
  const FAULT = /\b(error|errors|exception|traceback|stack ?trace|bug|crash(es|ed)?|fails?|failing|failed|broken|doesn'?t work|not working|regression)\b|\b(errore|eccezione|non funziona|si blocca|rotto|fallisce|fallito|guasto)\b/i;
  document.addEventListener("basalt:sent", e => {
    const text = e.detail && e.detail.text;
    bugText = (typeof text === "string" && roleNow() === codingRole && FAULT.test(text)) ? text : "";
    refresh();
  });

  window.codingUi = { refresh, role: roleNow, defaultRole: () => codingRole };
  document.addEventListener("basalt:view", () => { view = null; viewFolder = ""; refresh(); });

  (async () => {
    const r = await call("GET", "api/coding");
    if (!r.ok || !Array.isArray(r.data.roles)) return;
    roles = r.data.roles; codingRole = r.data.coding_role;
    applyT(document.getElementById("coding-card"));
    refresh();
  })();
})();
