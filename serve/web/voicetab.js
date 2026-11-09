"use strict";
/* The Voice tab: talk with the model or with the agent by voice. It shows the voice service and its functions (read only: they are loaded in
   Models > Voice services), chooses who answers (direct, or the agent), the personality and what the agent may do without asking, and has
   a place to try it: Hold to talk and Speak a test sentence. The Avatar card is voiceavatar.js (the picture, the avatars, the creation of a new one). The microphone and the speakers are the PC's, and the voice service owns
   them; the page only asks the server, which forwards a fixed list of requests (serve/voice_proxy.py). The permission level is the Chat's
   own (same levels, same words) and is also set in Settings > Voice. Everything the server says enters the page as text. */
(function () {
  const root = document.getElementById("vt-root"), setRoot = document.getElementById("vb-root");
  if (!root) return;

  const POLL_MS = 2500, PERMS = ["plan", "read_only", "default", "auto"], CHAT_PERM_KEY = "tadamoo.chat.perm";
  const FNS = ["stt", "tts", "avatar"], MODES = ["direct", "harness"];
  let B = null, personas = [], personasOk = false, timer = 0, loadFailed = "";
  let live = { up: false, reason: "", fns: null, state: null };
  let talk = { down: false, starting: false, active: false, msg: "", bad: false }, say = { busy: false, msg: "", bad: false };
  let sayText = "", talkSig = "", brainMsg = { text: "", bad: false };

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick, id) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; if (id) b.id = id; return b; };
  const errText = r => r.status === 0 ? T("vt.e.offline") : (r.data && r.data.error && (LANG === "it" && r.data.error.message_it ? r.data.error.message_it : r.data.error.message)) || `HTTP ${r.status}`;
  const active = () => document.body.dataset.view === "voice" && !document.getElementById("view-voice").hidden;
  const settingsActive = () => !!setRoot && document.body.dataset.view === "settings" && !document.getElementById("vb-section").hidden;
  const chatPerm = () => { try { const v = localStorage.getItem(CHAT_PERM_KEY); return PERMS.includes(v) ? v : ""; } catch (e) { return ""; } };
  const keepChatPerm = v => { try { localStorage.setItem(CHAT_PERM_KEY, v); } catch (e) { /* this page only */ } };

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const svc = (method, path, body) => api("POST", "api/voice/service/call", { method, path, ...(body ? { body } : {}) });

  /* ---- the skeleton: five cards, each repainted on its own ---- */
  const parts = {};
  function build() {
    root.replaceChildren();
    root.append(el("p", "vt-intro", T("vt.intro")));
    const status = el("p", "sr"); status.id = "vt-live"; status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    root.append(status);
    const grid = el("div", "vt-grid");
    for (const [id, key] of [["brain", "vt.brain"], ["talk", "vt.talk"], ["last", "vt.last"], ["service", "vt.service"], ["avatar", "vt.avatar"], ["meetings", "vt.meetings"]]) {
      const s = el("section", "vt-card"), h = el("h3", "vt-h", T(key));
      h.id = "vt-h-" + id; s.id = "vt-card-" + id; s.setAttribute("aria-labelledby", h.id);
      s.append(h);
      parts[id] = { s, h };
      grid.append(s);
    }
    root.append(grid);
    if (window.VoiceAvatar) window.VoiceAvatar.mount(parts.avatar.s, avatarCtx);
    if (window.VoiceMeet) window.VoiceMeet.mount(parts.meetings.s, avatarCtx);
  }
  const body = id => { const h = parts[id].h; while (h.nextSibling) h.nextSibling.remove(); return parts[id].s; };
  const chip = (text, cls) => el("span", "vt-chip " + (cls || ""), text);
  const note = (text, bad) => { const n = el("p", "vt-note", text); n.setAttribute("role", bad ? "alert" : "status"); n.dataset.bad = bad ? "1" : ""; n.hidden = !text; return n; };
  const openModels = () => { view("loader"); const s = document.getElementById("sub-voicesvc"); if (s) s.click(); };
  const avatarCtx = {
    live: () => live, personas: () => personas, persona: () => (B ? B.persona : ""), svc, api, openModels,
    reloadPersonas: async () => { const p = await api("GET", "api/personas"); personasOk = p.ok; personas = p.ok ? (p.data.personas || []) : []; paintBrain(); }
  };

  /* ---- the service and its functions ---- */
  function paintService() {
    const s = body("service"), row = el("div", "vt-chips");
    row.append(chip(live.up ? T("vt.service.up") : T("vt.service.down"), live.up ? "vt-ok" : "vt-off"));
    s.append(row);
    if (!live.up && live.reason) s.append(note(live.reason, true));
    const list = el("ul", "vt-fns");
    for (const f of FNS) {
      const info = live.fns && live.fns[f], li = el("li", "vt-fn"), state = info ? info.state : "unknown";
      li.append(el("span", "vt-fn-name", T("vt.fn." + f)), chip(T("vt.st." + state), state === "loaded" ? "vt-ok" : state === "error" ? "vt-warn" : "vt-off"));
      if (info && info.name) li.append(el("span", "vt-fn-model", info.name));
      list.append(li);
    }
    s.append(list, btn(T("vt.fn.open"), "ghost", openModels, "vt-open-models"));
  }

  /* ---- who answers ---- */
  function modePick() {
    const set = el("fieldset", "vt-seg");
    set.append(el("legend", "vt-legend", T("vt.mode.legend")));
    for (const m of MODES) {
      const lab = el("label", "vt-opt"), input = el("input"), txt = el("span", "vt-opt-t");
      input.type = "radio"; input.name = "vt-mode"; input.value = m; input.id = "vt-mode-" + m; input.checked = B.mode === m;
      input.onchange = () => saveBrain({ mode: m });
      txt.append(el("b", "", T("vt.mode." + m)), el("span", "vt-opt-d", T("vt.mode." + m + ".d")));
      lab.append(input, txt);
      set.append(lab);
    }
    return set;
  }
  function personaPick() {
    const row = el("div", "vt-field"), lab = el("label", "", T("vt.persona")), sel = el("select");
    lab.htmlFor = sel.id = "vt-persona";
    const none = el("option", "", T("vt.persona.none"));
    none.value = "";
    sel.append(none);
    personas.forEach(p => { const o = el("option", "", p.name || p.slug); o.value = p.slug; sel.append(o); });
    if (B.persona && !personas.some(p => p.slug === B.persona)) { const o = el("option", "", T("vt.persona.missing", B.persona)); o.value = B.persona; sel.append(o); }
    sel.value = B.persona;
    sel.onchange = () => saveBrain({ persona: sel.value });
    row.append(lab, sel);
    if (!personasOk) row.append(el("p", "vt-help", T("vt.persona.offline")));
    return row;
  }
  function permission(prefix) {
    const set = el("fieldset", "vt-seg");
    set.append(el("legend", "vt-legend", T("vt.perm")));
    for (const m of PERMS) {
      const lab = el("label", "vt-opt"), input = el("input"), txt = el("span", "vt-opt-t");
      input.type = "radio"; input.name = prefix + "-perm"; input.value = m; input.id = prefix + "-perm-" + m; input.checked = B.permission === m;
      input.onchange = () => saveBrain({ permission: m });
      txt.append(el("b", "", T("ag.mode." + m)), el("span", "vt-opt-d", T("ag.mode." + m + ".d")));
      lab.append(input, txt);
      set.append(lab);
    }
    set.append(el("p", "vt-help", T("vt.perm.help")));
    if (B.permission === "auto") set.append(note(T("ag.auto.risk"), true));
    return set;
  }
  function hotkeys(prefix) {
    const wrap = el("div", "vt-field"), row = el("label", "vt-check"), input = el("input");
    input.type = "checkbox"; input.id = prefix + "-hotkeys"; input.checked = !!B.hotkeys;
    input.onchange = () => saveBrain({ hotkeys: input.checked });
    row.append(input, el("span", "", T("vt.hotkeys")));
    wrap.append(row, el("p", "vt-help", T("vt.hotkeys.help")));
    return wrap;
  }
  function paintBrain() {
    const s = body("brain");
    if (!B) { s.append(el("p", "vt-help", loadFailed || T("vt.loading"))); return; }
    s.append(modePick(), personaPick(), permission("vt"), hotkeys("vt"), note(brainMsg.text, brainMsg.bad));
  }
  async function saveBrain(change) {
    const r = await api("POST", "api/voice/brain", change);
    if (r.ok) {
      B = r.data; brainMsg = { text: T("vt.saved"), bad: false };
      if (change.permission) keepChatPerm(change.permission);       // one setting: the Chat starts new conversations at the same level
    } else brainMsg = { text: errText(r), bad: true };
    paintBrain(); paintSettings();
    const keep = change.permission ? "-perm-" + change.permission : change.hotkeys !== undefined ? "-hotkeys" : change.mode ? "-mode-" + change.mode : "";
    const f = keep && (document.getElementById("vt" + keep) || document.getElementById("vb" + keep));
    if (f && document.body.contains(f) && !document.activeElement.matches("input, select, button")) f.focus();
  }
  function paintSettings() {
    if (!setRoot) return;
    if (!B) { setRoot.replaceChildren(el("p", "vt-help", loadFailed || T("vt.loading"))); return; }
    const box = el("section", "vt-card"), h = el("h3", "vt-h", T("vt.set.title"));
    h.id = "vb-h2"; box.setAttribute("aria-labelledby", h.id);
    box.append(h, el("p", "vt-help", T("vt.set.intro")), permission("vb"), hotkeys("vb"), note(brainMsg.text, brainMsg.bad));
    setRoot.replaceChildren(box, ...(window.VoiceAvatar ? [window.VoiceAvatar.settingsCard(api)] : []), ...(window.VoiceEnhance ? [window.VoiceEnhance.settingsCard(api)] : []), ...(window.VoiceMeet ? [window.VoiceMeet.settingsCard(api)] : []), ...(window.VoiceInterrupt ? [window.VoiceInterrupt.settingsCard(api)] : []));
  }

  /* ---- try it ---- */
  function why() {
    if (!live.up) return T("vt.off.service", live.reason || T("vt.service.down"));
    const missing = ["stt", "tts"].filter(f => !(live.fns && live.fns[f] && live.fns[f].state === "loaded"));
    return missing.length ? T("vt.off.fns", missing.map(f => T("vt.fn." + f)).join(", ")) : "";
  }
  const sessionOf = () => (live.state && live.state.session) || {};
  const sigOf = () => JSON.stringify([why(), sessionOf().active, sessionOf().phase, sessionOf().brain, talk, say]);
  function paintTalk() {
    talkSig = sigOf();
    const s = body("talk"), reason = why(), sess = sessionOf();
    const label = talk.active ? T("vt.talk.holding") : talk.starting ? T("vt.talk.starting") : T("vt.talk.hold");
    const hold = btn(label, "primary vt-hold", () => { /* the press and the release below decide */ }, "vt-hold");
    hold.disabled = !!reason;
    hold.setAttribute("aria-pressed", String(talk.active || talk.starting));
    hold.oncontextmenu = e => e.preventDefault();
    hold.onpointerdown = e => { if (hold.disabled) return; e.preventDefault(); if (hold.setPointerCapture && e.pointerId != null) hold.setPointerCapture(e.pointerId); press(); };
    hold.onpointerup = hold.onpointercancel = () => release();
    hold.onkeydown = e => { if ((e.key === " " || e.key === "Enter") && !e.repeat && !hold.disabled) { e.preventDefault(); press(); } };
    hold.onkeyup = e => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); release(); } };
    s.append(el("p", "vt-help", T("vt.talk.mic")), hold);
    if (reason) s.append(note(reason, true));
    if (sess.active) {
      const row = el("div", "vt-chips");
      row.append(chip(T("vt.sess.on", T("vt.mode." + (sess.brain || "direct")), T("vt.phase." + (sess.phase || "idle"))), "vt-ok"), btn(T("vt.talk.stop"), "ghost", stopSession, "vt-stop"));
      s.append(row);
    }
    s.append(note(talk.msg, talk.bad));
    const f = el("div", "vt-field"), lab = el("label", "", T("vt.say.label")), input = el("input");
    input.type = "text"; input.id = "vt-say"; input.value = sayText; input.maxLength = 300; input.autocomplete = "off"; lab.htmlFor = "vt-say";
    const go = btn(say.busy ? T("vt.say.busy") : T("vt.say.button"), "", speak, "vt-say-go");
    go.disabled = !!reason || say.busy || !sayText.trim();
    input.oninput = () => { sayText = input.value; go.disabled = !!why() || say.busy || !sayText.trim(); };
    f.append(lab, input);
    s.append(f, go, note(say.msg, say.bad));
  }
  async function press() {
    if (talk.down) return;
    talk = { ...talk, down: true, starting: true, msg: "", bad: false };
    paintTalk();
    const r = await svc("POST", "/session/start", { mode: "half" });
    talk.starting = false;
    if (r.ok) talk.active = true; else { talk.msg = r.data && r.data.error && r.data.error.code === "not_loaded" ? why() || errText(r) : errText(r); talk.bad = true; talk.down = false; }
    if (talk.active && !talk.down) { await stopSession(); return; }       // let go before it had started
    paintTalk();
    refreshLive();
  }
  function release() {
    if (!talk.down) return;
    talk.down = false;
    if (talk.active) stopSession(); else paintTalk();
  }
  async function stopSession() {
    const r = await svc("POST", "/session/stop");
    talk.active = false; talk.down = false;
    if (!r.ok) { talk.msg = errText(r); talk.bad = true; }
    paintTalk();
    refreshLive();
  }
  async function speak() {
    say = { busy: true, msg: "", bad: false };
    paintTalk();
    const r = await svc("POST", "/brain/say", { text: sayText.trim() });
    say = { busy: false, msg: r.ok ? T("vt.say.done") : errText(r), bad: !r.ok };
    paintTalk();
  }

  /* ---- the last exchange ---- */
  function paintLast() {
    const s = body("last"), st = live.state;
    if (st && st.pending) {
      const p = st.pending;
      s.append(note(p.voice ? T("vt.pending.voice") : T("vt.pending.screen"), !p.voice));
      if (!p.voice) s.append(btn(T("vt.pending.open"), "ghost", () => view("chat"), "vt-open-chat"));
    }
    const x = st && st.last;
    if (!x) { s.append(el("p", "vt-help", T("vt.last.none"))); return; }
    const dl = el("dl", "vt-ex");
    const add = (k, v, cls) => { dl.append(el("dt", "", k), el("dd", cls || "", v)); };
    add(T("vt.last.you"), x.user);
    add(T("vt.last.reply"), x.reply, "vt-full");
    add(T("vt.last.spoken"), x.spoken);
    s.append(chip(T("vt.last.mode", T("vt.mode." + x.mode)), "vt-ok"), dl);
    if (x.error) s.append(note(x.error, true));
  }

  /* ---- the avatar ---- */
  function paintAvatar() { if (window.VoiceAvatar) window.VoiceAvatar.update(); if (window.VoiceMeet) window.VoiceMeet.update(); }

  function paintAll() { paintService(); paintBrain(); paintTalk(); paintLast(); paintAvatar(); paintSettings(); }

  /* ---- loading ---- */
  async function loadSettings() {
    const [b, p] = await Promise.all([api("GET", "api/voice/brain"), api("GET", "api/personas")]);
    if (b.ok) { B = b.data; loadFailed = ""; } else loadFailed = errText(b);
    personasOk = p.ok; personas = p.ok ? (p.data.personas || []) : [];
    if (B && !B.permission_set && chatPerm() && chatPerm() !== B.permission) {       // the Chat's level, kept once so that the service follows it
      const r = await api("POST", "api/voice/brain", { permission: chatPerm() });
      if (r.ok) B = r.data;
    }
    paintBrain(); paintSettings();
  }
  async function refreshLive() {
    const [st, fn] = await Promise.all([svc("GET", "/brain/state"), svc("GET", "/functions")]);
    const was = live.up;
    live = { up: st.ok, reason: st.ok ? "" : errText(st), fns: fn.ok ? fn.data.functions : null, state: st.ok ? st.data : null };
    if (!live.up) talk.active = false;
    if (was !== live.up) { const l = document.getElementById("vt-live"); if (l) l.textContent = live.up ? T("vt.service.up") : T("vt.service.down"); }
    paintService(); paintLast(); paintAvatar();
    const typing = document.activeElement && document.activeElement.id === "vt-say", holding = talk.down || talk.starting;
    if (!typing && !holding && sigOf() !== talkSig) paintTalk();                     // the button is not replaced under a finger
    else { const h = document.getElementById("vt-hold"); if (h && !holding) h.disabled = !!why(); }
  }
  function tick() {
    clearTimeout(timer);
    if (!active() && !settingsActive()) return;
    refreshLive().finally(() => { timer = setTimeout(tick, POLL_MS); });
  }

  const translate = () => { applyT(document.getElementById("view-voice")); applyT(document.getElementById("vb-section")); };
  sayText = T("vt.say.sample");
  translate();
  build();
  paintAll();
  document.addEventListener("tadamoo:view", e => {
    if (e.detail === "voice") { loadSettings(); tick(); } else if (e.detail !== "settings") clearTimeout(timer);
  });
  document.addEventListener("tadamoo:sub", e => { if (e.detail === "voice") { loadSettings(); tick(); } });
  document.addEventListener("tadamoo:lang", () => { sayText = T("vt.say.sample"); translate(); build(); paintAll(); });
  document.addEventListener("visibilitychange", () => { if (document.hidden) release(); else if (active() || settingsActive()) tick(); });
  window.addEventListener("blur", () => release());
  if (active()) { loadSettings(); tick(); }
})();
