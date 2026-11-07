"use strict";
/* The Long answer control: a button beside Send that opens a small dialog. Once on, it stays on for that conversation until it is turned off;
   a new conversation starts as the "new conversations" box says. In the Chat the options go to the server as `long_answer`
   (serve/longform.py writes the text in parts); in the Agent the message carries one line that the harness reads, and the harness itself writes
   the text in parts. */
(() => {
  const KEY = "tadamoo.longanswer", ON_KEY = "tadamoo.longanswer.on", MAX_KEPT = 200, NEW = "new";
  const MODES = ["write", "translate", "rewrite"];
  const load = () => { try { const v = JSON.parse(localStorage.getItem(KEY) || "{}"); return v && typeof v === "object" ? v : {}; } catch (e) { return {}; } };
  const keep = v => { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) { /* the options are kept for this page only */ } };

  const dlg = $("#long-dlg"), open = $("#long-open"), form = $("#la-form"), say = $("#la-msg");
  const f = { mode: $("#la-mode"), words: $("#la-words"), lang: $("#la-lang"), instr: $("#la-instr"), style: $("#la-style"), gloss: $("#la-gloss"), protect: $("#la-protect"),
    fresh: $("#la-default") };
  /* which conversations have it on: {id: bool}, plus "new" for the one not started yet; `defaults` is what a new conversation starts with */
  const loadOn = () => { try { const v = JSON.parse(localStorage.getItem(ON_KEY) || "{}"); return v && typeof v === "object" ? v : {}; } catch (e) { return {}; } };
  const keepOn = v => {
    const ids = Object.keys(v);
    for (const id of ids.slice(0, Math.max(0, ids.length - MAX_KEPT))) delete v[id];
    try { localStorage.setItem(ON_KEY, JSON.stringify(v)); } catch (e) { /* it is kept for this page only */ }
  };
  let on = loadOn();
  const scope = () => (window.chatUi && window.chatUi.sessionId && window.chatUi.sessionId()) || NEW;
  const defaults = () => load().fresh === true;
  const armed = () => { const v = on[scope()]; return v === undefined ? defaults() : v === true; };
  const setArmed = value => { on[scope()] = value; keepOn(on); paint(); };

  function restore() {
    const s = load();
    f.mode.value = MODES.includes(s.mode) ? s.mode : "write";
    f.words.value = s.words || 3000;
    f.lang.value = s.lang || ""; f.instr.value = s.instr || ""; f.style.value = s.style || ""; f.gloss.value = s.gloss || "";
    f.protect.checked = s.protect !== false;
    f.fresh.checked = s.fresh === true;
  }
  const values = () => ({ mode: f.mode.value, words: Number(f.words.value) || 0, lang: f.lang.value.trim(), instr: f.instr.value.trim(), style: f.style.value.trim(),
    gloss: f.gloss.value, protect: f.protect.checked, fresh: f.fresh.checked });
  function fit() {                                     // each mode shows the fields it uses
    const m = f.mode.value;
    $("#la-words-box").hidden = m === "translate";
    $("#la-instr-box").hidden = m !== "rewrite";
    $("#la-lang-box").querySelector("label").textContent = T(m === "translate" ? "la.lang.to" : "la.lang");
  }
  function problem(v) {
    if (v.mode === "write" && (v.words < 50 || v.words > 200000)) return T("la.bad.words");
    if (v.mode === "translate" && !v.lang) return T("la.bad.lang");
    if (v.mode === "rewrite" && !v.instr) return T("la.bad.instr");
    return "";
  }
  function paint() {
    const now = armed();
    open.setAttribute("aria-pressed", String(now));
    open.textContent = T(now ? "la.open.on" : "la.open");
    open.dataset.on = now ? "1" : "";
  }
  const glossary = text => {
    const out = {};
    for (const line of text.split("\n")) {
      const m = line.match(/^\s*(.+?)\s*(?:=>|=|->)\s*(.+?)\s*$/);
      if (m) out[m[1]] = m[2];
    }
    return out;
  };
  function options(v) {
    const o = { mode: v.mode, protect: v.protect };
    if (v.mode === "write") o.target_words = v.words;
    if (v.lang) o.target_language = v.lang;
    if (v.mode !== "write") { if (v.mode === "rewrite") o.instruction = v.instr; }
    if (v.style) o.style = v.style;
    const g = glossary(v.gloss);
    if (Object.keys(g).length) o.glossary = g;
    return o;
  }
  /* the conversation changed: a fresh one starts again from the default; one just created takes what the unstarted one had */
  document.addEventListener("tadamoo:conversation", e => {
    const created = e.detail && e.detail.created, id = scope();
    if (created && id !== NEW && on[NEW] !== undefined && on[id] === undefined) on[id] = on[NEW];
    if (id === NEW || created) { delete on[NEW]; keepOn(on); }
    paint();
  });

  open.addEventListener("click", () => { restore(); fit(); say.textContent = ""; dlg.showModal(); f.mode.focus(); });
  f.mode.addEventListener("change", fit);
  $("#la-close").addEventListener("click", () => dlg.close());
  $("#la-off").addEventListener("click", () => { setArmed(false); dlg.close(); });
  form.addEventListener("submit", e => {
    e.preventDefault();
    const v = values(), bad = problem(v);
    if (bad) { say.textContent = bad; return; }
    keep(v);
    setArmed(true); dlg.close();
    $("#prompt").focus();
  });
  document.addEventListener("tadamoo:lang", () => { paint(); fit(); });

  window.longAnswer = {
    armed,
    /* the Chat: the `long_answer` field of the next request, or null; it stays on until the person turns it off */
    request() {
      return armed() ? options(load()) : null;
    },
    /* the Agent: the message with the one line the harness reads */
    decorate(text) {
      if (!armed()) return text;
      const v = load();
      const part = s => String(s).replace(/[\]\n;]+/g, " ").trim().slice(0, 80);
      const bits = [`mode=${v.mode}`, ...(v.mode === "write" ? [`words=${v.words}`] : []), ...(v.lang ? [`language=${part(v.lang)}`] : []),
        ...(v.instr && v.mode === "rewrite" ? [`task=${part(v.instr)}`] : []), ...(v.style ? [`style=${part(v.style)}`] : [])];
      return `${text}\n\n[long answer: ${bits.join("; ")}]`;
    },
  };
  restore(); fit(); paint();
})();
