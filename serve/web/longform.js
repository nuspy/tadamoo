"use strict";
/* The Long answer control: a button beside Send that opens a small dialog. The options apply to the next message only.
   In the Chat they go to the server as `long_answer` (serve/longform.py writes the text in parts); in the Agent the message carries one
   line that the harness reads, and the harness itself writes the text in parts. */
(() => {
  const KEY = "basalt.longanswer";
  const MODES = ["write", "translate", "rewrite"];
  const load = () => { try { const v = JSON.parse(localStorage.getItem(KEY) || "{}"); return v && typeof v === "object" ? v : {}; } catch (e) { return {}; } };
  const keep = v => { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) { /* the options are kept for this page only */ } };

  const dlg = $("#long-dlg"), open = $("#long-open"), form = $("#la-form"), say = $("#la-msg");
  const f = { mode: $("#la-mode"), words: $("#la-words"), lang: $("#la-lang"), instr: $("#la-instr"), style: $("#la-style"), gloss: $("#la-gloss"), protect: $("#la-protect") };
  let armed = false;

  function restore() {
    const s = load();
    f.mode.value = MODES.includes(s.mode) ? s.mode : "write";
    f.words.value = s.words || 3000;
    f.lang.value = s.lang || ""; f.instr.value = s.instr || ""; f.style.value = s.style || ""; f.gloss.value = s.gloss || "";
    f.protect.checked = s.protect !== false;
  }
  const values = () => ({ mode: f.mode.value, words: Number(f.words.value) || 0, lang: f.lang.value.trim(), instr: f.instr.value.trim(), style: f.style.value.trim(),
    gloss: f.gloss.value, protect: f.protect.checked });
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
    open.setAttribute("aria-pressed", String(armed));
    open.textContent = T(armed ? "la.open.on" : "la.open");
    open.dataset.on = armed ? "1" : "";
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
  function disarm() { armed = false; paint(); }

  open.addEventListener("click", () => { restore(); fit(); say.textContent = ""; dlg.showModal(); f.mode.focus(); });
  f.mode.addEventListener("change", fit);
  $("#la-close").addEventListener("click", () => dlg.close());
  $("#la-off").addEventListener("click", () => { disarm(); dlg.close(); });
  form.addEventListener("submit", e => {
    e.preventDefault();
    const v = values(), bad = problem(v);
    if (bad) { say.textContent = bad; return; }
    keep(v);
    armed = true; paint(); dlg.close();
    $("#prompt").focus();
  });
  document.addEventListener("basalt:lang", () => { paint(); fit(); });

  window.longAnswer = {
    armed: () => armed,
    /* the Chat: the `long_answer` field of the next request, or null; the control turns itself off */
    request() {
      if (!armed) return null;
      const o = options(load());
      disarm();
      return o;
    },
    /* the Agent: the message with the one line the harness reads */
    decorate(text) {
      if (!armed) return text;
      const v = load();
      disarm();
      const part = s => String(s).replace(/[\]\n;]+/g, " ").trim().slice(0, 80);
      const bits = [`mode=${v.mode}`, ...(v.mode === "write" ? [`words=${v.words}`] : []), ...(v.lang ? [`language=${part(v.lang)}`] : []),
        ...(v.instr && v.mode === "rewrite" ? [`task=${part(v.instr)}`] : []), ...(v.style ? [`style=${part(v.style)}`] : [])];
      return `${text}\n\n[long answer: ${bits.join("; ")}]`;
    },
  };
  restore(); fit(); paint();
})();
