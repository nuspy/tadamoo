"use strict";
/* The "Spoken language" card of Settings > Voice: whether the avatar follows the language the person speaks, which languages it may use, how sure it must be
   before it changes, and the voice of each language (voice/callbridge/language_follow.py, docs/voice/language.md). The choices are voice.json `language`
   (GET and POST api/voice/language, this PC only). The status line asks the running voice session (GET and POST /session/language through the voice
   service) which language it speaks now and why, and gives the way back to the automatic mode. Everything the server says enters the page as text.
   voicetab.js puts the card under the "Speaking over the avatar" card. */
(function () {
  const CODES = ["it", "en", "fr", "de", "es", "pt", "ja", "zh", "hi", "hu", "ru", "nl", "pl", "tr", "ko", "ar", "cs"];
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const msgOf = r => {
    const e = r.data && r.data.error;
    if (typeof e === "string") return e;
    return (e && ((LANG === "it" && e.message_it) || e.message)) || (r.status === 0 ? T("vlg.e.offline") : `HTTP ${r.status}`);
  };
  const nameOf = code => T("vlg.lang." + code);

  let cfg = null, live = null, note = { text: "", bad: false }, timer = 0;
  const ui = {};

  function field(id, labelText, helpKey, input) {
    const w = el("div", "vlg-field"), l = el("label", "", labelText);
    l.htmlFor = id; input.id = id;
    w.append(l, input);
    if (helpKey) w.append(el("p", "vt-help", T(helpKey)));
    return w;
  }

  function number(min, max, step) {
    const i = el("input"); i.type = "number"; i.inputMode = "decimal"; i.min = String(min); i.max = String(max); i.step = String(step); i.autocomplete = "off";
    return i;
  }

  function paintLive() {
    if (!ui.status) return;
    const s = live;
    let text;
    if (!s) text = T("vlg.live.unknown");
    else if (!s.active) text = T("vlg.live.none");
    else text = T(s.mode === "follow" ? "vlg.live.follow" : "vlg.live.locked", CODES.includes(s.language) ? nameOf(s.language) : s.language);
    ui.status.textContent = text;
    ui.auto.hidden = !(s && s.active && s.mode === "locked" && s.follow);
    const warns = [];
    if (s && s.engine_fallback) warns.push(T("vlg.w.fallback"));
    if (s && (s.warnings || []).includes("stt_english_only")) warns.push(T("vlg.w.english_only"));
    if (s && (s.warnings || []).includes("stt_language_fixed")) warns.push(T("vlg.w.stt_fixed"));
    ui.warn.textContent = warns.join(" ");
    ui.warn.hidden = !warns.length;
  }

  function paint() {
    if (!cfg) return;
    ui.follow.checked = cfg.follow;
    for (const code of CODES) ui.langs[code].checked = cfg.enabled.includes(code);
    ui.threshold.value = String(Math.round(cfg.threshold * 100));
    ui.short.value = String(cfg.short_s);
    ui.threshold.disabled = ui.short.disabled = !cfg.follow;
    ui.voices.replaceChildren();
    for (const code of cfg.enabled) {
      const i = el("input"); i.type = "text"; i.maxLength = 80; i.autocomplete = "off"; i.spellcheck = false; i.value = cfg.voices[code] || "";
      i.onchange = () => { const v = Object.assign({}, cfg.voices); if (i.value.trim()) v[code] = i.value.trim(); else delete v[code]; ui.save({ voices: v }); };
      ui.voices.append(field("vlg-voice-" + code, T("vlg.voice", nameOf(code)), null, i));
    }
    ui.note.textContent = note.text; ui.note.hidden = !note.text; ui.note.dataset.bad = note.bad ? "1" : "0";
    ui.note.setAttribute("role", note.bad ? "alert" : "status");
  }

  async function refreshLive(svc) {
    const r = await svc("GET", "/session/language");
    live = r.ok ? r.data : null;
    paintLive();
  }

  function build(api, svc) {
    const box = el("section", "vt-card"), h = el("h3", "vt-h", T("vlg.title"));
    h.id = "vlg-h"; box.setAttribute("aria-labelledby", h.id);
    ui.save = async change => {
      note = { text: "", bad: false };
      const r = await api("POST", "api/voice/language", change);
      if (r.ok) { cfg = r.data; note = { text: T("vlg.saved"), bad: false }; } else note = { text: msgOf(r), bad: true };
      paint();
    };

    const status = el("p", "vlg-status"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    const auto = el("button", "btn vlg-auto", T("vlg.auto")); auto.type = "button"; auto.hidden = true;
    auto.onclick = async () => {
      const r = await svc("POST", "/session/language", { language: "auto" });
      if (!r.ok) { note = { text: msgOf(r), bad: true }; paint(); }
      refreshLive(svc);
    };
    const warn = el("p", "vt-note vlg-warn"); warn.hidden = true; warn.setAttribute("role", "status");
    const live_row = el("div", "vlg-live"); live_row.append(status, auto);

    const follow = el("input"); follow.type = "checkbox"; follow.id = "vlg-follow"; follow.onchange = () => ui.save({ follow: follow.checked });
    const fw = el("div", "vlg-field"), fl = el("label", "vt-check"); fl.append(follow, el("span", "", T("vlg.follow")));
    fw.append(fl, el("p", "vt-help", T("vlg.follow.help")));

    const langBox = el("fieldset", "vlg-langs"), legend = el("legend", "", T("vlg.enabled"));
    langBox.append(legend);
    ui.langs = {};
    for (const code of CODES) {
      const row = el("label", "vt-check vlg-lang"), cb = el("input"); cb.type = "checkbox"; cb.id = "vlg-lang-" + code;
      cb.onchange = () => {
        const next = CODES.filter(c => (c === code ? cb.checked : ui.langs[c].checked));
        if (!next.length) { cb.checked = true; note = { text: T("vlg.e.one"), bad: true }; paint(); return; }
        ui.save({ enabled: next });
      };
      row.append(cb, el("span", "", nameOf(code)));
      ui.langs[code] = cb;
      langBox.append(row);
    }
    const langHelp = el("p", "vt-help", T("vlg.enabled.help"));

    const voices = el("div", "vlg-voices");
    const voicesHead = el("h4", "vlg-sub", T("vlg.voices"));
    const voicesHelp = el("p", "vt-help", T("vlg.voices.help"));

    const adv = el("details", "vlg-adv"), sum = el("summary", "", T("vlg.advanced"));
    const threshold = number(50, 99, 1), short = number(0.5, 5, 0.5);
    threshold.onchange = () => ui.save({ threshold: Number(threshold.value) / 100 });
    short.onchange = () => ui.save({ short_s: Number(short.value) });
    adv.append(sum, field("vlg-threshold", T("vlg.threshold"), "vlg.threshold.help", threshold), field("vlg-short", T("vlg.short"), "vlg.short.help", short));

    const saved = el("p", "vt-note"); saved.id = "vlg-note"; saved.hidden = true;
    box.append(h, el("p", "vt-help", T("vlg.intro")), live_row, warn, fw, langBox, langHelp, voicesHead, voicesHelp, voices, adv, el("p", "vt-help", T("vlg.session")), saved);
    Object.assign(ui, { status, auto, warn, follow, threshold, short, voices, note: saved });
    return box;
  }

  function settingsCard(api, svc) {
    const box = build(api, svc);
    api("GET", "api/voice/language").then(r => { if (r.ok) cfg = r.data; else note = { text: msgOf(r), bad: true }; paint(); });
    refreshLive(svc);
    clearInterval(timer);
    timer = setInterval(() => { if (!box.isConnected) { clearInterval(timer); return; } refreshLive(svc); }, 4000);
    return box;
  }

  window.VoiceLanguage = { settingsCard };
})();
