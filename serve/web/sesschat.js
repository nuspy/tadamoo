"use strict";
/* A small chat on a conversation of the harness: the person's messages and the answers as bubbles, a box to write, and the wait while it works.
   Used by the Plans and Cron Jobs tabs to change a plan or a job by telling the harness what to change (the editing conversation, /api/agent/editor).
   Everything the server says is put in the page as text. */
(function (root) {
  const KEEP = 12, POLL_MS = 1500;

  /* the bubbles of a saved conversation: what the person said and what was answered, in words; tool traffic and empty turns left out */
  function bubbles(messages) {
    return (Array.isArray(messages) ? messages : [])
      .filter(m => m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string" && m.content.trim())
      .map(m => ({ role: m.role, text: m.content.trim() }))
      .slice(-KEEP);
  }
  const working = state => state === "busy" || state === "starting" || state === "asking";

  const api = { bubbles, working, KEEP };
  root.SessChat = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof document === "undefined" || !document.createElement) return;

  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  async function call(method, path, payload) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(payload ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: payload ? JSON.stringify(payload) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const why = r => r.status === 0 ? T("sc.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`);

  /* opts: { id, kind: "plan" | "cron" | "", target: () => name of what to change, ref: the conversation to follow (default: the editing one), onDone } */
  function mount(host, opts) {
    const S = { sid: "", log: [], busy: false, note: "", timer: 0 };
    const ref = opts.ref || "tab-editor";
    const log = h("div", "sc-log"); log.setAttribute("role", "log"); log.setAttribute("aria-live", "polite");
    const empty = h("p", "sc-empty", T(opts.emptyKey || "sc.empty"));
    const label = h("label", "sr", T(opts.labelKey || "sc.label")); label.htmlFor = opts.id + "-input";
    const input = h("textarea"); input.id = opts.id + "-input"; input.rows = 2; input.placeholder = T(opts.placeholderKey || "sc.ph");
    const send = h("button", "btn primary sc-send", T("sc.send")); send.type = "submit";
    const note = h("p", "setstatus sc-note"); note.setAttribute("role", "status"); note.setAttribute("aria-live", "polite");
    const form = h("form", "sc-form", label, input, send);
    host.replaceChildren(h("div", "sc", log, empty, form, note));

    function paint() {
      log.replaceChildren(...S.log.map(m => h("div", "sc-msg " + m.role, h("div", "sc-t", m.text))));
      empty.hidden = S.log.length > 0;
      send.disabled = S.busy; send.textContent = T(S.busy ? "sc.working" : "sc.send");
      note.textContent = S.note; note.classList.toggle("bad", !!S.note);
      log.scrollTop = log.scrollHeight;
    }
    async function load() {
      if (!S.sid) return;
      const r = await call("GET", `api/agent/sessions/${encodeURIComponent(S.sid)}/history`);
      if (r.ok) S.log = bubbles(r.data.messages);
      paint();
    }
    async function watch() {
      clearTimeout(S.timer);
      const r = await call("GET", `api/agent/sessions/${encodeURIComponent(S.sid)}/status`);
      if (r.ok && working(r.data.state)) { S.timer = setTimeout(watch, POLL_MS); return; }
      S.busy = false;
      await load();
      if (opts.onDone) opts.onDone();
    }
    async function ask(text) {
      S.busy = true; S.note = ""; S.log.push({ role: "user", text }); paint();
      const r = opts.kind
        ? await call("POST", "api/agent/editor", { kind: opts.kind, target: opts.target(), instruction: text })
        : await call("POST", `api/agent/sessions/${encodeURIComponent(S.sid)}/send`, { text });
      if (!r.ok) { S.busy = false; S.note = why(r); S.log.pop(); input.value = text; paint(); return; }
      if (r.data.session) S.sid = r.data.session;
      watch();
    }
    form.addEventListener("submit", e => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text || S.busy) return;
      if (opts.kind && !String(opts.target() || "").trim()) { S.note = T(opts.kind === "plan" ? "sc.pick.plan" : "sc.pick.cron"); paint(); return; }
      input.value = "";
      ask(text);
    });
    input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); } });
    async function start() {
      const r = await call("GET", `api/agent/sessions/${encodeURIComponent(ref)}/status`);
      if (r.ok) { S.sid = r.data.id; S.busy = working(r.data.state); await load(); if (S.busy) watch(); } else paint();
    }
    document.addEventListener("tadamoo:lang", paint);
    paint();
    start();
    return { follow(sid) { S.sid = sid; return load(); }, reload: load };
  }
  api.mount = mount;
})(typeof window !== "undefined" ? window : globalThis);
