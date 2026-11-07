"use strict";
// Runs the decision of serve/web/attention.js and prints [{name, ok, error}] as JSON (read by test_attention_page.py).
const path = require("path");
const { decide } = require(path.join(__dirname, "..", "web", "attention.js"));
const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };

t("nothing has come out yet: nothing flashes", () => {
  eq(decide(null, 0, false), { seen: null, flash: false });
  eq(decide(100, 0, false), { seen: 100, flash: false });
});
t("the first ever look finds nothing new, whatever is already there", () => {
  eq(decide(null, 500, false), { seen: 500, flash: false });
});
t("a result after the last look flashes until the tab is opened", () => {
  eq(decide(100, 200, false), { seen: 100, flash: true });
  eq(decide(100, 200, true), { seen: 200, flash: false });
});
t("a result already seen does not flash again", () => {
  eq(decide(200, 200, false), { seen: 200, flash: false });
  eq(decide(300, 200, false), { seen: 300, flash: false });
});
t("a text for the time is read as a number", () => {
  eq(decide(100, "250.5", false), { seen: 100, flash: true });
});
console.log(JSON.stringify(cases.map(([name, fn]) => { try { fn(); return { name, ok: true }; } catch (e) { return { name, ok: false, error: String(e.message || e) }; } })));
