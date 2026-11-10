"use strict";
/* Mermaid sources the drawing library refuses for a reason that is the same every time, put right by rule: the page draws each candidate and keeps
   the first that works, and asks the model to repair the source only when none does. */
(root => {
  const KINDS = [[/^\s*mindmap\b/i, "mindmap"], [/^\s*(flowchart|graph)\b/i, "flow"], [/^\s*sequenceDiagram\b/i, "sequence"]];
  const kindOf = source => { const first = source.split("\n").find(l => l.trim() && !l.trim().startsWith("%%")) || ""; const k = KINDS.find(([re]) => re.test(first)); return k ? k[1] : ""; };
  const quote = text => `"${text.replace(/"/g, "#quot;")}"`;

  /* a mindmap node is `id[text]`, `id(text)` or plain text: a quoted string, with or without an id before it, is written as `id["text"]` */
  function mindmap(source) {
    const lines = source.split("\n");
    let first = true;
    return lines.map(line => {
      if (!line.trim() || line.trim().startsWith("%%")) return line;
      if (first) { first = false; return line; }                          // the `mindmap` line
      const indent = line.match(/^\s*/)[0], body = line.trim();
      let m = body.match(/^([^\s"'()[\]{}]+)\s+"(.*)"$/);
      if (m) return `${indent}${m[1]}[${quote(m[2])}]`;
      m = body.match(/^"(.*)"$/);
      if (m) return `${indent}[${quote(m[1])}]`;
      return line;
    }).join("\n");
  }

  /* a label with brackets, parentheses, a colon or a quote inside is quoted, on the node and on the edge */
  function flow(source) {
    const needs = t => /[()[\]{}:;,'"]/.test(t) && !/^\s*"[^"]*"\s*$/.test(t);
    return source.split("\n").map(line => {
      if (/^\s*(flowchart|graph|subgraph|classDef|class|style|linkStyle|click|%%)/i.test(line) && !/^\s*subgraph\b/i.test(line)) return line;
      return line
        .replace(/(\b[\w-]+)\[([^\]\n]+)\]/g, (all, id, t) => (needs(t) && !/^[\[(/\\>]/.test(t) ? `${id}[${quote(t.trim())}]` : all))
        .replace(/(\b[\w-]+)\(([^()\n]*[[\]{}:;,'"][^()\n]*)\)/g, (all, id, t) => (/^[\w-]+$/.test(id) && !/^"/.test(t.trim()) ? `${id}(${quote(t.trim())})` : all))
        .replace(/\|([^|\n]+)\|/g, (all, t) => (needs(t) ? `|${quote(t.trim())}|` : all));
    }).join("\n");
  }

  /* a semicolon ends a statement for the parser: in the text of a message it is written as its code */
  function sequence(source) {
    return source.split("\n").map(line => line.replace(/^(\s*[^:\n]*?(?:-?-?>>?|--?[x)])[^:\n]*:)(.*)$/, (all, head, text) => head + text.replace(/;/g, "#59;"))).join("\n");
  }

  const RULES = { mindmap, flow, sequence };

  /* the sources worth drawing instead of `source`, each different from it and from the others, the most careful first */
  function candidates(source) {
    if (typeof source !== "string") return [];
    const out = [], add = s => { if (s && s !== source && !out.includes(s)) out.push(s); };
    const rule = RULES[kindOf(source)];
    if (rule) add(rule(source));
    add(source.replace(/<br\s*\/?>/gi, " "));
    if (rule) add(rule(source.replace(/<br\s*\/?>/gi, " ")));
    return out;
  }

  const api = { candidates, kindOf };
  root.diagramFix = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
