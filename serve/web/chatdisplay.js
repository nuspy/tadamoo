"use strict";
/* How the Agent chat shows the model's reasoning and its actions (tools and background commands): the choice of each is a setting a preset keeps (chatset.js, `shows`): full, compact or absent.
   Full: every block is a line of its own, closed once it is done (a reasoning box is open while the model thinks). Compact: consecutive reasoning blocks are one closed line and consecutive
   tooling blocks another, a container that opens like the others (how many, and which actions); a run of one block is its own line. Absent: nothing of it is shown. */
window.chatDisplay = (() => {
  const MODES = ["full", "compact", "absent"];
  const seriesOf = b => b.t === "reasoning" ? "reasoning" : (b.t === "tool" || b.t === "job") ? "tooling" : "";
  const modeFor = m => MODES.includes(m) ? m : "full";
  const modeOf = kind => modeFor(typeof chatSettings === "object" && chatSettings && chatSettings.shows ? chatSettings.shows(kind) : "full");

  /* the line a container shows when it is closed */
  function summary(kind, blocks, T) {
    if (kind === "reasoning") return T("ch.group.reasoning", blocks.length);
    const names = new Map();
    let failed = 0, running = 0;
    for (const b of blocks) {
      const n = b.name || b.id || "?";
      names.set(n, (names.get(n) || 0) + 1);
      if (b.status === "failed") failed++;
      if (b.status === "running" || b.state === "running") running++;
    }
    const shown = [...names].slice(0, 4).map(([n, c]) => c > 1 ? `${n} ×${c}` : n).join(", ") + (names.size > 4 ? ", …" : "");
    return T("ch.group.tooling", blocks.length, shown) + (failed ? " · " + T("ch.group.failed", failed) : "") + (running ? " · " + T("ch.group.running", running) : "");
  }

  const mk = (tag, cls) => { const e = document.createElement(tag); if (cls) e.className = cls; return e; };
  function group(kind, first) {
    const el = mk("details", "agroup"), sum = mk("summary"), body = mk("div", "agroup-b"), title = mk("span", "agroup-t");
    el.dataset.kind = kind;
    sum.append(title);
    el.append(sum, body);
    sum.addEventListener("click", () => { el.dataset.touched = "1"; });        // a reader who opened or closed it keeps it so until the setting changes
    first.row.before(el);
    return { kind, el, body, title, blocks: [] };
  }

  /* the row of a block that has just been made goes into the container of the run it continues, or on its own */
  function place(it, b) {
    const kind = seriesOf(b), i = it.blocks.indexOf(b), prev = i > 0 ? it.blocks[i - 1] : null;
    if (!kind || !prev || seriesOf(prev) !== kind || !prev.row) { it.body.append(b.row); return; }
    let g = prev.group;
    if (!g) { g = prev.group = group(kind, prev); g.blocks.push(prev); g.body.append(prev.row); }
    b.group = g;
    g.blocks.push(b);
    g.body.append(b.row);
  }

  /* after the blocks were painted: what is absent is hidden; in compact each run is in its container, which says what is in it and is closed unless the reader opened it; otherwise the rows are on their own.
     `modes` is the open conversation's own saved choice, when there is one: the settings panel binds to it only in some chat modes (chatset.js), so a block list would otherwise show whatever
     the panel last held - possibly another conversation's choice, or the machine default - rather than the one this conversation was actually given. */
  function paint(it, T, modes) {
    modes = modes || { reasoning: modeOf("reasoning"), tooling: modeOf("tooling") };
    const done = new Set();
    for (const b of it.blocks) {
      const kind = seriesOf(b);
      if (!kind || !b.row) continue;
      if (modes[kind] === "absent") b.row.hidden = true;
      const g = b.group;
      if (!g || done.has(g)) continue;
      done.add(g);
      if (modes[kind] === "compact") {
        for (const x of g.blocks) if (x.row.parentNode !== g.body) g.body.append(x.row);
        g.el.hidden = g.blocks.every(x => x.row.hidden);
        g.title.textContent = summary(kind, g.blocks, T);
        if (!g.el.dataset.touched) g.el.open = false;
      } else {
        for (const x of g.blocks) if (x.row.parentNode !== g.el.parentNode) g.el.before(x.row);
        g.el.hidden = true;
      }
    }
  }

  /* the setting changed: what a reader opened or closed by hand gives way to it */
  function reset() { document.querySelectorAll(".agroup").forEach(g => { delete g.dataset.touched; }); }

  return { seriesOf, summary, place, paint, reset, modeFor };
})();
