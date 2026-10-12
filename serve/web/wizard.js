"use strict";
/* The configuration wizard ("Configurazione guidata"): finds the best values for this PC and saves them as "Machine
   Default" (a load profile, made the default, and a chat preset with the model's own sampling values).
   1 this PC: RAM, commit, page file, processor, GPU, video adapters; where the main monitor is (detected, confirmed
     here)  2 what to leave to the other programs: a share of RAM and commit, and VRAM  3 the model  4 the computed
   values and their prediction  5 an optional measurement on a real load  6 save. GET/POST /api/wizard. */
(() => {
  const S = {
    en: {
      title: "Configuration wizard", step: "Step {0} of {1}", back: "Back", next: "Next", close: "Close",
      intro: "This PC has no Machine Default yet. In a few steps the wizard finds the best values for it and saves them; you can run it again from the Loader.",
      never: "Do not offer it at start again", h1: "This PC", h2: "What to leave to the other programs", h3: "Model",
      h4: "Computed values", h5: "Measure (optional)", h6: "Save as Machine Default",
      ram: "RAM", commit: "Commit (Windows)", pagefile: "Page file", cpu: "Processor", gpu: "GPU", adapters: "Video adapters",
      ramv: "{0} GB, {1} GB available", commitv: "{0} of {1} GB in use ({2})", pfauto: "managed by Windows: it may grow. Basalt never makes it grow: every plan leaves part of the commit limit free.",
      pffixed: "fixed size", pfnone: "none", pfunk: "unknown", cores: "{0} cores", coresh: "{0} cores ({1} performance + {2} efficiency)",
      gpuv: "{0}, {1} GB of VRAM, {2} GB in use now", nogpu: "no NVIDIA GPU could be read",
      primary: "main monitor", outputs: "{0} monitor(s) on the desktop", noadapters: "This system does not say which adapter shows the monitor.",
      detected: "Detected: the main monitor is on {0}.", detectedSame: "Detected: this GPU ({0}) drives the main monitor.",
      secOff: "This GPU also has a secondary monitor connected (switched off now): turning it on takes a little VRAM.",
      secOn: "This GPU also drives a secondary monitor.", unknownMon: "Not detected: say where the main monitor is.",
      where: "Where is the main monitor connected?", wNone: "To another video adapter (integrated or separate): this GPU drives no monitor",
      wSecondary: "To another adapter, and this GPU has a secondary monitor too", wPrimary: "To this GPU ({0})",
      share: "RAM and commit left free (%)", shareh: "Proposed 15%. Never less than 2 GB; the page file never grows.",
      vram: "VRAM left free (GB)", vramh: "Proposed: {0}",
      model: "Model", nomodel: "No model is ready in the models folders: choose them in the Loader first.",
      sampling: "The model's own chat values: {0}", nosampling: "the model file sets no chat values",
      fits: "Fits", nofit: "Does not fit", need: "{0} GB needed, {1} available", speed: "Predicted: {0} tok/s answer, {1} tok/s prompt reading",
      measureh: "About 5 to 10 minutes: Basalt loads the computed values, checks the real memory after the load against the plan (smaller values if it is above), measures the speed, tries the drafting floor per request (0.3, 0.5, 0.7) and the verify windows 5, 7 and 8 (one reload each), and keeps the fastest. Without it the values stay as computed.",
      measure: "Measure now", measureLoad: "Load the model and measure", measureRestart: "Basalt starts again with this model; the measurement starts by itself and this page reconnects.",
      measuring: "Measuring: {0}…", measured: "Measured: {0} tok/s (answer), {1} tok/s (with reasoning), {2} tok/s (prompt reading).",
      cannot: "The measurement needs less than {0} of the commit limit in use (now {1}): close other programs, or skip it.",
      skipped: "Not measured: the values are the computed ones.", failed: "The measurement failed: {0}",
      steps: { check: "checking memory", load: "loading", calibrate: "calibrating", decode: "answer", reasoning: "answer with reasoning", prefill: "long prompt", spec_min_p: "drafting floor", spec: "verify window" },
      save: "Save as Machine Default", saved: "Saved: “Machine Default” is now the default load profile and the chat's default preset.",
      use: "Use in the Loader", summary: "Summary", left: "Left free: {0} of RAM and commit, {1} GB of VRAM; monitor: {2}",
      monitor: { primary: "on this GPU", secondary: "on another adapter, secondary on this GPU", none: "on another adapter" },
      windows: "Verify windows: {0}; kept {1}.", floors: "Drafting floor: {0}; kept {1}.",
      offer: "Configuration wizard", machine: "Restore Machine Default", machineDone: "Machine Default in the form: check the prediction, then Load.",
      noMachine: "There is no Machine Default yet: run the configuration wizard.",
    },
    it: {
      title: "Configurazione guidata", step: "Passo {0} di {1}", back: "Indietro", next: "Avanti", close: "Chiudi",
      intro: "Questo PC non ha ancora un Machine Default. In pochi passi la configurazione guidata trova i valori migliori e li salva; puoi rifarla dal Loader.",
      never: "Non proporla più all'avvio", h1: "Questo PC", h2: "Cosa lasciare agli altri programmi", h3: "Modello",
      h4: "Valori calcolati", h5: "Misura (facoltativa)", h6: "Salva come Machine Default",
      ram: "RAM", commit: "Commit (Windows)", pagefile: "File di paging", cpu: "Processore", gpu: "GPU", adapters: "Schede video",
      ramv: "{0} GB, {1} GB disponibili", commitv: "{0} di {1} GB in uso ({2})", pfauto: "gestito da Windows: può crescere. Basalt non lo fa mai crescere: ogni piano lascia libera una parte del limite di commit.",
      pffixed: "dimensione fissa", pfnone: "nessuno", pfunk: "sconosciuto", cores: "{0} core", coresh: "{0} core ({1} performance + {2} efficienza)",
      gpuv: "{0}, {1} GB di VRAM, {2} GB in uso ora", nogpu: "nessuna GPU NVIDIA leggibile",
      primary: "monitor principale", outputs: "{0} monitor sul desktop", noadapters: "Questo sistema non dice quale scheda mostra il monitor.",
      detected: "Rilevato: il monitor principale è collegato a {0}.", detectedSame: "Rilevato: questa GPU ({0}) pilota il monitor principale.",
      secOff: "Su questa GPU c'è anche un monitor secondario (spento ora): accenderlo usa un po' di VRAM.",
      secOn: "Questa GPU pilota anche un monitor secondario.", unknownMon: "Non rilevato: indica dov'è il monitor principale.",
      where: "Il monitor principale è collegato a:", wNone: "un'altra scheda video (integrata o separata): questa GPU non pilota monitor",
      wSecondary: "un'altra scheda, e su questa GPU c'è anche un monitor secondario", wPrimary: "questa GPU ({0})",
      share: "RAM e commit da lasciare liberi (%)", shareh: "Proposta 15%. Mai meno di 2 GB; il file di paging non cresce mai.",
      vram: "VRAM da lasciare libera (GB)", vramh: "Proposta: {0}",
      model: "Modello", nomodel: "Nessun modello pronto nelle cartelle dei modelli: sceglile prima nel Loader.",
      sampling: "Valori di chat del modello: {0}", nosampling: "il file del modello non indica valori di chat",
      fits: "Entra", nofit: "Non entra", need: "servono {0} GB, disponibili {1}", speed: "Previsione: {0} tok/s in risposta, {1} tok/s in lettura",
      measureh: "Circa 5-10 minuti: Basalt carica i valori calcolati, confronta la memoria reale dopo il caricamento con il piano (valori più piccoli se è sopra), misura la velocità, prova la soglia del drafter per richiesta (0,3, 0,5, 0,7) e le finestre di verifica 5, 7 e 8 (un ricaricamento ciascuna) e tiene la più veloce. Senza misura restano i valori calcolati.",
      measure: "Misura ora", measureLoad: "Carica il modello e misura", measureRestart: "Basalt riparte con questo modello; la misura parte da sola e questa pagina si ricollega.",
      measuring: "Misura in corso: {0}…", measured: "Misurato: {0} tok/s (risposta), {1} tok/s (con ragionamento), {2} tok/s (lettura del prompt).",
      cannot: "La misura vuole meno del {0} del limite di commit in uso (ora {1}): chiudi altri programmi, oppure saltala.",
      skipped: "Non misurato: restano i valori calcolati.", failed: "La misura non è riuscita: {0}",
      steps: { check: "controllo della memoria", load: "caricamento", calibrate: "calibrazione", decode: "risposta", reasoning: "risposta con ragionamento", prefill: "prompt lungo", spec_min_p: "soglia del drafter", spec: "finestra di verifica" },
      save: "Salva come Machine Default", saved: "Salvato: «Machine Default» ora è il profilo di caricamento predefinito e il preset di chat predefinito.",
      use: "Usa nel Loader", summary: "Riepilogo", left: "Liberi: {0} di RAM e commit, {1} GB di VRAM; monitor: {2}",
      monitor: { primary: "su questa GPU", secondary: "su un'altra scheda, secondario su questa GPU", none: "su un'altra scheda" },
      windows: "Finestre di verifica: {0}; tenuta {1}.", floors: "Soglia del drafter: {0}; tenuta {1}.",
      offer: "Configurazione guidata", machine: "Ripristina Machine Default", machineDone: "Machine Default nel modulo: controlla la previsione, poi Carica.",
      noMachine: "Non c'è ancora un Machine Default: avvia la configurazione guidata.",
    },
  };
  const t = (key, ...a) => {
    const v = key.split(".").reduce((o, k) => o && o[k], S[LANG]) ?? key.split(".").reduce((o, k) => o && o[k], S.en) ?? key;
    return String(v).replace(/\{(\d+)\}/g, (_, i) => String(a[+i] ?? ""));
  };
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const GB = n => n == null ? "—" : nfmt(n / 2 ** 30, 1);
  const STEPS = 6, KEEP = "basalt.wizard";
  let W = null;            // {step, info, answers, touched, model, prop, measured, measureNote, offered}

  async function api(path, body) {
    const r = await fetch(path, body === undefined ? { cache: "no-store", headers: authHeaders() } :
      { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    return { r, j };
  }
  const remember = () => { try { sessionStorage.setItem(KEEP, JSON.stringify({ ...W, info: null })); } catch (e) { /* kept in this page */ } };
  const forget = () => { try { sessionStorage.removeItem(KEEP); } catch (e) { /* nothing kept */ } };

  function dialog() {
    let d = $("#wz");
    if (d) return d;
    document.body.insertAdjacentHTML("beforeend", `<dialog id="wz" class="wz" aria-labelledby="wz-h">
      <form method="dialog" novalidate class="wz-form">
        <header class="wz-head"><div><h2 id="wz-h"></h2><p class="wz-stepno" id="wz-stepno"></p></div>
          <button value="cancel" class="btn ghost wz-x" id="wz-x"></button></header>
        <div class="wz-body" id="wz-body" tabindex="-1"></div>
        <footer class="wz-foot"><p class="setstatus wz-msg" id="wz-msg" role="status" aria-live="polite"></p>
          <button type="button" class="btn ghost" data-wz="back"></button>
          <button type="button" class="btn primary" data-wz="next"></button></footer>
      </form></dialog>`);
    d = $("#wz");
    d.addEventListener("click", e => {
      const b = e.target.closest("[data-wz]");
      if (!b || b.disabled) return;
      ({ back: () => go(W.step - 1), next: () => go(W.step + 1), measure: measure, save: save, use: useInLoader })[b.dataset.wz]?.();
    });
    d.addEventListener("change", onInput);
    d.addEventListener("input", onInput);
    d.addEventListener("close", () => { if (!W || !W.measuring) forget(); });
    return d;
  }
  const msg = (text, kind) => { const m = $("#wz-msg"); m.textContent = text || ""; m.dataset.kind = kind || ""; };

  async function open(opts = {}) {
    const d = dialog();
    let kept = null;
    try { kept = JSON.parse(sessionStorage.getItem(KEEP) || "null"); } catch (e) { kept = null; }
    W = kept && opts.resume ? kept : { step: 1, answers: {}, touched: {}, model: (window.BasaltModels && BasaltModels.selected()) || null,
      prop: null, measured: null, offered: !!opts.offered };
    $("#wz-h").textContent = t("title");
    $("#wz-x").textContent = t("close");
    $("#wz-x").setAttribute("aria-label", t("close"));
    if (!d.open) d.showModal();
    msg("…");
    const { r, j } = await api("api/wizard" + (W.model ? "?model=" + encodeURIComponent(W.model) : ""));
    if (!r.ok) { msg((j.error && j.error.message) || `HTTP ${r.status}`, "err"); return; }
    W.info = j;
    if (!W.prop) W.prop = j.proposal;
    if (W.answers.monitor == null) W.answers.monitor = j.proposal.monitor;
    if (W.answers.free_share == null) W.answers.free_share = j.proposal.free_share;
    if (W.answers.vram_reserve_mib == null) W.answers.vram_reserve_mib = j.proposal.vram_reserve_mib;
    msg("");
    go(W.step, true);
    if (W.step === 5 && j.status && j.status.job === "measure") watchMeasure();
  }

  async function go(step, first) {
    step = Math.max(1, Math.min(STEPS, step));
    if (step === 4 && (!W.prop || (!first && W.step < 4))) await replan();
    W.step = step;
    remember();
    $("#wz-stepno").textContent = t("step", step, STEPS);
    const body = $("#wz-body");
    body.innerHTML = [null, step1, step2, step3, step4, step5, step6][step]();
    const back = $("#wz [data-wz=back]"), next = $("#wz [data-wz=next]");
    back.textContent = t("back"); next.textContent = t("next");
    back.hidden = step === 1; next.hidden = step === STEPS;
    next.disabled = (step === 3 && !W.model) || (step === 4 && !W.prop);
    if (!first) body.focus();
    msg("");
  }

  /* ---- 1: this PC ---- */
  function step1() {
    const hw = W.info.hardware, s = hw.system, g = hw.gpu || {}, c = hw.cpu || {}, m = hw.monitor || {}, pf = hw.page_file || {};
    const pfText = !pf.known ? t("pfunk") : !(pf.files || []).length ? t("pfnone") :
      pf.files.map(f => `${f.name} ${nfmt((f.allocated_mib || 0) / 1024, 1)} GB`).join(", ") + " · " + (pf.automatic ? t("pfauto") : t("pffixed"));
    const used = hw.commit_share_used != null ? nfmt(hw.commit_share_used * 100, 0) + "%" : "—";
    const adapters = hw.displays == null ? `<p class="help">${esc(t("noadapters"))}</p>` : `<ul class="wz-list">${hw.displays.map(a =>
      `<li><b>${esc(a.name)}</b>${a.primary ? ` <span class="badge" data-s="def">${esc(t("primary"))}</span>` : ""}${a.outputs ? ` · ${esc(t("outputs", a.outputs))}` : ""}</li>`).join("")}</ul>`;
    const det = m.where === "primary" ? t("detectedSame", g.name || "GPU") : m.primary_adapter ? t("detected", m.primary_adapter) : t("unknownMon");
    const sec = m.where === "secondary" ? (m.secondary_off ? t("secOff") : t("secOn")) : "";
    const gw = hw.gpu_warning && (hw.gpu_warning[LANG] || hw.gpu_warning.en);
    const radio = (v, label) => `<label class="radio"><input type="radio" name="wz-mon" value="${v}" ${W.answers.monitor === v ? "checked" : ""}><span>${esc(label)}</span></label>`;
    return `${W.offered ? `<p class="setnote wz-intro">${esc(t("intro"))}</p>
        <label class="wz-check"><input type="checkbox" id="wz-never"><span>${esc(t("never"))}</span></label>` : ""}
      <h3>${esc(t("h1"))}</h3>
      <dl class="kv wz-kv">
        <dt>${esc(t("ram"))}</dt><dd>${esc(t("ramv", GB(s.ram_total), GB(s.ram_available)))}</dd>
        <dt>${esc(t("commit"))}</dt><dd>${esc(t("commitv", GB(s.commit_used), GB(s.commit_limit), used))}</dd>
        <dt>${esc(t("pagefile"))}</dt><dd>${esc(pfText)}</dd>
        <dt>${esc(t("cpu"))}</dt><dd>${esc(c.hybrid ? t("coresh", c.physical, c.p_cores, c.e_cores) : t("cores", c.physical || c.logical || "?"))}</dd>
        <dt>${esc(t("gpu"))}</dt><dd>${esc(g.name ? t("gpuv", g.name, GB(g.vram_total), GB((g.vram_total || 0) - (g.vram_free || 0))) : t("nogpu"))}</dd>
      </dl>
      ${gw ? `<p class="setnote warnnote wz-warn" role="alert">${esc(gw)}</p>` : ""}
      <h3>${esc(t("adapters"))}</h3>${adapters}
      <p class="wz-det">${esc(det)}${sec ? `<br>${esc(sec)}` : ""}</p>
      <fieldset class="wz-mon"><legend>${esc(t("where"))}</legend>
        ${radio("none", t("wNone"))}${radio("secondary", t("wSecondary"))}${radio("primary", t("wPrimary", g.name || "GPU"))}</fieldset>`;
  }

  /* ---- 2: what to leave free ---- */
  function step2() {
    const p = W.prop || {}, rp = p.reserve_proposal || {};
    return `<h3>${esc(t("h2"))}</h3>
      <div class="field"><label for="wz-share">${esc(t("share"))}</label>
        <input id="wz-share" type="number" inputmode="decimal" min="5" max="50" step="1" value="${Math.round((W.answers.free_share || 0.15) * 100)}" aria-describedby="wz-share-h">
        <p class="help" id="wz-share-h">${esc(t("shareh"))}</p></div>
      <div class="field"><label for="wz-vram">${esc(t("vram"))}</label>
        <input id="wz-vram" type="number" inputmode="decimal" min="0" max="64" step="0.25" value="${+((W.answers.vram_reserve_mib || 0) / 1024).toFixed(2)}" aria-describedby="wz-vram-h">
        <p class="help" id="wz-vram-h">${esc(t("vramh", rp[LANG] || rp.en || ""))}</p></div>`;
  }

  /* ---- 3: the model ---- */
  function step3() {
    const M = window.BasaltModels && BasaltModels.data();
    const ready = ((M && M.models) || []).filter(m => m.status === "ready");
    if (!ready.length) return `<h3>${esc(t("h3"))}</h3><p class="setnote warnnote">${esc(t("nomodel"))}</p>`;
    if (!W.model || !ready.some(m => same(m.path, W.model))) W.model = ready[0].path;
    const sel = ready.find(m => same(m.path, W.model)) || ready[0];
    const sm = sel.sampling || {};
    const sText = Object.keys(sm).length ? Object.entries(sm).map(([k, v]) => `${k} ${v}`).join(" · ") : t("nosampling");
    return `<h3>${esc(t("h3"))}</h3>
      <div class="field"><label for="wz-model">${esc(t("model"))}</label>
        <select id="wz-model">${ready.map(m => `<option value="${esc(m.path)}" ${same(m.path, W.model) ? "selected" : ""}>${esc(m.name)}${m.quant && !m.name.includes(m.quant) ? " · " + esc(m.quant) : ""}</option>`).join("")}</select></div>
      <p class="help">${esc(t("sampling", sText))}</p>`;
  }
  const same = (a, b) => !!a && !!b && a.replace(/\\/g, "/").toLowerCase() === b.replace(/\\/g, "/").toLowerCase();

  /* ---- 4: the computed values ---- */
  async function replan() {
    msg("…");
    const { r, j } = await api("api/wizard/plan", { model: W.model, answers: { monitor: W.answers.monitor,
      free_share: W.answers.free_share, vram_reserve_mib: W.touched.vram ? W.answers.vram_reserve_mib : null } });
    if (!r.ok) { msg((j.error && j.error.message) || `HTTP ${r.status}`, "err"); return; }
    W.prop = j;
    if (!W.touched.vram) W.answers.vram_reserve_mib = j.vram_reserve_mib;
    W.measured = null;
    msg("");
  }
  function fitRow(kind, label) {
    const p = W.prop, need = p.need[kind], have = p.have[kind];
    const ok = !(p.problems || []).some(x => x.code === kind || (kind === "ram" && x.code === "ple_ram"));
    return `<li data-ok="${ok}"><span>${ok ? "✓" : "✕"} ${esc(label)}</span><b>${esc(t("need", GB(need), GB(have)))}</b></li>`;
  }
  function step4() {
    const p = W.prop;
    if (!p) return `<h3>${esc(t("h4"))}</h3>`;
    const rows = Object.entries(p.values).map(([k, v]) => {
      const why = (p.why || {})[k];
      return `<dt>${esc(T("f." + k))}: <code>${esc(String(v))}</code></dt><dd>${esc(why ? why[LANG] || why.en : "")}</dd>`;
    }).join("");
    return `<h3>${esc(t("h4"))}</h3>
      <ul class="wz-fit">${fitRow("vram", "VRAM")}${fitRow("ram", "RAM")}${fitRow("commit", "Commit")}</ul>
      <p class="wz-speed">${esc(t("speed", nfmt(p.speed.decode, 0), nfmt(p.speed.prefill, 0)))}</p>
      ${(p.problems || []).map(x => `<p class="ferr">${esc(LANG === "it" ? x.text_it : x.text)}</p>`).join("")}
      <dl class="wz-values">${rows}</dl>`;
  }

  /* ---- 5: the measurement ---- */
  function step5() {
    const hw = W.info.hardware, can = hw.can_measure;
    const other = W.info.relaunch && W.model && W.info.running_model && !same(W.model, W.info.running_model) && !(W.info.hardware || {}).mock;
    const res = W.measured ? `<p class="wz-res" data-kind="ok">${esc(t("measured", nfmt(W.measured.decode, 0), nfmt(W.measured.decode_reasoning, 0), nfmt(W.measured.prefill, 0)))}</p>${tries()}` :
      W.measureNote ? `<p class="wz-res" data-kind="${W.measureNote.kind}">${esc(W.measureNote.text)}</p>` : "";
    return `<h3>${esc(t("h5"))}</h3><p class="setnote">${esc(t("measureh"))}</p>
      ${!can ? `<p class="setnote warnnote">${esc(t("cannot", nfmt(hw.measure_commit_max * 100, 0) + "%", nfmt((hw.commit_share_used || 0) * 100, 0) + "%"))}</p>` : ""}
      ${other ? `<p class="help">${esc(t("measureRestart"))}</p>` : ""}
      <div class="setactions"><button type="button" class="btn" data-wz="measure" ${can && W.prop && W.prop.fits ? "" : "disabled"}>${esc(other ? t("measureLoad") : t("measure"))}</button></div>
      <div class="wz-progress" id="wz-progress" aria-live="polite"></div>${res}`;
  }
  // every candidate the measurement tried, with its speed, and the one kept
  function tries() {
    const m = W.measured || {}, v = (W.prop || {}).values || {};
    const list = o => Object.entries(o || {}).map(([k, x]) => `${nfmt(Number(k), k.includes(".") ? 1 : 0)} → ${nfmt(x, 0)} tok/s`).join(" · ");
    return (m.spec ? `<p class="help">${esc(t("windows", list(m.spec), v["engine.spec"]))}</p>` : "") +
      (m.spec_min_p ? `<p class="help">${esc(t("floors", list(m.spec_min_p), nfmt(v["engine.spec-min-p"], 1)))}</p>` : "");
  }
  async function measure() {
    const { r, j } = await api("api/wizard/measure", { model: W.model, values: W.prop.values });
    if (!r.ok) { W.measureNote = { kind: "err", text: t("failed", (j.error && j.error.message) || `HTTP ${r.status}`) }; go(5); return; }
    W.measuring = true;
    remember();
    if (j.measure === "after_restart") { $("#wz-progress").textContent = t("measureRestart"); waitBack(); return; }
    watchMeasure();
  }
  function waitBack() {                     // the server restarts with the model: reload once it answers
    let down = false;
    const tick = async () => {
      let up = false;
      try { up = (await fetch("health", { cache: "no-store" })).ok; } catch (e) { up = false; }
      if (!up) down = true;
      if (up && down) { location.hash = "#loader"; location.search = "?wizard=resume"; return; }
      setTimeout(tick, 1000);
    };
    setTimeout(tick, 1000);
  }
  function watchMeasure() {
    const btn = $("#wz [data-wz=measure]"); if (btn) btn.disabled = true;
    let idleSince = 0;
    const tick = async () => {
      const { r, j } = await api("api/loader/status");
      if (!r.ok) { setTimeout(tick, 1500); return; }
      const box = $("#wz-progress");
      if ((j.job === "measure" && ["waiting", "measuring", "stopping", "starting", "calibrating"].includes(j.phase)) ||
          (j.job !== "measure" && (j.loading || !j.loaded))) {      // still loading the model the measurement uses
        if (box) box.textContent = t("measuring", t("steps." + (j.job === "measure" ? (j.step || "check") : "load")));
        setTimeout(tick, 800); return;
      }
      if (j.job !== "measure") {             // loaded, the measurement not started yet: wait a little
        idleSince = idleSince || Date.now();
        if (Date.now() - idleSince < 15000) { setTimeout(tick, 800); return; }
        W.measureNote = { kind: "err", text: t("failed", "—") };
      }
      if (j.job === "measure" && j.phase === "ready") {
        const out = JSON.parse(j.message || "{}");
        W.measured = out.measured; W.prop = { ...W.prop, values: out.values }; W.measureNote = null;
      } else if (j.job === "measure" && j.phase === "failed") {
        W.measureNote = { kind: "err", text: t("failed", j.error || "") };
      }
      W.measuring = false;
      remember();
      if (W.step === 5) go(5);
    };
    tick();
  }

  /* ---- 6: save ---- */
  function step6() {
    const p = W.prop || { values: {} };
    return `<h3>${esc(t("h6"))}</h3>
      <p>${esc(t("left", nfmt((W.answers.free_share || 0.15) * 100, 0) + "%", nfmt((W.answers.vram_reserve_mib || 0) / 1024, 2), t("monitor." + (W.answers.monitor || "none"))))}</p>
      <p class="help">${esc((window.BasaltModels && BasaltModels.name(W.model)) || "")} · ${esc(W.measured ? t("measured", nfmt(W.measured.decode, 0), nfmt(W.measured.decode_reasoning, 0), nfmt(W.measured.prefill, 0)) : t("skipped"))}</p>
      <div class="setactions"><button type="button" class="btn primary" data-wz="save" ${W.prop ? "" : "disabled"}>${esc(t("save"))}</button>
        <button type="button" class="btn" data-wz="use" hidden>${esc(t("use"))}</button></div>
      <p class="wz-res" id="wz-saved" role="status" aria-live="polite"></p>`;
  }
  async function save() {
    const { r, j } = await api("api/wizard/save", { model: W.model, values: W.prop.values, free_share: W.answers.free_share,
      vram_reserve_mib: W.answers.vram_reserve_mib, monitor: W.answers.monitor, measured: W.measured });
    const out = $("#wz-saved");
    if (!r.ok) { out.textContent = (j.error && j.error.message) || `HTTP ${r.status}`; out.dataset.kind = "err"; return; }
    out.textContent = t("saved"); out.dataset.kind = "ok";
    $("#wz [data-wz=use]").hidden = false; $("#wz [data-wz=save]").disabled = true;
    forget();
    document.dispatchEvent(new CustomEvent("basalt:machine"));
  }
  function useInLoader() {
    $("#wz").close();
    document.dispatchEvent(new CustomEvent("basalt:machine", { detail: { apply: true } }));
  }

  function onInput(e) {
    const el = e.target;
    if (el.name === "wz-mon") { W.answers.monitor = el.value; W.prop = null; if (!W.touched.vram) { W.answers.vram_reserve_mib = null; replan().then(() => remember()); } }
    else if (el.id === "wz-share") { const v = Number(el.value) / 100; if (Number.isFinite(v) && v >= 0.05 && v <= 0.5) { W.answers.free_share = v; W.prop = null; } }
    else if (el.id === "wz-vram") { const v = Number(String(el.value).replace(",", ".")); if (Number.isFinite(v) && v >= 0) { W.answers.vram_reserve_mib = Math.round(v * 1024); W.touched.vram = true; W.prop = null; } }
    else if (el.id === "wz-model") { W.model = el.value; W.prop = null; if (W.step === 3) go(3); }
    else if (el.id === "wz-never") { try { localStorage.setItem("basalt.wizard.skip", el.checked ? "1" : "0"); } catch (err) { /* asked again next time */ } }
    if (W.step === 4 || !W.prop) { const next = $("#wz [data-wz=next]"); if (next && W.step === 4) next.disabled = !W.prop; }
    remember();
  }

  window.BasaltWizard = {
    open,
    offer() {
      let skip = false;
      try { skip = localStorage.getItem("basalt.wizard.skip") === "1"; } catch (e) { skip = false; }
      if (!skip) open({ offered: true });
    },
    resume() { open({ resume: true }); },
    t,
  };

  /* after the server started again for the measurement (this page reloaded, or the app opened it again): go on */
  let kept = null;
  try { kept = JSON.parse(sessionStorage.getItem(KEEP) || "null"); } catch (e) { kept = null; }
  if (kept && kept.measuring && !new URLSearchParams(location.search).get("wizard")) {
    if (viewOn !== "loader") view("loader");
    open({ resume: true });
  }
})();
