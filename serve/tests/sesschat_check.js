"use strict";
// Runs the pure part of serve/web/sesschat.js and prints [{name, ok, error}] as JSON (read by test_sesschat_page.py).
const path = require("path");
const { bubbles, working, KEEP } = require(path.join(__dirname, "..", "web", "sesschat.js"));
const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };

t("only what the person said and what was answered, in words, is shown", () => {
  eq(bubbles([
    { role: "user", content: "add a check" },
    { role: "assistant", content: "", tool_calls: [{ id: "1" }] },
    { role: "tool", content: "{\"ok\":true}", tool_call_id: "1" },
    { role: "assistant", content: "  Done: a check every 2 hours.  " },
    { role: "system", content: "rules" },
    { role: "user", content: [{ type: "image" }] },
    null,
  ]), [{ role: "user", text: "add a check" }, { role: "assistant", text: "Done: a check every 2 hours." }]);
});
t("only the last few are kept", () => {
  const many = Array.from({ length: 30 }, (_, i) => ({ role: "user", content: "m" + i }));
  const got = bubbles(many);
  eq(got.length, KEEP); eq(got[got.length - 1].text, "m29");
});
t("anything that is not a list gives nothing", () => { eq(bubbles(undefined), []); eq(bubbles({}), []); });
t("the conversation is at work while it is busy, starting or asking", () => {
  eq(["busy", "starting", "asking", "idle", "dormant", "closed", ""].map(working), [true, true, true, false, false, false, false]);
});
console.log(JSON.stringify(cases.map(([name, fn]) => { try { fn(); return { name, ok: true }; } catch (e) { return { name, ok: false, error: String(e.message || e) }; } })));
