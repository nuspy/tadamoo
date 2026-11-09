"use strict";
/* The "Speaking over the avatar" card of Settings > Voice: what the avatar does when the person talks while it is still speaking (voice/callbridge/
   interruption.py). The choices are voice.json `interruptions` (GET and POST api/voice/interruptions, this PC only); the avatar's own words and the
   person's are never shown here. The server's text enters the page as text. A change reaches the next phone connection.
   voicetab.js puts the card under the Meetings card. */
(function () {
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const msgOf = r => {
    const e = r.data && r.data.error;
    if (typeof e === "string") return e;
    return (e && ((LANG === "it" && e.message_it) || e.message)) || (r.status === 0 ? T("vi.e.offline") : `HTTP ${r.status}`);
  };

  let cfg = null, note = { text: "", bad: false };
  const ui = {};

  function check(id, labelKey, helpKey, onchange) {
    const w = el("div", "vi-field"), row = el("label", "vt-check"), i = el("input");
    i.type = "checkbox"; i.id = id; i.onchange = onchange;
    row.append(i, el("span", "", T(labelKey)));
    w.append(row, el("p", "vt-help", T(helpKey)));
    return [w, i];
  }

  function number(id, labelKey, helpKey, min, max, step, onchange) {
    const w = el("div", "vi-field"), l = el("label", "", T(labelKey)), i = el("input");
    l.htmlFor = id; i.id = id; i.type = "number"; i.inputMode = "numeric"; i.min = String(min); i.max = String(max); i.step = String(step); i.autocomplete = "off";
    i.onchange = onchange;
    w.append(l, i, el("p", "vt-help", T(helpKey)));
    return [w, i];
  }

  function paint() {
    if (!cfg) return;
    ui.enabled.checked = cfg.enabled;
    ui.offer.checked = cfg.resume_offer;
    ui.timeout.value = String(cfg.judge_timeout_ms);
    ui.duck.value = String(Math.round(cfg.duck_level * 100));
    ui.timeout.disabled = ui.duck.disabled = ui.offer.disabled = !cfg.enabled;
    ui.note.textContent = note.text; ui.note.hidden = !note.text; ui.note.dataset.bad = note.bad ? "1" : "0";
    ui.note.setAttribute("role", note.bad ? "alert" : "status");
  }

  async function save(api, change) {
    note = { text: "", bad: false };
    const r = await api("POST", "api/voice/interruptions", change);
    if (r.ok) { cfg = r.data; note = { text: T("vi.saved"), bad: false }; } else note = { text: msgOf(r), bad: true };
    paint();
  }

  function build(api) {
    const box = el("section", "vt-card"), h = el("h3", "vt-h", T("vi.title"));
    h.id = "vi-h"; box.setAttribute("aria-labelledby", h.id);
    const [ew, enabled] = check("vi-enabled", "vi.enabled", "vi.enabled.help", () => save(api, { enabled: enabled.checked }));
    const [tw, timeout] = number("vi-timeout", "vi.timeout", "vi.timeout.help", 200, 5000, 50, () => save(api, { judge_timeout_ms: Number(timeout.value) }));
    const [dw, duck] = number("vi-duck", "vi.duck", "vi.duck.help", 0, 90, 5, () => save(api, { duck_level: Number(duck.value) / 100 }));
    const [ow, offer] = check("vi-offer", "vi.offer", "vi.offer.help", () => save(api, { resume_offer: offer.checked }));
    const status = el("p", "vt-note"); status.id = "vi-note"; status.hidden = true;
    box.append(h, el("p", "vt-help", T("vi.intro")), ew, tw, dw, ow, el("p", "vt-help", T("vi.session")), status);
    Object.assign(ui, { enabled, timeout, duck, offer, note: status });
    return box;
  }

  function settingsCard(api) {
    const box = build(api);
    api("GET", "api/voice/interruptions").then(r => { if (r.ok) cfg = r.data; else note = { text: msgOf(r), bad: true }; paint(); });
    return box;
  }

  window.VoiceInterrupt = { settingsCard };
})();
