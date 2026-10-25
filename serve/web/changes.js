"use strict";
/* Changes panel (Monitor view): the agent's recent file edits with their diffs, from GET api/changes, and the
   review of each edit block by block through POST api/changes/review. The agent applies an edit when it makes it;
   "Undo" puts the old lines back. Colour is never the only signal: every row has a sign (+, ±, −), every diff line
   keeps its own, and a review state is always written out. */
(function () {
  const POLL_MS = 3000;
  const CONFIRM_MS = 5000;
  let last = "";
  let timer = null;
  let edits = [];
  const openIds = new Set();                              // rows whose diff is open survive a redraw

  const kindOf = e => e.kind || (e.added > 0 && !e.removed ? "added" : e.removed > 0 && !e.added ? "deleted" : "modified");
  const SIGN = { added: "+", modified: "±", deleted: "−" };
  const OPEN_STATES = ["pending", "partial"];
  const signature = list => JSON.stringify(list.map(e => [e.id, e.file, e.time, e.added, e.removed, e.review, e.blocks]));

  /* The diff as its header and its blocks, split at the "@@" lines the server writes. */
  function split(text) {
    const blocks = [];
    let head = [];
    text.split("\n").forEach(line => {
      if (line.startsWith("@@")) blocks.push([line]);
      else if (blocks.length) blocks[blocks.length - 1].push(line);
      else head.push(line);
    });
    blocks.forEach(b => { while (b.length && b[b.length - 1] === "") b.pop(); });
    return { head: head.filter(Boolean), blocks };
  }

  function lines(arr) {
    const pre = document.createElement("pre");
    pre.className = "chg-diff";
    pre.tabIndex = 0;                                   // scrollable region: reachable from the keyboard
    arr.forEach(line => {
      const span = document.createElement("span");
      span.className = "dfl " + (line.startsWith("@@") ? "dfl-hunk" : line.startsWith("+") ? "dfl-add" : line.startsWith("-") ? "dfl-del" : "");
      span.textContent = line + "\n";
      pre.appendChild(span);
    });
    return pre;
  }

  /* A button that asks twice before it undoes anything: the first press changes its label, the second acts. */
  function undoButton(label, aria, act) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn chg-undo";
    b.textContent = label;
    b.setAttribute("aria-label", aria);
    let armed = null;
    b.addEventListener("click", () => {
      if (armed) { clearTimeout(armed); armed = null; act(); return; }
      b.textContent = T("ch.confirm");
      b.classList.add("armed");
      armed = setTimeout(() => { armed = null; b.textContent = label; b.classList.remove("armed"); }, CONFIRM_MS);
    });
    return b;
  }

  function plainButton(label, aria, act) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "btn";
    b.textContent = label;
    b.setAttribute("aria-label", aria);
    b.addEventListener("click", act);
    return b;
  }

  async function review(edit, action, block, holder) {
    holder.querySelectorAll("button").forEach(b => { b.disabled = true; });
    let message = "";
    try {
      const res = await fetch("api/changes/review", {
        method: "POST", cache: "no-store", headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(block === null ? { id: edit.id, action } : { id: edit.id, action, block }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.edit) {
        edits = edits.map(e => e.id === edit.id ? data.edit : e);
        last = signature(edits);
        draw();
        return;
      }
      message = (data.error && data.error.message) || String(res.status);
    } catch (e) {
      message = String(e && e.message || e);
    }
    holder.querySelectorAll("button").forEach(b => { b.disabled = false; });
    let note = holder.querySelector(".chg-note");
    if (!note) {
      note = document.createElement("p");
      note.className = "chg-note";
      note.setAttribute("role", "alert");
      holder.appendChild(note);
    }
    note.textContent = T("ch.failed", message);
  }

  function row(edit) {
    const kind = ["added", "modified", "deleted"].includes(kindOf(edit)) ? kindOf(edit) : "modified";
    const state = edit.review || "unavailable";
    const reviewable = OPEN_STATES.includes(state);
    const el = document.createElement("li");
    el.className = `chg-row chg-${kind} chg-st-${state}`;
    const head = document.createElement("div");
    head.className = "chg-head";
    const name = document.createElement("span");
    name.className = "chg-file";
    name.textContent = `${SIGN[kind]} ${edit.file || "?"}`;
    const count = document.createElement("span");
    count.className = "chg-count";
    count.textContent = `+${edit.added ?? 0} −${edit.removed ?? 0}`;
    head.append(name, count);
    el.appendChild(head);

    if (["kept", "reverted", "superseded", "partial", "pending"].includes(state)) {
      const badge = document.createElement("p");
      badge.className = "chg-state";
      badge.textContent = T("ch.state." + state);
      el.appendChild(badge);
    }

    const holder = document.createElement("div");
    holder.className = "chg-actions";
    if (reviewable) {
      holder.append(
        plainButton(T("ch.keep.all"), `${T("ch.keep.all")} ${edit.file}`, () => review(edit, "keep_all", null, holder)),
        undoButton(T("ch.undo.all"), `${T("ch.undo.all")} ${edit.file}`, () => review(edit, "reject_all", null, holder)));
      el.appendChild(holder);
    }

    if (edit.diff) {
      const details = document.createElement("details");
      details.dataset.eid = edit.id;
      details.open = openIds.has(edit.id);
      details.addEventListener("toggle", () => { if (details.open) openIds.add(edit.id); else openIds.delete(edit.id); });
      const summary = document.createElement("summary");
      summary.textContent = T("ch.diff");
      details.appendChild(summary);
      const parts = split(edit.diff);
      if (reviewable && parts.blocks.length) {
        const total = parts.blocks.length;
        parts.blocks.forEach((block, i) => {
          const wrap = document.createElement("div");
          wrap.className = "chg-block";
          const label = document.createElement("div");
          label.className = "chg-blockhead";
          const title = document.createElement("span");
          title.textContent = T("ch.block", i + 1, total);
          label.appendChild(title);
          const kept = Array.isArray(edit.blocks) && edit.blocks[i] === true;
          const bh = document.createElement("div");
          bh.className = "chg-actions";
          if (kept) {
            const mark = document.createElement("span");
            mark.className = "chg-kept";
            mark.textContent = T("ch.kept");
            bh.appendChild(mark);
          } else {
            bh.appendChild(plainButton(T("ch.keep"), `${T("ch.keep")} ${T("ch.block", i + 1, total)} ${edit.file}`, () => review(edit, "keep", i, bh)));
          }
          bh.appendChild(undoButton(T("ch.undo"), `${T("ch.undo")} ${T("ch.block", i + 1, total)} ${edit.file}`, () => review(edit, "reject", i, bh)));
          label.appendChild(bh);
          wrap.append(label, lines(block));
          details.appendChild(wrap);
        });
      } else {
        details.appendChild(lines(edit.diff.split("\n")));
      }
      el.appendChild(details);
    }
    return el;
  }

  /* Scroll positions that a redraw would reset: the panel's own scroller and each open diff. */
  function scroller(el) {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p).overflowY;
      if ((o === "auto" || o === "scroll") && p.scrollHeight > p.clientHeight) return p;
    }
    return null;
  }
  const diffKeys = root => {
    const keys = new Map();
    root.querySelectorAll("details[data-eid]").forEach(d => d.querySelectorAll(".chg-diff").forEach((pre, i) => keys.set(`${d.dataset.eid}:${i}`, pre)));
    return keys;
  };

  function draw() {
    const root = document.getElementById("changes-root");
    if (!root) return;
    root.querySelectorAll("details[data-eid]").forEach(d => { if (d.open) openIds.add(d.dataset.eid); else openIds.delete(d.dataset.eid); });
    const host = scroller(root), top = host ? host.scrollTop : 0;
    const diffTops = new Map([...diffKeys(root)].map(([k, pre]) => [k, pre.scrollTop]));
    paint(root);
    if (host) host.scrollTop = top;
    diffKeys(root).forEach((pre, k) => { if (diffTops.get(k)) pre.scrollTop = diffTops.get(k); });
  }

  function paint(root) {
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

  async function refresh() {
    const root = document.getElementById("changes-root");
    if (!root) return;
    let data = null;
    try {
      const res = await fetch("api/changes?t=" + Date.now(), { cache: "no-store", headers: authHeaders() });
      if (res.ok) data = await res.json();
    } catch (e) { /* offline: keep what is shown */ }
    if (!data) return;
    const next = Array.isArray(data.edits) ? data.edits : [];
    const sig = signature(next);
    if (sig === last) return;                           // unchanged: leave the open diffs and their scroll alone
    last = sig;
    edits = next;
    draw();
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
