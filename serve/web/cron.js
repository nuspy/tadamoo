"use strict";
/* Cron Jobs: the agent's recurring tasks (the routines of the server), with a list, an editor, the latest results of a job, and a small
   chat with the model to talk a job over. The chat only proposes: a proposal fills the editor and nothing is saved or turned on until the
   person presses Save and On. Everything the server or the model wrote enters the page as text. */
(function () {
  const root = document.getElementById("cron-root");
  if (!root) return;

  const MODES = ["plan", "read_only", "default", "auto"];
  const PRESETS = ["every 1h", "every 30m", "daily 08:30", "weekdays 08:30", "mon,wed,fri 18:15"];
  const POLL_MS = 5000;
  const S = { jobs: [], running: "", queued: [], selected: "", runs: [], armed: "", chat: [], busy: false, ready: false };
  let timer = 0, armTimer = 0, form = null, msgEl, statusEl;

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick, tip) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; if (tip) b.title = tip; return b; };
  const errText = r => (r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`;

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const say = (text, bad) => { statusEl.textContent = text; statusEl.classList.toggle("bad", !!bad); };
  const when = ts => (ts ? new Date(ts * 1000).toLocaleString(LANG === "it" ? "it-IT" : "en-GB", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "-");
  const job = id => S.jobs.find(j => j.id === id);

  /* ---- the list ---- */
  function jobRow(j) {
    const li = el("li", "cj" + (j.id === S.selected ? " sel" : "") + (j.enabled ? " on" : ""));
    const open = el("button", "cj-open"); open.type = "button"; open.title = T("cj.tip.edit");
    const last = j.last_status ? `${T("cj.status." + (["done", "asked", "failed", "skipped", "cancelled", "timeout"].includes(j.last_status) ? j.last_status : "failed"))} ${when(j.last_run)}` : T("cj.never");
    const next = j.enabled && j.next_run ? T("cj.next", when(j.next_run)) : T("cj.off");
    open.append(el("span", "cj-t", j.name), el("span", "cj-s", `${j.schedule} · ${next}`), el("span", "cj-s", last));
    open.onclick = () => select(j.id);
    const acts = el("div", "cj-acts");
    const armed = S.armed === j.id;
    acts.append(
      btn(j.enabled ? T("cj.turnoff") : T("cj.turnon"), j.enabled ? "" : "primary", () => toggle(j), j.enabled ? T("cj.tip.off") : T("cj.tip.on")),
      btn(T("cj.run"), "", () => runNow(j), T("cj.tip.run")),
      btn(armed ? T("cj.del.sure") : T("cj.del"), armed ? "cj-sure" : "ghost", () => remove(j), armed ? T("cj.tip.delsure") : T("cj.tip.del")));
    li.append(open, acts);
    return li;
  }
  function paintList() {
    const list = document.getElementById("cj-list");
    list.replaceChildren(...S.jobs.map(jobRow));
    document.getElementById("cj-empty").hidden = S.jobs.length > 0;
    const run = document.getElementById("cj-running");
    run.hidden = !S.running;
    run.textContent = S.running ? T("cj.running", (job(S.running) || {}).name || S.running) : "";
  }
  async function load() {
    const r = await api("GET", "api/routines");
    if (!r.ok) { say(errText(r) || T("cj.offline"), true); return; }
    S.jobs = r.data.routines || [];
    S.running = r.data.running || "";
    S.queued = r.data.queued || [];
    paintList();
    if (S.selected && S.selected !== "new" && !job(S.selected)) { S.selected = ""; paintEditor(); }
    if (S.selected && S.selected !== "new") loadRuns();
  }
  async function loadRuns() {
    const r = await api("GET", `api/routines/runs?id=${encodeURIComponent(S.selected)}&limit=8`);
    S.runs = r.ok ? r.data.runs || [] : [];
    paintRuns();
  }

  async function toggle(j) {
    const r = await api("POST", `api/routines/${j.id}/${j.enabled ? "disable" : "enable"}`, {});
    if (!r.ok) { say(errText(r), true); return; }
    say(T(j.enabled ? "cj.turnedoff" : "cj.turnedon", j.name));
    load();
  }
  async function runNow(j) {
    const r = await api("POST", `api/routines/${j.id}/run`, {});
    if (!r.ok) { say(errText(r), true); return; }
    say(T("cj.started", j.name));
    load();
  }
  async function remove(j) {
    if (S.armed !== j.id) { S.armed = j.id; clearTimeout(armTimer); armTimer = setTimeout(() => { S.armed = ""; paintList(); }, 4000); paintList(); return; }
    clearTimeout(armTimer); S.armed = "";
    const r = await api("POST", `api/routines/${j.id}/remove`, {});
    if (!r.ok) { say(errText(r), true); paintList(); return; }
    if (S.selected === j.id) { S.selected = ""; paintEditor(); }
    say(T("cj.deleted", j.name));
    load();
  }

  /* ---- the editor ---- */
  function field(id, label, node, help) {
    const wrap = el("div", "cj-field");
    const l = el("label", "", label); l.htmlFor = id;
    wrap.append(l, node);
    if (help) wrap.append(el("p", "help", help));
    return wrap;
  }
  function select(id) {
    S.selected = id;
    paintList(); paintEditor();
    if (id !== "new") loadRuns(); else { S.runs = []; paintRuns(); }
  }
  function paintEditor(values) {
    const host = document.getElementById("cj-editor");
    const existing = S.selected && S.selected !== "new" ? job(S.selected) : null;
    host.hidden = !S.selected;
    document.getElementById("cj-pick").hidden = !!S.selected;
    if (!S.selected) return;
    const v = values || existing || { name: "", prompt: "", workspace: defaultFolder(), schedule: "weekdays 08:30", mode: "read_only", allow: [], max_turns: 10 };
    const name = el("input"); name.id = "cj-name"; name.maxLength = 80; name.value = v.name || ""; name.autocomplete = "off";
    const prompt = el("textarea"); prompt.id = "cj-prompt"; prompt.rows = 6; prompt.maxLength = 4000; prompt.value = v.prompt || "";
    const folder = el("input"); folder.id = "cj-folder"; folder.setAttribute("list", "cj-folders"); folder.value = v.workspace || ""; folder.autocomplete = "off"; folder.spellcheck = false;
    const folders = document.getElementById("cj-folders");
    folders.replaceChildren(...((window.agentProjects && window.agentProjects.list()) || []).map(p => { const o = el("option"); o.value = p.path; return o; }));
    const schedule = el("input"); schedule.id = "cj-schedule"; schedule.value = v.schedule || ""; schedule.autocomplete = "off"; schedule.spellcheck = false;
    const presets = el("div", "cj-presets"); presets.append(...PRESETS.map(p => btn(p, "ghost", () => { schedule.value = p; schedule.focus(); })));
    const steps = el("input"); steps.id = "cj-steps"; steps.type = "number"; steps.min = "1"; steps.max = "50"; steps.value = String(v.max_turns || 10);
    const allow = el("textarea"); allow.id = "cj-allow"; allow.rows = 3; allow.spellcheck = false; allow.value = (v.allow || []).join("\n");
    const modes = el("fieldset", "cj-modes"); modes.append(el("legend", "", T("cj.mode")));
    const risk = el("div", "cj-risk");
    MODES.forEach(m => {
      const lab = el("label", "cj-mode"), r = el("input"); r.type = "radio"; r.name = "cj-mode"; r.value = m; r.checked = (v.mode || "read_only") === m;
      r.onchange = () => paintRisk();
      const t = el("span"); t.append(el("b", "", T("ag.mode." + m)), el("small", "", T("ag.mode." + m + ".d")));
      lab.append(r, t); modes.append(lab);
    });
    function paintRisk() { risk.replaceChildren(...(modeNow() === "auto" ? [riskLabel(T("cj.risk"))] : [])); }
    const modeNow = () => (modes.querySelector("input:checked") || {}).value || "read_only";
    paintRisk();
    const save = btn(T("cj.save"), "primary", () => saveJob(existing), T("cj.tip.save"));
    const msg = el("p", "cj-msg"); msg.id = "cj-msg"; msg.setAttribute("role", "status");
    form = { name, prompt, folder, schedule, steps, allow, modeNow, msg };
    host.replaceChildren(
      el("div", "sh", ""),
      (() => { const f = el("form", "pad cj-form"); f.noValidate = true; f.onsubmit = e => { e.preventDefault(); saveJob(existing); };
        f.append(field("cj-name", T("cj.name"), name), field("cj-prompt", T("cj.prompt"), prompt, T("cj.prompt.help")),
          field("cj-folder", T("cj.folder"), folder, T("cj.folder.help")),
          field("cj-schedule", T("cj.schedule"), schedule, T("cj.schedule.help")), presets, modes, risk,
          field("cj-steps", T("cj.steps"), steps), field("cj-allow", T("cj.allow"), allow, T("cj.allow.help")),
          existing ? el("p", "help", T(existing.enabled ? "cj.is.on" : "cj.is.off")) : el("p", "help", T("cj.new.off")), msg, el("div", "cj-buttons", save)); return f; })());
    host.firstChild.append(el("b", "", existing ? T("cj.edit", existing.name) : T("cj.new")));
    document.getElementById("cj-runs").hidden = !existing;
  }
  const defaultFolder = () => { const p = window.agentProjects && window.agentProjects.list()[0]; return p ? p.path : ""; };
  async function saveJob(existing) {
    const f = form;
    const body = { name: f.name.value.trim(), prompt: f.prompt.value.trim(), workspace: f.folder.value.trim(), schedule: f.schedule.value.trim(), mode: f.modeNow(),
      allow: f.allow.value.split("\n").map(s => s.trim()).filter(Boolean), max_turns: Number(f.steps.value) || 10 };
    if (body.mode === "auto") {
      if (!(await riskConfirm([T("cj.risk")]))) return;
      body.confirm_auto = true;
    }
    const r = existing ? await api("POST", `api/routines/${existing.id}/update`, body) : await api("POST", "api/routines", body);
    if (!r.ok) { f.msg.textContent = errText(r); f.msg.classList.add("bad"); return; }
    say(T(existing ? "cj.saved" : "cj.created", r.data.routine.name));
    S.selected = r.data.routine.id;
    await load();
    paintEditor();
    loadRuns();
  }
  function paintRuns() {
    const list = document.getElementById("cj-runs-list");
    document.getElementById("cj-runs-none").hidden = S.runs.length > 0;
    list.replaceChildren(...S.runs.map(r => {
      const li = el("li", "cj-run " + r.status);
      li.append(el("b", "", T("cj.status." + (["done", "asked", "failed", "skipped", "cancelled", "timeout"].includes(r.status) ? r.status : "failed"))), el("span", "", ` · ${when(r.started)}${r.seconds ? ` · ${Math.round(r.seconds)} s` : ""}`));
      if (r.summary) li.append(el("p", "cj-sum", r.summary.slice(0, 600)));
      return li;
    }));
  }

  /* ---- the small chat: it talks a job over and may propose one; the proposal only fills the editor ---- */
  function system() {
    const jobs = S.jobs.map(j => `- id ${j.id}: "${j.name}" | ${j.schedule} | ${j.mode} | ${j.enabled ? "on" : "off"} | folder ${j.workspace} | task: ${String(j.prompt).replace(/\s+/g, " ").slice(0, 160)}`).join("\n") || "(none yet)";
    const folders = ((window.agentProjects && window.agentProjects.list()) || []).map(p => p.path).join("\n") || "(none added)";
    return ["You help the person plan the recurring jobs of their local agent. Answer briefly, in the person's language.",
      "A job has: name (at most 80 characters), prompt (what the agent does each time, written as an instruction), workspace (the full path of an existing folder),",
      "schedule, mode and max_turns. Schedules are only `every 30m`, `every 2h`, `every 1d` (at least a minute) or fixed local times: `daily 08:30`, `weekdays 08:30`,",
      "`weekends 10:00`, `mon,wed,fri 18:15`. There is no cron syntax: if the person gives one, convert it. Modes: plan, read_only (the default), default (edits files, asks",
      "before commands) and auto (runs commands without asking: suggest it only if the person asks for it). A job runs by itself, so its prompt must not need an answer",
      "from the person. When you propose a new job or a change, write it in one fenced block with the language job holding JSON with those fields (add \"id\" with the id of the",
      "existing job when you change one) and say what it does in a sentence; the person reviews it in the editor and decides. Never say that a job was saved or turned on.",
      "", "Folders the person added:", folders, "", "Current jobs:", jobs].join("\n");
  }
  function proposalOf(text) {
    const m = /```job\s*\n([\s\S]*?)```/.exec(text);
    if (!m) return null;
    try {
      const p = JSON.parse(m[1]);
      if (!p || typeof p !== "object" || Array.isArray(p)) return null;
      return { id: typeof p.id === "string" ? p.id : "", name: String(p.name || "").slice(0, 80), prompt: String(p.prompt || "").slice(0, 4000), workspace: String(p.workspace || ""),
        schedule: String(p.schedule || ""), mode: MODES.includes(p.mode) ? p.mode : "read_only", allow: Array.isArray(p.allow) ? p.allow.filter(x => typeof x === "string").slice(0, 10) : [],
        max_turns: Number.isInteger(p.max_turns) ? p.max_turns : 10 };
    } catch (e) { return null; }
  }
  function use(p) {
    const existing = p.id && job(p.id);
    S.selected = existing ? existing.id : "new";
    paintList();
    paintEditor({ ...(existing || {}), ...p, workspace: p.workspace || (existing && existing.workspace) || defaultFolder() });
    document.getElementById("cj-name").focus();
    say(T("cj.proposal.used"));
  }
  function paintChat() {
    msgEl.replaceChildren(...S.chat.map(m => {
      const d = el("div", "cjm " + m.role);
      d.append(el("div", "cjm-t", m.text.replace(/```job[\s\S]*?```/g, "").trim() || (m.proposal ? T("cj.chat.proposal") : "")));
      if (m.proposal) {
        const card = el("div", "cj-prop");
        card.append(el("b", "", m.proposal.name || T("cj.new")), el("span", "", ` · ${m.proposal.schedule} · ${T("ag.mode." + m.proposal.mode)}`),
          el("p", "cj-sum", m.proposal.prompt.slice(0, 240)), btn(T("cj.proposal.use"), "primary", () => use(m.proposal), T("cj.tip.use")));
        d.append(card);
      }
      return d;
    }));
    document.getElementById("cj-chat-empty").hidden = S.chat.length > 0;
    msgEl.scrollTop = msgEl.scrollHeight;
  }
  async function ask(text) {
    if (!text || S.busy) return;
    S.chat.push({ role: "user", text });
    S.busy = true; paintChat(); setBusy(true);
    const messages = [{ role: "system", content: system() }, ...S.chat.slice(-12).map(m => ({ role: m.role === "user" ? "user" : "assistant", content: m.text }))];
    let reply = "";
    try {
      const r = await fetch("v1/chat/completions", { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify({ messages, stream: false, max_tokens: 1500, temperature: 0.3 }) });
      const j = await r.json();
      reply = r.ok ? ((j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content) || "") : ((j.error && j.error.message) || `HTTP ${r.status}`);
      if (!r.ok) reply = T("cj.chat.fail", reply);
    } catch (e) { reply = T("cj.chat.fail", String(e.message || e)); }
    S.chat.push({ role: "assistant", text: reply, proposal: proposalOf(reply) });
    S.busy = false; setBusy(false); paintChat();
  }
  function setBusy(on) { document.getElementById("cj-chat-send").disabled = on; document.getElementById("cj-chat-send").textContent = on ? T("cj.chat.thinking") : T("cj.chat.send"); }

  /* ---- the page ---- */
  function build() {
    statusEl = document.getElementById("cj-status");
    msgEl = document.getElementById("cj-chat-msgs");
    document.getElementById("cj-new").onclick = () => select("new");
    const input = document.getElementById("cj-chat-input");
    document.getElementById("cj-chat-form").onsubmit = e => { e.preventDefault(); const t = input.value.trim(); input.value = ""; ask(t); };
    input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); document.getElementById("cj-chat-form").requestSubmit(); } });
    document.getElementById("cj-chat-clear").onclick = () => { S.chat = []; paintChat(); };
    applyT(root);
    paintChat();
  }
  function schedule() {
    clearTimeout(timer);
    if (document.body.dataset.view !== "cron" || document.hidden) return;
    timer = setTimeout(async () => { if (!(form && root.contains(document.activeElement) && document.activeElement.matches("input, textarea"))) await load(); schedule(); }, POLL_MS);
  }
  async function enter() {
    if (!S.ready) { build(); S.ready = true; }
    if (window.agentProjects) await window.agentProjects.load();
    await load();
    schedule();
  }
  document.addEventListener("basalt:view", e => { if (e.detail === "cron") enter(); else clearTimeout(timer); });
  document.addEventListener("visibilitychange", schedule);
  if (document.body.dataset.view === "cron") enter();
})();
