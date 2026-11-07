"use strict";
/* What a plan or a cron job produced, read as a conversation: the lines of its log and its results as bubbles, in time order, and a box to answer
   or ask the conversation that did the work. Used by the Plans tab (the plan's log, its results and who changed what) and the Cron Jobs tab (a run
   and the conversation it held). Everything the server says is put in the page as text. */
(function (root) {
  const KEEP = 60;

  /* the entries of a plan: its journal lines (what it did by itself and what its log and result steps wrote) and the changes made to it, oldest first.
     `actorName(actor)` gives a person's word for who did it. */
  function planEntries(journal, history, actorName) {
    const rows = [];
    (Array.isArray(journal) ? journal : []).forEach(j => { if (j && j.text) rows.push({ role: "assistant", kind: j.kind || "auto", text: String(j.text), t: Number(j.t) || 0 }); });
    (Array.isArray(history) ? history : []).forEach(x => {
      if (!x || !x.summary || x.type === "auto") return;                      // an "auto" change is already a line of the journal
      const mine = x.actor === "user";
      rows.push({ role: mine ? "user" : "event", kind: x.type || "", text: mine ? String(x.summary) : `${actorName(x.actor)}: ${x.summary}`, t: Number(x.t) || 0 });
    });
    return rows.sort((a, b) => a.t - b.t).slice(-KEEP);
  }
  /* a cron run as entries: the result it left, and the messages of the conversation it held when there are some */
  function runEntries(run, messages) {
    const rows = [];
    const said = (Array.isArray(messages) ? messages : []).filter(m => m && (m.role === "user" || m.role === "assistant") && typeof m.content === "string" && m.content.trim());
    said.forEach(m => rows.push({ role: m.role, kind: "", text: m.content.trim(), t: 0 }));
    if (!said.length && run && run.summary) rows.push({ role: "assistant", kind: run.status || "", text: String(run.summary), t: Number(run.started) || 0 });
    return rows.slice(-KEEP);
  }

  const api = { planEntries, runEntries, KEEP };
  root.RunLog = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof document === "undefined" || !document.createElement) return;

  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const clock = t => (t > 0 ? new Date(t * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "");

  async function call(method, path, payload) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(payload ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: payload ? JSON.stringify(payload) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  /* a message to a conversation by its name or id: "" when it was sent, else why not */
  async function sendToSession(ref, text) {
    const found = await call("GET", `api/agent/sessions/${encodeURIComponent(ref)}/status`);
    if (!found.ok) return found.status === 0 ? T("rl.offline") : T("rl.gone");
    const sent = await call("POST", `api/agent/sessions/${encodeURIComponent(found.data.id)}/send`, { text });
    return sent.ok ? "" : ((sent.data && sent.data.error && sent.data.error.message) || `HTTP ${sent.status}`);
  }
  /* the messages of a conversation, for a run that held one; [] when it is not there any more */
  async function messagesOf(sid) {
    const r = await call("GET", `api/agent/sessions/${encodeURIComponent(sid)}/history`);
    return r.ok ? r.data.messages || [] : [];
  }
  api.sendToSession = sendToSession; api.messagesOf = messagesOf;

  /* o: { id, emptyKey, labelKey, placeholderKey, noReplyKey, send: async text => "" (all right) or a message that says why not, or null when nobody can be asked } */
  function mount(host, o) {
    const S = { entries: [], busy: false, note: "" };
    const log = h("div", "sc-log rl-log"); log.setAttribute("role", "log"); log.setAttribute("aria-live", "polite");
    const empty = h("p", "sc-empty", T(o.emptyKey || "rl.empty"));
    const note = h("p", "setstatus sc-note"); note.setAttribute("role", "status"); note.setAttribute("aria-live", "polite");
    const label = h("label", "sr", T(o.labelKey || "rl.label")); label.htmlFor = o.id + "-input";
    const input = h("textarea"); input.id = o.id + "-input"; input.rows = 2; input.placeholder = T(o.placeholderKey || "rl.ph");
    const send = h("button", "btn primary sc-send", T("rl.send")); send.type = "submit";
    const form = h("form", "sc-form", label, input, send);
    const noReply = h("p", "sc-empty", T(o.noReplyKey || "rl.noreply"));
    host.replaceChildren(h("div", "sc", log, empty, form, noReply, note));

    const canReply = () => o.send !== null && o.send !== undefined;
    function paint() {
      log.replaceChildren(...S.entries.map(m => h("div", `sc-msg ${m.role}${m.kind === "output" ? " rl-output" : ""}`,
        m.t ? h("div", "sc-time", clock(m.t)) : null, h("div", "sc-t", m.text))));
      empty.hidden = S.entries.length > 0;
      form.hidden = !canReply(); noReply.hidden = canReply();
      send.disabled = S.busy; send.textContent = T(S.busy ? "rl.sending" : "rl.send");
      note.textContent = S.note; note.classList.toggle("bad", !!S.note);
      log.scrollTop = log.scrollHeight;
    }
    form.addEventListener("submit", async e => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text || S.busy || !canReply()) return;
      S.busy = true; S.note = ""; paint();
      let why = "";
      try { why = await o.send(text); } catch (err) { why = String((err && err.message) || err); }
      S.busy = false;
      if (why) { S.note = why; input.value = text; } else { input.value = ""; S.entries.push({ role: "user", kind: "", text, t: Date.now() / 1000 }); if (o.after) o.after(); }
      paint();
    });
    input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); } });
    document.addEventListener("tadamoo:lang", paint);
    paint();
    return { set(entries) { S.entries = entries || []; paint(); }, setSend(fn) { o.send = fn; paint(); } };
  }
  api.mount = mount;
})(typeof window !== "undefined" ? window : globalThis);
