"use strict";
// Runs the pure list functions of serve/web/listtools.js and prints [{name, ok, error}] as JSON (read by test_target_page.py).
const path = require("path");
const LT = require(path.join(__dirname, "..", "web", "listtools.js"));
const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };
const at = (text, caret) => { const c = caret === undefined ? text.length : caret; return [text, c, c]; };
const mark = r => (r ? r.text.slice(0, r.start) + "|" + r.text.slice(r.end) : r);       // the result with the caret shown as |

t("parse reads both kinds, any indent, and refuses prose", () => {
  eq(LT.parse("- a").kind, "ul"); eq(LT.parse("* a").mark, "*"); eq(LT.parse("  3) a"), { indent: "  ", kind: "ol", num: 3, delim: ")", body: "a" });
  eq(LT.parse("-a"), null); eq(LT.parse("**bold** text"), null); eq(LT.parse("2024 was good"), null); eq(LT.parse("- ").body, "");
});

t("toggle numbers the selected lines and a second press takes the numbers off", () => {
  const text = "alpha\nbeta\n\ngamma";
  const a = LT.toggle(text, 0, text.length, "ol");
  eq(a.text, "1. alpha\n2. beta\n\n3. gamma");
  eq(LT.toggle(a.text, 0, a.text.length, "ol").text, text);
});

t("toggle turns a numbered list into a bulleted one and back, keeping the indent", () => {
  const text = "1. a\n  2. b";
  const ul = LT.toggle(text, 0, text.length, "ul");
  eq(ul.text, "- a\n  - b");
  eq(LT.toggle(ul.text, 0, ul.text.length, "ol").text, "1. a\n  1. b");
});

t("toggle on a caret alone works on its line and leaves the caret at its end", () => {
  const r = LT.toggle("one\ntwo\nthree", 5, 5, "ul");
  eq(r.text, "one\n- two\nthree"); eq(r.start, 9); eq(r.end, 9);
});

t("toggle on an empty line starts a list there", () => {
  eq(mark(LT.toggle("intro\n", 6, 6, "ol")), "intro\n1. |");
  eq(mark(LT.toggle("", 0, 0, "ul")), "- |");
});

t("a selection ending at the start of a line does not take that line in", () => {
  const text = "a\nb\nc";
  eq(LT.toggle(text, 0, 4, "ol").text, "1. a\n2. b\nc");
});

t("Enter at the end of a numbered line starts the next number", () => eq(mark(LT.enter(...at("1. one"))), "1. one\n2. |"));
t("Enter keeps the same bullet", () => { eq(mark(LT.enter(...at("* one"))), "* one\n* |"); eq(mark(LT.enter(...at("  - one"))), "  - one\n  - |"); });
t("Enter keeps the bracket of a numbered line", () => eq(mark(LT.enter(...at("4) four"))), "4) four\n5) |"));

t("Enter on an empty item leaves the list", () => {
  eq(mark(LT.enter(...at("- one\n- "))), "- one\n|");
  eq(mark(LT.enter(...at("1. one\n2. "))), "1. one\n|");
});

t("Enter on an empty indented item goes one level out first", () => {
  eq(mark(LT.enter(...at("1. one\n  1. "))), "1. one\n2. |");
});

t("Enter in the middle of an item splits it and the numbers after it follow", () => {
  const r = LT.enter("1. ab\n2. c", 4, 4);                  // between a and b
  eq(r.text, "1. a\n2. b\n3. c"); eq(r.start, 8);
  eq(mark(LT.enter(...at("1. a\n2. b\n3. c", 4))), "1. a\n2. |\n3. b\n4. c");
});

t("Enter does nothing special outside a list, inside the marker or with a selection", () => {
  eq(LT.enter(...at("plain text")), null); eq(LT.enter("- one", 1, 1), null); eq(LT.enter("- one", 2, 5), null);
});

t("Tab indents and Shift+Tab outdents list lines, and the inner list starts at 1", () => {
  const text = "1. a\n2. b";
  const r = LT.tab(text, 7, 7, false);
  eq(r.text, "1. a\n  1. b");
  eq(LT.tab(r.text, r.start, r.end, true).text, "1. a\n2. b");
});

t("Tab outdent on a line at the left margin, and Tab outside a list, are left to the browser", () => {
  eq(LT.tab("- a", 1, 1, true), null); eq(LT.tab("plain", 1, 1, false), null); eq(LT.tab("- a\nplain", 0, 9, false), null);
});

t("Tab on several lines moves them all", () => {
  eq(LT.tab("- a\n- b", 0, 7, false).text, "  - a\n  - b");
});

t("renumber fixes a list after an item was removed or inserted by hand", () => {
  eq(LT.renumber("1. a\n3. b\n3. c", 0, 0).text, "1. a\n2. b\n3. c");
  eq(LT.renumberLines(["3. a", "  1. x", "  7. y", "9. b"]), ["3. a", "  1. x", "  2. y", "4. b"]);
});

t("the numbers keep their caret when they grow a digit", () => {
  const lines = Array.from({ length: 10 }, (_, i) => `${i + 1}. item`);
  lines.splice(1, 0, "1. new");
  const text = lines.join("\n"), caret = text.length;
  const r = LT.renumber(text, caret, caret);
  eq(r.text.split("\n").pop(), "11. item"); eq(r.start, r.text.length);
});

t("pasted lines are offered a list only when they look like items", () => {
  eq(LT.lookLikeItems("buy milk\nbuy bread\ncall Anna"), true);
  eq(LT.lookLikeItems("one line only"), false);
  eq(LT.lookLikeItems("- a\n- b"), false);                                            // already a list
  eq(LT.lookLikeItems("function f() {\n  return 1;\n}\n"), false);                    // code
  eq(LT.lookLikeItems("```\na\nb\n```"), false);
  eq(LT.lookLikeItems("x".repeat(300) + "\n" + "y".repeat(300)), false);              // paragraphs
});

t("listify numbers or bullets the pasted lines and replaces old markers", () => {
  eq(LT.listify("a\nb\n\nc", "ol"), "1. a\n2. b\n\n3. c");
  eq(LT.listify("- a\n* b", "ol"), "1. a\n2. b");
  eq(LT.listify("1. a\n2. b", "ul"), "- a\n- b");
});

t("a pasted numbered list goes on from the numbers around it", () => {
  const r = LT.pasted("1. a\n2. b\n", 10, 10, "1. c\n2. d");
  eq(r.text, "1. a\n2. b\n3. c\n4. d");
});

t("a pasted plain text goes in as it is", () => {
  const r = LT.pasted("ab", 1, 1, "x\ny");
  eq(r.text, "ax\nyb"); eq(r.start, 4);
});

const results = cases.map(([name, fn]) => { try { fn(); return { name, ok: true }; } catch (e) { return { name, ok: false, error: String(e.message || e) }; } });
console.log(JSON.stringify(results));
