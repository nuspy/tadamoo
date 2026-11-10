"use strict";
// Runs the rules of serve/web/diagramfix.js and prints [{name, ok, error}] as JSON (read by test_diagramfix_page.py).
const path = require("path");
const { candidates, kindOf } = require(path.join(__dirname, "..", "web", "diagramfix.js"));
const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };

t("the kind is read from the first line, comments aside", () => {
  eq(kindOf("%% x\nmindmap\n  root"), "mindmap");
  eq(kindOf("flowchart TD\n A-->B"), "flow");
  eq(kindOf("graph LR\n A-->B"), "flow");
  eq(kindOf("sequenceDiagram\n A->>B: x"), "sequence");
  eq(kindOf("pie\n \"a\": 1"), "");
});
t("a mindmap node with an id before a quoted text, or only a quoted text, is bracketed", () => {
  const src = 'mindmap\n  root((R))\n    DMT "a: b"\n      "leaf (x)"';
  eq(candidates(src)[0], 'mindmap\n  root((R))\n    DMT["a: b"]\n      ["leaf (x)"]');
});
t("a flowchart label with parentheses or a colon is quoted, on the node and on the edge", () => {
  const out = candidates("flowchart TD\n  A[Costo (stimato): 10] --> B{Ok? si/no}\n  B -->|si (forse)| C[Fine]")[0];
  eq(out, 'flowchart TD\n  A["Costo (stimato): 10"] --> B{Ok? si/no}\n  B -->|"si (forse)"| C[Fine]');
});
t("a semicolon in the text of a message is written as its code", () => {
  eq(candidates("sequenceDiagram\n  A->>B: chiedi; poi aspetta")[0], "sequenceDiagram\n  A->>B: chiedi#59; poi aspetta");
});
t("a source the rules do not change has no candidate", () => {
  eq(candidates("flowchart TD\n  A[Utente] --> B[Server]"), []);
  eq(candidates("pie\n  \"a\": 1"), []);
  eq(candidates(42), []);
});
t("a line break tag is the last resort and is offered too", () => {
  eq(candidates("pie title a<br/>b\n  \"a\": 1"), ["pie title a b\n  \"a\": 1"]);
});

const results = cases.map(([name, fn]) => { try { fn(); return { name, ok: true, error: "" }; } catch (e) { return { name, ok: false, error: String(e.message || e) }; } });
console.log(JSON.stringify(results));
