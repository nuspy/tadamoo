"use strict";
/* The card of questions the agent (or the intake) puts to the person: single choice, multiple choice, free text and yes/no, a recommended
   option first, and always a row of their own. The logic is made of pure functions on (card, selection) so it runs the same in a page and
   under node; the part below draws the card with real radio buttons, checkboxes and a text area (keyboard and screen reader for free).
   Everything the agent wrote is put in the page as text nodes. The answer leaves as "ask:" + JSON of option ids and typed text; the server
   checks it against the card it stored and puts it in words for the model. */
(function (root) {
  const PREFIX = "ask:", MAX_TEXT = 500;

  /* ---- the selection: {questionId: {picked: [optionId], other: bool, text: string}} ---- */
  function initial(card) {
    const sel = {};
    (card.questions || []).forEach(q => { sel[q.id] = { picked: [], other: false, text: "" }; });
    return sel;
  }
  function setPick(card, sel, qid, oid, on) {
    const q = (card.questions || []).find(x => x.id === qid), s = sel[qid];
    if (!q || !s) return sel;
    if (q.kind === "multi") {
      s.picked = on ? [...new Set([...s.picked, oid])] : s.picked.filter(x => x !== oid);
    } else {                                              // single and confirm: one choice, which also takes the row of their own away
      s.picked = on ? [oid] : []; s.other = false;
    }
    return sel;
  }
  function setOther(card, sel, qid, on) {
    const q = (card.questions || []).find(x => x.id === qid), s = sel[qid];
    if (!q || !s) return sel;
    s.other = !!on;
    if (on && q.kind === "single") s.picked = [];
    if (!on) s.text = "";
    return sel;
  }
  function setText(card, sel, qid, text) {
    const q = (card.questions || []).find(x => x.id === qid), s = sel[qid];
    if (!q || !s) return sel;
    s.text = String(text).slice(0, MAX_TEXT);
    if (q.kind !== "free") {                              // typing in the row of their own chooses it
      s.other = s.text !== "" || s.other;
      if (s.text !== "" && q.kind === "single") s.picked = [];
    }
    return sel;
  }
  const typed = (q, s) => (q.kind === "free" || s.other ? s.text.trim() : "");
  const answered = (q, s) => s.picked.length > 0 || typed(q, s) !== "";
  /* the first question that still needs an answer, or null */
  function missing(card, sel) {
    for (const q of card.questions || []) {
      if (q.required !== false && !answered(q, sel[q.id])) return q.id;
    }
    return null;
  }
  /* the answer as it is sent */
  function encode(card, sel, status) {
    const answers = {};
    if (status === "submitted") {
      (card.questions || []).forEach(q => {
        const s = sel[q.id];
        answers[q.id] = { picked: s.picked.slice(), text: typed(q, s) };
      });
    }
    return PREFIX + JSON.stringify({ v: 1, status, answers });
  }
  /* one line for each question, for the read-only card after the answer */
  function lines(card, sel) {
    return (card.questions || []).map(q => {
      const s = sel[q.id] || { picked: [], text: "" }, byId = Object.fromEntries((q.options || []).map(o => [o.id, o.label])),
        parts = s.picked.map(p => byId[p] || p);
      if (typed(q, s)) parts.push(q.kind === "free" ? typed(q, s) : "“" + typed(q, s) + "”");
      return { text: q.text, answer: parts.join("; ") };
    });
  }
  /* a card with one question of single or confirm kind is answered by one click */
  const fast = card => (card.questions || []).length === 1 && ["single", "confirm"].includes(card.questions[0].kind);

  const api = { initial, setPick, setOther, setText, missing, encode, lines, fast, PREFIX, MAX_TEXT };
  if (typeof module !== "undefined" && module.exports) { module.exports = api; return; }

  /* ---- the page part ---- */
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  let seq = 0;

  /* Draws the card of block `b` (b.questions, b.intro; b.sel and b.status are kept on it) into `el`.
     ctx: T (translate), send(value) (posts the answer), answered (the card is closed), focusFirst (take the focus now). */
  function draw(el, b, ctx) {
    const T = ctx.T, card = { questions: b.questions || [] };
    b.sel = b.sel || initial(card);
    const done = ctx.answered;
    if (el.dataset.built && !done) return;                // the person is choosing: the card is not drawn again
    el.dataset.built = done ? "" : "1";
    const sel = b.sel, id = `aq${++seq}`, note = mk("p", "aask-h aq-note"), form = mk("div", "aq-form");
    note.setAttribute("role", "status"); note.setAttribute("aria-live", "polite");
    el.dataset.mode = "question"; el.classList.add("aq-card");
    if (done) { el.replaceChildren(...doneView(b, card, sel, T)); return; }
    if (b.intro) form.append(mk("p", "aask-q", b.intro));
    const total = card.questions.length, go = mk("button", "btn primary", T("ch.ask.send")), skip = mk("button", "btn", T("ch.ask.skip")), chat = mk("button", "btn ghost", T("ch.ask.chat"));
    [go, skip, chat].forEach(x => { x.type = "button"; });
    go.title = T("ch.ask.send.tip"); skip.title = T("ch.ask.skip.tip"); chat.title = T("ch.ask.chat.tip");
    const refresh = () => {
      const m = missing(card, sel);
      go.disabled = m !== null;
      const q = card.questions.find(x => x.id === m);
      note.textContent = q ? T("ch.ask.missing", q.text) : "";
      form.querySelectorAll(".aq-other-text").forEach(t => { t.hidden = !sel[t.dataset.q].other; });
    };
    const submit = status => {
      if (status === "submitted" && missing(card, sel) !== null) return;
      b.status = status;
      ctx.send(encode(card, sel, status), status);
    };
    card.questions.forEach((q, qi) => {
      const fs = mk("fieldset", "aq"), lg = mk("legend", "aq-lg"), s = sel[q.id], name = `${id}-${q.id}`;
      lg.id = `${name}-l`;
      if (total > 1) lg.append(mk("span", "aq-n", T("ch.ask.n_of_m", qi + 1, total)), " ");
      lg.append(mk("span", "aq-t", q.text));
      if (q.required === false) lg.append(" ", mk("span", "aq-opt-tag", T("ch.ask.optional")));
      fs.append(lg);
      if (q.kind === "free") {
        const ta = mk("textarea", "aq-free"); ta.rows = 2; ta.maxLength = MAX_TEXT; ta.value = s.text; ta.placeholder = q.placeholder || T("ch.ask.free.ph");
        ta.setAttribute("aria-labelledby", lg.id); ta.dataset.q = q.id;
        ta.oninput = () => { setText(card, sel, q.id, ta.value); refresh(); };
        ta.onkeydown = e => { if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && total === 1 && missing(card, sel) === null) { e.preventDefault(); submit("submitted"); } };
        fs.append(ta);
        form.append(fs);
        return;
      }
      const group = mk("div", "aq-opts"), multi = q.kind === "multi";
      group.setAttribute("role", multi ? "group" : "radiogroup"); group.setAttribute("aria-labelledby", lg.id);
      const row = (oid, label, description, recommended, picked, onchange) => {
        const lab = mk("label", "aq-row"), inp = mk("input"), body = mk("span", "aq-body");
        inp.type = multi ? "checkbox" : "radio"; inp.name = name; inp.value = oid; inp.checked = picked;
        body.append(mk("b", "aq-l", label));
        if (recommended) body.append(" ", mk("span", "aq-rec", "★ " + T("ch.ask.recommended")));
        if (description) body.append(mk("span", "aq-d aask-h", description));
        lab.append(inp, body);
        inp.onchange = () => { onchange(inp.checked); refresh(); };
        inp.dataset.q = q.id;
        if (fast(card)) inp.addEventListener("click", ev => { if (ev.detail > 0 && oid !== "__other") { onchange(true); refresh(); submit("submitted"); } });
        return { lab, inp };
      };
      (q.options || []).forEach(o => {
        const label = q.kind === "confirm" ? T(o.id === "yes" ? "ch.ask.yes" : "ch.ask.no") : o.label;
        const rec = q.kind === "confirm" ? q.recommended === (o.id === "yes") && typeof q.recommended === "boolean" : o.recommended === true;
        group.append(row(o.id, label, q.kind === "confirm" ? "" : o.description, rec, s.picked.includes(o.id), on => setPick(card, sel, q.id, o.id, on)).lab);
      });
      if (q.allow_other !== false && q.kind !== "confirm") {
        const r = row("__other", T("ch.ask.other"), "", false, s.other, on => { setOther(card, sel, q.id, on); if (on) text.focus(); });
        const text = mk("input", "aq-other-text"); text.type = "text"; text.maxLength = MAX_TEXT; text.value = s.text; text.dataset.q = q.id; text.hidden = !s.other;
        text.placeholder = T("ch.ask.other.ph"); text.setAttribute("aria-label", T("ch.ask.other.ph"));
        text.oninput = () => { setText(card, sel, q.id, text.value); r.inp.checked = sel[q.id].other; refresh(); };
        text.onkeydown = e => { if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && total === 1 && missing(card, sel) === null) { e.preventDefault(); submit("submitted"); } };
        group.append(r.lab, text);
      }
      fs.append(group);
      form.append(fs);
    });
    const foot = mk("div", "aask-b aq-foot");
    foot.setAttribute("role", "group"); foot.setAttribute("aria-label", T("ch.answers"));
    go.onclick = () => submit("submitted"); skip.onclick = () => submit("skipped"); chat.onclick = () => submit("chat");
    foot.append(go, skip, chat);
    el.onkeydown = e => {
      if (e.key === "Enter" && e.ctrlKey) { e.preventDefault(); submit("submitted"); return; }
      const t = e.target;
      if (/^[1-9]$/.test(e.key) && !e.ctrlKey && !e.altKey && !e.metaKey && t && !(t.tagName === "TEXTAREA" || (t.tagName === "INPUT" && t.type === "text"))) {
        const set = t.closest && t.closest("fieldset.aq");
        const target = set && set.querySelectorAll(".aq-row input")[Number(e.key) - 1];
        if (target) { e.preventDefault(); target.click(); target.focus(); }
      }
    };
    el.replaceChildren(form, note, foot);
    refresh();
    if (ctx.focusFirst) {
      const first = el.querySelector("input, textarea");
      if (first) first.focus({ preventScroll: true });
    }
  }

  function doneView(b, card, sel, T) {
    const out = [];
    if (b.status === "skipped" || b.answered === "skipped") out.push(mk("p", "aask-h", T("ch.ask.skipped")));
    else if (b.status === "chat" || b.answered === "chat") out.push(mk("p", "aask-h", T("ch.ask.in_chat")));
    else if (b.answered === "" && !b.status) out.push(mk("p", "aask-h", T("ch.ask.closed")));
    else {
      out.push(mk("p", "aask-h", T("ch.ask.answered")));
      if (Object.values(sel).some(x => x.picked.length || x.text)) {          // answered on this page; from another device only the fact is known
        const ul = mk("ul", "aq-sum");
        lines(card, sel).forEach(l => ul.append(mk("li", "", l.text + " → " + (l.answer || T("ch.ask.noanswer")))));
        out.push(ul);
      }
    }
    return out;
  }

  root.AskCard = { ...api, draw };
})(typeof window !== "undefined" ? window : globalThis);
