"use strict";
// Runs serve/web/singleflight.js and prints [{name, ok, error}] as JSON (read by test_single_poll.py).
const path = require("path");
const singleFlight = require(path.join(__dirname, "..", "web", "singleflight.js"));
const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };
const later = ms => new Promise(r => setTimeout(r, ms));

t("a call made while one is running is not started beside it", async () => {
  let live = 0, peak = 0, runs = 0;
  const f = singleFlight(async () => { runs++; live++; peak = Math.max(peak, live); await later(20); live--; });
  await Promise.all([f(), f(), f()]);
  eq(peak, 1);
  eq(runs, 2);                                      // the first, and one more for everything asked meanwhile
});

t("the call that waited runs with the arguments of the last ask", async () => {
  const seen = [];
  const f = singleFlight(async x => { seen.push(x); await later(10); });
  await Promise.all([f("a"), f("b"), f("c")]);
  eq(seen, ["a", "c"]);
});

t("a failure frees the function for the next call", async () => {
  let n = 0;
  const f = singleFlight(async () => { n++; if (n === 1) throw new Error("boom"); });
  await f().catch(() => {});
  await f();
  eq(n, 2);
});

(async () => {
  const results = [];
  for (const [name, fn] of cases) {
    try { await fn(); results.push({ name, ok: true }); } catch (e) { results.push({ name, ok: false, error: String(e.message || e) }); }
  }
  console.log(JSON.stringify(results));
})();
