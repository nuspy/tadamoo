"use strict";
/* The exfiltration test under Monitor > Security: the configuration, start and stop, the live counters, the findings, the report and
   the reviewer's opinion. The test itself is the Openweight Canary add-on; the server (serve/canary_api.py) starts it, keeps the
   security monitor on while it runs and switches it off at the end. This section lives below the monitor's own and is put back
   whenever the monitor page is rebuilt. */
(function () {
  const root = $("#view-security");
  const kit = window.nwKit;
  if (!root || !kit) return;
  const { el, call, btn, say, field, checkRow, chip, when, num } = kit;
  const POLL_MS = 3000;
  const SEVERITIES = ["high", "medium", "low", "info"];
  const sec = el("section", "nw-sec cn");
  sec.id = "cn-section";

  let S = null, built = false, formFilled = false, busy = false, sevFilter = "", shown = "", reviewSig = "";
  let rows = [], total = 0;
  const ui = {};

  const dur = s => {
    s = Math.max(0, Math.round(s));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? T("cn.dur.hm", h, m) : m ? T("cn.dur.m", m) : T("cn.dur.s", s);
  };
  const sevChip = sev => chip(T("cn.sev." + sev), sev === "high" || sev === "medium" ? "warn" : "");

  function attach() {
    if (!sec.isConnected) root.append(sec);
  }

  /* ---- the form ---- */
  function select(options, value) {
    const s = el("select", "nw-in");
    options.forEach(([v, label]) => { const o = el("option", null, label); o.value = v; s.append(o); });
    s.value = value;
    return s;
  }
  function input(type, extra) {
    const i = el("input", "nw-in");
    i.type = type;
    Object.assign(i, extra || {});
    return i;
  }
  function numberInput(min) {
    const i = input("number");
    i.min = String(min);
    i.inputMode = "decimal";
    return i;
  }

  function buildForm() {
    const form = el("details", "nw-manual");
    form.open = true;
    form.append(el("summary", "nw-sum", T("cn.cfg.title")));
    form.append(el("p", "nw-hint", T("cn.cfg.intro")));

    ui.runtime = select([["basalt", T("cn.rt.basalt")]], "basalt");
    form.append(field("cn-runtime", "cn.f.runtime", "cn.h.runtime", ui.runtime));
    ui.model = input("text");
    form.append(field("cn-model", "cn.f.model", "cn.h.model", ui.model));
    ui.base = input("url");
    ui.base.inputMode = "url";
    form.append(field("cn-base", "cn.f.base", "cn.h.base", ui.base));
    ui.key = input("password");
    ui.key.autocomplete = "off";
    form.append(field("cn-key", "cn.f.key", "cn.h.key", ui.key));
    ui.vendor = select([["auto", T("cn.vendor.auto")]], "auto");
    form.append(field("cn-vendor", "cn.f.vendor", "cn.h.vendor", ui.vendor));

    ui.mode = select([["forever", T("cn.mode.forever")], ["fixed", T("cn.mode.fixed")]], "forever");
    ui.mode.addEventListener("change", paintMode);
    form.append(field("cn-mode", "cn.f.mode", "cn.h.mode", ui.mode));
    ui.minutes = numberInput(1);
    ui.minutesField = field("cn-minutes", "cn.f.minutes", "cn.h.minutes", ui.minutes);
    form.append(ui.minutesField);
    ui.rate = numberInput(0.1);
    form.append(field("cn-rate", "cn.f.rate", "cn.h.rate", ui.rate));
    ui.every = numberInput(1);
    form.append(field("cn-every", "cn.f.every", "cn.h.every", ui.every));

    /* Reserved for the harness profile: which agent harness the test imitates (the add-on lists them with "python -m canary harnesses").
       It stays empty, and hidden, while the add-on offers none. */
    ui.harness = select([["", T("cn.harness.default")]], "");
    ui.harnessBox = field("cn-harness", "cn.f.harness", "cn.h.harness", ui.harness);
    ui.harnessBox.id = "cn-harness-profile";
    ui.harnessBox.hidden = true;
    form.append(ui.harnessBox);

    ui.keep = input("checkbox");
    form.append(checkRow("cn-keep", "cn.f.keep", "cn.h.keep", ui.keep));

    const rv = el("fieldset", "cn-rv");
    rv.append(el("legend", "nw-lab", T("cn.rv.title")), el("p", "nw-hint", T("cn.rv.intro")));
    ui.rvOn = input("checkbox");
    rv.append(checkRow("cn-rv-on", "cn.rv.on", "cn.rv.onh", ui.rvOn));
    ui.rvKind = select([["openai", "OpenAI"], ["anthropic", "Anthropic"]], "openai");
    rv.append(field("cn-rv-kind", "cn.rv.kind", "cn.rv.kindh", ui.rvKind));
    ui.rvBase = input("url");
    ui.rvBase.inputMode = "url";
    rv.append(field("cn-rv-base", "cn.rv.base", "cn.rv.baseh", ui.rvBase));
    ui.rvKey = input("password");
    ui.rvKey.autocomplete = "off";
    rv.append(field("cn-rv-key", "cn.rv.key", "cn.rv.keyh", ui.rvKey));
    ui.rvModel = input("text");
    rv.append(field("cn-rv-model", "cn.rv.model", "cn.rv.modelh", ui.rvModel));
    form.append(rv);

    const acts = el("div", "nw-act");
    ui.save = btn(T("cn.save"), "", save);
    ui.cfgMsg = el("p", "nw-line");
    ui.cfgMsg.setAttribute("role", "status");
    acts.append(ui.save);
    form.append(acts, ui.cfgMsg);
    return form;
  }

  function paintMode() {
    ui.minutesField.hidden = ui.mode.value !== "fixed";
  }

  function fillOptions(sel, items, keep) {
    const cur = sel.value;
    const sig = items.map(i => i[0]).join("|");
    if (sel.dataset.sig === sig) return;
    sel.dataset.sig = sig;
    sel.replaceChildren();
    items.forEach(([v, label]) => { const o = el("option", null, label); o.value = v; sel.append(o); });
    sel.value = items.some(i => i[0] === cur) ? cur : keep;
  }

  function fillForm() {
    const s = S.settings;
    fillOptions(ui.runtime, [["basalt", T("cn.rt.basalt")]].concat((S.runtimes || []).filter(r => r.id !== "basalt").map(r => [r.id, r.name])), "basalt");
    fillOptions(ui.vendor, [["auto", T("cn.vendor.auto")]].concat((S.vendors || []).map(v => [v.id, v.name])), "auto");
    const hs = S.harnesses || [];
    ui.harnessBox.hidden = !hs.length;
    fillOptions(ui.harness, [["", T("cn.harness.default")]].concat(hs.map(h => [h.id, h.name || h.id])), "");
    if (formFilled) return;
    formFilled = true;
    ui.runtime.value = s.runtime;
    ui.model.value = s.model;
    ui.base.value = s.base_url;
    ui.key.value = s.api_key;
    ui.vendor.value = s.vendor;
    ui.mode.value = s.duration_minutes ? "fixed" : "forever";
    ui.minutes.value = s.duration_minutes || "";
    ui.rate.value = s.sessions_per_hour;
    ui.every.value = s.review_every;
    ui.harness.value = s.harness || "";
    ui.keep.checked = !!s.keep_running;
    ui.rvOn.checked = !!s.reviewer.enabled;
    ui.rvKind.value = s.reviewer.kind;
    ui.rvBase.value = s.reviewer.base_url;
    ui.rvKey.value = s.reviewer.api_key;
    ui.rvModel.value = s.reviewer.model;
    paintMode();
  }

  function collect() {
    return {
      runtime: ui.runtime.value, model: ui.model.value, base_url: ui.base.value, api_key: ui.key.value, vendor: ui.vendor.value,
      duration_minutes: ui.mode.value === "fixed" ? Number(ui.minutes.value) || 0 : null,
      sessions_per_hour: Number(ui.rate.value) || 0, review_every: Math.floor(Number(ui.every.value)) || 0,
      keep_running: ui.keep.checked, harness: ui.harnessBox.hidden ? "" : ui.harness.value,
      reviewer: { enabled: ui.rvOn.checked, kind: ui.rvKind.value, base_url: ui.rvBase.value, api_key: ui.rvKey.value, model: ui.rvModel.value },
    };
  }

  async function save() {
    try {
      S = await call("api/canary/settings", collect());
      formFilled = false;
      fillForm();
      say(ui.cfgMsg, T("cn.saved"));
      paintStatus();
    } catch (e) { say(ui.cfgMsg, e.message, true); }
  }

  /* ---- start and stop ---- */
  async function start() {
    if (busy) return;
    busy = true;
    ui.runMsg.dataset.kind = "";
    try {
      const j = await call("api/canary/start", collect());
      formFilled = false;
      say(ui.runMsg, j.netwatch_turned_on ? T("cn.started.nw") : T("cn.started"));
    } catch (e) { say(ui.runMsg, e.message, true); }
    busy = false;
    await refresh(true);
  }
  async function stop() {
    if (busy) return;
    busy = true;
    try {
      await call("api/canary/stop", {});
      say(ui.runMsg, T("cn.stopping"));
    } catch (e) { say(ui.runMsg, e.message, true); }
    busy = false;
    await refresh(true);
  }

  /* ---- the page ---- */
  function build() {
    sec.replaceChildren();
    sec.append(el("h2", "nw-h", T("cn.title")), el("p", "nw-p", T("cn.intro")));
    ui.missing = el("p", "nw-warn", T("cn.missing"));
    ui.missing.hidden = true;
    sec.append(ui.missing);

    ui.body = el("div", "cn-body");
    ui.body.hidden = true;

    const run = el("div", "cn-run");
    const top = el("div", "nw-top");
    ui.state = chip("", "");
    ui.since = el("span", "nw-time");
    top.append(ui.state, ui.since);
    ui.counts = el("div", "nw-tags");
    ui.line = el("p", "nw-line");
    ui.nwline = el("p", "nw-hint");
    const acts = el("div", "nw-act");
    ui.start = btn(T("cn.start"), "primary", start);
    ui.stop = btn(T("cn.stop"), "", stop);
    acts.append(ui.start, ui.stop);
    ui.runMsg = el("p", "nw-line");
    ui.runMsg.setAttribute("role", "status");
    run.append(top, ui.counts, ui.line, ui.nwline, acts, ui.runMsg, el("p", "nw-hint", T("cn.run.help")));
    ui.body.append(run, buildForm());

    const fs = el("div", "cn-find");
    fs.append(el("h3", "nw-h", T("cn.find.title")), el("p", "nw-hint", T("cn.find.intro")));
    const bar = el("div", "nw-bar");
    ui.sevBtns = {};
    [""].concat(SEVERITIES).forEach(sv => {
      const b = btn(sv ? T("cn.sev." + sv) : T("cn.sev.all"), "ghost", () => { sevFilter = sv; shown = ""; paintSev(); loadFindings(); });
      ui.sevBtns[sv] = b;
      bar.append(b);
    });
    ui.verify = btn(T("cn.verify"), "ghost", verify);
    bar.append(ui.verify);
    ui.verMsg = el("p", "nw-line");
    ui.verMsg.setAttribute("role", "status");
    ui.list = el("ul", "nw-list");
    ui.regMsg = el("p", "nw-line");
    ui.regMsg.setAttribute("role", "status");
    fs.append(bar, ui.verMsg, ui.list, ui.regMsg);

    const rp = el("div", "cn-report");
    rp.append(el("h3", "nw-h", T("cn.rep.title")), el("p", "nw-hint", T("cn.rep.intro")));
    const rb = el("div", "nw-act");
    rb.append(btn(T("cn.rep.make"), "", makeReport), btn(T("cn.rep.show"), "ghost", showReport));
    ui.repMsg = el("p", "nw-line");
    ui.repMsg.setAttribute("role", "status");
    ui.repBox = el("details", "nw-manual");
    ui.repBox.hidden = true;
    ui.repBox.append(el("summary", "nw-sum", T("cn.rep.view")));
    ui.repText = el("pre", "nw-mono cn-pre");
    ui.repBox.append(ui.repText);
    rp.append(rb, ui.repMsg, ui.repBox);

    const rv = el("div", "cn-review");
    rv.append(el("h3", "nw-h", T("cn.rev.title")), el("p", "nw-hint", T("cn.rev.intro")));
    const vb = el("div", "nw-act");
    ui.revBtn = btn(T("cn.rev.ask"), "", ask);
    vb.append(ui.revBtn);
    ui.revMsg = el("p", "nw-line");
    ui.revMsg.setAttribute("role", "status");
    ui.revList = el("ul", "nw-list");
    rv.append(vb, ui.revMsg, ui.revList);

    const lg = el("details", "nw-manual");
    lg.append(el("summary", "nw-sum", T("cn.log.title")));
    ui.log = el("pre", "nw-mono cn-pre");
    lg.append(ui.log);
    lg.addEventListener("toggle", () => { if (lg.open) loadLog(); });

    ui.body.append(fs, rp, rv, lg);
    sec.append(ui.body);
    built = true;
    formFilled = false;
    shown = "";
    reviewSig = "";
    paintSev();
    if (S) { paintStatus(); fillForm(); }
  }

  function paintSev() {
    Object.entries(ui.sevBtns).forEach(([k, b]) => b.setAttribute("aria-pressed", String(k === sevFilter)));
  }

  function paintStatus() {
    if (!S || !built) return;
    ui.missing.hidden = !!S.addon;
    ui.body.hidden = !S.addon;
    if (!S.addon) return;
    const running = !!S.running, stopping = S.state === "stopping";
    ui.state.textContent = T("cn.state." + (S.state || "idle"));
    ui.state.className = "nw-chip" + (running ? " warn" : "");
    ui.since.textContent = S.started ? (running ? T("cn.since", dur(Date.now() / 1000 - S.started)) : T("cn.ended", when(S.finished || S.started))) : "";
    ui.counts.replaceChildren(chip(T("cn.sessions", num.format(S.sessions || 0)), "tag"));
    SEVERITIES.forEach(sv => ui.counts.append(chip(`${T("cn.sev." + sv)}: ${(S.findings && S.findings[sv]) || 0}`, (S.findings && S.findings[sv] && (sv === "high" || sv === "medium")) ? "warn" : "tag")));
    const e = S.effective || {};
    ui.line.textContent = running
      ? (S.duration_ends ? T("cn.line.until", when(S.duration_ends)) : T("cn.line.forever"))
      : (e.model ? T("cn.line.next", e.model, e.base_url) : T("cn.line.nomodel"));
    const nw = S.netwatch || {};
    ui.nwline.textContent = nw.by_canary && running ? T("cn.nw.auto") : nw.enabled ? T("cn.nw.on") : T("cn.nw.off");
    ui.start.disabled = running || busy;
    ui.stop.disabled = !running || stopping;
    ui.revBtn.disabled = !(S.settings && S.settings.reviewer && S.settings.reviewer.enabled) || (S.review && S.review.state === "running");
  }

  /* ---- findings ---- */
  function summary(f) {
    const ev = f.evidence || {};
    if (ev.host || ev.remote_ip) {
      const port = ev.remote_port ? `:${ev.remote_port}` : "";
      return `${ev.process ? ev.process + " → " : ""}${ev.host ? ev.host + " (" + (ev.remote_ip || "") + port + ")" : ev.remote_ip + port}`;
    }
    const t = ev.note || ev.message || ev.canary || JSON.stringify(ev);
    return String(t).slice(0, 300);
  }

  function findingRow(f) {
    const li = el("li", "nw-row" + (f.severity === "high" ? " bad" : ""));
    const top = el("div", "nw-top");
    top.append(sevChip(f.severity), chip("#" + f.n, ""), chip(f.kind || "", "tag"), el("span", "nw-time", f.ts ? when(f.ts) : ""));
    if (f.session != null) top.append(chip(T("cn.session", f.session), ""));
    li.append(top, el("div", "nw-what", summary(f)));
    const det = el("details", "nw-manual");
    det.append(el("summary", "nw-sum", T("cn.find.evidence")), el("pre", "nw-mono cn-pre", JSON.stringify(f.evidence, null, 2)));
    const acts = el("div", "nw-act");
    if (f.registered) acts.append(chip(T("cn.find.registered"), "tag"));
    else acts.append(btn(T("cn.find.register"), "", async () => {
      try { await call(`api/canary/findings/${f.n}/register`, {}); say(ui.regMsg, T("cn.find.regdone", f.n)); await loadFindings(); } catch (e) { say(ui.regMsg, e.message, true); }
    }));
    li.append(det, acts);
    return li;
  }

  async function loadFindings() {
    try {
      const j = await call("api/canary/findings" + (sevFilter ? "?severity=" + sevFilter : ""));
      rows = j.findings || [];
      total = j.total || 0;
      const sig = JSON.stringify([sevFilter, rows.map(r => [r.n, r.registered])]);
      if (sig === shown) return;
      shown = sig;
      ui.list.replaceChildren(...(rows.length ? rows.map(findingRow) : [el("li", "nw-hint", T(total ? "cn.find.nomatch" : "cn.find.none"))]));
    } catch (e) { say(ui.regMsg, e.message, true); }
  }

  async function verify() {
    try {
      const j = await call("api/canary/verify");
      say(ui.verMsg, j.ok ? T("cn.verify.ok", j.count) : T("cn.verify.bad", j.message), !j.ok);
    } catch (e) { say(ui.verMsg, e.message, true); }
  }

  /* ---- report, review, log ---- */
  async function showReport(make) {
    try {
      const j = make === true ? await call("api/canary/report", {}) : await call("api/canary/report");
      if (!j.exists) { ui.repBox.hidden = true; say(ui.repMsg, T("cn.rep.none")); return; }
      ui.repText.textContent = j.markdown + (j.truncated ? "\n\n…" : "");
      ui.repBox.hidden = false;
      ui.repBox.open = true;
      say(ui.repMsg, T("cn.rep.from", when(j.generated)));
    } catch (e) { say(ui.repMsg, e.message, true); }
  }
  const makeReport = () => showReport(true);

  async function ask() {
    try { await call("api/canary/review", {}); say(ui.revMsg, T("cn.rev.running")); } catch (e) { say(ui.revMsg, e.message, true); return; }
    await refresh(true);
  }

  function reviewRow(r) {
    const li = el("li", "nw-row");
    const a = r.assessment;
    const top = el("div", "nw-top");
    top.append(chip("#" + r.n, ""), chip(r.model || "", "tag"), el("span", "nw-time", r.time || ""));
    if (a && a.confidence) top.append(chip(T("cn.rev.conf", a.confidence), ""));
    li.append(top);
    if (r.error) li.append(el("p", "nw-warn", r.error));
    else if (!a) li.append(el("p", "nw-hint", T("cn.rev.unreadable")));
    else {
      li.append(el("p", "nw-what", a.summary || ""));
      const list = (title, items, fmt) => {
        if (!Array.isArray(items) || !items.length) return;
        const d = el("details", "nw-manual");
        d.append(el("summary", "nw-sum", `${title} (${items.length})`));
        const ul = el("ul", "nw-plain");
        items.forEach(it => ul.append(el("li", "nw-tagx", fmt(it))));
        d.append(ul);
        li.append(d);
      };
      const pair = it => (it && typeof it === "object") ? `${it.item || ""}: ${it.why || ""}` : String(it);
      list(T("cn.rev.susp"), a.suspicious, pair);
      list(T("cn.rev.legit"), a.explained_by_legitimate_behaviour, pair);
      list(T("cn.rev.next"), a.next_checks, x => String(x));
    }
    return li;
  }

  async function loadReviews() {
    try {
      const j = await call("api/canary/reviews");
      const sig = JSON.stringify([j.state, (j.reviews || []).map(r => r.n)]);
      const st = j.state || {};
      if (st.state === "running") say(ui.revMsg, T("cn.rev.running"));
      else if (st.state === "error") say(ui.revMsg, T("cn.rev.error", st.error || ""), true);
      else if (st.state === "done") say(ui.revMsg, T("cn.rev.done"));
      if (sig === reviewSig) return;
      reviewSig = sig;
      ui.revList.replaceChildren(...((j.reviews || []).length ? j.reviews.map(reviewRow) : [el("li", "nw-hint", T("cn.rev.none"))]));
    } catch (e) { say(ui.revMsg, e.message, true); }
  }

  async function loadLog() {
    try { ui.log.textContent = (await call("api/canary/log")).log || T("cn.log.none"); } catch (e) { ui.log.textContent = e.message; }
  }

  /* ---- refresh ---- */
  let lastCount = -1, lastState = "";
  async function refresh(force) {
    if (!built) return;
    try {
      S = await call("api/canary");
      fillForm();
      paintStatus();
      if (!S.addon) return;
      const count = S.findings_total, state = S.state + (S.review ? S.review.state : "");
      if (force || count !== lastCount || state !== lastState) {
        lastCount = count; lastState = state;
        await Promise.all([loadFindings(), loadReviews()]);
      }
    } catch (e) { ui.missing.hidden = false; ui.missing.textContent = e.message; }
  }

  function enter() {
    attach();
    if (!built) build();
    refresh(true);
  }

  document.addEventListener("basalt:view", e => { if (e.detail === "security") enter(); });
  document.addEventListener("basalt:lang", () => { if (built) { build(); refresh(true); } });
  setInterval(() => { if (document.body.dataset.view === "security" && !document.hidden) { attach(); refresh(false); } }, POLL_MS);
  if (document.body.dataset.view === "security") enter();
})();
