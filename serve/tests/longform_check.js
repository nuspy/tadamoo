"use strict";
// Runs serve/web/longform.js on a stand-in page and prints [{name, ok, error}] as JSON (read by test_longform_sticky.py).
const path = require("path");
const mem = {};
global.localStorage = { getItem: k => (k in mem ? mem[k] : null), setItem: (k, v) => { mem[k] = String(v); } };
global.window = global;
const els = {};
const el = () => {
  const e = { value: "", checked: false, hidden: false, dataset: {}, textContent: "", listeners: {}, attrs: {},
    setAttribute(k, v) { e.attrs[k] = v; }, addEventListener(t, fn) { (e.listeners[t] = e.listeners[t] || []).push(fn); },
    querySelector() { return el(); }, focus() {}, showModal() {}, close() {} };
  return e;
};
global.$ = sel => (els[sel] = els[sel] || el());
global.T = k => k;
const docListeners = {};
global.document = { addEventListener: (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); } };
const fire = (type, detail) => (docListeners[type] || []).forEach(fn => fn({ detail }));
let current = "";
global.chatUi = { sessionId: () => current };
require(path.join(__dirname, "..", "web", "longform.js"));
const LA = global.longAnswer;
const submit = () => els["#la-on"].listeners.click[0]();            // "Keep on for this conversation": the sticky choice these older cases exercise
const submitOnce = () => els["#la-once"].listeners.click[0]();      // "Only this next prompt": the default scope
const turnOff = () => els["#la-off"].listeners.click[0]();
const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };

t("it is off at first and, once on, it is still on after a message was sent", () => {
  current = ""; fire("tadamoo:conversation");
  eq(LA.request(), null);
  els["#la-words"].value = "1500"; els["#la-mode"].value = "write";
  submit();
  const first = LA.request();
  eq(first.target_words, 1500);
  eq(LA.armed(), true);
  eq(LA.request(), first);
  eq(LA.decorate("hello").includes("[long answer: mode=write; size=fixed; words=1500]"), true);
  eq(LA.decorate("again").includes("[long answer:"), true);
  eq(els["#long-open"].attrs["aria-pressed"], "true");
});

t("each conversation keeps its own choice", () => {
  current = "a"; fire("tadamoo:conversation");
  eq(LA.armed(), false);
  submit();
  eq(LA.armed(), true);
  current = "b"; fire("tadamoo:conversation");
  eq(LA.armed(), false); eq(els["#long-open"].attrs["aria-pressed"], "false");
  current = "a"; fire("tadamoo:conversation");
  eq(LA.armed(), true);
  turnOff();
  eq(LA.armed(), false);
});

t("a conversation created on the first message takes what the unstarted one had, and the next new one starts again", () => {
  current = ""; fire("tadamoo:conversation");
  submit();
  eq(LA.armed(), true);
  current = "s1"; fire("tadamoo:conversation", { created: true });
  eq(LA.armed(), true);
  current = ""; fire("tadamoo:conversation");
  eq(LA.armed(), false);
});

t("with the new-conversations box ticked a fresh conversation starts on, and turning it off there stays off", () => {
  current = ""; fire("tadamoo:conversation");
  els["#la-default"].checked = true;
  submit();
  current = "s2"; fire("tadamoo:conversation");
  eq(LA.armed(), true);
  turnOff();
  eq(LA.armed(), false);
  current = "s3"; fire("tadamoo:conversation");
  eq(LA.armed(), true);
  els["#la-default"].checked = false; current = "s4"; fire("tadamoo:conversation"); submit(); turnOff();
  current = "s5"; fire("tadamoo:conversation");
  eq(LA.armed(), false);
});

t("once is armed for exactly the next prompt, then turns itself off", () => {
  current = "s6"; fire("tadamoo:conversation");
  eq(LA.armed(), false);
  els["#la-words"].value = "4000"; els["#la-mode"].value = "write"; els["#la-size"].value = "fixed";
  submitOnce();
  eq(LA.armed(), true);
  eq(LA.decorate("first").includes("[long answer:"), true);
  eq(LA.armed(), false);                                             // used once: off again, with no second click
  eq(LA.decorate("second"), "second");                                // and the next message is ordinary
});

t("once used through request() (the Chat) turns itself off the same way", () => {
  current = "s7"; fire("tadamoo:conversation");
  submitOnce();
  eq(LA.request() !== null, true);
  eq(LA.armed(), false);
  eq(LA.request(), null);
});

t("heuristic asks for no fixed number at all, hybrid gives one as a base only", () => {
  current = "s8"; fire("tadamoo:conversation");
  els["#la-mode"].value = "write"; els["#la-size"].value = "heuristic";
  submit();
  eq(LA.request().size, "heuristic");
  eq("target_words" in LA.request(), false);
  eq(LA.decorate("x").includes("size=heuristic"), true);
  eq(LA.decorate("x").includes("words="), false);
  turnOff();
  current = "s9"; fire("tadamoo:conversation");
  els["#la-size"].value = "hybrid"; els["#la-words"].value = "9000";
  submit();
  eq(LA.request().size, "hybrid");
  eq(LA.request().target_words, 9000);
  eq(LA.decorate("x").includes("size=hybrid; words=9000"), true);
});

console.log(JSON.stringify(cases.map(([name, fn]) => { try { fn(); return { name, ok: true }; } catch (e) { return { name, ok: false, error: String(e.message || e) }; } })));
