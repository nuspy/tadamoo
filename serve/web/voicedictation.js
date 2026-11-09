"use strict";
/* The "Dictation" card of Settings > Voice: the global hotkey that records while it is held and pastes what was said into the focused app when it is
   released (voice/callbridge/dictation.py), and whether the clipboard history (Ctrl+Alt+arrows) is running. The choices are voice.json `dictation` (GET and
   POST api/voice/dictation, this PC only). The live state is the voice service's own /components/status, asked through the server; what it says
   enters the page as text. A change reaches the running service within a moment: no restart. voicetab.js puts the card under the language card. */
(function () {
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const msgOf = r => {
    const e = r.data && r.data.error;
    if (typeof e === "string") return e;
    return (e && ((LANG === "it" && e.message_it) || e.message)) || (r.status === 0 ? T("vd.e.offline") : `HTTP ${r.status}`);
  };

  let cfg = null, note = { text: "", bad: false }, live = null, timer = null;
  const ui = {};

  const STATE_KEYS = { running: "vd.state.running", loading: "vd.state.loading", error: "vd.state.error", stopped: "vd.state.stopped",
                       disabled: "vd.state.disabled", unsupported: "vd.state.unsupported" };

  function stateText(part, noun) {
    if (!part) return T("vd.live.unknown", noun);
    const base = T(STATE_KEYS[part.state] || "vd.state.other", noun, String(part.state));
    return part.detail ? `${base} ${String(part.detail)}` : base;
  }

  function paint() {
    if (cfg) {
      ui.enabled.checked = cfg.enabled;
      if (document.activeElement !== ui.hotkey) ui.hotkey.value = cfg.hotkey;
      ui.hotkey.disabled = !cfg.enabled;
    }
    ui.note.textContent = note.text; ui.note.hidden = !note.text; ui.note.dataset.bad = note.bad ? "1" : "0";
    ui.note.setAttribute("role", note.bad ? "alert" : "status");
    ui.live.replaceChildren();
    if (live === false) ui.live.append(el("p", "vt-help", T("vd.live.down")));
    else if (live) ui.live.append(el("p", "vt-help", stateText(live.dictation, T("vd.what.dictation"))), el("p", "vt-help", stateText(live.clipboard_history, T("vd.what.clipboard"))));
  }

  async function refreshLive(api) {
    const r = await api("POST", "api/voice/service/call", { method: "GET", path: "/components/status" });
    live = r.ok && r.data ? r.data : false;
    paint();
  }

  async function save(api, change) {
    note = { text: "", bad: false };
    const r = await api("POST", "api/voice/dictation", change);
    if (r.ok) { cfg = r.data; note = { text: T("vd.saved"), bad: false }; } else note = { text: msgOf(r), bad: true };
    paint();
    if (r.ok) setTimeout(() => refreshLive(api), 1500);         // the service reads the file at its next request, then applies it
  }

  function build(api) {
    const box = el("section", "vt-card"), h = el("h3", "vt-h", T("vd.title"));
    h.id = "vd-h"; box.setAttribute("aria-labelledby", h.id);
    const ew = el("div", "vd-field"), row = el("label", "vt-check"), enabled = el("input");
    enabled.type = "checkbox"; enabled.id = "vd-enabled"; enabled.onchange = () => save(api, { enabled: enabled.checked });
    row.append(enabled, el("span", "", T("vd.enabled")));
    ew.append(row, el("p", "vt-help", T("vd.enabled.help")));
    const hw = el("div", "vd-field"), label = el("label", "", T("vd.hotkey")), hotkey = el("input");
    label.htmlFor = "vd-hotkey"; hotkey.id = "vd-hotkey"; hotkey.type = "text"; hotkey.autocomplete = "off"; hotkey.spellcheck = false; hotkey.maxLength = 40;
    hotkey.setAttribute("autocapitalize", "off");
    hotkey.onchange = () => save(api, { hotkey: hotkey.value });
    hw.append(label, hotkey, el("p", "vt-help", T("vd.hotkey.help")));
    const status = el("p", "vt-note"); status.id = "vd-note"; status.hidden = true;
    const liveBox = el("div", "vd-live"); liveBox.setAttribute("aria-live", "polite");
    box.append(h, el("p", "vt-help", T("vd.intro")), ew, hw, status, liveBox);
    Object.assign(ui, { enabled, hotkey, note: status, live: liveBox });
    return box;
  }

  function settingsCard(api) {
    if (timer) { clearInterval(timer); timer = null; }
    const box = build(api);
    paint();
    api("GET", "api/voice/dictation").then(r => { if (r.ok) cfg = r.data; else note = { text: msgOf(r), bad: true }; paint(); });
    refreshLive(api);
    timer = setInterval(() => {
      if (!box.isConnected) { clearInterval(timer); timer = null; return; }
      if (!document.hidden) refreshLive(api);
    }, 10000);
    return box;
  }

  window.VoiceDictation = { settingsCard };
})();
