"use strict";
/* Settings > Benchmarks: public benchmarks run against the harness with the model this server has loaded. The list with its status and checks, the
   import (with a confirmation), the start form, live progress, the failure report, the rank preview and the two-step publish desk. Nothing here
   reaches the network: the server imports the benchmark; the board's results file is pasted by the person. Everything shown enters the page as text. */
(function () {
  const host = document.getElementById("bn-root");
  if (!host) return;

  const POLL_MS = 2000, ARM_MS = 4000, RUNS_SHOWN = 8, TRIALS_MAX = 20, ATTEMPTS_MAX = 100000;
  const CLASSES = ["environment_problem", "context_overflow", "timeout", "missing_executable", "blocked_by_site", "permission_refused", "tool_format_error", "loop",
    "judge_disagreement", "bad_plan", "wrong_final_state", "unknown"];
  const SCORING = { "verifier": "bn.scoring.verifier", "judge": "bn.scoring.judge", "verifier+judge": "bn.scoring.both" };
  const FIELDS = /^(INPUT|TEXTAREA|SELECT)$/;
  let S = null, note = "", sel = "", pre = {}, pending = "", armed = "", armTimer = 0, timer = 0, visible = false, started = {};
  let runId = "", R = null, rep = null, rank = null, boardText = "", boardAddr = "", lessonNote = {}, pack = null, desk = null, typed = "", deskNote = "", rankNote = "";
  const form = { subset: {}, trials: {}, attempts: {}, reasoning: {}, tasks: {}, power: "" };

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick, disabled) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; if (disabled) b.disabled = true; return b; };
  const errText = r => (r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`;
  const tx = (key, fallback) => { const s = T(key); return s === key ? fallback : s; };
  const when = iso => (iso || "").replace("T", " ").slice(0, 16);
  const spec = id => (S && S.benchmarks.find(b => b.id === id)) || null;

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const arm = key => { armed = key; clearTimeout(armTimer); armTimer = setTimeout(() => { armed = ""; paintList(); }, ARM_MS); paintList(); };

  /* ---- loading and polling ---- */
  async function load() {
    const r = await api("GET", "api/bench");
    if (!r.ok) { note = errText(r); S = null; paintAll(); return; }
    note = ""; S = r.data;
    Object.entries(S.jobs).forEach(([id, j]) => {
      const key = id + ":" + j.started;
      if (j.status !== "running" && !started[key]) { started[key] = true; if (j.action === "start" && j.run_id) openRun(j.run_id); if (j.action !== "start") delete pre[id]; }
    });
    paintAll();
    schedule();
  }
  const active = () => S && (Object.values(S.jobs).some(j => j.status === "running") || S.runs.some(r => r.status === "running" || r.status === "queued"));
  function schedule() { clearTimeout(timer); if (visible && active()) timer = setTimeout(tick, POLL_MS); }
  async function tick() { await load(); if (runId) await loadRun(); schedule(); }

  async function loadRun() {
    const r = await api("GET", "api/bench/run?id=" + encodeURIComponent(runId));
    if (!r.ok) { R = null; paintRun(); return; }
    const finished = ["done", "stopped", "failed"].includes(r.data.status);
    const changed = !R || R.status !== r.data.status;
    R = r.data;
    if (finished && (changed || !rep)) await loadResults();
    paintRun(); paintResults(); paintDesk();
  }
  async function loadResults() {
    const rr = await api("GET", "api/bench/report?id=" + encodeURIComponent(runId));
    rep = rr.ok ? rr.data : null;
    rank = null;
    if (R && R.status === "done") { const k = await api("POST", "api/bench/ranking", { id: runId, leaderboard_text: boardText || undefined }); rank = k.ok ? k.data : null; rankNote = k.ok ? "" : errText(k); }
  }
  async function openRun(id) {
    runId = id; R = null; rep = null; rank = null; pack = null; desk = null; typed = ""; deskNote = ""; lessonNote = {}; rankNote = "";
    paintRun(); paintResults(); paintDesk();
    await loadRun();
    schedule();
  }

  /* ---- the model and the machine ---- */
  function paintTop() {
    const box = hostTop;
    if (guard(box)) return;
    const m = S && S.model;
    const rows = [el("p", "bn-intro", T("bn.intro"))];
    if (note) rows.push(el("p", "bn-bad", note));
    const card = el("div", "bn-block");
    card.append(el("h3", "", T("bn.model.title")));
    if (!S) { card.append(el("p", "dim", T("bn.loading"))); }
    else {
      card.append(el("p", "bn-line", m ? T("bn.model.line", m.name, m.quantisation || "?") : T("bn.model.none")));
      if (m) card.append(el("p", "bn-line dim", T("bn.model.file", m.file || "?", m.endpoint)));
      card.append(el("p", "bn-line dim", T("bn.model.only")));
      if (S.power_limit_w != null) card.append(el("p", "bn-line", T(S.power_limit_measured ? "bn.power.measured" : "bn.power.typed", nfmt(S.power_limit_w))));
      if (S.power_limit_w == null || !S.power_limit_measured) card.append(powerField());
      card.append(el("p", "bn-line " + (S.gpu_busy ? "bn-warn" : "dim"), S.gpu_busy ? T("bn.gpu.busy", S.gpu_busy) : T("bn.gpu.free")));
    }
    rows.push(card);
    box.replaceChildren(...rows);
  }
  function powerField() {
    const wrap = el("div", "bn-field"), lab = el("label", "", T("bn.power.label")), inp = el("input");
    inp.id = "bn-power"; inp.type = "number"; inp.min = "50"; inp.max = "1500"; inp.step = "1"; inp.inputMode = "numeric"; inp.value = form.power; lab.htmlFor = "bn-power";
    inp.oninput = () => { form.power = inp.value; };
    wrap.append(lab, inp, el("small", "dim", T("bn.power.help")));
    return wrap;
  }

  /* ---- the list ---- */
  const needs = b => [b.needs.docker && T("bn.need.docker"), b.needs.wsl2 && T("bn.need.wsl2"), b.needs.internet && T("bn.need.internet"), T("bn.need.git")].filter(Boolean).join(" · ");
  function statusLine(b) {
    const s = b.status;
    return s.installed ? T("bn.status.in", String(s.commit || "").slice(0, 10), when(s.imported_at), nfmt(s.size_bytes / 1048576)) : T("bn.status.out");
  }
  function card(b) {
    const li = el("li", "bn-card" + (b.id === sel ? " open" : "")), job = S.jobs[b.id];
    li.append(el("div", "bn-title", b.title), el("p", "bn-line", statusLine(b)), el("p", "bn-line dim", `${T("bn.licence")}: ${b.licence} · ${T("bn.needs")}: ${needs(b)}`));
    li.append(el("p", "bn-line dim", `${tx(SCORING[b.scoring], b.scoring)} · ${T("bn.board." + b.leaderboard)}`));
    if (!b.comparable_with_board) li.append(el("p", "bn-line bn-warn", T("bn.provisional.note")));
    if (!b.adapter.available) li.append(el("p", "bn-line bn-warn", T("bn.adapter.none")));
    if (job) li.append(el("p", "bn-line " + (job.status === "failed" ? "bn-bad" : "dim"), T("bn.job." + job.action + "." + job.status, job.error || "")));
    const bar = el("div", "bn-bar"), running = job && job.status === "running", st = b.status;
    if (!st.installed) bar.append(btn(T("bn.import"), "primary", () => { pending = b.id; paintList(); }, running));
    else {
      bar.append(btn(T("bn.update"), "", () => act("update", b.id), running));
      bar.append(btn(armed === "rm" + b.id ? T("bn.remove.sure") : T("bn.remove"), armed === "rm" + b.id ? "danger" : "ghost", () => armed === "rm" + b.id ? remove(b.id) : arm("rm" + b.id), running));
    }
    bar.append(btn(T("bn.check"), "ghost", () => check(b.id)), btn(b.id === sel ? T("bn.close") : T("bn.open"), "ghost", () => { sel = b.id === sel ? "" : b.id; paintList(); }));
    li.append(bar);
    if (pending === b.id) li.append(importConfirm(b));
    if (pre[b.id]) li.append(checks(pre[b.id]));
    if (b.id === sel) li.append(detail(b));
    return li;
  }
  function importConfirm(b) {
    const box = el("div", "bn-confirm", null);
    box.append(el("p", "bn-line", T("bn.import.what", b.title)), el("p", "bn-line", T("bn.import.source", b.source)),
      el("p", "bn-line", T("bn.import.size", b.download_size)), el("p", "bn-line", T("bn.import.where", b.status.path)), el("p", "bn-line dim", T("bn.import.never")));
    if (b.to_confirm && b.to_confirm.length) box.append(el("p", "bn-line dim", T("bn.import.confirm", b.to_confirm.join(", "))));
    const bar = el("div", "bn-bar");
    bar.append(btn(T("bn.import.go"), "primary", () => { pending = ""; act("install", b.id); }), btn(T("bn.cancel"), "ghost", () => { pending = ""; paintList(); }));
    box.append(bar);
    return box;
  }
  function checks(c) {
    const box = el("div", "bn-checks"), head = el("p", "bn-line " + (c.ready ? "bn-ok" : "bn-bad"), c.ready ? T("bn.ready") : T("bn.notready"));
    const list = el("ul", "bn-checklist");
    c.checks.forEach(k => {
      const li = el("li", k.ok ? "ok" : "bad");
      li.append(el("span", "bn-mark", k.ok ? T("bn.mark.ok") : (k.required ? T("bn.mark.bad") : T("bn.mark.opt"))), el("span", "", " " + k.detail));
      if (!k.ok && k.fix) li.append(el("div", "bn-fix", T("bn.fix", k.fix)));
      list.append(li);
    });
    box.append(head, list);
    return box;
  }

  /* ---- start form ---- */
  function detail(b) {
    const box = el("div", "bn-detail");
    box.append(el("h4", "", T("bn.start.title")));
    if (b.notes) box.append(el("p", "bn-line dim", b.notes));
    const subset = el("select"), sl = el("label", "", T("bn.start.subset"));
    subset.id = "bn-subset-" + b.id; sl.htmlFor = subset.id;
    b.subsets.forEach(s => { const o = el("option", "", tx("bn.sub." + b.id + "." + s.id, s.label)); o.value = s.id; subset.append(o); });
    subset.value = form.subset[b.id] || b.subsets[0].id;
    subset.onchange = () => { form.subset[b.id] = subset.value; };
    const trials = el("input"), tl = el("label", "", T("bn.start.trials"));
    trials.id = "bn-trials-" + b.id; tl.htmlFor = trials.id; trials.type = "number"; trials.min = "1"; trials.max = String(TRIALS_MAX); trials.step = "1"; trials.inputMode = "numeric";
    trials.value = form.trials[b.id] || "1";
    trials.oninput = () => { form.trials[b.id] = trials.value; };
    const attempts = el("input"), atl = el("label", "", T("bn.start.attempts"));
    attempts.id = "bn-attempts-" + b.id; atl.htmlFor = attempts.id; attempts.type = "number"; attempts.min = "0"; attempts.max = String(ATTEMPTS_MAX); attempts.step = "1"; attempts.inputMode = "numeric";
    attempts.value = form.attempts[b.id] || "0";
    attempts.oninput = () => { form.attempts[b.id] = attempts.value; };
    const reasoning = el("select"), rl = el("label", "", T("bn.start.reasoning"));
    reasoning.id = "bn-reasoning-" + b.id; rl.htmlFor = reasoning.id;
    [["", "bn.start.reasoning.model"], ["low", "bn.start.reasoning.low"], ["medium", "bn.start.reasoning.medium"], ["high", "bn.start.reasoning.high"]].forEach(([v, k]) => { const o = el("option", "", T(k)); o.value = v; reasoning.append(o); });
    reasoning.value = form.reasoning[b.id] || "";
    reasoning.onchange = () => { form.reasoning[b.id] = reasoning.value; };
    const only = el("input"), ol = el("label", "", T("bn.start.tasks"));
    only.id = "bn-tasks-" + b.id; ol.htmlFor = only.id; only.type = "text"; only.autocomplete = "off"; only.spellcheck = false;
    only.value = form.tasks[b.id] || "";
    only.oninput = () => { form.tasks[b.id] = only.value; };
    const row1 = el("div", "bn-field"), row2 = el("div", "bn-field"), row3 = el("div", "bn-field"), row4 = el("div", "bn-field"), row5 = el("div", "bn-field");
    row5.append(ol, only, el("p", "bn-line dim", T("bn.start.tasks.help")));
    row1.append(sl, subset); row2.append(tl, trials); row3.append(atl, attempts, el("p", "bn-line dim", T("bn.start.attempts.help")));
    row4.append(rl, reasoning, el("p", "bn-line dim", T("bn.start.reasoning.help")));
    const why = startBlock(b);
    box.append(row1, row5, row2, row3, row4, el("p", "bn-line dim", S.model ? T("bn.start.model", S.model.name, S.model.quantisation || "?") : T("bn.model.none")));
    if (why) box.append(el("p", "bn-line bn-warn", why));
    box.append(btn(T("bn.start.go"), "primary", () => {
      const n = Number(form.trials[b.id] || 1), body = { id: b.id, subset: form.subset[b.id] || b.subsets[0].id, trials: n, max_failed_attempts: Number(form.attempts[b.id] || 0), reasoning: form.reasoning[b.id] || "", tasks: (form.tasks[b.id] || "").split(/[\s,;]+/).filter(Boolean) };
      if (S.power_limit_w == null || !S.power_limit_measured) if (form.power) body.power_limit_w = Number(form.power);
      act("start", b.id, body);
    }, !!why));
    return box;
  }
  function startBlock(b) {
    const job = S.jobs[b.id];
    if (!S.model) return T("bn.model.none");
    if (!b.status.installed) return T("bn.start.notinstalled");
    if (!b.adapter.available) return T("bn.adapter.none");
    if (Object.values(S.jobs).some(j => j.status === "running")) return T("bn.start.jobbusy");
    if (S.runs.some(r => r.status === "running" || r.status === "queued")) return T("bn.start.runbusy");
    if (S.gpu_busy) return T("bn.gpu.busy", S.gpu_busy);
    return job && job.status === "running" ? T("bn.start.jobbusy") : "";
  }

  async function check(id) {
    pre[id] = { ready: false, checks: [{ ok: true, required: true, detail: T("bn.checking"), fix: "" }] };
    paintList();
    const r = await api("GET", "api/bench/preflight?id=" + encodeURIComponent(id));
    if (r.ok) pre[id] = r.data; else { delete pre[id]; note = errText(r); paintTop(); }
    paintList();
  }
  async function act(action, id, body) {
    const r = await api("POST", "api/bench/" + action, body || { id });
    note = r.ok ? "" : errText(r);
    if (!r.ok && r.data && r.data.error && r.data.error.type === "power_limit_unknown") form.power = form.power || "";
    paintTop();
    await load();
  }
  async function remove(id) {
    armed = ""; delete pre[id];
    await act("remove", id);
  }

  function runsList() {
    const box = el("div", "bn-block");
    box.append(el("h3", "", T("bn.runs.title")));
    if (!S || !S.runs.length) { box.append(el("p", "dim", T("bn.runs.none"))); return box; }
    const ul = el("ul", "bn-runs");
    S.runs.slice(0, RUNS_SHOWN).forEach(r => {
      const li = el("li", "bn-run" + (r.run_id === runId ? " open" : ""));
      li.append(el("span", "bn-runname", `${(spec(r.benchmark) || { title: r.benchmark }).title} · ${when(r.created)}`),
        el("span", "bn-runstate", `${T("bn.status." + r.status)} ${r.done}/${r.total}${r.provisional ? " · " + T("bn.prov.short") : ""}`),
        btn(T("bn.open"), "ghost", () => openRun(r.run_id)));
      ul.append(li);
    });
    box.append(ul);
    return box;
  }

  /* ---- progress ---- */
  function paintRun() {
    const box = hostRun;
    if (guard(box)) return;
    if (!runId) { box.replaceChildren(); return; }
    const card = el("div", "bn-block");
    card.append(el("h3", "", T("bn.run.title")));
    if (!R) { card.append(el("p", "dim", T("bn.loading"))); box.replaceChildren(card); return; }
    const s = (spec(R.benchmark) || { title: R.benchmark });
    card.append(el("p", "bn-line", `${s.title} · ${T("bn.status." + R.status)}`));
    if (R.provisional) card.append(el("p", "bn-line bn-warn", T("bn.prov.label")));
    const bar = el("progress"); bar.max = Math.max(1, R.total || 1); bar.value = R.done || 0;
    bar.setAttribute("aria-label", T("bn.run.progress"));
    card.append(bar, el("p", "bn-line", T("bn.run.counts", R.done || 0, R.total || 0, R.passed || 0, R.failed || 0, R.invalid || 0)));
    if (R.current) card.append(el("p", "bn-line dim", T("bn.run.current", R.current)));
    card.append(el("p", "bn-line dim", T("bn.run.cost", nfmt(R.tokens || 0), nfmt(R.seconds || 0, 1), nfmt(R.power_limit_w), (R.model && R.model.name) || "?", String(R.benchmark_commit || "").slice(0, 10))));
    if (R.error) card.append(el("p", "bn-bad", R.error));
    const acts = el("div", "bn-bar");
    if (R.status === "running" || R.status === "queued") acts.append(btn(T("bn.run.stop"), "danger", async () => { await api("POST", "api/bench/stop", { id: runId }); await tick(); }));
    if (["stopped", "failed"].includes(R.status)) acts.append(btn(T("bn.run.resume"), "primary", async () => { const r = await api("POST", "api/bench/resume", { id: runId }); note = r.ok ? "" : errText(r); paintTop(); await tick(); }));
    card.append(acts);
    box.replaceChildren(card);
  }

  /* ---- results ---- */
  const rate = (k, v) => typeof v === "number" ? (/_rate$|accuracy$/.test(k) ? `${nfmt(v * 100, 1)} %` : nfmt(v, 3)) : String(v);
  function paintResults() {
    const box = hostResults;
    if (guard(box)) return;
    if (!R || !rep || !["done", "stopped", "failed"].includes(R.status)) { box.replaceChildren(); return; }
    const s = spec(R.benchmark) || { primary_metric: "" }, card = el("div", "bn-block");
    card.append(el("h3", "", T("bn.res.title")));
    if (rep.provisional) card.append(el("p", "bn-line bn-warn", T("bn.prov.label")));
    const metrics = Object.entries(rep.metrics || {});
    if (!metrics.length) card.append(el("p", "dim", T("bn.res.nometrics")));
    const dl = el("ul", "bn-metrics");
    metrics.forEach(([k, v]) => dl.append(el("li", k === s.primary_metric ? "primary" : "", `${k}: ${rate(k, v)}`)));
    card.append(dl);
    if (rank && rank.interval && rank.interval.n) card.append(el("p", "bn-line", T("bn.res.interval", nfmt(rank.interval.low * 100, 1), nfmt(rank.interval.high * 100, 1), rank.interval.n)));
    card.append(el("p", "bn-line dim", T("bn.res.counts", rep.trials, rep.passed, rep.failed, rep.invalid)));
    card.append(failureReport(rep), rankBlock(s));
    box.replaceChildren(card);
  }
  function failureReport(r) {
    const box = el("div", "bn-sub");
    box.append(el("h4", "", T("bn.rep.title")));
    const cls = Object.entries(r.classes || {});
    if (!cls.length) box.append(el("p", "dim", T("bn.rep.none")));
    else {
      const ul = el("ul", "bn-classes");
      cls.forEach(([c, n]) => ul.append(el("li", "", `${tx("bn.class." + c, c)}: ${n}`)));
      box.append(ul);
    }
    if (r.sometimes_pass.length) {
      box.append(el("h4", "", T("bn.rep.sometimes")));
      const ul = el("ul", "bn-classes");
      r.sometimes_pass.forEach(t => ul.append(el("li", "", T("bn.rep.sometimes.line", t.task, t.passed, t.of))));
      box.append(ul);
    }
    if (r.recommendations.length) {
      box.append(el("h4", "", T("bn.rep.top")));
      const ol = el("ol", "bn-top");
      r.recommendations.forEach(x => {
        const li = el("li", "");
        li.append(el("b", "", `${tx("bn.class." + x.class, x.title)} (${x.count})`), el("div", "bn-line", tx("bn.change." + x.class, x.change)), el("div", "bn-line dim", T("bn.rep.part", x.component)));
        ol.append(li);
      });
      box.append(ol);
    }
    box.append(el("p", "bn-line dim", T("bn.rep.cost", nfmt(r.cost.tokens), nfmt(r.cost.seconds, 1), nfmt(r.cost.tokens_per_trial, 1), nfmt(r.cost.seconds_per_trial, 1))));
    if (r.candidate_lessons.length) {
      box.append(el("h4", "", T("bn.les.title")), el("p", "bn-line dim", T("bn.les.help")));
      const ul = el("ul", "bn-lessons");
      r.candidate_lessons.forEach((l, i) => {
        const li = el("li", "");
        li.append(el("div", "bn-line", l.situation), el("div", "bn-line dim", `${l.went_wrong}. ${l.fix}`));
        const bar = el("div", "bn-bar");
        bar.append(btn(T("bn.les.add"), "", async () => {
          const a = await api("POST", "api/bench/add_lesson", { id: runId, index: i });
          lessonNote[i] = a.ok ? T(a.data.action === "added" ? "bn.les.added" : "bn.les.confirmed") : errText(a);
          paintResults();
        }), ...(lessonNote[i] ? [el("span", "bn-line", lessonNote[i])] : []));
        li.append(bar);
        ul.append(li);
      });
      box.append(ul);
    }
    return box;
  }
  function rankBlock(s) {
    const box = el("div", "bn-sub");
    box.append(el("h4", "", T("bn.rank.title")));
    if (!R || R.status !== "done") { box.append(el("p", "dim", T("bn.rank.notdone"))); return box; }
    if (rankNote) box.append(el("p", "bn-bad", rankNote));
    if (rank) {
      if (rank.basis === "entries") {
        box.append(el("p", "bn-line", T("bn.rank.entries", rank.position, rank.of, rank.percentile == null ? "?" : nfmt(rank.percentile, 1))));
        if (rank.nearest_above) box.append(el("p", "bn-line dim", T("bn.rank.above", rank.nearest_above.name, nfmt(rank.nearest_above.value, 1))));
        if (rank.nearest_below) box.append(el("p", "bn-line dim", T("bn.rank.below", rank.nearest_below.name, nfmt(rank.nearest_below.value, 1))));
        if (rank.position_range) box.append(el("p", "bn-line dim", T("bn.rank.range", rank.position_range.best, rank.position_range.worst)));
      } else box.append(el("p", "bn-line", T("bn.rank.local")));
      if (rank.provisional) box.append(el("p", "bn-line bn-warn", T("bn.prov.rank")));
    }
    box.append(el("p", "bn-line dim", T("bn.rank.help")));
    const addr = el("input"), al = el("label", "", T("bn.rank.addr")), area = el("textarea"), tl = el("label", "", T("bn.rank.paste"));
    addr.id = "bn-addr"; al.htmlFor = "bn-addr"; addr.type = "text"; addr.value = boardAddr; addr.autocomplete = "off"; addr.oninput = () => { boardAddr = addr.value; };
    area.id = "bn-board"; tl.htmlFor = "bn-board"; area.rows = 5; area.value = boardText; area.oninput = () => { boardText = area.value; };
    const w1 = el("div", "bn-field"), w2 = el("div", "bn-field");
    w1.append(al, addr); w2.append(tl, area);
    box.append(w1, w2, btn(T("bn.rank.go"), "", async () => { await loadResults(); paintResults(); }));
    return box;
  }

  /* ---- the publish desk ---- */
  function block(s) {
    const none = () => (!R || R.status !== "done") ? T("bn.pub.notdone") : s.leaderboard === "none" ? T("bn.pub.none") : "";
    if (none()) return { prepare: none(), publish: none() };
    const publish = R.provisional ? T("bn.pub.provisional") : s.leaderboard === "closed" ? T("bn.pub.closed") : s.leaderboard === "email" ? T("bn.pub.email") : "";
    return { prepare: "", publish };
  }
  function paintDesk() {
    const box = hostDesk;
    if (guard(box)) return;
    if (!R || !spec(R.benchmark)) { box.replaceChildren(); return; }
    const s = spec(R.benchmark), why = block(s), card = el("div", "bn-block");
    card.append(el("h3", "", T("bn.pub.title")), el("p", "bn-line dim", T("bn.pub.intro")), el("p", "bn-line dim", `${T("bn.pub.board")}: ${T("bn.board." + s.leaderboard)}${s.leaderboard_target ? " · " + s.leaderboard_target : ""}`));
    if (why.prepare) card.append(el("p", "bn-line bn-warn", why.prepare));
    else if (why.publish) card.append(el("p", "bn-line bn-warn", why.publish));
    const bar = el("div", "bn-bar");
    bar.append(btn(T("bn.pub.prepare"), "", prepare, !!why.prepare), btn(T("bn.pub.begin"), "primary", () => begin(s), !!why.publish));
    card.append(bar);
    if (deskNote) card.append(el("p", deskNote.startsWith("!") ? "bn-bad" : "bn-ok", deskNote.replace(/^!/, "")));
    if (pack) card.append(packageView(pack));
    if (desk) card.append(confirmView());
    box.replaceChildren(card);
  }
  function filesList(files) {
    const ul = el("ul", "bn-classes");
    files.forEach(f => ul.append(el("li", "", `${f.name} · ${nfmt(f.size)} B · ${f.sha256.slice(0, 12)}…`)));
    return ul;
  }
  function packageView(p) {
    const box = el("div", "bn-sub");
    box.append(el("h4", "", T("bn.pub.files")));
    const d = p.manifest.destination;
    box.append(filesList(p.files), el("p", "bn-line", T("bn.pub.dest", d.text)), el("p", "bn-line", T("bn.pub.who", d.who)), el("p", "bn-line dim", T("bn.pub.folder", p.folder)));
    return box;
  }
  function confirmView() {
    const box = el("div", "bn-sub bn-confirm");
    box.append(el("h4", "", T("bn.pub.step2")), el("pre", "bn-summary", desk.summary), el("p", "bn-line", T("bn.pub.type", desk.confirm_phrase)));
    const inp = el("input"), lab = el("label", "", T("bn.pub.field"));
    inp.id = "bn-phrase"; lab.htmlFor = "bn-phrase"; inp.type = "text"; inp.value = typed; inp.autocomplete = "off"; inp.spellcheck = false; inp.setAttribute("autocapitalize", "off");
    ["paste", "drop"].forEach(ev => inp.addEventListener(ev, e => e.preventDefault()));
    const send = btn(T("bn.pub.send"), "danger", finish, !typed.trim());
    inp.oninput = () => { typed = inp.value; send.disabled = !typed.trim(); };
    const w = el("div", "bn-field"), bar = el("div", "bn-bar");
    w.append(lab, inp);
    bar.append(send, btn(T("bn.cancel"), "ghost", () => { desk = null; typed = ""; deskNote = ""; paintDesk(); }));
    box.append(el("h5", "", T("bn.pub.files")), filesList(desk.files), w, bar);
    return box;
  }
  async function prepare() {
    const r = await api("POST", "api/bench/prepare_publish", { id: runId });
    pack = r.ok ? r.data : null; deskNote = r.ok ? "" : "!" + errText(r);
    paintDesk();
  }
  async function begin(s) {
    const r = await api("POST", "api/bench/begin_publish", { id: runId, destination: s.leaderboard });
    if (r.ok) { desk = r.data; typed = ""; deskNote = ""; }
    else { desk = null; deskNote = "!" + errText(r); }
    paintDesk();
  }
  async function finish() {
    const r = await api("POST", "api/bench/finish_publish", { token: desk.token, phrase: typed });
    if (r.ok) { deskNote = r.data.message || (r.data.sent ? T("bn.pub.sent") : T("bn.pub.notsent")); desk = null; typed = ""; }
    else { deskNote = "!" + errText(r); if (r.data && r.data.error && ["confirmation"].includes(r.data.error.type) && /expired|Unknown/.test(errText(r))) { desk = null; typed = ""; } }
    paintDesk();
  }

  /* ---- painting ---- */
  const hostTop = el("div"), hostList = el("div"), hostRun = el("div"), hostResults = el("div"), hostDesk = el("div");
  const guard = box => { const a = document.activeElement; return !!(a && box.contains(a) && FIELDS.test(a.tagName)); };      // a field being typed in is not repainted under the person
  function paintList() {
    const box = hostList;
    if (guard(box)) return;
    const out = el("div", "bn-block");
    out.append(el("h3", "", T("bn.list.title")));
    if (!S) { out.append(el("p", "dim", T("bn.loading"))); box.replaceChildren(out); return; }
    const ul = el("ul", "bn-list");
    S.benchmarks.forEach(b => ul.append(card(b)));
    out.append(ul);
    box.replaceChildren(out, runsList());
  }
  function paintAll() { paintTop(); paintList(); paintRun(); paintResults(); paintDesk(); }

  host.append(hostTop, hostList, hostRun, hostResults, hostDesk);
  paintAll();
  document.addEventListener("basalt:sub", e => { visible = e.detail === "benchmarks"; if (visible) load(); else clearTimeout(timer); });
  document.addEventListener("basalt:lang", () => { if (S) paintAll(); });
})();
