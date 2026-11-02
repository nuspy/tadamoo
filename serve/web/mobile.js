"use strict";
/* Settings > Phones: install and connect Tailscale, allow phones, make and take away the keys of the phones, and show a key as a QR code for the app to read.
   A key's code is shown when it is made and can be shown again from the list (the server keeps the key encrypted); one made before that
   is shown once only. Everything shown enters the page as text. */
(function () {
  const host = document.getElementById("mb-root");
  if (!host) return;

  const POLL_MS = 3000;
  let S = null, shown = null, armed = "", timer = 0, note = "", upInfo = null, armTimer = 0, days = "", limitsNote = "", listenNote = "", listenDraft = null;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick, tip) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; if (tip) b.title = tip; return b; };
  const errText = r => (r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`;
  const when = ts => (ts ? new Date(ts * 1000).toLocaleString(LANG === "it" ? "it-IT" : "en-GB", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "-");

  const endText = k => (!k.expires ? T("mb.keys.noend") : k.expired ? T("mb.keys.expired", when(k.expires)) : T("mb.keys.ends", when(k.expires)));
  const DURATIONS = [30, 90, 365, 0];

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const active = () => document.body.dataset.view === "settings" && !document.getElementById("view-settings").hidden && !document.getElementById("mb-section").hidden;

  async function refresh() {
    const r = await api("GET", "api/remote");
    if (!r.ok) { note = errText(r); paint(); return; }
    S = r.data;
    paint();
  }
  function poll() {
    clearTimeout(timer);
    if (!active() || document.hidden) return;
    // a box being typed in is not redrawn under the hands of the person
    timer = setTimeout(async () => { if (!host.contains(document.activeElement) || !document.activeElement.matches("input[type=text],textarea,select")) await refresh(); poll(); }, POLL_MS);
  }

  /* ---- the pieces ---- */
  function tailscaleBlock() {
    const t = S.tailscale, box = el("div", "mb-block");
    box.append(el("h3", "", T("mb.ts.title")));
    if (S.install && S.install.state === "running") { box.append(el("p", "", T("mb.ts.installing"))); return box; }
    if (!t.installed) {
      box.append(el("p", "", T("mb.ts.none")));
      box.append(btn(T("mb.ts.install"), "primary", async () => { const r = await api("POST", "api/remote/tailscale/install", {}); if (!r.ok) note = errText(r); await refresh(); poll(); }, T("mb.ts.install.tip")));
      if (S.install && S.install.state === "failed") box.append(el("p", "mb-bad", T("mb.ts.install.failed", S.install.output || "")));
      box.append(el("p", "help", T("mb.ts.manual")));
      return box;
    }
    if (t.state !== "Running") {
      box.append(el("p", "", T("mb.ts.off", t.state)));
      box.append(btn(T("mb.ts.connect"), "primary", async () => { const r = await api("POST", "api/remote/tailscale/up", {}); upInfo = r.ok ? r.data.up : { message: errText(r) }; await refresh(); poll(); }, T("mb.ts.connect.tip")));
      if (upInfo && upInfo.url) {
        const p = el("p", "");
        p.append(T("mb.ts.signin") + " ", sourceLinkOf(upInfo.url));
        box.append(p);
      } else if (upInfo && upInfo.message) box.append(el("p", "help", upInfo.message));
      return box;
    }
    upInfo = null;
    box.append(el("p", "mb-ok", T("mb.ts.on", t.ip, t.dns_name || "-")));
    return box;
  }
  const sourceLinkOf = url => (window.chatUi && window.chatUi.sourceLink ? window.chatUi.sourceLink(url, T("mb.ts.open")) : el("a", "", url));

  function switchBlock() {
    const box = el("div", "mb-block");
    box.append(el("h3", "", T("mb.allow.title")));
    const label = el("label", "mb-check"), cb = el("input");
    cb.type = "checkbox"; cb.checked = S.enabled; cb.disabled = S.listen === "tailscale" && S.tailscale.state !== "Running";
    if (cb.disabled) cb.title = T("mb.allow.needts");
    cb.onchange = async () => { const r = await api("POST", `api/remote/${cb.checked ? "enable" : "disable"}`, {}); if (!r.ok) note = errText(r); await refresh(); };
    label.append(cb, el("span", "", T("mb.allow.label")));
    box.append(label, el("p", "help", T("mb.allow.help")));
    if (S.listening) box.append(el("p", "mb-ok", T("mb.allow.on", S.address)));
    else if (S.enabled && S.error) box.append(el("p", "mb-bad", S.error));
    box.append(listenBlock());
    return box;
  }

  function listenBlock() {
    const box = el("div", "mb-listen"), cur = S.listen || "tailscale";
    const draft = listenDraft || { kind: cur === "tailscale" || cur === "0.0.0.0" ? cur : "custom", ip: cur === "tailscale" || cur === "0.0.0.0" ? "" : cur };
    const sel = el("select"), lab = el("label", "", T("mb.listen.label")), ip = el("input"), ipLab = el("label", "", T("mb.listen.ip"));
    sel.id = "mb-listen"; lab.htmlFor = sel.id; ip.id = "mb-listen-ip"; ipLab.htmlFor = ip.id;
    [["tailscale", "mb.listen.tailscale"], ["0.0.0.0", "mb.listen.all"], ["custom", "mb.listen.custom"]].forEach(([v, key]) => { const o = el("option", "", T(key)); o.value = v; sel.append(o); });
    sel.value = draft.kind; ip.type = "text"; ip.value = draft.ip; ip.placeholder = "192.168.1.20"; ip.inputMode = "decimal"; ip.autocomplete = "off"; ip.spellcheck = false;
    const HELP = { "tailscale": "mb.listen.help.tailscale", "0.0.0.0": "mb.listen.help.all", "custom": "mb.listen.help.custom" };
    const help = el("p", "help");
    const sync = () => { ip.hidden = ipLab.hidden = sel.value !== "custom"; help.textContent = T(HELP[sel.value]); listenDraft = { kind: sel.value, ip: ip.value }; };
    sel.onchange = () => { listenNote = ""; sync(); }; ip.oninput = () => { listenNote = ""; sync(); }; sync();
    const save = btn(T("mb.listen.save"), "primary", async () => {
      const r = await api("POST", "api/remote/listen", { listen: sel.value === "custom" ? ip.value : sel.value });
      listenNote = r.ok ? T("mb.listen.saved") : errText(r);
      if (r.ok) { S = r.data; listenDraft = null; }
      paint();
    }, T("mb.listen.save.tip"));
    box.append(lab, sel, help, ipLab, ip, save);
    if (listenNote) { box.append(el("p", listenNote === T("mb.listen.saved") ? "mb-ok" : "mb-bad", listenNote)); }
    return box;
  }

  function keysBlock() {
    const box = el("div", "mb-block");
    box.append(el("h3", "", T("mb.keys.title")), el("p", "help", T("mb.keys.help")));
    const list = el("ul", "mb-keys");
    S.keys.forEach(k => {
      const li = el("li", "mb-key"), sure = armed === k.id;
      li.append(el("span", "mb-kname", k.label), el("span", "mb-kmeta", T("mb.keys.meta", when(k.created), k.last_used ? when(k.last_used) : T("mb.keys.never")) + (k.recoverable ? "" : " · " + T("mb.keys.noshow"))));
      const end = el("span", k.expired ? "mb-kmeta mb-bad" : "mb-kmeta", endText(k));
      li.append(end);
      li.append(btn(T("mb.keys.renew"), "ghost", async () => {
        const n = Number(days === "" ? S.limits.key_days : days);
        const r = await api("POST", "api/remote/keys/renew", { id: k.id, days: n }); if (!r.ok) note = errText(r); await refresh();
      }, T("mb.keys.renew.tip")));
      if (k.recoverable) li.append(btn(T("mb.keys.show"), "ghost", async () => {
        const r = await api("POST", "api/remote/keys/show", { id: k.id });
        if (!r.ok) { note = errText(r); paint(); return; }
        shown = { key: r.data.key, label: r.data.row.label, again: true }; note = ""; S = r.data; paint();
      }, T("mb.keys.show.tip")));
      li.append(btn(sure ? T("mb.keys.revoke.sure") : T("mb.keys.revoke"), sure ? "mb-sure" : "ghost", async () => {
          if (!sure) { armed = k.id; clearTimeout(armTimer); armTimer = setTimeout(() => { armed = ""; paint(); }, 4000); paint(); return; }
          armed = ""; const r = await api("POST", "api/remote/keys/revoke", { id: k.id }); if (!r.ok) note = errText(r); await refresh();
        }, sure ? T("mb.keys.revoke.tip2") : T("mb.keys.revoke.tip")));
      list.append(li);
    });
    box.append(S.keys.length ? list : el("p", "dim", T("mb.keys.none")));
    const form = el("form", "mb-new"), name = el("input"), go = btn(T("mb.keys.make"), "primary", () => form.requestSubmit(), T("mb.keys.make.tip"));
    const len = el("select"), lenLab = el("label", "sr", T("mb.keys.len")), warn = el("p", "mb-bad", T("mb.keys.neverwarn"));
    len.id = "mb-len"; lenLab.htmlFor = "mb-len"; len.title = T("mb.keys.len.tip");
    DURATIONS.forEach(n => { const o = el("option", "", n ? T("mb.keys.days", n) : T("mb.keys.forever")); o.value = String(n); len.append(o); });
    len.value = days === "" ? String(S.limits.key_days) : String(days);
    warn.hidden = len.value !== "0";
    len.onchange = () => { days = len.value; warn.hidden = len.value !== "0"; };
    name.id = "mb-name"; name.maxLength = 60; name.autocomplete = "off"; name.placeholder = T("mb.keys.ph");
    const lab = el("label", "sr", T("mb.keys.name")); lab.htmlFor = "mb-name";
    form.onsubmit = async e => {
      e.preventDefault();
      const r = await api("POST", "api/remote/keys/create", { label: name.value, days: Number(len.value) });
      if (!r.ok) { note = errText(r); paint(); return; }
      shown = { key: r.data.key, label: r.data.row.label, again: !!r.data.row.recoverable };
      note = "";
      S = r.data;
      paint();
    };
    form.append(lab, name, lenLab, len, go);
    box.append(form, el("p", "help", S.limits.key_days ? T("mb.keys.len.help", S.limits.key_days) : T("mb.keys.len.help.none")), warn);
    return box;
  }

  function limitsBlock() {
    const box = el("div", "mb-block mb-limits"), L = S.limits;
    box.append(el("h3", "", T("mb.lim.title")), el("p", "help", T("mb.lim.help")));
    const chooses = el("input"), choosesLab = el("label", "mb-check", T("mb.lim.chooses"));
    chooses.type = "checkbox"; chooses.id = "mb-chooses"; chooses.checked = !!L.phone_chooses; choosesLab.htmlFor = chooses.id; choosesLab.prepend(chooses);
    const mode = el("select"), modeLab = el("label", "", T("mb.lim.mode")), rules = el("textarea"), rulesLab = el("label", "", T("mb.lim.rules"));
    mode.id = "mb-maxmode"; modeLab.htmlFor = mode.id; rules.id = "mb-rules"; rulesLab.htmlFor = rules.id;
    L.modes.forEach(m => { const o = el("option", "", T("mb.mode." + m)); o.value = m; mode.append(o); });
    mode.value = L.max_mode; rules.value = L.rules.join("\n"); rules.rows = 3; rules.spellcheck = false; rules.placeholder = "shell_command:pytest *";
    const help = el("p", "help", T("mb.lim.mode.help." + L.max_mode));
    mode.onchange = () => { help.textContent = T("mb.lim.mode.help." + mode.value); };
    const fixed = el("div", "mb-fixed");                       // what applies only while phones may not choose
    const lock = () => { fixed.hidden = chooses.checked; };
    chooses.onchange = lock; lock();
    const save = btn(T("mb.lim.save"), "primary", async () => {
      const r = await api("POST", "api/remote/limits", { phone_chooses: chooses.checked, max_mode: mode.value, rules: rules.value.split("\n").map(x => x.trim()).filter(Boolean) });
      limitsNote = r.ok ? T("mb.lim.saved") : errText(r); if (r.ok) S = r.data; paint();
    }, T("mb.lim.save.tip"));
    fixed.append(modeLab, mode, help, rulesLab, rules, el("p", "help", T("mb.lim.rules.help")));
    box.append(choosesLab, el("p", "help", T("mb.lim.chooses.help")), fixed, save);
    if (limitsNote) box.append(el("p", "mb-ok", limitsNote));
    return box;
  }

  async function qrBlock() {
    const box = el("div", "mb-block mb-fresh");
    box.append(el("h3", "", T("mb.fresh.title", shown.label)), shown.again ? el("p", "help", T("mb.fresh.again")) : el("p", "mb-bad", T("mb.fresh.once")));
    const address = S.address || (S.tailscale.ip ? `${S.tailscale.ip}:${S.port}` : "");
    const payload = JSON.stringify({ v: 1, url: address ? `http://${address}` : "", key: shown.key, name: shown.label });
    const key = el("pre", "mb-keytext"); key.textContent = shown.key; key.tabIndex = 0;
    const copy = btn(T("mb.fresh.copy"), "", async () => { try { await navigator.clipboard.writeText(shown.key); copy.textContent = T("mb.fresh.copied"); } catch (e) { copy.textContent = T("mb.fresh.copyfail"); } }, T("mb.fresh.copy.tip"));
    const addr = el("p", "", T("mb.fresh.address", address || T("mb.fresh.noaddress")));
    box.append(addr, key, copy);
    if (address) {
      const canvas = el("canvas", "mb-qr"); canvas.setAttribute("role", "img"); canvas.setAttribute("aria-label", T("mb.fresh.qr"));
      box.append(canvas, el("p", "help", T("mb.fresh.qrhelp")));
      loadQr().then(() => {
        const qr = window.qrcode(0, "M"); qr.addData(payload); qr.make();
        const n = qr.getModuleCount(), cell = 5, margin = 4 * cell, size = n * cell + 2 * margin, ctx = canvas.getContext("2d");
        canvas.width = canvas.height = size;
        ctx.fillStyle = "#fff"; ctx.fillRect(0, 0, size, size);
        ctx.fillStyle = "#000";
        for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) if (qr.isDark(r, c)) ctx.fillRect(margin + c * cell, margin + r * cell, cell, cell);
      }).catch(() => { canvas.hidden = true; });
    }
    box.append(btn(T("mb.fresh.done"), "ghost", () => { shown = null; paint(); }));
    return box;
  }
  let qrReady = null;
  function loadQr() {
    if (window.qrcode) return Promise.resolve();
    if (!qrReady) qrReady = new Promise((ok, no) => { const s = document.createElement("script"); s.src = "web/vendor/qrcode.js"; s.onload = ok; s.onerror = () => { qrReady = null; no(new Error("qr")); }; document.head.append(s); });
    return qrReady;
  }

  function paint() {
    if (!S) { host.replaceChildren(el("p", "dim", note || T("mb.loading"))); return; }
    const parts = [el("p", "help", T("mb.intro")), tailscaleBlock(), switchBlock(), keysBlock(), limitsBlock()];
    if (shown) parts.splice(3, 0, el("div", "mb-slot"));
    if (note) parts.push(el("p", "mb-bad", note));
    parts.push(el("p", "help", T("mb.phone.help")));
    host.replaceChildren(...parts);
    if (shown) qrBlock().then(b => { const slot = host.querySelector(".mb-slot"); if (slot) slot.replaceWith(b); });
  }

  document.addEventListener("basalt:sub", e => { if (e.detail === "mobile") { refresh(); poll(); } else clearTimeout(timer); });
  document.addEventListener("basalt:view", e => { if (e.detail !== "settings") clearTimeout(timer); });
  document.addEventListener("visibilitychange", poll);
  applyT(document.getElementById("mb-section"));
  paint();
})();
