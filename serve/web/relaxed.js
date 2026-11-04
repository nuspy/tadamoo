"use strict";
/* The settings relaxed drafting depends on, and how to put them back. Relaxed drafting only acts when the drafter runs and one
   conversation is served at a time, so switching it on in the Loader sets those, remembers what they were, and switching it off
   gives back what it changed, unless the person changed that setting since. Pure functions on the form's values; loader.js does
   the reading and writing of the form. */
(function (root) {
  const SPEC = "engine.spec", MTP = "mtp", PARALLEL = "server.parallel", FULL = "server.slot-full-context", SIDE = "server.side-slot";
  const NAMES = [MTP, SPEC, PARALLEL, FULL, SIDE];
  const SPEC_ON = "7";                                        // the window the measurements used
  const num = v => { const x = Number(v); return Number.isFinite(x) ? x : 0; };

  /* `cur`: the form's values {mtp: bool, engine.spec: "", server.parallel: "", server.slot-full-context: bool}.
     Returns what to change: `set` (name -> new value) and `before` (name -> the value it had). */
  function enter(cur) {
    const before = {}, set = {};
    if (!cur[MTP] || num(cur[SPEC]) < 2) { before[MTP] = !!cur[MTP]; before[SPEC] = cur[SPEC] ?? ""; set[MTP] = true; set[SPEC] = SPEC_ON; }
    if (num(cur[PARALLEL] || 1) > 1) { before[PARALLEL] = cur[PARALLEL]; set[PARALLEL] = "1"; }
    if (cur[FULL]) { before[FULL] = true; set[FULL] = false; }
    if (num(cur[SIDE]) > 0) { before[SIDE] = cur[SIDE]; set[SIDE] = "0"; }      // the technical session turns the batch on: no relaxed drafting there
    return { before, set };
  }

  /* What to give back: a setting goes back to what it was only while it still holds the value this feature set; one changed by hand since is kept. */
  function leave(cur, undo) {
    const restore = {}, kept = [];
    for (const name of Object.keys(undo.before)) {
      const now = cur[name], want = undo.set[name];
      const same = typeof want === "boolean" ? !!now === want : String(now ?? "").trim() === String(want);
      if (same) restore[name] = undo.before[name]; else kept.push(name);
    }
    return { restore, kept };
  }

  /* Whether the form still shows what `undo` set (a profile or a reload may have replaced it). */
  function holds(cur, undo) {
    return !!undo && Object.keys(undo.set || {}).every(name => {
      const now = cur[name], want = undo.set[name];
      return typeof want === "boolean" ? !!now === want : String(now ?? "").trim() === String(want);
    });
  }

  root.TadamooRelaxed = { NAMES, enter, leave, holds };
})(typeof window !== "undefined" ? window : globalThis);
