"use strict";
// Runs the decisions of serve/web/human.js and prints [{name, ok, error}] as JSON (read by test_human_page.py).
const path = require("path");
const { pick, readValue, keyOf, SNOOZE_MS } = require(path.join(__dirname, "..", "web", "human.js"));
const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };
const a = { plan_id: "p", node_id: "a" }, b = { plan_id: "p", node_id: "b" }, c = { plan_id: "q", node_id: "a" };

t("the oldest request comes first", () => { eq(pick([a, b], {}, 0), a); eq(pick([], {}, 0), null); eq(pick(null, {}, 0), null); });
t("a request left for later is skipped until its time, then comes back", () => {
  const later = { [keyOf(a)]: 1000 + SNOOZE_MS };
  eq(pick([a, b], later, 1000), b);
  eq(pick([a], later, 1000), null);
  eq(pick([a, b], later, 1000 + SNOOZE_MS + 1), a);
});
t("two plans may have a step of the same name", () => { eq(keyOf(a) === keyOf(c), false); eq(pick([a, c], { [keyOf(a)]: 9 }, 0), c); });
t("a value is read as text, a number must be one, an empty one is not an answer", () => {
  eq(readValue({ kind: "text" }, "  hello "), { value: "hello" });
  eq(readValue({ kind: "text" }, "   "), { error: "hr.need" });
  eq(readValue({ kind: "number" }, "12.5"), { value: "12.5" });
  eq(readValue({ kind: "number" }, "many"), { error: "hr.need.number" });
  eq(readValue({ kind: "choice" }, null), { error: "hr.need" });
});
const out = [];
for (const [name, fn] of cases) { try { fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message }); } }
console.log(JSON.stringify(out));
