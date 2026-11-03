"use strict";
/* Export of the conversation: a window to choose what goes in (prompts, reasoning, actions, answers), the format (docx, md, pdf, html, odt)
   and the file name; the server (api/export) makes the file and the system's "Save as" window lets the person choose the folder.
   The page sends only what was ticked. Everything shown comes in as text; nothing is built as markup. */
(() => {
  const KEY = "tadamoo.export";
  const FORMATS = ["docx", "md", "pdf", "html", "odt"];
  const KINDS = ["prompts", "reasoning", "actions", "answers"];
  const DEFAULTS = { prompts: true, reasoning: false, actions: false, answers: true, format: "docx" };
  const ITEM_KIND = { prompt: "prompts", reasoning: "reasoning", action: "actions", answer: "answers" };
  const KIND_TEXT = {
    prompts: ["exp.k.prompts", "exp.k.prompts.d"], reasoning: ["exp.k.reasoning", "exp.k.reasoning.d"],
    actions: ["exp.k.actions", "exp.k.actions.d"], answers: ["exp.k.answers", "exp.k.answers.d"],
  };
  const FORMAT_TEXT = {
    docx: ["exp.f.docx", "exp.f.docx.d"], md: ["exp.f.md", "exp.f.md.d"], pdf: ["exp.f.pdf", "exp.f.pdf.d"],
    html: ["exp.f.html", "exp.f.html.d"], odt: ["exp.f.odt", "exp.f.odt.d"],
  };
  const IMAGE_LIMIT = 6 << 20, IMAGE_SIDE = 1600, DIAGRAM_SIDE = 2400;
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const load = () => { try { return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) || "{}") }; } catch (e) { return { ...DEFAULTS }; } };
  const keep = v => { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) { /* this page only */ } };
  const chatStatus = text => { const el = document.getElementById("chat-status"); if (el) el.textContent = text; };

  /* ---- the conversation, whichever chat is on screen ---- */
  window.TadamooChat = {
    snapshot() {
      const a = window.agentChat;
      const snap = a && a.active() && a.snapshot ? a.snapshot() : (window.chatUi && window.chatUi.snapshot ? window.chatUi.snapshot() : { title: "", session: "", items: [] });
      return { title: snap.title || "", session: snap.session || "", items: Array.isArray(snap.items) ? snap.items : [] };
    },
    /* what would be sent for a selection: only the ticked kinds, with their pictures and diagrams */
    prepare: (sel, title) => collect(sel, title),
  };

  /* ---- pictures and diagrams: read from the page, as data the server can embed ---- */
  const readUrl = blob => new Promise(done => { const r = new FileReader(); r.onload = () => done(String(r.result)); r.onerror = () => done(""); r.readAsDataURL(blob); });
  const loadImage = url => new Promise(done => { const i = new Image(); i.onload = () => done(i); i.onerror = () => done(null); i.src = url; });
  function canvasPng(img, w, h, side, backdrop) {
    const k = Math.min(1, side / Math.max(w, h));
    const c = document.createElement("canvas");
    c.width = Math.max(1, Math.round(w * k)); c.height = Math.max(1, Math.round(h * k));
    const g = c.getContext("2d");
    if (backdrop) { g.fillStyle = backdrop; g.fillRect(0, 0, c.width, c.height); }
    g.drawImage(img, 0, 0, c.width, c.height);
    try { return c.toDataURL("image/png"); } catch (e) { return ""; }
  }
  async function pictureData(blob) {                      // PNG, JPEG and GIF go as they are; anything else (or too large) is redrawn as a PNG
    if (/^image\/(png|jpeg|gif)$/.test(blob.type) && blob.size <= IMAGE_LIMIT) return readUrl(blob);
    const url = URL.createObjectURL(blob);
    try {
      const img = await loadImage(url);
      return img && img.naturalWidth ? canvasPng(img, img.naturalWidth, img.naturalHeight, IMAGE_SIDE, "#ffffff") : "";
    } finally { URL.revokeObjectURL(url); }
  }
  async function fetchPicture(sid, id) {
    try {
      const r = await fetch(`api/outputs/${encodeURIComponent(sid)}/${id}/file`, { headers: authHeaders() });
      return r.ok ? await pictureData(await r.blob()) : "";
    } catch (e) { return ""; }
  }
  async function diagramPng(svg) {                        // the diagram as a picture for the formats that cannot hold an SVG
    let root;
    try {
      const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
      root = doc.documentElement;
      if (!root || root.localName !== "svg" || doc.querySelector("parsererror")) return "";
    } catch (e) { return ""; }
    const vb = (root.getAttribute("viewBox") || "").trim().split(/[\s,]+/).map(Number);
    const attr = n => (/%$/.test(root.getAttribute(n) || "") ? NaN : parseFloat(root.getAttribute(n)));
    const w = attr("width") > 0 ? attr("width") : vb[2], h = attr("height") > 0 ? attr("height") : vb[3];
    if (!(w > 0 && h > 0)) return "";
    root.setAttribute("width", String(w)); root.setAttribute("height", String(h));
    const img = await loadImage("data:image/svg+xml;charset=utf-8," + encodeURIComponent(new XMLSerializer().serializeToString(root)));
    return img ? canvasPng(img, w * 2, h * 2, DIAGRAM_SIDE, "#151a1f") : "";
  }

  /* what the server needs for the ticked kinds, and nothing else */
  async function collect(sel, title, given) {
    const snap = given || window.TadamooChat.snapshot();
    const items = [];
    for (const it of snap.items) {
      if (it.kind === "action" && !sel.actions) {           // only the sources and documents it found travel: they are listed with the answers
        const head = /^\[file:[a-f0-9]{12}:[^\]]{1,120}\]/.exec(it.result || "");
        if (sel.answers && ((it.sources && it.sources.length) || head)) items.push({ kind: "action", turn: it.turn, sources: it.sources || [], result: head ? head[0] : "" });
        continue;
      }
      if (sel[ITEM_KIND[it.kind]]) items.push(it);
    }
    const assets = {}, diagrams = {};
    const answers = items.filter(it => it.kind === "answer").map(it => it.text || "");
    const wanted = new Set();
    for (const text of answers) for (const m of text.matchAll(/!\[[^\]\n]*\]\((tadamoo-file:[a-f0-9]{12})\)/g)) wanted.add(m[1]);
    for (const key of [...wanted].slice(0, 100)) {
      const data = snap.session ? await fetchPicture(snap.session, key.slice("tadamoo-file:".length)) : "";
      if (data) assets[key] = data;
    }
    for (const text of answers) {
      for (const m of text.matchAll(/(?:^|\n) {0,3}```mermaid[^\n]*\n([\s\S]*?)\n {0,3}```[ \t]*(?:\n|$)/g)) {
        const code = m[1], svg = window.chatUi && window.chatUi.diagram ? window.chatUi.diagram(code) : "";
        if (svg && !diagrams[code]) diagrams[code] = { svg, png: await diagramPng(svg) };
      }
    }
    return {
      title, session: snap.session, items, assets, diagrams, lang: LANG,
      base: new URL(".", location.href).href, tz: new Date().getTimezoneOffset(), exported_at: Date.now() / 1000,
    };
  }

  /* ---- saving: the desktop app and the browsers each have their own "Save as" window ---- */
  const inApp = () => !!(window.chrome && window.chrome.webview);
  async function saveBlob(blob, name) {                    // "saved" | "cancelled" | "failed"
    const link = () => { const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; document.body.append(a); a.click(); a.remove(); return a.href; };
    try {
      const app = window.chrome && window.chrome.webview;
      if (app) {                                           // the app opens "Save as" itself and says how it ended
        return await new Promise(done => {
          let href = "";
          const timer = setTimeout(() => { app.removeEventListener("message", on); done("failed"); }, 600000);
          const on = e => {
            const m = e.data || {};
            if (m.type !== "download" || m.name !== name) return;
            clearTimeout(timer); app.removeEventListener("message", on);
            setTimeout(() => URL.revokeObjectURL(href), 30000);
            done(m.state === "completed" ? "saved" : m.state === "cancelled" ? "cancelled" : "failed");
          };
          app.addEventListener("message", on);
          href = link();
        });
      }
      if (window.showSaveFilePicker) {
        const handle = await window.showSaveFilePicker({ suggestedName: name });
        const w = await handle.createWritable(); await w.write(blob); await w.close();
        return "saved";
      }
      const href = link();
      setTimeout(() => URL.revokeObjectURL(href), 30000);
      return "saved";
    } catch (e) { return e && e.name === "AbortError" ? "cancelled" : "failed"; }
  }
  function printInApp(token, name) {                       // the app prints the held page to a PDF where its "Save as" window says
    const app = window.chrome.webview, id = "exp" + Date.now().toString(36);
    const url = new URL("api/export/held/" + token, location.href).href;
    return new Promise((resolve, reject) => {
      let started = null, ended = null;
      const finish = (ok, value) => { clearTimeout(started); clearTimeout(ended); app.removeEventListener("message", on); if (ok) resolve(value); else reject(new Error(value)); };
      const on = e => {
        const m = e.data || {};
        if (m.type !== "exportPdf" || m.id !== id) return;
        if (m.state === "started") { clearTimeout(started); return; }
        if (m.state === "completed" || m.state === "cancelled") finish(true, m.state === "completed" ? "saved" : "cancelled");
        else finish(false, T("exp.pdf.app", m.error || ""));
      };
      started = setTimeout(() => finish(false, T("exp.pdf.old")), 4000);
      ended = setTimeout(() => finish(false, T("exp.pdf.app", "timeout")), 600000);
      app.addEventListener("message", on);
      app.postMessage({ type: "exportPdf", id, name, url });
    });
  }
  function printInBrowser(blob, win) {                     // the page opens in a window of its own and the print window follows
    const url = URL.createObjectURL(new Blob([blob], { type: "text/html" }));
    win.location.href = url;
    let tries = 0;
    const timer = setInterval(() => {
      let ready = false;
      try { ready = !win.closed && win.location.href.startsWith("blob:") && win.document.readyState === "complete"; if (win.closed || ++tries > 100) clearInterval(timer); } catch (e) { clearInterval(timer); }
      if (ready) { clearInterval(timer); setTimeout(() => { try { win.focus(); win.print(); } catch (e) { /* the person prints it by hand */ } }, 250); }
    }, 150);
    setTimeout(() => URL.revokeObjectURL(url), 600000);
  }
  const problem = async r => {
    try { const j = await r.json(); if (j && j.error && j.error.message) return j.error.message; } catch (e) { /* no body */ }
    return `HTTP ${r.status}`;
  };

  /* the server makes the file for `payload` ({snapshot, select, bare?}) and it is saved where the person says; a PDF is printed instead.
     Returns {state: "saved" | "cancelled" | "failed", fileName, stay} (stay: the window stays open with a message), and `used` when `win` was taken. */
  async function produce(payload, format, file, win, status) {
    const app = inApp();
    const r = await fetch("api/export", { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ ...payload, format, name: file, hint: format === "pdf" && !app, hold: format === "pdf" && app }) });
    if (!r.ok) throw new Error(await problem(r));
    let fileName = file + "." + format;
    if (format === "pdf" && app) {
      const j = await r.json();
      return { state: await printInApp(j.token, j.name), fileName: j.name || fileName };
    }
    const blob = await r.blob();
    try { fileName = decodeURIComponent(r.headers.get("X-Tadamoo-Filename") || "") || fileName; } catch (err) { /* the name asked for */ }
    if (format === "pdf" && win) { printInBrowser(blob, win); status.textContent = T("exp.pdf.browser"); return { state: "saved", fileName, stay: true, used: true }; }
    if (format === "pdf") { await saveBlob(new Blob([blob], { type: "text/html" }), fileName.replace(/\.pdf$/, ".html")); status.textContent = T("exp.pdf.nowin"); return { state: "saved", fileName, stay: true }; }
    return { state: await saveBlob(blob, fileName), fileName };
  }

  /* ---- the window ---- */
  const pad = n => String(n).padStart(2, "0");
  const today = () => { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };
  function titleOf(snap) {
    if (snap.title) return snap.title;
    const first = snap.items.find(it => it.kind === "prompt" && (it.text || "").trim());
    return first ? first.text.trim().split("\n")[0].slice(0, 60) : T("exp.untitled");
  }
  const cleanName = s => s.replace(/[\\/:*?"<>|\u0000-\u001f]/g, " ").replace(/\s+/g, " ").trim().slice(0, 120);

  function openDialog(trigger) {
    const snap = window.TadamooChat.snapshot();
    const counts = { prompts: 0, reasoning: 0, actions: 0, answers: 0 };
    snap.items.forEach(it => { if (ITEM_KIND[it.kind]) counts[ITEM_KIND[it.kind]]++; });
    const saved = load();
    const dialog = mk("dialog", "exp");
    dialog.setAttribute("aria-labelledby", "exp-title"); dialog.setAttribute("aria-describedby", "exp-lead");
    const form = mk("form", "exp-form");
    const head = mk("div", "exp-head"), h2 = mk("h2", "", T("exp.title")), x = mk("button", "btn ghost exp-x", T("exp.close"));
    h2.id = "exp-title"; x.type = "button"; x.title = T("exp.close.title");
    head.append(h2, x);
    const body = mk("div", "exp-body");
    const lead = mk("p", "exp-lead", T("exp.lead")); lead.id = "exp-lead";
    body.append(lead);

    const set = mk("fieldset", "exp-set");
    set.append(mk("legend", "", T("exp.what")));
    const boxes = {};
    KINDS.forEach(kind => {
      const row = mk("div", "exp-opt"), label = mk("label", "exp-check"), box = mk("input");
      const help = mk("p", "exp-help", T(KIND_TEXT[kind][1])); help.id = "exp-d-" + kind;
      box.type = "checkbox"; box.checked = !!saved[kind]; box.setAttribute("aria-describedby", help.id);
      label.append(box, mk("span", "", T(KIND_TEXT[kind][0], counts[kind])));
      row.append(label, help); set.append(row);
      boxes[kind] = box;
    });
    body.append(set);

    const fmtRow = mk("div", "exp-field"), fmtLabel = mk("label", "", T("exp.format")), fmt = mk("select");
    fmt.id = "exp-format"; fmtLabel.htmlFor = fmt.id;
    FORMATS.forEach(f => { const o = mk("option", "", T(FORMAT_TEXT[f][0])); o.value = f; fmt.append(o); });
    fmt.value = FORMATS.includes(saved.format) ? saved.format : DEFAULTS.format;
    const fmtHelp = mk("p", "exp-help"); fmtHelp.id = "exp-format-d"; fmt.setAttribute("aria-describedby", fmtHelp.id);
    const paintFormat = () => { fmtHelp.textContent = T(FORMAT_TEXT[fmt.value][1]); };
    fmtRow.append(fmtLabel, fmt, fmtHelp); body.append(fmtRow);

    const nameRow = mk("div", "exp-field"), nameLabel = mk("label", "", T("exp.name")), name = mk("input", "exp-name");
    name.id = "exp-name"; nameLabel.htmlFor = name.id; name.type = "text"; name.autocomplete = "off"; name.spellcheck = false; name.maxLength = 120;
    name.value = cleanName(`${titleOf(snap)} ${today()}`);
    const nameHelp = mk("p", "exp-help", T("exp.name.d")); nameHelp.id = "exp-name-d"; name.setAttribute("aria-describedby", nameHelp.id);
    nameRow.append(nameLabel, name, nameHelp); body.append(nameRow);
    body.append(mk("p", "exp-help", T("exp.base", new URL(".", location.href).href)));

    const foot = mk("div", "exp-foot"), status = mk("p", "exp-status"), cancel = mk("button", "btn", T("exp.cancel")), go = mk("button", "btn primary", T("exp.save"));
    status.setAttribute("role", "status"); status.id = "exp-status";
    cancel.type = "button"; go.type = "submit"; go.title = T("exp.save.title");
    foot.append(status, cancel, go);
    form.append(head, body, foot); dialog.append(form);

    const ticked = () => Object.fromEntries(KINDS.map(k => [k, boxes[k].checked]));
    const available = () => snap.items.length > 0;
    const whyNot = () => {                                  // what stops a save now, in words ("" when nothing does)
      const sel = ticked(), any = KINDS.some(k => sel[k]), some = KINDS.some(k => sel[k] && counts[k] > 0);
      return !available() ? T("exp.empty") : !any ? T("exp.none") : !some ? T("exp.nothing") : "";
    };
    let busy = false;
    const refresh = () => { go.disabled = busy || !!whyNot(); if (!busy) status.textContent = whyNot(); };
    const working = on => { busy = on; dialog.setAttribute("aria-busy", String(on)); go.disabled = on || !!whyNot(); };
    KINDS.forEach(k => { boxes[k].onchange = refresh; });
    fmt.onchange = paintFormat; paintFormat(); refresh();

    const close = () => { try { dialog.close(); } catch (e) { /* closed */ } };
    x.onclick = cancel.onclick = close;
    dialog.addEventListener("close", () => { dialog.remove(); trigger.focus(); });
    dialog.addEventListener("click", e => { if (e.target === dialog) close(); });
    form.onsubmit = async e => {
      e.preventDefault();
      if (busy || go.disabled) return;
      const sel = ticked(), format = fmt.value, file = cleanName(name.value) || T("exp.untitled");
      keep({ ...sel, format });
      working(true); status.textContent = T("exp.working");
      const app = inApp();
      let win = null;
      if (format === "pdf" && !app) { try { win = window.open("", "_blank"); } catch (err) { win = null; } }     // opened now, while the click still allows it
      let used = false;
      try {
        const done = await produce({ snapshot: await collect(sel, titleOf(snap)), select: sel }, format, file, win, status);
        used = done.used;
        if (done.stay) { working(false); return; }
        if (done.state === "saved") { chatStatus(T("exp.saved", done.fileName)); close(); return; }
        status.textContent = done.state === "cancelled" ? T("exp.cancelled") : T("exp.fail", "");
      } catch (err) {
        status.textContent = T("exp.fail", (err && err.message) || "");
      } finally {
        if (win && !used) { try { win.close(); } catch (err) { /* already closed */ } }
      }
      working(false);
    };
    document.body.append(dialog);
    dialog.showModal();
    (go.disabled ? boxes.prompts : go).focus();
  }

  /* ---- one message: "Save as file" beside Copy. Only the person's click opens it; the text goes through the same route as the export ---- */
  const MESSAGE_KEY = "tadamoo.export.message";
  const loadFormat = () => { try { const f = JSON.parse(localStorage.getItem(MESSAGE_KEY) || "{}").format; return FORMATS.includes(f) ? f : "md"; } catch (e) { return "md"; } };
  const keepFormat = format => { try { localStorage.setItem(MESSAGE_KEY, JSON.stringify({ format })); } catch (e) { /* this page only */ } };
  const headingOf = text => {
    const line = String(text || "").split("\n").map(l => l.replace(/^\s*#+\s*/, "").replace(/[*_`]/g, "").trim()).find(Boolean);
    return line ? line.slice(0, 60) : T("exp.untitled");
  };
  function openMessageDialog(trigger, message) {
    const text = String((message && message.text) || "");
    if (!text.trim()) return;
    const dialog = mk("dialog", "exp");
    dialog.setAttribute("aria-labelledby", "sf-title"); dialog.setAttribute("aria-describedby", "sf-lead");
    const form = mk("form", "exp-form");
    const head = mk("div", "exp-head"), h2 = mk("h2", "", T("sf.title")), x = mk("button", "btn ghost exp-x", T("exp.close"));
    h2.id = "sf-title"; x.type = "button"; x.title = T("exp.close.title");
    head.append(h2, x);
    const body = mk("div", "exp-body");
    const lead = mk("p", "exp-lead", T("sf.lead")); lead.id = "sf-lead";
    body.append(lead);
    const fmtRow = mk("div", "exp-field"), fmtLabel = mk("label", "", T("exp.format")), fmt = mk("select");
    fmt.id = "sf-format"; fmtLabel.htmlFor = fmt.id;
    FORMATS.forEach(f => { const o = mk("option", "", T(FORMAT_TEXT[f][0])); o.value = f; fmt.append(o); });
    fmt.value = loadFormat();
    const fmtHelp = mk("p", "exp-help"); fmtHelp.id = "sf-format-d"; fmt.setAttribute("aria-describedby", fmtHelp.id);
    const paintFormat = () => { fmtHelp.textContent = T(FORMAT_TEXT[fmt.value][1]); };
    fmtRow.append(fmtLabel, fmt, fmtHelp); body.append(fmtRow);
    const nameRow = mk("div", "exp-field"), nameLabel = mk("label", "", T("exp.name")), name = mk("input", "exp-name");
    name.id = "sf-name"; nameLabel.htmlFor = name.id; name.type = "text"; name.autocomplete = "off"; name.spellcheck = false; name.maxLength = 120;
    name.value = cleanName(`${headingOf(text)} ${today()}`);
    const nameHelp = mk("p", "exp-help", T("exp.name.d")); nameHelp.id = "sf-name-d"; name.setAttribute("aria-describedby", nameHelp.id);
    nameRow.append(nameLabel, name, nameHelp); body.append(nameRow);
    const foot = mk("div", "exp-foot"), status = mk("p", "exp-status"), cancel = mk("button", "btn", T("exp.cancel")), go = mk("button", "btn primary", T("exp.save"));
    status.setAttribute("role", "status");
    cancel.type = "button"; go.type = "submit"; go.title = T("exp.save.title");
    foot.append(status, cancel, go);
    form.append(head, body, foot); dialog.append(form);
    fmt.onchange = paintFormat; paintFormat();

    let busy = false;
    const working = on => { busy = on; dialog.setAttribute("aria-busy", String(on)); go.disabled = on; };
    const close = () => { try { dialog.close(); } catch (e) { /* closed */ } };
    x.onclick = cancel.onclick = close;
    dialog.addEventListener("close", () => { dialog.remove(); if (trigger && trigger.isConnected) trigger.focus(); });
    dialog.addEventListener("click", e => { if (e.target === dialog) close(); });
    form.onsubmit = async e => {
      e.preventDefault();
      if (busy) return;
      const format = fmt.value, file = cleanName(name.value) || T("exp.untitled");
      keepFormat(format);
      working(true); status.textContent = T("exp.working");
      let win = null;
      if (format === "pdf" && !inApp()) { try { win = window.open("", "_blank"); } catch (err) { win = null; } }
      let used = false;
      try {
        const snap = { title: "", session: window.TadamooChat.snapshot().session, items: [{ kind: "answer", turn: message.turn || 1, text, time: message.time || 0 }] };
        const done = await produce({ snapshot: await collect({ answers: true }, headingOf(text), snap), select: { answers: true }, bare: true }, format, file, win, status);
        used = done.used;
        if (done.stay) { working(false); return; }
        if (done.state === "saved") { chatStatus(T("exp.saved", done.fileName)); close(); return; }
        status.textContent = done.state === "cancelled" ? T("exp.cancelled") : T("exp.fail", "");
      } catch (err) {
        status.textContent = T("exp.fail", (err && err.message) || "");
      } finally {
        if (win && !used) { try { win.close(); } catch (err) { /* already closed */ } }
      }
      working(false);
    };
    document.body.append(dialog);
    dialog.showModal();
    go.focus();
  }
  window.TadamooSave = { message: openMessageDialog };

  const button = document.getElementById("export-open");
  if (button) button.addEventListener("click", () => openDialog(button));
})();
