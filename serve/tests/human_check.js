"use strict";
// Runs the decisions of serve/web/human.js and prints [{name, ok, error}] as JSON (read by test_human_page.py).
const path = require("path");
const H = require(path.join(__dirname, "..", "web", "human.js"));
const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };

const ch = [{ value: "n3", label: "Tre notizie", label_en: "Three stories" }, { value: "n6", label: "Sei notizie", label_en: "Six stories" }];
const A = { plan_id: "p", plan_title: "P", node_id: "a", index: 2, ask: "value", kind: "text", choices: [], prompt: "Tema?", prompt_en: "Topic?", help: "Una frase.", proposed: "politica" };
const B = { plan_id: "p", plan_title: "P", node_id: "b", index: 1, ask: "value", kind: "choice", choices: ch, prompt: "Quante?", prompt_en: "How many?", proposed: "n6" };
const C = { plan_id: "p", plan_title: "P", node_id: "c", index: 3, ask: "approve", choices: [], prompt: "Va bene?", proposed: null };
const D = { plan_id: "q", plan_title: "Q", node_id: "a", index: 1, ask: "value", kind: "number", choices: [], prompt: "Quanto?" };

t("the questions of one plan are one path, in the order of the plan", () => {
  const g = H.pickGroup([A, B, C, D], {}, 0);
  eq([g.key, g.items.map(r => r.node_id)], ["p", ["b", "a", "c"]]);
  eq(H.pickGroup([], {}, 0), null); eq(H.pickGroup(null, {}, 0), null);
});
t("a plan left for later is skipped until its time, then comes back; the next plan is shown meanwhile", () => {
  const later = { p: 1000 + H.SNOOZE_MS };
  eq(H.pickGroup([A, B, D], later, 1000).key, "q");
  eq(H.pickGroup([A, B], later, 1000), null);
  eq(H.pickGroup([A, B], later, 1000 + H.SNOOZE_MS + 1).key, "p");
});
t("what the plan proposes, or what was answered the last time, is in the field at first; what was typed before wins", () => {
  eq(H.startAnswers([A, B, C], null), { a: "politica", b: "n6" });
  eq(H.startAnswers([A, B, C], { a: "sport", c: true }), { a: "sport", b: "n6", c: true });
});
t("the question is in the person's language, the other one when it is missing; a choice is read by its label, never its value", () => {
  eq([H.say(A, "prompt", "it"), H.say(A, "prompt", "en"), H.say(A, "help", "en"), H.say(C, "prompt", "en")], ["Tema?", "Topic?", "Una frase.", "Va bene?"]);
  eq([H.labelOf(ch[0], "it"), H.labelOf(ch[0], "en"), H.labelOf({ value: "x" }, "en")], ["Tre notizie", "Three stories", "x"]);
  eq([H.shown(B, "n3", "it", {}), H.shown(B, "n3", "en", {}), H.shown(C, true, "it", { yes: "Sì", no: "No" }), H.shown(C, false, "it", { yes: "Sì", no: "No" })], ["Tre notizie", "Three stories", "Sì", "No"]);
});
t("an answer is checked for its question", () => {
  eq(H.check(A, "  hi "), { value: "hi" }); eq(H.check(A, "  "), { error: "hr.need" });
  eq(H.check(D, "12.5"), { value: "12.5" }); eq(H.check(D, "many"), { error: "hr.need.number" });
  eq(H.check(B, "n3"), { value: "n3" }); eq(H.check(B, "Tre notizie"), { error: "hr.need.choice" }); eq(H.check(B, ""), { error: "hr.need.choice" });
  eq(H.check(C, true), { value: true }); eq(H.check(C, undefined), { error: "hr.need.yesno" });
});
t("Next goes on only with a good answer, to the summary after the last; Back goes back; Edit from the summary returns there", () => {
  const w = H.newPath([B, A, C], null);
  eq(w.step, 0); eq(H.next(w), {}); eq(w.step, 1); eq(H.next(w), {}); eq(w.step, 2);
  eq(H.next(w), { error: "hr.need.yesno" }); eq(w.step, 2);                    // the approval has no answer yet
  w.answers.c = true; eq(H.next(w), {}); eq(w.step, 3);                          // the summary
  eq(H.missing(w), -1);
  H.edit(w, 0); eq(w.step, 0); w.answers.b = "n3"; eq(H.next(w), {}); eq(w.step, 3);   // one change, and it is back at the summary
  H.prev(w); eq(w.step, 2); H.prev(w); H.prev(w); H.prev(w); eq(w.step, 0);       // it does not go below the first
});
t("everything is sent together, as the server takes it; one that is missing is found", () => {
  const w = H.newPath([B, A, C], null);
  eq(H.missing(w), 2);
  w.answers.c = false;
  eq(H.payload(w), { b: "n6", a: "politica", c: false }); eq(H.missing(w), -1);
});
t("when the questions that are asking change, the answers given stay and the step stays where it can", () => {
  const w = H.newPath([B, A, C], null);
  w.answers.a = "mine"; w.step = 2;
  const r = H.reconcile(w, [B, A]);
  eq([r.items.length, r.answers.a, r.step], [2, "mine", 2]);
  eq(H.reconcile(w, [A]).step, 1);
});
const out = [];
for (const [name, fn] of cases) { try { fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message }); } }
console.log(JSON.stringify(out));
