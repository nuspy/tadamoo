"use strict";
/* Reverse engineering with REA, in the Agent tab. One switch registers REA's MCP server and skill with TaDaMoo; the list below it
   shows the programs REA can use. A press of Install, or of Install all, runs the steps shown, in the order of what each program needs,
   in the background; this page follows it. Everything the server sends goes in as text. See docs/rea.md. */
(function () {
  const box = $("#rea-box");
  if (!box) return;
  const on = $("#rea-on"), msg = $("#rea-msg"), body = $("#rea-body"), state = $("#rea-state");
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  let S = null, busy = false, timer = 0;
  const running = () => !!(S && S.install && S.install.running);
  const results = () => { const out = {}; ((S && S.install && S.install.results) || []).forEach(r => { out[r.tool] = r; }); return out; };
  const name = id => T("rea.t." + id);

  async function call(path, body) {
    const opts = body === undefined ? { cache: "no-store", headers: authHeaders() }
      : { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(body) };
    const r = await fetch(path, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((j.error && j.error.message) || `HTTP ${r.status}`);
    return j;
  }

  function say(text, bad) { msg.textContent = text || ""; msg.dataset.kind = bad ? "bad" : ""; }
  const yesno = (v, k) => T(v ? k + ".on" : k + ".off");

  function btn(text, fn, disabled) {
    const b = el("button", "btn", text);
    b.type = "button";
    b.disabled = !!disabled;
    b.addEventListener("click", fn);
    return b;
  }

  // what happened to a program in the last installation
  function outcome(r) {
    if (r.skipped === "present") return null;
    if (r.skipped === "platform") return T("rea.skip.platform");
    if (r.skipped === "needs") return T("rea.skip.needs", r.output.split(", ").map(name).join(", "));
    if (r.skipped === "manual") return T("rea.skip.manual");
    return r.ok ? T("rea.ok", r.method, r.attempts) : T("rea.failed", r.code, r.attempts);
  }

  function toolRow(t, res) {
    const li = el("li", "rea-tool");
    const top = el("div", "rea-top");
    const chip = t.detected ? ["rea-chip ok", T("rea.found")] : !t.supported ? ["rea-chip", T("rea.nosystem")] : ["rea-chip", T("rea.missing")];
    top.append(el("b", "rea-name", name(t.id)), el("span", chip[0], chip[1]));
    li.append(top, el("p", "rea-hint", T("rea.tw." + t.id)));
    if (t.detected) {
      li.append(el("p", "rea-mono", t.path + (t.version ? "  (" + t.version + ")" : "")));
    } else if (!t.supported) {
      li.append(el("p", "rea-hint", T("rea.only", t.only.join(", "))));
    } else if (t.installable) {
      li.append(el("p", "rea-hint", T("rea.steps")));
      t.steps.forEach(s => li.append(el("code", "rea-cmd", s)));
      li.append(btn(running() ? T("rea.installing") : T("rea.install"), () => install({ tool: t.id }), busy || running()));
    } else {
      li.append(el("p", "rea-hint", T("rea.manual", t.link)));
    }
    if (t.needs.length && !t.detected && t.supported) li.append(el("p", "rea-hint", T("rea.needs", t.needs.map(name).join(", "))));
    if (res) {
      const line = outcome(res);
      if (line) li.append(el("p", "rea-hint", line));
      if (res.output && res.skipped !== "needs") { const pre = el("pre", "rea-out", res.output); pre.tabIndex = 0; li.append(pre); }
    } else if (running() && S.install.plan.includes(t.id) && !t.detected) {
      li.append(el("p", "rea-hint", S.install.current === t.id ? T("rea.now") : T("rea.queued")));
    }
    return li;
  }

  function paint() {
    if (!S) return;
    on.checked = S.enabled;
    on.disabled = busy || !S.addon.present;
    state.textContent = "";
    if (!S.addon.present) { state.append(el("p", "rea-warn", T("rea.noaddon"))); }
    else {
      state.append(el("p", "rea-hint", T("rea.addon", S.addon.version, S.addon.node_version || "?")));
      state.append(el("p", "rea-hint", T("rea.mcp", yesno(S.mcp.registered, "rea.mcpstate")) + " " + T("rea.skills", yesno(S.skills.imported, "rea.skillstate"))));
    }
    body.textContent = "";
    body.append(el("p", "rea-hint", T("rea.risk")));
    body.append(el("h3", "rea-h", T("rea.tools.h")), el("p", "rea-hint", T("rea.tools.hint")));
    const missing = S.tools.filter(t => !t.detected && t.installable);
    const bar = el("div", "rea-bar");
    bar.append(btn(T("rea.all", missing.length), () => install({ all: true }), busy || running() || !missing.length));
    bar.append(btn(T("rea.refresh"), () => load(), busy || running()));
    body.append(bar);
    if (running()) {
      const done = S.install.results.length, total = S.install.plan.length;
      const p = el("p", "rea-hint", T("rea.progress", Math.min(done + 1, total), total, S.install.current ? name(S.install.current) : ""));
      p.setAttribute("role", "status");
      body.append(p);
    }
    body.append(el("p", "rea-hint", T("rea.uac")));
    const res = results();
    const list = el("ul", "rea-list");
    S.tools.forEach(t => list.append(toolRow(t, res[t.id])));
    body.append(list);
  }

  // while an installation runs the page asks again every two seconds
  function follow() {
    clearTimeout(timer);
    if (running()) timer = setTimeout(load, 2000);
  }

  async function load() {
    try {
      const before = running();
      S = await call("/api/rea");
      say(before && !running() ? T("rea.installed") : (running() ? T("rea.installing") : ""));
      paint();
      follow();
    } catch (e) { say(e.message, true); follow(); }
  }

  async function install(req) {
    busy = true; paint();
    try {
      const j = await call("/api/rea/install", req);
      S = j.status; say(T("rea.installing"));
    } catch (e) { say(e.message, true); }
    busy = false; paint(); follow();
  }

  on.addEventListener("change", async () => {
    const want = on.checked;
    busy = true; paint();
    try {
      S = await call(want ? "/api/rea/enable" : "/api/rea/disable", {});
      say(T(want ? "rea.enabled" : "rea.disabled"));
    } catch (e) { on.checked = !want; say(e.message, true); }
    busy = false; paint();
  });

  document.addEventListener("tadamoo:view", e => { if (e.detail === "agent") load(); });
  document.addEventListener("tadamoo:lang", () => paint());
  if (document.body.dataset.view === "agent") load();
})();
