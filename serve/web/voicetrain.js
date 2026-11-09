"use strict";
/* The "Train a personalised model" section of the Avatar card: for an avatar that has a video, the two personalised engines (Ultralight-Digital-Human and
   FeatherTalk). The person asks for a plan (POST /avatar/train/plan through the voice proxy) and sees the video it would use, an estimated duration labelled as
   an estimate, the GPU memory (a figure only when the upstream docs give one), what will be created and the checks; the training starts only from the dialog's
   "Start training" button (confirm: true with the plan's token). While it runs the section shows the phase, the epoch, the time left and the end of the log,
   and a button that cancels. The avatar keeps working with the other engines meanwhile. Each engine also has a "Set up" panel: every prerequisite as a list with what
   fixes it (an Install button for the weights, which runs the catalog's own plan, licence acknowledgement and download), the Python of the engines (chosen here, kept by
   the voice service), a check of what that Python has, and the export of the trained model for the phone. Everything the server says enters the page as text.
   voiceavatar.js gives it a node and calls paint(node, ctx) on its poll. */
(function () {
  const ENGINES = ["ultralight", "feathertalk", "er_nerf"];
  const LIST_MS = 30000;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const clear = node => { while (node.firstChild) node.firstChild.remove(); return node; };
  const words = o => (o && (o[LANG] || o.en)) || "";
  const msgOf = r => {
    const e = r.data && r.data.error;
    if (typeof e === "string") return e;
    return (e && ((LANG === "it" && e.message_it) || e.message)) || (r.status === 0 ? T("vtr.e.offline") : `HTTP ${r.status}`);
  };
  const clock = s => {
    const m = Math.max(1, Math.round(s / 60));
    return m >= 60 ? T("vtr.h", Math.floor(m / 60), m % 60) : T("vtr.min", m);
  };

  let ctx = null, node = null, avatars = [], listAt = 0, status = null, avatar = "", asr = "wenet", epochs = 200, fresh = false;
  let planning = "", dialog = null, plan = null, starting = false, msg = { text: "", bad: false }, sig = "";
  const setups = {}, opened = {}, pyText = {}, setupMsg = {}, working = {};       // per engine: the Set up answer, whether its panel is open, the Python being typed, the line under it
  let install = null, installing = "", dl = null, idialog = null;
  const mb = n => `${Math.max(1, Math.round((n || 0) / 1048576))} MB`;

  async function refreshList() {
    listAt = Date.now();
    const r = await ctx.svc("GET", "/avatar/list");
    if (r.ok && r.data) avatars = (r.data.avatars || []).filter(a => (a.clips || []).length);
    if (!avatars.some(a => a.name === avatar)) avatar = avatars.length ? avatars[0].name : "";
  }
  async function refreshStatus() {
    const r = await ctx.svc("GET", "/avatar/train/status");
    if (r.ok) status = r.data;
  }
  const running = () => !!status && status.state === "running";

  function note(text, bad) {
    const p = el("p", "vt-help vtr-note", text || "");
    p.dataset.bad = bad ? "1" : "";
    p.hidden = !text;
    return p;
  }

  async function ask(engine) {
    planning = engine; msg = { text: "", bad: false }; paintNow();
    const body = engine === "er_nerf" ? { avatar, engine } : { avatar, engine, epochs, fresh };       // ER-NeRF has no epochs or audio model to choose
    if (engine === "ultralight") body.asr = asr;
    const r = await ctx.svc("POST", "/avatar/train/plan", body);
    planning = "";
    if (!r.ok) { msg = { text: msgOf(r), bad: true }; paintNow(); return; }
    plan = r.data;
    openDialog();
    paintNow();
  }

  async function start() {
    if (!plan || starting) return;
    starting = true; paintDialog();
    const r = await ctx.svc("POST", "/avatar/train/start", { token: plan.token, confirm: true });
    starting = false;
    if (!r.ok) {
      const fresher = r.data && r.data.plan;
      if (fresher) plan = fresher;
      msg = { text: msgOf(r), bad: true };
      paintDialog();
      return;
    }
    status = r.data;
    closeDialog();
    msg = { text: "", bad: false };
    paintNow();
  }
  async function cancel() {
    const r = await ctx.svc("POST", "/avatar/train/cancel");
    if (!r.ok) msg = { text: msgOf(r), bad: true };
    await refreshStatus();
    paintNow();
  }

  // ---------------------------------------------------------------- the confirm dialog
  function openDialog() {
    closeDialog();
    dialog = el("dialog", "vtr-dialog");
    dialog.setAttribute("aria-labelledby", "vtr-dlg-h");
    dialog.addEventListener("cancel", () => { dialog = null; });
    dialog.addEventListener("close", () => { if (dialog === null || !dialog.isConnected) return; dialog.remove(); dialog = null; plan = null; });
    document.body.append(dialog);
    if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
    paintDialog();
  }
  function closeDialog() {
    if (!dialog) return;
    const d = dialog; dialog = null;
    if (typeof d.close === "function" && d.open) d.close();
    d.remove();
    plan = null;
  }
  const list = (items, cls) => { const ul = el("ul", cls || "vtr-list"); items.forEach(i => ul.append(el("li", "", i))); return ul; };

  function paintDialog() {
    if (!dialog || !plan) return;
    clear(dialog);
    const p = plan, box = el("div", "vtr-dlg");
    const h = el("h4", "vtr-h", T("vtr.dlg.title", p.name));
    h.id = "vtr-dlg-h";
    box.append(h);
    if (p.problems && p.problems.length) {
      box.append(el("h5", "vtr-sub", T("vtr.problems")), list(p.problems.map(words)));
    }
    if (p.video) {
      box.append(el("p", "vtr-text", T("vtr.video", p.video.clip, clock(p.video.seconds), p.video.fps)));
    }
    if (p.estimate) {
      const e = p.estimate, lo = e.seconds[0], hi = e.seconds[1];
      box.append(el("h5", "vtr-sub", T("vtr.est.title")),
        el("p", "vtr-text vtr-strong", lo === hi ? T("vtr.est.one", clock(lo)) : T("vtr.est.range", clock(lo), clock(hi))),
        el("p", "vtr-text", words(e.note)));
      if (p.resume_epoch) box.append(el("p", "vtr-text", T("vtr.resume", p.resume_epoch)));
    }
    box.append(el("h5", "vtr-sub", T("vtr.vram.title")), el("p", "vtr-text", words(p.vram)));
    box.append(el("h5", "vtr-sub", T("vtr.creates")), list((p.creates || []).map(words)));
    box.append(el("h5", "vtr-sub", T("vtr.checks")));
    const checks = el("ul", "vtr-list vtr-checks");
    for (const c of p.checks || []) {
      const li = el("li", "vtr-check");
      li.dataset.ok = c.ok ? "1" : "0";
      li.append(el("strong", "vtr-mark", c.ok ? T("vtr.check.ok") : T("vtr.check.no")), document.createTextNode(" " + (LANG === "it" ? c.it : c.en)));
      checks.append(li);
    }
    box.append(checks);
    box.append(note(msg.text, msg.bad));
    const nav = el("div", "vtr-nav");
    const go = el("button", "btn", starting ? T("vtr.starting") : T("vtr.start"));
    go.type = "button"; go.id = "vtr-start"; go.disabled = starting || !p.ok; go.onclick = start;
    const close = el("button", "btn", T("vtr.close"));
    close.type = "button"; close.id = "vtr-close"; close.onclick = () => { closeDialog(); paintNow(); };
    nav.append(go, close);
    box.append(nav);
    dialog.append(box);
  }

  // ---------------------------------------------------------------- the section
  function controls(box) {
    const f = el("div", "vt-field"), lab = el("label", "", T("vtr.avatar")), sel = el("select");
    sel.id = "vtr-avatar"; lab.htmlFor = sel.id; sel.disabled = running();
    for (const a of avatars) { const o = el("option", "", a.display_name || a.name); o.value = a.name; sel.append(o); }
    sel.value = avatar;
    sel.onchange = () => { avatar = sel.value; };
    f.append(lab, sel);
    const g = el("div", "vt-field"), glab = el("label", "", T("vtr.epochs")), num = el("input");
    num.type = "number"; num.id = "vtr-epochs"; num.min = "1"; num.max = "400"; num.step = "1"; num.inputMode = "numeric"; num.value = String(epochs); glab.htmlFor = num.id;
    num.disabled = running();
    num.onchange = () => { const v = parseInt(num.value, 10); epochs = Number.isFinite(v) ? Math.min(400, Math.max(1, v)) : 200; num.value = String(epochs); };
    g.append(glab, num, el("p", "vt-help", T("vtr.epochs.help")));
    const h = el("div", "vt-field"), hlab = el("label", "", T("vtr.asr")), hs = el("select");
    hs.id = "vtr-asr"; hlab.htmlFor = hs.id; hs.disabled = running();
    for (const k of ["wenet", "hubert"]) { const o = el("option", "", T("vtr.asr." + k)); o.value = k; hs.append(o); }
    hs.value = asr; hs.onchange = () => { asr = hs.value; };
    h.append(hlab, hs);
    const c = el("label", "vtr-fresh"), cb = el("input");
    cb.type = "checkbox"; cb.id = "vtr-fresh"; cb.checked = fresh; cb.disabled = running(); cb.onchange = () => { fresh = cb.checked; };
    c.append(cb, el("span", "", T("vtr.fresh")));
    box.append(f, g, h, c);
  }

  // ---------------------------------------------------------------- the Set up panel
  async function loadSetup(engine, probe) {
    working[engine] = probe ? "probe" : "load";
    paintNow();
    const body = { engine, probe: !!probe };
    if (avatar) body.avatar = avatar;
    if (engine === "ultralight") body.asr = asr;
    const r = await ctx.svc("POST", "/avatar/train/setup", body);
    working[engine] = "";
    if (r.ok) { setups[engine] = r.data; if (!probe) setupMsg[engine] = setupMsg[engine] && setupMsg[engine].keep ? setupMsg[engine] : { text: "", bad: false }; }
    else setupMsg[engine] = { text: msgOf(r), bad: true };
    paintNow();
  }
  async function savePython(engine, path) {
    working[engine] = "save";
    const r = await ctx.svc("POST", "/avatar/train/python", { path });
    working[engine] = "";
    setupMsg[engine] = r.ok ? { text: T("vtr.python.saved"), bad: false, keep: true } : { text: msgOf(r), bad: true };
    delete pyText[engine];
    await loadSetup(engine, false);
  }
  async function exportAgain(engine) {
    working[engine] = "export";
    paintNow();
    const r = await ctx.svc("POST", "/avatar/train/export", { avatar, engine });
    working[engine] = "";
    setupMsg[engine] = r.ok ? { text: T("vtr.export.done", r.data.files), bad: false, keep: true } : { text: msgOf(r), bad: true };
    await loadSetup(engine, false);
  }
  function checkList(items) {
    const ul = el("ul", "vtr-list vtr-checks");
    for (const c of items) {
      const li = el("li", "vtr-check");
      li.dataset.ok = c.ok ? "1" : "0";
      li.append(el("strong", "vtr-mark", c.ok ? T("vtr.check.ok") : T("vtr.check.no")), document.createTextNode(" " + (LANG === "it" ? c.it : c.en)));
      ul.append(li);
    }
    return ul;
  }
  function setupPanel(engine) {
    const d = el("details", "vtr-setup");
    d.id = "vtr-setup-" + engine; d.open = !!opened[engine];
    d.addEventListener("toggle", () => { opened[engine] = d.open; if (d.open && !setups[engine] && !working[engine]) loadSetup(engine, false); });
    d.append(el("summary", "", T("vtr.setup")));
    const s = setups[engine];
    if (!s) { d.append(el("p", "vtr-text", T("vtr.setup.loading")), note((setupMsg[engine] || {}).text, true)); return d; }
    d.append(el("p", "vtr-text", T("vtr.setup.intro")));
    const list = el("ul", "vtr-list vtr-checks");
    for (const c of s.items) {
      const li = el("li", "vtr-check");
      li.dataset.ok = c.ok ? "1" : "0"; li.dataset.item = c.id;
      li.append(el("strong", "vtr-mark", c.ok ? T("vtr.check.ok") : T("vtr.check.no")), document.createTextNode(" " + (LANG === "it" ? c.it : c.en)));
      if (!c.ok && c.install) {
        const b = el("button", "btn", T("vtr.item.install"));
        b.type = "button"; b.id = `vtr-install-${engine}-${c.id}`; b.disabled = !!installing;
        b.onclick = () => openInstall(c.install);
        li.append(document.createElement("br"), b);
      }
      list.append(li);
    }
    d.append(el("h5", "vtr-sub", T("vtr.items.title")), list);
    // the Python of the engines
    const f = el("div", "vt-field"), lab = el("label", "", T("vtr.python.label")), inp = el("input");
    inp.type = "text"; inp.id = "vtr-python-" + engine; inp.autocomplete = "off"; inp.spellcheck = false; lab.htmlFor = inp.id;
    inp.value = pyText[engine] !== undefined ? pyText[engine] : s.python.path;
    inp.oninput = () => { pyText[engine] = inp.value; };
    const where = s.python.source === "settings" ? T("vtr.python.src.settings") : s.python.source === "env" ? T("vtr.python.src.env", s.python.env) : T("vtr.python.src.none", s.python.env);
    f.append(lab, inp, el("p", "vt-help", T("vtr.python.help") + " " + where));
    const save = el("button", "btn", T("vtr.python.save")); save.type = "button"; save.id = "vtr-python-save-" + engine; save.disabled = working[engine] === "save";
    save.onclick = () => savePython(engine, inp.value.trim());
    const probe = el("button", "btn", working[engine] === "probe" ? T("vtr.env.checking") : T("vtr.env.check")); probe.type = "button"; probe.id = "vtr-env-" + engine;
    probe.disabled = working[engine] === "probe" || !s.python.path;
    probe.onclick = () => loadSetup(engine, true);
    const nav = el("div", "vtr-nav"); nav.append(save, probe);
    d.append(el("h5", "vtr-sub", T("vtr.python.title")), f, nav);
    if (s.environment) d.append(el("h5", "vtr-sub", T("vtr.env.title")), s.environment.length ? checkList(s.environment) : el("p", "vtr-text", T("vtr.env.none")));
    // the export for the phone
    d.append(el("h5", "vtr-sub", T("vtr.export.title")));
    if (!avatar) d.append(el("p", "vtr-text", T("vtr.export.noavatar")));
    else if (!s.trained) d.append(el("p", "vtr-text", T("vtr.export.none", T("vtr.engine." + engine))));
    else {
      const t = s.trained;
      d.append(el("p", "vtr-text", T("vtr.export.trained", avatar, t.epochs) + " " + (t.exported ? T("vtr.export.yes") : T("vtr.export.no")) + (t.export_error ? " " + T("vtr.export.err", t.export_error) : "")));
      const b = el("button", "btn", working[engine] === "export" ? T("vtr.export.working") : T("vtr.export.button"));
      b.type = "button"; b.id = "vtr-export-" + engine; b.disabled = working[engine] === "export" || running();
      b.onclick = () => exportAgain(engine);
      d.append(b);
    }
    d.append(note((setupMsg[engine] || {}).text, (setupMsg[engine] || {}).bad));
    const re = el("button", "btn", T("vtr.setup.refresh")); re.type = "button"; re.id = "vtr-refresh-" + engine; re.onclick = () => loadSetup(engine, false);
    d.append(re);
    return d;
  }

  // ---------------------------------------------------------------- installing a weight (the catalog's own plan and download)
  async function openInstall(entry) {
    install = { entry, plan: null, ack: false, nc: false, msg: "", starting: false, loading: true };
    openInstallDialog();
    const r = await ctx.api("GET", "api/voice/download/plan?id=" + encodeURIComponent(entry));
    if (!install) return;
    install.loading = false;
    if (r.ok) install.plan = r.data; else install.msg = msgOf(r);
    paintInstall();
  }
  function openInstallDialog() {
    closeInstallDialog();
    idialog = el("dialog", "vtr-dialog");
    idialog.setAttribute("aria-labelledby", "vtr-inst-h");
    idialog.addEventListener("close", () => { if (idialog) { idialog.remove(); idialog = null; install = null; } });
    document.body.append(idialog);
    if (typeof idialog.showModal === "function") idialog.showModal(); else idialog.setAttribute("open", "");
    paintInstall();
  }
  function closeInstallDialog() {
    if (!idialog) return;
    const d = idialog; idialog = null;
    if (typeof d.close === "function" && d.open) d.close();
    d.remove();
    install = null;
  }
  function paintInstall() {
    if (!idialog || !install) return;
    clear(idialog);
    const i = install, p = i.plan, box = el("div", "vtr-dlg");
    const h = el("h4", "vtr-h", T("vtr.inst.title", p ? p.name : i.entry));
    h.id = "vtr-inst-h";
    box.append(h);
    if (i.loading) box.append(el("p", "vtr-text", T("vtr.inst.loading")));
    if (p) {
      box.append(el("p", "vtr-text", T("vtr.inst.files", p.files.length, mb(p.remaining_bytes), mb(p.total_bytes))), el("p", "vtr-text", T("vtr.inst.dest", p.folder)));
      const lic = p.license || {};
      box.append(el("h5", "vtr-sub", T("vtr.inst.license", lic.name || "")), el("p", "vtr-text", words(lic)));
      if (lic.text) box.append(el("pre", "vtr-log", lic.text));
      if (p.problems && p.problems.length) {
        box.append(el("h5", "vtr-sub", T("vtr.inst.problems")));
        const ul = el("ul", "vtr-list");
        p.problems.forEach(x => ul.append(el("li", "", (LANG === "it" ? x.text_it : x.text) || x.text)));
        box.append(ul);
      }
      const ready = p.ok;
      const check = (id, text, key) => {
        const row = el("label", "vtr-fresh"), input = el("input");
        input.type = "checkbox"; input.id = id; input.checked = i[key]; input.disabled = !ready;
        input.onchange = () => { i[key] = input.checked; paintInstall(); };
        row.append(input, el("span", "", text));
        return row;
      };
      box.append(check("vtr-inst-ack", T("vtr.inst.ack"), "ack"));
      if (p.noncommercial) box.append(check("vtr-inst-nc", T("vtr.inst.nc"), "nc"));
      box.append(el("p", "vtr-text", T("vtr.inst.nothing")));
    }
    box.append(note(i.msg, true));
    const nav = el("div", "vtr-nav"), go = el("button", "btn", i.starting ? T("vtr.starting") : T("vtr.inst.go"));
    go.type = "button"; go.id = "vtr-inst-go"; go.disabled = i.starting || !p || !p.ok || !i.ack || (p.noncommercial && !i.nc);
    go.onclick = installGo;
    const close = el("button", "btn", T("vtr.close"));
    close.type = "button"; close.id = "vtr-inst-close"; close.onclick = () => { closeInstallDialog(); paintNow(); };
    nav.append(go, close);
    box.append(nav);
    idialog.append(box);
  }
  async function installGo() {
    const i = install;
    if (!i || !i.plan || i.starting) return;
    i.starting = true; paintInstall();
    const r = await ctx.api("POST", "api/voice/download", { id: i.entry, token: i.plan.token, confirm: true, ack: i.ack, noncommercial: i.nc });
    if (!install) return;
    i.starting = false;
    if (!r.ok) { if (r.data && r.data.plan) i.plan = r.data.plan; i.msg = msgOf(r); paintInstall(); return; }
    installing = i.entry; dl = r.data;
    closeInstallDialog();
    paintNow();
  }
  async function pollDownload() {
    if (!installing) return;
    const r = await ctx.api("GET", "api/voice/download/status");
    if (!r.ok) return;
    dl = r.data;
    if (["done", "failed", "cancelled", "idle"].includes(dl.phase)) {
      const fin = dl.phase;
      installing = "";
      if (fin === "failed") msg = { text: T("vtr.inst.failed", (dl.error && (LANG === "it" ? dl.error.message_it : dl.error.message)) || ""), bad: true };
      for (const e of ENGINES) if (setups[e]) await loadSetup(e, false);
    }
  }
  function downloadLine(box) {
    if (!installing || !dl) return;
    box.append(el("p", "vtr-text vtr-strong", T("vtr.inst.running", dl.name || installing, mb(dl.bytes_done), mb(dl.bytes_total))));
  }

  function engineCard(engine) {
    const card = el("article", "vtr-engine");
    card.dataset.engine = engine;
    card.append(el("h5", "vtr-engine-h", T("vtr.engine." + engine)), el("p", "vtr-text", T("vtr.engine." + engine + ".help")));
    const b = el("button", "btn", planning === engine ? T("vtr.planning") : T("vtr.plan"));
    b.type = "button"; b.id = "vtr-plan-" + engine; b.disabled = !avatar || running() || !!planning;
    b.onclick = () => ask(engine);
    card.append(b, setupPanel(engine));
    return card;
  }

  function runView(box) {
    const s = status, view = el("div", "vtr-run");
    view.setAttribute("role", "status");
    view.append(el("p", "vtr-text vtr-strong", T("vtr.state.running", T("vtr.engine." + s.engine), s.avatar)));
    view.append(el("p", "vtr-text", T("vtr.phase." + s.phase)));
    const bar = el("progress", "vtr-bar"); bar.max = 100; bar.value = s.percent || 0; bar.setAttribute("aria-label", T("vtr.phase." + s.phase));
    view.append(bar, el("p", "vtr-text", T("vtr.percent", Math.round(s.percent || 0)) + (s.phase === "train" && s.epoch ? " · " + T("vtr.epoch", s.epoch, s.epochs) : "") + (s.eta_s ? " · " + T("vtr.eta", clock(s.eta_s)) : "")));
    const stop = el("button", "btn", s.cancelling ? T("vtr.cancelling") : T("vtr.cancel"));
    stop.type = "button"; stop.id = "vtr-cancel"; stop.disabled = !!s.cancelling; stop.onclick = cancel;
    view.append(stop);
    if ((s.log || []).length) {
      const d = el("details", "vtr-more"), sm = el("summary", "", T("vtr.log")), pre = el("pre", "vtr-log", s.log.join("\n"));
      d.append(sm, pre); view.append(d);
    }
    box.append(view);
  }

  function lastResult(box) {
    const s = status;
    if (!s || s.state === "idle" || s.state === "running") return;
    const text = s.state === "done" ? T("vtr.done", s.message) : s.state === "cancelled" ? T("vtr.cancelled", s.message) : T("vtr.failed", s.error || s.message);
    const p = el("p", "vtr-text vtr-result", T("vtr.last", T("vtr.engine." + s.engine), s.avatar) + " " + text);
    p.dataset.state = s.state;
    box.append(p);
    if (s.state === "failed" && (s.log || []).length) {
      const d = el("details", "vtr-more"), pre = el("pre", "vtr-log", s.log.join("\n"));
      d.append(el("summary", "", T("vtr.log")), pre); box.append(d);
    }
  }

  function signature() {
    const s = status || {};
    return JSON.stringify([avatars.map(a => a.name), avatar, asr, epochs, fresh, planning, !!dialog, s.state, s.phase, Math.round(s.percent || 0), s.epoch, s.eta_s, s.cancelling, (s.log || []).length, msg, starting, LANG, !!idialog, installing, dl && Math.round((dl.bytes_done || 0) / 1048576), Object.keys(setups).map(k => [k, setups[k] && setups[k].ok, (working[k] || ""), !!opened[k]])]);
  }
  function render() {
    if (!node) return;
    const box = clear(node);
    const sec = el("section", "vtr-card");
    sec.setAttribute("aria-labelledby", "vtr-h");
    const h = el("h4", "va-h", T("vtr.title"));
    h.id = "vtr-h";
    sec.append(h, el("p", "vt-help", T("vtr.intro")));
    if (!avatars.length && !running()) {
      sec.append(el("p", "vt-help vtr-none", T("vtr.noavatar")));
    } else {
      if (running()) runView(sec);
      controls(sec);
      const wrap = el("div", "vtr-engines");
      ENGINES.forEach(e => wrap.append(engineCard(e)));
      downloadLine(sec);
      sec.append(wrap, note(msg.text, msg.bad));
      lastResult(sec);
    }
    box.append(sec);
  }
  function paintNow() { sig = signature(); render(); if (dialog) paintDialog(); }

  async function paint(target, context) {
    if (node !== target) { node = target; sig = ""; }
    ctx = context;
    if (!listAt || Date.now() - listAt > LIST_MS) await refreshList();
    const was = status && status.state;
    await refreshStatus();
    await pollDownload();
    if (was === "running" && status && status.state !== "running") await refreshList();
    if (signature() !== sig) paintNow();
  }
  function stop() { closeDialog(); closeInstallDialog(); }

  document.addEventListener("tadamoo:view", e => { if (e.detail !== "voice") stop(); });
  window.VoiceTrain = { paint, stop, _state: () => ({ status, plan, avatar }) };
})();
