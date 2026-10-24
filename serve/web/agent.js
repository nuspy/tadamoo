"use strict";
/* The Agent view: start the agent on a folder and a task, see what it does, answer its questions, stop it.
   The agent is a separate process the server starts (POST api/agent/runs); this page only asks and shows.
   Everything the agent or the server says is put in the page as text, never as markup. */
(function () {
  const POLL_BUSY_MS = 1500;
  const POLL_IDLE_MS = 6000;
  const LOG_LINES = 300;
  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* the choice lasts until the page closes */ } },
  };
  const $id = id => document.getElementById(id);
  const MODES = ["plan", "read_only", "default", "auto"];

  let runs = [];
  let active = null;
  let selected = null;
  let approvals = { pending: [], recent: [] };
  let loaded = true;
  let timer = null;
  let starting = false;
  let logSig = "";

  const text = (tag, cls, value) => {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    el.textContent = value;
    return el;
  };

  async function call(method, path, body) {
    try {
      const res = await fetch(path, {
        method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() },
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await res.json().catch(() => ({}));
      return { ok: res.ok, status: res.status, data };
    } catch (e) {
      return { ok: false, status: 0, data: {} };
    }
  }

  const explain = res => {
    const err = (res.data && res.data.error) || {};
    if (res.status === 0) return T("ag.e.offline");
    if (res.status === 401) return T("ag.e.key");
    if (res.status === 403) return T("ag.e.local");
    const key = err.code ? "ag.e." + err.code : "";
    return key && T(key) !== key ? T(key) : (err.message || String(res.status));
  };

  /* ---- formatting ---- */
  function clock(seconds) {
    const s = Math.max(0, Math.round(seconds));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    const two = n => String(n).padStart(2, "0");
    return h ? `${h}:${two(m)}:${two(r)}` : `${m}:${two(r)}`;
  }
  const when = ts => (ts ? new Date(ts * 1000).toLocaleString(LANG === "it" ? "it-IT" : "en-US", { dateStyle: "short", timeStyle: "short" }) : "");

  /* ---- the form ---- */
  const modeValue = () => (document.querySelector('input[name="ag-mode"]:checked') || {}).value || "default";

  function savedForm() {
    $id("ag-folder").value = store.get("basalt.agent.folder", "");
    const mode = store.get("basalt.agent.mode", "default");
    const radio = document.querySelector(`input[name="ag-mode"][value="${MODES.includes(mode) && mode !== "auto" ? mode : "default"}"]`);
    if (radio) radio.checked = true;
    $id("ag-steps").value = store.get("basalt.agent.steps", "15");
    $id("ag-allow").value = store.get("basalt.agent.allow", "");
  }

  function rememberForm() {
    store.set("basalt.agent.folder", $id("ag-folder").value.trim());
    store.set("basalt.agent.mode", modeValue() === "auto" ? "default" : modeValue());    // automatic is chosen anew each time
    store.set("basalt.agent.steps", $id("ag-steps").value);
    store.set("basalt.agent.allow", $id("ag-allow").value);
  }

  function showMessage(message, bad = true) {
    const el = $id("ag-msg");
    el.textContent = message;
    el.classList.toggle("bad", bad && !!message);
  }

  function syncForm() {
    const busy = starting || !!active;
    $id("ag-auto-row").hidden = modeValue() !== "auto";
    $id("ag-start").disabled = busy || !loaded;
    $id("ag-stop").hidden = !active;
    $id("ag-nomodel").hidden = loaded;
    document.querySelectorAll("#ag-form input, #ag-form textarea").forEach(el => { el.disabled = busy; });
    $id("ag-start").textContent = starting ? T("ag.starting") : T("ag.start");
  }

  async function submit(event) {
    if (event) event.preventDefault();
    if (starting || active) return;
    const mode = modeValue();
    if (mode === "auto" && !$id("ag-auto").checked) { showMessage(T("ag.e.auto_confirm")); $id("ag-auto").focus(); return; }
    const allow = $id("ag-allow").value.split("\n").map(s => s.trim()).filter(Boolean);
    rememberForm();
    starting = true;
    showMessage(T("ag.starting"), false);
    syncForm();
    const res = await call("POST", "api/agent/runs", {
      workspace: $id("ag-folder").value.trim(), goal: $id("ag-goal").value, mode, allow,
      max_turns: Number($id("ag-steps").value) || 15, confirm_auto: mode === "auto" && $id("ag-auto").checked,
    });
    starting = false;
    if (!res.ok) { showMessage(explain(res)); syncForm(); return; }
    showMessage("");
    $id("ag-auto").checked = false;
    selected = res.data.run.id;
    logSig = "";
    await tick();
    const heading = $id("ag-run-h");
    if (heading) heading.focus();
  }

  async function stop() {
    if (!active) return;
    const res = await call("POST", `api/agent/runs/${active}/stop`, {});
    if (!res.ok) showMessage(explain(res));
    await tick();
  }

  /* ---- what the agent is doing ---- */
  function renderRun() {
    const run = runs.find(r => r.id === selected) || runs[0];
    const box = $id("ag-run");
    if (!run) {
      box.replaceChildren(text("p", "dim ag-empty", T("ag.run.none")));
      $id("ag-log").textContent = "";
      return;
    }
    const head = document.createElement("div");
    head.className = `ag-status st-${run.status}`;
    const icons = { running: "▶", done: "✓", failed: "✕", stopped: "■", interrupted: "!" };
    head.append(text("span", "ag-badge", `${icons[run.status] || "•"} ${T("ag.status." + run.status)}`));
    const end = run.ended || Date.now() / 1000;
    head.append(text("span", "ag-time", clock(end - run.started)));
    const meta = document.createElement("dl");
    meta.className = "ag-meta";
    [["ag.run.task", run.goal], ["ag.run.folder", run.workspace], ["ag.run.mode", T("ag.mode." + run.mode)], ["ag.run.started", when(run.started)]]
      .forEach(([k, v]) => { meta.append(text("dt", "", T(k)), text("dd", "", v)); });
    box.replaceChildren(head, meta);
    if (run.summary && run.status !== "running") box.append(text("p", "ag-summary", run.summary));
    const go = document.createElement("button");
    go.type = "button";
    go.className = "btn";
    go.textContent = T("ag.run.review");
    go.addEventListener("click", () => view("stats"));
    box.append(go);
  }

  async function refreshLog() {
    const run = runs.find(r => r.id === selected) || runs[0];
    const pre = $id("ag-log");
    if (!run) return;
    const sig = `${run.id}|${run.status}|${run.ended}`;
    if (run.status !== "running" && sig === logSig) return;
    logSig = sig;
    const res = await call("GET", `api/agent/runs/${run.id}/log?lines=${LOG_LINES}`);
    if (!res.ok) return;
    const near = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
    pre.textContent = res.data.text || T("ag.log.empty");
    if (near) pre.scrollTop = pre.scrollHeight;
  }

  function renderHistory() {
    const box = $id("ag-history");
    const older = runs.filter(r => r.id !== (runs.find(x => x.id === selected) || runs[0] || {}).id).slice(0, 10);
    if (!older.length) { box.replaceChildren(text("p", "dim ag-empty", T("ag.hist.none"))); return; }
    const list = document.createElement("ul");
    list.className = "ag-list";
    older.forEach(r => {
      const li = document.createElement("li");
      li.className = `ag-item st-${r.status}`;
      const info = document.createElement("div");
      info.append(text("div", "ag-item-goal", r.goal), text("div", "ag-item-sub", `${T("ag.status." + r.status)} · ${when(r.started)} · ${r.workspace}`));
      const show = document.createElement("button");
      show.type = "button";
      show.className = "btn";
      show.textContent = T("ag.hist.show");
      show.setAttribute("aria-label", `${T("ag.hist.show")}: ${r.goal}`);
      show.addEventListener("click", () => { selected = r.id; logSig = ""; renderAll(); refreshLog(); });
      li.append(info, show);
      list.append(li);
    });
    box.replaceChildren(list);
  }

  /* ---- what it asks ---- */
  async function decide(item, approve) {
    const res = await call("POST", `api/agent/approvals/${item.id}`, { decision: approve ? "approve" : "deny" });
    if (!res.ok) showMessage(explain(res));
    await tick();
  }

  function renderApprovals() {
    const box = $id("ag-approvals");
    const pending = approvals.pending || [];
    if (!pending.length) {
      box.replaceChildren(text("p", "dim ag-empty", T("ag.ap.none")));
    } else {
      const list = document.createElement("ul");
      list.className = "ag-list";
      pending.forEach(item => {
        const li = document.createElement("li");
        li.className = "ag-ask";
        li.append(text("div", "ag-ask-title", T("ag.ap.ask", item.tool)));
        const pre = text("pre", "ag-args", prettyArgs(item.arguments));
        pre.tabIndex = 0;
        li.append(pre);
        const row = document.createElement("div");
        row.className = "ag-actions";
        const yes = document.createElement("button");
        yes.type = "button";
        yes.className = "btn primary";
        yes.textContent = T("ag.ap.approve");
        yes.setAttribute("aria-label", `${T("ag.ap.approve")}: ${item.tool}`);
        yes.addEventListener("click", () => decide(item, true));
        const no = document.createElement("button");
        no.type = "button";
        no.className = "btn";
        no.textContent = T("ag.ap.deny");
        no.setAttribute("aria-label", `${T("ag.ap.deny")}: ${item.tool}`);
        no.addEventListener("click", () => decide(item, false));
        row.append(yes, no);
        li.append(row);
        list.append(li);
      });
      box.replaceChildren(list);
    }
    const section = $id("ag-ap-sec");
    if (section) section.classList.toggle("ag-waiting", pending.length > 0);
    const tab = $id("tab-agent");
    if (tab) {
      const n = pending.length;
      tab.textContent = n ? `${T("tab.agent")} (${n})` : T("tab.agent");
      if (n) tab.setAttribute("aria-label", `${T("tab.agent")}: ${T("ag.ap.count", n)}`); else tab.removeAttribute("aria-label");
    }
  }

  function prettyArgs(raw) {
    try {
      const data = JSON.parse(raw);
      if (data && typeof data === "object" && typeof data.command === "string" && Object.keys(data).length === 1) return data.command;
      return JSON.stringify(data, null, 2);
    } catch (e) {
      return String(raw);
    }
  }

  /* ---- polling ---- */
  function renderAll() {
    renderRun();
    renderHistory();
    renderApprovals();
    syncForm();
  }

  async function tick() {
    const [r, a, l] = await Promise.all([call("GET", "api/agent/runs"), call("GET", "api/agent/approvals"), call("GET", "api/loader/status")]);
    if (r.ok) {
      runs = r.data.runs || [];
      active = r.data.active || null;
      if (!selected || !runs.some(x => x.id === selected)) selected = active || (runs[0] && runs[0].id) || null;
    }
    if (a.ok) approvals = a.data;
    if (l.ok) loaded = !!l.data.loaded;
    renderAll();
    await refreshLog();
    schedule();
  }

  function schedule() {
    clearTimeout(timer);
    timer = null;
    if (document.hidden) return;
    const onPage = document.body.dataset.view === "agent";
    if (!onPage && !active) return;                       // off the page and nothing running: nothing to watch
    timer = setTimeout(tick, active ? POLL_BUSY_MS : POLL_IDLE_MS);
  }

  function datalist() {
    const folders = [...new Set(runs.map(r => r.workspace))].slice(0, 8);
    $id("ag-folders").replaceChildren(...folders.map(f => { const o = document.createElement("option"); o.value = f; return o; }));
  }

  function init() {
    if (!$id("ag-form")) return;
    applyT($id("view-agent"));                              // the static texts of the view, in the page's language
    savedForm();
    $id("ag-form").addEventListener("submit", submit);
    $id("ag-stop").addEventListener("click", stop);
    document.querySelectorAll('input[name="ag-mode"]').forEach(r => r.addEventListener("change", syncForm));
    $id("ag-goal").addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit(e); });
    $id("ag-nomodel-go").addEventListener("click", () => view("loader"));
    renderAll();
  }

  document.addEventListener("basalt:view", e => { if (e.detail === "agent") { tick().then(datalist); } else schedule(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); else schedule(); });
  document.addEventListener("DOMContentLoaded", () => { init(); tick().then(datalist); });
})();
