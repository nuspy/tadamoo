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
    $("#attach").hidden = on ? false : $("#attach").dataset.can !== "1";
    const attachName = T(on ? "ch.att.btn" : "ch.att.img");
    $("#attach").setAttribute("aria-label", attachName); $("#attach").title = attachName;
    $("#conv-help").hidden = !on;
    $("#mode-help").textContent = T("ch.mode." + mode + ".d");
    drawPend();
    if (on) { $("#chat-hint").textContent = T("ch.hint"); poll(true); } else { clearTimeout(timer); $("#chat-hint").textContent = T("ch.hint.direct"); }
    setControls();
  }
  function setControls() {
    const on = active();
    if (!on) return;
    const asking = state === "asking";
    const bp = chatSettings.patterns().branches;
    $("#branch-chat").hidden = !(sid && bp && bp.on);
    $("#stop").hidden = !(busyNow || state === "busy" || state === "starting" || asking);
    $("#send").disabled = false;
    $("#prompt").placeholder = asking ? T("ch.hint.asking") : busyNow ? T("ch.ph.busy") : T("ch.ph");
    paintCopy();
  }
  modeSel.value = mode;
  modeSel.onchange = () => { mode = modeSel.value; keep(MODE_KEY, mode); if (sid) forget(); applyMode(); };

  /* ---- the list of conversations ---- */
  async function refreshList() {
    const r = await api("GET", "");
    if (!r.ok) { supported = r.status === 404 || r.status === 0 ? false : supported; return; }
    supported = true;
    const sel = list.value || sid;
    list.replaceChildren(Object.assign(mk("option", "", T("ch.new.opt")), { value: "" }),
      ...r.json.sessions.filter(s => s.state !== "closed").map(s => Object.assign(mk("option", "", `${s.title || s.id} · ${T("ch.state." + s.state)}`), { value: s.id })));
    list.value = [...list.options].some(o => o.value === sel) ? sel : "";
  }
  async function branch() {
    const h = await api("GET", `/${sid}/history`);
    const turn = h.ok ? h.json.messages.filter(m => m.role === "user").length : 0;
    if (!turn) { status(T("ch.branch.none")); return; }
    const r = await api("POST", `/${sid}/branch`, { turn });
    if (!r.ok) { status(errText(r)); return; }
    await refreshList();
    list.value = r.json.session.id;
    await openSession(r.json.session.id);
    status(T("ch.branch.done"));
  }
  $("#branch-chat").onclick = branch;
  document.addEventListener("basalt:chatset", () => setControls());
  list.onchange = () => { if (list.value) openSession(list.value); else newChat(); };

  function forget() { pend = []; sid = ""; after = 0; state = "dormant"; rec = null; items = []; current = null; Object.keys(byTurn).forEach(k => delete byTurn[k]); box.querySelectorAll(".cmsg, .anote, .ablocked").forEach(e => e.remove()); empty.hidden = false; keep(LAST_KEY, ""); copyShown = false; }
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
  function userItem(text, files) { const it = { kind: "user", text, files: files || [] }; items.push(it); return it; }
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
      case "user": closeQuestions(); userItem(e.text, e.attachments); break;
      case "accepted": assistant(e.turn); started(e.turn); break;
      case "started": assistant(e.turn); started(e.turn); break;
      case "woke": noteItem({ s: T("ch.woke", String(e.text || "").replace(/^\[planner\]\s*/, "").slice(0, 200)), route: e.turn }); assistant(e.turn); started(e.turn); break;
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
        } else if (d.type === "verify") {                                        // the answer measured against what was asked
          const list = (d.violations || []).join(" ");
          addBlock({ t: "note", s: d.ok ? T("ch.verify.ok", (d.constraints || []).length) : d.retry ? T("ch.verify.retry", list) : T("ch.verify.unmet", list) }, e.turn);
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
        if (copyShown) loadCopy();
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
        if (text.startsWith("[harness]")) { assistant(); addBlock({ t: "note", s: text.replace(/^\[harness\]\s*/, "") }); } else userItem(text, (m.attachments || []).map(n => ({ name: n })));
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
    if (b.kind === "code") return T(key, f.who);
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
      const made = /^\[file:([a-f0-9]{12}):([^\]]{1,120})\]/.exec(b.output || "");          // written by the tool, never by the model's own text
      let save = el.querySelector(".asave");
      if (made && !save) { save = btn(T("ch.out.save", made[2]), "primary asave", () => saveOutput(made[1], made[2], save)); el.querySelector("summary").after(save); }
      let chip = el.querySelector(".chip");
      const pid = b.name.startsWith("plan_") ? planIdOf(b) : "";
      if (pid && !chip) { chip = btn(T("ch.plan.open"), "ghost chip", () => openPlan(pid)); el.querySelector("summary").after(chip); }
      return;
    }
    if (b.t === "ask" && b.kind === "code") {                                  // a second-factor code: typed here, it goes to the tool and nowhere else
      el.dataset.mode = b.mode;
      const done = b.answered !== null;
      const input = mk("input"); input.type = "password"; input.autocomplete = "one-time-code"; input.inputMode = "numeric"; input.maxLength = 32;
      input.setAttribute("aria-label", T("ch.code.label")); input.disabled = done;
      const go = btn(T("ch.code.send"), "primary", () => { if (input.value.trim()) answer(b, input.value.trim()); }); go.disabled = done;
      input.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); go.click(); } };
      const row = mk("div", "aask-b"); row.append(input, go);
      el.replaceChildren(mk("p", "aask-q", askQuestion(b)), row);
      if (done) el.append(mk("p", "aask-h", T("ch.code.sent")));
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
        if (it.kind === "user") {
          const bubble = mk("div", "bubble");
          if (it.files && it.files.length) bubble.append(mk("div", "achips", it.files.map(f => f.name).join(" · ")));
          bubble.append(mk("div", "", it.text));
          it.el.append(bubble);
          it.el.append(backButton(it));
        }
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

  /* ---- going back: the conversation returns to just after one of the person's requests (what came later is taken off, a copy is kept) ---- */
  function backButton(it) {
    let armed = false, timer = 0;
    const b = btn(T("ch.back"), "ghost aback", async () => {
      if (busyNow || state === "busy" || state === "asking") { status(T("ch.back.busy")); return; }
      if (!armed) { armed = true; b.textContent = T("ch.back.sure"); timer = setTimeout(() => { armed = false; b.textContent = T("ch.back"); }, 5000); return; }
      clearTimeout(timer); armed = false; b.textContent = T("ch.back");
      const turn = items.filter(x => x.kind === "user").indexOf(it) + 1;
      const r = await api("POST", `/${sid}/back`, { turn });
      if (!r.ok) { status(errText(r)); return; }
      const keep = sid; await openSession(keep); status(T("ch.back.done"));
    });
    b.setAttribute("aria-label", T("ch.back.n", it.text.slice(0, 60)));
    return b;
  }

  /* ---- files to attach: uploaded to this conversation as they are picked ---- */
  let pend = [];                                      // [{id, name, family, size} | {name, uploading, pct}]
  const sizeText = n => n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1e3)) + " kB";
  function drawPend() {
    const row = $("#aatt");
    row.hidden = !pend.length || !active();
    row.replaceChildren(...pend.map((f, k) => {
      const d = mk("div", "aatt-i"); d.setAttribute("role", "listitem");
      d.append(mk("span", "aatt-n", f.name), mk("span", "dim", f.uploading ? `${f.pct || 0}%` : f.error ? f.error : `${T("ch.fam." + f.family)} · ${sizeText(f.size)}`));
      const rm = btn(T("ch.att.remove"), "ghost", async () => {
        if (f.id) await api2("POST", `${sid}/${f.id}/remove`);
        pend.splice(pend.indexOf(f), 1); drawPend(); $("#prompt").focus();
      });
      rm.setAttribute("aria-label", T("ch.att.remove.n", f.name));
      d.append(rm); return d;
    }));
    setControls();
  }
  const api2 = async (method, path) => { try { const r = await fetch("api/attachments/" + path, { method, headers: { ...authHeaders(), "Content-Type": "application/json" }, body: method === "POST" ? "{}" : undefined }); return r.ok; } catch (e) { return false; } };
  function upload(file) {
    return new Promise(resolve => {
      const f = { name: file.name || T("ch.att.pasted"), uploading: true, pct: 0 };
      pend.push(f); drawPend();
      const x = new XMLHttpRequest();
      x.open("POST", `api/attachments?session=${encodeURIComponent(sid)}&name=${encodeURIComponent(f.name)}`);
      const h = authHeaders(); Object.keys(h).forEach(k => x.setRequestHeader(k, h[k]));
      x.upload.onprogress = e => { if (e.lengthComputable) { f.pct = Math.round(100 * e.loaded / e.total); drawPend(); } };
      x.onload = () => {
        let j = {}; try { j = JSON.parse(x.responseText); } catch (e) { /* no body */ }
        if (x.status === 200 && j.attachment) Object.assign(f, j.attachment, { uploading: false });
        else { f.uploading = false; f.error = (j.error && j.error.message) || `HTTP ${x.status}`; setTimeout(() => { pend = pend.filter(p => p !== f); drawPend(); }, 4000); }
        drawPend(); resolve();
      };
      x.onerror = () => { f.uploading = false; f.error = T("ch.att.fail"); drawPend(); setTimeout(() => { pend = pend.filter(p => p !== f); drawPend(); }, 4000); resolve(); };
      x.send(file);
    });
  }
  async function attachFiles(files) {
    if (!files.length || !(await ensure())) return;
    for (const f of files) await upload(f);
  }
  document.getElementById("attach").addEventListener("click", e => { if (active()) { e.preventDefault(); e.stopImmediatePropagation(); $("#afile").click(); } }, true);
  $("#afile").onchange = e => { attachFiles([...e.target.files]); e.target.value = ""; };
  const dropZone = document.getElementById("view-chat");
  dropZone.addEventListener("dragover", e => { if (active() && [...(e.dataTransfer || {}).types || []].includes("Files")) e.preventDefault(); });
  dropZone.addEventListener("drop", e => { if (active() && e.dataTransfer && e.dataTransfer.files.length) { e.preventDefault(); attachFiles([...e.dataTransfer.files]); } });

  /* ---- a file the agent made: a button to save it where the person chooses ---- */
  async function saveOutput(id, name, button) {
    button.disabled = true;
    try {
      const r = await fetch(`api/outputs/${encodeURIComponent(sid)}/${encodeURIComponent(id)}`, { headers: authHeaders() });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      const app = window.chrome && window.chrome.webview;
      if (app) {                                              // the desktop app opens "Save as" itself and says how it ended
        const ended = new Promise(done => {
          const timer = setTimeout(() => { app.removeEventListener("message", on); done("failed"); }, 600000);
          const on = e => {
            const m = e.data || {};
            if (m.type !== "download" || m.name !== name) return;
            clearTimeout(timer); app.removeEventListener("message", on); done(m.state);
          };
          app.addEventListener("message", on);
        });
        const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; document.body.append(a); a.click(); a.remove();
        const state = await ended;
        setTimeout(() => URL.revokeObjectURL(a.href), 30000);
        if (state === "completed") status(T("ch.out.saved", name)); else if (state !== "cancelled") status(T("ch.out.fail", name));
      } else if (window.showSaveFilePicker) {                 // the system's "Save as" window, with the file's own name
        const h = await window.showSaveFilePicker({ suggestedName: name });
        const w = await h.createWritable(); await w.write(blob); await w.close();
        status(T("ch.out.saved", name));
      } else {
        const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; document.body.append(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 30000);
        status(T("ch.out.saved", name));
      }
    } catch (e) { if (!(e && e.name === "AbortError")) status(T("ch.out.fail", name)); }
    button.disabled = false;
  }

  /* ---- the conversation's own copy of the folder: what it changed waits here until the person applies it ---- */
  const COPY_KEY = "basalt.chat.copy";
  const copyBar = $("#acopy"), copyText = $("#acopy-text"), copyOpen = $("#acopy-open"), copyPanel = $("#acopy-panel"), copyAsk = $("#acopy-ask"), copyAlways = $("#acopy-always");
  copyAlways.checked = load(COPY_KEY, "") === "always";
  copyAlways.onchange = () => keep(COPY_KEY, copyAlways.checked ? "always" : "");
  const copyMode = () => (copyAlways.checked ? "always" : "auto");
  let copyShown = false;
  const kindText = s => T("wc." + (["added", "deleted"].includes(s) ? s : "modified"));
  function paintCopy() {
    const on = active() && !!(rec && rec.worktree);
    copyAsk.hidden = !!sid;                                   // the choice is made before the first message
    copyBar.hidden = !active() || (!!sid && !on);
    copyText.textContent = on ? T("wc.in.copy") : "";
    copyOpen.hidden = !on;
    if (!on) { copyPanel.hidden = true; copyShown = false; return; }
    copyOpen.textContent = T(copyShown ? "wc.hide" : "wc.review");
    copyOpen.setAttribute("aria-expanded", String(copyShown));
    copyPanel.hidden = !copyShown;
  }
  async function loadCopy(message) {
    if (!sid || !rec || !rec.worktree) return;
    const r = await api("GET", `/${sid}/copy`);
    copyPanel.replaceChildren();
    if (!r.ok) { copyPanel.append(mk("p", "acopy-msg", T("wc.fail", errText(r)))); return; }
    const changes = r.json.changes || [];
    copyOpen.textContent = T(copyShown ? "wc.hide" : "wc.review.n", changes.length);
    if (!changes.length) copyPanel.append(mk("p", "acopy-msg", T("wc.none")));
    const ul = mk("ul", "acopy-files");
    for (const c of changes) {
      const li = mk("li", "acopy-file"), label = mk("label"), cb = mk("input");
      cb.type = "checkbox"; cb.checked = true; cb.value = c.path;
      label.append(cb, mk("span", "", c.path));
      const shown = mk("pre", "acopy-diff"); shown.hidden = true;
      const more = btn(T("wc.show"), "ghost", async () => {
        if (shown.hidden && !shown.textContent) {
          const d = await api("GET", `/${sid}/copy/diff?path=${encodeURIComponent(c.path)}`);
          shown.textContent = d.ok ? (d.json.diff || "") : T("wc.fail", errText(d));
        }
        shown.hidden = !shown.hidden;
        more.textContent = T(shown.hidden ? "wc.show" : "wc.hide.one");
      });
      li.append(label, mk("span", "acopy-kind", kindText(c.status)), more, shown);
      ul.append(li);
    }
    copyPanel.append(ul);
    if (changes.length) {
      const acts = mk("div", "acopy-actions");
      acts.append(btn(T("wc.apply.sel"), "primary", () => applyCopy(false)), btn(T("wc.apply.all"), "", () => applyCopy(true)));
      copyPanel.append(acts);
    }
    const drop = btn(T("wc.discard"), "ghost", () => discardCopy(drop));
    copyPanel.append(drop);
    copyPanel.append(mk("p", "acopy-msg", message || (changes.length ? T("wc.kept") : "")));
    copyPanel.lastChild.setAttribute("role", "status");
  }
  async function applyCopy(all) {
    const paths = all ? null : [...copyPanel.querySelectorAll('.acopy-file input[type="checkbox"]:checked')].map(i => i.value);
    if (!all && !paths.length) { loadCopy(T("wc.pick")); return; }
    const r = await api("POST", `/${sid}/copy/apply`, paths ? { paths } : {});
    if (!r.ok) { loadCopy(r.status === 409 && r.json.error && r.json.error.code === "busy" ? T("wc.busy") : T("wc.fail", errText(r))); return; }
    rec = r.json.session || rec;
    loadCopy(T("wc.applied", (r.json.applied || []).join(", ") || "-"));
  }
  let armed = 0;
  async function discardCopy(button) {
    if (!armed) {                                              // a second press within a few seconds: no dialog to answer, nothing lost by a stray tap
      button.textContent = T("wc.discard.again"); status(T("wc.discard.sure"));
      armed = setTimeout(() => { armed = 0; if (button.isConnected) button.textContent = T("wc.discard"); }, 6000);
      return;
    }
    clearTimeout(armed); armed = 0;
    const r = await api("POST", `/${sid}/copy/discard`, {});
    if (!r.ok) { loadCopy(r.status === 409 ? T("wc.busy") : T("wc.fail", errText(r))); return; }
    rec = r.json.session; copyShown = false; paintCopy(); status(T("wc.discarded")); await refreshList();
  }
  copyOpen.onclick = () => { copyShown = !copyShown; paintCopy(); if (copyShown) loadCopy(); };

  /* ---- talking to the conversation ---- */
  async function ensure() {
    if (sid) return true;
    const r = await api("POST", "", { mode: "default", ask_only: mode === "ask", context_patterns: chatSettings.patterns(), context_policy: chatSettings.policy(), context_threshold: chatSettings.threshold(), end_turn_bias: chatSettings.body().end_turn_bias, worktree: copyMode() });
    if (!r.ok) { status(errText(r)); return false; }
    sid = r.json.session.id; rec = r.json.session; keep(LAST_KEY, sid); after = 0;
    await refreshList(); list.value = sid;
    return true;
  }
  async function send(text) {
    if (!text) return;
    if (!(await ensure())) { window.chatUi.setPrompt(text); return; }
    if (pend.some(f => f.uploading)) { status(T("ch.att.wait")); window.chatUi.setPrompt(text); return; }
    const files = pend.filter(f => f.id);
    const r = await api("POST", `/${sid}/send`, { text, ...(files.length ? { attachments: files.map(f => f.id) } : {}) });
    if (!r.ok) { status(errText(r)); window.chatUi.setPrompt(text); return; }
    pend = []; drawPend();
    state = "busy"; busyNow = true; status(T("ch.working")); setControls(); poll(true);
  }
  async function answer(b, value) {
    if (b.mode === "question") { b.answered = value; b.dirty = true; render(); send(value); return; }     // the button is the person's next message
    const r = await api("POST", `/${sid}/answer`, { ask: b.id, value });
    if (!r.ok) { status(errText(r)); return; }
    b.answered = b.kind === "code" ? "-" : value; b.dirty = true; state = "busy"; render(); poll(true);
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
  window.chatUi.agentActive = active;

  [box, $("#chathelp"), $("#acopy")].forEach(root => applyT(root));   // the static texts, in the page's language
  (async () => {
    await refreshList();
    $("#attach").dataset.can = $("#attach").hidden ? "" : "1";
    if (supported === false) { modeSel.querySelectorAll("option").forEach(o => { if (o.value !== "direct") o.disabled = true; }); modeSel.value = "direct"; }
    applyMode();
    const last = load(LAST_KEY, "");
    if (active() && last && [...list.options].some(o => o.value === last)) { list.value = last; openSession(last); }
  })();
})();
