"use strict";
/* Telegram: Settings > Voice has the bot's section (the token, which is write-only, the switch, the permission level, the allow-list, the senders that
   were ignored, the home channel, the import from Hermes), and the header of Chat and Code has the button that moves the open conversation to a chat
   of Telegram and brings it back. The permission level is Telegram's own, kept by the server (/api/permission-level). Everything the server says
   enters the page as text. */
(function () {
  const host = document.getElementById("tgm-root"), moveBtn = document.getElementById("tgm-move");
  if (!host || !moveBtn) return;

  const POLL_MS = 3000, HEADER_MS = 15000;
  let S = null, L = null, failed = "", timer = 0, busy = false, last = 0;
  const ui = { token: "", tokenBad: false, level: "", levelBad: false, allow: "", allowBad: false, home: "", homeBad: false, imp: "", impBad: false, test: "", testBad: false, send: "", sendBad: false };

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };
  const why = r => (r.status === 0 ? T("tgm.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`));
  const here = () => document.body.dataset.view === "settings" && !document.getElementById("view-settings").hidden && !document.getElementById("tgm-section").hidden;

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  /* ---- Settings ---- */
  const note = (id, text, bad) => { const p = el("p", "tgm-note", text); p.id = id; p.setAttribute("role", "status"); p.dataset.bad = bad ? "1" : "0"; p.hidden = !text; return p; };
  const field = (id, label, input) => { const w = el("div", "tgm-field"), l = el("label", "", label); l.htmlFor = id; w.append(l, input); return w; };
  const named = (label, input) => { input.setAttribute("aria-label", label); const w = el("div", "tgm-field"); w.append(input); return w; };     // the heading above is the label
  const stateLine = () => {
    const box = el("div", "tgm-state"), dot = el("span", "tgm-dot"), word = el("b", "", T("tgm.state." + S.state));
    dot.dataset.state = S.state; dot.setAttribute("aria-hidden", "true");
    box.append(dot, word);
    if (S.bot) box.append(el("span", "tgm-bot", T("tgm.bot", S.bot)));
    const p = el("div", "tgm-statebox");
    p.append(box);
    if (S.message) p.append(el("p", "tgm-help tgm-statemsg", S.message));
    return p;
  };

  function tokenBox() {
    const box = el("div", "tgm-box"), input = el("input");
    input.type = "password"; input.id = "tgm-token"; input.autocomplete = "off"; input.spellcheck = false; input.placeholder = T("tgm.token.ph"); input.maxLength = 120;
    const row = el("div", "tgm-row");
    row.append(btn(T("tgm.save"), "primary", async () => {
      if (!input.value.trim()) { ui.token = T("tgm.token.empty"); ui.tokenBad = true; return paint(); }
      const r = await api("POST", "api/telegram/token", { token: input.value.trim() });
      input.value = "";
      ui.token = r.ok ? T("tgm.token.saved") : why(r); ui.tokenBad = !r.ok;
      if (r.ok) S = r.data;
      paint();
    }));
    if (S.configured) {
      row.append(btn(T("tgm.remove"), "", async () => {
        const r = await api("POST", "api/telegram/token", { token: "" });
        ui.token = r.ok ? T("tgm.token.removed") : why(r); ui.tokenBad = !r.ok;
        if (r.ok) S = r.data;
        paint();
      }));
      row.append(btn(T("tgm.test"), "", async () => {
        ui.test = T("tgm.testing"); ui.testBad = false; paint();
        const r = await api("POST", "api/telegram/test", {});
        ui.test = r.ok && r.data.ok ? T("tgm.test.ok", r.data.bot) : T("tgm.test.bad", r.ok ? r.data.message : why(r)); ui.testBad = !(r.ok && r.data.ok);
        paint();
      }));
    }
    box.append(el("h3", "tgm-h", T("tgm.token")), el("p", "tgm-help", S.configured ? T("tgm.token.set") : T("tgm.token.none")), named(T("tgm.token"), input), row,
               note("tgm-token-note", ui.token, ui.tokenBad), note("tgm-test-note", ui.test, ui.testBad));
    return box;
  }

  function switchBox() {
    const box = el("div", "tgm-box"), row = el("label", "tgm-switch"), input = el("input");
    input.type = "checkbox"; input.id = "tgm-enabled"; input.checked = !!S.enabled;
    input.onchange = async () => {
      const r = await api("POST", "api/telegram/settings", { enabled: input.checked });
      if (r.ok) S = r.data; else { ui.token = why(r); ui.tokenBad = true; }
      paint();
    };
    row.append(input, el("span", "", T("tgm.enable")));
    box.append(row, el("p", "tgm-help", T("tgm.enable.help")));
    return box;
  }

  function levelBox() {
    const box = el("div", "tgm-box"), sel = el("select");
    sel.id = "tgm-level";
    S.modes.forEach(m => { const o = el("option", "", T("ag.mode." + m)); o.value = m; sel.append(o); });
    sel.value = S.mode;
    sel.onchange = async () => {
      const r = await api("POST", "api/permission-level", { channel: "telegram", level: sel.value });
      ui.level = r.ok ? "" : T("lvl.e.save", why(r)); ui.levelBad = !r.ok;
      if (r.ok) { L = r.data; S.mode = r.data.level; }
      paint();
    };
    const where = L ? T("lvl.where.telegram", T("ag.mode." + L.levels.chat), T("ag.mode." + L.levels.voice)) : "";
    box.append(el("h3", "tgm-h", T("tgm.level")), named(T("tgm.level"), sel), el("p", "tgm-help", T("ag.mode." + S.mode + ".d")),
               ...(where ? [el("p", "tgm-help", where)] : []), el("p", "tgm-help", T("tgm.level.help") + " " + T("lvl.risk")),
               ...(S.mode === "auto" ? [note("tgm-level-risk", T("ag.auto.risk"), true)] : []), note("tgm-level-note", ui.level, ui.levelBad));
    return box;
  }

  function allowBox() {
    const box = el("div", "tgm-box"), list = el("ul", "tgm-list");
    S.allowed.forEach(p => {
      const li = el("li", "tgm-item"), name = p.name ? `${p.id} (@${p.name})` : String(p.id);
      const rm = btn(T("tgm.remove"), "", async () => {
        const r = await api("POST", "api/telegram/allow", { remove: p.id });
        ui.allow = r.ok ? "" : why(r); ui.allowBad = !r.ok;
        if (r.ok) S = r.data;
        paint();
      });
      rm.setAttribute("aria-label", T("tgm.allow.remove", name));
      li.append(el("span", "tgm-id", name), rm);
      list.append(li);
    });
    const input = el("input");
    input.type = "text"; input.inputMode = "numeric"; input.id = "tgm-add"; input.autocomplete = "off"; input.placeholder = "123456789";
    const add = async () => {
      const r = await api("POST", "api/telegram/allow", { add: input.value.trim() });
      ui.allow = r.ok ? "" : why(r); ui.allowBad = !r.ok;
      if (r.ok) { S = r.data; input.value = ""; }
      paint();
    };
    input.onkeydown = e => { if (e.key === "Enter") add(); };
    const row = el("div", "tgm-row");
    row.append(field("tgm-add", T("tgm.allow.add"), input), btn(T("tgm.add"), "", add));
    box.append(el("h3", "tgm-h", T("tgm.allow")), el("p", "tgm-help", T("tgm.allow.help")), S.allowed.length ? list : el("p", "tgm-help", T("tgm.allow.none")), row, note("tgm-allow-note", ui.allow, ui.allowBad));
    return box;
  }

  function pairBox() {
    const box = el("div", "tgm-box"), list = el("ul", "tgm-list");
    S.pairing.forEach(p => {
      const li = el("li", "tgm-item tgm-pair"), who = el("div", "tgm-who");
      who.append(el("span", "tgm-id", p.username ? `${p.id} (@${p.username})` : String(p.id)), el("span", "tgm-line", p.line ? `“${p.line}”` : ""), el("span", "tgm-help", T("tgm.pair.count", p.count)));
      const act = el("div", "tgm-row");
      const ok = btn(T("tgm.authorise"), "primary", async () => {
        const r = await api("POST", "api/telegram/pairing", { authorise: p.id });
        if (r.ok) S = r.data; else { ui.allow = why(r); ui.allowBad = true; }
        paint();
      });
      ok.setAttribute("aria-label", `${T("tgm.authorise")} ${p.id}`);
      const no = btn(T("tgm.dismiss"), "", async () => {
        const r = await api("POST", "api/telegram/pairing", { dismiss: p.id });
        if (r.ok) S = r.data;
        paint();
      });
      no.setAttribute("aria-label", `${T("tgm.dismiss")} ${p.id}`);
      act.append(ok, no);
      li.append(who, act);
      list.append(li);
    });
    box.append(el("h3", "tgm-h", T("tgm.pair")), el("p", "tgm-help", T("tgm.pair.help")), S.pairing.length ? list : el("p", "tgm-help", T("tgm.pair.none")));
    return box;
  }

  function homeBox() {
    const box = el("div", "tgm-box"), input = el("input");
    input.type = "text"; input.inputMode = "numeric"; input.id = "tgm-home"; input.autocomplete = "off"; input.value = S.home ? String(S.home) : ""; input.placeholder = "-1001234567890";
    const row = el("div", "tgm-row");
    row.append(btn(T("tgm.save"), "", async () => {
      const r = await api("POST", "api/telegram/settings", { home: input.value.trim() });
      ui.home = r.ok ? T("tgm.home.saved") : why(r); ui.homeBad = !r.ok;
      if (r.ok) S = r.data;
      paint();
    }), btn(T("tgm.sendtest"), "", async () => {
      const r = await api("POST", "api/telegram/send-test", {});
      ui.send = r.ok && r.data.ok ? T("tgm.sent") : (r.ok ? r.data.message : why(r)); ui.sendBad = !(r.ok && r.data.ok);
      paint();
    }));
    box.append(el("h3", "tgm-h", T("tgm.home")), el("p", "tgm-help", T("tgm.home.help")), named(T("tgm.home"), input), row, note("tgm-home-note", ui.home, ui.homeBad), note("tgm-send-note", ui.send, ui.sendBad));
    return box;
  }

  function importBox() {
    const box = el("div", "tgm-box"), row = el("div", "tgm-row");
    const run = btn(T("tgm.import.btn"), "", async () => {
      const r = await api("POST", "api/telegram/import", {});
      if (!r.ok) ui.imp = why(r), ui.impBad = true;
      else {
        const f = r.data.imported || {};
        S = r.data;
        ui.impBad = !f.file;
        ui.imp = !f.file ? T("tgm.import.none") : T("tgm.import.done", f.token ? (f.token_valid ? T("tgm.import.saved") : T("tgm.import.badtoken")) : T("tgm.import.no"), f.allowed, f.home ? T("tgm.import.yes") : T("tgm.import.no")) + (f.skipped ? " " + T("tgm.import.skipped", f.skipped) : "");
      }
      paint();
    });
    row.append(run);
    box.append(el("h3", "tgm-h", T("tgm.import")), el("p", "tgm-help", T("tgm.import.help")), el("p", "tgm-help", T("tgm.one")), row, note("tgm-import-note", ui.imp, ui.impBad));
    return box;
  }

  function paint() {
    const focus = document.activeElement && host.contains(document.activeElement) ? document.activeElement.id : "";
    const typed = {};
    host.querySelectorAll("input[type=text],input[type=password]").forEach(i => { if (i.id && i.value) typed[i.id] = i.value; });
    if (!S) { host.replaceChildren(el("p", "tgm-help", failed || T("tgm.loading"))); return; }
    host.replaceChildren(el("p", "tgm-intro", T("tgm.intro")), stateLine(), switchBox(), tokenBox(), levelBox(), allowBox(), pairBox(), homeBox(), importBox());
    Object.keys(typed).forEach(id => { const i = document.getElementById(id); if (i && !i.value) i.value = typed[id]; });
    if (focus) { const f = document.getElementById(focus); if (f) f.focus(); }
  }

  async function refresh() {
    if (busy) return;
    busy = true;
    const [r, l] = await Promise.all([api("GET", "api/telegram"), api("GET", "api/permission-level")]);
    busy = false;
    if (r.ok) { S = r.data; failed = ""; } else failed = why(r);
    L = l.ok ? l.data : null;
    last = Date.now();
    paint();
    paintMove();
  }
  function tick() {
    clearTimeout(timer);
    if (here()) { const typing = document.activeElement && host.contains(document.activeElement) && /INPUT|SELECT/.test(document.activeElement.tagName); if (!typing) refresh(); timer = setTimeout(tick, POLL_MS); }
  }

  /* ---- the button in the header: move the open conversation to Telegram, or bring it back ---- */
  const chat = () => window.agentChat || null;
  const pop = el("div", "tgm-pop");
  pop.id = "tgm-pop"; pop.hidden = true; pop.setAttribute("role", "dialog"); pop.setAttribute("aria-label", T("tgm.pick"));
  moveBtn.insertAdjacentElement("afterend", pop);
  function paintMove() {
    const view = document.body.dataset.view, c = chat(), rec = c && c.active() ? c.record() : null, sid = c && c.active() ? c.current() : "";
    const moved = !!(rec && rec.telegram_chat), can = !!(S && S.can_move);
    moveBtn.hidden = !((view === "chat" || view === "agent") && sid && rec && (moved || can));
    moveBtn.textContent = moved ? T("tgm.back") : T("tgm.move");
    moveBtn.title = moved ? T("tgm.back.title") : T("tgm.move.title");
    moveBtn.setAttribute("aria-expanded", String(!pop.hidden));
    if (moveBtn.hidden) pop.hidden = true;
  }
  const say = text => { const s = document.getElementById("chat-status"); if (s) s.textContent = text; };
  async function put(chatId) {
    const sid = chat().current();
    const r = await api("POST", `api/agent/sessions/${encodeURIComponent(sid)}/telegram`, { chat: chatId });
    pop.hidden = true;
    say(r.ok ? T(chatId === null ? "tgm.returned" : "tgm.moved") : why(r));
    if (r.ok) chat().refresh();
    paintMove();
  }
  moveBtn.onclick = async () => {
    const c = chat(), rec = c && c.record();
    if (!rec) return;
    if (rec.telegram_chat) return put(null);
    if (!pop.hidden) { pop.hidden = true; return paintMove(); }
    await refresh();
    const targets = (S && S.targets) || [];
    if (!targets.length) return say(T("tgm.none"));
    if (targets.length === 1) return put(targets[0].id);
    pop.replaceChildren(el("p", "tgm-help", T("tgm.pick")), ...targets.map(t => btn(t.label, "", () => put(t.id))), btn(T("tgm.cancel"), "", () => { pop.hidden = true; paintMove(); }));
    pop.hidden = false;
    const at = moveBtn.getBoundingClientRect();                                  // under the button, inside the window
    pop.style.top = `${Math.round(at.bottom + 4)}px`;
    pop.style.left = `${Math.max(16, Math.min(Math.round(at.left), window.innerWidth - pop.offsetWidth - 16))}px`;
    paintMove();
    const first = pop.querySelector("button");
    if (first) first.focus();
  };
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !pop.hidden) { pop.hidden = true; moveBtn.focus(); paintMove(); } });
  document.addEventListener("tadamoo:conversation", () => setTimeout(paintMove, 0));
  document.addEventListener("tadamoo:view", () => { setTimeout(paintMove, 0); if (Date.now() - last > HEADER_MS) refresh(); });
  document.addEventListener("tadamoo:lang", () => { paint(); paintMove(); });
  document.addEventListener("tadamoo:sub", e => { if (e.detail === "voice") { refresh(); tick(); } });
  setInterval(() => { paintMove(); const v = document.body.dataset.view; if ((v === "chat" || v === "agent") && Date.now() - last > HEADER_MS) refresh(); }, 2000);
  window.telegramUi = { paint: paintMove, refresh };
  refresh();
})();
