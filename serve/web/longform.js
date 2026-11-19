"use strict";
/* The Long answer control: a button beside Send that opens a small dialog. Confirming it chooses how long it stays armed - once (the default: only
   the next prompt, then it turns itself off) or for the whole conversation, until it is turned off by hand; a new conversation starts as the "new
   conversations" box says. In the Chat the options go to the server as `long_answer` (serve/longform.py writes the text in parts); in the Agent
   the message carries one line that the harness reads, and the harness itself writes the text in parts.
   How long (`size`): "fixed" asks for the words given, as before. "heuristic" gives no number at all: the harness judges once it has actually
   looked at the request (serve/longform.py's own estimate, already there for `write_long` with no word count - this only reaches it from the
   person's own control for the first time). "hybrid" gives an approximate number that bounds that same judgment instead of fixing it. */
(() => {
  const KEY = "tadamoo.longanswer", ON_KEY = "tadamoo.longanswer.on", MAX_KEPT = 200, NEW = "new";
  const MODES = ["write", "translate", "rewrite"], SIZES = ["fixed", "heuristic", "hybrid"];
  const load = () => { try { const v = JSON.parse(localStorage.getItem(KEY) || "{}"); return v && typeof v === "object" ? v : {}; } catch (e) { return {}; } };
  const keep = v => { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) { /* the options are kept for this page only */ } };

  const dlg = $("#long-dlg"), open = $("#long-open"), say = $("#la-msg");
  const f = { mode: $("#la-mode"), size: $("#la-size"), words: $("#la-words"), lang: $("#la-lang"), instr: $("#la-instr"), style: $("#la-style"), gloss: $("#la-gloss"),
    protect: $("#la-protect"), fresh: $("#la-default") };
  /* which conversations have it on: {id: true | "once" | undefined}, plus "new" for the one not started yet; `defaults` is what a new one starts with */
  const loadOn = () => { try { const v = JSON.parse(localStorage.getItem(ON_KEY) || "{}"); return v && typeof v === "object" ? v : {}; } catch (e) { return {}; } };
  const keepOn = v => {
    const ids = Object.keys(v);
    for (const id of ids.slice(0, Math.max(0, ids.length - MAX_KEPT))) delete v[id];
    try { localStorage.setItem(ON_KEY, JSON.stringify(v)); } catch (e) { /* it is kept for this page only */ }
  };
  let on = loadOn();
  const scope = () => (window.chatUi && window.chatUi.sessionId && window.chatUi.sessionId()) || NEW;
  const defaults = () => load().fresh === true;
  const armed = () => { const v = on[scope()]; return v === undefined ? defaults() : v !== false; };
  const setArmed = value => { on[scope()] = value; keepOn(on); paint(); };
  /* "once" turns itself off right after the one message it was armed for is actually sent (decorate/request, called exactly there) */
  const useOnce = () => { if (on[scope()] === "once") setArmed(false); };

  function restore() {
    const s = load();
    f.mode.value = MODES.includes(s.mode) ? s.mode : "write";
    f.size.value = SIZES.includes(s.size) ? s.size : "fixed";
    f.words.value = s.words || 3000;
    f.lang.value = s.lang || ""; f.instr.value = s.instr || ""; f.style.value = s.style || ""; f.gloss.value = s.gloss || "";
    f.protect.checked = s.protect !== false;
    f.fresh.checked = s.fresh === true;
  }
  const values = () => ({ mode: f.mode.value, size: f.size.value, words: Number(f.words.value) || 0, lang: f.lang.value.trim(), instr: f.instr.value.trim(),
    style: f.style.value.trim(), gloss: f.gloss.value, protect: f.protect.checked, fresh: f.fresh.checked });
  function fit() {                                     // each mode and size shows the fields it uses
    const m = f.mode.value, s = f.size.value;
    $("#la-size-box").hidden = m !== "write";           // a translation or a rewrite has no length to judge: it follows the source
    $("#la-words-box").hidden = m === "translate" || (m === "write" && s === "heuristic");
    if (m === "write") $("#la-words-box").querySelector("label").textContent = T(s === "hybrid" ? "la.words.base" : "la.words");
    $("#la-instr-box").hidden = m !== "rewrite";
    $("#la-lang-box").querySelector("label").textContent = T(m === "translate" ? "la.lang.to" : "la.lang");
  }
  function problem(v) {
    if (v.mode === "write" && v.size !== "heuristic" && (v.words < 50 || v.words > 200000)) return T("la.bad.words");
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
    if (v.mode === "write") { o.size = v.size; if (v.size !== "heuristic") o.target_words = v.words; }
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
  f.size.addEventListener("change", fit);
  $("#la-close").addEventListener("click", () => dlg.close());
  $("#la-off").addEventListener("click", () => { setArmed(false); dlg.close(); });
  function confirm(scopeValue) {
    const v = values(), bad = problem(v);
    if (bad) { say.textContent = bad; return; }
    keep(v);
    setArmed(scopeValue); dlg.close();
    $("#prompt").focus();
  }
  $("#la-once").addEventListener("click", () => confirm("once"));
  $("#la-on").addEventListener("click", () => confirm(true));
  document.addEventListener("tadamoo:lang", () => { paint(); fit(); });

  window.longAnswer = {
    armed,
    /* the Chat: the `long_answer` field of the next request, or null; "once" turns itself off right after this call */
    request() {
      if (!armed()) return null;
      const o = options(load());
      useOnce();
      return o;
    },
    /* the Agent: the message with the one line the harness reads; "once" turns itself off right after this call */
    decorate(text) {
      if (!armed()) return text;
      const v = load();
      const part = s => String(s).replace(/[\]\n;]+/g, " ").trim().slice(0, 80);
      const bits = [`mode=${v.mode}`, ...(v.mode === "write" ? [`size=${v.size}`, ...(v.size !== "heuristic" ? [`words=${v.words}`] : [])] : []),
        ...(v.lang ? [`language=${part(v.lang)}`] : []), ...(v.instr && v.mode === "rewrite" ? [`task=${part(v.instr)}`] : []), ...(v.style ? [`style=${part(v.style)}`] : [])];
      useOnce();
      return `${text}\n\n[long answer: ${bits.join("; ")}]`;
    },
  };
  restore(); fit(); paint();
})();
