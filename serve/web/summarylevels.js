"use strict";
/* Settings > Inference > Summarise a text: a text pasted or read from a file, one of four levels, and the summary written in the technical session
   (POST api/summarize). The work can take minutes for a long text; the stop button of the technical session ends it. Everything the server says is put in the page as text. */
(() => {
  const root = document.getElementById("sl-root");
  if (!root) return;
  const LEVELS = ["integral", "medium", "high", "executive"];
  const q = id => document.getElementById(id);
  const input = q("sl-text"), level = q("sl-level"), lang = q("sl-lang"), go = q("sl-go"), note = q("sl-note"), out = q("sl-out");
  let busy = false, last = null;

  const say = (text, bad) => { note.textContent = text || ""; note.dataset.bad = bad ? "1" : ""; };
  const num = n => new Intl.NumberFormat(LANG === "it" ? "it-IT" : "en-US").format(n);

  function paintLevels() {
    const keep = level.value || "medium";
    level.replaceChildren(...LEVELS.map(l => Object.assign(document.createElement("option"), { value: l, textContent: T("sm.l." + l) })));
    level.value = keep;
    q("sl-level-help").textContent = T("sm.l." + level.value + ".d");
  }

  function paintResult() {
    q("sl-result").hidden = !last;
    if (!last) return;
    out.value = last.summary;
    q("sl-stats").textContent = T("sm.stats", num(last.chars_in), num(last.chars_out), Math.round(100 * last.chars_out / Math.max(1, last.chars_in)), last.parts, T(last.side ? "sm.where.side" : "sm.where.slot"));
  }

  function refresh() {
    go.disabled = busy || !input.value.trim();
    go.textContent = busy ? T("sm.working") : T("sm.go");
  }

  async function run() {
    if (busy || !input.value.trim()) return;
    busy = true; last = null; refresh(); paintResult(); say(T("sm.running"));
    try {
      const r = await fetch("api/summarize", { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ text: input.value, level: level.value, ...(lang.value.trim() ? { language: lang.value.trim() } : {}) }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error((j.error && j.error.message) || `HTTP ${r.status}`);
      last = j; say(T("sm.done"));
    } catch (e) { say(T("sm.failed", e.message), true); }
    busy = false; refresh(); paintResult();
  }

  q("sl-file").addEventListener("change", async e => {
    const f = e.target.files && e.target.files[0];
    e.target.value = "";
    if (!f) return;
    if (f.size > 8 << 20) return say(T("sm.toolarge"), true);
    try { input.value = await f.text(); say(T("sm.loaded", f.name, num(input.value.length))); } catch (err) { say(T("sm.failed", err.message), true); }
    refresh();
  });
  input.addEventListener("input", refresh);
  level.addEventListener("change", () => { q("sl-level-help").textContent = T("sm.l." + level.value + ".d"); });
  go.addEventListener("click", run);
  q("sl-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(out.value); say(T("sm.copied")); } catch (e) { out.select(); say(T("sm.select")); }
  });
  q("sl-save").addEventListener("click", () => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([out.value + "\n"], { type: "text/markdown" }));
    a.download = `summary-${level.value}.md`;
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
  });
  q("sl-clear").addEventListener("click", () => { input.value = ""; last = null; say(""); refresh(); paintResult(); });
  document.addEventListener("tadamoo:lang", () => { paintLevels(); refresh(); paintResult(); });
  paintLevels(); refresh(); paintResult();
  applyT(root);
})();
