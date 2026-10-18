"use strict";
/* Download: the models Basalt can run, to download from Hugging Face (GET /api/models/catalog). Each card says what the
   file is and how it differs from the base model; “Download…” opens the full description, the plan (files, size,
   disk, access), the license to accept and starts the job (POST /api/models/download-plan | download, GET
   /api/models/download). A gated repository needs a Hugging Face token: a small assistant explains how to get it and
   takes it for this session only (POST /api/models/hf-token); nothing secret is stored or shown. A GGUF set appears
   in the list as “To convert” when it is downloaded. */
(() => {
  const S = {
    en: {
      title: "Download models", count: "{0} available", intro: "Qwen3.8-Flash-Next in the versions that run on Basalt, to download from Hugging Face. Ready files load as they are; GGUF files are converted here after the download. Every file is checked against its SHA-256, and an interrupted download continues where it stopped.",
      show: "Show", f: { all: "All", ready: "Ready to use (.basalt)", convert: "GGUF to convert", uncensored: "Uncensored", base: "Base model", coder: "Coder", swift: "Swift 1.5", unsloth: "Unsloth", no: "Not compatible" },
      ready: "Ready to use", convert: "To convert", untested: "Not tried in Basalt", unc: "Uncensored", gate: "Needs a Hugging Face token", here: "On this PC", no: "Not compatible",
      details: "Details", fidelity: "How close to the full model", page: "Model page on Hugging Face", get: "Download…", why: "Why not",
      lic: "License: {0}", size: "{0} GB",
      foot: "Reviewed {0}. Sizes are for the files at the pinned revision; the figures come from the source named in each description and are not comparable between sources.",
      dlg: "Download “{0}”", checking: "Checking the files, the disk and the access…", files: "Files", total: "To download: {0} GB of {1} GB (the rest is already here)",
      free: "Free on the disk: {0} GB", dest: "Download into", destHelp: "A subfolder is created inside it. Choose one of your models folders to see the model in the list.",
      notIn: "This folder is not one of your models folders: the model will not appear in the list until you add it in Settings.",
      conv: "These are GGUF files: the conversion needs about as much free disk space again afterwards.",
      accept: "I have read the license summary above and accept it{0}.", acceptWarn: " and the warning", cancel: "Cancel", go: "Download ({0} GB)",
      access: "Hugging Face access", accessOk: "Access is set: {0}.", accessUser: "account {0}", accessNo: "No token found.", setup: "Set up access…",
      src: { session: "pasted for this session", env: "the HF_TOKEN variable", file: "the Hugging Face login file" },
      job: "Downloading “{0}”", phase: { running: "downloading", verifying: "checking the file", done: "done", failed: "failed", cancelled: "stopped", idle: "" },
      stop: "Stop the download", resume: "Continue…", log: "Log", file: "file {0} of {1}: {2}", speed: "{0} MB/s", left: "about {0} left",
      doneConv: "Downloaded to {0}. It is in the list as “To convert”: press Convert… in the model list above.", doneReady: "Downloaded to {0}. It is ready: choose it in the model list above.",
      stopped: "Stopped. The partial files stay and the download continues from them.",
      err: { network: "No answer from Hugging Face: check the connection.", token: "Hugging Face refuses the token.", terms: "The terms of this repository have not been accepted yet.", hash: "A file did not match its SHA-256 and was discarded.", disk: "The disk is full.", missing_file: "A file is no longer on Hugging Face.", redirect: "Hugging Face sent the download to an untrusted address; it was stopped.", io: "The file could not be written." },
      hfTitle: "Hugging Face access", hfIntro: "This repository is gated: Hugging Face asks for a free account, for you to accept its terms, and for a token. Follow the steps; nothing is saved by Basalt unless you set it yourself.",
      s1: "Create a free account (or sign in)", s1b: "Open huggingface.co/join", s2: "Accept the terms on the model's page", s2b: "Open the model page",
      s3: "Create a read-only token", s3b: "Open the token settings", s3h: "Choose “Create new token”, type Read, and copy the token (it starts with hf_).",
      s4: "Give the token to Basalt", s4a: "Paste it here (kept in memory until the server stops, never saved)", tokenLbl: "Token", use: "Use this token", forget: "Forget the pasted token",
      s4b: "Or set it once for every start, in PowerShell:", s4c: "Or log in with Hugging Face's own tool (it needs its package first):", copy: "Copy", copied: "Copied",
      close: "Close", checkNow: "Check again", bad: "Hugging Face does not accept this token.", saved: "Token accepted: account {0}.",
    },
    it: {
      title: "Scarica modelli", count: "{0} disponibili", intro: "Qwen3.8-Flash-Next nelle versioni che girano su Basalt, da scaricare da Hugging Face. I file pronti si caricano così come sono; i GGUF si convertono qui dopo il download. Ogni file è controllato con il suo SHA-256 e un download interrotto riprende da dove si è fermato.",
      show: "Mostra", f: { all: "Tutti", ready: "Pronti all'uso (.basalt)", convert: "GGUF da convertire", uncensored: "Uncensored", base: "Modello base", coder: "Coder", swift: "Swift 1.5", unsloth: "Unsloth", no: "Non compatibili" },
      ready: "Pronto all'uso", convert: "Da convertire", untested: "Non provato con Basalt", unc: "Uncensored", gate: "Serve un token Hugging Face", here: "Su questo PC", no: "Non compatibile",
      details: "Dettagli", fidelity: "Quanto è vicino al modello pieno", page: "Pagina del modello su Hugging Face", get: "Scarica…", why: "Perché no",
      lic: "Licenza: {0}", size: "{0} GB",
      foot: "Rivisto il {0}. Le dimensioni sono quelle dei file alla revisione fissata; le misure vengono dalla fonte indicata in ogni descrizione e non sono confrontabili tra fonti diverse.",
      dlg: "Scaricare «{0}»?", checking: "Controllo i file, il disco e l'accesso…", files: "File", total: "Da scaricare: {0} GB su {1} GB (il resto è già qui)",
      free: "Liberi sul disco: {0} GB", dest: "Scarica in", destHelp: "Al suo interno viene creata una sottocartella. Scegli una delle tue cartelle dei modelli per vedere il modello nell'elenco.",
      notIn: "Questa cartella non è una delle tue cartelle dei modelli: il modello non comparirà nell'elenco finché non la aggiungi nelle Impostazioni.",
      conv: "Sono file GGUF: poi la conversione richiede altrettanto spazio libero su disco.",
      accept: "Ho letto il riassunto della licenza qui sopra e lo accetto{0}.", acceptWarn: " e l'avviso", cancel: "Annulla", go: "Scarica ({0} GB)",
      access: "Accesso a Hugging Face", accessOk: "Accesso impostato: {0}.", accessUser: "account {0}", accessNo: "Nessun token trovato.", setup: "Imposta l'accesso…",
      src: { session: "incollato per questa sessione", env: "la variabile HF_TOKEN", file: "il file di login di Hugging Face" },
      job: "Scarico «{0}»", phase: { running: "in corso", verifying: "controllo il file", done: "finito", failed: "non riuscito", cancelled: "fermato", idle: "" },
      stop: "Ferma il download", resume: "Continua…", log: "Registro", file: "file {0} di {1}: {2}", speed: "{0} MB/s", left: "mancano circa {0}",
      doneConv: "Scaricato in {0}. È nell'elenco come «Da convertire»: premi Converti… nell'elenco dei modelli qui sopra.", doneReady: "Scaricato in {0}. È pronto: sceglilo nell'elenco dei modelli qui sopra.",
      stopped: "Fermato. I file parziali restano e il download continua da lì.",
      err: { network: "Nessuna risposta da Hugging Face: controlla la connessione.", token: "Hugging Face rifiuta il token.", terms: "I termini di questo repository non sono ancora stati accettati.", hash: "Un file non corrispondeva al suo SHA-256 ed è stato scartato.", disk: "Il disco è pieno.", missing_file: "Un file non è più su Hugging Face.", redirect: "Hugging Face ha mandato il download a un indirizzo non fidato: è stato fermato.", io: "Non è stato possibile scrivere il file." },
      hfTitle: "Accesso a Hugging Face", hfIntro: "Questo repository è ad accesso controllato: Hugging Face chiede un account gratuito, che tu ne accetti i termini e un token. Segui i passi; Basalt non salva nulla a meno che non lo imposti tu.",
      s1: "Crea un account gratuito (o accedi)", s1b: "Apri huggingface.co/join", s2: "Accetta i termini nella pagina del modello", s2b: "Apri la pagina del modello",
      s3: "Crea un token di sola lettura", s3b: "Apri le impostazioni dei token", s3h: "Scegli «Create new token», tipo Read, e copia il token (inizia con hf_).",
      s4: "Dai il token a Basalt", s4a: "Incollalo qui (resta in memoria finché il server non si ferma, non viene mai salvato)", tokenLbl: "Token", use: "Usa questo token", forget: "Dimentica il token incollato",
      s4b: "Oppure impostalo una volta per ogni avvio, in PowerShell:", s4c: "Oppure accedi con lo strumento di Hugging Face (prima serve il suo pacchetto):", copy: "Copia", copied: "Copiato",
      close: "Chiudi", checkNow: "Controlla di nuovo", bad: "Hugging Face non accetta questo token.", saved: "Token accettato: account {0}.",
    },
  };
  const t = (key, ...a) => {
    const v = key.split(".").reduce((o, k) => o && o[k], S[LANG]) ?? key.split(".").reduce((o, k) => o && o[k], S.en) ?? key;
    return String(v).replace(/\{(\d+)\}/g, (_, i) => String(a[+i] ?? ""));
  };
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const GB = n => nfmt((n || 0) / 2 ** 30, 1);
  const say = x => (x && (x[LANG] || x.en)) || "";
  let C = null, job = null, poll = 0, plan = null, planSeq = 0, cur = null, hf = null, filter = "all", doneSeen = null;

  async function api(path, body) {
    const r = await fetch(path, body === undefined ? { cache: "no-store", headers: authHeaders() } :
      { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) askKey();
    return { r, j };
  }
  const errText = j => (j && j.error && (LANG === "it" && j.error.message_it ? j.error.message_it : j.error.message)) || "";
  const entry = id => C && C.entries.find(e => e.id === id);
  const localHas = id => ((window.BasaltModels && BasaltModels.data() || {}).models || []).some(m => m.catalog === id);

  /* ---- the list ---- */
  const kindOf = e => e.convert === "no" ? "incompatible" : e.convert === "ready" ? "ready" : "convert";
  function show(e) {
    return filter === "all" || (filter === "ready" && e.convert === "ready") || (filter === "convert" && (e.convert === "ok" || e.convert === "untested"))
      || (filter === "uncensored" && e.uncensored && e.convert !== "no") || (filter === "no" && e.convert === "no")
      || (["base", "coder", "swift", "unsloth"].includes(filter) && e.family === filter && e.convert !== "no");
  }
  function badges(e) {
    const b = [];
    if (e.convert === "no") b.push(`<span class="badge" data-s="incompatible">✕ ${esc(t("no"))}</span>`);
    else b.push(e.convert === "ready" ? `<span class="badge" data-s="ready">✓ ${esc(t("ready"))}</span>` : `<span class="badge" data-s="convert">⇄ ${esc(t("convert"))}</span>`);
    if (e.convert === "untested") b.push(`<span class="badge" data-s="att">${esc(t("untested"))}</span>`);
    if (e.uncensored && e.convert !== "no") b.push(`<span class="badge" data-s="unc">⚠ ${esc(t("unc"))}</span>`);
    if (e.gated && e.convert !== "no") b.push(`<span class="badge" data-s="att">🔑 ${esc(t("gate"))}</span>`);
    if (localHas(e.id)) b.push(`<span class="badge" data-s="ready">${esc(t("here"))}</span>`);
    return b.join(" ");
  }
  function chips(e) {
    const c = [];
    if (e.quant && e.quant !== "—") c.push(e.quant);
    if (e.approx_bytes) c.push(t("size", GB(e.approx_bytes)));
    if (e.fidelity && e.fidelity.short) c.push(say(e.fidelity.short));
    return c.map(x => `<li>${esc(x)}</li>`).join("");
  }
  function extra(e) {
    const g = C.generic, out = [];
    out.push(`<p class="mdl-note">${esc(say(g.plain))}</p>`);
    if (e.convert === "ready") out.push(`<p class="mdl-note">${esc(say(g.ready))}</p>`);
    else if (e.convert !== "no") out.push(`<p class="mdl-note">${esc(say(g.convert))}</p><p class="mdl-note">${esc(say(g.mmproj))}</p>`);
    (e.notes || []).forEach(n => out.push(`<p class="mdl-note ${n === C.uncensored_warning || say(n) === say(C.uncensored_warning) ? "dl-warn" : ""}">${esc(say(n))}</p>`));
    return out.join("");
  }
  function card(e) {
    const lic = C.licenses[e.license] || {};
    const body = e.convert === "no" ? `<p class="mdl-why">${esc(say(e.reason))}</p>` : `<p class="mdl-why">${esc(say(e.short))}</p>`;
    const detail = `<details class="dl-more"><summary>${esc(t("details"))}</summary>
        <p class="mdl-note">${esc(say(e.long))}</p>
        ${e.fidelity ? `<p class="mdl-note"><b>${esc(t("fidelity"))}.</b> ${esc(say(e.fidelity))}</p>` : ""}
        ${e.convert === "no" ? "" : extra(e)}
        ${e.convert === "no" ? "" : `<p class="mdl-note">${esc(t("lic", lic.name || ""))}${lic.url ? ` · <a href="${esc(lic.url)}" target="_blank" rel="noopener noreferrer">${esc(lic.url.replace(/^https:\/\//, ""))}</a>` : ""}</p>`}
        ${e.page ? `<p class="mdl-note"><a href="${esc(e.page)}" target="_blank" rel="noopener noreferrer">${esc(t("page"))}</a></p>` : ""}
      </details>`;
    return `<li class="mdl-card dl-card" data-s="${kindOf(e)}" data-id="${esc(e.id)}">
      <div class="mdl-h"><b>${esc(e.name)}</b>${badges(e)}</div>
      <ul class="chips">${chips(e)}</ul>
      ${body}${detail}
      ${e.downloadable ? `<div class="mdl-act"><button type="button" class="btn primary" data-d="get" data-id="${esc(e.id)}" aria-label="${esc(t("get") + " " + e.name)}">${esc(t("get"))}</button></div>` : ""}
    </li>`;
  }
  function render() {
    const box = $("#ld-download");
    if (!box || !C) return;
    const open = box.querySelector("details.dlpanel") ? box.querySelector("details.dlpanel").open : false;
    const openMore = [...box.querySelectorAll("details.dlpanel-more")].map(d => d.open);
    const list = C.entries.filter(show);
    const n = C.entries.filter(e => e.downloadable).length;
    box.innerHTML = `<details class="dlpanel"><summary><b>${esc(t("title"))}</b><span class="dl-count">${esc(t("count", n))}</span></summary>
      <div class="dl-body">
        <p class="setnote">${esc(t("intro"))}</p>
        <div class="dl-jobbox" aria-live="polite">${jobHtml()}</div>
        <div class="field dl-filter"><label for="dl-filter">${esc(t("show"))}</label>
          <select id="dl-filter">${Object.keys(S.en.f).map(k => `<option value="${k}"${k === filter ? " selected" : ""}>${esc(t("f." + k))}</option>`).join("")}</select></div>
        <ul class="mdl-list dl-list">${list.map(card).join("")}</ul>
        <p class="setnote dl-foot">${esc(t("foot", C.reviewed))}</p>
      </div></details>`;
    const d = box.querySelector("details.dlpanel"); if (open) d.open = true;
    [...box.querySelectorAll("details.dlpanel-more")].forEach((x, i) => { if (openMore[i]) x.open = true; });
  }

  /* ---- the job ---- */
  const fmtEta = s => s == null ? "" : s >= 3600 ? `${Math.floor(s / 3600)} h ${String(Math.round((s % 3600) / 60)).padStart(2, "0")} min` : s >= 90 ? `${Math.round(s / 60)} min` : `${s} s`;
  function jobHtml() {
    const j = job;
    if (!j || !j.id || j.phase === "idle") return "";
    const pct = j.bytes_total ? Math.min(100, Math.floor(j.bytes_done / j.bytes_total * 100)) : 0;
    const live = j.phase === "running" || j.phase === "verifying";
    const e = entry(j.id), name = j.name || (e && e.name) || j.id;
    const line = [`${GB(j.bytes_done)} / ${GB(j.bytes_total)} GB`, `${pct}%`];
    if (live && j.speed_bps > 1) line.push(t("speed", nfmt(j.speed_bps / 1e6, 1)));
    if (live && j.eta_s != null) line.push(t("left", fmtEta(j.eta_s)));
    if (j.file) line.push(t("file", Math.min(j.files_total, (j.files_done || 0) + (live ? 1 : 0)), j.files_total, j.file));
    const err = j.error ? (t("err." + j.error.code) !== "err." + j.error.code ? t("err." + j.error.code) : (LANG === "it" ? j.error.it : j.error.en)) : "";
    const res = j.phase === "done" ? t(j.convert === "ready" ? "doneReady" : "doneConv", j.folder) : j.phase === "cancelled" ? t("stopped") : "";
    return `<section class="mdl-job dl-job" data-phase="${esc(j.phase)}" aria-label="${esc(t("job", name))}">
      <div class="mdl-job-h"><b>${esc(t("job", name))}</b><span>${esc(t("phase." + j.phase))}</span></div>
      <div class="mdl-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="${esc(t("job", name))}"><i style="width:${pct}%"></i></div>
      <p class="mdl-job-s">${esc(line.join(" · "))}</p>
      ${err ? `<p class="ferr" role="alert">${esc(err)}</p>` : ""}${res ? `<p class="mdl-job-r">${esc(res)}</p>` : ""}
      <details><summary>${esc(t("log"))}</summary><pre class="mdl-log">${esc((j.log || []).join("\n"))}</pre></details>
      <div class="mdl-act">${live ? `<button type="button" class="btn ghost" data-d="stop">${esc(t("stop"))}</button>`
        : (j.phase === "failed" || j.phase === "cancelled") && e && e.downloadable ? `<button type="button" class="btn primary" data-d="get" data-id="${esc(j.id)}">${esc(t("resume"))}</button>` : ""}</div>
    </section>`;
  }
  function paintJob() {
    const b = $("#ld-download .dl-jobbox");
    if (!b) return;
    const keep = [...b.querySelectorAll("details")].map(d => d.open);
    b.innerHTML = jobHtml();
    [...b.querySelectorAll("details")].forEach((d, i) => { if (keep[i]) d.open = true; });
  }
  function watch() {
    clearTimeout(poll);
    const tick = async () => {
      const { r, j } = await api("api/models/download");
      if (!r.ok) return;
      job = j; paintJob();
      if (j.phase === "running" || j.phase === "verifying") { poll = setTimeout(tick, 1000); return; }
      if (j.phase === "done" && doneSeen !== j.finished) {
        doneSeen = j.finished;
        if (window.BasaltModels) await BasaltModels.refresh(true);
        render();
      }
    };
    poll = setTimeout(tick, 500);
  }
  async function stopJob() { const { j } = await api("api/models/download-cancel", {}); job = j; paintJob(); watch(); }

  /* ---- the download dialog ---- */
  function dialog() {
    let d = $("#dl-dlg");
    if (!d) {
      document.body.insertAdjacentHTML("beforeend", `<dialog id="dl-dlg" class="mdl-dlg" aria-labelledby="dl-dlg-h">
        <form method="dialog" novalidate>
          <h2 id="dl-dlg-h"></h2>
          <div class="dl-desc"></div>
          <div class="dl-plan" role="status" aria-live="polite"></div>
          <div class="field"><label for="dl-dest">${esc(t("dest"))}</label>
            <input id="dl-dest" type="text" list="dl-dests" autocomplete="off" spellcheck="false" autocapitalize="off" aria-describedby="dl-dest-h"><datalist id="dl-dests"></datalist>
            <p class="help" id="dl-dest-h">${esc(t("destHelp"))}</p><p class="help dl-warn" id="dl-notin" hidden>${esc(t("notIn"))}</p></div>
          <div class="dl-hfbox" hidden></div>
          <div class="dl-lic"></div>
          <ul class="mdl-problems" role="alert"></ul>
          <div class="setactions"><button value="cancel" class="btn ghost">${esc(t("cancel"))}</button>
            <button type="button" class="btn primary" data-d="go" disabled></button></div>
        </form></dialog>`);
      d = $("#dl-dlg");
      let tm = 0;
      d.querySelector("#dl-dest").addEventListener("input", () => { clearTimeout(tm); tm = setTimeout(replan, 450); });
      d.addEventListener("change", e => { if (e.target.id === "dl-ack") paintGo(); });
    }
    return d;
  }
  function describeHtml(e) {
    return `<p class="mdl-dlg-sum">${esc(say(e.long))}</p>
      ${e.fidelity ? `<p class="mdl-note"><b>${esc(t("fidelity"))}.</b> ${esc(say(e.fidelity))}</p>` : ""}${extra(e)}`;
  }
  function licHtml(e) {
    const lic = C.licenses[e.license] || {};
    return `<p class="mdl-note"><b>${esc(t("lic", lic.name || ""))}</b>${lic.url ? ` · <a href="${esc(lic.url)}" target="_blank" rel="noopener noreferrer">${esc(lic.url.replace(/^https:\/\//, ""))}</a>` : ""}</p>
      <p class="mdl-note">${esc(say(lic))}</p>
      <label class="dl-ack"><input type="checkbox" id="dl-ack"> <span>${esc(t("accept", e.uncensored ? t("acceptWarn") : ""))}</span></label>`;
  }
  async function openGet(id) {
    const e = entry(id), d = dialog();
    if (!e) return;
    cur = e; plan = null;
    d.querySelector("#dl-dlg-h").textContent = t("dlg", e.name);
    d.querySelector(".dl-desc").innerHTML = describeHtml(e);
    d.querySelector(".dl-lic").innerHTML = licHtml(e);
    d.querySelector(".dl-plan").textContent = t("checking");
    d.querySelector(".mdl-problems").innerHTML = "";
    d.querySelector("#dl-dest").value = C.default_dest || "";
    d.querySelector("#dl-dests").innerHTML = (C.dirs || []).map(x => `<option value="${esc(x)}"></option>`).join("");
    d.querySelector("[data-d=go]").disabled = true;
    d.querySelector("[data-d=go]").textContent = t("go", "…");
    d.showModal();
    await replan();
  }
  function paintGo() {
    const d = dialog(), go = d.querySelector("[data-d=go]"), ack = d.querySelector("#dl-ack");
    go.disabled = !(plan && plan.ok && ack && ack.checked);
  }
  async function replan() {
    const d = dialog(), n = ++planSeq;
    const dest = d.querySelector("#dl-dest").value.trim();
    const { r, j } = await api("api/models/download-plan", { id: cur.id, dest: dest || null });
    if (n !== planSeq) return;
    plan = r.ok ? j : null;
    const box = d.querySelector(".dl-plan");
    if (!plan) { box.textContent = errText(j) || `HTTP ${r.status}`; paintGo(); return; }
    if (!dest && plan.dest) d.querySelector("#dl-dest").value = plan.dest.replace(/\//g, navigator.platform.startsWith("Win") ? "\\" : "/");
    box.innerHTML = `<p class="mdl-note"><b>${esc(t("files"))}</b></p><ul class="dl-files">${plan.files.map(f =>
      `<li><code>${esc(f.name)}</code> <span>${esc(t("size", GB(f.size)))}${f.have ? " ✓" : f.part ? ` (${esc(GB(f.part))} ✓)` : ""}</span></li>`).join("")}</ul>
      <p class="mdl-dlg-sum">${esc(t("total", GB(plan.remaining_bytes), GB(plan.total_bytes)))}${plan.free_bytes != null ? " · " + esc(t("free", GB(plan.free_bytes))) : ""}</p>
      ${cur.kind === "gguf" ? `<p class="help">${esc(t("conv"))}</p>` : ""}`;
    d.querySelector("#dl-notin").hidden = plan.in_models_dirs || !plan.dest;
    d.querySelector("[data-d=go]").textContent = t("go", GB(plan.remaining_bytes));
    d.querySelector(".mdl-problems").innerHTML = (plan.problems || []).map(p => `<li>${esc(LANG === "it" ? p.text_it : p.text)}</li>`).join("");
    const hfb = d.querySelector(".dl-hfbox");
    const need = cur.gated || plan.access === "token" || plan.access === "terms";
    hfb.hidden = !need;
    if (need) hfb.innerHTML = `<p class="mdl-note"><b>${esc(t("access"))}.</b> ${plan.token_source ? esc(t("accessOk", t("src." + plan.token_source))) : esc(t("accessNo"))}
      <button type="button" class="btn ghost" data-d="hf">${esc(t("setup"))}</button></p>`;
    paintGo();
  }
  async function startGet() {
    const d = dialog();
    if (!plan || !cur) return;
    const { r, j } = await api("api/models/download", { id: cur.id, dest: d.querySelector("#dl-dest").value.trim() || null, token: plan.token, confirm: true, ack: true });
    if (r.status === 409 && j.plan) { await replan(); return; }
    if (!r.ok) { d.querySelector(".mdl-problems").innerHTML = `<li>${esc(errText(j) || `HTTP ${r.status}`)}</li>`; return; }
    d.close();
    job = j; render(); const b = $("#ld-download details.dlpanel"); if (b) b.open = true; watch();
  }

  /* ---- the Hugging Face assistant ---- */
  const CMD_ENV = 'setx HF_TOKEN "hf_..."';
  const CMD_CLI = ".venv\\Scripts\\pip install -U huggingface_hub\n.venv\\Scripts\\huggingface-cli login";
  function hfDialog() {
    let d = $("#dl-hf");
    if (!d) {
      document.body.insertAdjacentHTML("beforeend", `<dialog id="dl-hf" class="mdl-dlg" aria-labelledby="dl-hf-h">
        <form method="dialog" novalidate><h2 id="dl-hf-h">${esc(t("hfTitle"))}</h2>
          <p class="mdl-dlg-sum">${esc(t("hfIntro"))}</p>
          <ol class="dl-steps">
            <li><b>${esc(t("s1"))}</b><br><a class="btn ghost" href="https://huggingface.co/join" target="_blank" rel="noopener noreferrer">${esc(t("s1b"))}</a></li>
            <li><b>${esc(t("s2"))}</b><br><a class="btn ghost dl-modelpage" href="https://huggingface.co" target="_blank" rel="noopener noreferrer">${esc(t("s2b"))}</a></li>
            <li><b>${esc(t("s3"))}</b><br><a class="btn ghost" href="https://huggingface.co/settings/tokens" target="_blank" rel="noopener noreferrer">${esc(t("s3b"))}</a>
              <p class="help">${esc(t("s3h"))}</p></li>
            <li><b>${esc(t("s4"))}</b>
              <div class="field"><label for="dl-token">${esc(t("s4a"))}</label>
                <input id="dl-token" type="password" autocomplete="off" spellcheck="false" autocapitalize="off" inputmode="text" aria-label="${esc(t("tokenLbl"))}">
                <div class="mdl-act"><button type="button" class="btn primary" data-d="usetoken">${esc(t("use"))}</button>
                  <button type="button" class="btn ghost" data-d="forget">${esc(t("forget"))}</button></div></div>
              <p class="help">${esc(t("s4b"))}</p><pre class="dl-cmd">${esc(CMD_ENV)}</pre><button type="button" class="btn ghost" data-d="copy" data-text="${esc(CMD_ENV)}">${esc(t("copy"))}</button>
              <p class="help">${esc(t("s4c"))}</p><pre class="dl-cmd">${esc(CMD_CLI)}</pre><button type="button" class="btn ghost" data-d="copy" data-text="${esc(CMD_CLI)}">${esc(t("copy"))}</button></li>
          </ol>
          <p class="setstatus dl-hfmsg" role="status" aria-live="polite"></p>
          <div class="setactions"><button type="button" class="btn ghost" data-d="hfcheck">${esc(t("checkNow"))}</button><button value="cancel" class="btn primary">${esc(t("close"))}</button></div>
        </form></dialog>`);
      d = $("#dl-hf");
    }
    return d;
  }
  function hfMsg(text, kind) { const m = $("#dl-hf .dl-hfmsg"); if (m) { m.textContent = text; m.dataset.kind = kind || ""; } }
  async function openHf() {
    const d = hfDialog();
    d.querySelector(".dl-modelpage").href = cur && cur.page ? cur.page : "https://huggingface.co";
    d.querySelector("#dl-token").value = "";
    d.showModal();
    await checkHf();
  }
  async function checkHf() {
    hfMsg(t("checking"));
    const { r, j } = await api("api/models/hf");
    hf = r.ok ? j : null;
    if (hf && hf.source) hfMsg(hf.user ? t("accessOk", t("src." + hf.source) + ", " + t("accessUser", hf.user)) : (hf.error === "refused" ? t("err.token") : t("err.network")), hf.user ? "ok" : "err");
    else hfMsg(t("accessNo"));
    if (cur && $("#dl-dlg").open) await replan();
  }
  async function useToken() {
    const inp = $("#dl-token"), v = inp.value.trim();
    if (!v) return;
    const { r, j } = await api("api/models/hf-token", { token: v });
    inp.value = "";
    if (!r.ok) { hfMsg(errText(j) || t("bad"), "err"); return; }
    hfMsg(t("saved", j.user || "?"), "ok");
    if (cur && $("#dl-dlg").open) await replan();
  }
  async function forgetToken() { await api("api/models/hf-token", { token: "" }); await checkHf(); }
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); hfMsg(t("copied"), "ok"); } catch { hfMsg(text); }
  }

  /* ---- events ---- */
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-d]");
    if (!b || !b.closest("#ld-download, #dl-dlg, #dl-hf")) return;
    const k = b.dataset.d;
    if (k === "get") openGet(b.dataset.id);
    else if (k === "go") startGet();
    else if (k === "stop") stopJob();
    else if (k === "hf") openHf();
    else if (k === "usetoken") useToken();
    else if (k === "forget") forgetToken();
    else if (k === "hfcheck") checkHf();
    else if (k === "copy") copy(b.dataset.text);
  });
  document.addEventListener("change", e => {
    if (e.target.id === "dl-filter") { filter = e.target.value; render(); const d = $("#ld-download details.dlpanel"); if (d) d.open = true; const s = $("#dl-filter"); if (s) s.focus(); }
  });

  /* ---- what the model picker shows for a model of the catalog ---- */
  window.BasaltCatalog = {
    entry: id => entry(id),
    tip(m) {
      const e = m && m.catalog && entry(m.catalog);
      if (!e) return "";
      return [say(e.short), e.fidelity && e.fidelity.short ? say(e.fidelity.short) : ""].filter(Boolean).join(" · ");
    },
    loaded: () => !!C,
  };

  async function load() {
    const { r, j } = await api("api/models/catalog");
    if (!r.ok) return;
    C = j; render();
    if (window.BasaltModels && BasaltModels.rerender) BasaltModels.rerender();
    const s = await api("api/models/download");
    if (s.r.ok) { job = s.j; paintJob(); if (job.phase === "running" || job.phase === "verifying") watch(); }
  }
  load();
})();
