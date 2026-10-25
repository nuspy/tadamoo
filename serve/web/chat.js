"use strict";
/* Chat: streams from this server's /v1/chat/completions. Replies are rendered from escaped text, so the model's
   output can never inject markup. The conversation is kept in this browser's localStorage when it allows it. */
(() => {
  const STORE = "basalt.chat", EFFORT = "basalt.effort", IMAGE_MAX = 12 << 20;
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const load = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } };
  const keep = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch (e) { return false; } };

  let messages = load(STORE, []);
  if (!Array.isArray(messages)) messages = [];
  let attachments = [];                        // [{name, url}]
  let busy = null;                             // {controller, msg, el}
  let images = false, model = "";

  const msgs = $("#msgs"), prompt = $("#prompt"), effort = $("#effort");
  effort.value = load(EFFORT, "") || "";
  effort.onchange = () => keep(EFFORT, effort.value);

  /* ---- Markdown: escape first, then format a small, safe subset ---- */
  function inline(s) {
    const codes = [];
    s = s.replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
    s = esc(s)
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*\w])\*([^*\n]+)\*(?![*\w])/g, "$1<em>$2</em>")
      .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s"'<>]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${esc(codes[+i])}</code>`);
  }
  function blocks(text) {
    const out = [], lines = text.split("\n");
    let para = [], list = null;
    const flushPara = () => { if (para.length) out.push(`<p>${para.map(inline).join("<br>")}</p>`); para = []; };
    const flushList = () => { if (list) out.push(`<${list.tag}>${list.items.map(i => `<li>${inline(i)}</li>`).join("")}</${list.tag}>`); list = null; };
    for (let i = 0; i < lines.length; i++) {
      const l = lines[i];
      let m;
      if (!l.trim()) { flushPara(); flushList(); continue; }
      if ((m = l.match(/^(#{1,6})\s+(.*)$/))) { flushPara(); flushList(); const h = m[1].length <= 2 ? "h3" : "h4"; out.push(`<${h}>${inline(m[2])}</${h}>`); continue; }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(l)) { flushPara(); flushList(); out.push("<hr>"); continue; }
      if ((m = l.match(/^>\s?(.*)$/))) { flushPara(); flushList(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); continue; }
      if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|?[\s:-]+\|[\s|:-]*$/.test(lines[i + 1])) {
        flushPara(); flushList();
        const cells = row => row.trim().replace(/^\||\|$/g, "").split("|").map(c => inline(c.trim()));
        let html = `<div class="mdt"><table><thead><tr>${cells(l).map(c => `<th>${c}</th>`).join("")}</tr></thead><tbody>`;
        i += 2;
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) html += `<tr>${cells(lines[i++]).map(c => `<td>${c}</td>`).join("")}</tr>`;
        i--;
        out.push(html + "</tbody></table></div>");
        continue;
      }
      if ((m = l.match(/^\s*(?:[-*+]|(\d+)[.)])\s+(.*)$/))) {
        flushPara();
        const tag = m[1] ? "ol" : "ul";
        if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
        list.items.push(m[2]);
        continue;
      }
      if (list && /^\s{2,}\S/.test(l)) { list.items[list.items.length - 1] += " " + l.trim(); continue; }
      flushList();
      para.push(l);
    }
    flushPara(); flushList();
    return out.join("");
  }
  function markdown(text) {
    let html = "", rest = text;
    for (;;) {
      const m = rest.match(/(^|\n)```([^\n`]*)\n/);
      if (!m) { html += blocks(rest); break; }
      html += blocks(rest.slice(0, m.index));
      rest = rest.slice(m.index + m[0].length);
      const end = rest.match(/(^|\n)```[ \t]*(\n|$)/);
      const code = end ? rest.slice(0, end.index) : rest;              // no closing fence yet: still streaming
      html += `<div class="code"><span class="lang">${esc(m[2].trim() || "code")}</span><pre tabindex="0"><code>${esc(code)}</code></pre></div>`;
      if (!end) break;
      rest = rest.slice(end.index + end[0].length);
    }
    return html;
  }

  /* ---- messages ---- */
  const time = t => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  function msgEl(m, i) {
    const el = document.createElement("article");
    el.className = `cmsg ${m.role}${m.outside ? " outside" : ""}`;
    el.dataset.i = i;
    if (m.outside) {
      const o = document.createElement("div"); o.className = "outside-l"; o.textContent = T("outside"); el.append(o);
    }
    if (m.role === "summary") {
      const d = document.createElement("details"); d.className = "compacted";
      const s = document.createElement("summary"); s.textContent = T("compact.title", (m.originals || []).length);
      const body = document.createElement("div"); body.className = "answer"; body.innerHTML = markdown(m.text || "");
      const orig = document.createElement("details"); orig.className = "orig";
      const os = document.createElement("summary"); os.textContent = T("compact.show");
      const list = document.createElement("div"); list.className = "orig-list";
      (m.originals || []).forEach(o => {
        const p = document.createElement("div"); p.className = `orig-m ${o.role}`;
        const who = document.createElement("b"); who.textContent = o.role === "user" ? "You" : o.role === "summary" ? "Summary" : "Model";
        const t = document.createElement("div"); t.textContent = o.text || "";
        p.append(who, t); list.append(p);
      });
      orig.append(os, list);
      const undo = document.createElement("button"); undo.type = "button"; undo.className = "btn ghost undo"; undo.textContent = T("compact.undo");
      d.append(s, body, orig, undo);
      el.append(d);
      return el;
    }
    if (m.role === "user") {
      if (m.images && m.images.length) {
        const wrap = document.createElement("div"); wrap.className = "imgs";
        m.images.forEach(im => {
          if (im.url) { const img = document.createElement("img"); img.src = im.url; img.alt = im.name || "Attached image"; wrap.append(img); }
          else { const c = document.createElement("span"); c.className = "chip"; c.textContent = `Image: ${im.name || "attached"}`; wrap.append(c); }
        });
        el.append(wrap);
      }
      const b = document.createElement("div"); b.className = "bubble"; b.textContent = m.text; el.append(b);
      const meta = document.createElement("div"); meta.className = "meta"; meta.textContent = `You · ${time(m.time)}`; el.append(meta);
    } else {
      el.innerHTML = `<details class="think" hidden><summary><span class="think-t"></span></summary><div class="think-b" tabindex="0" role="region" aria-label="Reasoning"></div></details>` +
        `<div class="answer"></div><div class="meta"><span class="meta-t"></span>` +
        `<button type="button" class="btn ghost copy" hidden>Copy</button>` +
        `<button type="button" class="btn ghost retry" hidden></button></div>`;
      watchThink(el);
      paint(el, m, false);
    }
    return el;
  }
  /* the reasoning box: open while the model thinks and closed once it is done, unless the reader took over by
     opening or closing it or by scrolling inside it; it shows 20 lines and scrolls for the rest */
  function watchThink(el) {
    const det = el.querySelector(".think"), body = el.querySelector(".think-b");
    const touch = () => { det.dataset.touched = "1"; };
    det.querySelector("summary").addEventListener("click", touch);
    const scrolled = () => { if (body.scrollHeight > body.clientHeight) touch(); };
    for (const ev of ["wheel", "touchmove", "pointerdown"]) body.addEventListener(ev, scrolled, { passive: true });
    body.addEventListener("keydown", e => { if (/^(Arrow|Page|Home|End)/.test(e.key)) scrolled(); });
  }
  function paintThink(el, m, streaming) {
    const det = el.querySelector(".think");
    if (!m.reasoning || !chatSettings.showReasoning()) { det.hidden = true; return; }
    det.hidden = false;
    const now = streaming && !m.text;
    el.querySelector(".think-t").textContent = now ? "Thinking…" : m.thinkSecs != null ? `Thought for ${m.thinkSecs.toFixed(1)} s` : "Thinking";
    const body = el.querySelector(".think-b"), shown = body.firstChild ? body.firstChild.length : 0;
    if (!shown || shown > m.reasoning.length) {
      body.textContent = m.reasoning;
      body.scrollTop = body.scrollHeight;
    } else if (shown < m.reasoning.length) {
      const atEnd = body.scrollHeight - body.scrollTop - body.clientHeight < 8;      // only a reader at the end is followed
      body.firstChild.appendData(m.reasoning.slice(shown));
      if (atEnd) body.scrollTop = body.scrollHeight;
    }
    if (now && !det.dataset.auto) { det.dataset.auto = "1"; det.open = true; }
    else if (!now && det.dataset.auto === "1") { det.dataset.auto = "done"; if (!det.dataset.touched) det.open = false; }
  }
  document.addEventListener("basalt:showreasoning", () => {
    msgs.querySelectorAll(".cmsg.assistant").forEach(e => { const m = messages[+e.dataset.i]; if (m) paintThink(e, m, !!busy && busy.el === e); });
  });
  function paint(el, m, streaming) {
    paintThink(el, m, streaming);
    const a = el.querySelector(".answer");
    if (m.error) {
      a.replaceChildren(Object.assign(document.createElement("p"), { className: "err", textContent: `Error: ${m.error}` }));
    } else if (!m.text && streaming) {
      a.innerHTML = `<p class="wait">${m.reasoning ? "Thinking" : "Waiting for the model"}<span class="dots" aria-hidden="true"></span></p>`;
    } else {
      a.innerHTML = markdown(m.text || "");
    }
    el.querySelector(".meta-t").textContent = m.meta || (streaming ? "" : m.stopped ? "Stopped" : "");
    el.querySelector(".copy").hidden = streaming || !m.text;
    const retry = el.querySelector(".retry");
    retry.hidden = streaming || !m.cut;
    if (m.cut && !streaming) el.querySelector(".meta-t").textContent = `${m.meta ? m.meta + " · " : ""}${T("chat.cut")}`;
    retry.textContent = T("chat.retry");
  }
  /* a reply the model ended in the middle of a word: no stop, no length limit, no error. Sent back with the
     conversation it teaches the model to end the next replies the same way, so it is kept on the page but not sent;
     the reader can ask again with it removed */
  const looksCut = m => m.finish === "stop" && !m.error && !m.stopped && /[\p{L}\p{N}]$/u.test((m.text || "").trimEnd());
  function render() {
    msgs.querySelectorAll(".cmsg").forEach(e => e.remove());
    $("#chat-empty").hidden = messages.length > 0;
    messages.forEach((m, i) => msgs.append(msgEl(m, i)));
    scrollDown(true);
  }
  // the messages scroll inside their panel on a wide screen and with the page on a phone
  const scroller = () => (msgs.scrollHeight > msgs.clientHeight + 1 && getComputedStyle(msgs).overflowY !== "visible") ? msgs : document.scrollingElement;
  function scrollDown(force) {
    const s = scroller();
    if (force || s.scrollHeight - s.scrollTop - s.clientHeight < 140) s.scrollTop = s.scrollHeight;
  }
  function save() {
    // images make the store large: if it is full, keep the text and the image names only
    if (!keep(STORE, messages)) keep(STORE, messages.map(m => ({ ...m, images: (m.images || []).map(i => ({ name: i.name })) })));
  }
  msgs.addEventListener("click", e => {
    const u = e.target.closest(".undo");
    if (u && !busy) {                                   // put the original messages back in place of the summary
      const i = +u.closest(".cmsg").dataset.i, s = messages[i];
      if (s && s.role === "summary") {
        messages.splice(i, 1, ...(s.originals || []));
        save(); render(); status(T("compact.undone")); meter();
      }
      return;
    }
    const rt = e.target.closest(".retry");
    if (rt && !busy) {
      const i = +rt.closest(".cmsg").dataset.i, u = messages[i - 1];
      if (u && u.role === "user") {
        messages.splice(i - 1, 2);
        attachments = u.images || [];
        drawAttachments();
        prompt.value = u.text || "";
        render(); save(); send();
      }
      return;
    }
    const b = e.target.closest(".copy");
    if (!b) return;
    const m = messages[+b.closest(".cmsg").dataset.i];
    const done = ok => { b.textContent = ok ? "Copied" : "Copy failed"; setTimeout(() => { b.textContent = "Copy"; }, 1500); };
    if (navigator.clipboard) navigator.clipboard.writeText(m.text).then(() => done(true), () => done(false)); else done(false);
  });

  /* ---- sending ---- */
  // the conversation as API messages, with the system prompt first; `at[k]` is the conversation index of message k
  // (-1 for the system prompt). A compaction summary goes as the user message the server wrote for it.
  function apiMessages() {
    const out = [], at = [];
    const sys = chatSettings.system();
    if (sys) { out.push({ role: "system", content: sys }); at.push(-1); }
    messages.forEach((m, i) => {
      if (busy && busy.msg === m) return;
      if (m.role === "user") {
        const imgs = (m.images || []).filter(i => i.url);
        out.push({ role: "user", content: imgs.length ? [{ type: "text", text: m.text }, ...imgs.map(i => ({ type: "image_url", image_url: { url: i.url } }))] : m.text });
      } else if (m.role === "summary") {
        out.push({ role: "user", content: m.content || m.text || "" });
      } else {
        if (m.error && !m.text) return;                 // a failed reply is not part of the conversation
        if (m.cut) return;                              // nor is one the model ended mid-word: it would teach the next ones to stop short
        out.push({ role: "assistant", content: m.text || "" });
      }
      at.push(i);
    });
    return { out, at };
  }

  /* ---- the context: counted by the server, the meter under the prompt, the overflow policy before a send ---- */
  let meterTimer = 0, meterSeq = 0;
  function showMeter(used, limit) {
    const box = $("#ctx-meter");
    if (!(limit > 0)) { $("#ctx-text").textContent = T("ctx.unknown"); box.setAttribute("aria-valuenow", "0"); $("#ctx-bar").style.width = "0"; return; }
    const pctv = Math.min(100, used / limit * 100);
    $("#ctx-text").textContent = T("ctx.value", nfmt(used), nfmt(limit));
    box.setAttribute("aria-valuenow", pctv.toFixed(0));
    box.setAttribute("aria-valuetext", T("ctx.value", nfmt(used), nfmt(limit)));
    $("#ctx-bar").style.width = pctv.toFixed(1) + "%";
    box.dataset.level = pctv >= 90 ? "full" : pctv >= 70 ? "high" : "";
  }
  async function context(policy, extra) {
    const { out, at } = apiMessages();
    if (!out.length) return { out, at, r: null };
    const sampling = chatSettings.body();
    const body = { messages: out, policy, max_tokens: sampling.max_tokens || 0, threshold: chatSettings.threshold(), ...extra };
    if (effort.value) body.reasoning_effort = effort.value;
    const r = await fetch("api/chat/context", { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(body) });
    if (!r.ok) return { out, at, r: null };
    return { out, at, r: await r.json() };
  }
  function meter() {
    clearTimeout(meterTimer);
    meterTimer = setTimeout(async () => {
      const seq = ++meterSeq;
      if (!messages.length && !chatSettings.system()) { showMeter(0, limitKnown); return; }
      try {
        const { r } = await context("stop");
        if (seq === meterSeq && r) { limitKnown = r.limit; showMeter(r.used, r.limit); }
      } catch (e) { /* the meter waits for the next change */ }
    }, 250);
  }
  let limitKnown = 0;
  document.addEventListener("basalt:chatset", meter);
  effort.addEventListener("change", meter);

  // apply the policy before a send: true to go on, false when the conversation cannot be sent
  async function fitContext(m) {
    const policy = chatSettings.policy();
    let res;
    try { res = await context(policy); } catch (e) { return true; }   // the server says no later if it must
    const r = res.r;
    if (!r) return true;
    limitKnown = r.limit;
    messages.forEach(x => { delete x.outside; });
    if (r.action === "over") {
      m.error = T("ctx.over", nfmt(r.used + r.reserve), nfmt(r.limit));
      showMeter(r.used, r.limit);
      return false;
    }
    if (r.action === "dropped") {
      const kept = new Set(r.keep.map(k => res.at[k]));
      let n = 0;
      messages.forEach((x, i) => { if (x !== m && !kept.has(i)) { x.outside = true; n++; } });
      status(T("ctx.dropped", n));
    } else if (r.action === "compacted") {
      const idx = r.summary.replaces.map(k => res.at[k]).filter(i => i >= 0).sort((a, b) => a - b);
      const originals = idx.map(i => messages[i]).flatMap(x => x.role === "summary" ? (x.originals || [x]) : [x]);
      const entry = { role: "summary", text: r.summary.text, content: r.summary.message.content, originals, time: Date.now() };
      for (let k = idx.length - 1; k >= 0; k--) messages.splice(idx[k], 1);
      messages.splice(idx[0], 0, entry);
    }
    showMeter(r.used, r.limit);
    return true;
  }
  function setBusy(on) {
    $("#stop").hidden = !on;
    $("#send").disabled = on;
    msgs.setAttribute("aria-busy", String(on));
  }
  const status = t => { $("#chat-status").textContent = t; };

  // the conversations mode (agentchat.js) takes the composer when it is on; this file keeps the model-only chat
  const agent = () => (window.agentChat && window.agentChat.active() ? window.agentChat : null);

  async function send() {
    if (prompt.value.startsWith("/") && window.slash) {              // a command is run, not sent; an unknown one stays to be edited
      const done = window.slash.run(prompt.value.trim());
      if (done === "keep") return;
      if (done) { prompt.value = ""; autosize(); return; }
    }
    const a = agent();
    if (a) { const t = prompt.value.trim(); if (t) { prompt.value = ""; autosize(); a.send(t); } return; }
    const text = prompt.value.trim();
    if ((!text && !attachments.length) || busy) return;
    messages.push({ role: "user", text, images: attachments, time: Date.now() });
    attachments = []; drawAttachments();
    prompt.value = ""; autosize();
    const m = { role: "assistant", text: "", reasoning: "", time: Date.now() };
    messages.push(m);
    render();
    let el = msgs.lastElementChild;
    const controller = new AbortController();
    busy = { controller, msg: m, el };
    setBusy(true);
    let first = null, thinkStart = null, usage = null, frame = 0;
    const draw = () => { frame = 0; paint(el, m, true); scrollDown(); };
    if (chatSettings.policy() === "compact") status(T("ctx.compacting"));
    const fits = await fitContext(m);
    if (busy && busy.msg === m) { render(); el = busy.el = msgs.lastElementChild; paint(el, m, true); }
    if (!fits || controller.signal.aborted) {
      if (controller.signal.aborted) m.stopped = true;
      busy = null; setBusy(false); paint(el, m, false);
      status(m.error || "Stopped"); save(); scrollDown();
      return;
    }
    status("The model is answering");
    // a server started with --listen-first may still be loading the model: the request waits for it
    fetch("v1/status", { cache: "no-store", headers: authHeaders() }).then(r => r.ok ? r.json() : null)
      .then(s => { if (s && s.loading && busy && busy.msg === m && !first) status("The model is still loading; the answer starts when it is ready"); })
      .catch(() => {});
    const body = { model: model || undefined, ...chatSettings.body(), stream: true };
    const api = apiMessages();                          // the sliding window leaves out what is marked outside
    body.messages = api.out.filter((x, k) => api.at[k] < 0 || !messages[api.at[k]].outside);
    if (effort.value) body.reasoning_effort = effort.value;
    try {
      const r = await fetch("v1/chat/completions", { method: "POST", signal: controller.signal,
        headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(body) });
      if (!r.ok) {
        let msg = `HTTP ${r.status}`;
        try { msg = (await r.json()).error.message || msg; } catch (e) { /* not JSON */ }
        if (r.status === 401) { msg = "this server needs an API key: enter it above, then send again"; askKey(); }
        throw new Error(msg);
      }
      const reader = r.body.getReader(), dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let nl;
        while ((nl = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line.startsWith("data:")) continue;              // ": keep-alive" while a long prompt is read
          const data = line.slice(5).trim();
          if (data === "[DONE]") continue;
          let j;
          try { j = JSON.parse(data); } catch (e) { continue; }
          if (j.error) throw new Error(j.error.message || "the engine reported an error");
          if (j.usage) usage = j.usage;
          if (j.choices && j.choices[0] && j.choices[0].finish_reason) m.finish = j.choices[0].finish_reason;
          if (j.structured_output) m.structured = j.structured_output;
          const d = (j.choices && j.choices[0] && j.choices[0].delta) || {};
          if (d.reasoning_content) { first = first || performance.now(); thinkStart = thinkStart || performance.now(); m.reasoning += d.reasoning_content; }
          if (d.content) {
            first = first || performance.now();
            if (thinkStart && m.thinkSecs == null) m.thinkSecs = (performance.now() - thinkStart) / 1000;
            m.text += d.content;
          }
          if (!frame) frame = requestAnimationFrame(draw);
        }
      }
    } catch (e) {
      if (e.name === "AbortError") m.stopped = true;
      else m.error = e.message || String(e);
    }
    if (frame) cancelAnimationFrame(frame);
    if (thinkStart && m.thinkSecs == null) m.thinkSecs = (performance.now() - thinkStart) / 1000;
    const n = usage && usage.completion_tokens;
    if (n && first) {
      const secs = (performance.now() - first) / 1000;
      m.meta = `${n.toLocaleString("en-US")} tokens${secs > 0.25 ? ` · ${(n / secs).toFixed(1)} tok/s` : ""}${m.stopped ? " · stopped" : ""}`;
    } else if (m.stopped) m.meta = "Stopped";
    if (m.structured) {                                 // validated JSON: shown as a code block
      m.meta = `${m.meta ? m.meta + " · " : ""}${T("cs.validated", m.structured.attempts)}`;
      try { m.text = "```json\n" + JSON.stringify(JSON.parse(m.text), null, 2) + "\n```"; } catch (e) { /* left as sent */ }
    }
    m.cut = looksCut(m);
    busy = null;
    setBusy(false);
    paint(el, m, false);
    status(m.error ? `Error: ${m.error}` : m.stopped ? "Stopped" : "Answer complete");
    save();
    scrollDown();
    meter();
  }

  $("#composer").onsubmit = e => { e.preventDefault(); send(); };
  $("#stop").onclick = () => { const a = agent(); if (a) a.stop(); else if (busy) busy.controller.abort(); };
  prompt.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
    else if (e.key === "Escape" && busy) busy.controller.abort();
  });
  function autosize() { prompt.style.height = "auto"; prompt.style.height = `${Math.min(prompt.scrollHeight + 2, innerHeight * 0.4)}px`; }
  prompt.addEventListener("input", autosize);
  $("#new-chat").onclick = () => {
    const a = agent();
    if (a) { a.newChat(); prompt.focus(); return; }
    if (busy) busy.controller.abort();
    messages = []; save(); render(); status(T("ch.new")); prompt.focus(); meter();
  };

  /* ---- images (only when the server reads them) ---- */
  function drawAttachments() {
    const box = $("#attachments");
    box.hidden = !attachments.length;
    box.replaceChildren(...attachments.map((a, k) => {
      const d = document.createElement("div"); d.className = "att";
      const img = document.createElement("img"); img.src = a.url; img.alt = "";
      const b = document.createElement("button"); b.type = "button"; b.className = "btn ghost"; b.textContent = "Remove";
      b.setAttribute("aria-label", `Remove ${a.name}`);
      b.onclick = () => { attachments.splice(k, 1); drawAttachments(); prompt.focus(); };
      const n = document.createElement("span"); n.textContent = a.name;
      d.append(img, n, b); return d;
    }));
  }
  function addFiles(files) {
    for (const f of files) {
      if (!f.type.startsWith("image/")) { status(`${f.name}: not an image`); continue; }
      if (f.size > IMAGE_MAX) { status(`${f.name}: larger than 12 MB`); continue; }
      const r = new FileReader();
      r.onload = () => { attachments.push({ name: f.name || "pasted image", url: r.result }); drawAttachments(); };
      r.readAsDataURL(f);
    }
  }
  $("#attach").onclick = () => $("#file").click();
  $("#file").onchange = e => { addFiles([...e.target.files]); e.target.value = ""; };
  prompt.addEventListener("paste", e => {
    if (!images) return;
    const files = [...((e.clipboardData || {}).files || [])].filter(f => f.type.startsWith("image/"));
    if (files.length) { e.preventDefault(); addFiles(files); }
  });

  async function capabilities() {
    const h = await healthReady;
    if (h) { images = !!h.images; model = h.model || ""; }
    try {                                               // the model list says it too, when this client may read it
      const r = await fetch("v1/models", { cache: "no-store", headers: authHeaders() });
      if (r.ok) {
        // the served model: the list holds the folders' other models too ("unloaded")
        const all = (await r.json()).data || [];
        const d = all.find(x => x.id === model) || all.find(x => (x.status || {}).value === "loaded");
        if (d) { model = d.id || model; images = ((d.architecture || {}).input_modalities || []).includes("image"); }
      }
    } catch (e) { /* /health is enough */ }
    $("#attach").hidden = !images && !(window.chatUi && window.chatUi.agentActive && window.chatUi.agentActive());
    $("#chat-model").textContent = model;
    prompt.placeholder = images ? "Message the model, or attach an image" : "Message the model";
  }
  document.addEventListener("basalt:key", () => { capabilities(); meter(); });
  document.addEventListener("basalt:view", e => { if (e.detail === "chat") { scrollDown(true); meter(); } });

  // what the conversations mode shares: the safe Markdown and the reasoning box, so both chats look and behave alike
  window.chatUi = {
    renderMarkdown: (el, text) => { el.innerHTML = markdown(text || ""); },
    watchThink, paintThink, scrollDown, autosize,
    setPrompt: t => { prompt.value = t; autosize(); prompt.focus(); },
  };

  render();
  capabilities().then(meter);
})();
