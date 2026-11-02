"use strict";
/* List tools for text boxes: a numbered list and a bulleted list from the selected lines (two small buttons and Ctrl+Shift+7 / Ctrl+Shift+8),
   Enter at the end of a list line starts the next item, Enter on an empty item leaves the list (one level out first when it is indented),
   Tab and Shift+Tab indent and outdent list lines, and pasted lines can be turned into a list. The text logic is made of pure functions on
   (text, selectionStart, selectionEnd) that return the new text and selection, so it runs the same in a page and under node; the page part
   below only reads keys and writes the result back through the browser's own editing command, which keeps Undo working. */
(function (root) {
  const BULLETS = "-*•";
  const ITEM = /^(\s*)(?:([-*•])|(\d+)([.)]))\s(.*)$/;
  const INDENT = "  ", MAX_INDENT = 12;

  /* ---- lines ---- */
  function parse(line) {                                  // {indent, kind: "ul" | "ol", mark, num, delim, body} for a list line, else null
    const m = ITEM.exec(line);
    if (!m) return null;
    return m[2] ? { indent: m[1], kind: "ul", mark: m[2], body: m[5] } : { indent: m[1], kind: "ol", num: parseInt(m[3], 10), delim: m[4], body: m[5] };
  }
  const markerOf = it => (it.kind === "ul" ? it.mark : it.num + it.delim);
  const lineText = it => it.indent + markerOf(it) + " " + it.body;
  const lineStart = (text, pos) => text.lastIndexOf("\n", pos - 1) + 1;
  const lineEnd = (text, pos) => { const i = text.indexOf("\n", pos); return i < 0 ? text.length : i; };

  /* the lines a selection touches: [from, to) in the text, and the lines themselves */
  function block(text, start, end) {
    const from = lineStart(text, start);
    let to = lineEnd(text, end > start && text[end - 1] === "\n" ? end - 1 : end);
    if (to < from) to = from;
    return { from, to, lines: text.slice(from, to).split("\n") };
  }
  function put(text, from, to, lines, start, end) {       // the text with the block replaced; a caller gives the new selection
    return { text: text.slice(0, from) + lines.join("\n") + text.slice(to), start, end };
  }

  /* ---- numbering ---- */
  function renumberLines(lines) {                          // numbers each indent level on its own: the list starts at the number its first item has, an inner level at 1
    const counter = {};
    return lines.map((line, i) => {
      const it = parse(line);
      if (!it) return line;
      const level = it.indent.length;
      Object.keys(counter).forEach(k => { if (+k > level) delete counter[k]; });
      if (it.kind === "ul") { delete counter[level]; return line; }
      counter[level] = counter[level] === undefined ? (i === 0 ? it.num : 1) : counter[level] + 1;
      return lineText({ ...it, num: counter[level] });
    });
  }
  function runAround(text, pos) {                          // the consecutive list lines around a position: [from, to)
    let from = lineStart(text, pos), to = lineEnd(text, pos);
    while (from > 0) { const s = lineStart(text, from - 1); if (!parse(text.slice(s, from - 1))) break; from = s; }
    while (to < text.length) { const e = lineEnd(text, to + 1); if (!parse(text.slice(to + 1, e))) break; to = e; }
    return { from, to };
  }
  function renumber(text, start, end) {                   // renumbers the list run the caret is in; keeps the caret on the same line and column
    const r = runAround(text, start);
    const before = text.slice(r.from, r.to).split("\n"), after = renumberLines(before);
    if (before.join("\n") === after.join("\n")) return { text, start, end };
    const shift = pos => {
      if (pos <= r.from) return pos;
      if (pos >= r.to) return pos + after.join("\n").length - before.join("\n").length;
      let off = pos - r.from, i = 0;
      while (off > before[i].length) { off -= before[i].length + 1; i++; }
      const it = parse(before[i]), grew = after[i].length - before[i].length;
      const col = off >= (it.indent + markerOf(it) + " ").length ? off + grew : off;      // a caret in the text moves with a longer number
      return r.from + after.slice(0, i).reduce((n, l) => n + l.length + 1, 0) + Math.min(col, after[i].length);
    };
    return put(text, r.from, r.to, after, shift(start), shift(end));
  }

  /* ---- the buttons ---- */
  function toggle(text, start, end, kind) {               // the selected lines become a list of that kind, or lose it when they all are one already
    const b = block(text, start, end);
    const items = b.lines.map(parse), filled = b.lines.map(l => l.trim() !== "");
    const all = b.lines.every((l, i) => !filled[i] || (items[i] && items[i].kind === kind));
    if (b.lines.length === 1 && !filled[0] && !items[0]) {          // an empty line: the caret starts a list
      const mark = kind === "ol" ? "1. " : "- ";
      return { text: text.slice(0, b.from) + b.lines[0] + mark + text.slice(b.to), start: b.from + b.lines[0].length + mark.length, end: b.from + b.lines[0].length + mark.length };
    }
    const counter = {};
    const lines = b.lines.map((l, i) => {
      if (!filled[i]) return l;
      const it = items[i];
      if (all) return it.indent + it.body;
      const indent = it ? it.indent : /^\s*/.exec(l)[0], body = it ? it.body : l.slice(indent.length);
      if (kind === "ul") return indent + "- " + body;
      counter[indent.length] = (counter[indent.length] || 0) + 1;
      return indent + counter[indent.length] + ". " + body;
    });
    const joined = lines.join("\n");
    const out = put(text, b.from, b.to, lines, b.from, b.from + joined.length);
    if (start === end) out.start = out.end = b.from + joined.length;       // a caret goes to the end of the line it was on
    return renumber(out.text, out.start, out.end);
  }

  /* ---- Enter ---- */
  function enter(text, start, end) {                      // the new text when Enter is a list's, null when the browser's own newline is right
    if (start !== end) return null;
    const ls = lineStart(text, start), le = lineEnd(text, start), it = parse(text.slice(ls, le));
    if (!it) return null;
    const prefix = (it.indent + markerOf(it) + " ").length;
    if (start - ls < prefix) return null;                 // the caret is inside the marker
    if (it.body.trim() === "" && start === le) {          // an empty item: one level out, or out of the list
      if (it.indent.length >= INDENT.length) {
        const out = it.indent.slice(INDENT.length);
        const line = lineText({ ...it, indent: out, num: it.num });
        return renumber(text.slice(0, ls) + line + text.slice(le), ls + line.length, ls + line.length);
      }
      return { text: text.slice(0, ls) + text.slice(le), start: ls, end: ls };
    }
    const next = it.kind === "ul" ? it : { ...it, num: it.num + 1 };
    const tail = text.slice(start, le).replace(/^\s+/, "");
    const head = lineText({ ...next, body: "" });
    const at = ls + text.slice(ls, start).replace(/\s+$/, "").length;
    const out = text.slice(0, at) + "\n" + head + tail + text.slice(le);
    const caret = at + 1 + head.length;
    return renumber(out, caret, caret);
  }

  /* ---- Tab ---- */
  function tab(text, start, end, outdent) {               // indent or outdent the list lines the selection touches; null if some line is not a list line
    const b = block(text, start, end);
    if (!b.lines.length || !b.lines.every(l => parse(l))) return null;
    if (outdent && b.lines.every(l => parse(l).indent.length === 0)) return null;    // nothing to outdent: Shift+Tab keeps its other meanings
    if (!outdent && b.lines.some(l => parse(l).indent.length >= MAX_INDENT)) return { text, start, end };
    let first = 0, total = 0;
    const lines = b.lines.map((l, i) => {
      const it = parse(l);
      let indent = it.indent;
      if (outdent) indent = indent.startsWith("\t") ? indent.slice(1) : indent.slice(Math.min(INDENT.length, indent.length));
      else indent = indent + INDENT;
      const out = lineText({ ...it, indent });
      if (i === 0) first = out.length - l.length;
      total += out.length - l.length;
      return out;
    });
    const out = put(text, b.from, b.to, lines, Math.max(b.from, start + first), end + total);
    return renumber(out.text, out.start, out.end);
  }

  /* ---- paste ---- */
  function lookLikeItems(pasted) {                        // several short lines that are not code: worth offering a list for
    const lines = pasted.replace(/\r/g, "").split("\n").filter(l => l.trim());
    if (lines.length < 2 || /```/.test(pasted)) return false;
    if (lines.every(l => parse(l))) return false;         // already a list
    const code = lines.filter(l => /^( {4}|\t)|[;{}]\s*$/.test(l)).length;
    const long = lines.filter(l => l.length > 240).length;
    return code * 2 < lines.length && long * 2 < lines.length;
  }
  function listify(pasted, kind) {                        // every filled line gets a marker (an old marker is replaced); blank lines stay
    const counter = {};
    return pasted.replace(/\r/g, "").split("\n").map(l => {
      if (!l.trim()) return l;
      const it = parse(l), indent = it ? it.indent : /^\s*/.exec(l)[0], body = it ? it.body : l.slice(indent.length);
      if (kind === "ul") return indent + "- " + body;
      counter[indent.length] = (counter[indent.length] || 0) + 1;
      return indent + counter[indent.length] + ". " + body;
    }).join("\n");
  }
  function pasted(text, start, end, chunk) {              // a pasted list continues the numbering around it; other text goes in as it is
    const out = text.slice(0, start) + chunk + text.slice(end);
    const caret = start + chunk.length;
    const lines = chunk.replace(/\r/g, "").split("\n").filter(l => l.trim());
    return lines.length && lines.every(l => parse(l)) ? renumber(out, caret, caret) : { text: out, start: caret, end: caret };
  }

  const api = { parse, toggle, enter, tab, renumber, lookLikeItems, listify, pasted, renumberLines };
  if (typeof module !== "undefined" && module.exports) { module.exports = api; return; }

  /* ---- the page ---- */
  const SVGNS = "http://www.w3.org/2000/svg";
  const ICONS = {
    ol: ["M4 5h2v4", "M4 9h4", "M4 13c0-1 2-1 2 0s-2 2-2 3h3", "M11 6h9", "M11 12h9", "M11 18h9"],
    ul: ["M5 6h.01", "M5 12h.01", "M5 18h.01", "M10 6h10", "M10 12h10", "M10 18h10"],
  };
  function icon(kind) {
    const s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("aria-hidden", "true"); s.setAttribute("focusable", "false"); s.setAttribute("class", "lt-icon");
    ICONS[kind].forEach(d => { const p = document.createElementNS(SVGNS, "path"); p.setAttribute("d", d); s.append(p); });
    return s;
  }

  function write(ta, result) {                            // writes only what changed, as one edit the browser can undo
    const old = ta.value, now = result.text;
    if (old !== now) {
      let a = 0;
      while (a < old.length && a < now.length && old[a] === now[a]) a++;
      let b = 0;
      while (b < old.length - a && b < now.length - a && old[old.length - 1 - b] === now[now.length - 1 - b]) b++;
      const piece = now.slice(a, now.length - b);
      ta.focus();
      ta.setSelectionRange(a, old.length - b);
      let done = false;
      try { done = piece ? document.execCommand("insertText", false, piece) : document.execCommand("delete"); } catch (e) { done = false; }
      if (!done || ta.value !== now) { ta.value = now; ta.dispatchEvent(new Event("input", { bubbles: true })); }
    }
    ta.setSelectionRange(result.start, result.end);
  }

  /* gives a text box the list tools. options.bar(element) places the two buttons; options.offer(info) shows the question after a paste */
  function attach(ta, options) {
    const opts = options || {};
    let escaped = false;
    const apply = r => { if (r) write(ta, r); };
    const sel = () => [ta.selectionStart, ta.selectionEnd];
    function button(kind) {
      const b = document.createElement("button"), name = T(kind === "ol" ? "lt.ol" : "lt.ul"), keys = "Ctrl+Shift+" + (kind === "ol" ? "7" : "8");
      b.type = "button"; b.className = "lt-btn"; b.dataset.kind = kind;
      b.title = `${name} (${keys}): ${T("lt.help")}`;
      b.setAttribute("aria-label", name); b.setAttribute("aria-keyshortcuts", keys.replace("Ctrl", "Control"));
      b.append(icon(kind));
      b.onmousedown = e => e.preventDefault();            // the text box keeps its focus and its selection
      b.onclick = () => { ta.focus(); apply(toggle(ta.value, ...sel(), kind)); };
      return b;
    }
    let bar = null;
    if (opts.bar) {
      bar = document.createElement("span"); bar.className = "lt-bar"; bar.setAttribute("role", "group"); bar.setAttribute("aria-label", T("lt.bar"));
      bar.append(button("ol"), button("ul"));
      opts.bar(bar);
    }
    ta.addEventListener("keydown", e => {
      if (e.isComposing) return;
      if (e.ctrlKey && e.shiftKey && !e.altKey && !e.metaKey && (e.code === "Digit7" || e.code === "Digit8")) {
        e.preventDefault(); e.stopImmediatePropagation(); apply(toggle(ta.value, ...sel(), e.code === "Digit7" ? "ol" : "ul")); return;
      }
      if (e.key === "Escape") { escaped = true; return; }
      const wasEscaped = escaped;
      escaped = false;
      if (e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey) {
        const r = enter(ta.value, ...sel());
        if (r) { e.preventDefault(); e.stopImmediatePropagation(); apply(r); }
      } else if (e.key === "Tab" && !e.ctrlKey && !e.altKey && !e.metaKey && !wasEscaped) {          // Escape, then Tab, leaves the box
        const r = tab(ta.value, ...sel(), e.shiftKey);
        if (r) { e.preventDefault(); e.stopImmediatePropagation(); apply(r); }
      }
    }, true);
    ta.addEventListener("blur", () => { escaped = false; });
    ta.addEventListener("paste", e => {
      const raw = e.clipboardData && e.clipboardData.getData("text/plain");
      if (!raw) return;
      const chunk = raw.replace(/\r\n?/g, "\n"), [s, f] = sel();
      const lines = chunk.split("\n").filter(l => l.trim());
      if (lines.length > 1 && lines.every(l => parse(l))) { e.preventDefault(); apply(pasted(ta.value, s, f, chunk)); return; }       // a list: the numbering goes on
      if (!lookLikeItems(chunk)) return;
      e.preventDefault();
      apply(pasted(ta.value, s, f, chunk));
      if (opts.offer) {
        opts.offer({ convert: kind => {
          const cur = ta.value, now = listify(chunk, kind);
          if (cur.slice(s, s + chunk.length) === chunk) apply({ text: cur.slice(0, s) + now + cur.slice(s + chunk.length), start: s + now.length, end: s + now.length });
        } });
      }
    });
    return { toggle: kind => apply(toggle(ta.value, ...sel(), kind)), bar };
  }

  root.ListTools = { ...api, attach };

  /* the question after a paste of several lines: three plain buttons, gone with the first press, with Escape or when the box is emptied */
  function offerIn(host, ta) {
    return info => {
      const old = host.querySelector(".lt-offer");
      if (old) old.remove();
      const row = document.createElement("div");
      row.className = "lt-offer"; row.setAttribute("role", "group"); row.setAttribute("aria-label", T("lt.offer"));
      const say = document.createElement("span"); say.textContent = T("lt.offer");
      const pick = (label, kind) => {
        const b = document.createElement("button");
        b.type = "button"; b.className = "btn ghost"; b.textContent = label;
        b.onclick = () => { if (kind) info.convert(kind); row.remove(); ta.focus(); };
        return b;
      };
      row.append(say, pick(T("lt.ol"), "ol"), pick(T("lt.ul"), "ul"), pick(T("lt.keep"), ""));
      ta.before(row);
    };
  }

  /* the normal prompt box of the Chat and the Agent tab */
  function boot() {
    const prompt = document.getElementById("prompt"), crow = prompt && prompt.parentElement.querySelector(".crow");
    if (!prompt || !crow) return;
    attach(prompt, { bar: bar => { const after = document.getElementById("attach"); if (after && after.parentElement === crow) after.after(bar); else crow.prepend(bar); }, offer: offerIn(prompt.parentElement, prompt) });
    prompt.addEventListener("input", () => { const row = prompt.parentElement.querySelector(".lt-offer"); if (row && !prompt.value.trim()) row.remove(); });
  }
  root.ListTools.offerIn = offerIn;
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})(typeof window !== "undefined" ? window : globalThis);
