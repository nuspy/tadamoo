"use strict";
/* The Avatar card of the Voice tab and the camera setting of Settings > Voice. The card shows the avatar function and the personality's avatar,
   the rendered picture (an MJPEG stream relayed by the server), a sentence to try, the avatars on this PC (prepare a clip, load one, give it to a
   personality, delete) and the guided creation of a new one: name, photo (the only thing needed: the avatar for the phone and the ESP32 is made from
   it at once, with no GPU), an optional voice sample, an optional short video (real mouth shapes and the emergency video from the PC), then the
   avatar is made and the page shows what it can do. The PC video (MuseTalk) is prepared only when the person asks for it.
   Every request goes through the server: JSON ones on the proxy's list (serve/voice_proxy.py), files and the picture on /api/voice/avatar/*
   (serve/voice_avatar.py). Everything the server says enters the page as text. voicetab.js mounts the card and calls update() on its poll. */
(function () {
  const STEPS = ["name", "foto", "voce", "video", "make"];
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };
  const clear = node => { while (node.firstChild) node.firstChild.remove(); return node; };
  const mb = bytes => bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
  const sigOf = v => JSON.stringify(v);
  const msgOf = r => {                                       // the service answers {error: "text"}, the server {error: {message}}
    const e = r.data && r.data.error;
    if (typeof e === "string") return e;
    return (e && ((LANG === "it" && e.message_it) || e.message)) || (r.status === 0 ? T("va.e.offline") : `HTTP ${r.status}`);
  };

  let ctx = null, card = null, S = null, list = null, listSig = "", cam = null, camMsg = "", busy = false;
  let wiz = null, mouth = null, say = { busy: false, msg: "", bad: false }, sayText = "", rowMsg = {}, askDelete = "", useMsg = {}, stream = null;
  const posters = new Map();                                 // name -> {sig, url}
  const sheets = new Map();                                  // name -> {sig, url}: the mouth sprites over the photo
  const parts = {}, sigs = {}, pick = {};
  const changed = (id, value) => { const v = sigOf(value); if (sigs[id] === v) return false; sigs[id] = v; return true; };

  // ---------------------------------------------------------------- the live picture
  function stopStream() {
    if (!stream) return;
    stream.stop();
    stream = null;
  }
  function startStream(img) {
    if (stream && stream.img === img) return;
    stopStream();
    const url = "api/voice/avatar/preview.mjpg";
    const headers = authHeaders();
    if (!Object.keys(headers).length) {                       // nothing to send: the browser plays the MJPEG itself
      img.src = url + "?t=" + Date.now();
      stream = { img, stop: () => { img.removeAttribute("src"); } };
      return;
    }
    const ctl = new AbortController();
    let last = "";
    stream = { img, stop: () => { ctl.abort(); if (last) URL.revokeObjectURL(last); img.removeAttribute("src"); } };
    (async () => {                                           // with a key the picture is read here: an <img> cannot send it
      try {
        const r = await fetch(url, { headers, signal: ctl.signal, cache: "no-store" });
        if (!r.ok || !r.body) return;
        const reader = r.body.getReader();
        let buf = new Uint8Array(0);
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          const joined = new Uint8Array(buf.length + value.length);
          joined.set(buf); joined.set(value, buf.length); buf = joined;
          for (;;) {
            let s = -1, e = -1;
            for (let i = 0; i + 1 < buf.length; i++) if (buf[i] === 0xFF && buf[i + 1] === 0xD8) { s = i; break; }
            if (s < 0) { buf = new Uint8Array(0); break; }
            for (let i = s + 2; i + 1 < buf.length; i++) if (buf[i] === 0xFF && buf[i + 1] === 0xD9) { e = i + 2; break; }
            if (e < 0) { buf = buf.slice(s); break; }
            const next = URL.createObjectURL(new Blob([buf.slice(s, e)], { type: "image/jpeg" }));
            img.src = next;
            if (last) URL.revokeObjectURL(last);
            last = next; buf = buf.slice(e);
          }
        }
      } catch (e) { /* the stream ended or the tab left */ }
    })();
  }

  // ---------------------------------------------------------------- pictures with the key
  async function posterUrl(name, sig) {
    const have = posters.get(name);
    if (have && have.sig === sig) return have.url;
    try {
      const r = await fetch(`api/voice/avatar/${encodeURIComponent(name)}/poster`, { headers: authHeaders(), cache: "no-store" });
      if (!r.ok) return "";
      const url = URL.createObjectURL(await r.blob());
      if (have) URL.revokeObjectURL(have.url);
      posters.set(name, { sig, url });
      return url;
    } catch (e) { return ""; }
  }
  async function fillPoster(img, name, sig) { const url = await posterUrl(name, sig); if (url && img.isConnected) img.src = url; }
  async function sheetUrl(name, sig) {
    const have = sheets.get(name);
    if (have && have.sig === sig) return have.url;
    try {
      const r = await fetch(`api/voice/avatar/${encodeURIComponent(name)}/sprites.png`, { headers: authHeaders(), cache: "no-store" });
      if (!r.ok) return "";
      const url = URL.createObjectURL(await r.blob());
      if (have) URL.revokeObjectURL(have.url);
      sheets.set(name, { sig, url });
      return url;
    } catch (e) { return ""; }
  }
  async function fillSheet(img, name, sig) { const url = await sheetUrl(name, sig); if (url && img.isConnected) img.src = url; else if (img.isConnected) img.hidden = true; }

  // ---------------------------------------------------------------- the card
  const loaded = () => !!(S && S.loaded);
  const fnInfo = () => { const l = ctx.live(); return l && l.fns && l.fns.avatar; };
  const note = (text, bad) => { const n = el("p", "vt-note", text); n.setAttribute("role", bad ? "alert" : "status"); n.dataset.bad = bad ? "1" : ""; n.hidden = !text; return n; };

  function mount(node, context) {
    ctx = context; card = node; list = null; Object.keys(sigs).forEach(k => delete sigs[k]);
    for (const k of Object.keys(parts)) delete parts[k];
    for (const id of ["status", "engine", "stage", "say", "list", "train", "wizard"]) { parts[id] = el("div", "va-part"); parts[id].id = "va-" + id; card.append(parts[id]); }
    sayText = sayText || T("va.sample");
    paintAll();
    refreshList();
  }
  function paintAll() { Object.keys(sigs).forEach(k => delete sigs[k]); paintStatus(); paintEngine(); paintStage(); paintSay(); paintList(); paintTrain(); paintWizard(); }

  async function update() {
    if (!card || busy) return;
    const l = ctx.live();
    if (!l.up) { S = null; paintAll(); return; }
    busy = true;
    const r = await ctx.svc("GET", "/avatar/state");
    busy = false;
    const was = S;
    S = r.ok ? r.data : null;
    if (was && was.prepare && was.prepare.state === "running" && S && S.prepare && S.prepare.state !== "running") { rowMsg[S.prepare.avatar] = prepMessage(S.prepare); refreshList(); }
    paintStatus(); paintEngine(); paintStage(); paintSay(); paintTrain();
    if (listSig !== sigOf([S && S.prepare, ctx.personas().length, S && S.persona])) paintList();
    const acts = document.getElementById("va-result-acts");
    if (wiz && wiz.result && acts && changed("result", [S && S.persona, S && S.preparing, S && S.prepare, S && S.loaded, rowMsg])) fillResultActions(acts, wiz.result);
  }
  function stop() { stopStream(); }

  function paintStatus() {
    const l = ctx.live(), fn = fnInfo();
    if (!changed("status", [l.up, l.reason, fn, ctx.persona(), ctx.personas().map(p => p.slug + p.name), S && S.link, S && S.error, S && S.preparing])) return;
    const box = clear(parts.status), row = el("div", "vt-chips");
    const state = fn ? fn.state : "unknown";
    row.append(el("span", "vt-chip " + (state === "loaded" ? "vt-ok" : state === "error" ? "vt-warn" : "vt-off"), `${T("va.fn")}: ${T("vt.st." + state)}`));
    const slug = ctx.persona(), p = ctx.personas().find(x => x.slug === slug);
    row.append(el("span", "vt-chip", slug ? T("va.persona", p ? p.name : slug) : T("va.persona.none")));
    if (S && S.link) row.append(el("span", "vt-chip " + (S.link.avatar ? "vt-ok" : ""), S.link.avatar ? T("va.avatar", S.link.avatar) : T("va.avatar.none")));
    box.append(row);
    if (!l.up) { box.append(note(l.reason, true)); return; }
    const why = fn && (fn.reason || fn.problem || (fn.state === "error" ? fn.error : ""));
    if (fn && fn.state !== "loaded") {
      box.append(el("p", "vt-help", T("va.off")));
      if (why) box.append(note(why, true));
      box.append(btn(T("va.off.open"), "ghost", ctx.openModels));
    } else if (S && S.link && S.link.problem && slug) box.append(note(S.link.problem, false));
    if (S && S.error && !S.preparing) box.append(note(S.error, true));
  }

  function paintEngine() {                                    // the engine and the height the web preview uses (serve/web/voiceengines.js)
    const text = window.VoiceEngines ? window.VoiceEngines.webLine(ctx.api) : "";
    if (!changed("engine", text)) return;
    clear(parts.engine);
    if (text) parts.engine.append(Object.assign(el("p", "vt-help", text), { id: "va-engine-line" }));
  }

  function paintTrain() {                                     // the training of a personalised model (serve/web/voicetrain.js)
    if (window.VoiceTrain) window.VoiceTrain.paint(parts.train, ctx);
  }

  function paintStage() {
    const pr = S && S.progress;
    if (!changed("stage", [loaded(), S && S.running, S && S.preparing, pr && pr.phase, pr && Math.round(pr.percent || 0), S && S.link && S.link.avatar, rowMsg])) return;
    const box = clear(parts.stage);
    stopStreamIfNot();
    if (!loaded()) return;
    if (S.preparing) {
      const pr = S.progress;
      box.append(note(T("va.starting", S.preparing, pr && pr.phase ? `${T("va.phase." + pr.phase)} ${pr.percent == null ? "" : Math.round(pr.percent) + "%"}` : "..."), false));
      return;
    }
    if (!S.running) {
      box.append(el("p", "vt-help", T("va.nopic")));
      if (S.link && S.link.avatar) box.append(loadButton(S.link.avatar, T("va.load.mine")));
      return;
    }
    const wrap = el("div", "va-stage"), img = el("img", "va-live");
    img.alt = T("va.stream.alt"); img.width = 480; img.height = 480;
    wrap.append(img, el("p", "va-wait vt-help", T("va.waiting")));
    img.onload = () => { const w = wrap.querySelector(".va-wait"); if (w) w.hidden = true; };
    box.append(wrap);
    startStream(img);
  }
  function stopStreamIfNot() { if (!loaded() || !S.running || S.preparing) stopStream(); }

  function loadButton(name, label) {
    const b = btn(rowMsg["load:" + name] === "busy" ? T("va.loading") : (label || T("va.load")), "", async () => {
      rowMsg["load:" + name] = "busy"; paintStage(); paintList();
      const r = await ctx.svc("POST", "/avatar/start", { persona: name });
      rowMsg["load:" + name] = r.ok ? "" : msgOf(r);
      update(); paintList();
    });
    b.disabled = !loaded() || rowMsg["load:" + name] === "busy" || !!(S && S.preparing);
    return b;
  }

  function paintSay() {
    const ready = !!(S && S.running);
    if (!changed("say", [ready, say])) return;
    const box = clear(parts.say);
    const f = el("div", "vt-field"), lab = el("label", "", T("va.say.label")), input = el("input");
    input.type = "text"; input.id = "va-say"; input.value = sayText; input.maxLength = 300; input.autocomplete = "off"; lab.htmlFor = "va-say";
    const go = btn(say.busy ? T("va.say.busy") : T("va.say.go"), "", async () => {
      say = { busy: true, msg: "", bad: false }; paintSay();
      const r = await ctx.svc("POST", "/avatar/speak", { text: sayText.trim() });
      say = { busy: false, msg: r.ok ? T("va.say.done") : msgOf(r), bad: !r.ok }; paintSay();
    });
    go.id = "va-say-go";
    go.disabled = !ready || say.busy || !sayText.trim();
    input.disabled = !ready;
    input.oninput = () => { sayText = input.value; go.disabled = !(S && S.running) || say.busy || !sayText.trim(); };
    f.append(lab, input);
    box.append(f, go);
    if (!ready) box.append(el("p", "vt-help", T("va.say.off")));
    box.append(note(say.msg, say.bad));
  }

  // ---------------------------------------------------------------- the avatars
  async function refreshList() {
    const r = await ctx.svc("GET", "/avatar/list");
    list = r.ok ? r.data : null;
    paintList();
  }
  function prepMessage(job) {
    if (job.state === "done") return T("va.prepare.done");
    if (job.state === "cancelled") return T("va.prepare.cancelled");
    return T("va.prepare.failed", job.error || "?");
  }
  function usePicker(row) {
    const wrap = el("div", "va-use"), f = el("div", "vt-field"), lab = el("label", "", T("va.use.pick")), sel = el("select");
    sel.id = "va-use-" + row.name; lab.htmlFor = sel.id;
    sel.onchange = () => { pick[row.name] = sel.value; };
    ctx.personas().forEach(p => { const o = el("option", "", p.name || p.slug); o.value = p.slug; sel.append(o); });
    const mine = ctx.persona();
    if (pick[row.name] && ctx.personas().some(p => p.slug === pick[row.name])) sel.value = pick[row.name];
    else if (mine && ctx.personas().some(p => p.slug === mine)) sel.value = mine;
    f.append(lab, sel);
    const go = btn(T("va.use.go"), "", async () => {
      const p = ctx.personas().find(x => x.slug === sel.value);
      if (!p) return;
      const body = { avatar: row.name };
      if (row.voice && row.voice.engine) body.voice = { engine: row.voice.engine, voice: row.voice.voice };
      const r = await ctx.api("POST", `api/personas/${encodeURIComponent(p.slug)}/update`, body);
      useMsg[row.name] = r.ok ? { text: T("va.use.done", p.name || p.slug, row.name) + (body.voice ? T("va.use.voice", body.voice.voice, body.voice.engine) : ""), bad: false } : { text: msgOf(r), bad: true };
      if (r.ok) await ctx.reloadPersonas();
      paintList();
    });
    go.disabled = !ctx.personas().length;
    wrap.append(f, go);
    return wrap;
  }
  function paintList() {
    const box = clear(parts.list), job = S && S.prepare;
    listSig = sigOf([job, ctx.personas().length, S && S.persona]);
    sigs.list = "";
    box.append(el("h4", "va-h", T("va.list")));
    if (!list) { box.append(note(ctx.live().up ? "" : ctx.live().reason, true)); return; }
    if (!list.avatars.length) box.append(el("p", "vt-help", T("va.list.none")));
    const ul = el("ul", "va-list");
    for (const row of list.avatars) {
      const li = el("li", "va-row");
      li.dataset.name = row.name;
      const img = el("img", "va-thumb");
      img.alt = T("va.poster.alt", row.display_name); img.width = 72; img.height = 72;
      if (row.poster) fillPoster(img, row.name, sigOf([row.size_bytes, row.created]));
      const info = el("div", "va-info"), chips = el("div", "vt-chips");
      info.append(el("strong", "va-name", row.display_name));
      chips.append(el("span", "vt-chip", row.clips.length ? T("va.row.clips", row.clips.length) : T("va.row.noclip")));
      if (row.clips.length) chips.append(el("span", "vt-chip " + (row.prepared.length === row.clips.length ? "vt-ok" : ""), row.prepared.length === row.clips.length ? T("va.row.prepared") : T("va.row.notprepared")));
      if (row.wav2lip) chips.append(el("span", "vt-chip", T("va.row.phone")));
      if (row.visemes) chips.append(el("span", "vt-chip", T("va.row.visemes")));
      if (row.voice && row.voice.voice) chips.append(el("span", "vt-chip", T("va.row.voice", `${row.voice.voice} (${row.voice.engine})`)));
      chips.append(el("span", "vt-chip", mb(row.size_bytes)));
      if (S && S.persona === row.name) chips.append(el("span", "vt-chip vt-ok", T("va.is.loaded")));
      info.append(chips);
      const acts = el("div", "va-acts");
      const active = S && S.persona === row.name;
      if (!active) acts.append(loadButton(row.name));
      const running = job && job.state === "running" && job.avatar === row.name;
      if (row.clips.length && row.prepared.length < row.clips.length && !running) {
        const clip = row.clips.find(c => !row.prepared.includes(c));
        const b = btn(T("va.prepare"), "", async () => {
          const r = await ctx.svc("POST", "/avatar/prepare", { avatar: row.name, clip });
          rowMsg[row.name] = r.ok ? "" : msgOf(r);
          update(); paintList();
        });
        b.disabled = !loaded() || !!(job && job.state === "running");
        acts.append(b);
      }
      acts.append(btn(T("va.delete"), "ghost", () => { askDelete = askDelete === row.name ? "" : row.name; paintList(); }));
      li.append(img, info, acts);
      if (running) {
        const pr = job.progress, line = el("p", "va-prog");
        line.setAttribute("role", "status");
        line.textContent = pr ? (pr.eta_s != null ? T("va.prepare.running", T("va.phase." + pr.phase), Math.round(pr.percent), Math.round(pr.eta_s)) : T("va.prepare.running.noeta", T("va.phase." + pr.phase), Math.round(pr.percent))) : T("va.loading");
        const bar = el("progress", "va-bar"); bar.max = 100; bar.value = pr ? pr.percent : 0;
        li.append(line, bar, btn(T("va.prepare.cancel"), "ghost", async () => { await ctx.svc("POST", "/avatar/prepare/cancel"); update(); }));
      } else if (rowMsg[row.name]) li.append(note(rowMsg[row.name], !/prepared|cancelled/.test(rowMsg[row.name]) && rowMsg[row.name] !== T("va.prepare.done") && rowMsg[row.name] !== T("va.prepare.cancelled")));
      if (rowMsg["load:" + row.name] && rowMsg["load:" + row.name] !== "busy") li.append(note(rowMsg["load:" + row.name], true));
      if (row.clips.length && row.prepared.length < row.clips.length && !running) li.append(el("p", "vt-help", T("va.prepare.why")));
      li.append(usePicker(row));
      if (useMsg[row.name]) li.append(note(useMsg[row.name].text, useMsg[row.name].bad));
      if (askDelete === row.name) {
        const ask = el("div", "va-ask");
        ask.append(el("p", "vt-note", T("va.delete.sure", row.display_name, mb(row.size_bytes))),
          btn(T("va.delete.yes"), "", async () => {
            const r = await ctx.svc("DELETE", `/avatar/${row.name}`);
            askDelete = "";
            rowMsg[row.name] = r.ok ? "" : (r.data && r.data.code === "in_use" ? T("va.delete.busy") : msgOf(r));
            await refreshList();
          }), btn(T("va.delete.no"), "ghost", () => { askDelete = ""; paintList(); }));
        li.append(ask);
      }
      ul.append(li);
    }
    box.append(ul);
    if (list.avatars.length) box.append(el("p", "vt-help", T("va.list.size", mb(list.size_bytes))), el("p", "vt-help", T("va.use.hint")));
    box.append(btn(T("va.new"), "primary", openWizard));
    box.lastChild.id = "va-new";
    box.lastChild.hidden = !!wiz;
  }

  // ---------------------------------------------------------------- the wizard
  function upload(step, file, onProgress) {
    return new Promise(resolve => {
      const x = new XMLHttpRequest(), form = new FormData();
      form.append("file", file);
      x.open("POST", `api/voice/avatar/wizard/${wiz.draft}/${step}`);
      const h = authHeaders();
      Object.keys(h).forEach(k => x.setRequestHeader(k, h[k]));
      x.upload.onprogress = e => { if (e.lengthComputable) onProgress(Math.round(100 * e.loaded / e.total)); };
      x.onload = () => { let data = {}; try { data = JSON.parse(x.responseText); } catch (e) { /* no body */ } resolve({ ok: x.status >= 200 && x.status < 300, status: x.status, data }); };
      x.onerror = () => resolve({ ok: false, status: 0, data: {} });
      x.send(form);
    });
  }
  async function openWizard() {
    wiz = { step: 0, draft: "", name: "", sent: {}, msg: "", bad: false, progress: null, result: null, making: false, starting: true };
    paintList(); paintWizard();
    const r = await ctx.svc("POST", "/avatar/wizard/start");
    if (!wiz) return;
    wiz.starting = false;
    if (r.ok) wiz.draft = r.data.draft_id; else { wiz.msg = msgOf(r); wiz.bad = true; }
    paintWizard();
  }
  async function closeWizard() {
    const w = wiz;
    wiz = null; mouth = null;
    if (w && w.draft && !w.result) await ctx.svc("DELETE", `/avatar/wizard/${w.draft}`);
    paintList(); paintWizard();
    refreshList();
  }
  function fileStep(step, help, accept, multiple) {
    const box = el("div", "va-file"), f = el("div", "vt-field"), lab = el("label", "", T("va.choose")), input = el("input");
    input.type = "file"; input.accept = accept; input.id = "va-file-" + step; input.multiple = !!multiple; lab.htmlFor = input.id;
    if (step === "foto") input.setAttribute("capture", "user");
    const prog = el("progress", "va-bar"); prog.max = 100; prog.value = 0; prog.hidden = true;
    const status = note(wiz.sent[step] || "", false);
    input.onchange = async () => {
      const files = Array.from(input.files || []);
      if (!files.length) return;
      for (const file of files) {
        prog.hidden = false; prog.value = 0; wiz.msg = "";
        const r = await upload(step, file, p => { prog.value = p; status.textContent = T("va.uploading", p); status.hidden = false; });
        if (!wiz) return;
        if (r.ok) {
          wiz.sent[step] = T("va.uploaded", file.name);
          status.textContent = wiz.sent[step]; status.dataset.bad = ""; status.hidden = false;
        } else { status.textContent = msgOf(r); status.dataset.bad = "1"; status.hidden = false; break; }
      }
      prog.hidden = true;
      paintWizardButtons();
    };
    f.append(lab, input);
    box.append(el("p", "vt-help", help), f, prog, status);
    return box;
  }
  function paintWizard() {
    const box = clear(parts.wizard);
    if (!wiz) return;
    const w = wiz, name = STEPS[w.step];
    const wrap = el("section", "va-wiz");
    wrap.setAttribute("aria-label", T("va.new"));
    if (w.result) { wrap.append(resultView(w.result)); box.append(wrap); return; }
    const head = el("div", "va-wiz-head");
    head.append(el("h4", "va-h", `${T("va.new")} · ${T("va.s." + name)}`), el("span", "vt-chip", T("va.step", w.step + 1, STEPS.length)));
    wrap.append(head);
    if (w.starting) wrap.append(el("p", "vt-help", T("va.loading")));
    else if (!w.draft) wrap.append(note(w.msg, true));
    else if (name === "name") {
      const f = el("div", "vt-field"), lab = el("label", "", T("va.name.label")), input = el("input");
      input.type = "text"; input.id = "va-name"; input.maxLength = 40; input.value = w.name; input.autocomplete = "off"; lab.htmlFor = input.id;
      input.oninput = () => { w.name = input.value; paintWizardButtons(); };
      f.append(lab, input, el("p", "vt-help", T("va.name.help")));
      wrap.append(f);
    } else if (name === "foto") wrap.append(fileStep("foto", T("va.foto.help"), "image/*", false));
    else if (name === "voce") wrap.append(fileStep("voce", T("va.voce.help"), "audio/*,.wav,.mp3,.flac,.ogg", false));
    else if (name === "video") wrap.append(fileStep("video", T("va.video.help"), "video/mp4,.mp4", false));
    else {
      wrap.append(el("p", "vt-help", T("va.make.sum", w.name.trim(), w.sent.voce ? T("va.make.voice") : "", w.sent.video ? T("va.make.video") : "")));
    }
    wrap.append(note(w.msg, w.bad));
    const nav = el("div", "va-nav");
    nav.id = "va-nav";
    wrap.append(nav);
    box.append(wrap);
    paintWizardButtons();
  }
  const canGo = () => {
    const w = wiz, step = STEPS[w.step];
    if (step === "name") return !!w.name.trim() && w.name.trim().length <= 40;
    if (step === "foto") return !!w.sent.foto;
    return true;
  };
  function paintWizardButtons() {
    const nav = document.getElementById("va-nav");
    if (!nav || !wiz) return;
    clear(nav);
    const w = wiz, step = STEPS[w.step], optional = ["voce", "video"].includes(step);
    nav.append(btn(T("va.cancel"), "ghost", closeWizard));
    if (w.step > 0) nav.append(btn(T("va.back"), "ghost", () => { w.step -= 1; w.msg = ""; paintWizard(); }));
    if (step === "make") {
      const go = btn(w.making ? T("va.make.busy") : T("va.make.go"), "primary", makeIt);
      go.id = "va-make"; go.disabled = w.making || !w.sent.foto || !w.name.trim();
      nav.append(go);
    } else {
      const next = btn(optional && !wizSent(step) ? T("va.skip") : T("va.next"), "primary", () => { w.step += 1; w.msg = ""; paintWizard(); });
      next.id = "va-next"; next.disabled = !canGo();
      nav.append(next);
    }
  }
  const wizSent = step => !!wiz.sent[step];
  async function makeIt() {
    const w = wiz;
    w.making = true; w.msg = ""; paintWizardButtons();
    const r = await ctx.svc("POST", `/avatar/wizard/${w.draft}/elabora`, { name: w.name.trim(), language: LANG });
    w.making = false;
    if (!r.ok) { w.msg = msgOf(r); w.bad = true; paintWizardButtons(); paintWizard(); return; }
    w.result = { ...r.data, stamp: Date.now() }; mouth = { p1: null, p2: null };
    await refreshList();
    paintWizard();
  }
  function resultView(res) {
    const view = el("div", "va-result"), name = res.avatar;
    view.append(el("h4", "va-h", T("va.made", res.display_name)));
    const stage = el("div", "va-mouth-wrap"), img = el("img", "va-poster");
    img.alt = T("va.poster.alt", res.display_name); img.width = 320; img.height = 320;
    fillPoster(img, name, "new-" + res.stamp);
    const d1 = el("span", "va-dot"), d2 = el("span", "va-dot");
    d1.hidden = d2.hidden = true;
    stage.append(img, d1, d2);
    const place = (dot, p) => { dot.hidden = !p; if (p) { dot.style.left = (100 * p[0] / img.naturalWidth) + "%"; dot.style.top = (100 * p[1] / img.naturalHeight) + "%"; } };
    const help = el("p", "vt-help", T("va.mouth.left")), save = btn(T("va.mouth.save"), "", async () => {
      const r = await ctx.svc("POST", `/personas/${name}/wav2lip/bocca`, { p1: mouth.p1, p2: mouth.p2 });
      help.textContent = r.ok ? T("va.mouth.saved") : msgOf(r);
      if (r.ok) { res.stamp = Date.now(); const sheet = view.querySelector(".va-sheet"); if (sheet) { sheet.hidden = false; fillSheet(sheet, name, "mouth-" + res.stamp); } }
    });
    save.disabled = true;
    img.onclick = ev => {
      const rect = img.getBoundingClientRect(), p = [(ev.clientX - rect.left) * img.naturalWidth / rect.width, (ev.clientY - rect.top) * img.naturalHeight / rect.height];
      if (!mouth.p1 || (mouth.p1 && mouth.p2)) { mouth.p1 = p; mouth.p2 = null; place(d2, null); help.textContent = T("va.mouth.right"); } else { mouth.p2 = p; help.textContent = T("va.mouth.both"); }
      place(d1, mouth.p1); place(d2, mouth.p2);
      save.disabled = !(mouth.p1 && mouth.p2);
    };
    view.append(modesView(res), el("p", "vt-help", T("va.mouth.help")), stage, help, save);
    if (res.voice && res.voice.engine) view.append(el("p", "vt-help", T("va.voice.set", res.voice.engine)));
    else if (res.voice_note) view.append(note(T("va.voice.note", res.voice_note), false));
    const row = list && list.avatars.find(a => a.name === name);
    view.append(row ? usePicker({ name, voice: res.voice }) : el("span"));
    const actions = el("div", "va-acts");
    actions.id = "va-result-acts";
    fillResultActions(actions, res);
    view.append(actions, el("p", "vt-help", res.clips.length ? T("va.prepare.why") : T("va.nopic")));
    return view;
  }
  function modesView(res) {
    const box = el("div", "va-modes"), have = res.render_modes || [];
    box.append(el("h5", "va-h", T("va.modes")));
    const ul = el("ul", "va-mode-list");
    for (const m of ["visemes", "wav2lip", "video"]) {
      const li = el("li", "va-mode"), on = have.includes(m);
      li.dataset.mode = m; li.dataset.on = on ? "1" : "";
      li.append(el("strong", "", `${T("va.mode." + m)}: ${on ? T("va.mode.yes") : T("va.mode.no")}`), el("span", "vt-help", T("va.mode." + m + ".help")));
      ul.append(li);
    }
    box.append(ul);
    if (have.includes("visemes")) {
      box.append(el("h5", "va-h", T("va.sheet.title")));
      const sheet = el("img", "va-sheet");
      sheet.alt = T("va.sheet.alt", res.display_name);
      fillSheet(sheet, res.avatar, "new-" + res.stamp);
      box.append(sheet);
    }
    return box;
  }
  function fillResultActions(actions, res) {
    clear(actions);
    const name = res.avatar, job = S && S.prepare, running = job && job.state === "running" && job.avatar === name;
    if (!(S && S.persona === name)) actions.append(loadButton(name));
    if (res.clips.length) {
      const b = btn(T("va.prepare"), "", async () => {
        const r = await ctx.svc("POST", "/avatar/prepare", { avatar: name, clip: res.clips[0] });
        rowMsg[name] = r.ok ? "" : msgOf(r);
        await update(); paintList(); fillResultActions(actions, res);
      });
      b.id = "va-result-prepare";
      b.disabled = !loaded() || !!(job && job.state === "running");
      actions.append(b);
    }
    if (running) {
      const pr = job.progress, line = el("p", "va-prog");
      line.setAttribute("role", "status");
      line.textContent = pr ? T("va.prepare.running.noeta", T("va.phase." + pr.phase), Math.round(pr.percent)) : T("va.loading");
      const bar = el("progress", "va-bar"); bar.max = 100; bar.value = pr ? pr.percent : 0;
      actions.append(line, bar, btn(T("va.prepare.cancel"), "ghost", async () => { await ctx.svc("POST", "/avatar/prepare/cancel"); update(); }));
    } else if (rowMsg[name]) actions.append(note(rowMsg[name], rowMsg[name] !== T("va.prepare.done") && rowMsg[name] !== T("va.prepare.cancelled")));
    const done = btn(T("va.done"), "primary", closeWizard);
    done.id = "va-done";
    actions.append(done);
  }

  // ---------------------------------------------------------------- Settings > Voice: the camera
  function settingsCard(apiFn) {
    const api = apiFn, box = el("section", "vt-card"), h = el("h3", "vt-h", T("va.cam.title"));
    h.id = "vb-h-avatar"; box.setAttribute("aria-labelledby", h.id);
    const wrap = el("div", "vt-field"), row = el("label", "vt-check"), input = el("input"), status = el("p", "vt-help");
    input.type = "checkbox"; input.id = "va-camera"; input.checked = !!(cam && cam.virtual_camera);
    input.onchange = async () => {
      const r = await api("POST", "api/voice/avatar", { virtual_camera: input.checked });
      if (r.ok) { cam = r.data; camMsg = T("va.cam.saved"); } else { camMsg = msgOf(r); input.checked = !input.checked; }
      paintCamStatus(status);
    };
    row.append(input, el("span", "", T("va.cam.label")));
    wrap.append(row, el("p", "vt-help", T("va.cam.help")));
    box.append(h, wrap, status);
    paintCamStatus(status);
    if (!cam) api("GET", "api/voice/avatar").then(r => { if (r.ok) { cam = r.data; input.checked = !!cam.virtual_camera; paintCamStatus(status); } });
    return box;
  }
  function paintCamStatus(status) {
    const c = S && S.outputs && S.outputs.camera;
    status.id = "va-camera-status";
    status.setAttribute("role", "status");
    if (camMsg) { status.textContent = camMsg; return; }
    if (!cam || !cam.virtual_camera) status.textContent = T("va.cam.off");
    else if (c && !c.supported) status.textContent = T("va.cam.unsupported", c.reason);
    else if (c && c.running) status.textContent = T("va.cam.running", c.device || "?");
    else if (c && c.reason) status.textContent = c.reason;
    else status.textContent = T("va.cam.waiting");
  }

  document.addEventListener("tadamoo:avatar-engines", () => { if (card && parts.engine) paintEngine(); });
  document.addEventListener("tadamoo:view", e => { if (e.detail !== "voice") stop(); });
  document.addEventListener("visibilitychange", () => { if (document.hidden) stop(); });
  window.VoiceAvatar = { mount, update, stop, settingsCard, _state: () => ({ S, wiz }) };
})();
