"use strict";
// Runs the page part of serve/web/askcard.js (AskCard.draw) on a tiny stand-in for the DOM and prints [{name, ok, error}] as JSON
// (read by test_ask_card_page.py). It drives the card with key presses and clicks the way a person does and reads the result from the
// elements and from what the card sends; the stand-in covers only what the card uses.
const fs = require("fs");
const path = require("path");

/* ---- a tiny DOM ---- */
class Text { constructor(t) { this.text = String(t); this.parent = null; } }
const state = { active: null };
class El {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.parent = null; this.attrs = {}; this.dataset = {}; this.hidden = false;
    this.tabIndex = 0; this.classes = new Set(); this.checked = false; this.value = ""; this.listeners = {};
    this.classList = { add: c => this.classes.add(c), contains: c => this.classes.has(c) };
  }
  set className(v) { this.classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this.classes].join(" "); }
  set textContent(v) { this.children = [new Text(v)]; this.children[0].parent = this; }
  get textContent() { return this.children.map(c => (c instanceof Text ? c.text : c.textContent)).join(""); }
  append(...nodes) { nodes.forEach(n => { const x = typeof n === "string" ? new Text(n) : n; if (x.parent) x.parent.children = x.parent.children.filter(c => c !== x); x.parent = this; this.children.push(x); }); }
  replaceChildren(...nodes) { this.children.forEach(c => { c.parent = null; }); this.children = []; this.append(...nodes); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  get elements() { return this.children.filter(c => c instanceof El); }
  all() { return this.elements.flatMap(c => [c, ...c.all()]); }
  ancestors() { const out = []; for (let n = this; n; n = n.parent) out.push(n); return out; }
  attr(k) {
    if (k === "hidden") return this.hidden ? "" : null;
    if (k === "type") return this.type || null;
    return this.getAttribute(k);
  }
  is(compound) {
    const m = /^([a-z]*)((?:\.[\w-]+)*)((?:\[[\w-]+(?:=[\w"-]+)?\])*)(:not\(\[hidden\]\))?$/i.exec(compound);
    if (!m) throw new Error("selector not supported: " + compound);
    if (m[1] && this.tagName !== m[1].toUpperCase()) return false;
    for (const c of m[2].split(".").filter(Boolean)) if (!this.classes.has(c)) return false;
    for (const a of m[3].match(/\[[^\]]+\]/g) || []) {
      const [k, v] = a.slice(1, -1).split("=");
      const got = this.attr(k);
      if (v === undefined ? got === null : got !== v.replace(/"/g, "")) return false;
    }
    return !(m[4] && this.hidden);
  }
  matches(sel) { return sel.split(",").some(one => { const parts = one.trim().split(/\s+/); return this.is(parts[parts.length - 1]) && this.chain(parts.slice(0, -1)); }); }
  chain(parts) { let node = this.parent; for (let i = parts.length - 1; i >= 0; i--) { while (node && !node.is(parts[i])) node = node.parent; if (!node) return false; node = node.parent; } return true; }
  querySelectorAll(sel) { return this.all().filter(e => e.matches(sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) { return this.ancestors().find(n => n instanceof El && n.matches(sel)) || null; }
  focus() { if (!this.ancestors().some(n => n.hidden)) state.active = this; }
  click() {
    if (this.tagName === "INPUT" && (this.type === "radio" || this.type === "checkbox")) {
      if (this.type === "radio") { if (this.checked) return; this.top().all().filter(e => e.tagName === "INPUT" && e.type === "radio" && e.name === this.name).forEach(e => { e.checked = false; }); this.checked = true; }
      else this.checked = !this.checked;
      (this.listeners.click || []).forEach(f => f({ detail: 0 }));
      if (this.onchange) this.onchange();
      return;
    }
    if (this.onclick) this.onclick();
  }
  top() { let n = this; while (n.parent) n = n.parent; return n; }
}
const document = { createElement: tag => new El(tag), get activeElement() { return state.active; } };
const window = {};
new Function("window", "document", fs.readFileSync(path.join(__dirname, "..", "web", "askcard.js"), "utf8"))(window, document);
const AC = window.AskCard;

/* ---- helpers ---- */
const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };
const same = (got, want) => { if (got !== want) throw new Error("not the element that was wanted: " + (got && (got.className || got.tagName))); };
const T = (k, ...a) => (a.length ? `${k}:${a.join("|")}` : k);
const card = () => [
  { id: "q1", text: "Which database?", kind: "single", required: true, allow_other: true, options: [{ id: "o1", label: "PostgreSQL", recommended: true }, { id: "o2", label: "SQLite" }] },
  { id: "q2", text: "Which sections?", kind: "multi", required: false, allow_other: true, options: [{ id: "o1", label: "Intro" }, { id: "o2", label: "End" }] },
  { id: "q3", text: "Anything out of scope?", kind: "free", required: false, options: [] },
];
function open(questions, extra = {}) {
  const el = new El("div"), b = { questions }, sent = [], chats = [];
  const ctx = { T, answered: false, focusFirst: false, send: (v, how) => sent.push([v, how]), chat: q => chats.push(q), ...extra };
  AC.draw(el, b, ctx);
  return { el, b, ctx, sent, chats, redraw: () => AC.draw(el, b, ctx) };
}
function key(target, k, mods = {}) {
  const ev = { key: k, target, ctrlKey: !!mods.ctrl, altKey: false, metaKey: false, shiftKey: !!mods.shift, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
  for (const n of target.ancestors()) { if (n.onkeydown) n.onkeydown(ev); }
  return ev;
}
const tabs = el => el.querySelectorAll(".aq-tab");
const panels = el => el.querySelectorAll(".aq-panel");
const shown = el => panels(el).findIndex(p => !p.hidden);
const rows = (el, i) => panels(el)[i].querySelectorAll(".aq-row input");

t("several questions are tabs with roles, one selected, one panel shown, and only the selected tab is a tab stop", () => {
  const { el } = open(card());
  eq(el.querySelector(".aq-tabs").getAttribute("role"), "tablist");
  eq(tabs(el).map(x => x.getAttribute("role")), ["tab", "tab", "tab"]);
  eq(tabs(el).map(x => x.getAttribute("aria-selected")), ["true", "false", "false"]);
  eq(tabs(el).map(x => x.tabIndex), [0, -1, -1]);
  eq(panels(el).map(p => p.getAttribute("role")), ["tabpanel", "tabpanel", "tabpanel"]);
  eq(panels(el).map((p, i) => p.getAttribute("aria-labelledby") === tabs(el)[i].id && tabs(el)[i].getAttribute("aria-controls") === p.id), [true, true, true]);
  eq(panels(el).map(p => p.hidden), [false, true, true]);
});

t("one question has no tabs", () => {
  const { el } = open([card()[0]]);
  eq(tabs(el).length, 0);
  same(el.querySelector(".aq-tabs"), null);
});

t("left and right on an answer move between the questions and land on its first answer", () => {
  const { el } = open(card());
  rows(el, 0)[0].focus();
  const ev = key(rows(el, 0)[0], "ArrowRight");
  eq(ev.defaultPrevented, true);
  eq(shown(el), 1); eq(tabs(el).map(x => x.getAttribute("aria-selected")), ["false", "true", "false"]);
  same(document.activeElement, rows(el, 1)[0]);
  key(document.activeElement, "ArrowLeft");
  eq(shown(el), 0); same(document.activeElement, rows(el, 0)[0]);
  key(document.activeElement, "ArrowLeft");                               // the first question has nothing before it
  eq(shown(el), 0);
  key(document.activeElement, "ArrowRight"); key(document.activeElement, "ArrowRight"); key(document.activeElement, "ArrowRight");
  eq(shown(el), 2);                                                       // and the last nothing after it
});

t("left, right, home and end on a tab move the tab; down goes into its answers", () => {
  const { el } = open(card());
  tabs(el)[0].focus();
  key(tabs(el)[0], "ArrowRight");
  same(document.activeElement, tabs(el)[1]); eq(shown(el), 1);
  key(tabs(el)[1], "End"); eq(shown(el), 2); same(document.activeElement, tabs(el)[2]);
  key(tabs(el)[2], "Home"); eq(shown(el), 0);
  key(tabs(el)[0], "ArrowDown"); same(document.activeElement, rows(el, 0)[0]);
  tabs(el)[1].onclick(); eq(shown(el), 1);                                // a tap on a tab shows its question
});

t("up and down move the focus between the answers without choosing, Enter picks", () => {
  const { el } = open(card());
  const r = rows(el, 0);                                                  // PostgreSQL, SQLite, Other
  r[0].focus();
  key(r[0], "ArrowDown"); same(document.activeElement, r[1]);
  eq(r.map(x => x.checked), [false, false, false]);
  key(r[1], "ArrowDown"); same(document.activeElement, r[2]);
  key(r[2], "ArrowDown"); same(document.activeElement, r[2]);               // no wrapping
  key(r[2], "ArrowUp"); key(r[1], "ArrowUp");
  same(document.activeElement, r[0]);
  key(r[1], "Enter");
  eq(r.map(x => x.checked), [false, true, false]);
  eq(shown(el), 1);                                                       // a picked single moves on to the next question
});

t("Enter toggles an answer of a multi and stays on the question", () => {
  const { el, b } = open(card());
  tabs(el)[1].onclick();
  const r = rows(el, 1);
  r[0].focus(); key(r[0], "Enter"); key(r[1], "Enter");
  eq(b.sel.q2.picked, ["o1", "o2"]);
  key(r[0], "Enter");
  eq(b.sel.q2.picked, ["o2"]);
  eq(shown(el), 1);
});

t("Enter on the row of their own opens the text and puts the focus in it; up and down leave it", () => {
  const { el, b } = open(card());
  const r = rows(el, 0), text = panels(el)[0].querySelector(".aq-other-text");
  eq(text.hidden, true);
  r[2].focus(); key(r[2], "Enter");
  eq(b.sel.q1.other, true); eq(text.hidden, false); same(document.activeElement, text);
  eq(shown(el), 0);                                                       // choosing the row of their own does not move on
  const ev = key(text, "ArrowLeft"); eq(ev.defaultPrevented, false);      // in a text field the arrows are the caret's
  key(text, "ArrowUp"); same(document.activeElement, r[2]);
  key(r[2], "ArrowDown"); same(document.activeElement, text);
});

t("the number keys still pick on the shown question, also from a tab", () => {
  const { el, b } = open(card());
  tabs(el)[0].focus(); key(tabs(el)[0], "2");
  eq(b.sel.q1.picked, ["o2"]);
  rows(el, 0)[0].focus(); key(rows(el, 0)[0], "1");
  eq(b.sel.q1.picked, ["o1"]);
});

t("Ctrl+Enter sends only when every required question is answered, from any control", () => {
  const { el, sent } = open(card());
  key(tabs(el)[0], "Enter", { ctrl: true });
  eq(sent.length, 0);
  rows(el, 0)[0].click();
  key(el.querySelector(".aq-foot").elements[1], "Enter", { ctrl: true });
  eq(sent.length, 1); eq(sent[0][1], "submitted");
  eq(JSON.parse(sent[0][0].slice(4)).answers.q1, { picked: ["o1"], text: "" });
});

t("a card of one single question is answered by Enter on the focused answer", () => {
  const { el, sent } = open([card()[0]]);
  const r = rows(el, 0);
  r[0].focus(); key(r[0], "ArrowDown"); key(r[1], "Enter");
  eq(sent.length, 1);
  eq(JSON.parse(sent[0][0].slice(4)).answers.q1.picked, ["o2"]);
  const other = open([card()[0]]);                                        // the row of their own waits for the text
  const o = rows(other.el, 0)[2]; o.focus(); key(o, "Enter");
  eq(other.sent.length, 0);
});

t("the tab of an answered question shows it, in words for a screen reader too", () => {
  const { el } = open(card());
  eq(tabs(el)[0].querySelector(".aq-tm").hidden, true);
  rows(el, 0)[0].click();
  eq(tabs(el)[0].querySelector(".aq-tm").hidden, false);
  eq(tabs(el)[0].querySelector(".sr").textContent, ", ch.ask.tab.done");
});

t("Keep chatting sets the card aside with its topic quoted, keeps the choices and reopens", () => {
  const { el, b, chats, sent } = open(card());
  rows(el, 0)[1].click();
  const chat = el.querySelector(".aq-foot").elements[2];
  chat.onclick();
  eq(sent.length, 0);                                                     // nothing is answered
  eq(chats, ["> Which database?\n> Which sections?\n> Anything out of scope?\n\n"]);
  eq(b.dismissed, true); eq(tabs(el).length, 0);
  el.dataset.built = "1";
  const re = el.querySelector(".btn"); eq(re.textContent, "ch.ask.reopen");
  eq(el.querySelector("p").getAttribute("role"), "status");
  re.onclick();
  eq(b.dismissed, false); eq(tabs(el).length, 3);
  eq(b.sel.q1.picked, ["o2"]); eq(rows(el, 0)[1].checked, true);          // the choice is still there
  same(document.activeElement, tabs(el)[0]);
});

t("a card set aside is not drawn over by the next paint, and closes when the person writes instead", () => {
  const { el, b, ctx, redraw } = open(card());
  el.querySelector(".aq-foot").elements[2].onclick();
  const aside = el.children.length;
  redraw();
  eq(el.children.length, aside); eq(tabs(el).length, 0);
  ctx.answered = true; b.answered = ""; redraw();
  eq(el.textContent, "ch.ask.closed");
});

t("the shown question is kept when the card is drawn again after being set aside", () => {
  const { el, b } = open(card());
  tabs(el)[2].onclick(); eq(b.tab, 2);
  el.querySelector(".aq-foot").elements[2].onclick();
  el.querySelector(".btn").onclick();
  eq(shown(el), 2);
});

const results = cases.map(([name, fn]) => { try { state.active = null; fn(); return { name, ok: true }; } catch (e) { return { name, ok: false, error: String(e.stack || e.message || e) }; } });
console.log(JSON.stringify(results));
