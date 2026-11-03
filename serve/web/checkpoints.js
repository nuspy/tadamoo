"use strict";
/* Checkpoints in the chat settings. The setting (GET/POST api/agent/checkpoint-mode) has four positions: follow the
   permission level, on, on but never stop the action, off. The list shows the checkpoints of the folder the open
   conversation works in (GET api/agent/sessions/<id>/checkpoints): when, the action, the files kept, the size, and
   the later actions a restore cannot undo. A restore and a delete ask twice: the first press changes the label, the
   second acts. */
(function () {
  const CONFIRM_MS = 5000;
  const MODES = ["", "on", "soft", "off"];
  const list = $("#cs-cp-list");
  const state = $("#cs-cp-state");
  const radios = [...document.querySelectorAll('input[name="cs-cp"]')];
  let mode = "";
  let busy = false;

  const say = (text, kind) => { state.textContent = text; state.dataset.kind = kind || ""; };
  const sid = () => (window.agentChat && window.agentChat.current && window.agentChat.current()) || "";
  const size = b => {
    const k = new Intl.NumberFormat(LANG === "it" ? "it-IT" : "en-US", { maximumFractionDigits: 1 });
    b = Number.isFinite(b) ? b : 0;
    return b < 1024 ? `${b} B` : b < 1048576 ? `${k.format(b / 1024)} KB` : b < 1073741824 ? `${k.format(b / 1048576)} MB` : `${k.format(b / 1073741824)} GB`;
  };
  const when = t => new Date(t * 1000).toLocaleString(LANG === "it" ? "it-IT" : "en-US");
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

  async function call(path, body) {
    const opts = body === undefined ? { cache: "no-store", headers: authHeaders() }
      : { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(body) };
    const r = await fetch(path, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error((j.error && j.error.message) || `HTTP ${r.status}`); e.code = j.error && j.error.code; throw e; }
    return j;
  }

  function paint(m) {
    mode = MODES.includes(m) ? m : "";
    radios.forEach(r => { r.checked = r.value === mode; });
  }

  /* A button that asks twice: the first press writes the question in its label. */
  function twice(label, aria, act) {
    const b = el("button", "btn", label);
    b.type = "button";
    b.setAttribute("aria-label", aria);
    let armed = null;
    b.addEventListener("click", () => {
      if (armed) { clearTimeout(armed); armed = null; b.textContent = label; b.classList.remove("armed"); act(); return; }
      b.textContent = T("ck.again");
      b.classList.add("armed");
      armed = setTimeout(() => { armed = null; b.textContent = label; b.classList.remove("armed"); }, CONFIRM_MS);
    });
    return b;
  }

  function row(cp) {
    const li = el("li", "ck-row");
    const head = el("div", "ck-head");
    head.append(el("b", "", cp.description || cp.id), el("span", "ck-meta", when(cp.time)));
    li.append(head);
    li.append(el("div", "ck-meta", T("ck.files", cp.files, size(cp.bytes)) + (cp.legacy ? " " + T("ck.legacy") : "")));
    if (cp.paths && cp.paths.length) {
      const d = el("details");
      d.append(el("summary", "", T("ck.show")));
      const ul = el("ul", "ck-files");
      cp.paths.forEach(p => ul.append(el("li", "", p.path + (p.existed ? "" : " " + T("ck.new")))));
      d.append(ul);
      li.append(d);
    }
    if (cp.not_undone && cp.not_undone.length) li.append(el("p", "ck-warn", T("ck.irreversible", cp.not_undone.map(a => a.what || a.tool).join("; "))));
    const act = el("div", "ck-act");
    act.append(
      twice(T("ck.restore"), T("ck.restore.aria", cp.description || cp.id), () => run(`restore`, { checkpoint: cp.id, accept_irreversible: true }, T("ck.restored"))),
      twice(T("ck.delete"), T("ck.delete.aria", cp.description || cp.id), () => run(`delete`, { checkpoint: cp.id }, T("ck.deleted"))));
    li.append(act);
    return li;
  }

  async function load() {
    list.replaceChildren();
    const id = sid();
    try {
      if (!id) { paint((await call("api/agent/checkpoint-mode")).mode); say(T("ck.nochat")); return; }
      const j = await call(`api/agent/sessions/${encodeURIComponent(id)}/checkpoints`);
      paint(j.mode);
      j.checkpoints.slice().reverse().forEach(cp => list.append(row(cp)));
      say(j.checkpoints.length ? T("ck.total", j.checkpoints.length, size(j.bytes)) : T("ck.none"));
    } catch (e) { say(T("ck.failed", e.message), "error"); }
  }

  async function run(what, body, done) {
    const id = sid();
    if (!id || busy) return;
    busy = true;
    try {
      await call(`api/agent/sessions/${encodeURIComponent(id)}/checkpoints/${what}`, body);
      await load();
      say(done);
    } catch (e) { say(T("ck.failed", e.message), "error"); }
    busy = false;
  }

  async function setMode(value) {
    const want = value === "default" ? "" : value;
    if (!MODES.includes(want)) return false;
    try { paint((await call("api/agent/checkpoint-mode", { mode: want })).mode); say(T("ck.saved")); return true; }
    catch (e) { say(T("ck.failed", e.message), "error"); return false; }
  }

  radios.forEach(r => r.addEventListener("change", () => { if (r.checked) setMode(r.value || "default"); }));
  $("#cs-cp-refresh").addEventListener("click", load);
  const all = twice(T("ck.delete.all"), T("ck.delete.all"), () => run("delete", { all: true }, T("ck.deleted")));
  all.id = "cs-cp-all";
  $("#cs-cp-all").replaceWith(all);
  $("#cs-open").addEventListener("click", load);
  document.addEventListener("tadamoo:open-session", () => setTimeout(() => { if (!$("#chatset").hidden) load(); }, 300));
  applyT($("#cs-cp"));
  window.checkpointsUi = { mode: () => mode, setMode, load, label: m => T("ck.m." + (m || "default")) };
})();
