"use strict";
/* Changes panel (Monitor view): the agent's recent file edits with their diffs, from GET api/changes.
   Colour is never the only signal: every row has a sign (+, ±, −) and the diff lines keep theirs. */
(function () {
  const POLL_MS = 3000;
  let last = "";
  let timer = null;

  const kindOf = e => e.kind || (e.added > 0 && !e.removed ? "added" : e.removed > 0 && !e.added ? "deleted" : "modified");
  const SIGN = { added: "+", modified: "±", deleted: "−" };

  function diffBlock(text) {
    const pre = document.createElement("pre");
    pre.className = "chg-diff";
    pre.tabIndex = 0;                                   // scrollable region: reachable from the keyboard
    text.split("\n").forEach(line => {
      const span = document.createElement("span");
      span.className = "dl " + (line.startsWith("@@") ? "dl-hunk" : line.startsWith("+") ? "dl-add" : line.startsWith("-") ? "dl-del" : "");
      span.textContent = line + "\n";
      pre.appendChild(span);
    });
    return pre;
  }

  function row(edit) {
    const kind = ["added", "modified", "deleted"].includes(kindOf(edit)) ? kindOf(edit) : "modified";
    const el = document.createElement("li");
    el.className = `chg-row chg-${kind}`;
    const head = document.createElement("div");
    head.className = "chg-head";
    const name = document.createElement("span");
    name.className = "chg-file";
    name.textContent = `${SIGN[kind]} ${edit.file || "?"}`;
    const count = document.createElement("span");
    count.className = "chg-count";
    count.textContent = `+${edit.added ?? 0} −${edit.removed ?? 0}`;
    head.append(name, count);
    if (edit.diff) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = T("ch.diff");
      details.append(summary, diffBlock(edit.diff));
      el.append(head, details);
    } else {
      el.appendChild(head);
    }
    return el;
  }

  async function refresh() {
    const root = document.getElementById("changes-root");
    if (!root) return;
    let data = null;
    try {
      const res = await fetch("api/changes?t=" + Date.now(), { cache: "no-store", headers: authHeaders() });
      if (res.ok) data = await res.json();
    } catch (e) { /* offline: keep what is shown */ }
    if (!data) return;
    const edits = Array.isArray(data.edits) ? data.edits : [];
    const sig = JSON.stringify(edits.map(e => [e.file, e.time, e.added, e.removed]));
    if (sig === last) return;                           // unchanged: leave the open diffs and their scroll alone
    last = sig;
    if (!edits.length) {
      const empty = document.createElement("p");
      empty.className = "chg-empty dim";
      empty.textContent = T("ch.empty");
      root.replaceChildren(empty);
      return;
    }
    const list = document.createElement("ul");
    list.className = "chg-list";
    edits.slice().reverse().forEach(e => list.appendChild(row(e)));
    root.replaceChildren(list);
  }

  function schedule() {
    clearInterval(timer);
    timer = null;
    if (document.body.dataset.view === "stats" && !document.hidden) {
      refresh();
      timer = setInterval(refresh, POLL_MS);
    }
  }

  document.addEventListener("basalt:view", schedule);
  document.addEventListener("visibilitychange", schedule);
  document.addEventListener("DOMContentLoaded", schedule);
})();
