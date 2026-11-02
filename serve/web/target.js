"use strict";
/* Target Loop: the button next to Send (in the Chat and in the Agent tab, which share one page) and its dialog. The person describes a target in a
   large text box (with the list tools of listtools.js); the server splits it into items to check and tasks, works through them in this conversation,
   reviews the result against the evidence and goes on with what is not met (serve/target_api.py, tadamoo/target.py). The dialog shows the target
   with what is achieved struck through in red, the items with notes the person can edit, the tasks and sub-tasks, the passes, and every earlier
   target of the conversation, which can be opened, resumed or run again. Everything the server or the model wrote enters the page as text nodes. */
(() => {
  const dlg = document.getElementById("target-dlg"), openBtn = document.getElementById("target-open");
  if (!dlg || !openBtn) return;
  const DRAFT = "basalt.target.draft", OPTS = "basalt.target.options", FAST_MS = 1200, SLOW_MS = 4000, FORGET_MS = 4000;
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick, tip) => { const b = mk("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; if (tip) b.title = tip; return b; };
  const load = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } };
  const keep = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* this page only */ } };
  const $t = id => document.getElementById(id);
  const text = $t("tg-text"), form = $t("tg-new-view"), view = $t("tg-view"), list = $t("tg-list"), statusEl = $t("tg-status");
  const chat = () => window.agentChat;

  let sid = "", targets = [], shownId = "", timer = 0, signature = "", deferred = false, forgetArmed = "", forgetTimer = 0;

  async function api(path, method, body) {
    try {
      const r = await fetch(`api/agent/sessions/${sid}/targets${path}`, { method: method || "GET", cache: "no-store",
        headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let j = {};
      try { j = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, json: j };
    } catch (e) { return { ok: false, status: 0, json: {} }; }
  }
  function errMsg(r) {
    const e = (r.json && r.json.error) || {}, key = "tg.e." + (e.code || "");
    const known = e.code && T(key) !== key;
    return known ? T(key) : T("tg.e.other", e.message || `HTTP ${r.status}`);
  }
  const say = (msg, bad) => { statusEl.textContent = msg || ""; statusEl.dataset.bad = bad ? "1" : ""; };
  const when = s => new Date(s * 1000).toLocaleString(LANG === "it" ? "it-IT" : "en-GB", { dateStyle: "short", timeStyle: "short" });
  const critText = c => (c.check ? T("tg.check." + c.check.kind, c.check.value) : c.text);
  const byId = (items, id) => items.find(x => x.id === id);

  /* ---- the button next to Send: it needs a conversation with the agent ---- */
  function syncButton() {
    const a = chat(), ok = !!(a && a.active && a.active());
    openBtn.disabled = !ok;
    openBtn.title = ok ? T("tg.open.tip") : T("tg.open.needs");
  }
  setInterval(syncButton, 1500);
  syncButton();

  async function open() {
    const a = chat();
    if (!a || !a.active()) return;
    if (!(await a.ensure())) return;
    sid = a.current();
    targets = []; signature = ""; shownId = ""; forgetArmed = "";
    say(T("tg.loading"));
    if (!dlg.open) dlg.showModal();
    await refresh(true);
    const running = targets.find(t => t.state === "running");
    if (running) showTarget(running.id); else showNew();
  }
  openBtn.onclick = open;
  $t("tg-close").onclick = () => dlg.close();
  dlg.addEventListener("close", () => { clearTimeout(timer); });

  /* ---- reading the server ---- */
  async function refresh(force) {
    clearTimeout(timer);
    if (!dlg.open || !sid) return;
    const r = await api("");
    if (r.ok) {
      targets = r.json.targets || [];
      if (statusEl.textContent === T("tg.loading")) say("");
      paintHistory();
      const t = byId(targets, shownId);
      if (t) {
        const sig = [t.id, t.updated, t.state].join("|");
        const typing = view.contains(document.activeElement) && document.activeElement.tagName === "TEXTAREA";
        if (force || sig !== signature) { if (typing && !force) deferred = true; else { deferred = false; paintTarget(t); } }
      }
    }
    const running = targets.some(t => t.state === "running");
    timer = setTimeout(() => refresh(deferred), running ? FAST_MS : SLOW_MS);
  }
  document.addEventListener("basalt:target", () => { if (dlg.open) refresh(false); });

  /* ---- the history ---- */
  function paintHistory() {
    $t("tg-hist-empty").hidden = targets.length > 0;
    list.replaceChildren(...targets.map(t => {
      const li = mk("li"), b = mk("button", "tgd-item");
      b.type = "button"; b.setAttribute("aria-current", String(t.id === shownId));
      const c = t.counts || {};
      b.append(mk("span", "tgd-item-t", t.text),
        mk("span", "tgd-item-m", `${T("tg.state." + t.state)} · ${T("tg.progress", c.achieved || 0, c.total || 0)} · ${when(t.created)}`));
      b.onclick = () => showTarget(t.id);
      li.append(b);
      return li;
    }));
  }

  /* ---- a new target ---- */
  function readOptions() {
    const lines = id => $t(id).value.split("\n").map(x => x.trim()).filter(Boolean);
    return { verify: lines("tg-verify"), files: lines("tg-files"), stall_passes: parseInt($t("tg-stall").value, 10) || 3, max_iterations: parseInt($t("tg-max").value, 10) || 0,
             first_message: $t("tg-first").value };
  }
  function putOptions(o) {
    const opts = o || {};
    $t("tg-verify").value = (opts.verify || []).join("\n"); $t("tg-files").value = (opts.files || []).join("\n");
    $t("tg-stall").value = opts.stall_passes || 3; $t("tg-max").value = opts.max_iterations || 0;
    $t("tg-first").value = opts.first_message === "separate" ? "separate" : "joined";
  }
  function showNew(draft, options) {
    shownId = ""; signature = "";
    form.hidden = false; view.hidden = true;
    text.value = draft != null ? draft : load(DRAFT, "");
    putOptions(options || load(OPTS, {}));
    paintHistory();
    text.focus();
  }
  text.addEventListener("input", () => keep(DRAFT, text.value));
  text.addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); form.requestSubmit(); } });
  $t("tg-new").onclick = () => { say(""); showNew(); };
  form.onsubmit = async e => {
    e.preventDefault();
    const body = text.value.trim();
    if (!body) { say(T("tg.e.text_missing"), true); text.focus(); return; }
    const start = $t("tg-start"), options = readOptions();
    start.disabled = true;
    const r = await api("", "POST", { text: body, options });
    start.disabled = false;
    if (!r.ok) { say(errMsg(r), true); return; }
    keep(OPTS, options); keep(DRAFT, "");
    text.value = "";
    say(T("tg.started"));
    setTimeout(() => { if (statusEl.textContent === T("tg.started")) say(""); }, 5000);
    await refresh(true);
    showTarget(r.json.target.id);
    if (chat() && chat().refresh) chat().refresh();
  };
  if (window.ListTools) window.ListTools.attach(text, { bar: bar => $t("tg-bar").append(bar), offer: window.ListTools.offerIn(form, text) });

  /* ---- a target ---- */
  function showTarget(id) {
    shownId = id; signature = ""; forgetArmed = ""; deferred = false;
    form.hidden = true; view.hidden = false;
    const t = byId(targets, id);
    if (t) paintTarget(t);
    paintHistory();
  }
  function replace(t) {
    targets = targets.map(x => (x.id === t.id ? t : x));
    paintHistory();
    paintTarget(t);
  }
  function highlighted(t) {
    const box = mk("div", "tgd-text"), marks = [];
    t.criteria.forEach(c => { const s = (c.source || "").trim(), i = s ? t.text.indexOf(s) : -1; if (i >= 0 && c.status !== "pending") marks.push({ from: i, to: i + s.length, c }); });
    marks.sort((a, b) => a.from - b.from);
    let pos = 0;
    marks.forEach(m => {
      if (m.from < pos) return;
      box.append(document.createTextNode(t.text.slice(pos, m.from)));
      const span = mk("span", "tg-mark-" + m.c.status, t.text.slice(m.from, m.to));
      span.title = T("tg.crit." + m.c.status);
      box.append(span, mk("span", "sr", ` [${T("tg.crit." + m.c.status)}] `));
      pos = m.to;
    });
    box.append(document.createTextNode(t.text.slice(pos)));
    return box;
  }
  function openList(t) {
    return t.criteria.filter(c => ["pending", "partial", "not_met"].includes(c.status)).map(c => critText(c)).join("; ") || "-";
  }
  function reportOf(t) {
    if (t.state === "running") return "";
    if (t.state === "achieved") return T("tg.report.achieved", (t.summary || {}).total || t.criteria.length, (t.summary || {}).passes || t.iterations.length, (t.summary || {}).seconds || 0);
    if (t.state === "blocked") return T("tg.report.blocked", t.criteria.filter(c => c.status === "blocked").map(critText).join("; "));
    const key = "tg.report." + (t.stop_reason || "person");
    return t.stop_reason === "error" ? T(key, t.error || "", openList(t)) : T(key, openList(t));
  }
  function critRow(t, c) {
    const li = mk("li", "tgd-crit"), head = mk("div", "tgd-crit-h");
    li.dataset.s = c.status;
    head.append(mk("span", "tgd-crit-st", `${c.status === "achieved" ? "✓ " : c.status === "blocked" ? "⏸ " : c.status === "pending" ? "○ " : c.status === "partial" ? "◐ " : "✕ "}${T("tg.crit." + c.status)}`),
      mk("span", "tgd-crit-t", critText(c)), mk("span", "tgd-crit-id", T("tg.crit.id", c.id) + (c.by === "person" ? ` · ${T("tg.crit.by")}` : "")));
    li.append(head);
    if (c.missing && c.status !== "achieved") li.append(mk("p", "", T("tg.crit.missing", c.missing)));
    if (c.evidence) li.append(mk("p", "", T("tg.crit.evidence", c.evidence)));
    (c.flags || []).forEach(f => li.append(mk("p", "", T("tg.flag." + f))));
    const row = mk("div", "tgd-crit-f"), area = mk("textarea"), id = `tg-note-${c.id}`;
    area.id = id; area.value = c.note || ""; area.placeholder = T("tg.note.ph"); area.rows = 2; area.setAttribute("aria-label", `${T("tg.note.label")}: ${critText(c)}`);
    area.onchange = async () => {
      const r = await api(`/${t.id}/notes`, "POST", { criterion: c.id, note: area.value });
      say(r.ok ? T("tg.note.saved") : T("tg.note.fail", errMsg(r)), !r.ok);
      if (r.ok) targets = targets.map(x => (x.id === t.id ? r.json.target : x));
    };
    const buttons = mk("div", "tgd-crit-b");
    const mark = (status, label, tip) => btn(label, "ghost", async () => {
      const r = await api(`/${t.id}/notes`, "POST", { criterion: c.id, note: area.value, status });
      if (r.ok) replace(r.json.target); else say(errMsg(r), true);
    }, tip);
    if (c.status !== "achieved") buttons.append(mark("achieved", T("tg.mark.achieved"), T("tg.mark.achieved.tip")));
    if (c.status !== "blocked" && c.status !== "achieved") buttons.append(mark("blocked", T("tg.mark.blocked"), T("tg.mark.blocked.tip")));
    if (c.status !== "pending") buttons.append(mark("pending", T("tg.mark.recheck"), T("tg.mark.recheck.tip")));
    row.append(area, buttons);
    li.append(row);
    return li;
  }
  function taskNode(t, task, all) {
    const li = mk("li", "tgd-task"), head = mk("div", "tgd-task-h"), kids = all.filter(k => k.parent === task.id);
    li.dataset.s = task.status;
    head.append(mk("b", "", task.title), mk("span", "tgd-task-s", T("tg.task." + task.status)));
    if (kids.length) head.append(mk("span", "tgd-task-m", T("tg.task.group", kids.length)));
    if ((task.serves || []).length) head.append(mk("span", "tgd-task-m", T("tg.task.serves", task.serves.join(", "))));
    if ((task.depends_on || []).length) head.append(mk("span", "tgd-task-m", T("tg.task.after", task.depends_on.join(", "))));
    li.append(head);
    const meta = mk("div", "tgd-task-m", T("tg.task.id", `tg-${t.id}-${task.id}`));
    li.append(meta);
    if (task.result && !kids.length) li.append(mk("div", "tgd-task-m", task.result));
    if (kids.length) { const ul = mk("ul"); kids.forEach(k => ul.append(taskNode(t, k, all))); li.append(ul); }
    return li;
  }
  function passRow(t, it) {
    const li = mk("li", "tgd-pass"), v = it.verdict || {};
    const secs = it.ended && it.started ? Math.round(it.ended - it.started) : 0;
    li.append(mk("b", "", T("tg.pass.line", it.n, v.achieved || 0, v.total || 0, (it.tasks || []).length)), document.createTextNode(` · ${T("tg.pass.took", secs)}`));
    const sub = mk("ul");
    if ((it.checks || []).length) sub.append(mk("li", "", T("tg.pass.checks", it.checks.filter(c => c.ok).length, it.checks.length)));
    const next = it.next;
    if (next) {
      if (next.mode === "approach") sub.append(mk("li", "", T("tg.pass.approach")));
      (next.analysis || []).forEach(a => { sub.append(mk("li", "", T("tg.pass.why", a.id, a.why))); sub.append(mk("li", "", T("tg.pass.change", a.change))); });
    }
    if (sub.children.length) li.append(sub);
    return li;
  }
  function actions(t) {
    const row = mk("div", "tgd-actions");
    const act = async (path, body) => { const r = await api(`/${t.id}/${path}`, "POST", body); if (!r.ok) { say(errMsg(r), true); return null; } say(""); return r.json; };
    if (t.state === "running") row.append(btn(T("tg.stop"), "primary", async () => { const j = await act("stop", {}); if (j) replace(j.target); }, T("tg.stop.tip")));
    else {
      if (t.state !== "achieved") row.append(btn(T("tg.resume"), "primary", async () => { const j = await act("rerun", { mode: "resume" }); if (j) { replace(j.target); refresh(true); } }, T("tg.resume.tip")));
      row.append(btn(T("tg.copy"), "", async () => { const j = await act("rerun", { mode: "copy" }); if (j) { await refresh(true); showTarget(j.target.id); } }, T("tg.copy.tip")));
      row.append(btn(T("tg.reuse"), "ghost", () => showNew(t.text, t.options), T("tg.reuse.tip")));
      const forget = btn(forgetArmed === t.id ? T("tg.forget.sure") : T("tg.forget"), "ghost", async () => {
        if (forgetArmed !== t.id) { forgetArmed = t.id; clearTimeout(forgetTimer); forgetTimer = setTimeout(() => { forgetArmed = ""; const cur = byId(targets, shownId); if (cur) paintTarget(cur); }, FORGET_MS); paintTarget(t); return; }
        const j = await act("forget", {});
        if (j) { targets = j.targets || []; forgetArmed = ""; targets.length ? showTarget(targets[0].id) : showNew(); paintHistory(); }
      }, T("tg.forget.tip"));
      row.append(forget);
    }
    return row;
  }
  function paintTarget(t) {
    signature = [t.id, t.updated, t.state].join("|");
    const c = t.counts || { achieved: 0, total: t.criteria.length };
    const head = mk("div", "tgd-head"), badge = mk("span", "tgd-badge", T("tg.state." + t.state));
    badge.dataset.s = t.state;
    const prog = mk("div", "tgd-prog"), bar = mk("i");
    prog.setAttribute("role", "progressbar"); prog.setAttribute("aria-valuemin", "0"); prog.setAttribute("aria-valuemax", String(c.total)); prog.setAttribute("aria-valuenow", String(c.achieved));
    prog.setAttribute("aria-label", T("tg.progress", c.achieved, c.total));
    bar.style.width = c.total ? `${Math.round(100 * c.achieved / c.total)}%` : "0%";
    prog.append(bar);
    head.append(badge, mk("span", "", T("tg.progress", c.achieved, c.total)), prog);
    if (t.state === "running" && t.phase) head.append(mk("span", "tgd-dim", T("tg.phase." + t.phase)));
    const parts = [head, actions(t)];
    const rep = reportOf(t);
    if (rep) parts.push(mk("p", "tgd-report", rep));
    const textSec = mk("section"); textSec.append(mk("h3", "", T("tg.sec.text")), highlighted(t));
    parts.push(textSec);
    if (t.criteria.length) {
      const sec = mk("section"), ul = mk("ul", "tgd-crits");
      t.criteria.forEach(x => ul.append(critRow(t, x)));
      sec.append(mk("h3", "", T("tg.sec.criteria")), mk("p", "tgd-dim", T("tg.sec.criteria.help")), ul);
      parts.push(sec);
    }
    if (t.tasks.length) {
      const sec = mk("section"), ul = mk("ul", "tgd-tasks");
      t.tasks.filter(x => !x.parent).forEach(x => ul.append(taskNode(t, x, t.tasks)));
      sec.append(mk("h3", "", `${T("tg.sec.tasks")} (${T("tg.tasks.count", t.tasks.filter(x => !t.tasks.some(k => k.parent === x.id)).length)})`), mk("p", "tgd-dim", T("tg.sec.tasks.help")), ul);
      parts.push(sec);
    }
    if (t.iterations.length) {
      const sec = mk("section"), ul = mk("ul", "tgd-passes");
      t.iterations.forEach(it => ul.append(passRow(t, it)));
      sec.append(mk("h3", "", T("tg.sec.passes")), ul);
      parts.push(sec);
    }
    view.replaceChildren(...parts);
  }

  [dlg].forEach(root => applyT(root));
})();
