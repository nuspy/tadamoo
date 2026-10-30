"use strict";
// Runs the pure functions of serve/web/askcard.js and prints [{name, ok, error}] as JSON (read by test_ask_card_page.py).
const path = require("path");
const AC = require(path.join(__dirname, "..", "web", "askcard.js"));
const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };

const card = () => ({ questions: [
  { id: "q1", text: "Which database?", kind: "single", required: true, allow_other: true, options: [{ id: "o1", label: "PostgreSQL", recommended: true }, { id: "o2", label: "SQLite" }] },
  { id: "q2", text: "Which sections?", kind: "multi", required: false, allow_other: true, options: [{ id: "o1", label: "Intro" }, { id: "o2", label: "End" }, { id: "o3", label: "Glossary" }] },
  { id: "q3", text: "Anything out of scope?", kind: "free", required: false, options: [] },
  { id: "q4", text: "Publish it?", kind: "confirm", required: true, options: [{ id: "yes", label: "Yes" }, { id: "no", label: "No" }] },
] });

t("nothing is chosen at first and the first required question is the one missing", () => {
  const c = card(), s = AC.initial(c);
  eq(AC.missing(c, s), "q1");
  AC.setPick(c, s, "q1", "o1", true);
  eq(AC.missing(c, s), "q4");
  AC.setPick(c, s, "q4", "no", true);
  eq(AC.missing(c, s), null);
});

t("a single keeps one choice and the row of their own replaces it", () => {
  const c = card(), s = AC.initial(c);
  AC.setPick(c, s, "q1", "o1", true); AC.setPick(c, s, "q1", "o2", true);
  eq(s.q1.picked, ["o2"]);
  AC.setOther(c, s, "q1", true);
  eq(s.q1.picked, []); eq(AC.missing(c, s), "q1");                      // chosen but empty is not an answer
  AC.setText(c, s, "q1", "MariaDB");
  eq(AC.encode(c, s, "submitted").includes('"text":"MariaDB"'), true);
  AC.setPick(c, s, "q1", "o1", true);
  eq(s.q1.other, false);
  eq(JSON.parse(AC.encode(c, s, "submitted").slice(AC.PREFIX.length)).answers.q1, { picked: ["o1"], text: "" });
});

t("typing in the row of their own chooses it, for a single and a multi", () => {
  const c = card(), s = AC.initial(c);
  AC.setPick(c, s, "q1", "o1", true);
  AC.setText(c, s, "q1", "mine");
  eq(s.q1, { picked: [], other: true, text: "mine" });
  AC.setText(c, s, "q2", "a glossary");
  eq(s.q2.other, true);
});

t("a multi keeps several choices and an optional question may stay empty", () => {
  const c = card(), s = AC.initial(c);
  AC.setPick(c, s, "q2", "o1", true); AC.setPick(c, s, "q2", "o3", true); AC.setPick(c, s, "q2", "o1", false);
  eq(s.q2.picked, ["o3"]);
  AC.setPick(c, s, "q1", "o1", true); AC.setPick(c, s, "q4", "yes", true);
  eq(AC.missing(c, s), null);
});

t("the answer leaves as ask: and JSON of ids, with the typed text only where it counts", () => {
  const c = card(), s = AC.initial(c);
  AC.setPick(c, s, "q1", "o2", true); AC.setPick(c, s, "q2", "o1", true); AC.setText(c, s, "q2", "x"); AC.setText(c, s, "q3", "free words"); AC.setPick(c, s, "q4", "yes", true);
  const out = AC.encode(c, s, "submitted");
  eq(out.startsWith("ask:"), true);
  eq(JSON.parse(out.slice(4)), { v: 1, status: "submitted", answers: { q1: { picked: ["o2"], text: "" }, q2: { picked: ["o1"], text: "x" }, q3: { picked: [], text: "free words" }, q4: { picked: ["yes"], text: "" } } });
  eq(JSON.parse(AC.encode(c, s, "skipped").slice(4)), { v: 1, status: "skipped", answers: {} });
  eq(JSON.parse(AC.encode(c, s, "chat").slice(4)).status, "chat");
});

t("a text is cut at the limit", () => {
  const c = card(), s = AC.initial(c);
  AC.setText(c, s, "q3", "z".repeat(900));
  eq(s.q3.text.length, AC.MAX_TEXT);
});

t("one question of single or confirm kind is answered by one click, anything else needs Send", () => {
  eq(AC.fast({ questions: [{ id: "q1", kind: "single" }] }), true);
  eq(AC.fast({ questions: [{ id: "q1", kind: "confirm" }] }), true);
  eq(AC.fast({ questions: [{ id: "q1", kind: "multi" }] }), false);
  eq(AC.fast({ questions: [{ id: "q1", kind: "free" }] }), false);
  eq(AC.fast(card()), false);
});

t("the read-only lines name the labels, not the ids", () => {
  const c = card(), s = AC.initial(c);
  AC.setPick(c, s, "q1", "o1", true); AC.setPick(c, s, "q2", "o1", true); AC.setPick(c, s, "q2", "o3", true); AC.setText(c, s, "q2", "more");
  const l = AC.lines(c, s);
  eq(l[0], { text: "Which database?", answer: "PostgreSQL" });
  eq(l[1].answer, "Intro; Glossary; “more”");
  eq(l[2].answer, "");
});

const results = cases.map(([name, fn]) => { try { fn(); return { name, ok: true }; } catch (e) { return { name, ok: false, error: String(e.message || e) }; } });
console.log(JSON.stringify(results));
