"use strict";
/* Chat on conversations: each conversation is a long-lived agent process on this server (api/agent/sessions). The page
   follows what happens by polling the conversation's events and shows, in order, what the agent wrote, thought and did
   (tools with their result, plans touched), the questions it puts with their buttons, and what it needs from the person.
   Everything the agent or a tool wrote enters the page through text nodes; the Markdown of its answers goes through the
   same safe renderer as the model-only chat. */
(() => {
  const MODE_KEY = "basalt.chatmode", LAST_KEY = "basalt.chat.session";
  const MODES = ["agent", "ask", "direct"];
  const FAST_MS = 350, SLOW_MS = 3000, SHOWN_OUTPUT = 1500;
  const load = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch (e) { return d; } };
  const keep = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* this page only */ } };
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = mk("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };

  let mode = load(MODE_KEY, "agent");
  if (!MODES.includes(mode)) mode = "agent";
  let supported = null;                               // whether this server offers conversations
  let sid = "", after = 0, state = "dormant", rec = null, timer = 0, items = [], current = null, busyNow = false;
  const status = t => { $("#chat-status").textContent = t; };
  const scroller = () => (box.scrollHeight > box.clientHeight + 1 && getComputedStyle(box).overflowY !== "visible") ? box : document.scrollingElement;
  function scrollDown(force) { const s = scroller(); if (force || s.scrollHeight - s.scrollTop - s.clientHeight < 160) s.scrollTop = s.scrollHeight; }

  const box = $("#amsgs"), empty = $("#achat-empty"), list = $("#asessions"), modeSel = $("#chatmode");

  async function api(method, path, body) {
    try {
      const r = await fetch("api/agent/sessions" + path, { method, cache: "no-store",
        headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let j = {};
      try { j = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, json: j };
    } catch (e) { return { ok: false, status: 0, json: {} }; }
  }
  const errText = r => (r.json && r.json.error && r.json.error.message) || `HTTP ${r.status}`;

  /* ---- the mode: conversations (with tools or question only) or the model-only chat of chat.js ---- */
  const active = () => mode !== "direct" && supported !== false;
  function applyMode() {
    const on = active();
    $("#msgs").hidden = on;
    box.hidden = !on;
    list.parentElement.hidden = !on;
    $("#ctx-meter").hidden = on;
    $("#attach").hidden = on || $("#attach").dataset.can !== "1";
    if (on) { $("#chat-hint").textContent = T("ch.hint"); poll(true); } else { clearTimeout(timer); $("#chat-hint").textContent = T("ch.hint.direct"); }
    setControls();
  }
  function setControls() {
    const on = active();
    if (!on) return;
    const asking = state === "asking";
    $("#stop").hidden = !(busyNow || state === "busy" || state === "starting" || asking);
    $("#send").disabled = false;
    $("#prompt").placeholder = asking ? T("ch.hint.asking") : busyNow ? T("ch.ph.busy") : T("ch.ph");
  }
  modeSel.value = mode;
  modeSel.onchange = () => { mode = modeSel.value; keep(MODE_KEY, mode); if (sid) forget(); applyMode(); };

  /* ---- the list of conversations ---- */
  async function refreshList() {
    const r = await api("GET", "");
    if (!r.ok) { supported = r.status === 404 || r.status === 0 ? false : supported; return; }
    supported = true;
    const sel = list.value || sid;
    list.replaceChildren(Object.assign(mk("option", "", T("ch.new")), { value: "" }),
      ...r.json.sessions.filter(s => s.state !== "closed").map(s => Object.assign(mk("option", "", `${s.title || s.id} · ${T("ch.state." + s.state)}`), { value: s.id })));
    list.value = [...list.options].some(o => o.value === sel) ? sel : "";
  }
  list.onchange = () => { if (list.value) openSession(list.value); else newChat(); };

  function forget() { sid = ""; after = 0; state = "dormant"; rec = null; items = []; current = null; Object.keys(byTurn).forEach(k => delete byTurn[k]); box.querySelectorAll(".cmsg, .anote, .ablocked").forEach(e => e.remove()); empty.hidden = false; keep(LAST_KEY, ""); }
  function newChat() { forget(); list.value = ""; status(T("ch.new")); setControls(); }
  async function openSession(id) {
    forget();
    const h = await api("GET", `/${id}/history`);
    if (!h.ok) { status(errText(h)); return; }
    sid = id; keep(LAST_KEY, id); rec = h.json.session; state = rec.state;
    fromHistory(h.json.messages || []);
    if (state === "busy" || state === "starting" || state === "asking") {      // mid-turn: rebuild what is going on from its events
      forget(); sid = id;
      const e = await api("GET", `/${id}/events?after=0`);
      if (e.ok) { e.json.events.forEach(apply); after = e.json.next; state = e.json.state; }
    } else {
      const e = await api("GET", `/${id}/events?after=0`);
      if (e.ok) after = e.json.next;
    }
    syncRecord(); render(); poll(true);
  }

  /* ---- the view model: the person's messages and, for each answer, blocks in the order things happened ---- */
  const byTurn = {};                                  // the answer of each task: tasks can take turns, and come back
  function userItem(text) { const it = { kind: "user", text }; items.push(it); return it; }
  function assistant(turn) {
    if (turn && byTurn[turn]) { current = byTurn[turn]; return current; }
    if (!turn && current && !current.done) return current;
    current = { kind: "assistant", blocks: [], done: false, meta: "", turn };
    if (turn) byTurn[turn] = current;
    items.push(current);
    return current;
  }
  function noteItem(it) { items.push({ kind: "note", ...it }); }
  function textBlock(kind, text, turn) {
    const a = assistant(turn), last = a.blocks[a.blocks.length - 1];
    if (last && last.t === kind) { last.s += text; last.dirty = true; return; }
    if (last && last.t === "reasoning") last.over = true;
    a.blocks.push({ t: kind, s: text, dirty: true });
  }
  function addBlock(b, turn) { const a = assistant(turn); const last = a.blocks[a.blocks.length - 1]; if (last && last.t === "reasoning") last.over = true; a.blocks.push({ ...b, dirty: true }); return a.blocks[a.blocks.length - 1]; }
  const toolBlock = id => { for (const it of items) { const b = (it.blocks || []).find(x => x.t === "tool" && x.id === id); if (b) return b; } return null; };

  function apply(e) {
    switch (e.event) {
      case "user": closeQuestions(); userItem(e.text); break;
      case "accepted": assistant(e.turn); started(e.turn); break;
      case "started": assistant(e.turn); started(e.turn); break;
      case "delta": textBlock(e.kind === "reasoning" ? "reasoning" : "text", e.text || "", e.turn); break;
      case "queued": break;                                                    // the person's own bubble is already there
      case "routed": noteItem({ s: T("ch.routed." + e.how, e.position || 0), route: e.turn, how: e.how }); break;
      case "suspended": noteItem({ s: T("ch.suspended") }); break;
      case "resumed": noteItem({ s: T("ch.resumed") }); assistant(e.turn); break;
      case "cleared": noteItem({ s: T("ch.cleared", e.count) }); break;
      case "dropped": started(e.turn); noteItem({ s: T("ch.dropped") }); break;
      case "agent": {
        const d = e.data || {};
        if (d.type === "assistant") {                                          // the whole message; the pieces came before it
          const a = assistant(e.turn);
          if (!a.blocks.some(b => b.t === "text" || b.t === "reasoning") && (d.content || d.reasoning)) {
            if (d.reasoning) textBlock("reasoning", d.reasoning, e.turn);
            if (d.content) textBlock("text", d.content, e.turn);
          }
        } else if (d.type === "tool_start") {
          if (d.name === "ask_user") break;                                    // its question is shown by the question itself
          addBlock({ t: "tool", id: d.call_id, name: d.name, args: d.arguments || {}, status: "running", output: "" }, e.turn);
        } else if (d.type === "tool_end") {
          if (d.name === "ask_user") break;
          const b = toolBlock(d.call_id) || addBlock({ t: "tool", id: d.call_id, name: d.name, args: {}, status: "running", output: "" }, e.turn);
          b.status = d.status === "completed" ? "ok" : "failed"; b.decision = d.decision; b.output = String(d.output || "").slice(0, SHOWN_OUTPUT); b.dirty = true;
        }
        break;
      }
      case "ask": addBlock({ t: "ask", id: e.id, mode: e.mode, kind: e.kind || (e.facts || {}).kind, question: e.question, options: e.options || [], facts: e.facts || {}, tool: e.tool, args: e.arguments, answered: null }, e.turn); break;
      case "answered": { for (const it of items) { const b = (it.blocks || []).find(x => x.t === "ask" && x.id === e.ask); if (b) { b.answered = e.value; b.dirty = true; } } break; }
      case "retrying": addBlock({ t: "note", s: T("ch.retrying", e.attempt, e.of, Math.round(e.wait_s)) }, e.turn); break;
      case "restarting": addBlock({ t: "note", s: T("ch.restarting", e.attempt, e.of) }); break;
      case "stalled": addBlock({ t: "note", s: T("ch.stalled") }); break;
      case "blocked": addBlock({ t: "blocked", reason: e.reason, detail: e.detail, options: e.options || [] }, e.turn); break;
      case "done": {
        const a = assistant(e.turn); a.done = true; a.stop = e.stop_reason;
        a.meta = e.stop_reason === "cancelled" ? T("ch.cancelled") : e.stop_reason === "asked" ? "" : e.error ? e.error : "";
        a.blocks.forEach(b => { if (b.t === "reasoning") b.over = true; });
        break;
      }
      default: break;
    }
  }
  function started(turn) {                                                      // a waiting request has begun (or was taken out): no more buttons
    items.forEach(it => { if (it.kind === "note" && it.route === turn && !it.over) { it.over = true; it.dirty = true; } });
  }
  function closeQuestions() { items.forEach(it => (it.blocks || []).forEach(b => { if (b.t === "ask" && b.mode === "question" && !b.answered) { b.answered = ""; b.dirty = true; } })); }

  function fromHistory(messages) {
    for (const m of messages) {
      if (m.role === "user") {
        const text = typeof m.content === "string" ? m.content : "";
        if (text.startsWith("[harness]")) { assistant(); addBlock({ t: "note", s: text.replace(/^\[harness\]\s*/, "") }); } else userItem(text);
      } else if (m.role === "assistant") {
        if (m.content) textBlock("text", m.content);
        (m.tool_calls || []).forEach(tc => {
          let args = {}; try { args = JSON.parse((tc.function || {}).arguments || "{}"); } catch (e) { /* shown as is */ }
          if ((tc.function || {}).name === "ask_user") return;
          addBlock({ t: "tool", id: tc.id, name: (tc.function || {}).name, args, status: "ok", output: "" });
        });
        if (current) current.done = true;
      } else if (m.role === "tool") {
        const b = toolBlock(m.tool_call_id) || (items.length ? items.flatMap(i => i.blocks || []).find(x => x.t === "tool" && x.id === m.tool_call_id) : null);
        if (b) { b.output = String(m.content || "").slice(0, SHOWN_OUTPUT); b.dirty = true; }
      }
    }
    current = null;
  }

  /* ---- drawing ---- */
  const planIdOf = b => (b.args && b.args.plan_id) || ((b.output.match(/Plan ([A-Za-z0-9_-]+):/) || [])[1]) || "";
  function openPlan(id) {
    try { sessionStorage.setItem("basalt.openplan", id); } catch (e) { /* the view opens anyway */ }
    location.hash = "plans";
  }
  function askQuestion(b) {                                                    // the harness's question, in the language of the page
    const f = b.facts || {}, key = "ch.q." + b.kind;
    if (b.kind === "plan_create") return T(key, f.title, (f.steps || []).length);
    if (b.kind === "plan_mark") return T(key, f.plan, f.step, f.to);
    if (b.kind === "plan_change") return T(key, f.plan, f.title);
    if (b.kind === "plan_archive") return T(key, f.plan);
    if (b.kind === "permission") return T(key, b.tool, JSON.stringify(b.args || {}).slice(0, 200));
    return b.question;
  }
  const optLabel = (b, o) => (b.mode === "confirm" ? T(o.value === "confirm" ? "ch.yes" : "ch.no") : o.label);

  function makeBlock(b) {
    if (b.t === "reasoning") {
      const d = mk("details", "think"); d.hidden = true;
      d.append(Object.assign(mk("summary"), {}), mk("div", "think-b"));
      d.querySelector("summary").append(mk("span", "think-t"));
      d.querySelector(".think-b").tabIndex = 0; d.querySelector(".think-b").setAttribute("role", "region"); d.querySelector(".think-b").setAttribute("aria-label", T("ch.reasoning"));
      const wrap = mk("div", "ablock"); wrap.append(d); window.chatUi.watchThink(wrap); return wrap;
    }
    if (b.t === "text") return mk("div", "answer");
    if (b.t === "tool") {
      const d = mk("details", "atool"), s = mk("summary"), a = mk("pre", "atool-a"), o = mk("pre", "atool-o");
      s.append(mk("span", "atool-n"), mk("span", "atool-s"));
      d.append(s, a, o); return d;
    }
    if (b.t === "ask") return mk("div", "aask");
    if (b.t === "blocked") return mk("div", "ablocked");
    return mk("div", "anote");
  }
  function paintBlock(b, el, item) {
    if (b.t === "reasoning") { window.chatUi.paintThink(el, { reasoning: b.s, text: b.over || item.done ? "x" : "" }, !(b.over || item.done)); el.hidden = !b.s; return; }
    if (b.t === "text") { window.chatUi.renderMarkdown(el, b.s); return; }
    if (b.t === "note") { el.textContent = b.s; return; }
    if (b.t === "tool") {
      el.dataset.s = b.status;
      el.querySelector(".atool-n").textContent = b.name;
      el.querySelector(".atool-s").textContent = " · " + T("ch.tool." + b.status);
      el.querySelector(".atool-a").textContent = JSON.stringify(b.args, null, 1);
      el.querySelector(".atool-o").textContent = b.output;
      el.querySelector(".atool-o").hidden = !b.output;
      let chip = el.querySelector(".chip");
      const pid = b.name.startsWith("plan_") ? planIdOf(b) : "";
      if (pid && !chip) { chip = btn(T("ch.plan.open"), "ghost chip", () => openPlan(pid)); el.querySelector("summary").after(chip); }
      return;
    }
    if (b.t === "ask") {
      el.dataset.mode = b.mode;
      const q = mk("p", "aask-q", b.mode === "confirm" ? askQuestion(b) : b.question), row = mk("div", "aask-b");
      row.setAttribute("role", "group"); row.setAttribute("aria-label", T("ch.answers"));
      const done = b.answered !== null;
      (b.options || []).forEach(o => {
        const x = btn(optLabel(b, o), b.mode === "confirm" && o.value === "confirm" ? "primary" : "", () => answer(b, o.value));
        x.disabled = done || (b.mode === "question" && busyNow); x.title = b.mode === "question" ? o.value : "";
        if (done && b.answered === o.value) x.classList.add("picked");
        row.append(x);
      });
      el.replaceChildren(q, row);
      if (b.mode === "question" && !done) el.append(mk("p", "aask-h", T("ch.write")));
      if (done && b.mode === "confirm") el.append(mk("p", "aask-h", T("ch.answered", optLabel(b, (b.options || []).find(o => o.value === b.answered) || { label: b.answered, value: b.answered }))));
      return;
    }
    if (b.t === "blocked") {
      const t = T("ch.blocked." + b.reason); const known = t !== "ch.blocked." + b.reason;
      el.replaceChildren(mk("b", "", known ? t : T("ch.blocked.other")), mk("p", "", b.detail || ""));
      const row = mk("div", "aask-b");
      row.append(btn(T("ch.resume"), "primary", () => control("resume")), btn(T("ch.close"), "", () => control("close")));
      if (!b.options.length) row.hidden = true;
      el.append(row);
    }
  }
  function paintNote(it) {
    it.el.replaceChildren(mk("span", "", it.s));
    if (it.route && it.how === "queue" && !it.over) {                         // a request that waits: it can be made urgent or taken out
      it.el.append(btn(T("ch.promote"), "ghost", () => tell("promote", it.route)), btn(T("ch.drop"), "ghost", () => tell("drop", it.route)));
    }
  }
  async function tell(what, turn) {
    const r = await api("POST", `/${sid}/${what}`, { turn });
    if (!r.ok) status(errText(r));
  }
  function render() {
    empty.hidden = items.length > 0;
    items.forEach(it => {
      if (it.kind === "note") {
        if (!it.el) { it.el = mk("div", "anote cmsg"); box.insertBefore(it.el, null); it.dirty = true; }
        if (it.dirty) { paintNote(it); it.dirty = false; }
        return;
      }
      if (!it.el) {
        it.el = mk("article", `cmsg ${it.kind}`);
        if (it.kind === "user") { it.el.append(Object.assign(mk("div", "bubble"), { textContent: it.text })); }
        else { it.body = mk("div", "ablocks"); it.metaEl = mk("div", "meta"); it.el.append(it.body, it.metaEl); }
        box.insertBefore(it.el, null);
      }
      if (it.kind !== "assistant") return;
      it.blocks.forEach(b => {
        if (!b.el) { b.el = makeBlock(b); it.body.append(b.el); b.dirty = true; }
        if (b.dirty || b.t === "reasoning" || b.t === "text") { paintBlock(b, b.el, it); b.dirty = false; }
      });
      it.metaEl.textContent = it.meta;
    });
    scrollDown();
    setControls();
  }

  /* ---- talking to the conversation ---- */
  async function ensure() {
    if (sid) return true;
    const r = await api("POST", "", { mode: "default", ask_only: mode === "ask" });
    if (!r.ok) { status(errText(r)); return false; }
    sid = r.json.session.id; rec = r.json.session; keep(LAST_KEY, sid); after = 0;
    await refreshList(); list.value = sid;
    return true;
  }
  async function send(text) {
    if (!text) return;
    if (!(await ensure())) { window.chatUi.setPrompt(text); return; }
    const r = await api("POST", `/${sid}/send`, { text });
    if (!r.ok) { status(errText(r)); window.chatUi.setPrompt(text); return; }
    state = "busy"; busyNow = true; status(T("ch.working")); setControls(); poll(true);
  }
  async function answer(b, value) {
    if (b.mode === "question") { b.answered = value; b.dirty = true; render(); send(value); return; }     // the button is the person's next message
    const r = await api("POST", `/${sid}/answer`, { ask: b.id, value });
    if (!r.ok) { status(errText(r)); return; }
    b.answered = value; b.dirty = true; state = "busy"; render(); poll(true);
  }
  async function control(what) {
    const r = await api("POST", `/${sid}/${what}`, {});
    if (!r.ok) { status(errText(r)); return; }
    if (what === "close") { newChat(); refreshList(); } else { state = "busy"; busyNow = true; poll(true); }
  }
  async function stop() {
    if (!sid) return;
    const r = await api("POST", `/${sid}/cancel`, {});
    if (!r.ok) status(errText(r)); else status(T("ch.stopping"));
  }

  /* ---- following the conversation ---- */
  function syncRecord() {
    busyNow = state === "busy" || state === "starting" || state === "asking";
    setControls();
  }
  async function poll(now) {
    clearTimeout(timer);
    if (!active() || !sid || document.hidden || !document.getElementById("view-chat") || document.getElementById("view-chat").hidden) {
      if (active()) timer = setTimeout(() => poll(), SLOW_MS);
      return;
    }
    const r = await api("GET", `/${sid}/events?after=${after}`);
    if (r.ok) {
      const before = state;
      r.json.events.forEach(apply);
      after = r.json.next; state = r.json.state;
      syncRecord();
      if (r.json.events.length || before !== state) render();
      if (before !== state && (state === "idle" || state === "dormant")) { status(T("ch.done")); refreshList(); }
    } else if (r.status === 404) { newChat(); }
    timer = setTimeout(() => poll(), busyNow ? FAST_MS : SLOW_MS);
  }
  document.addEventListener("basalt:view", e => { if (e.detail === "chat") { refreshList(); poll(true); } });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(true); });

  window.agentChat = { active, send, stop, newChat };

  applyT(box);                                        // the static texts, in the page's language
  (async () => {
    await refreshList();
    $("#attach").dataset.can = $("#attach").hidden ? "" : "1";
    if (supported === false) { modeSel.querySelectorAll("option").forEach(o => { if (o.value !== "direct") o.disabled = true; }); modeSel.value = "direct"; }
    applyMode();
    const last = load(LAST_KEY, "");
    if (active() && last && [...list.options].some(o => o.value === last)) { list.value = last; openSession(last); }
  })();
})();
