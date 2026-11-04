"use strict";
/* Joining the pieces of a reply the output limit cut (the same two functions as tadamoo/continuation.py, which the tests run against
   this file with one table of cases). A cut reply is sent back up to its last whitespace (cutPoint): the model writes the unfinished
   word again. joinCut puts the whole cut text and the new reply together: no doubled or missing space, one paragraph break at most,
   and none of the words the model repeated. */
const cutText = (() => {
  const WORD_MAX = 48, OVERLAP_MIN = 6, OVERLAP_MAX = 400;
  const isSpace = c => /\s/.test(c);
  const trimEnd = s => s.replace(/\s+$/, "");

  function cutPoint(text) {
    const m = /\s(\S+)$/.exec(text);
    return m && m[1].length <= WORD_MAX ? m.index + 1 : text.length;
  }

  // how many characters at the start of `body` repeat the end of `text`: whole words, except that the last may be the word the cut left unfinished
  function overlap(text, body) {
    const stripped = trimEnd(text), base = stripped.slice(-OVERLAP_MAX), openEnd = stripped.length === text.length;
    for (let k = Math.min(base.length, body.length, OVERLAP_MAX); k >= OVERLAP_MIN; k--) {
      if (base.endsWith(body.slice(0, k)) && (base.length === k || isSpace(base[base.length - k - 1])) && (openEnd || k === body.length || isSpace(body[k]))) return k;
    }
    return 0;
  }

  function joinCut(text, add) {
    if (!add.trim()) return text;
    if (!text) return add;
    const lead = add.slice(0, add.length - add.replace(/^\s+/, "").length), body = add.slice(lead.length);
    const k = overlap(text, body);
    if (k) return body.slice(k).trim() ? trimEnd(text) + body.slice(k) : text;
    const kept = text.slice(0, cutPoint(text)), tail = kept.slice(trimEnd(kept).length);
    if (tail && lead) {
      const newlines = (tail + lead).split("\n").length - 1;
      return trimEnd(kept) + (newlines ? "\n".repeat(Math.min(2, newlines)) : " ") + body;
    }
    return kept + lead + body;
  }
  return { cutPoint, joinCut };
})();
