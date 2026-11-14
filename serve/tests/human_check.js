"use strict";
// Runs the decisions of serve/web/human.js and prints [{name, ok, error}] as JSON (read by test_human_page.py).
const path = require("path");
const H = require(path.join(__dirname, "..", "web", "human.js"));
const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };

const ch = [{ value: "n3", label: "Tre notizie", label_en: "Three stories" }, { value: "n6", label: "Sei notizie", label_en: "Six stories" }];
const ch3 = ch.concat([{ value: "n9", label: "Nove notizie", label_en: "Nine stories" }]);
const base = { plan_id: "p", plan_title: "P", ask: "value", kind: "text", choices: [], other: false };
const A = { ...base, node_id: "a", index: 2, prompt: "Tema?", prompt_en: "Topic?", help: "Una frase.", proposed: "politica", group: "Cosa e quando", group_en: "What and when" };
const B = { ...base, node_id: "b", index: 1, kind: "choice", choices: ch, prompt: "Quante?", prompt_en: "How many?", proposed: "n6", group: "Cosa e quando", group_en: "What and when" };
const C = { ...base, node_id: "c", index: 3, ask: "approve", prompt: "Va bene?", proposed: null, group: "Approvazioni", group_en: "Approvals" };
const D = { ...base, plan_id: "q", plan_title: "Q", node_id: "a", index: 1, kind: "number", prompt: "Quanto?" };
const M = { ...base, node_id: "m", index: 5, kind: "choice", choices: ch3, other: true, prompt: "Quante?", proposed: "n6" };
const Mclosed = { ...M, node_id: "mc", other: false };
const Two = { ...base, node_id: "w", index: 6, kind: "choice", choices: [{ value: "on", label: "Attivo", label_en: "On" }, { value: "off", label: "Non attivo", label_en: "Off" }], prompt: "Attivo?" };
const Task = { ...base, node_id: "t", index: 7, ask: "task", prompt: "Controlla il cavo", content: "Guarda il cavo." };

t("ALL the questions of one plan are one list, in the order of the plan, and the next plan has its own", () => {
  const g = H.pickGroup([A, B, C, D], {}, 0);
  eq([g.key, g.items.map(r => r.node_id)], ["p", ["b", "a", "c"]]);
  const many = Array.from({ length: 43 }, (_, i) => ({ ...base, node_id: "q" + i, index: 43 - i, prompt: "Q" + i }));
  const all = H.pickGroup(many, {}, 0);
  eq([all.items.length, all.items[0].node_id, all.items[42].node_id], [43, "q42", "q0"]);
  eq(H.pickGroup([], {}, 0), null); eq(H.pickGroup(null, {}, 0), null);
});
t("a plan left for later is skipped until its time, then comes back; the next plan is shown meanwhile", () => {
  const later = { p: 1000 + H.SNOOZE_MS };
  eq(H.pickGroup([A, B, D], later, 1000).key, "q");
  eq(H.pickGroup([A, B], later, 1000), null);
  eq(H.pickGroup([A, B], later, 1000 + H.SNOOZE_MS + 1).key, "p");
});
t("a heading goes before the first of each group, in the person's language, when the plan groups the questions", () => {
  const s = H.sections([B, A, C, M], "it");
  eq(s.map(x => x.heading), ["Cosa e quando", "", "Approvazioni", ""]);
  eq(H.sections([B, A, C], "en").map(x => x.heading), ["What and when", "", "Approvals"]);
  eq(H.sections([M, Two], "it").map(x => x.heading), ["", ""]);                  // no group: no heading
});
t("what the plan proposes, or what was answered the last time, is in the field at first; what was typed before wins", () => {
  eq(H.startAnswers([A, B, C], null), { a: "politica", b: "n6" });
  eq(H.startAnswers([A, B, C], { a: "sport", c: true }), { a: "sport", b: "n6", c: true });
});
t("the question is in the person's language, the other one when it is missing", () => {
  eq([H.say(A, "prompt", "it"), H.say(A, "prompt", "en"), H.say(A, "help", "en"), H.say(C, "prompt", "en")], ["Tema?", "Topic?", "Una frase.", "Va bene?"]);
  eq([H.labelOf(ch[0], "it"), H.labelOf(ch[0], "en"), H.labelOf({ value: "x" }, "en")], ["Tre notizie", "Three stories", "x"]);
});
t("two possible answers are two buttons; more than two are a menu; a free text is the field; a task is its own thing", () => {
  eq([H.twoWays(C), H.twoWays(Two), H.twoWays(B), H.twoWays(M), H.twoWays(A), H.twoWays(Task)], [true, true, true, false, false, false]);
});
t("an answer is checked for its question", () => {
  eq(H.check(A, "  hi "), { value: "hi" }); eq(H.check(A, "  "), { error: "hr.need" });
  eq(H.check(D, "12.5"), { value: "12.5" }); eq(H.check(D, "many"), { error: "hr.need.number" });
  eq(H.check(B, "n3"), { value: "n3" }); eq(H.check(B, "Tre notizie"), { error: "hr.need.pick2" }); eq(H.check(B, ""), { error: "hr.need.pick2" });
  eq(H.check(C, true), { value: true }); eq(H.check(C, undefined), { error: "hr.need.yesno" });
});
t("a menu of more than two choices takes what is written when it offers Other; a closed one and two choices do not", () => {
  eq(H.check(M, "n9"), { value: "n9" }); eq(H.check(M, "Nove e mezzo"), { value: "Nove e mezzo" });
  eq(H.check(M, "", true), { error: "hr.need" }); eq(H.check(M, ""), { error: "hr.need.pick" });
  eq(H.check(Mclosed, "Nove e mezzo"), { error: "hr.need.choice" });
  eq(H.check(Two, "on"), { value: "on" }); eq(H.check(Two, "other"), { error: "hr.need.pick2" });
  eq([H.writing(M, "Nove e mezzo"), H.writing(M, "n9"), H.writing(Mclosed, "x"), H.writing(M, "")], [true, false, false, false]);
  eq(H.newForm([M], { m: "Nove e mezzo" }).other, { m: true });                  // what was written the last time opens the field of Other
});
t("a task is done or cannot be done now; the note is optional; what cannot be done now is not sent", () => {
  eq(H.check(Task, undefined), { error: "hr.need.task" }); eq(H.check(Task, { note: "x" }), { error: "hr.need.task" });
  eq(H.check(Task, { state: "done", note: "" }), { value: { state: "done", note: "" } });
  const w = H.newForm([B, Task], null);
  w.answers.t = { state: "later", note: "" };
  eq(H.payload(w), { b: "n6" }); eq(H.notNow(w), ["t"]);
  w.answers.t = { state: "done", note: "  cable fine " };
  eq(H.payload(w), { b: "n6", t: { done: true, note: "cable fine" } }); eq(H.notNow(w), []);
});
t("the ones not answered yet are counted, and everything is sent together", () => {
  const w = H.newForm([B, A, C, Task], null);
  eq(H.unanswered(w), ["c", "t"]);
  w.answers.c = false; w.answers.t = { state: "done" };
  eq(H.unanswered(w), []);
  eq(H.payload(w), { b: "n6", a: "politica", c: false, t: { done: true, note: "" } });
});
t("a task left for later is out of the list for 10 minutes while the rest of the plan goes on", () => {
  eq(H.pickGroup([B, Task], { "p/t": 5000 }, 1000).items.map(r => r.node_id), ["b"]);
  eq(H.pickGroup([Task], { "p/t": 5000 }, 1000), null);
  eq(H.pickGroup([Task], { "p/t": 5000 }, 6000).items.map(r => r.node_id), ["t"]);
});
t("when the questions that are asking change, the answers given stay", () => {
  const w = H.newForm([B, A, C], null);
  w.answers.a = "mine";
  const r = H.reconcile(w, [B, A]);
  eq([r.items.length, r.answers.a], [2, "mine"]);
});
const out = [];
for (const [name, fn] of cases) { try { fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message }); } }
console.log(JSON.stringify(out));
