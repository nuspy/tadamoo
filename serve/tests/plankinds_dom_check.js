"use strict";
// Runs the page part of serve/web/plankinds.js (the palette and the form of a kind) on a tiny stand-in for the DOM and prints [{name, ok, error}] as JSON
// (read by test_plankinds_page.py). It drives the palette with clicks and pointer moves and the form with typed values.
class Text { constructor(t) { this.text = String(t); this.nodeType = 3; } }
class El {
  constructor(tag) {
    this.nodeType = 1; this.tagName = tag; this.children = []; this.attrs = {}; this.dataset = {}; this.listeners = {}; this.classes = new Set(); this.style = {}; this._value = ""; this.disabled = false;
    this.classList = { add: c => this.classes.add(c), remove: c => this.classes.delete(c), contains: c => this.classes.has(c), toggle: (c, on) => (on ? this.classes.add(c) : this.classes.delete(c)) };
  }
  get value() { if (this.tagName === "select") { const o = this.children.find(c => c.selected); return o ? o.value : this._value; } return this._value; }
  set value(v) { this._value = String(v); if (this.tagName === "select") this.children.forEach(c => { c.selected = c.value === String(v); }); }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(" "); }
  append(...nodes) { nodes.forEach(n => this.children.push(typeof n === "string" ? new Text(n) : n)); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  fire(type, ev) { (this.listeners[type] || []).forEach(fn => fn(ev)); }
  all() { return this.children.filter(c => c instanceof El).flatMap(c => [c, ...c.all()]); }
  remove() { this.removed = true; }
}
const windowListeners = {};
global.window = global;
global.addEventListener = (t, fn) => { (windowListeners[t] = windowListeners[t] || new Set()).add(fn); };
global.removeEventListener = (t, fn) => { (windowListeners[t] || new Set()).delete(fn); };
const fireWindow = (t, ev) => [...(windowListeners[t] || [])].forEach(fn => fn(ev));
const body = new El("body");
global.document = { createElement: t => new El(t), createElementNS: (ns, t) => new El(t), createTextNode: t => new Text(t), body };
const PK = require(require("path").join(__dirname, "..", "web", "plankinds.js"));

const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };
const T = k => k;
const KINDS = [
  { type: "action", family: "action", fields: [] },
  { type: "wait_duration", family: "wait", fields: [{ name: "seconds", kind: "number", required: true, unit: "seconds" }] },
  { type: "log", family: "io", fields: [{ name: "message", kind: "textarea", required: true }] },
];

function build() {
  const log = { picked: [], dropped: [] };
  const canvas = new El("div");
  let overCanvas = false;
  const box = PK.palette(KINDS, { T, canvas: () => canvas, over: () => overCanvas, pick: k => log.picked.push(k), drop: (k, e) => log.dropped.push([k, e.clientX, e.clientY]) });
  const items = box.all().filter(e => e.tagName === "button");
  return { box, items, log, canvas, over: v => { overCanvas = v; } };
}
const press = (item, x, y, type) => item.fire("pointerdown", { pointerType: type || "mouse", button: 0, clientX: x, clientY: y });

t("every kind is a button with its name, and a tap or Enter picks it", () => {
  const p = build();
  eq(p.items.map(i => i.dataset.kind), ["action", "wait_duration", "log"]);
  eq(p.items[1].attrs["aria-label"].startsWith("pl.type.wait_duration"), true);
  p.items[1].fire("click", {});
  eq(p.log.picked, ["wait_duration"]);
  p.box.dispose();
});
t("a kind dragged and let go over the plan is dropped there, and not picked", () => {
  const p = build();
  press(p.items[2], 10, 10);
  fireWindow("pointermove", { clientX: 40, clientY: 50 });
  eq(body.children.some(c => c.classes && c.classes.has("pk-ghost")), true);
  p.over(true);
  fireWindow("pointermove", { clientX: 60, clientY: 70 });
  eq(p.canvas.classes.has("pk-over"), true);
  fireWindow("pointerup", { clientX: 60, clientY: 70 });
  p.items[2].fire("click", {});                                                  // the click that follows a drag is not a tap
  eq(p.log.dropped, [["log", 60, 70]]);
  eq(p.log.picked, []);
  eq(p.canvas.classes.has("pk-over"), false);
  p.box.dispose();
});
t("let go anywhere else, a drag drops nothing", () => {
  const p = build();
  press(p.items[0], 0, 0);
  fireWindow("pointermove", { clientX: 30, clientY: 30 });
  p.over(false);
  fireWindow("pointerup", { clientX: 30, clientY: 30 });
  eq(p.log.dropped, []);
  p.box.dispose();
});
t("Escape cancels a drag", () => {
  const p = build();
  press(p.items[0], 0, 0);
  fireWindow("pointermove", { clientX: 30, clientY: 30 });
  p.over(true);
  fireWindow("keydown", { key: "Escape" });
  fireWindow("pointerup", { clientX: 30, clientY: 30 });
  eq(p.log.dropped, []);
  p.box.dispose();
});
t("a finger never starts a drag, so the page can still be scrolled", () => {
  const p = build();
  press(p.items[0], 0, 0, "touch");
  fireWindow("pointermove", { clientX: 90, clientY: 90 });
  p.over(true);
  fireWindow("pointerup", { clientX: 90, clientY: 90 });
  eq(p.log.dropped, []);
  p.box.dispose();
});
t("a palette that is disposed leaves no listener on the window", () => {
  const before = ["pointermove", "pointerup", "pointercancel", "keydown"].map(k => (windowListeners[k] || new Set()).size);
  const p = build();
  p.box.dispose();
  eq(["pointermove", "pointerup", "pointercancel", "keydown"].map(k => (windowListeners[k] || new Set()).size), before);
});
t("the form of a kind gives its spec from what was typed, with a time in the unit chosen", () => {
  const form = PK.specForm(KINDS[1].fields, {}, "x", T, false);
  const inputs = form.el.all().filter(e => e.tagName === "input" || e.tagName === "select");
  eq(form.collect().errors, ["seconds"]);                                           // needed and empty
  const number = inputs.find(e => e.tagName === "input"), unit = inputs.find(e => e.tagName === "select");
  number.value = "2"; unit.value = "h";
  eq(form.collect(), { spec: { seconds: 7200 }, errors: [] });
});
t("a saved spec fills the form back in, in the biggest unit", () => {
  const form = PK.specForm(KINDS[1].fields, { seconds: 5400 }, "x", T, false);
  const inputs = form.el.all().filter(e => e.tagName === "input" || e.tagName === "select");
  eq([inputs[0].value, inputs[1].value], ["90", "min"]);
  eq(form.collect().spec, { seconds: 5400 });
});
t("a finished step has its fields switched off", () => {
  const form = PK.specForm(KINDS[2].fields, { message: "hello" }, "x", T, true);
  eq(form.el.all().filter(e => e.tagName === "textarea").every(e => e.disabled), true);
});
const DECISION = [{ name: "input", kind: "expression", hidden: true }, { name: "mode", kind: "choice", choices: ["first", "all"], default: "first" }, { name: "branches", kind: "branches", required: true }];
const V = x => ({ kind: "value", value: x });
const cmp = (id, label, left, op, right, extra) => ({ id, label, left, op: V(op), right, ...(extra || {}) });
const ctx = { inputs: ["total"], variables: ["limit"] };
const textOf = e => e.children.map(c => (c.text !== undefined ? c.text : textOf(c))).join("");
const rowsOf = form => form.el.all().filter(e => e.classes.has("pk-branch"));
const buttons = r => r.all().filter(e => e.tagName === "button");
t("a comparison is three rows (first term, comparison, second term), each with a source; each branch moves and goes with buttons of its own", () => {
  const form = PK.specForm(DECISION, { mode: "first", branches: [cmp("b1", "< 10", { kind: "python", expr: "n" }, "<", V(10)), { id: "b2", label: "big", condition: "n > 99" }, { id: "b3", label: "else", op: "else" }] }, "x", T, false, ctx);
  const rows = rowsOf(form);
  eq(rows.length, 3);
  eq(rows[0].all().filter(e => e.classes.has("pk-term")).length, 3);
  eq(rows[0].all().filter(e => e.classes.has("pk-termlab")).map(textOf), ["pl.br.term.left", "pl.br.term.op", "pl.br.term.right"]);
  eq(buttons(rows[0]).map(b => b.attrs["aria-label"]).slice(-3), ["pl.br.up", "pl.br.down", "pl.br.remove"]);
  eq(buttons(rows[0]).filter(b => b.attrs["aria-label"] === "pl.br.up")[0].disabled, true);               // the first cannot go up
  eq(form.collect().spec.branches, [cmp("b1", "< 10", { kind: "python", expr: "n" }, "<", V("10")), { id: "b2", label: "big", condition: "n > 99" }, { id: "b3", label: "else", op: "else" }]);
  buttons(rows[0]).filter(b => b.attrs["aria-label"] === "pl.br.down")[0].fire("click", {});
  eq(form.collect().spec.branches.map(b => b.id), ["b2", "b1", "b3"]);
});
t("each term can be a value, a python expression, an input of the node, a value of the plan or a description for the agent", () => {
  const branch = { id: "b1", label: "x", left: { kind: "input", from: "total" }, op: V(">"), right: { kind: "input", variable: "limit" } };
  const form = PK.specForm(DECISION, { branches: [branch] }, "x", T, false, ctx);
  eq(form.collect().spec.branches[0], branch);
  const kinds = rowsOf(form)[0].all().filter(e => e.classes.has("pk-kind"));
  eq(kinds[0].children.map(o => o.value), ["value", "python", "input", "agent"]);
  eq(kinds[1].children.map(o => o.value), ["value", "input", "agent"]);                                       // the comparison is not an expression of its own
  const picks = rowsOf(form)[0].all().filter(e => e.tagName === "select" && !e.classes.has("pk-kind") && e.attrs["aria-label"] !== "pl.br.type" && e.attrs["aria-label"] !== "pl.br.term.op");
  eq(picks[0].children.map(o => o.value), ["", "from:total", "var:limit"]);
  kinds[2].value = "agent"; kinds[2].fire("change");
  const area = rowsOf(form)[0].all().filter(e => e.tagName === "textarea")[0];
  area.value = "how many tickets are left"; area.fire("input");
  eq(form.collect().spec.branches[0].right, { kind: "agent", describe: "how many tickets are left" });
});
t("an input term with no input and no plan value says how to get one", () => {
  const form = PK.specForm(DECISION, { branches: [{ id: "b1", label: "x", left: { kind: "input", from: "ghost" }, op: V("="), right: V(1) }] }, "x", T, false, { inputs: [], variables: [] });
  eq(rowsOf(form)[0].all().some(e => textOf(e) === "pl.br.input.none"), true);
  eq(form.collect().spec.branches[0].left, { kind: "input", from: "ghost" });                                  // what was saved is kept, not lost
});
t("a branch is added before the else, a second else cannot be added, and the last branch cannot be removed", () => {
  const form = PK.specForm(DECISION, { branches: [cmp("b1", "x", V(1), "<", V(2))] }, "x", T, false, ctx);
  const bar = () => form.el.all().filter(e => e.classes.has("pk-bradd"));
  bar()[1].fire("click", {});                                                       // add Else
  eq(form.collect().spec.branches.map(b => b.op.value || b.op), ["<", "else"]);
  eq(bar()[1].hidden, true);
  bar()[0].fire("click", {});                                                       // a new branch goes before the else
  eq(form.collect().spec.branches.map(b => (b.op && b.op.value) || b.op), ["<", "<", "else"]);
  rowsOf(form).forEach(r => r.all().filter(e => e.attrs["aria-label"] === "pl.br.remove").forEach(b => b.fire("click", {})));
  eq(rowsOf(form).length >= 1, true);
});
t("between shows a fourth row for the upper end, and an empty decision lacks its branches", () => {
  const form = PK.specForm(DECISION, { branches: [cmp("b1", "mid", V(15), "between", V(10), { right2: V(30) })] }, "x", T, false, ctx);
  eq(rowsOf(form)[0].all().filter(e => e.classes.has("pk-term")).length, 4);
  eq(form.collect().spec.branches[0], cmp("b1", "mid", V("15"), "between", V("10"), { right2: V("30") }));
  eq(PK.readSpec(DECISION, { branches: [] }).errors, ["branches"]);
});
t("the hidden field of a kind is not drawn", () => {
  const form = PK.specForm(DECISION, { branches: [cmp("b1", "x", V(1), "<", V(2))] }, "x", T, false, ctx);
  eq(form.el.all().filter(e => e.tagName === "label").map(textOf).some(t => t.includes("pl.spec.input")), false);
});
t("a finished decision has its branch controls switched off", () => {
  const form = PK.specForm(DECISION, { branches: [cmp("b1", "x", V(1), "<", V(2))] }, "x", T, true, ctx);
  eq(rowsOf(form).every(r => r.all().filter(e => e.tagName === "input" || e.tagName === "button" || e.tagName === "select").every(e => e.disabled)), true);
});
t("an archives field is the picker, and what it gives is the spec", () => {
  global.ragPick = { create: opts => ({ el: new El("div"), findId: "rp-1-find", get: () => ["a-1", "b-2"], opts }) };
  const form = PK.specForm([{ name: "archives", kind: "archives", required: true }], { archives: ["a-1"] }, "x", T, false);
  eq(form.collect(), { spec: { archives: ["a-1", "b-2"] }, errors: [] });
  delete global.ragPick;
});
console.log(JSON.stringify(cases.map(([name, fn]) => { try { fn(); return { name, ok: true }; } catch (e) { return { name, ok: false, error: String(e.message || e) }; } })));
