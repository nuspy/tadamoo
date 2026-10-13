"use strict";
/* Models: the models folders and every model under them with its status for Basalt (GET /api/models). The Loader
   picks the model to load (verify, convert); Settings holds the app's choices: the folders and the default server
   model. Until the folders are set the Loader asks for them first. A conversion opens a dialog that states the
   new file's size and the disk's free space; it starts only from that plan (its token). */
(() => {
  const S = {
    en: {
      title: "Model", pick: "Model to load", rescan: "Search again", none: "No model ready in the folders",
      ask: "Before going on, choose the folders where your models are (.basalt files and GGUF downloads). The search also looks in their subfolders.",
      dirs: "Models folders", dirsHelp: "Folders searched for .basalt and GGUF files, subfolders included. More than one is fine (for example your .basalt files and LM Studio's downloads).",
      dir: "Folder {0}", remove: "Remove", removeN: "Remove folder {0}", add: "Add a folder", save: "Save folders",
      browse: "Browse…", browseN: "Choose folder {0} in the Windows dialog",
      found: "Found on this PC:", use: "Use {0}", saved: "Folders saved: {0} models found.", saving: "Searching the folders…",
      noDir: "Add at least one folder.",
      ready: "Ready", convert: "To convert", incompatible: "Not compatible", attached: "attachment",
      all: "Models found: ready {0} · to convert {1} · not compatible {2}", inc: "Not compatible ({0})",
      experts: "experts {0} GB", mtp: "MTP drafter", mtpSource: "MTP from {0}", mtpFetch: "MTP to download",
      vision: "vision", outside: "outside the folders (from the config file)",
      choose: "Use", verify: "Verify", conv: "Convert…", isDefault: "server default",
      defaultSet: "“{0}” is now the default server model: Avvia-Basalt.ps1 without --model starts it.",
      defModel: "Default server model", defModelHelp: "Avvia-Basalt.ps1 and the app start this model when no --model is given. Each load can still pick another one in the Loader.",
      defNone: "None: the config file's model", defIs: "Default server model: {0}.", defNoneShort: "No default server model.",
      inSettings: "Change it in Settings", dirsIn: "Models folders ({0}): in Settings",
      dlgTitle: "Convert “{0}”?", out: "Output folder", file: "The new file: {0}", cmd: "Command", cancel: "Cancel",
      go: "Convert (writes ~{0} GB)", planning: "Checking disk and memory…",
      job: { convert: "Converting “{0}”", verify: "Verifying “{0}”" },
      phase: { running: "running", done: "done", failed: "failed", cancelled: "cancelled" },
      step: { fetch: "downloading Qwen's MTP drafter", check: "checking the drafter", convert: "writing the .basalt file", verify: "checking every section" },
      log: "Log", stop: "Cancel the job", verifyOk: "Every section matches its SHA-256.", verifyBad: "A section does not match: the file is damaged.",
      convDone: "Conversion done: {0}. It is in the list as Ready.",
    },
    it: {
      title: "Modello", pick: "Modello da caricare", rescan: "Cerca di nuovo", none: "Nessun modello pronto nelle cartelle",
      ask: "Prima di continuare scegli le cartelle in cui sono i modelli (file .basalt e GGUF scaricati). La ricerca guarda anche nelle sottocartelle.",
      dirs: "Cartelle dei modelli", dirsHelp: "Cartelle in cui cercare i file .basalt e GGUF, sottocartelle comprese. Possono essere più di una (per esempio i tuoi .basalt e i download di LM Studio).",
      dir: "Cartella {0}", remove: "Rimuovi", removeN: "Rimuovi la cartella {0}", add: "Aggiungi una cartella", save: "Salva le cartelle",
      browse: "Sfoglia…", browseN: "Scegli la cartella {0} con la finestra di Windows",
      found: "Trovate su questo PC:", use: "Usa {0}", saved: "Cartelle salvate: trovati {0} modelli.", saving: "Cerco nelle cartelle…",
      noDir: "Aggiungi almeno una cartella.",
      ready: "Pronto", convert: "Da convertire", incompatible: "Non compatibile", attached: "allegato",
      all: "Modelli trovati: pronti {0} · da convertire {1} · non compatibili {2}", inc: "Non compatibili ({0})",
      experts: "esperti {0} GB", mtp: "drafter MTP", mtpSource: "MTP da {0}", mtpFetch: "MTP da scaricare",
      vision: "visione", outside: "fuori dalle cartelle (dal file di configurazione)",
      choose: "Usa", verify: "Verifica", conv: "Converti…", isDefault: "predefinito per il server",
      defaultSet: "«{0}» ora è il modello predefinito per il server: Avvia-Basalt.ps1 senza --model avvia questo.",
      defModel: "Modello predefinito per il server", defModelHelp: "Avvia-Basalt.ps1 e l'app avviano questo modello quando non si indica --model. Ogni caricamento può comunque sceglierne un altro nel Loader.",
      defNone: "Nessuno: il modello del file di configurazione", defIs: "Modello predefinito per il server: {0}.", defNoneShort: "Nessun modello predefinito per il server.",
      inSettings: "Cambialo nelle Impostazioni", dirsIn: "Cartelle dei modelli ({0}): nelle Impostazioni",
      dlgTitle: "Convertire «{0}»?", out: "Cartella di uscita", file: "Il nuovo file: {0}", cmd: "Comando", cancel: "Annulla",
      go: "Converti (scrive ~{0} GB)", planning: "Controllo disco e memoria…",
      job: { convert: "Conversione di «{0}»", verify: "Verifica di «{0}»" },
      phase: { running: "in corso", done: "fatto", failed: "non riuscito", cancelled: "annullato" },
      step: { fetch: "scarico il drafter MTP di Qwen", check: "controllo il drafter", convert: "scrivo il file .basalt", verify: "controllo ogni sezione" },
      log: "Registro", stop: "Annulla il lavoro", verifyOk: "Ogni sezione corrisponde al suo SHA-256.", verifyBad: "Una sezione non corrisponde: il file è danneggiato.",
      convDone: "Conversione finita: {0}. È nell'elenco come Pronto.",
    },
  };
  const t = (key, ...a) => {
    const v = key.split(".").reduce((o, k) => o && o[k], S[LANG]) ?? key.split(".").reduce((o, k) => o && o[k], S.en) ?? key;
    return String(v).replace(/\{(\d+)\}/g, (_, i) => String(a[+i] ?? ""));
  };
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  // sizes in GiB written "GB", as Windows Explorer shows them
  const GB = n => nfmt((n || 0) / 2 ** 30, 1);
  const same = (a, b) => !!a && !!b && a.replace(/\\/g, "/").toLowerCase() === b.replace(/\\/g, "/").toLowerCase();
  const say = it => (it && (it[LANG] || it.en)) || "";
  let M = null, selected = null, listeners = [], poll = 0, plan = null, planSeq = 0, lastJob = null;

  async function api(path, body) {
    const r = await fetch(path, body === undefined ? { cache: "no-store", headers: authHeaders() } :
      { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) askKey();
    return { r, j };
  }

  /* ---- the folders editor ---- */
  // inside the desktop app (WebView2) a folder is picked with the Windows dialog; in a browser it is typed
  const app = window.chrome && window.chrome.webview;
  if (app) app.addEventListener("message", e => {
    const m = e.data || {};
    if (m.type !== "folder" || !m.path) return;
    const input = document.getElementById(m.id);
    if (input) { input.value = m.path; input.dispatchEvent(new Event("input", { bubbles: true })); input.focus(); }
  });
  function dirsEditor(where, dirs) {
    const rows = (dirs.length ? dirs : [""]).map((d, i) => `
      <div class="field mdl-dirrow" data-i="${i}">
        <label for="${where}-dir-${i}">${esc(t("dir", i + 1))}</label>
        <div class="pfrow"><input id="${where}-dir-${i}" type="text" value="${esc(d)}" autocomplete="off" spellcheck="false" autocapitalize="off" inputmode="url">
          ${app ? `<button type="button" class="btn" data-m="browse" data-for="${where}-dir-${i}" aria-label="${esc(t("browseN", i + 1))}">${esc(t("browse"))}</button>` : ""}
          <button type="button" class="btn ghost" data-m="del" aria-label="${esc(t("removeN", i + 1))}">${esc(t("remove"))}</button></div>
        <p class="ferr" role="alert" hidden></p>
      </div>`).join("");
    const sugg = (M && M.suggestions || []).filter(s => !dirs.some(d => same(d, s)));
    return `<form class="mdl-dirs" data-where="${where}" novalidate>
      <p class="setnote">${esc(t("dirsHelp"))}</p>
      <div class="mdl-dirrows">${rows}</div>
      ${sugg.length ? `<p class="mdl-sugg-h">${esc(t("found"))}</p><div class="pfrow wrap mdl-sugg">${sugg.map(s =>
        `<button type="button" class="btn ghost" data-m="sugg" data-path="${esc(s)}">${esc(t("use", s))}</button>`).join("")}</div>` : ""}
      <div class="setactions"><p class="setstatus" role="status" aria-live="polite"></p>
        <button type="button" class="btn ghost" data-m="add">${esc(t("add"))}</button>
        <button type="submit" class="btn primary">${esc(t("save"))}</button></div>
    </form>`;
  }
  function readDirs(form) { return [...form.querySelectorAll(".mdl-dirrow input")].map(i => i.value.trim()).filter(Boolean); }
  async function saveDirs(form) {
    const dirs = readDirs(form), st = form.querySelector(".setstatus");
    form.querySelectorAll(".ferr").forEach(p => { p.hidden = true; p.textContent = ""; });
    form.querySelectorAll("[aria-invalid]").forEach(x => x.removeAttribute("aria-invalid"));
    if (!dirs.length) { st.textContent = t("noDir"); st.dataset.kind = "err"; return; }
    st.textContent = t("saving"); st.dataset.kind = "";
    const { r, j } = await api("api/models/dirs", { dirs });
    if (!r.ok) {
      const rows = [...form.querySelectorAll(".mdl-dirrow")].filter(x => x.querySelector("input").value.trim());
      Object.entries(j.fields || {}).forEach(([i, why]) => {
        const row = rows[+i]; if (!row) return;
        const p = row.querySelector(".ferr"); p.textContent = say(why); p.hidden = false;
        row.querySelector("input").setAttribute("aria-invalid", "true");
      });
      st.textContent = (j.error && j.error.message) || `HTTP ${r.status}`; st.dataset.kind = "err";
      const bad = form.querySelector("[aria-invalid]"); if (bad) bad.focus();
      return;
    }
    M = j; pickDefault(); render();
    const n = (M.models || []).filter(m => m.role === "model").length;
    $$(".mdl-dirs .setstatus").forEach(e => { e.textContent = t("saved", n); e.dataset.kind = "ok"; });
  }

  /* ---- the list ---- */
  const BADGE = { ready: "✓", convert: "⇄", incompatible: "✕" };
  function chips(m) {
    const c = [];
    if (m.quant) c.push(m.quant);
    if (m.size_bytes) c.push(`${GB(m.size_bytes)} GB`);
    if (m.experts_bytes) c.push(t("experts", GB(m.experts_bytes)));
    if (m.expert_types) c.push(m.expert_types);
    if (m.mtp === "present") c.push(t("mtp"));
    if (m.mtp === "source") c.push(t("mtpSource", (m.mtp_source || "").split("/").pop()));
    if (m.mtp === "fetch") c.push(t("mtpFetch"));
    if (m.vision) c.push(t("vision"));
    return c.map(x => `<li>${esc(x)}</li>`).join("");
  }
  function card(m) {
    const isDef = same(m.path, M.default_model);
    const acts = m.status === "ready"
      ? `<button type="button" class="btn" data-m="choose" data-path="${esc(m.path)}" ${same(m.path, selected) ? "aria-pressed=\"true\"" : ""}>${esc(t("choose"))}</button>
         <button type="button" class="btn ghost" data-m="verify" data-path="${esc(m.path)}">${esc(t("verify"))}</button>`
      : m.status === "convert" ? `<button type="button" class="btn primary" data-m="conv" data-id="${esc(m.id)}">${esc(t("conv"))}</button>` : "";
    return `<li class="mdl-card" data-s="${m.status}">
      <div class="mdl-h"><b>${esc(m.name)}</b><span class="badge" data-s="${m.status}">${BADGE[m.status] || ""} ${esc(t(m.status))}</span>
        ${m.role !== "model" ? `<span class="badge" data-s="att">${esc(t("attached"))}</span>` : ""}${isDef ? `<span class="badge" data-s="def">★ ${esc(t("isDefault"))}</span>` : ""}</div>
      <p class="mdl-path"><code>${esc(m.path)}</code>${m.files && m.files.length > 1 ? ` <span>+${m.files.length - 1}</span>` : ""}${m.outside ? ` · ${esc(t("outside"))}` : ""}</p>
      <ul class="chips">${chips(m)}</ul>
      <p class="mdl-why">${esc(say(m.reason))}</p>
      ${(m.notes || []).map(n => `<p class="mdl-note">${esc(say(n))}</p>`).join("")}
      ${acts ? `<div class="mdl-act">${acts}</div>` : ""}
    </li>`;
  }
  const readyOnes = () => (M && M.models || []).filter(m => m.status === "ready");
  const nameOf = path => { const m = (M && M.models || []).find(x => same(x.path, path)); return m ? m.name : (path || "").split(/[\\/]/).pop(); };
  function pickDefault() {
    if (selected && readyOnes().some(m => same(m.path, selected))) return;
    const cur = M && M.current;
    const r = readyOnes();
    selected = (r.find(m => same(m.path, cur)) || r.find(m => same(m.path, M && M.default_model)) || r[0] || {}).path || null;
  }
  function jobHtml() {
    const j = M && M.job;
    if (!j || !j.kind || j.phase === "idle") return "";
    const pct = Math.round((j.progress || 0) * 100), running = j.phase === "running";
    const bytes = j.bytes_total ? ` · ${GB(j.bytes_done)} / ${GB(j.bytes_total)} GB` : "";
    const res = j.kind === "verify" && j.phase !== "running" ? (j.result === "ok" ? t("verifyOk") : j.result === "bad" ? t("verifyBad") : "")
      : j.kind === "convert" && j.phase === "done" ? t("convDone", j.out_path || "") : "";
    return `<section class="mdl-job" data-phase="${esc(j.phase)}" aria-label="${esc(t("job." + j.kind, j.name || ""))}">
      <div class="mdl-job-h"><b>${esc(t("job." + j.kind, j.name || ""))}</b><span>${esc(t("phase." + j.phase))}</span></div>
      <div class="mdl-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}" aria-label="${esc(t("job." + j.kind, j.name || ""))}"><i style="width:${pct}%"></i></div>
      <p class="mdl-job-s">${running && j.step ? esc(t("step." + j.step)) + " · " : ""}${pct}%${bytes}</p>
      ${j.error ? `<p class="ferr">${esc(j.error)}</p>` : ""}${res ? `<p class="mdl-job-r">${esc(res)}</p>` : ""}
      <details><summary>${esc(t("log"))}</summary><pre class="mdl-log">${esc((j.log || []).join("\n"))}</pre></details>
      ${running ? `<div class="mdl-act"><button type="button" class="btn ghost" data-m="cancel">${esc(t("stop"))}</button></div>` : ""}
    </section>`;
  }
  function loaderHtml() {
    if (!M) return "";
    if (!M.configured) return `<div class="mdl-ask"><p class="setnote warnnote">${esc(t("ask"))}</p>${dirsEditor("ld", [])}</div>`;
    const r = readyOnes(), models = M.models.filter(m => m.status !== "incompatible");
    const inc = M.models.filter(m => m.status === "incompatible"), c = M.counts || {};
    const sel = r.find(m => same(m.path, selected));
    return `<div class="mdl-pick">
        <label for="ld-mdl-sel">${esc(t("pick"))}</label>
        <div class="pfrow"><select id="ld-mdl-sel">${r.length ? r.map(m => `<option value="${esc(m.path)}">${esc(`✓ ${m.name}${m.quant && !m.name.includes(m.quant) ? " · " + m.quant : ""} · ${GB(m.size_bytes)} GB`)}</option>`).join("")
          : `<option value="">${esc(t("none"))}</option>`}</select>
          <button type="button" class="btn ghost" data-m="rescan">${esc(t("rescan"))}</button></div>
      </div>
      ${sel ? `<ul class="chips mdl-sel-chips" aria-live="polite">${chips(sel)}</ul>
        <div class="pfrow wrap">${same(sel.path, M.default_model) ? `<span class="badge" data-s="def">★ ${esc(t("isDefault"))}</span>` : ""}
          <button type="button" class="btn ghost" data-m="verify" data-path="${esc(sel.path)}">${esc(t("verify"))}</button></div>` : ""}
      <div class="pfrow wrap mdl-def"><span>${esc(M.default_model ? t("defIs", nameOf(M.default_model)) : t("defNoneShort"))}</span>
        <a class="btn ghost mdl-goset" href="#settings">${esc(t("inSettings"))}</a></div>
      <p class="setstatus mdl-msg" role="status" aria-live="polite"></p>
      ${jobHtml()}
      <details class="mdl-all"><summary>${esc(t("all", c.ready || 0, c.convert || 0, c.incompatible || 0))}</summary>
        <ul class="mdl-list">${models.map(card).join("")}</ul>
        ${inc.length ? `<details class="mdl-inc"><summary>${esc(t("inc", inc.length))}</summary><ul class="mdl-list">${inc.map(card).join("")}</ul></details>` : ""}
        <p class="mdl-dirbox"><a class="btn ghost mdl-goset" href="#settings">${esc(t("dirsIn", M.dirs.length))}</a></p>
      </details>`;
  }
  function render() {
    const ld = $("#ld-models");
    if (ld) {
      const open = [...ld.querySelectorAll("details")].map(d => d.open);
      ld.innerHTML = `<div class="sh"><b>${esc(t("title"))}</b></div><div class="mdl">${loaderHtml()}</div>`;
      [...ld.querySelectorAll("details")].forEach((d, i) => { if (open[i]) d.open = true; });
      const s = ld.querySelector("#ld-mdl-sel"); if (s && selected) s.value = selected;
      const blocked = M && !M.configured;
      $("#view-loader").toggleAttribute("data-blocked", !!blocked);
    }
    const st = $("#st-models");
    if (st && M) {
      const keep = document.activeElement && document.activeElement.id === "st-defmodel";
      const known = !M.default_model || readyOnes().some(m => same(m.path, M.default_model));
      st.innerHTML = `<div class="sh"><b>${esc(t("dirs"))}</b></div>${M.configured ? "" : `<p class="setnote warnnote">${esc(t("ask"))}</p>`}${dirsEditor("st", M.dirs)}
        <div class="field mdl-defbox"><label for="st-defmodel">${esc(t("defModel"))}</label>
          <select id="st-defmodel" aria-describedby="st-defmodel-h">${M.default_model ? "" : `<option value="">${esc(t("defNone"))}</option>`}${known ? ""
            : `<option value="${esc(M.default_model)}">${esc(nameOf(M.default_model))}</option>`}${readyOnes().map(m =>
            `<option value="${esc(m.path)}">${esc(`${m.name}${m.quant && !m.name.includes(m.quant) ? " · " + m.quant : ""}`)}</option>`).join("")}</select>
          <p class="help" id="st-defmodel-h">${esc(t("defModelHelp"))}</p>
          <p class="setstatus mdl-msg" role="status" aria-live="polite"></p></div>`;
      const s = st.querySelector("#st-defmodel");
      s.value = M.default_model ? ((readyOnes().find(m => same(m.path, M.default_model)) || {}).path || M.default_model) : "";
      if (keep) s.focus();
    }
  }

  /* ---- actions ---- */
  function msg(text, kind) { $$(".mdl-msg").forEach(e => { e.textContent = text; e.dataset.kind = kind || ""; }); }
  function choose(path) {
    selected = path; render();
    listeners.forEach(f => f(selected));
  }
  async function verify(path) {
    const { r, j } = await api("api/models/verify", { path });
    if (!r.ok) { msg((j.error && j.error.message) || `HTTP ${r.status}`, "err"); return; }
    M.job = j; render(); watch();
  }
  async function makeDefault(path) {
    const { r, j } = await api("api/models/default", { path });
    if (!r.ok) { msg((j.error && j.error.message) || `HTTP ${r.status}`, "err"); return; }
    M.default_model = j.default_model; render();
    msg(t("defaultSet", nameOf(path)), "ok");
  }
  async function cancelJob() { const { j } = await api("api/models/cancel", {}); M.job = j; render(); }
  function watch() {
    clearTimeout(poll);
    const tick = async () => {
      const { r, j } = await api("api/models/job");
      if (!r.ok) return;
      M.job = j;
      const running = j.phase === "running";
      updateJob();
      if (running) { poll = setTimeout(tick, 1000); return; }
      if (lastJob !== j.finished) { lastJob = j.finished; await refresh(j.kind === "convert"); }
    };
    poll = setTimeout(tick, 600);
  }
  function updateJob() {
    $$(".mdl-job").forEach(e => e.remove());
    const at = $("#ld-models .mdl-msg");
    if (at) at.insertAdjacentHTML("afterend", jobHtml());
  }

  /* ---- the conversion dialog ---- */
  function dialog() {
    let d = $("#mdl-dlg");
    if (!d) {
      document.body.insertAdjacentHTML("beforeend", `<dialog id="mdl-dlg" class="mdl-dlg" aria-labelledby="mdl-dlg-h">
        <form method="dialog" novalidate>
          <h2 id="mdl-dlg-h"></h2><p class="mdl-dlg-sum" role="status" aria-live="polite"></p>
          <div class="field"><label for="mdl-out">${esc(t("out"))}</label>
            <input id="mdl-out" type="text" autocomplete="off" spellcheck="false" autocapitalize="off" aria-describedby="mdl-out-h">
            <p class="help" id="mdl-out-h"></p></div>
          <ul class="mdl-problems" role="alert"></ul>
          <details><summary>${esc(t("cmd"))}</summary><code class="mdl-cmd"></code></details>
          <div class="setactions"><button value="cancel" class="btn ghost">${esc(t("cancel"))}</button>
            <button type="button" class="btn primary" data-m="go" disabled></button></div>
        </form></dialog>`);
      d = $("#mdl-dlg");
      let tm = 0;
      d.querySelector("#mdl-out").addEventListener("input", () => { clearTimeout(tm); tm = setTimeout(() => replan(), 400); });
      d.querySelector("[data-m=go]").addEventListener("click", startConvert);
    }
    return d;
  }
  async function openConvert(id) {
    const m = M.models.find(x => x.id === id), d = dialog();
    d.dataset.id = id;
    d.querySelector("#mdl-dlg-h").textContent = t("dlgTitle", m ? m.name : id);
    d.querySelector("#mdl-out").value = M.output_dir || "";
    d.querySelector(".mdl-dlg-sum").textContent = t("planning");
    d.querySelector(".mdl-problems").innerHTML = "";
    d.querySelector("[data-m=go]").disabled = true;
    d.querySelector("[data-m=go]").textContent = t("go", "…");
    d.showModal();
    await replan(true);
  }
  async function replan(first) {
    const d = dialog(), n = ++planSeq;
    const out = d.querySelector("#mdl-out").value.trim();
    const { r, j } = await api("api/models/plan", { id: d.dataset.id, out_dir: out || null });
    if (n !== planSeq) return;
    plan = r.ok ? j : null;
    const go = d.querySelector("[data-m=go]");
    if (!plan) { d.querySelector(".mdl-dlg-sum").textContent = (j.error && j.error.message) || `HTTP ${r.status}`; go.disabled = true; return; }
    if (first && !out && plan.out_dir) d.querySelector("#mdl-out").value = plan.out_dir.replace(/\//g, navigator.platform.startsWith("Win") ? "\\" : "/");
    d.querySelector(".mdl-dlg-sum").textContent = say(plan.summary);
    d.querySelector("#mdl-out-h").textContent = plan.out_path ? t("file", plan.out_path) : "";
    d.querySelector(".mdl-problems").innerHTML = (plan.problems || []).map(p => `<li>${esc(LANG === "it" ? p.text_it : p.text)}</li>`).join("");
    d.querySelector(".mdl-cmd").textContent = "python tools/convert/convert.py " + plan.args.map(a => /\s/.test(a) ? `"${a}"` : a).join(" ");
    go.textContent = t("go", GB(plan.estimate_bytes));
    go.disabled = !plan.ok;
  }
  async function startConvert() {
    const d = dialog();
    if (!plan) return;
    const { r, j } = await api("api/models/convert", { id: d.dataset.id, out_dir: d.querySelector("#mdl-out").value.trim() || null, token: plan.token, confirm: true });
    if (r.status === 409 && j.plan) { plan = j.plan; await replan(); return; }
    if (!r.ok) { d.querySelector(".mdl-problems").innerHTML = `<li>${esc((j.error && j.error.message) || `HTTP ${r.status}`)}</li>`; return; }
    d.close();
    M.job = j; render(); watch();
  }

  /* ---- events ---- */
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-m]");
    if (!b || !b.closest("#ld-models, #st-models")) return;
    const form = b.closest("form.mdl-dirs");
    const m = b.dataset.m;
    if (m === "add" && form) {
      const dirs = [...form.querySelectorAll(".mdl-dirrow input")].map(i => i.value);
      form.outerHTML = dirsEditor(form.dataset.where, [...dirs, ""].filter((x, i, a) => x || i === a.length - 1));
      const ins = $$(`form[data-where="${form.dataset.where}"] .mdl-dirrow input`); if (ins.length) ins[ins.length - 1].focus();
    } else if (m === "del" && form) {
      const row = b.closest(".mdl-dirrow"), dirs = [...form.querySelectorAll(".mdl-dirrow")].filter(x => x !== row).map(x => x.querySelector("input").value);
      form.outerHTML = dirsEditor(form.dataset.where, dirs);
    } else if (m === "sugg" && form) {
      const dirs = readDirs(form);
      form.outerHTML = dirsEditor(form.dataset.where, [...dirs, b.dataset.path]);
    } else if (m === "browse" && app) {
      const input = document.getElementById(b.dataset.for);
      app.postMessage({ type: "pickFolder", id: b.dataset.for, path: input ? input.value.trim() : "" });
    } else if (m === "rescan") refresh(true);
    else if (m === "choose") choose(b.dataset.path);
    else if (m === "verify") verify(b.dataset.path);
    else if (m === "conv") openConvert(b.dataset.id);
    else if (m === "cancel") cancelJob();
  });
  document.addEventListener("submit", e => {
    const f = e.target.closest("form.mdl-dirs");
    if (f) { e.preventDefault(); saveDirs(f); }
  });
  document.addEventListener("change", e => {
    if (e.target.id === "ld-mdl-sel") choose(e.target.value);
    if (e.target.id === "st-defmodel" && e.target.value) makeDefault(e.target.value);
  });

  async function refresh(rescan) {
    const { r, j } = await api("api/models" + (rescan ? "?rescan=1" : ""));
    if (!r.ok) return;
    const before = selected;
    M = j; pickDefault(); render();
    if (selected !== before) listeners.forEach(f => f(selected));
    if (M.job && M.job.phase === "running") watch();
    return M;
  }

  window.BasaltModels = {
    refresh, selected: () => selected, data: () => M,
    select(path) { if (path && readyOnes().some(m => same(m.path, path))) choose(path); },
    onChange(fn) { listeners.push(fn); },
    name(path) { const m = (M && M.models || []).find(x => same(x.path, path)); return m ? m.name : (path || "").split(/[\\/]/).pop(); },
  };
})();
