"use strict";
/* Reverse engineering with REA, in the Agent tab. One switch registers REA's MCP server and skill with Basalt; the list below it
   shows the programs REA can use and installs one only when its button is pressed, with the command shown first.
   Everything the server sends goes in as text. See docs/rea.md. */
(function () {
  const box = $("#rea-box");
  if (!box) return;
  const on = $("#rea-on"), msg = $("#rea-msg"), body = $("#rea-body"), state = $("#rea-state");
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  let S = null, busy = false;
  const outputs = {};                       // tool id -> the last install result, kept across repaints

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

  function toolRow(t) {
    const li = el("li", "rea-tool");
    const top = el("div", "rea-top");
    top.append(el("b", "rea-name", T("rea.t." + t.id)), el("span", "rea-chip" + (t.detected ? " ok" : ""), t.detected ? T("rea.found") : T("rea.missing")));
    li.append(top, el("p", "rea-hint", T("rea.tw." + t.id)));
    if (t.detected) {
      li.append(el("p", "rea-mono", t.path + (t.version ? "  (" + t.version + ")" : "")));
    } else if (t.command.length) {
      li.append(el("p", "rea-hint", T("rea.cmd")), el("code", "rea-cmd", t.command.join(" ")));
      li.append(btn(busy ? T("rea.installing") : T("rea.install"), () => install({ tool: t.id }), busy));
    } else {
      const p = el("p", "rea-hint", T("rea.manual", t.link));
      li.append(p);
    }
    if (t.needs.length && !t.detected) li.append(el("p", "rea-hint", T("rea.needs", t.needs.map(n => T("rea.t." + n)).join(", "))));
    const out = outputs[t.id];
    if (out) {
      li.append(el("p", "rea-hint", out.code === 0 ? T("rea.done") : T("rea.failed", out.code)));
      if (out.output) { const pre = el("pre", "rea-out", out.output); pre.tabIndex = 0; li.append(pre); }
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
    const missing = S.tools.filter(t => !t.detected && t.command.length);
    const bar = el("div", "rea-bar");
    bar.append(btn(T("rea.all", missing.length), () => install({ all: true }), busy || !missing.length));
    bar.append(btn(T("rea.refresh"), () => load(), busy));
    const list = el("ul", "rea-list");
    S.tools.forEach(t => list.append(toolRow(t)));
    body.append(bar, el("p", "rea-hint", T("rea.uac")), list);
  }

  async function load() {
    try { S = await call("/api/rea"); say(""); paint(); }
    catch (e) { say(e.message, true); }
  }

  async function install(req) {
    busy = true; paint(); say(T("rea.installing"));
    try {
      const j = await call("/api/rea/install", req);
      j.results.forEach(r => { outputs[r.tool] = r; });
      S = j.status; say(T("rea.installed"));
    } catch (e) { say(e.message, true); }
    busy = false; paint();
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

  document.addEventListener("basalt:view", e => { if (e.detail === "agent") load(); });
  document.addEventListener("basalt:lang", () => paint());
  if (document.body.dataset.view === "agent") load();
})();
