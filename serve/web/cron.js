"use strict";
/* Cron Jobs: the agent's recurring tasks (the routines of the server), with a list, an editor, the latest results of a job, and a small
   chat with the model to talk a job over. The chat only proposes: a proposal fills the editor and nothing is saved until the
   person presses Save. Everything the server or the model wrote enters the page as text. */
(function () {
  const root = document.getElementById("cron-root");
  if (!root) return;

  const PRESETS = ["every 1h", "every 30m", "daily 08:30", "weekdays 08:30", "mon,wed,fri 18:15"];
  const POLL_MS = 5000;
  const STATUSES = ["done", "parked", "failed", "cancelled", "timeout", "yielded", "skipped"];
  const S = { jobs: [], running: [], queued: [], pending: [], selected: "", runs: [], armed: "", chat: [], busy: false, ready: false, skills: [], stopped: false, improve: null };
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
    const last = j.last_status ? `${T("cj.status." + (STATUSES.includes(j.last_status) ? j.last_status : "failed"))} ${when(j.last_run)}` : T("cj.never");
    const next = j.enabled && j.next_run ? T("cj.next", when(j.next_run)) : T("cj.off");
    open.append(el("span", "cj-t", j.name), el("span", "cj-s", `${j.schedule} · ${next}`), el("span", "cj-s", last));
    if (j.skill) open.append(el("span", "cj-s", T("cj.skillrow", j.skill)));
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
    run.hidden = !S.running.length;
    run.textContent = S.running.length ? T("cj.running", S.running.map(id => (job(id) || {}).name || id).join(", ")) : "";
    paintPending();
  }
  function paintPending() {
    const box = document.getElementById("cj-pending"), list = document.getElementById("cj-pending-list");
    box.hidden = !S.pending.length;
    list.replaceChildren(...S.pending.map(p => {
      const li = el("li", "cj-pend");
      li.append(el("b", "", p.name || p.routine), el("span", "", ` · ${when(p.created)}`), el("p", "cj-sum", String(p.question || "").slice(0, 600)));
      if ((p.options || []).length) li.append(el("p", "help", T("cj.pending.options", p.options.join(" / "))));
      li.append(btn(T("cj.pending.dismiss"), "ghost", () => dismiss(p), T("cj.tip.dismiss")));
      return li;
    }));
  }
  async function dismiss(p) {
    const r = await api("POST", `api/routines/${p.id}/dismiss`, {});
    if (!r.ok) { say(errText(r), true); return; }
    say(T("cj.pending.dismissed"));
    load();
  }

  /* ---- messages a schedule sent that never reached their target (serve/deliveries.py) ---- */
  function deadRow(d) {
    const li = el("li", "cj-pend");
    li.append(el("b", "", d.target), el("span", "", ` · ${when(d.created)}`),
             el("p", "cj-sum", T("cj.dead.reason." + (["unreachable", "busy_timeout", "loop", "budget"].includes(d.reason) ? d.reason : "unreachable"))),
             el("p", "cj-sum", String(d.message || "").slice(0, 300)));
    const row = el("div", "cj-acts");
    row.append(btn(T("cj.dead.retry"), "primary", () => retryDead(d), T("cj.dead.retry")), btn(T("cj.pending.dismiss"), "ghost", () => dismissDead(d), T("cj.tip.dismiss")));
    li.append(row);
    return li;
  }
  async function loadDead() {
    const r = await api("GET", "api/agent/deliveries?state=dead");
    const box = document.getElementById("cj-dead"), list = document.getElementById("cj-dead-list");
    const items = (r.ok ? r.data.deliveries || [] : []).filter(d => /^(cron|plan):/.test(d.provenance) || d.provenance === "cron");
    box.hidden = !items.length;
    list.replaceChildren(...items.map(deadRow));
  }
  async function retryDead(d) {
    const r = await api("POST", `api/agent/deliveries/${d.id}/retry`, {});
    if (!r.ok) { say(errText(r), true); return; }
    say(T("cj.dead.retried"));
    loadDead();
  }
  async function dismissDead(d) {
    const r = await api("POST", `api/agent/deliveries/${d.id}/dismiss`, {});
    if (!r.ok) { say(errText(r), true); return; }
    loadDead();
  }
  async function loadSkills() {
    const r = await api("GET", "api/personal-skills");
    S.skills = r.ok ? r.data.skills || [] : [];
  }
  async function load() {
    const r = await api("GET", "api/routines");
    if (!r.ok) { say(errText(r) || T("cj.offline"), true); return; }
    S.jobs = r.data.routines || [];
    S.running = r.data.running || [];
    S.pending = r.data.pending || [];
    S.queued = r.data.queued || [];
    S.stopped = !!r.data.stopped;
    paintStop();
    paintList();
    loadDead();
    if (S.selected && S.selected !== "new" && !job(S.selected)) { S.selected = ""; paintEditor(); }
    if (S.selected && S.selected !== "new") loadRuns();
  }
  async function setStop() {
    const r = await api("POST", "api/routines/stop", { on: !S.stopped });
    if (!r.ok) { say(errText(r), true); return; }
    S.stopped = !!r.data.stopped;
    paintStop();
    say(T(S.stopped ? "cj.stop.on" : "cj.stop.off"));
  }
  function paintStop() {
    const b = document.getElementById("cj-stop");
    if (!b) return;
    b.textContent = T(S.stopped ? "cj.stop.release" : "cj.stop.btn");
    b.classList.toggle("primary", S.stopped);
    b.setAttribute("aria-pressed", S.stopped ? "true" : "false");
  }
  async function improveJob(existing) {
    form.msg.textContent = T("cj.improve.working"); form.msg.classList.remove("bad");
    const r = await api("POST", `api/routines/${existing.id}/improve`, {});
    if (!r.ok) { form.msg.textContent = errText(r); form.msg.classList.add("bad"); return; }
    S.improve = r.data;
    form.msg.textContent = "";
    paintImprove(existing);
  }
  function paintImprove(existing) {
    const box = document.getElementById("cj-improve");
    if (!box) return;
    const p = S.improve;
    if (!p) { box.hidden = true; return; }
    box.hidden = false;
    const code = el("pre", "cj-code"); code.textContent = p.script;
    box.replaceChildren(el("p", "help", T("cj.improve.result", p.tool_steps, p.ai_calls)), code,
      btn(T("cj.improve.apply"), "primary", async () => {
        const a = await api("POST", `api/routines/${existing.id}/improve_apply`, p);
        if (!a.ok) { say(errText(a), true); return; }
        S.improve = null; say(T("cj.improve.done", a.data.skill)); await load(); paintEditor();
      }, T("cj.improve.tip")), btn(T("cj.improve.discard"), "ghost", () => { S.improve = null; paintImprove(existing); }));
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
    const v = values || existing || { name: "", prompt: "", workspace: defaultFolder(), schedule: "weekdays 08:30", max_turns: null, max_minutes: null };
    const name = el("input"); name.id = "cj-name"; name.maxLength = 80; name.value = v.name || ""; name.autocomplete = "off";
    const prompt = el("textarea"); prompt.id = "cj-prompt"; prompt.rows = 6; prompt.maxLength = 4000; prompt.value = v.prompt || "";
    const folder = el("input"); folder.id = "cj-folder"; folder.setAttribute("list", "cj-folders"); folder.value = v.workspace || ""; folder.autocomplete = "off"; folder.spellcheck = false;
    const folders = document.getElementById("cj-folders");
    folders.replaceChildren(...((window.agentProjects && window.agentProjects.list()) || []).map(p => { const o = el("option"); o.value = p.path; return o; }));
    const schedule = el("input"); schedule.id = "cj-schedule"; schedule.value = v.schedule || ""; schedule.autocomplete = "off"; schedule.spellcheck = false;
    const presets = el("div", "cj-presets"); presets.append(...PRESETS.map(p => btn(p, "ghost", () => { schedule.value = p; schedule.focus(); })));
    const steps = el("input"); steps.id = "cj-steps"; steps.type = "number"; steps.min = "1"; steps.inputMode = "numeric"; steps.placeholder = T("cj.limit.none"); steps.value = Number.isInteger(v.max_turns) ? String(v.max_turns) : "";
    const minutes = el("input"); minutes.id = "cj-minutes"; minutes.type = "number"; minutes.min = "1"; minutes.inputMode = "numeric"; minutes.placeholder = T("cj.limit.none"); minutes.value = Number.isInteger(v.max_minutes) ? String(v.max_minutes) : "";
    const yieldS = el("input"); yieldS.id = "cj-yield"; yieldS.type = "number"; yieldS.min = "0"; yieldS.max = "3600"; yieldS.inputMode = "numeric"; yieldS.placeholder = T("cj.yield.never");
    yieldS.value = Number.isInteger(v.yield_s) ? String(v.yield_s) : "";
    const check = el("input"); check.id = "cj-check"; check.value = v.check || ""; check.maxLength = 1000; check.autocomplete = "off"; check.spellcheck = false;
    const indexPast = el("input"); indexPast.id = "cj-index"; indexPast.type = "checkbox"; indexPast.checked = v.index_past === true;
    // what it runs: a task written here, or one of the person's personal skills with its values
    const kinds = el("fieldset", "cj-modes"); kinds.append(el("legend", "", T("cj.kind")));
    ["task", "skill"].forEach(k => {
      const lab = el("label", "cj-mode"), r = el("input"); r.type = "radio"; r.name = "cj-kind"; r.value = k; r.checked = (v.skill ? "skill" : "task") === k;
      r.disabled = k === "skill" && !S.skills.length && !v.skill;
      r.onchange = () => { paintKind(); };
      const t = el("span"); t.append(el("b", "", T("cj.kind." + k)), el("small", "", T("cj.kind." + k + ".d")));
      lab.append(r, t); kinds.append(lab);
    });
    const skillSel = el("select"); skillSel.id = "cj-skill";
    const approved = S.skills.filter(s => s.status === "approved" || s.slug === v.skill);
    skillSel.append(...approved.map(s => { const o = el("option", "", s.name); o.value = s.slug; return o; }));
    if (v.skill) skillSel.value = v.skill;
    const valuesBox = el("div", "cj-values");
    let fields = [];
    const kindNow = () => (kinds.querySelector("input:checked") || {}).value || "task";
    function drawValues() {
      const skill = S.skills.find(s => s.slug === skillSel.value);
      fields = ((skill && skill.params) || []).map(p => ({ spec: p, ...window.pskills.valueField(p, (v.values || {})[p.name]) }));
      valuesBox.replaceChildren(...fields.map(f => f.el), ...(fields.length ? [el("p", "help", T("cj.values.help"))] : [el("p", "help", T("cj.values.none"))]));
    }
    skillSel.onchange = drawValues;
    const skillWrap = field("cj-skill", T("cj.skill"), skillSel);
    function paintKind() {
      const skill = kindNow() === "skill";
      skillWrap.hidden = !skill; valuesBox.hidden = !skill; promptWrap.hidden = skill;
      if (skill) drawValues();
    }
    const save = btn(T("cj.save"), "primary", () => saveJob(existing), T("cj.tip.save"));
    const msg = el("p", "cj-msg"); msg.id = "cj-msg"; msg.setAttribute("role", "status");
    const promptWrap = field("cj-prompt", T("cj.prompt"), prompt, T("cj.prompt.help"));
    const valuesNow = () => { const out = {}; fields.forEach(f => { const x = f.get(); if (String(x).trim() !== "") out[f.spec.name] = x; }); return out; };
    form = { name, prompt, folder, schedule, steps, minutes, yieldS, check, indexPast, msg, kindNow, skillSel, valuesNow };
    host.replaceChildren(
      el("div", "sh", ""),
      (() => { const f = el("form", "pad cj-form"); f.noValidate = true; f.onsubmit = e => { e.preventDefault(); saveJob(existing); };
        f.append(field("cj-name", T("cj.name"), name), kinds, skillWrap, valuesBox, promptWrap,
          field("cj-folder", T("cj.folder"), folder, T("cj.folder.help")),
          field("cj-schedule", T("cj.schedule"), schedule, T("cj.schedule.help")), presets, el("p", "help", T("cj.auto.help")),
          field("cj-steps", T("cj.steps"), steps, T("cj.steps.help")), field("cj-minutes", T("cj.minutes"), minutes, T("cj.minutes.help")), field("cj-yield", T("cj.yield"), yieldS, T("cj.yield.help")),
          field("cj-check", T("cj.check"), check, T("cj.check.help")),
          field("cj-index", T("cj.index"), indexPast, T("cj.index.help")),
          el("p", "help", T(existing ? (existing.enabled ? "cj.is.on" : "cj.is.off") : "cj.new.on")), msg,
          (() => { const row = el("div", "cj-buttons"); row.append(save, ...(existing ? [btn(T("cj.improve"), "ghost", () => improveJob(existing), T("cj.improve.help"))] : [])); return row; })(),
          (() => { const d = el("div", "cj-improve"); d.id = "cj-improve"; d.hidden = true; return d; })()); return f; })());
    host.firstChild.append(el("b", "", existing ? T("cj.edit", existing.name) : T("cj.new")));
    paintKind();
    document.getElementById("cj-runs").hidden = !existing;
    S.improve = null;
  }
  const defaultFolder = () => { const p = window.agentProjects && window.agentProjects.list()[0]; return p ? p.path : ""; };
  async function saveJob(existing) {
    const f = form;
    const skill = f.kindNow() === "skill";
    const body = { name: f.name.value.trim(), prompt: skill ? "" : f.prompt.value.trim(), workspace: f.folder.value.trim(), schedule: f.schedule.value.trim(),
      max_turns: f.steps.value.trim() === "" ? null : Number(f.steps.value), max_minutes: f.minutes.value.trim() === "" ? null : Number(f.minutes.value),
      yield_s: f.yieldS.value.trim() === "" ? null : Number(f.yieldS.value), index_past: f.indexPast.checked, check: f.check.value.trim(),
      skill: skill ? f.skillSel.value : "", values: skill ? f.valuesNow() : {} };
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
      li.append(el("b", "", T("cj.status." + (STATUSES.includes(r.status) ? r.status : "failed"))), el("span", "", ` · ${when(r.started)}${r.seconds ? ` · ${Math.round(r.seconds)} s` : ""}`));
      if (r.summary) li.append(el("p", "cj-sum", r.summary.slice(0, 600)));
      return li;
    }));
  }

  /* ---- the small chat: it talks a job over and may propose one; the proposal only fills the editor ---- */
  function system() {
    const jobs = S.jobs.map(j => `- id ${j.id}: "${j.name}" | ${j.schedule} | ${j.enabled ? "on" : "off"} | folder ${j.workspace} | task: ${String(j.prompt).replace(/\s+/g, " ").slice(0, 160)}`).join("\n") || "(none yet)";
    const folders = ((window.agentProjects && window.agentProjects.list()) || []).map(p => p.path).join("\n") || "(none added)";
    const skills = S.skills.filter(s => s.status === "approved").map(s => `- ${s.slug}: ${(s.params || []).map(p => `${p.name} (${p.type})`).join(", ") || "no parameters"}`).join("\n") || "(none)";
    return ["You help the person plan the recurring jobs of their local agent. Answer briefly, in the person's language.",
      "A job has: name (at most 80 characters), prompt (what the agent does each time, written as an instruction), workspace (the full path of an existing folder),",
      "schedule, and the optional limits max_turns (steps) and max_minutes (time of a run; leave them out for none). Schedules are only `every 30s`, `every 30m`, `every 2h`, `every 1d` or fixed local times: `daily 08:30`, `weekdays 08:30`,",
      "`weekends 10:00`, `mon,wed,fri 18:15`. There is no cron syntax: if the person gives one, convert it. A job runs by itself, in Automatic, and keeps going",
      "when the person is away: write its prompt so that it does not wait for an answer. When you propose a new job or a change, write it in one fenced block with the language job holding JSON with those fields (add \"id\" with the id of the",
      "existing job when you change one) and say what it does in a sentence; the person reviews it in the editor and decides. Never say that a job was saved or is running.",
      "A job may instead run one of the person's personal skills: add \"skill\" (its slug) and \"values\" (an object of name: text) and leave prompt empty. Dates in values may be",
      "words such as today, tomorrow or in 3 days: they are worked out each time the job runs. Use only the skills listed here.",
      "", "Folders the person added:", folders, "", "Personal skills (slug: parameters):", skills, "", "Current jobs:", jobs].join("\n");
  }
  function proposalOf(text) {
    const m = /```job\s*\n([\s\S]*?)```/.exec(text);
    if (!m) return null;
    try {
      const p = JSON.parse(m[1]);
      if (!p || typeof p !== "object" || Array.isArray(p)) return null;
      return { skill: typeof p.skill === "string" ? p.skill : "", values: p.values && typeof p.values === "object" && !Array.isArray(p.values) ? p.values : {},
        id: typeof p.id === "string" ? p.id : "", name: String(p.name || "").slice(0, 80), prompt: String(p.prompt || "").slice(0, 4000), workspace: String(p.workspace || ""),
        schedule: String(p.schedule || ""),
        max_turns: Number.isInteger(p.max_turns) && p.max_turns >= 1 ? p.max_turns : null, max_minutes: Number.isInteger(p.max_minutes) && p.max_minutes >= 1 ? p.max_minutes : null };
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
        card.append(el("b", "", m.proposal.name || T("cj.new")), el("span", "", ` · ${m.proposal.schedule}`),
          el("p", "cj-sum", m.proposal.skill ? T("cj.skillrow", m.proposal.skill) : m.proposal.prompt.slice(0, 240)), btn(T("cj.proposal.use"), "primary", () => use(m.proposal), T("cj.tip.use")));
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
    SessChat.mount(document.getElementById("cj-instr-root"), {
      id: "cj-instr", kind: "cron", emptyKey: "sc.empty.cron", placeholderKey: "sc.ph.cron", labelKey: "sc.title.cron",
      target: () => { const j = S.selected && S.selected !== "new" ? job(S.selected) : null; return j ? `${j.name} (id ${j.id})` : ""; },
      onDone: () => document.dispatchEvent(new CustomEvent("tadamoo:edited")) });
    document.getElementById("cj-stop").onclick = setStop;
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
    await loadSkills();
    await load();
    schedule();
  }
  document.addEventListener("tadamoo:view", e => { if (e.detail === "cron") enter(); else clearTimeout(timer); });
  document.addEventListener("visibilitychange", schedule);
  document.addEventListener("tadamoo:edited", () => { if (S.ready) load(); });
  if (document.body.dataset.view === "cron") enter();
})();
