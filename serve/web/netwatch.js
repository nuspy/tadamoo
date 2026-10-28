"use strict";
/* The security monitor under Monitor > Security. It shows the switch, what is covered, the processes watched, the settings,
   the register of reports and the exceptions. The alert dialog is shown from here whichever page is open: the alerts route is
   asked every few seconds and one dialog is open at a time. Nothing is blocked; an exception only hides a report from the
   alerts. Deleting a report is one press: the server keeps the log verifiable (see docs/security-monitor.md). */
(function () {
  const root = $("#view-security");
  const ALERT_MS = 5000, VIEW_MS = 3000;
  const TAGS = ["alibaba_cn_domain", "proxy_env", "system_proxy", "local_proxy_listener", "doh_or_tor", "no_agent_call",
    "disk_outside_expected", "archive_or_staging", "persistence"];
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const num = new Intl.NumberFormat(LANG === "it" ? "it-IT" : "en-US", { maximumFractionDigits: 1 });
  const when = t => new Date(t * 1000).toLocaleString(LANG === "it" ? "it-IT" : "en-US");

  let S = null;                    // status
  let events = [], total = 0, excs = [], drafts = [];
  let filter = "all", sig = "", touched = false;
  const picked = new Set();
  const snoozed = new Set();
  let alertTimer = 0, viewTimer = 0, built = false, dlg = null, dlgBusy = false, alertsOff = false;
  const ui = {};

  async function call(path, body) {
    const opts = body === undefined ? { cache: "no-store", headers: authHeaders() }
      : { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(body) };
    const r = await fetch(path, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { const e = new Error((j.error && j.error.message) || `HTTP ${r.status}`); e.status = r.status; throw e; }
    return j;
  }

  function btn(text, cls, fn) {
    const b = el("button", "btn" + (cls ? " " + cls : ""), text);
    b.type = "button";
    if (fn) b.addEventListener("click", fn);
    return b;
  }

  function say(node, text, bad) { node.textContent = text || ""; node.dataset.kind = bad ? "bad" : ""; }

  /* a field with its label and its explanation; the explanation is tied to the control for screen readers */
  function field(id, labelKey, hintKey, control) {
    const wrap = el("div", "nw-field");
    const l = el("label", "nw-lab", T(labelKey));
    l.htmlFor = id;
    control.id = id;
    const h = el("p", "nw-hint", T(hintKey));
    h.id = id + "-h";
    control.setAttribute("aria-describedby", h.id);
    wrap.append(l, control, h);
    return wrap;
  }

  function checkRow(id, labelKey, hintKey, input) {
    const wrap = el("div", "nw-field");
    const l = el("label", "nw-check");
    input.id = id;
    l.append(input, el("span", null, T(labelKey)));
    const h = el("p", "nw-hint", T(hintKey));
    h.id = id + "-h";
    input.setAttribute("aria-describedby", h.id);
    wrap.append(l, h);
    return wrap;
  }

  function regexWarning(pattern) {
    try { new RegExp(pattern); return ""; } catch (e) { return String(e.message || e); }
  }

  function chip(text, kind) { return el("span", "nw-chip" + (kind ? " " + kind : ""), text); }
  function tagChips(tags) {
    const box = el("span", "nw-tags");
    (tags || []).forEach(t => { const c = chip(TAGS.includes(t) ? T("sec.tag." + t) : t, "tag"); c.title = TAGS.includes(t) ? T("sec.tagx." + t) : ""; box.append(c); });
    return box;
  }

  function target(ev) {
    if (ev.kind === "disk") return ev.host;
    if (ev.kind === "persistence") return ev.host;
    const port = ev.remote_port ? `:${ev.remote_port}` : "";
    return ev.host ? `${ev.host} (${ev.remote_ip}${port})` : `${ev.remote_ip}${port}`;
  }

  function describe(ev) {
    if (ev.kind === "disk") return T("sec.what.disk", ev.process, ev.host);
    if (ev.kind === "persistence") return T("sec.what.persist", ev.host);
    return T("sec.what.net", ev.process, ev.proto, target(ev));
  }

  /* ---- the page ---- */
  function build() {
    root.replaceChildren();
    const head = el("section", "nw-sec");
    head.append(el("h2", "nw-h", T("sec.title")), el("p", "nw-p", T("sec.intro")));
    const sw = el("input");
    sw.type = "checkbox";
    sw.setAttribute("role", "switch");
    sw.addEventListener("change", toggle);
    ui.sw = sw;
    head.append(checkRow("nw-on", "sec.on", "sec.on.hint", sw));
    ui.state = el("p", "nw-line");
    ui.state.setAttribute("role", "status");
    ui.cover = el("p", "nw-line");
    ui.warn = el("p", "nw-warn");
    head.append(ui.state, ui.cover, ui.warn);

    const watched = el("section", "nw-sec");
    watched.append(el("h3", "nw-h", T("sec.watched.h")), el("p", "nw-hint", T("sec.watched.hint")));
    ui.watched = el("ul", "nw-plain");
    watched.append(ui.watched);

    const set = el("section", "nw-sec");
    set.append(el("h3", "nw-h", T("sec.set.h")));
    ui.interval = Object.assign(el("input", "nw-in"), { type: "number", min: "0.5", step: "0.5", inputMode: "decimal" });
    ui.names = el("textarea", "nw-in"); ui.names.rows = 3;
    ui.folders = el("textarea", "nw-in"); ui.folders.rows = 3;
    ui.geo = Object.assign(el("input", "nw-in"), { type: "text", spellcheck: false });
    ui.maxlog = Object.assign(el("input", "nw-in"), { type: "number", min: "0", step: "1", inputMode: "numeric" });
    ui.wide = Object.assign(el("input"), { type: "checkbox" });
    set.append(field("nw-interval", "sec.set.interval", "sec.set.interval.hint", ui.interval),
      field("nw-names", "sec.set.names", "sec.set.names.hint", ui.names),
      field("nw-folders", "sec.set.folders", "sec.set.folders.hint", ui.folders));
    ui.expected = el("p", "nw-hint nw-mono");
    set.append(ui.expected, field("nw-geo", "sec.set.geo", "sec.set.geo.hint", ui.geo),
      field("nw-maxlog", "sec.set.maxlog", "sec.set.maxlog.hint", ui.maxlog),
      checkRow("nw-wide", "sec.set.system", "sec.set.system.hint", ui.wide));
    [ui.interval, ui.names, ui.folders, ui.geo, ui.maxlog, ui.wide].forEach(c => c.addEventListener("input", () => { touched = true; }));
    ui.setMsg = el("p", "nw-line"); ui.setMsg.setAttribute("role", "status");
    set.append(btn(T("sec.set.save"), "primary", saveSettings), ui.setMsg);

    const reg = el("section", "nw-sec");
    reg.append(el("h3", "nw-h", T("sec.reg.h")), el("p", "nw-hint", T("sec.reg.hint")));
    const fil = el("div", "nw-bar");
    fil.setAttribute("role", "group");
    fil.setAttribute("aria-label", T("sec.filter.aria"));
    ui.fAll = btn(T("sec.filter.all"), "", () => setFilter("all"));
    ui.fSus = btn(T("sec.filter.susp"), "", () => setFilter("suspicious"));
    fil.append(ui.fAll, ui.fSus, btn(T("sec.verify"), "ghost", verify), btn(T("sec.export"), "ghost", exportLog));
    ui.logline = el("p", "nw-line");
    ui.verifyMsg = el("p", "nw-line"); ui.verifyMsg.setAttribute("role", "status");
    const batch = el("div", "nw-bar");
    ui.delSel = btn("", "", () => remove({ ids: [...picked] }));
    batch.append(btn(T("sec.reg.select.all"), "ghost", () => { events.forEach(e => picked.add(e.id)); paintEvents(true); }),
      btn(T("sec.reg.select.none"), "ghost", () => { picked.clear(); paintEvents(true); }), ui.delSel,
      btn(T("sec.reg.del.all"), "", () => remove({ all: true })));
    const older = el("div", "nw-bar");
    ui.days = Object.assign(el("input", "nw-in nw-days"), { type: "number", min: "0", step: "1", value: "30", inputMode: "numeric" });
    ui.days.setAttribute("aria-label", T("sec.reg.older.days"));
    older.append(el("span", "nw-lab", T("sec.reg.older.l")), ui.days, el("span", "nw-lab", T("sec.reg.older.days")),
      btn(T("sec.reg.older.go"), "", () => remove({ older_than_days: Math.max(0, Number(ui.days.value) || 0) })));
    ui.regMsg = el("p", "nw-line"); ui.regMsg.setAttribute("role", "status");
    ui.count = el("p", "nw-hint");
    ui.list = el("ul", "nw-list");
    reg.append(fil, ui.logline, ui.verifyMsg, batch, older, ui.regMsg, ui.count, ui.list, manualForm());

    const ex = el("section", "nw-sec");
    ex.append(el("h3", "nw-h", T("sec.ex.h")), el("p", "nw-hint", T("sec.ex.hint")));
    ui.exList = el("ul", "nw-list");
    ex.append(ui.exList, btn(T("sec.ex.add"), "", () => { drafts.push({ kind: "behavior", pattern: "", note: "", enabled: true }); paintExceptions(); }));
    root.append(head, watched, set, reg, ex);
    built = true;
  }

  function manualForm() {
    const d = el("details", "nw-manual");
    d.append(el("summary", "nw-sum", T("sec.add.h")), el("p", "nw-hint", T("sec.add.hint")));
    const f = {};
    const mk = (key, id, type, extra) => { f[key] = Object.assign(el("input", "nw-in"), { type, ...extra }); return field("nw-m-" + id, "sec.add." + id, "sec.add." + id + ".hint", f[key]); };
    d.append(mk("process", "process", "text"), mk("proto", "proto", "text", { value: "tcp" }), mk("remote_ip", "ip", "text", { inputMode: "decimal", spellcheck: false }),
      mk("host", "host", "text"), mk("remote_port", "port", "number", { min: "0", max: "65535", inputMode: "numeric" }),
      mk("tags", "tags", "text", { placeholder: "doh_or_tor, proxy_env" }), mk("note", "note", "text"));
    const msg = el("p", "nw-line"); msg.setAttribute("role", "status");
    d.append(btn(T("sec.add.go"), "primary", async () => {
      const body = {}; Object.keys(f).forEach(k => { body[k] = f[k].value; });
      try { await call("api/netwatch/events", body); say(msg, T("sec.add.done")); Object.keys(f).forEach(k => { if (k !== "proto") f[k].value = ""; }); await refresh(true); }
      catch (e) { say(msg, e.message, true); }
    }), msg);
    return d;
  }

  /* ---- data ---- */
  async function loadStatus() {
    S = await call("api/netwatch");
    paintStatus();
  }
  async function loadEvents() {
    const j = await call(`api/netwatch/events?only=${filter}&limit=300`);
    events = j.events || []; total = j.total || 0;
    paintEvents(false);
  }
  async function loadExceptions() {
    excs = (await call("api/netwatch/exceptions")).exceptions || [];
    paintExceptions();
  }
  async function refresh(all) {
    try {
      await Promise.all([loadStatus(), loadEvents(), all ? loadExceptions() : null]);
    } catch (e) { say(ui.state, e.message, true); }
  }

  /* ---- painting ---- */
  function paintStatus() {
    if (!S) return;
    ui.sw.checked = !!S.enabled;
    const c = S.counters || {};
    ui.state.textContent = (S.enabled ? T("sec.state.on") : T("sec.state.off")) + " " + T("sec.state.counts", c.polls || 0, c.events || 0, c.suspicious || 0, c.suppressed || 0)
      + (S.open_alerts ? " " + T("sec.open", S.open_alerts) : "");
    ui.state.dataset.kind = "";
    const cov = S.coverage || {};
    ui.cover.textContent = `${cov.admin ? T("sec.cover.admin.yes") : T("sec.cover.admin.no")} ${T("sec.cover.note")} ${T("sec.cover.geo", S.geo || "")}`
      + (S.tool_calls_reported ? "" : " " + T("sec.toolcalls.no"));
    const warn = [];
    if (!S.psutil) warn.push(T("sec.nopsutil"));
    if (S.last_error) warn.push(T("sec.err.last", S.last_error));
    ui.warn.textContent = warn.join(" ");
    ui.warn.hidden = !warn.length;
    ui.watched.replaceChildren();
    const w = S.watched || [];
    if (!w.length) ui.watched.append(el("li", "nw-hint", T("sec.watched.none")));
    w.forEach(p => ui.watched.append(el("li", "nw-watch", `${p.name} (pid ${p.pid}) ${T("sec.role." + p.role)}`)));
    ui.expected.textContent = T("sec.set.expected", (S.expected_folders || []).join("; "));
    const log = S.log || {};
    ui.logline.textContent = T("sec.log.line", log.records || 0, num.format((log.bytes || 0) / 1024));
    if (!touched) fillSettings();
  }

  function fillSettings() {
    const s = (S && S.settings) || {};
    ui.interval.value = s.interval_s;
    ui.names.value = (s.watch_extra_names || []).join("\n");
    ui.folders.value = (s.expected_folders || []).join("\n");
    ui.geo.value = s.geo_db_path || "";
    ui.maxlog.value = s.max_log_mb || 0;
    ui.wide.checked = !!s.alert_on_system_wide;
  }

  function setFilter(f) {
    filter = f;
    sig = "";
    loadEvents().catch(e => say(ui.regMsg, e.message, true));
  }

  function paintEvents(force) {
    ui.fAll.setAttribute("aria-pressed", String(filter === "all"));
    ui.fSus.setAttribute("aria-pressed", String(filter === "suspicious"));
    const ids = new Set(events.map(e => e.id));
    [...picked].forEach(i => { if (!ids.has(i)) picked.delete(i); });
    ui.delSel.textContent = T("sec.reg.del.sel", picked.size);
    ui.delSel.disabled = !picked.size;
    const now = events.map(e => e.id + e.alert_state + (e.suppressed ? "s" : "")).join("|") + "#" + [...picked].join(",") + filter;
    if (!force && now === sig) return;
    sig = now;
    ui.count.textContent = T("sec.reg.count", events.length, total);
    ui.list.replaceChildren();
    if (!events.length) ui.list.append(el("li", "nw-hint", T("sec.reg.empty")));
    events.forEach(ev => ui.list.append(row(ev)));
  }

  function row(ev) {
    const li = el("li", "nw-row" + (ev.suspicious && !ev.suppressed ? " bad" : ""));
    const top = el("div", "nw-top");
    const cb = el("input"); cb.type = "checkbox"; cb.checked = picked.has(ev.id);
    cb.setAttribute("aria-label", T("sec.reg.pick"));
    cb.addEventListener("change", () => { if (cb.checked) picked.add(ev.id); else picked.delete(ev.id); ui.delSel.textContent = T("sec.reg.del.sel", picked.size); ui.delSel.disabled = !picked.size; sig = ""; });
    const box = el("label", "nw-pick"); box.append(cb);
    top.append(box, el("span", "nw-time", when(ev.ts)));
    if (ev.suspicious) top.append(chip(T("sec.chip.susp"), "warn"));
    if (ev.suppressed) top.append(chip(T("sec.chip.supp"), ""));
    if (ev.manual) top.append(chip(T("sec.chip.manual"), ""));
    if (ev.alert_state === "open") top.append(chip(T("sec.chip.open"), "warn"));
    if (ev.alert_state === "resolved") top.append(chip(T("sec.chip.resolved"), ""));
    const what = el("div", "nw-what");
    what.append(el("b", null, ev.process), document.createTextNode(` ${ev.proto} `), el("span", "nw-mono", target(ev)));
    const acts = el("div", "nw-act");
    acts.append(btn(T("sec.row.except"), "", () => openDialog(ev, "row")), oneTap(T("sec.row.del"), T("sec.row.del.aria"), () => remove({ ids: [ev.id] })));
    li.append(top, what, tagChips(ev.tags), acts);
    return li;
  }

  function oneTap(label, aria, fn) { const b = btn(label, "", fn); b.setAttribute("aria-label", aria); return b; }

  /* ---- exceptions, edited in place ---- */
  function paintExceptions() {
    ui.exList.replaceChildren();
    const all = [...excs, ...drafts];
    if (!all.length) ui.exList.append(el("li", "nw-hint", T("sec.ex.none")));
    all.forEach(x => ui.exList.append(exRow(x)));
  }

  function exRow(x) {
    const li = el("li", "nw-row");
    const draft = !x.id;
    const kind = el("select", "nw-in");
    ["behavior", "resource"].forEach(k => { const o = el("option", null, T("sec.ex.kind." + k)); o.value = k; kind.append(o); });
    kind.value = x.kind;
    const pat = Object.assign(el("input", "nw-in nw-mono"), { type: "text", value: x.pattern, spellcheck: false });
    const note = Object.assign(el("input", "nw-in"), { type: "text", value: x.note || "" });
    const on = el("input"); on.type = "checkbox"; on.checked = x.enabled !== false;
    const msg = el("p", "nw-line"); msg.setAttribute("role", "status");
    const id = "nw-x-" + (x.id || "n" + drafts.indexOf(x));
    const first = el("div", "nw-fieldrow");
    first.append(field(id + "-k", "sec.ex.kind", "sec.ex.kind.hint", kind), field(id + "-p", "sec.ex.pattern", "sec.ex.pattern.hint", pat));
    li.append(first, field(id + "-n", "sec.ex.note", "sec.ex.note.hint", note), checkRow(id + "-e", "sec.ex.enabled", "sec.ex.enabled.hint", on), msg);
    const warn = () => { const w = regexWarning(pat.value); if (w) say(msg, T("sec.ex.regex.warn", w), true); else say(msg, ""); };
    pat.addEventListener("input", warn);
    const save = async () => {
      try {
        const out = await call("api/netwatch/exceptions", { id: x.id, kind: kind.value, pattern: pat.value, note: note.value, enabled: on.checked });
        if (draft) drafts.splice(drafts.indexOf(x), 1);
        else Object.assign(x, out);
        await loadExceptions();
        sig = ""; await loadEvents();
      } catch (e) { say(msg, e.message, true); }
    };
    on.addEventListener("change", () => { if (!draft) save(); });
    const acts = el("div", "nw-act");
    acts.append(btn(T("sec.ex.save"), "primary", save), oneTap(T("sec.ex.del"), T("sec.ex.del"), async () => {
      if (draft) { drafts.splice(drafts.indexOf(x), 1); paintExceptions(); return; }
      try { await call(`api/netwatch/exceptions/${encodeURIComponent(x.id)}/delete`, {}); await loadExceptions(); } catch (e) { say(msg, e.message, true); }
    }));
    li.append(acts);
    return li;
  }

  /* ---- actions ---- */
  async function toggle() {
    try { await call("api/netwatch/settings", { enabled: ui.sw.checked }); await refresh(false); startTimers(); } catch (e) { say(ui.state, e.message, true); ui.sw.checked = !ui.sw.checked; }
  }
  async function saveSettings() {
    const lines = v => v.split("\n").map(s => s.trim()).filter(Boolean);
    try {
      S = await call("api/netwatch/settings", {
        interval_s: Number(ui.interval.value), watch_extra_names: lines(ui.names.value), expected_folders: lines(ui.folders.value),
        geo_db_path: ui.geo.value.trim(), max_log_mb: Number(ui.maxlog.value) || 0, alert_on_system_wide: ui.wide.checked });
      touched = false; fillSettings(); paintStatus(); say(ui.setMsg, T("sec.set.saved"));
    } catch (e) { say(ui.setMsg, e.message, true); }
  }
  async function remove(req) {
    try {
      const j = await call("api/netwatch/events/delete", req);
      picked.clear(); sig = "";
      say(ui.regMsg, T("sec.reg.deleted", j.deleted));
      await refresh(false);
    } catch (e) { say(ui.regMsg, e.message, true); }
  }
  async function verify() {
    try {
      const j = await call("api/netwatch/verify");
      let t = j.ok ? T("sec.verify.ok", j.records) : T("sec.verify.bad", j.problem_count, (j.problems || [])[0] || "");
      if (j.compactions) t += " " + T("sec.verify.comp", j.compactions);
      say(ui.verifyMsg, t, !j.ok);
    } catch (e) { say(ui.verifyMsg, e.message, true); }
  }
  async function exportLog() {
    try {
      const j = await call("api/netwatch/export");
      const url = URL.createObjectURL(new Blob([JSON.stringify(j, null, 2)], { type: "application/json" }));
      const a = el("a"); a.href = url; a.download = "netwatch-export.json";
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (e) { say(ui.verifyMsg, e.message, true); }
  }

  /* ---- the dialog: the alert and the exception form of any row ---- */
  function ensureDialog() {
    if (dlg) return dlg;
    dlg = el("dialog", "nw-dlg");
    dlg.setAttribute("aria-labelledby", "nw-dlg-h");
    document.body.append(dlg);
    return dlg;
  }

  function openDialog(ev, mode) {
    if (dlgBusy) return;
    const d = ensureDialog();
    dlgBusy = true;
    const alert = mode === "alert";
    const form = el("form", "nw-dform");
    form.addEventListener("submit", e => e.preventDefault());
    const h = el("h2", "nw-h", alert ? T("sec.dlg.title.alert") : T("sec.dlg.title.row"));
    h.id = "nw-dlg-h";
    form.append(h, el("p", "nw-p", describe(ev)));
    if (ev.role === "engine" && ev.kind === "network") form.append(el("p", "nw-hint", T("sec.why.engine")));
    if ((ev.tags || []).length) {
      const ul = el("ul", "nw-plain");
      ev.tags.forEach(t => { const li = el("li", "nw-tagx"); li.append(el("b", null, TAGS.includes(t) ? T("sec.tag." + t) : t), document.createTextNode(" " + (TAGS.includes(t) ? T("sec.tagx." + t) : ""))); ul.append(li); });
      form.append(ul);
    }
    const sug = ev.suggest || {};
    const parts = {};
    [["behavior", sug.behavior_pattern, ev.behavior], ["resource", sug.resource_pattern, ev.resource]].forEach(([kind, pattern, text]) => {
      const box = el("div", "nw-ex");
      const cb = el("input"); cb.type = "checkbox";
      const l = el("label", "nw-check"); cb.id = `nw-d-${kind}-c`; l.htmlFor = cb.id; l.append(cb, el("span", null, T("sec.dlg.add")));
      const inp = Object.assign(el("input", "nw-in nw-mono"), { type: "text", value: pattern || "", spellcheck: false });
      const msg = el("p", "nw-line"); msg.setAttribute("role", "status");
      const fld = field(`nw-d-${kind}`, "sec.dlg." + kind, "sec.dlg." + kind + ".hint", inp);
      inp.addEventListener("input", () => { const w = regexWarning(inp.value); say(msg, w ? T("sec.ex.regex.warn", w) : "", !!w); });
      box.append(fld, el("p", "nw-hint nw-mono", T("sec.dlg.match", text)), l, msg);
      parts[kind] = { cb, inp, msg };
      form.append(box);
    });
    const note = Object.assign(el("input", "nw-in"), { type: "text" });
    form.append(field("nw-d-note", "sec.dlg.note", "sec.dlg.note.hint", note));
    const err = el("p", "nw-line"); err.setAttribute("role", "status");
    const acts = el("div", "nw-act");
    const ok = btn(T("sec.dlg.ok"), "primary");
    ok.disabled = true;
    const sync = () => { ok.disabled = !(parts.behavior.cb.checked || parts.resource.cb.checked); };
    parts.behavior.cb.addEventListener("change", sync); parts.resource.cb.addEventListener("change", sync);
    const finish = async () => { d.close(); await refresh(true); await pollAlerts(); };
    ok.addEventListener("click", async () => {
      const b = parts.behavior, r = parts.resource;
      try {
        if (ev.alert_state === "open") {
          await call(`api/netwatch/alerts/${encodeURIComponent(ev.id)}/resolve`, { add_behavior: b.cb.checked, behavior_pattern: b.inp.value,
            add_resource: r.cb.checked, resource_pattern: r.inp.value, note: note.value });
        } else {
          if (b.cb.checked) await call("api/netwatch/exceptions", { kind: "behavior", pattern: b.inp.value, note: note.value });
          if (r.cb.checked) await call("api/netwatch/exceptions", { kind: "resource", pattern: r.inp.value, note: note.value });
        }
        await finish();
      } catch (e) { say(err, e.message, true); }
    });
    acts.append(ok);
    if (alert) {
      acts.append(btn(T("sec.dlg.ignore"), "", async () => {
        try { await call(`api/netwatch/alerts/${encodeURIComponent(ev.id)}/resolve`, {}); await finish(); } catch (e) { say(err, e.message, true); }
      }));
    } else acts.append(btn(T("sec.dlg.cancel"), "ghost", () => d.close()));
    form.append(err, acts);
    d.replaceChildren(form);
    d.onclose = () => { dlgBusy = false; };
    d.oncancel = () => { if (alert) snoozed.add(ev.id); };        // Esc leaves the alert open in the register
    d.showModal();
    parts.behavior.cb.focus();
  }

  /* ---- timers ---- */
  async function pollAlerts() {
    if (alertsOff || dlgBusy || document.hidden) return;
    try {
      const j = await call("api/netwatch/alerts");
      const next = (j.alerts || []).find(a => !snoozed.has(a.id));
      if (next) openDialog(next, "alert");
    } catch (e) { if (e.status === 403 || e.status === 401) alertsOff = true; }
  }
  function startTimers() {
    clearInterval(alertTimer);
    alertTimer = setInterval(pollAlerts, ALERT_MS);
    clearInterval(viewTimer);
    viewTimer = setInterval(() => { if (document.body.dataset.view === "security" && !document.hidden) refresh(false); }, VIEW_MS);
  }

  document.addEventListener("basalt:view", e => {
    if (e.detail !== "security") return;
    if (!built) build();
    refresh(true);
  });
  document.addEventListener("basalt:lang", () => { if (built) { build(); paintStatus(); paintEvents(true); paintExceptions(); } });
  startTimers();
  pollAlerts();
  if (document.body.dataset.view === "security") { build(); refresh(true); }       // the page was opened on this view
})();
