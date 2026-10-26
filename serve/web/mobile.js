"use strict";
/* Settings > Phones: install and connect Tailscale, allow phones, make and take away the keys of the phones, and show a key as a QR code for the app to read.
   A key's code is shown when it is made and can be shown again from the list (the server keeps the key encrypted); one made before that
   is shown once only. Everything shown enters the page as text. */
(function () {
  const host = document.getElementById("mb-root");
  if (!host) return;

  const POLL_MS = 3000;
  let S = null, shown = null, armed = "", timer = 0, note = "", upInfo = null, armTimer = 0;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick, tip) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; if (tip) b.title = tip; return b; };
  const errText = r => (r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`;
  const when = ts => (ts ? new Date(ts * 1000).toLocaleString(LANG === "it" ? "it-IT" : "en-GB", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "-");

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
    timer = setTimeout(async () => { await refresh(); poll(); }, POLL_MS);
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
    cb.type = "checkbox"; cb.checked = S.enabled; cb.disabled = S.tailscale.state !== "Running";
    if (cb.disabled) cb.title = T("mb.allow.needts");
    cb.onchange = async () => { const r = await api("POST", `api/remote/${cb.checked ? "enable" : "disable"}`, {}); if (!r.ok) note = errText(r); await refresh(); };
    label.append(cb, el("span", "", T("mb.allow.label")));
    box.append(label, el("p", "help", T("mb.allow.help")));
    if (S.listening) box.append(el("p", "mb-ok", T("mb.allow.on", S.address)));
    else if (S.enabled && S.error) box.append(el("p", "mb-bad", S.error));
    return box;
  }

  function keysBlock() {
    const box = el("div", "mb-block");
    box.append(el("h3", "", T("mb.keys.title")), el("p", "help", T("mb.keys.help")));
    const list = el("ul", "mb-keys");
    S.keys.forEach(k => {
      const li = el("li", "mb-key"), sure = armed === k.id;
      li.append(el("span", "mb-kname", k.label), el("span", "mb-kmeta", T("mb.keys.meta", when(k.created), k.last_used ? when(k.last_used) : T("mb.keys.never")) + (k.recoverable ? "" : " · " + T("mb.keys.noshow"))));
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
    name.id = "mb-name"; name.maxLength = 60; name.autocomplete = "off"; name.placeholder = T("mb.keys.ph");
    const lab = el("label", "sr", T("mb.keys.name")); lab.htmlFor = "mb-name";
    form.onsubmit = async e => {
      e.preventDefault();
      const r = await api("POST", "api/remote/keys/create", { label: name.value });
      if (!r.ok) { note = errText(r); paint(); return; }
      shown = { key: r.data.key, label: r.data.row.label, again: !!r.data.row.recoverable };
      note = "";
      S = r.data;
      paint();
    };
    form.append(lab, name, go);
    box.append(form);
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
    const parts = [el("p", "help", T("mb.intro")), tailscaleBlock(), switchBlock(), keysBlock()];
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
