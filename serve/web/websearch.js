"use strict";
/* Settings > Web search: which search service and which page reader the assistant uses, one card per service. A card says what the service is, what is
   good and weak about it, and offers it in the cloud (a link to make the account, a link to the key, a write-only key field) and, where there is one,
   on this PC (its state, an address, the steps of the install shown before the install can be started, a removal). The key goes to the server and is
   never filled back. Everything the server says enters the page as text. */
(function () {
  const host = document.getElementById("ws-root");
  if (!host) return;

  const POLL_MS = 1000, ARM_MS = 4000;
  const NAMES = { duckduckgo: "DuckDuckGo", searxng: "SearXNG", brave: "Brave Search", tavily: "Tavily", exa: "Exa", firecrawl: "Firecrawl", playwright: "Playwright" };
  const ICONS = {
    ok: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM8 12.5l3 3 5-6",
    pause: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM10 9v6M14 9v6",
    none: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z",
    alert: "M12 3l10 18H2L12 3zM12 10v5M12 18v.5",
    plus: "M12 5v14M5 12h14",
    minus: "M5 12h14",
    ext: "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
    check: "M5 12.5l4.5 4.5L19 7",
  };
  let S = null, loadFailed = "", timer = 0, armTimer = 0;
  const state = {};                                   // per provider: the plan shown, the install job, the last test, the line under the card
  const st = pid => state[pid] || (state[pid] = { plan: null, planErr: "", showing: false, job: null, test: null, testing: false, msg: "", bad: false, armed: false, busy: false });

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  function icon(name) {
    const NS = "http://www.w3.org/2000/svg", s = document.createElementNS(NS, "svg"), p = document.createElementNS(NS, "path");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("class", "ws-ico"); s.setAttribute("aria-hidden", "true"); s.setAttribute("focusable", "false");
    p.setAttribute("d", ICONS[name]); p.setAttribute("fill", "none"); p.setAttribute("stroke", "currentColor"); p.setAttribute("stroke-width", "2");
    p.setAttribute("stroke-linecap", "round"); p.setAttribute("stroke-linejoin", "round");
    if (name === "none") p.setAttribute("stroke-dasharray", "3 3");
    s.append(p);
    return s;
  }
  const withIcon = (name, cls, text) => { const e = el("span", cls); e.append(icon(name), el("span", "", text)); return e; };
  const btn = (text, cls, onclick, id) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; if (id) b.id = id; return b; };
  const errText = r => r.status === 0 ? T("ws.e.offline") : (r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`;
  const webUrl = u => (typeof u === "string" && /^https:\/\//i.test(u) ? u : "");
  const active = () => document.body.dataset.view === "settings" && !document.getElementById("view-settings").hidden && !document.getElementById("ws-section").hidden;

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  function link(url, text, cls) {
    const a = el("a", cls || "ws-link"), safe = webUrl(url);
    if (safe) { a.href = safe; a.target = "_blank"; a.rel = "noopener noreferrer"; }
    a.append(icon("ext"), el("span", "", text), el("span", "sr", T("ws.newtab")));
    return a;
  }

  /* ---- loading and drawing ---- */
  async function refresh(quiet) {
    const r = await api("GET", "api/websearch");
    if (r.ok) { S = r.data; loadFailed = ""; } else loadFailed = errText(r);
    if (!quiet) paint();
    return r.ok;
  }
  function paint() {
    const focus = document.activeElement && host.contains(document.activeElement) ? document.activeElement.id : "";
    if (!S) { host.replaceChildren(el("p", "ws-intro", loadFailed || T("ws.loading"))); return; }
    host.replaceChildren(el("p", "ws-intro", T("ws.intro")), globalBox(), el("div", "ws-list", ""));
    S.providers.forEach(p => host.querySelector(".ws-list").append(card(p)));
    if (focus) { const f = document.getElementById(focus); if (f) f.focus(); }
  }
  function paintCard(pid) {
    const old = document.getElementById("ws-card-" + pid), p = S && S.providers.find(x => x.id === pid);
    if (!old || !p) return paint();
    const focus = document.activeElement && old.contains(document.activeElement) ? document.activeElement.id : "";
    old.replaceWith(card(p));
    if (focus) { const f = document.getElementById(focus); if (f) f.focus(); }
  }
  function say(pid, text, bad) {
    if (pid) { const s = st(pid); s.msg = text; s.bad = !!bad; paintCard(pid); return; }
    const n = document.getElementById("ws-gnote");
    if (n) { n.textContent = text; n.dataset.bad = bad ? "1" : ""; n.hidden = !text; }
  }
  async function change(body, pid) {
    const r = await api("POST", "api/websearch/select", body);
    if (r.ok) { S = r.data; if (pid) st(pid).msg = ""; paint(); say("", "", false); return true; }
    paint();
    say(pid || "", errText(r), true);
    return false;
  }

  /* ---- the two global choices ---- */
  function radio(name, id, label, checked, onchange) {
    const row = el("label", "ws-radio"), input = el("input");
    input.type = "radio"; input.name = name; input.id = id; input.checked = checked; input.onchange = () => { if (input.checked) onchange(); };
    row.append(input, el("span", "", label));
    return row;
  }
  function globalBox() {
    const box = el("div", "ws-global"), now = S.providers.find(p => p.search), rd = S.providers.find(p => p.reader);
    const line = el("p", "ws-now");
    line.append(el("b", "", T("ws.now.search", now ? NAMES[now.id] : "-")), document.createTextNode(" - "), el("span", "", T("ws.now.reader", rd ? NAMES[rd.id] : T("ws.reader.none"))));
    box.append(line, el("p", "help", T("ws.now.hint")));
    const set = el("fieldset", "ws-reader"), lg = el("legend", "", T("ws.reader.label"));
    set.append(lg, radio("ws-reader", "ws-reader-none", T("ws.reader.none"), S.reader === "none", () => change({ reader: "none" })), el("p", "help", T("ws.reader.help")));
    box.append(set);
    const auto = el("label", "ws-radio"), cb = el("input");
    cb.type = "checkbox"; cb.id = "ws-autostart"; cb.checked = !!S.autostart; cb.onchange = () => change({ autostart: cb.checked });
    auto.append(cb, el("span", "", T("ws.autostart")));
    box.append(auto);
    const note = el("p", "ws-note"); note.id = "ws-gnote"; note.hidden = true; note.setAttribute("role", "status");
    box.append(note);
    return box;
  }

  /* ---- one card ---- */
  function card(p) {
    const pid = p.id, s = st(pid), art = el("article", "ws-card" + (p.search || p.reader ? " ws-on" : ""));
    art.id = "ws-card-" + pid; art.setAttribute("aria-labelledby", "ws-h-" + pid);
    const head = el("div", "ws-head"), h = el("h3", "", NAMES[pid] || pid);
    h.id = "ws-h-" + pid;
    head.append(h);
    if (p.builtin) head.append(el("span", "ws-badge", T("ws.badge.builtin")));
    if (p.search) head.append(withIcon("check", "ws-badge ws-badge-on", T("ws.badge.search")));
    if (p.reader) head.append(withIcon("check", "ws-badge ws-badge-on", T("ws.badge.reader")));
    art.append(head, el("p", "ws-what", T(`ws.p.${pid}.what`)));

    const picks = el("div", "ws-picks");
    if (p.kind === "search" || p.kind === "both") picks.append(radio("ws-search", `ws-${pid}-search`, T("ws.use.search"), !!p.search, () => change({ search: pid }, pid)));
    if (p.kind === "reader" || p.kind === "both") picks.append(radio("ws-reader", `ws-${pid}-reader`, T("ws.use.reader"), !!p.reader, () => change({ reader: pid }, pid)));
    art.append(picks);

    if (p.builtin) art.append(el("p", "ws-note", T("ws.builtin.none")));
    else {
      if (p.modes.length > 1) art.append(modeChoice(p));
      art.append(p.mode === "cloud" && p.cloud ? cloudBlock(p) : p.local ? localBlock(p) : el("div"));
    }
    art.append(testRow(p));
    const msg = el("p", "ws-note", s.msg); msg.setAttribute("role", "status"); msg.dataset.bad = s.bad ? "1" : ""; msg.hidden = !s.msg;
    art.append(msg, goodWeak(pid));
    return art;
  }

  function modeChoice(p) {
    const box = el("div", "ws-mode"), group = el("div", "ws-seg");
    group.setAttribute("role", "group"); group.setAttribute("aria-label", T("ws.mode.group"));
    ["local", "cloud"].filter(m => p.modes.includes(m)).forEach(m => {
      const b = btn("", "ws-segbtn", () => { if (p.mode !== m) change({ mode: { [p.id]: m } }, p.id); }, `ws-${p.id}-mode-${m}`);
      b.setAttribute("aria-pressed", String(p.mode === m));
      if (p.mode === m) b.append(icon("check"));
      b.append(el("span", "", T("ws.mode." + m)));
      if (m === "local") b.append(el("span", "ws-pref", T("ws.mode.preferred")));
      group.append(b);
    });
    box.append(group);
    return box;
  }

  /* ---- the cloud half: the links, the key or the address ---- */
  function field(id, label, help) {
    const wrap = el("div", "ws-field"), lab = el("label", "", label);
    lab.htmlFor = id;
    wrap.append(lab);
    if (help) wrap.append(el("p", "help", help));
    return wrap;
  }
  function cloudBlock(p) {
    const pid = p.id, c = p.cloud, box = el("div", "ws-block");
    box.append(el("h4", "", T("ws.cloud.title")));
    const links = el("div", "ws-links");
    if (c.signup_url) links.append(link(c.signup_url, c.needs_url ? T("ws.cloud.instances") : T("ws.cloud.signup")));
    if (c.needs_key && c.keys_url) links.append(link(c.keys_url, T("ws.cloud.keys")));
    box.append(links);
    if (c.needs_key) {
      const form = el("form", "ws-form"), id = `ws-${pid}-key`, wrap = field(id, T("ws.key.label"), T("ws.key.help")), input = el("input");
      input.id = id; input.type = "password"; input.autocomplete = "off"; input.value = ""; input.spellcheck = false;
      input.setAttribute("autocapitalize", "off"); input.setAttribute("autocorrect", "off");
      wrap.append(input);
      const ks = el("p", "ws-keystate");
      ks.append(icon(c.key_set ? "ok" : "none"), el("span", "", c.key_set ? T("ws.key.set") : T("ws.key.unset")));
      const save = btn(T("ws.key.save"), "primary", () => form.requestSubmit(), `ws-${pid}-keysave`);
      form.append(wrap, ks, save);
      if (c.key_set) form.append(btn(T("ws.key.remove"), "ghost", () => setKey(pid, "", input), `ws-${pid}-keyremove`));
      form.onsubmit = e => { e.preventDefault(); if (!input.value.trim()) { say(pid, T("ws.key.empty"), true); return; } setKey(pid, input.value.trim(), input); };
      box.append(form);
    }
    if (c.needs_url) box.append(addressForm(p, p.url, T("ws.url.cloud"), T("ws.url.cloudhelp"), ""));
    return box;
  }
  async function setKey(pid, key, input) {
    input.value = "";                                                // the key is not kept in the page once sent
    const r = await api("POST", "api/websearch/key", { provider: pid, key });
    if (r.ok) { S = r.data; st(pid).msg = key ? T("ws.key.saved") : T("ws.key.removed"); st(pid).bad = false; } else { st(pid).msg = errText(r); st(pid).bad = true; }
    paintCard(pid);
  }
  function addressForm(p, value, label, help, fallback) {
    const pid = p.id, form = el("form", "ws-form"), id = `ws-${pid}-url-${p.mode}`, wrap = field(id, label, help), input = el("input");
    input.id = id; input.type = "url"; input.value = value || ""; input.placeholder = fallback || ""; input.autocomplete = "off"; input.spellcheck = false;
    input.setAttribute("inputmode", "url");
    wrap.append(input);
    if (fallback) wrap.append(el("p", "help", T("ws.url.default", fallback)));
    form.append(wrap, btn(T("ws.url.save"), "", () => form.requestSubmit(), `ws-${pid}-urlsave`));
    form.onsubmit = async e => {
      e.preventDefault();
      if (await change({ urls: { [pid]: input.value.trim() } }, pid)) { st(pid).msg = T("ws.url.saved"); st(pid).bad = false; paintCard(pid); }
    };
    return form;
  }

  /* ---- the local half: its state, its address, the steps, the install ---- */
  function localState(p) {
    const L = p.local;
    if (L.how === "npx") return ["none", "ws.st.npx"];
    if (L.how === "compose") return L.up ? ["ok", "ws.st.up"] : ["none", "ws.st.down"];
    if (L.running) return L.up ? ["ok", "ws.st.running"] : ["pause", "ws.st.runningdown"];
    return L.installed ? ["pause", "ws.st.stopped"] : ["none", "ws.st.none"];
  }
  const missing = L => L.needs.filter(n => !L.needs_present[n]);
  function localBlock(p) {
    const pid = p.id, L = p.local, s = st(pid), box = el("div", "ws-block");
    box.append(el("h4", "", T("ws.local.title")));
    const [ic, key] = localState(p), status = el("p", "ws-status");
    status.append(el("b", "", T("ws.st.label") + ": "), icon(ic), el("span", "", T(key)));
    box.append(status);
    missing(L).forEach(n => box.append(withIcon("alert", "ws-warn", T("ws.need." + n))));
    if (L.how !== "npx") box.append(addressForm(p, L.url, T("ws.url.label"), "", L.default_url));

    const bar = el("div", "ws-bar");
    bar.append(btn(s.showing ? T("ws.steps.hide") : T("ws.steps.show"), "", () => toggleSteps(pid), `ws-${pid}-steps`));
    if (L.automatic && (pid !== "searxng" || L.installed)) bar.append(btn(s.armed ? T("ws.remove.sure") : T("ws.remove"), "ghost" + (s.armed ? " ws-armed" : ""), () => remove(pid), `ws-${pid}-remove`));
    box.append(bar);
    if (s.showing) box.append(stepsBlock(p));
    const job = el("div", "ws-jobbox"); job.id = `ws-${pid}-job`;
    if (s.job) job.append(jobNode(pid));
    box.append(job);
    return box;
  }
  async function toggleSteps(pid) {
    const s = st(pid);
    if (s.showing) { s.showing = false; paintCard(pid); return; }
    const [r] = await Promise.all([api("GET", "api/websearch/plan?provider=" + encodeURIComponent(pid)), refresh(true)]);
    s.plan = r.ok ? r.data : null; s.planErr = r.ok ? "" : errText(r); s.showing = true;
    paintCard(pid);
    const f = document.getElementById(`ws-${pid}-steps`); if (f) f.focus();
  }
  function stepsBlock(p) {
    const pid = p.id, s = st(pid), box = el("div", "ws-steps");
    if (s.planErr) { box.append(withIcon("alert", "ws-warn", s.planErr)); return box; }
    const plan = s.plan;
    box.append(el("h5", "", T("ws.steps.title")));
    const ol = el("ol", "ws-steplist");
    plan.steps.forEach(step => { const li = el("li", ""); li.append(el("span", "ws-steplabel", step.label), el("pre", "ws-cmd", step.command)); ol.append(li); });
    box.append(ol);
    if (plan.note) box.append(el("p", "help", plan.note));
    if (!plan.automatic) {
      box.append(el("p", "", T("ws.steps.manual")));
      if (p.local.repo) box.append(link(p.local.repo, T("ws.steps.guide")));
      return box;
    }
    const lacking = missing(p.local), running = s.job && s.job.state === "running";
    box.append(withIcon("alert", "ws-warn", T("ws.safety")));
    const install = btn(T("ws.install"), "primary", () => startInstall(pid), `ws-${pid}-install`);
    install.disabled = lacking.length > 0 || running;
    box.append(install);
    if (lacking.length) box.append(el("p", "help", T("ws.install.missing")));
    return box;
  }
  async function remove(pid) {
    const s = st(pid);
    if (!s.armed) { s.armed = true; clearTimeout(armTimer); armTimer = setTimeout(() => { s.armed = false; paintCard(pid); }, ARM_MS); paintCard(pid); return; }
    s.armed = false; clearTimeout(armTimer);
    const r = await api("POST", "api/websearch/uninstall", { provider: pid });
    if (r.ok) { S = r.data; s.msg = T("ws.removed"); s.bad = false; s.job = null; } else { s.msg = errText(r); s.bad = true; }
    paint();
  }

  /* ---- the install job: its progress and its output ---- */
  async function startInstall(pid) {
    const s = st(pid);
    const r = await api("POST", "api/websearch/install", { provider: pid });
    if (!r.ok) { s.msg = errText(r); s.bad = true; paintCard(pid); return; }
    s.msg = ""; s.job = r.data; paintCard(pid); poll();
  }
  function jobNode(pid) {
    const j = st(pid).job, box = el("div", "ws-job");
    const head = el("p", "ws-jobline");
    head.append(el("b", "", j.state === "running" ? T("ws.job.running") : j.state === "done" ? T("ws.job.done") : T("ws.job.failed")),
                document.createTextNode(" " + T("ws.job.step", j.step, j.steps)));
    const log = el("pre", "ws-log", (j.lines || []).join("\n"));
    log.setAttribute("role", "log"); log.setAttribute("aria-label", T("ws.job.log")); log.tabIndex = 0;
    box.append(head, log);
    return box;
  }
  function paintJob(pid) {
    const holder = document.getElementById(`ws-${pid}-job`);
    if (!holder || !st(pid).job) return;
    holder.replaceChildren(jobNode(pid));
    const log = holder.querySelector(".ws-log"); if (log) log.scrollTop = log.scrollHeight;
  }
  function poll() {
    clearTimeout(timer);
    const running = Object.keys(state).filter(k => state[k].job && state[k].job.state === "running");
    if (!running.length || !active() || document.hidden) return;
    timer = setTimeout(async () => {
      for (const id of running) {
        const s = st(id), r = await api("GET", "api/websearch/job?id=" + encodeURIComponent(s.job.id));
        if (!r.ok) { s.msg = errText(r); s.bad = true; s.job = null; paintCard(id); continue; }
        const was = s.job.state; s.job = r.data;
        if (was === "running" && s.job.state !== "running") { await refresh(true); paint(); } else paintJob(id);
      }
      poll();
    }, POLL_MS);
  }

  /* ---- the test ---- */
  function testRow(p) {
    const pid = p.id, s = st(pid), row = el("div", "ws-testrow");
    const b = btn(s.testing ? T("ws.test.running") : T("ws.test"), "", () => runTest(pid), `ws-${pid}-test`);
    b.disabled = s.testing;
    const out = el("p", "ws-testout"); out.setAttribute("role", "status");
    if (s.test) {
      const t = s.test;
      out.append(icon(t.ok ? "ok" : "alert"), el("span", "", t.ok ? T("ws.test.ok", t.count, t.what === "characters" ? T("ws.what.characters") : T("ws.what.results"), t.ms) : T("ws.test.fail", t.message || "")));
    }
    row.append(b, out, el("p", "help ws-testhelp", T("ws.test.help")));
    return row;
  }
  async function runTest(pid) {
    const s = st(pid);
    s.testing = true; s.test = null; paintCard(pid);
    const r = await api("POST", "api/websearch/test", { provider: pid });
    s.testing = false;
    s.test = r.ok ? r.data : { ok: false, message: errText(r) };
    paintCard(pid);
  }

  /* ---- good and weak ---- */
  function goodWeak(pid) {
    const grid = el("div", "ws-gw");
    [["good", "plus"], ["weak", "minus"]].forEach(([k, ic]) => {
      const col = el("div", "ws-" + k), h = el("h4", ""), ul = el("ul", "");
      h.append(icon(ic), el("span", "", T("ws." + k)));
      T(`ws.p.${pid}.${k}`).split("\n").forEach(line => ul.append(el("li", "", line)));
      col.append(h, ul);
      grid.append(col);
    });
    return grid;
  }

  /* ---- when the tab is shown ---- */
  document.addEventListener("tadamoo:sub", e => {
    if (e.detail !== "websearch") return;
    refresh().then(() => poll());
  });
  document.addEventListener("tadamoo:lang", () => { if (S) paint(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && active()) poll(); });
  if (active()) refresh();
})();
