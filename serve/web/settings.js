"use strict";
/* Settings: the app's own preferences only. The language (serve/lang.py) and the share of the RAM and of the commit
   limit every plan keeps free for other programs (GET/POST /api/prefs), and whether the configuration wizard is
   offered at start (this browser). Every value decided when the model loads (context, KV cache, speculation, VRAM to
   leave free, port, ...) is in the Loader: the "Loading parameters" card here shows what runs and opens the Loader
   (loader.js fills it); the models folders and the default server model are models.js's. */
(() => {
  const msg = (el, text, kind) => { el.textContent = text; el.dataset.kind = kind || ""; };
  async function prefs() {
    const r = await fetch("api/prefs", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  }
  async function postPrefs(body) {
    const r = await fetch("api/prefs", { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((j.error && j.error.message) || `HTTP ${r.status}`);
    return j;
  }

  /* the user's language: the app, this page, the scripts and the command-line tools follow it */
  applyT($("#view-settings"));
  async function refresh() {
    try {
      const p = await prefs();
      $("#st-lang").value = p.chosen || "auto";
      const s = p.free_share || {};
      const share = $("#st-share");
      share.min = String(Math.round((s.min ?? 0.05) * 100));
      share.max = String(Math.round((s.max ?? 0.5) * 100));
      share.placeholder = String(Math.round((s.default ?? 0.15) * 100));
      if (document.activeElement !== share) share.value = String(Math.round((s.value ?? s.default ?? 0.15) * 100));
    } catch (e) { /* the controls keep what they show */ }
    let skip = false;
    try { skip = localStorage.getItem("basalt.wizard.skip") === "1"; } catch (e) { skip = false; }
    $("#st-wizard").checked = !skip;
  }
  $("#st-lang").addEventListener("change", async e => {
    const v = e.target.value;
    try { await postPrefs({ lang: v === "auto" ? null : v }); }
    catch (err) { msg($("#st-lang-msg"), err.message, "err"); return; }
    try { localStorage.removeItem("basalt.lang"); } catch (err) { /* nothing kept here */ }
    msg($("#st-lang-msg"), T("st.lang.saved"), "ok");
    setTimeout(() => location.reload(), 600);
  });

  /* the share of the RAM and of the commit limit left to the other programs (the wizard proposes it too) */
  $("#st-share-form").addEventListener("submit", async e => {
    e.preventDefault();
    const el = $("#st-share"), out = $("#st-share-msg"), n = Number(el.value);
    el.removeAttribute("aria-invalid");
    if (el.value.trim() === "" || !Number.isFinite(n) || n < +el.min || n > +el.max) {
      el.setAttribute("aria-invalid", "true");
      msg(out, T("st.share.bad", el.min, el.max), "err");
      el.focus();
      return;
    }
    try {
      const p = await postPrefs({ free_share: n / 100 });
      msg(out, T("st.share.saved", nfmt(p.free_share.value * 100, 0) + "%"), "ok");
      document.dispatchEvent(new CustomEvent("basalt:prefs"));
    } catch (err) { msg(out, T("st.share.fail", err.message), "err"); }
  });

  /* the configuration wizard at start (BasaltWizard.offer reads the same key) */
  $("#st-wizard").addEventListener("change", e => {
    try { localStorage.setItem("basalt.wizard.skip", e.target.checked ? "0" : "1"); } catch (err) { /* asked again */ }
  });

  document.addEventListener("basalt:view", e => { if (e.detail === "settings") refresh(); });
  if (viewOn === "settings") refresh();
})();
