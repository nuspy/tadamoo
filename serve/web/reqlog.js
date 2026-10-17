"use strict";
/* LOGS view: the server's request log (GET api/reqlog) as one card per request. Text from the log only ever enters the
   page through text nodes. New events are queued and drawn in animation-frame batches that append to the DOM; long
   texts are cut before they are put into the page and expanded in slices. Nothing of the log is stored in the browser. */
(() => {
  const POLL_MS = 1000, POLL_MAX_MS = 15000, OFF_POLL_MS = 5000;
  const CLIP_CHARS = 4000, CLIP_LINES = 60, SLICE = 100000;
  const MEM_CAP = 6e6, EVENT_COST = 200, MATCH_CAP = 5000, DROP_PER_FRAME = 40;
  const FRAME_MS = 8, FIND_WAIT_MS = 150, FIND_AGAIN_MS = 500, HEAVY = 1;
  const FORMAT_KEY = "basalt.logs.format";

  const el = id => document.getElementById(id);
  const mk = (tag, cls, txt) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (txt != null) e.textContent = txt;
    return e;
  };
  const N = v => nfmt(v);
  const secs = v => `${nfmt(v, v < 10 ? 2 : 1)} s`;
  const stamp = t => clock(t * 1000).slice(0, 8);
  const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  const isLow = c => c >= 0xDC00 && c <= 0xDFFF;

  const logEl = el("rl-log"), qEl = el("rl-q");
  const S = {
    after: 0, epoch: null, gen: 0, paused: false, follow: true, enabled: true, conn: "wait",
    kept: 0, chars: 0, pageDropped: 0, skipped: 0, srv: { events: 0, chars: 0 },
    pending: [], pendingChars: 0, stimer: 0, sfresh: false, sgen: 0, reveal: null, dirty: false, top: 0,
  };
  const incoming = [];
  let inHead = 0;
  const items = [];                  // the stream in order: cards and loose rows
  const cards = new Map();           // request id -> card
  const tasks = [];                  // generators run inside animation frames
  let raf = 0, uid = 0;
  let matches = [], curIdx = -1, cur = null, capped = false, markedUnits = new Set();

  /* ---- units: one searchable text, clipped when long ---- */
  function newUnit(parent, cls) {
    const wrap = mk("div", "rl-unit" + (cls ? " " + cls : ""));
    const u = { id: ++uid, text: "", cut: null, open: false, painted: 0, marks: [], wrap, el: mk("div", "rl-text"),
                btn: null, task: false, reset: true, item: null };
    wrap.append(u.el);
    parent.append(wrap);
    return u;
  }
  function fit(u) {                  // the clip point is fixed once the text is long, because text only grows
    if (u.cut != null) return false;
    const t = u.text, head = t.length > CLIP_CHARS ? t.slice(0, CLIP_CHARS) : t;
    let nl = -1, n = 0;
    while (n < CLIP_LINES && (nl = head.indexOf("\n", nl + 1)) !== -1) n++;
    if (n === CLIP_LINES && nl + 1 < t.length) u.cut = nl;
    else if (t.length > CLIP_CHARS) u.cut = isLow(t.charCodeAt(CLIP_CHARS)) ? CLIP_CHARS + 1 : CLIP_CHARS;
    return u.cut != null;
  }
  function addText(u, more, item) {
    if (!more) return;
    const was = u.cut;
    u.text += more;
    if (item) { item.chars += more.length; S.chars += more.length; }
    fit(u);
    if (u.cut != null) button(u);
    if (was == null) { u.reset = true; paint(u); }
    else if (u.open) paint(u);
    S.dirty = true;
  }
  function button(u) {
    if (!u.btn) {
      u.btn = mk("button", "btn rl-more");
      u.btn.type = "button";
      u.btn.onclick = () => (u.open ? shut(u) : expand(u));
      u.wrap.append(u.btn);
    }
    u.btn.textContent = u.open ? T("lg.less") : T("lg.more", N(u.text.length));
    u.btn.setAttribute("aria-expanded", String(u.open));
  }
  const expand = u => { u.open = true; u.reset = true; button(u); paint(u); };
  const shut = u => { u.open = false; u.reset = true; button(u); paint(u); };
  const limitOf = u => (u.open ? u.text.length : u.cut ?? u.text.length);

  function lowerBound(marks, pos) {
    let a = 0, b = marks.length;
    while (a < b) { const m = (a + b) >> 1; if (marks[m].s < pos) a = m + 1; else b = m; }
    return a;
  }
  function boundary(u, from, limit) {
    if (limit - from <= SLICE * 1.2) return limit;
    let b = from + SLICE;
    const nl = u.text.lastIndexOf("\n", b);
    if (nl > from + SLICE / 2) b = nl + 1;
    else if (isLow(u.text.charCodeAt(b))) b++;
    const i = lowerBound(u.marks, b) - 1;       // keep a highlighted match whole
    if (i >= 0 && u.marks[i].e > b) b = u.marks[i].e;
    return Math.min(b, limit);
  }
  function chunk(u, s, e) {
    const d = document.createElement("div");
    let at = s;
    for (let i = lowerBound(u.marks, s); i < u.marks.length && u.marks[i].s < e; i++) {
      const m = u.marks[i];
      if (m.e > e) break;
      if (m.s > at) d.append(u.text.slice(at, m.s));
      const k = mk("mark", m === cur ? "rl-cur" : "", u.text.slice(m.s, m.e));
      m.el = k;
      d.append(k);
      at = m.e;
    }
    if (at < e) d.append(u.text.slice(at, e));
    return d;
  }
  function* paintGen(u) {
    for (;;) {
      if (u.reset) { u.el.replaceChildren(); u.painted = 0; u.reset = false; }
      const limit = limitOf(u);
      if (u.painted >= limit) break;
      const end = boundary(u, u.painted, limit);
      u.el.append(chunk(u, u.painted, end));
      u.painted = end;
      S.dirty = true;
      yield end < limit ? HEAVY : 0;
    }
    u.task = false;
  }
  function paint(u) {
    if (u.task) return;
    u.task = true;
    tasks.push(paintGen(u));
    kick();
  }

  /* ---- scheduling ---- */
  function kick() { if (!raf && document.visibilityState === "visible") raf = requestAnimationFrame(frame); }
  function frame() {
    raf = 0;
    const t0 = performance.now();
    while (inHead < incoming.length && performance.now() - t0 < FRAME_MS) ingest(incoming[inHead++]);
    if (inHead > 2000) { incoming.splice(0, inHead); inHead = 0; }
    let heavy = false;
    while (tasks.length && !heavy && performance.now() - t0 < FRAME_MS) {
      const g = tasks.shift(), r = g.next();
      if (!r.done) { tasks.push(g); heavy = r.value === HEAVY; }
    }
    enforceCap();
    if (S.reveal) tryReveal();
    if (S.dirty && S.follow) toBottom();
    S.dirty = false;
    renderEmpty();
    if (inHead < incoming.length || tasks.length) kick();
  }
  function toBottom() { logEl.scrollTop = logEl.scrollHeight; }

  /* ---- events into cards and rows ---- */
  const KIND_TITLE = k => T("lg.k." + k);
  function addBlock(it, kind, t, title, att) {
    const b = { kind, attempt: att ?? null, el: mk("section", "rl-block"), units: [], count: null };
    b.el.dataset.k = kind;
    const h = mk("div", "rl-bh");
    h.append(mk("span", "rl-bt", title || KIND_TITLE(kind)), mk("span", "rl-bm", clock(t * 1000)));
    b.head = h;
    b.el.append(h);
    it.body.append(b.el);
    it.blocks.push(b);
    return b;
  }
  function blockUnit(it, b, text, cls) {
    const u = newUnit(b.el, cls);
    u.item = it;
    it.units.push(u);
    b.units.push(u);
    addText(u, text, it);
    return u;
  }
  const pairs = obj => Object.entries(obj).map(([k, v]) => `${k} ${v !== null && typeof v === "object" ? JSON.stringify(v) : v}`).join("   ");
  const lines = obj => Object.entries(obj).filter(([, v]) => v != null)
    .map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join("\n");

  function timingText(d) {
    const p = [];
    if (d.attempt != null) p.push(`attempt ${d.attempt}`);
    if (d.prompt_tokens != null) p.push(`prompt ${N(d.prompt_tokens)} tokens`);
    if (d.cached_tokens != null) p.push(`cached ${N(d.cached_tokens)}`);
    if (d.prefill_tokens != null) {
      const rate = d.prefill_s > 0 ? ` (${N(d.prefill_tokens / d.prefill_s)} tok/s)` : "";
      p.push(`prefill ${N(d.prefill_tokens)} tokens${d.prefill_s != null ? ` in ${secs(d.prefill_s)}` : ""}${rate}`);
    }
    if (d.first_token_s != null) p.push(`first token ${secs(d.first_token_s)}`);
    if (d.completion_tokens != null) p.push(`${N(d.completion_tokens)} tokens written`);
    if (d.tokens_per_s != null) p.push(`${nfmt(d.tokens_per_s, 1)} tok/s`);
    const known = new Set(["attempt", "prompt_tokens", "cached_tokens", "prefill_tokens", "prefill_s", "first_token_s", "completion_tokens", "tokens_per_s"]);
    for (const [k, v] of Object.entries(d)) if (!known.has(k) && v != null) p.push(`${k} ${typeof v === "object" ? JSON.stringify(v) : v}`);
    return p.join("  ·  ");
  }
  function gpuText(ev) {
    const d = ev.data || {}, p = [];
    p.push(`${stamp(ev.t)}  ${d.when || ""}`);
    for (const g of d.gpus || []) {
      const q = [`GPU${g.index}`];
      if (g.name) q.push(g.name);
      if (g.used_mib != null) q.push(`${N(g.used_mib)}${g.total_mib != null ? ` / ${N(g.total_mib)}` : ""} MiB`);
      if (g.util_pct != null) q.push(`${N(g.util_pct)}%`);
      if (g.temp_c != null) q.push(`${N(g.temp_c)} °C`);
      if (g.power_w != null) q.push(`${N(g.power_w)} W`);
      p.push(q.join(" "));
    }
    if (d.ram) p.push(`RAM ${N(d.ram.used_mib)} / ${N(d.ram.total_mib)} MiB`);
    if (d.cpu_pct != null) p.push(`CPU ${N(d.cpu_pct)}%`);
    return p.join("  |  ");
  }

  function newItem(type) {
    const it = { type, el: null, units: [], chars: 0, events: 0, dead: false };
    items.push(it);
    return it;
  }
  function place(it) {
    logEl.append(it.el);
    S.dirty = true;
  }
  function newCard(req, t) {
    const it = newItem("card");
    Object.assign(it, { req, t, blocks: [], last: null, gpu: null, err: false, cancel: false, done: false,
                        code: "", tps: null });
    it.el = mk("article", "rl-card");
    it.el.setAttribute("aria-label", T("lg.card", req));
    it.head = mk("header", "rl-head");
    it.hId = mk("span", "rl-id", req);
    it.hTime = mk("span", "rl-time", stamp(t));
    it.hEp = mk("span", "rl-ep");
    it.hClient = mk("span", "rl-meta");
    it.chip = mk("span", "rl-chip");
    it.hDur = mk("span", "rl-meta");
    it.hTps = mk("span", "rl-meta");
    it.head.append(it.hId, it.hTime, it.hEp, it.hClient, it.chip, it.hDur, it.hTps);
    it.body = mk("div", "rl-blocks");
    it.el.append(it.head, it.body);
    cards.set(req, it);
    chip(it);
    place(it);
    return it;
  }
  function chip(it) {
    const s = it.err ? "error" : it.cancel ? "cancelled" : it.done ? "done" : "running";
    it.chip.dataset.s = s;
    const label = T("lg.s." + s) + (s === "error" && it.code ? ` ${it.code}` : "");
    if (it.chip.textContent !== label) it.chip.textContent = label;
  }
  function cardEvent(it, ev) {
    const k = ev.kind, d = ev.data || {}, att = d.attempt ?? null;
    it.events++;
    S.kept++;
    it.chars += EVENT_COST;
    S.chars += EVENT_COST;
    if (k === "reasoning" || k === "content") {
      let b = it.last;
      if (!b || b.kind !== k || b.attempt !== att) {
        b = addBlock(it, k, ev.t, att != null ? `${KIND_TITLE(k)} - ${T("lg.attempt", att)}` : null, att);
        b.count = mk("span", "rl-bn");
        b.head.append(b.count);
        blockUnit(it, b, "");
        it.last = b;
      }
      addText(b.units[0], ev.text || "", it);
      b.count.textContent = T("lg.chars", N(b.units[0].text.length));
      return;
    }
    if (k === "gpu") {
      if (d.when === "tick") {
        if (!it.gpu) { it.gpu = addBlock(it, "gpu", ev.t); blockUnit(it, it.gpu, ""); }
        addText(it.gpu.units[0], (it.gpu.units[0].text ? "\n" : "") + gpuText(ev), it);
        return;
      }
      const b = addBlock(it, "gpu", ev.t);
      blockUnit(it, b, gpuText(ev));
      if (d.when === "start") it.gpu = b;
      it.last = b;
      return;
    }
    let b;
    switch (k) {
      case "request": {
        it.hEp.textContent = `${d.method || ""} ${d.endpoint || ""}`.trim();
        it.hClient.textContent = [d.client, d.stream ? "stream" : ""].filter(Boolean).join(" · ");
        b = addBlock(it, k, ev.t);
        const params = pairs({ ...(d.params || {}), ...(d.unparsed ? { unparsed: true } : {}) });
        if (params) blockUnit(it, b, params, "rl-params");
        blockUnit(it, b, body(ev.text || "", d.unparsed));
        break;
      }
      case "attempt": {
        const why = d.error || d.finish_reason || "";
        const title = d.ok ? T("lg.attempt.ok", att) : d.discarded ? T("lg.attempt.discarded", att, why) : T("lg.attempt.failed", att, why);
        b = addBlock(it, k, ev.t, title, att);
        b.el.dataset.ok = String(!!d.ok);
        blockUnit(it, b, lines({ finish_reason: d.finish_reason, error: d.error, retrying: d.retrying ? "yes" : null }));
        break;
      }
      case "repair":
        b = addBlock(it, k, ev.t, att != null ? `${KIND_TITLE(k)} - ${T("lg.attempt", att)}` : null, att);
        blockUnit(it, b, pairs({ path: d.path, cut: d.cut, valid_json: d.valid_json, repeated: d.repeated }), "rl-params");
        blockUnit(it, b, ev.text || "");
        break;
      case "timing":
        b = addBlock(it, k, ev.t);
        blockUnit(it, b, timingText(d));
        if (d.tokens_per_s != null) { it.tps = d.tokens_per_s; it.hTps.textContent = `${nfmt(it.tps, 1)} tok/s`; }
        break;
      case "finish": {
        it.done = true;
        if (d.finish_reason === "cancel") it.cancel = true;
        if (d.finish_reason === "error") it.err = true;
        const tm = d.timings || {};
        const rate = tm.predicted_per_second ?? it.tps;
        if (d.duration_s != null) it.hDur.textContent = secs(d.duration_s);
        if (rate != null) it.hTps.textContent = `${nfmt(rate, 1)} tok/s`;
        b = addBlock(it, k, ev.t);
        blockUnit(it, b, lines({ finish_reason: d.finish_reason, duration: d.duration_s != null ? secs(d.duration_s) : null,
                                 attempts: d.attempts, usage: d.usage, timings: d.timings }));
        break;
      }
      case "cancel":
        it.cancel = true;
        b = addBlock(it, k, ev.t);
        blockUnit(it, b, d.reason || "");
        break;
      case "error": {
        it.err = true;
        const m = /^(\d{3})\b/.exec(d.message || "");
        if (m) it.code = m[1];
        b = addBlock(it, k, ev.t);
        blockUnit(it, b, `${d.type || ""}${d.type ? ": " : ""}${d.message || ""}`);
        break;
      }
      case "engine": case "server":
        b = addBlock(it, k, ev.t, `${KIND_TITLE(k)} ${String(d.level || "info").toUpperCase()}`);
        blockUnit(it, b, ev.text || "");
        break;
      default:
        b = addBlock(it, k, ev.t, k);
        blockUnit(it, b, [d && Object.keys(d).length ? JSON.stringify(d) : "", ev.text || ""].filter(Boolean).join("\n"));
    }
    it.last = b;
    chip(it);
    S.dirty = true;
  }
  function body(t, unparsed) {       // a body sent on one line is shown indented when it is JSON
    if (unparsed || t.includes("\n") || t.length > 1e6 || !/^\s*[{[]/.test(t)) return t;
    try { return JSON.stringify(JSON.parse(t), null, 2); } catch (e) { return t; }
  }
  function rowEvent(ev) {
    const d = ev.data || {}, it = newItem("row");
    it.events = 1;
    S.kept++;
    const level = ["info", "warn", "err"].includes(d.level) ? d.level : ev.kind === "error" ? "err" : "info";
    it.el = mk("div", "rl-row");
    it.el.dataset.lv = level;
    it.el.append(mk("span", "rl-time", clock(ev.t * 1000)), mk("span", "rl-lv", level === "err" ? "ERR" : level.toUpperCase()),
                 mk("span", "rl-rk", ev.kind === "gpu" ? KIND_TITLE("gpu") : ev.kind === "engine" || ev.kind === "server" ? KIND_TITLE(ev.kind) : ev.kind));
    const u = newUnit(it.el);
    u.item = it;
    it.units.push(u);
    addText(u, ev.kind === "gpu" ? `${gpuText(ev)}${d.requests ? `  |  ${d.requests.join(", ")}` : ""}` : ev.text || (d && Object.keys(d).length ? JSON.stringify(d) : ""), it);
    it.chars += EVENT_COST;
    S.chars += EVENT_COST;
    place(it);
  }
  function ingest(ev) {
    S.dirty = true;
    if (!ev.req) {
      const d = ev.data || {};
      if (ev.kind === "gpu" && d.when === "tick") {
        const hit = (d.requests || []).map(r => cards.get(r)).filter(Boolean);
        if (hit.length) { hit.forEach(c => cardEvent(c, ev)); return afterIngest(); }
      }
      rowEvent(ev);
      return afterIngest();
    }
    cardEvent(cards.get(ev.req) || newCard(ev.req, ev.t), ev);
    afterIngest();
  }
  function afterIngest() { if (qEl.value) scheduleFind(FIND_AGAIN_MS, false); }

  function enforceCap() {
    let n = 0;
    while (S.chars > MEM_CAP && items.length > 1 && n < DROP_PER_FRAME) {
      const it = items.shift();
      S.chars -= it.chars;
      S.kept -= it.events;
      S.pageDropped += it.events;
      it.dead = true;
      it.el.remove();
      if (it.req) cards.delete(it.req);
      n++;
    }
    if (n) { renderDropNote(); if (qEl.value) scheduleFind(FIND_AGAIN_MS, false); }
    if (S.kept !== shownKept) renderStatus();
  }
  let dropNote = null;
  function renderDropNote() {
    if (!S.pageDropped) { dropNote?.remove(); dropNote = null; return; }
    if (!dropNote) { dropNote = mk("div", "rl-dropnote"); logEl.prepend(dropNote); }
    dropNote.textContent = T("lg.pagedropped", N(S.pageDropped));
  }
  let emptyEl = null;
  function renderEmpty() {
    const want = !items.length && S.enabled;
    if (want && !emptyEl) { emptyEl = mk("p", "rl-empty", T("lg.empty")); logEl.append(emptyEl); }
    else if (!want && emptyEl) { emptyEl.remove(); emptyEl = null; }
  }

  /* ---- find ---- */
  const countEl = el("rl-count");
  function allUnits() { const out = []; for (const it of items) for (const u of it.units) out.push(u); return out; }
  function scheduleFind(ms, fresh) {
    S.sfresh = S.sfresh || fresh;
    if (!qEl.value) return;
    if (fresh) { clearTimeout(S.stimer); S.stimer = 0; }
    if (S.stimer) return;
    S.stimer = setTimeout(() => { S.stimer = 0; const f = S.sfresh; S.sfresh = false; startFind(f); }, ms);
  }
  function startFind(fresh) {
    const q = qEl.value;
    S.sgen++;
    if (!q) { clearMatches(); return; }
    tasks.push(findGen(S.sgen, q, fresh));
    kick();
  }
  function* findGen(gen, q, fresh) {
    let re;
    try { re = new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "giu"); } catch (e) { return; }
    const found = [], per = new Map();
    let over = false;
    scan: for (const u of allUnits()) {
      if (gen !== S.sgen) return;
      re.lastIndex = 0;
      for (let m; (m = re.exec(u.text)); ) {
        if (found.length >= MATCH_CAP) { over = true; break scan; }
        const mm = { u, s: m.index, e: m.index + m[0].length, el: null };
        found.push(mm);
        (per.get(u) || per.set(u, []).get(u)).push(mm);
      }
      yield 0;
    }
    if (gen !== S.sgen) return;
    apply(found, per, over, fresh);
  }
  const sameMarks = (a, b) => a.length === b.length && a.every((m, i) => m.s === b[i].s && m.e === b[i].e);
  function apply(found, per, over, fresh) {
    const prevKey = cur ? `${cur.u.id}:${cur.s}` : null, prevIdx = curIdx;
    for (const u of new Set([...markedUnits, ...per.keys()])) {
      const now = per.get(u) || [];
      if (sameMarks(u.marks, now)) now.forEach((m, i) => { m.el = u.marks[i].el; });
      else { u.marks = now; u.reset = true; paint(u); }
      u.marks = now;
    }
    markedUnits = new Set(per.keys());
    matches = found;
    capped = over;
    let idx = -1;
    if (!fresh && prevKey) idx = matches.findIndex(m => `${m.u.id}:${m.s}` === prevKey);
    if (idx < 0 && matches.length) idx = fresh || prevIdx < 0 ? 0 : Math.min(prevIdx, matches.length - 1);
    select(idx, fresh);
  }
  function clearMatches() {
    for (const u of markedUnits) { u.marks = []; u.reset = true; paint(u); }
    markedUnits = new Set();
    matches = [];
    capped = false;
    S.reveal = null;
    select(-1, false);
  }
  function select(idx, scroll) {
    curIdx = idx;
    cur = idx >= 0 ? matches[idx] : null;
    logEl.querySelectorAll(".rl-cur").forEach(e => e.classList.remove("rl-cur"));
    countText();
    S.reveal = cur ? { m: cur, scroll } : null;
    if (cur && scroll) setFollow(false);
    if (cur && cur.e > limitOf(cur.u)) expand(cur.u);
    kick();
    if (cur) tryReveal();
  }
  function tryReveal() {
    const r = S.reveal;
    if (!r || r.m !== cur) { S.reveal = null; return; }
    if (r.m.u.item?.dead) { S.reveal = null; return; }
    const k = r.m.el;
    if (!k || !k.isConnected) return;
    k.classList.add("rl-cur");
    S.reveal = null;
    if (r.scroll) center(k);
  }
  function center(k) {
    const a = logEl.getBoundingClientRect(), b = k.getBoundingClientRect();
    const top = Math.max(0, logEl.scrollTop + (b.top - a.top) - (a.height - b.height) / 2);
    const near = Math.abs(top - logEl.scrollTop) < 3 * logEl.clientHeight;
    logEl.scrollTo({ top, behavior: reduced() || !near ? "auto" : "smooth" });
  }
  function countText() {
    const q = qEl.value;
    countEl.textContent = !q ? "" : cur ? T("lg.of", N(curIdx + 1), N(matches.length) + (capped ? "+" : "")) : T("lg.none");
  }
  function go(delta) {
    if (S.stimer) { scheduleFind(0, true); return; }
    if (!matches.length) return;
    select((curIdx + delta + matches.length) % matches.length, true);
  }
  qEl.addEventListener("input", () => {
    countEl.textContent = "";
    if (qEl.value) scheduleFind(FIND_WAIT_MS, true);
    else { clearTimeout(S.stimer); S.stimer = 0; startFind(true); }
  });
  qEl.addEventListener("keydown", e => {
    if (e.key === "Enter") { e.preventDefault(); go(e.shiftKey ? -1 : 1); }
    else if (e.key === "Escape") { e.preventDefault(); qEl.value = ""; clearTimeout(S.stimer); S.stimer = 0; startFind(true); }
  });
  el("rl-next").onclick = () => go(1);
  el("rl-prev").onclick = () => go(-1);

  /* ---- status line, pause, follow ---- */
  let shownKept = -1, infoKey = "";
  function setConn(c) { if (S.conn !== c) { S.conn = c; renderStatus(); } }
  function renderStatus() {
    const st = S.conn === "live" && S.paused ? "paused" : S.conn;
    const state = el("rl-state");
    state.dataset.s = st;
    state.textContent = T("lg.st." + (st === "wait" ? "wait" : st));
    shownKept = S.kept;
    el("rl-kept").textContent = T("lg.events", N(S.kept));
    const info = [];
    if (S.srv.events) info.push(T("lg.dropped", N(S.srv.events), N(S.srv.chars)));
    if (S.skipped) info.push(T("lg.skipped", N(S.skipped)));
    const key = info.join("|");
    if (key !== infoKey) {
      infoKey = key;
      el("rl-info").replaceChildren(...info.map(t => mk("span", null, t)));
    }
    const pill = el("rl-pending");
    pill.hidden = !(S.paused && S.pending.length);
    pill.textContent = T("lg.new", N(S.pending.length));
    el("rl-pause-t").textContent = T(S.paused ? "lg.resume" : "lg.pause");
    el("rl-pause").querySelector("use").setAttribute("href", S.paused ? "#rl-i-play" : "#rl-i-pause");
  }
  el("rl-pause").onclick = () => {
    S.paused = !S.paused;
    if (!S.paused) {
      for (const e of S.pending) incoming.push(e);
      S.pending = [];
      S.pendingChars = 0;
      S.dirty = true;
      kick();
    }
    renderStatus();
  };
  logEl.addEventListener("scroll", () => {
    if (logEl.clientHeight) S.top = logEl.scrollTop;        // a hidden panel forgets its position
    setFollow(logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40);
  });
  function setFollow(on) {
    S.follow = on;
    el("rl-latest").hidden = on;
  }
  el("rl-latest").onclick = () => { setFollow(true); toBottom(); };
  new ResizeObserver(() => { if (S.follow && logEl.clientHeight) toBottom(); }).observe(logEl);

  let msgTimer = 0;
  function say(m) {
    el("rl-msg").textContent = m;
    clearTimeout(msgTimer);
    msgTimer = setTimeout(() => { el("rl-msg").textContent = ""; }, 6000);
  }

  /* ---- reset, clear, save ---- */
  function resetView() {
    S.gen++;
    S.sgen++;
    clearTimeout(S.stimer);
    S.stimer = 0;
    tasks.length = 0;
    incoming.length = 0;
    inHead = 0;
    S.pending = [];
    S.pendingChars = 0;
    items.length = 0;
    cards.clear();
    logEl.replaceChildren();
    dropNote = emptyEl = null;
    matches = [];
    markedUnits = new Set();
    cur = null;
    curIdx = -1;
    capped = false;
    S.reveal = null;
    Object.assign(S, { kept: 0, chars: 0, pageDropped: 0, skipped: 0, srv: { events: 0, chars: 0 } });
    countText();
    setFollow(true);
    renderEmpty();
    renderStatus();
  }
  el("rl-clear").onclick = async () => {
    try {
      const r = await fetch("api/reqlog/clear", { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: "{}" });
      if (r.status === 401) { askKey(); return; }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      resetView();
      S.after = 0;
      S.epoch = null;
      say(T("lg.cleared"));
    } catch (e) {
      say(T("lg.clearfail", e.message));
    }
  };
  const fmtEl = el("rl-format");
  try { const f = localStorage.getItem(FORMAT_KEY); if (f === "text" || f === "jsonl") fmtEl.value = f; } catch (e) { /* storage blocked: Text */ }
  fmtEl.onchange = () => { try { localStorage.setItem(FORMAT_KEY, fmtEl.value); } catch (e) { /* kept for this page only */ } };
  el("rl-save").onclick = async () => {
    const btn = el("rl-save"), format = fmtEl.value === "jsonl" ? "jsonl" : "text";
    btn.disabled = true;
    try {
      const r = await fetch(`api/reqlog/export?format=${format}`, { cache: "no-store", headers: authHeaders() });
      if (r.status === 401) { askKey(); return; }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      const m = /filename="?([^";]+)"?/.exec(r.headers.get("Content-Disposition") || "");
      const d = new Date(), p = n => String(n).padStart(2, "0");
      const name = m ? m[1] : `basalt-requests-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}.${format === "jsonl" ? "jsonl" : "log"}`;
      const url = URL.createObjectURL(blob), a = mk("a");
      a.href = url;
      a.download = name;
      a.hidden = true;
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      say(T("lg.saved", name, blob.size >= 1 << 20 ? `${nfmt(blob.size / (1 << 20), 1)} MB` : `${nfmt(blob.size / 1024, 1)} KB`));
    } catch (e) {
      say(T("lg.savefail", e.message));
    } finally {
      btn.disabled = false;
    }
  };

  /* ---- polling: only while this view is shown ---- */
  let timer = 0, inflight = false, fails = 0, locked = false;
  const active = () => viewOn === "logs" && document.visibilityState === "visible" && !locked;
  function schedule(ms) {
    clearTimeout(timer);
    timer = active() ? setTimeout(poll, ms) : 0;
  }
  function offPanel(on) {
    S.enabled = !on;
    el("rl-off").hidden = !on;
    el("rl-main").hidden = on;
  }
  function take(d) {
    if (!d.enabled) { offPanel(true); return; }
    offPanel(false);
    if (S.epoch !== null && (d.epoch !== S.epoch || d.last < S.after)) {     // cleared elsewhere or the server restarted
      resetView();
      S.epoch = d.epoch;
      S.after = 0;
      return true;
    }
    if (S.epoch === null) S.epoch = d.epoch;
    S.skipped += d.skipped || 0;
    S.srv = d.dropped || S.srv;
    if (d.events.length) {
      if (S.paused) {
        for (const e of d.events) { S.pending.push(e); S.pendingChars += (e.text || "").length + EVENT_COST; }
        while (S.pendingChars > MEM_CAP && S.pending.length > 1) {      // the page keeps what it can hold, newest first
          const e = S.pending.shift();
          S.pendingChars -= (e.text || "").length + EVENT_COST;
          S.pageDropped++;
          renderDropNote();
        }
      } else {
        for (const e of d.events) incoming.push(e);
        kick();
      }
    }
    S.after = d.last;
    return false;
  }
  async function poll() {
    if (inflight || !active()) return;
    inflight = true;
    const gen = S.gen;
    let again = false;
    try {
      const r = await fetch(`api/reqlog?after=${S.after}`, { cache: "no-store", headers: authHeaders() });
      if (r.status === 401) { locked = true; setConn("locked"); askKey(); return; }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const d = await r.json();
      fails = 0;
      if (gen === S.gen) again = take(d);
      if (S.enabled) setConn("live");
      renderStatus();
    } catch (e) {
      fails++;
      setConn("off");
    } finally {
      inflight = false;
    }
    schedule(again ? 0 : !S.enabled ? OFF_POLL_MS : Math.min(POLL_MAX_MS, POLL_MS * 2 ** Math.min(fails, 5)));
  }
  function enter() {
    schedule(0);
    requestAnimationFrame(() => {
      if (S.follow) toBottom();
      else logEl.scrollTop = S.top;
    });
    kick();
  }
  function leave() {
    clearTimeout(timer);
    timer = 0;
  }
  document.addEventListener("basalt:view", e => (e.detail === "logs" ? enter() : leave()));
  document.addEventListener("basalt:key", () => { if (locked) { locked = false; if (active()) schedule(0); } });
  document.addEventListener("visibilitychange", () => { if (active() && !timer) schedule(0); kick(); });
  document.addEventListener("keydown", e => {
    if (viewOn === "logs" && e.key === "/" && !typing(e.target) && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); qEl.focus(); }
  });

  /* ---- start ---- */
  applyT(el("view-logs"));
  qEl.placeholder = T("lg.find.ph");
  el("rl-clear").title = T("lg.clear");
  el("rl-prev").title = T("lg.prev");
  el("rl-next").title = T("lg.next");
  renderStatus();
  renderEmpty();
  if (viewOn === "logs") enter();
})();
