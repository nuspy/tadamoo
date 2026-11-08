"use strict";
// Runs the pure part of serve/web/plankinds.js and prints [{name, ok, error}] as JSON (read by test_plankinds_page.py).
const path = require("path");
const PK = require(path.join(__dirname, "..", "web", "plankinds.js"));
const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };
const KINDS = [
  { type: "wait_duration", family: "wait", fields: [{ name: "seconds", kind: "number", required: true, unit: "seconds" }] },
  { type: "wait_condition", family: "wait", fields: [{ name: "expr", kind: "expression" }, { name: "describe", kind: "textarea" }, { name: "watch", kind: "names" },
    { name: "timeout_s", kind: "number", unit: "seconds" }, { name: "poll_s", kind: "number", default: 300, unit: "seconds" }, { name: "on_timeout", kind: "choice", choices: ["fail", "skip"], default: "fail" }] },
  { type: "set_var", family: "data", fields: [{ name: "name", kind: "name", required: true }, { name: "value", kind: "json" }, { name: "expr", kind: "expression" }] },
  { type: "loop", family: "flow", fields: [{ name: "while", kind: "expression", required: true }, { name: "max_iter", kind: "integer", default: 1000 }] },
];

t("the fields of a kind come from the table and an unknown kind has none", () => {
  eq(PK.fieldsOf(KINDS, "wait_duration").map(f => f.name), ["seconds"]);
  eq(PK.fieldsOf(KINDS, "teleport"), []);
  eq(PK.defaultsOf(PK.fieldsOf(KINDS, "wait_condition")), { poll_s: 300, on_timeout: "fail" });
});
t("a length of time is shown in the biggest unit that divides it evenly", () => {
  eq(PK.splitDuration(7200), { value: 2, unit: "h" });
  eq(PK.splitDuration(90), { value: 90, unit: "s" });
  eq(PK.splitDuration(86400 * 3), { value: 3, unit: "d" });
  eq(PK.splitDuration(300), { value: 5, unit: "min" });
  eq(PK.splitDuration(0), { value: "", unit: "min" });
  eq(PK.splitDuration("x"), { value: "", unit: "min" });
});
t("a value and a unit are joined back into seconds", () => {
  eq(PK.joinDuration("2", "h"), 7200);
  eq(PK.joinDuration("1.5", "min"), 90);
  eq(PK.joinDuration("10", "s"), 10);
});
t("a time field with a unit becomes seconds and an empty optional field is left out", () => {
  const f = PK.fieldsOf(KINDS, "wait_condition");
  eq(PK.readSpec(f, { describe: "  market up  ", timeout_s: { value: "2", unit: "h" }, poll_s: "", watch: "indice, volume ,", on_timeout: "skip", expr: "" }),
    { spec: { describe: "market up", watch: ["indice", "volume"], timeout_s: 7200, on_timeout: "skip" }, errors: [] });
});
const SNAP = {
  layers: [["a"], ["b", "c"], ["d"]], order: ["a", "b", "c", "d"],
  plan: { nodes: { a: { assignee: "agent" }, b: { assignee: "user" }, c: { assignee: "agent" }, d: { assignee: "agent" } } },
};
const DIMS = { W: 156, H: 56, GX: 54, GY: 28, PAD: 16, clamp: v => Math.max(0, Math.min(5000, v)) };
t("the layouts give every step a place, and left to right is the layers as columns", () => {
  for (const kind of PK.LAYOUTS) eq(Object.keys(PK.arrange(kind, SNAP, DIMS)).sort(), ["a", "b", "c", "d"]);
  const lr = PK.arrange("lr", SNAP, DIMS);
  eq([lr.a.x, lr.b.x, lr.d.x], [16, 226, 436]);
  eq([lr.b.y, lr.c.y], [16, 100]);
});
t("top to bottom swaps the axes, so the layers are rows", () => {
  const tb = PK.arrange("tb", SNAP, DIMS);
  eq(tb.a.y < tb.b.y && tb.b.y < tb.d.y, true);
  eq(tb.b.x < tb.c.x, true);
});
t("the compact layout is a grid in order that is wider than tall for a plan of any size", () => {
  const compact = PK.arrange("compact", SNAP, DIMS);
  eq([compact.a.y, compact.b.y], [16, 16]);
  eq(compact.a.x < compact.b.x, true);
});
t("lanes put each who-does-it in a band of its own, the agent first", () => {
  const lanes = PK.arrange("lanes", SNAP, DIMS);
  eq(lanes.a.y < lanes.b.y, true);                 // b is the person's: a band below the agent's
  eq(lanes.c.y < lanes.b.y, true);
  eq(lanes.b.x, lanes.c.x);                        // the same layer is the same column in every band
});
t("an unknown layout is the usual left to right one", () => {
  eq(PK.arrange("sideways", SNAP, DIMS), PK.arrange("lr", SNAP, DIMS));
});
t("a needed field that is missing or not a number is reported by name", () => {
  eq(PK.readSpec(PK.fieldsOf(KINDS, "wait_duration"), { seconds: "" }).errors, ["seconds"]);
  eq(PK.readSpec(PK.fieldsOf(KINDS, "wait_duration"), { seconds: { value: "abc", unit: "s" } }).errors, ["seconds"]);
  eq(PK.readSpec(PK.fieldsOf(KINDS, "loop"), { while: " ", max_iter: "5" }), { spec: { max_iter: 5 }, errors: ["while"] });
});
t("a value written as json keeps its type and anything else is text", () => {
  const f = PK.fieldsOf(KINDS, "set_var");
  eq(PK.readSpec(f, { name: "x", value: "42" }).spec, { name: "x", value: 42 });
  eq(PK.readSpec(f, { name: "x", value: "[1,2]" }).spec.value, [1, 2]);
  eq(PK.readSpec(f, { name: "x", value: "aperto" }).spec.value, "aperto");
  eq(PK.readSpec(f, { name: "x", value: "\"aperto\"" }).spec.value, "aperto");
});
t("a few words are given for under the title of a step", () => {
  eq(PK.brief("wait_duration", { seconds: 7200 }), "2 h");
  eq(PK.brief("set_var", { name: "x", value: 3 }), "x = 3");
  eq(PK.brief("wait_condition", { describe: "market up" }), "market up");
  eq(PK.brief("action", {}), "");
  eq(PK.brief("log", undefined), "");
});
t("every kind of the table has an icon", () => {
  for (const k of ["action", "sensor", "decision", "merge", "wait_duration", "wait_until", "wait_condition", "loop", "set_var", "get_var", "input", "output", "log"]) {
    if (!PK.ICONS[k]) throw new Error("no icon for " + k);
  }
});
console.log(JSON.stringify(cases.map(([name, fn]) => { try { fn(); return { name, ok: true }; } catch (e) { return { name, ok: false, error: String(e.message || e) }; } })));
