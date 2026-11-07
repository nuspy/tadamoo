"use strict";
// Runs the page part of serve/web/plankinds.js (the palette and the form of a kind) on a tiny stand-in for the DOM and prints [{name, ok, error}] as JSON
// (read by test_plankinds_page.py). It drives the palette with clicks and pointer moves and the form with typed values.
class Text { constructor(t) { this.text = String(t); this.nodeType = 3; } }
class El {
  constructor(tag) {
    this.nodeType = 1; this.tagName = tag; this.children = []; this.attrs = {}; this.dataset = {}; this.listeners = {}; this.classes = new Set(); this.style = {}; this.value = ""; this.disabled = false;
    this.classList = { add: c => this.classes.add(c), remove: c => this.classes.delete(c), contains: c => this.classes.has(c), toggle: (c, on) => (on ? this.classes.add(c) : this.classes.delete(c)) };
  }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(" "); }
  append(...nodes) { nodes.forEach(n => this.children.push(typeof n === "string" ? new Text(n) : n)); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
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
  eq([inputs[0].value, inputs[1].all().find(o => o.selected).value], ["90", "min"]);                  // the option marked chosen is the unit
  eq(form.collect().spec, { seconds: 5400 });
});
t("a finished step has its fields switched off", () => {
  const form = PK.specForm(KINDS[2].fields, { message: "hello" }, "x", T, true);
  eq(form.el.all().filter(e => e.tagName === "textarea").every(e => e.disabled), true);
});
console.log(JSON.stringify(cases.map(([name, fn]) => { try { fn(); return { name, ok: true }; } catch (e) { return { name, ok: false, error: String(e.message || e) }; } })));
