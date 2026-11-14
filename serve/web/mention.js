"use strict";
/* "@" in the message box (Chat and Agentic coding, which share it): a list of the OTHER conversations, ten at a time, newest use first. Letters typed after the "@" filter it by
   the conversation's name and card. A chosen one becomes the text "@name" in the message and a chip above the box that holds the conversation's id (the stable reference);
   when the message is sent the ids of the chips whose text is still in it go with it (`mentions`). Nothing is delivered to the other conversation here: a mention without
   that feature is simply kept in the message. The list works like the "/" menu of slash.js: Up and Down move, Tab, Enter or Right choose, Escape closes. Everything the server
   says is put in the page as text. */
(() => {
  const prompt = document.getElementById("prompt"), menu = document.getElementById("mention-menu"), strip = document.getElementById("mention-chips");
  if (!prompt || !menu || !strip) return;
  const PAGE = 10, DELAY = 140;
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  let rows = [], at = 0, next = null, query = "", span = null, serial = 0, timer = 0, loading = false, chips = [];

  const ago = t => { const d = new Date(t * 1000); return !t || isNaN(d) ? "" : d.toLocaleString(LANG, { dateStyle: "short", timeStyle: "short" }); };
  const current = () => (window.agentChat && window.agentChat.current && window.agentChat.current()) || "";

  /* the word being typed: an "@" at the start or after a space, and what follows it up to the caret, with no space in it */
  function typing() {
    const caret = prompt.selectionStart;
    if (caret !== prompt.selectionEnd) return null;
    const before = prompt.value.slice(0, caret), m = /(^|\s)@(\S*)$/.exec(before);
    return m ? { start: caret - m[2].length - 1, end: caret, query: m[2] } : null;
  }

  async function fetchPage(offset) {
    const mine = ++serial;
    loading = true;
    try {
      const r = await fetch(`api/agent/sessions/mentions?limit=${PAGE}&offset=${offset}&q=${encodeURIComponent(query)}&exclude=${encodeURIComponent(current())}`, { cache: "no-store", headers: authHeaders() });
      if (mine !== serial) return null;
      return r.ok ? await r.json() : { sessions: [], next: null };
    } catch (e) { return mine === serial ? { sessions: [], next: null } : null; }
    finally { if (mine === serial) loading = false; }
  }
  async function load(more) {
    const page = await fetchPage(more ? rows.length : 0);
    if (!page || !span) return;
    rows = more ? rows.concat(page.sessions) : page.sessions;
    next = page.next;
    if (!more) at = 0;
    paint(!more);
  }

  function close() {
    span = null; rows = []; next = null; serial++; clearTimeout(timer);
    menu.hidden = true; menu.replaceChildren();
    prompt.setAttribute("aria-controls", "slash-menu"); prompt.setAttribute("aria-expanded", "false"); prompt.removeAttribute("aria-activedescendant");
  }
  function paint(top) {
    menu.replaceChildren(...(rows.length ? rows.map((s, i) => {
      const o = mk("div", "slashitem mn-item" + (i === at ? " on" : ""));
      o.id = "mn-o" + i; o.setAttribute("role", "option"); o.setAttribute("aria-selected", String(i === at));
      o.append(mk("b", "mn-name", "@" + s.name), mk("span", "slashkind", s.folder || T("mn.chat")), mk("span", "slashd mn-sum", s.summary || ""), mk("span", "mn-when", ago(s.updated)));
      o.onpointerdown = e => { e.preventDefault(); at = i; choose(); };
      return o;
    }) : [mk("div", "mn-none", loading ? T("mn.loading") : T("mn.none"))]));
    menu.hidden = false;
    prompt.setAttribute("aria-controls", "mention-menu"); prompt.setAttribute("aria-expanded", "true");
    const on = menu.children[at];
    if (on && rows.length) { prompt.setAttribute("aria-activedescendant", on.id); on.scrollIntoView({ block: "nearest" }); } else prompt.removeAttribute("aria-activedescendant");
    if (top) menu.scrollTop = 0;
  }

  function update() {
    const t = typing();
    if (!t) { if (span) close(); return; }
    const first = !span, changed = first || t.query !== query;
    span = t; query = t.query;
    if (!changed) return;
    clearTimeout(timer);
    if (first) { rows = []; at = 0; loading = true; paint(); load(false); }                  // the first look is at once; the next letters wait a moment for the last one
    else timer = setTimeout(() => load(false), DELAY);
  }

  /* the choice: "@letters" becomes "@name " and a chip holds the id */
  function choose() {
    const s = rows[at];
    if (!s || !span) return;
    const token = "@" + s.name, text = prompt.value, after = text.slice(span.end), insert = token + (after.startsWith(" ") ? "" : " ");
    prompt.value = text.slice(0, span.start) + insert + after;
    const caret = span.start + insert.length;
    close();
    prompt.setSelectionRange(caret, caret);
    if (!chips.some(c => c.id === s.id)) chips.push({ id: s.id, name: s.name, token });
    else chips.find(c => c.id === s.id).token = token;
    drawChips();
    if (window.chatUi && window.chatUi.autosize) window.chatUi.autosize();
    prompt.focus();
  }

  function drawChips() {
    strip.hidden = !chips.length;
    strip.replaceChildren(...chips.map(c => {
      const chip = mk("span", "mn-chip"), b = mk("button", "mn-x", "×");
      b.type = "button"; b.setAttribute("aria-label", T("mn.remove", c.name));
      b.onclick = () => { chips = chips.filter(x => x !== c); prompt.value = prompt.value.split(c.token).join("").replace(/ {2,}/g, " "); drawChips(); prompt.focus(); };
      chip.append(mk("span", "mn-chip-t", "@" + c.name), b);
      chip.setAttribute("role", "listitem");
      return chip;
    }));
  }

  prompt.addEventListener("input", update);
  prompt.addEventListener("click", update);
  prompt.addEventListener("keyup", e => { if (["ArrowLeft", "Home", "End"].includes(e.key)) update(); });
  prompt.addEventListener("blur", () => { if (span) close(); });
  prompt.addEventListener("keydown", e => {
    if (menu.hidden || e.isComposing) return;
    const key = e.key, stop = () => { e.preventDefault(); e.stopImmediatePropagation(); };
    if ((key === "ArrowDown" || key === "ArrowUp") && rows.length) {
      stop();
      if (key === "ArrowDown" && at === rows.length - 1 && next !== null && !loading) { load(true).then(() => { if (at < rows.length - 1) { at++; paint(); } }); return; }          // past the last: the next ten
      at = (at + (key === "ArrowDown" ? 1 : rows.length - 1)) % rows.length; paint();
    } else if ((key === "Tab" && !e.shiftKey) || (key === "Enter" && !e.shiftKey) || (key === "ArrowRight" && !e.shiftKey && prompt.selectionStart === prompt.value.length)) {
      if (rows.length) { stop(); choose(); } else close();                                      // nothing matches: the key does what it always does (a message that begins with "@" is sent)
    } else if (key === "Escape") { stop(); close(); }
  }, true);
  menu.addEventListener("scroll", () => { if (next !== null && !loading && menu.scrollTop + menu.clientHeight >= menu.scrollHeight - 8) load(true); });

  /* what the page asks when it sends: the conversations still named in the text, and a way to forget them once the message went */
  window.mentionUi = {
    collect: text => chips.filter(c => text.includes(c.token)).map(c => c.id),
    clear: () => { chips = []; drawChips(); },
    chips: () => chips.map(c => ({ ...c })),
  };
  document.addEventListener("tadamoo:lang", () => { if (!menu.hidden) paint(); });
})();
