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
  /* a card with one question of single or confirm kind is sent by Enter on the answer that is already chosen; a click only chooses */
  const fast = card => (card.questions || []).length === 1 && ["single", "confirm"].includes(card.questions[0].kind);

  /* the quoted topic of the card, put at the start of the chat message when the person sets the card aside to talk it over */
  function quote(card) {
    const qs = (card.questions || []).map(q => String(q.text || "").replace(/\s+/g, " ").trim()).filter(Boolean);
    if (!qs.length) return "";
    const shown = qs.slice(0, 4).map(t => "> " + (t.length > 200 ? t.slice(0, 199) + "…" : t));
    if (qs.length > shown.length) shown.push("> …");
    return shown.join("\n") + "\n\n";
  }
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));
  /* What a key does inside the card. e: {key, ctrl, alt, meta, shift}. c: {where: "tab" | "option" | "text" | "area", index (the question),
     total, stop (the focused answer), stops (how many answers can take the focus), fast, multi, other (the focused row is the one of their own)}.
     Returns null (the browser keeps the key) or an action: send; question (go to question `to`, `focus` puts the focus on its first answer);
     stop (focus answer `to`); pick (choose or toggle the focused answer; `advance` moves on to the next question, `submit` sends: only a lone question whose focused answer is already chosen);
     stay (the key is taken and does nothing). Left and right move between questions, up and down between answers, Enter picks. */
  function keyAction(e, c) {
    if (e.key === "Enter" && e.ctrl) return { do: "send" };
    if (e.alt || e.meta || e.ctrl) return null;
    const k = e.key, last = c.total - 1, many = c.total > 1;
    if (!c.where || c.where === "area") return null;
    if (c.where === "tab") {
      if (k === "ArrowLeft") return { do: "question", to: clamp(c.index - 1, 0, last) };
      if (k === "ArrowRight") return { do: "question", to: clamp(c.index + 1, 0, last) };
      if (k === "Home") return { do: "question", to: 0 };
      if (k === "End") return { do: "question", to: last };
      if (k === "ArrowDown") return { do: "question", to: c.index, focus: true };
      return null;
    }
    if (k === "ArrowUp" || k === "ArrowDown") return { do: "stop", to: clamp(c.stop + (k === "ArrowDown" ? 1 : -1), 0, Math.max(0, c.stops - 1)) };
    if (c.where === "text") return k === "Enter" && !e.shift && many && c.index < last ? { do: "question", to: c.index + 1, focus: true } : null;
    if (k === "ArrowLeft" || k === "ArrowRight") {
      if (!many) return { do: "stay" };
      return { do: "question", to: clamp(c.index + (k === "ArrowRight" ? 1 : -1), 0, last), focus: true };
    }
    if (k === "Enter" && !e.shift) return { do: "pick", submit: !!c.fast && !c.other && !!c.picked, advance: many && !c.multi && !c.other && c.index < last };
    return null;
  }

  const api = { initial, setPick, setOther, setText, missing, encode, lines, fast, quote, keyAction, PREFIX, MAX_TEXT };
  if (typeof module !== "undefined" && module.exports) { module.exports = api; return; }

  /* ---- the page part ---- */
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  let seq = 0;

  /* Draws the card of block `b` (b.questions, b.intro; b.sel and b.status are kept on it) into `el`.
     ctx: T (translate), send(value) (posts the answer), answered (the card is closed), focusFirst (take the focus now),
     chat(quote) (puts the quoted topic in the message box and takes the focus there). */
  function draw(el, b, ctx) {
    const card = { questions: b.questions || [] };
    b.sel = b.sel || initial(card);
    const done = ctx.answered;
    if (el.dataset.built && !done) return;                // the person is choosing: the card is not drawn again
    el.dataset.built = done ? "" : "1";
    el.dataset.mode = "question"; el.classList.add("aq-card");
    if (done) { el.onkeydown = null; el.replaceChildren(...doneView(b, card, b.sel, ctx.T)); return; }
    const show = () => (b.dismissed ? aside(el, b, ctx, show) : build(el, b, card, ctx, show));
    show();
  }

  /* the card set aside while the person talks it over in the chat; it opens again from here */
  function aside(el, b, ctx, show) {
    const T = ctx.T, note = mk("p", "aask-h", T("ch.ask.aside")), re = mk("button", "btn", T("ch.ask.reopen")), row = mk("div", "aask-b");
    note.setAttribute("role", "status");
    re.type = "button"; re.title = T("ch.ask.reopen.tip");
    re.onclick = () => {
      b.dismissed = false; show();
      const first = el.querySelector(".aq-tab[aria-selected=true], .aq-panel:not([hidden]) input, .aq-panel:not([hidden]) textarea");
      if (first) first.focus();
    };
    row.append(re);
    el.onkeydown = null;
    el.replaceChildren(note, row);
  }

  /* the answers of a panel that can take the focus, in the order of the page */
  const stopsOf = panel => [...panel.querySelectorAll(".aq-row input, .aq-other-text:not([hidden]), .aq-free")];

  function build(el, b, card, ctx, show) {
    const T = ctx.T, sel = b.sel, id = `aq${++seq}`, note = mk("p", "aask-h aq-note"), form = mk("div", "aq-form");
    note.setAttribute("role", "status"); note.setAttribute("aria-live", "polite");
    if (b.intro) form.append(mk("p", "aask-q", b.intro));
    const total = card.questions.length, go = mk("button", "btn primary", T("ch.ask.send")), skip = mk("button", "btn", T("ch.ask.skip")), chat = mk("button", "btn ghost", T("ch.ask.chat"));
    [go, skip, chat].forEach(x => { x.type = "button"; });
    go.title = T("ch.ask.send.tip"); skip.title = T("ch.ask.skip.tip"); chat.title = T("ch.ask.chat.tip");
    const tabs = [], panels = [], tablist = mk("div", "aq-tabs");
    let active = clamp(b.tab || 0, 0, Math.max(0, total - 1));
    if (total > 1) { tablist.setAttribute("role", "tablist"); tablist.setAttribute("aria-label", T("ch.ask.tabs")); form.append(tablist); }
    /* shows question i; `focus` puts the focus on its tab or on its first answer */
    const select = (i, focus) => {
      active = clamp(i, 0, total - 1); b.tab = active;
      panels.forEach((p, k) => { p.hidden = k !== active; });
      tabs.forEach((t, k) => { t.setAttribute("aria-selected", String(k === active)); t.tabIndex = k === active ? 0 : -1; });
      if (focus === "tab" && tabs[active]) tabs[active].focus();
      if (focus === "stop") { const st = stopsOf(panels[active]); const on = st.find(x => x.checked) || st[0]; if (on) on.focus(); }
    };
    const refresh = () => {
      const m = missing(card, sel);
      go.disabled = m !== null;
      const q = card.questions.find(x => x.id === m);
      note.textContent = q ? T("ch.ask.missing", q.text) : "";
      form.querySelectorAll(".aq-other-text").forEach(t => { t.hidden = !sel[t.dataset.q].other; });
      tabs.forEach((t, k) => {
        const qq = card.questions[k], ok = answered(qq, sel[qq.id]);
        t.querySelector(".aq-tm").hidden = !ok;
        t.querySelector(".sr").textContent = ok ? ", " + T("ch.ask.tab.done") : "";
      });
    };
    const submit = status => {
      if (status === "submitted" && missing(card, sel) !== null) return;
      b.status = status;
      ctx.send(encode(card, sel, status), status);
    };
    card.questions.forEach((q, qi) => {
      const panel = mk("div", "aq-panel"), fs = mk("fieldset", "aq"), lg = mk("legend", "aq-lg"), s = sel[q.id], name = `${id}-${q.id}`;
      lg.id = `${name}-l`;
      panel.id = `${name}-p`; panel.hidden = qi !== active; panels.push(panel);
      if (total > 1) {
        const tab = mk("button", "aq-tab"), sr = mk("span", "sr"), mark = mk("span", "aq-tm", "✓");
        tab.type = "button"; tab.id = `${name}-t`; tab.title = q.text;
        tab.setAttribute("role", "tab"); tab.setAttribute("aria-controls", panel.id); tab.setAttribute("aria-selected", String(qi === active)); tab.tabIndex = qi === active ? 0 : -1;
        mark.setAttribute("aria-hidden", "true");
        tab.append(mk("span", "aq-tn", String(qi + 1)), mk("span", "aq-tt", q.text), mark, sr);
        tab.onclick = () => select(qi);
        tabs.push(tab); tablist.append(tab);
        panel.setAttribute("role", "tabpanel"); panel.setAttribute("aria-labelledby", tab.id);
      }
      panel.append(fs); form.append(panel);
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
        return { lab, inp };
      };
      (q.options || []).forEach(o => {
        const label = q.kind === "confirm" ? T(o.id === "yes" ? "ch.ask.yes" : "ch.ask.no") : o.label;
        const rec = q.kind === "confirm" ? q.recommended === (o.id === "yes") && typeof q.recommended === "boolean" : o.recommended === true;
        group.append(row(o.id, label, q.kind === "confirm" ? "" : o.description, rec, s.picked.includes(o.id), on => setPick(card, sel, q.id, o.id, on)).lab);
      });
      if (q.allow_other !== false && q.kind !== "confirm") {
        const r = row("__other", T("ch.ask.other"), "", false, s.other, on => { setOther(card, sel, q.id, on); if (on) { text.hidden = false; text.focus(); } });
        const text = mk("input", "aq-other-text"); text.type = "text"; text.maxLength = MAX_TEXT; text.value = s.text; text.dataset.q = q.id; text.hidden = !s.other;
        text.placeholder = T("ch.ask.other.ph"); text.setAttribute("aria-label", T("ch.ask.other.ph"));
        text.oninput = () => { setText(card, sel, q.id, text.value); r.inp.checked = sel[q.id].other; refresh(); };
        text.onkeydown = e => { if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && total === 1 && missing(card, sel) === null) { e.preventDefault(); submit("submitted"); } };
        group.append(r.lab, text);
      }
      fs.append(group);
    });
    const foot = mk("div", "aask-b aq-foot"), keys = mk("p", "aask-h aq-keys", T(total > 1 ? "ch.ask.keys" : "ch.ask.keys1"));
    foot.setAttribute("role", "group"); foot.setAttribute("aria-label", T("ch.answers"));
    go.onclick = () => submit("submitted"); skip.onclick = () => submit("skipped");
    chat.onclick = () => { b.dismissed = true; show(); if (ctx.chat) ctx.chat(quote(card)); };
    foot.append(go, skip, chat);
    el.onkeydown = e => {
      const t = e.target;
      if (!t || !t.closest) return;
      const tab = t.closest(".aq-tab"), panel = t.closest(".aq-panel");
      const where = tab ? "tab" : t.tagName === "TEXTAREA" ? "area" : t.classList.contains("aq-other-text") ? "text" : t.matches(".aq-row input") ? "option" : "";
      if (/^[1-9]$/.test(e.key) && !e.ctrlKey && !e.altKey && !e.metaKey && where !== "area" && where !== "text" && !(t.tagName === "INPUT" && t.type === "text")) {
        const set = panel || panels[active];
        const target = set && set.querySelectorAll(".aq-row input")[Number(e.key) - 1];
        if (target) { e.preventDefault(); target.click(); target.focus(); }
        return;
      }
      const index = tab ? tabs.indexOf(tab) : Math.max(0, panels.indexOf(panel)), stops = panel ? stopsOf(panel) : [], q = card.questions[index] || {};
      const act = keyAction({ key: e.key, ctrl: e.ctrlKey, alt: e.altKey, meta: e.metaKey, shift: e.shiftKey },
        { where, index, total, stop: stops.indexOf(t), stops: stops.length, fast: fast(card), picked: !!t.checked, multi: q.kind === "multi", other: t.value === "__other" });
      if (!act) return;
      e.preventDefault();
      if (act.do === "send") submit("submitted");
      else if (act.do === "question") select(act.to, act.focus ? "stop" : "tab");
      else if (act.do === "stop") { if (stops[act.to]) stops[act.to].focus(); }
      else if (act.do === "pick") {
        t.click();                                          // a script click never counts as the one-click answer; the card sends it below
        if (act.submit) submit("submitted"); else if (act.advance) select(index + 1, "stop");
      }
    };
    el.replaceChildren(form, note, keys, foot);
    refresh();
    if (ctx.focusFirst) {
      const first = stopsOf(panels[active] || el)[0];
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
