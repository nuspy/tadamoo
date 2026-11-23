"use strict";
/* Grammar and language check. Two parts that share the list language -> model (GET/POST api/language-check):
   1. Settings > Inference > Language check: the list (a language, a provider of the Providers page that runs here, optionally a model name) and the switch for
      the automatic offer after a translation.
   2. In the Chat, under an answer: a "Check grammar" button with a language menu, and, when the request was a translation into a language that has a model,
      the offer to check it. The corrected text is shown under the answer to copy; the answer itself is not changed.
   Everything the server says is put in the page as text. */
(() => {
  const q = id => document.getElementById(id);
  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const num = n => new Intl.NumberFormat(LANG === "it" ? "it-IT" : "en-US").format(n);
  async function call(path, payload) {
    const r = await fetch(path, payload === undefined ? { cache: "no-store", headers: authHeaders() }
      : { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(payload) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((j.error && j.error.message) || `HTTP ${r.status}`);
    return j;
  }
  const langName = (state, code) => ((state.supported || []).find(s => s.code === code) || { name: code }).name;
  let cached = null, cachedAt = 0;
  async function state(force) {
    if (!force && cached && Date.now() - cachedAt < 20000) return cached;
    cached = await call("api/language-check"); cachedAt = Date.now();
    return cached;
  }

  /* ---- Settings: the list ---- */
  const root = q("lk-root");
  if (root) {
    const note = q("lk-note"), list = q("lk-list"), lang = q("lk-lang"), prov = q("lk-prov"), model = q("lk-model"), add = q("lk-add"), auto = q("lk-auto");
    let view = null;
    const say = (text, bad) => { note.textContent = text || ""; note.dataset.bad = bad ? "1" : ""; };
    function draw() {
      if (!view) return;
      auto.checked = view.auto_offer;
      const keep = lang.value;
      lang.replaceChildren(...view.supported.map(s => Object.assign(h("option"), { value: s.code, textContent: s.name })));
      if (keep) lang.value = keep;
      prov.replaceChildren(...view.providers.map(p => Object.assign(h("option"), { value: p.id, textContent: p.name + (p.model ? " · " + p.model : "") })));
      add.disabled = !view.providers.length;
      q("lk-noprov").hidden = !!view.providers.length;
      const codes = Object.keys(view.languages);
      q("lk-empty").hidden = !!codes.length;
      list.replaceChildren(...codes.map(code => {
        const row = view.languages[code];
        const rm = h("button", "btn ghost", T("lk.remove")); rm.type = "button";
        rm.setAttribute("aria-label", T("lk.remove.of", langName(view, code)));
        rm.onclick = () => post({ remove: code });
        return h("li", "lkrow", h("span", "lkname", langName(view, code)),
          h("span", "lkmodel" + (row.ok ? "" : " lkbad"), row.ok ? row.provider_name + (row.model ? " · " + row.model : "") : T("lk.gone")), rm);
      }));
    }
    async function post(payload) {
      try { view = await call("api/language-check", payload); cached = view; cachedAt = Date.now(); say(T("lk.saved")); draw(); } catch (e) { say(e.message, true); }
    }
    async function load() { try { view = await state(true); draw(); } catch (e) { say(T("lk.failed", e.message), true); } }
    add.addEventListener("click", () => post({ set: { language: lang.value, provider: prov.value, model: model.value.trim() } }));
    auto.addEventListener("change", () => post({ auto_offer: auto.checked }));
    document.addEventListener("tadamoo:view", e => { if (e.detail === "settings") load(); });
    document.addEventListener("tadamoo:lang", draw);
    if (document.body.dataset.view === "settings") load();
    applyT(root);
  }

  /* ---- the Chat: the button under an answer, the panel and the offer ---- */
  function mount(acts, ops) {
    const panel = h("div", "lkpanel"); panel.hidden = true;
    const btn = h("button", "ghost lkbtn", T("lk.btn")); btn.type = "button"; btn.title = T("lk.btn.tip");
    let busy = false;

    async function open(code, offered) {
      let st;
      try { st = await state(false); } catch (e) { return; }
      const codes = Object.keys(st.languages);
      panel.replaceChildren(); panel.hidden = false;
      if (!codes.length) {
        panel.append(h("p", "lknote", T("lk.none")));
        const go = h("button", "btn", T("lk.setup")); go.type = "button";
        go.onclick = () => { const t = q("tab-settings"); if (t) t.click(); setTimeout(() => { const b = [...document.querySelectorAll("#viewsub button")].find(x => /Inference|Inferenza/.test(x.textContent)); if (b) b.click(); }, 50); };
        const close = h("button", "btn ghost", T("lk.close")); close.type = "button"; close.onclick = () => { panel.hidden = true; };
        panel.append(h("div", "lkactions", go, close));
        return;
      }
      const sel = h("select"); sel.id = "lk-pick-" + Math.random().toString(36).slice(2, 7);
      sel.append(...codes.map(c => Object.assign(h("option"), { value: c, textContent: langName(st, c) })));
      sel.value = codes.includes(code) ? code : codes[0];
      const label = h("label", "", T("lk.language")); label.htmlFor = sel.id;
      const run = h("button", "btn primary", T("lk.run")); run.type = "button";
      const close = h("button", "btn ghost", T("lk.close")); close.type = "button";
      const status = h("p", "lknote", offered ? T("lk.offer", langName(st, code)) : ""); status.setAttribute("role", "status");
      const result = h("div", "lkresult"); result.hidden = true;
      close.onclick = () => { panel.hidden = true; };
      run.onclick = async () => {
        if (busy) return;
        const text = ops.answer();
        if (!text) return;
        busy = true; run.disabled = true; result.hidden = true; status.textContent = T("lk.working"); status.dataset.bad = "";
        try {
          const j = await call("api/language-check/run", { text, language: sel.value });
          const out = h("textarea"); out.readOnly = true; out.value = j.text; out.rows = Math.min(14, Math.max(4, j.text.split("\n").length + 1)); out.setAttribute("aria-label", T("lk.result"));
          const copy = h("button", "btn", T("lk.copy")); copy.type = "button";
          copy.onclick = async () => { try { await navigator.clipboard.writeText(j.text); status.textContent = T("lk.copied"); } catch (e) { out.select(); status.textContent = T("lk.select"); } };
          result.replaceChildren(out, h("div", "lkactions", copy));
          result.hidden = false;
          status.textContent = j.changed ? T("lk.done", j.changed, j.parts) + (j.kept ? " " + T("lk.kept", j.kept) : "") : T("lk.nochange") + (j.kept ? " " + T("lk.kept", j.kept) : "");
        } catch (e) { status.textContent = T("lk.failed", e.message); status.dataset.bad = "1"; }
        busy = false; run.disabled = false;
      };
      panel.append(h("div", "lkrowform", h("div", "lkfield", label, sel)), h("div", "lkactions", run, close), status, result);
    }
    btn.onclick = () => { if (panel.hidden) open(""); else panel.hidden = true; };
    acts.append(btn);
    acts.after(panel);
    if (ops.offer) {                                                      // a translation into a language that has a model: offer it once
      call("api/language-check/offer", { request: ops.request() }).then(j => { if (j.language) open(j.language, true); }).catch(() => { /* no offer */ });
    }
    return panel;
  }

  window.langCheck = { mount, state };
})();
