"use strict";
/* Branches: the conversations placed under the one each was branched from (an indented tree a keyboard and a screen reader can walk),
   two continuations of one point run one after the other and shown side by side, and a safe preview of the files the agent made.
   Everything the server or the agent wrote enters the page through text nodes; the preview of a page runs in a sandboxed frame
   with no scripts, and a diagram is shown as its source. */
(() => {
  const dlg = document.getElementById("branches"), openBtn = document.getElementById("branches-open");
  if (!dlg || !openBtn) return;
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = mk("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };
  const $b = sel => dlg.querySelector(sel);
  const POLL_MS = 2000, PREVIEW_CHARS = 200000, PREVIEW_BYTES = 4000000;
  let timer = 0, picked = "";

  async function api(path, method, body) {
    try {
      const r = await fetch("api/agent/sessions" + path, { method: method || "GET", cache: "no-store",
        headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let j = {};
      try { j = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, json: j };
    } catch (e) { return { ok: false, status: 0, json: {} }; }
  }
  const errText = r => (r.json && r.json.error && r.json.error.message) || `HTTP ${r.status}`;
  const current = () => (window.agentChat && window.agentChat.current ? window.agentChat.current() : "");
  const say = (text, bad) => { const s = $b("#br-status"); s.textContent = text || ""; s.dataset.bad = bad ? "1" : ""; };

  /* ---- the tree: one tab stop, arrows walk it, Enter opens the conversation ---- */
  const tree = $b("#br-tree");
  function label(n, here) {
    const bits = [n.title, T("ch.state." + n.state), T("br.requests", n.turns || 0)];
    if (n.branched_from) bits.push(n.branched_from.turn ? T("br.from", n.branched_from.turn, n.branched_from.request || "") : T("br.orphan"));
    if (n.compare) bits.push(T("br.side", n.compare.side.toUpperCase()));
    (n.rewinds || []).slice(-1).forEach(r => bits.push(T("br.rewound", r.turn, r.was) + (r.still_done ? " " + T("br.stays", r.still_done) : "")));
    if (here) bits.push(T("br.current"));
    return bits.join(", ");
  }
  function item(n, level, here) {
    const li = mk("li"); li.setAttribute("role", "treeitem"); li.setAttribute("aria-level", String(level)); li.tabIndex = -1; li.dataset.id = n.id;
    li.setAttribute("aria-selected", String(n.id === here)); li.setAttribute("aria-label", label(n, n.id === here));
    const kids = n.children || [];
    if (kids.length) li.setAttribute("aria-expanded", "true");
    const row = mk("div", "brd-row");
    const tw = mk("span", "brd-tw", kids.length ? "▾" : "▪"); tw.setAttribute("aria-hidden", "true");
    tw.onclick = e => { if (kids.length) { e.stopPropagation(); toggle(li); } };
    const head = mk("span", "brd-t"); head.append(mk("b", "", n.title), mk("span", "brd-dim", ` · ${T("ch.state." + n.state)} · ${T("br.requests", n.turns || 0)}`));
    if (n.id === here) head.append(mk("span", "brd-tag", T("br.current")));
    if (n.compare) head.append(mk("span", "brd-tag", T("br.side", n.compare.side.toUpperCase())));
    if (n.copy) head.append(mk("span", "brd-tag", T("br.copy")));
    row.append(tw, head);
    if (n.branched_from) row.append(mk("span", "brd-sub", n.branched_from.turn ? T("br.from", n.branched_from.turn, n.branched_from.request || "") : T("br.orphan")));
    (n.rewinds || []).slice(-3).forEach(r => row.append(mk("span", "brd-sub", T("br.rewound", r.turn, r.was) + (r.still_done ? " — " + T("br.stays", r.still_done) : ""))));
    row.onclick = () => { setFocus(li); open(n.id); };
    li.append(row);
    if (kids.length) { const g = mk("ul"); g.setAttribute("role", "group"); kids.forEach(k => g.append(item(k, level + 1, here))); li.append(g); }
    return li;
  }
  const visible = () => [...tree.querySelectorAll('[role="treeitem"]')].filter(li => !li.parentElement.closest('[role="treeitem"][aria-expanded="false"]'));
  function setFocus(li) {
    tree.querySelectorAll('[role="treeitem"][tabindex="0"]').forEach(x => { x.tabIndex = -1; });
    li.tabIndex = 0; li.focus();
  }
  function toggle(li, want) {
    const was = li.getAttribute("aria-expanded");
    if (was == null) return;
    const on = want == null ? was !== "true" : want;
    li.setAttribute("aria-expanded", String(on));
    li.querySelector(":scope > ul").hidden = !on;
    li.querySelector(".brd-tw").textContent = on ? "▾" : "▸";
  }
  tree.addEventListener("keydown", e => {
    const li = e.target.closest('[role="treeitem"]');
    if (!li || e.altKey || e.ctrlKey || e.metaKey) return;
    const all = visible(), i = all.indexOf(li), expanded = li.getAttribute("aria-expanded");
    const go = x => { if (x) { e.preventDefault(); setFocus(x); } };
    if (e.key === "ArrowDown") go(all[i + 1]);
    else if (e.key === "ArrowUp") go(all[i - 1]);
    else if (e.key === "Home") go(all[0]);
    else if (e.key === "End") go(all[all.length - 1]);
    else if (e.key === "ArrowRight") { e.preventDefault(); if (expanded === "false") toggle(li, true); else if (expanded === "true") go(li.querySelector(':scope > ul > [role="treeitem"]')); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); if (expanded === "true") toggle(li, false); else go(li.parentElement.closest('[role="treeitem"]')); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(li.dataset.id); }
  });
  function open(id) {
    dlg.close();
    document.dispatchEvent(new CustomEvent("basalt:open-session", { detail: { id } }));
  }
  async function drawTree() {
    const r = await api("/tree");
    if (!r.ok) { say(errText(r), true); return; }
    const here = current(), roots = r.json.roots || [];
    tree.replaceChildren(...roots.map(n => item(n, 1, here)));
    $b("#br-empty").hidden = roots.length > 0;
    const first = tree.querySelector('[aria-selected="true"]') || tree.querySelector('[role="treeitem"]');
    if (first) first.tabIndex = 0;
  }

  /* ---- two continuations from one point of the open conversation ---- */
  const field = (id, name, el) => { const w = mk("label", "brd-f"); w.htmlFor = id; el.id = id; w.append(mk("span", "", name), el); return w; };
  function variantBox(key, title) {
    const box = mk("fieldset", "brd-v"); box.append(mk("legend", "", title));
    const text = mk("textarea"); text.rows = 3;
    const mode = mk("select"); [["", T("br.cmp.same")], ["default", "default"], ["plan", "plan"], ["read_only", "read_only"]].forEach(([v, t]) => mode.append(Object.assign(mk("option", "", t), { value: v })));
    const steps = mk("input"); steps.type = "number"; steps.min = "1"; steps.max = "50"; steps.placeholder = T("br.cmp.same");
    box.append(field(`br-${key}-text`, T("br.cmp.text"), text), field(`br-${key}-mode`, T("br.cmp.mode"), mode), field(`br-${key}-steps`, T("br.cmp.steps"), steps));
    box.value = () => ({ text: text.value, ...(mode.value ? { mode: mode.value } : {}), ...(steps.value ? { max_turns: Number(steps.value) } : {}) });
    return box;
  }
  let boxA, boxB, pointSel;
  async function drawForm() {
    const form = $b("#br-form"), sid = current();
    form.replaceChildren();
    let asked = [];
    if (sid) { const h = await api(`/${encodeURIComponent(sid)}/history`); if (h.ok) asked = (h.json.messages || []).filter(m => m.role === "user"); }
    $b("#br-cmp-none").hidden = asked.length > 0;
    if (!asked.length) return;
    pointSel = mk("select");
    asked.forEach((m, i) => pointSel.append(Object.assign(mk("option", "", `${i + 1}: ${String(m.content || "").replace(/\s+/g, " ").slice(0, 70)}`), { value: String(i + 1) })));
    pointSel.value = String(asked.length);
    boxA = variantBox("a", T("br.cmp.a")); boxB = variantBox("b", T("br.cmp.b"));
    const go = btn(T("br.cmp.go"), "primary", async () => {
      const r = await api(`/${encodeURIComponent(sid)}/compare`, "POST", { turn: Number(pointSel.value), variants: [boxA.value(), boxB.value()] });
      if (!r.ok) { say(errText(r), true); return; }
      say(T("br.cmp.started")); picked = r.json.compare.id; await drawCompares(); schedule();
    });
    const grid = mk("div", "brd-vs"); grid.append(boxA, boxB);
    form.append(field("br-point", T("br.cmp.point"), pointSel), grid, go);
  }
  async function drawCompares() {
    const sid = current(), list = $b("#br-cmps"), r = await api("/compares");
    if (!r.ok) return;
    const mine = (r.json.compares || []).filter(c => c.from.session === sid);
    list.replaceChildren(...mine.map(c => {
      const b = btn(`${c.sides.map(s => s.label || "").filter(Boolean).join(" / ") || c.id} · ${T("br.cmp.state." + c.state)} · ${new Date(c.created * 1000).toLocaleString()}`, "ghost", () => { picked = c.id; drawResult(); });
      b.setAttribute("aria-pressed", String(c.id === picked)); return b;
    }));
    if (!picked && mine.length) picked = mine[0].id;
    if (picked) await drawResult();
    return mine.some(c => c.state === "running");
  }
  function diffBlock(text) {
    const pre = mk("div", "brd-diff"); pre.tabIndex = 0;
    String(text || "").split("\n").forEach(line => pre.append(mk("div", line.startsWith("+") && !line.startsWith("+++") ? "d-add" : line.startsWith("-") && !line.startsWith("---") ? "d-del" : "d-ctx", line || " ")));
    return pre;
  }
  async function drawResult() {
    const out = $b("#br-result");
    if (!picked) { out.replaceChildren(); return; }
    const r = await api(`/compares/${encodeURIComponent(picked)}`);
    if (!r.ok) { out.replaceChildren(mk("p", "brd-dim", errText(r))); return; }
    const res = r.json, cols = mk("div", "brd-vs");
    res.sides.forEach((s, i) => {
      const col = mk("section", "brd-col"), tag = "AB"[i];
      col.setAttribute("aria-label", T("br.side", tag));
      col.append(mk("h4", "", `${T("br.side", tag)}${s.label ? " — " + s.label : ""}`), mk("p", "brd-dim", T("br.side.state." + s.state) + (s.error ? ": " + s.error : "")));
      col.append(mk("p", "brd-ask", s.text));
      const ans = mk("div", "brd-ans", s.answer || T("br.cmp.noanswer")); ans.tabIndex = 0; col.append(ans);
      if (s.session) col.append(btn(T("br.open.n", tag), "ghost", () => open(s.session)));
      cols.append(col);
    });
    out.replaceChildren(cols);
    if (res.answers_diff) out.append(mk("h4", "", T("br.cmp.diff")), diffBlock(res.answers_diff));
    if (res.files === null) { if (res.state === "done") out.append(mk("p", "brd-dim", T("br.cmp.nocopy"))); return; }
    out.append(mk("h4", "", T("br.cmp.files")));
    if (!res.files.length) out.append(mk("p", "brd-dim", T("br.cmp.nofiles")));
    const ul = mk("ul", "brd-files");
    res.files.forEach(f => {
      const li = mk("li"), d = mk("details");
      const st = x => x ? T("wc." + x) : T("br.cmp.notin");
      d.append(mk("summary", "", `${f.path} — A: ${st(f.a)}, B: ${st(f.b)} — ${f.same ? T("br.cmp.file.same") : T("br.cmp.file.differ")}`), diffBlock(f.diff || ""));
      li.append(d); ul.append(li);
    });
    out.append(ul);
  }
  function schedule() {
    clearTimeout(timer);
    if (!dlg.open) return;
    timer = setTimeout(async () => { const running = await drawCompares(); if (running) schedule(); }, POLL_MS);
  }

  /* ---- the files the agent made: a preview that never runs them ---- */
  const TEXTS = /\.(txt|log|csv|tsv|json|jsonl|xml|yaml|yml|toml|ini|py|js|ts|c|h|cpp|cu|rs|go|java|sh|ps1|bat|sql|css)$/i;
  const IMAGES = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp", bmp: "image/bmp", svg: "image/svg+xml" };
  const ext = name => (/\.([A-Za-z0-9]+)$/.exec(name) || [, ""])[1].toLowerCase();
  let shownUrl = "";
  async function preview(sid, f) {
    const box = $b("#br-prev");
    if (shownUrl) { URL.revokeObjectURL(shownUrl); shownUrl = ""; }
    box.replaceChildren(mk("p", "brd-dim", T("br.art.loading")));
    const e = ext(f.name), kind = IMAGES[e] ? "image" : (e === "md" || e === "markdown") ? "markdown" : (e === "html" || e === "htm") ? "html" : (e === "mmd" || e === "mermaid") ? "diagram" : TEXTS.test(f.name) ? "text" : "";
    if (!kind) { box.replaceChildren(mk("p", "brd-dim", T("br.art.none"))); return; }
    if ((f.size || 0) > PREVIEW_BYTES) { box.replaceChildren(mk("p", "brd-dim", T("br.art.big"))); return; }
    try {
      const r = await fetch(`api/outputs/${encodeURIComponent(sid)}/${encodeURIComponent(f.id)}`, { headers: authHeaders() });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const blob = await r.blob();
      if (kind === "image") {
        shownUrl = URL.createObjectURL(new Blob([blob], { type: IMAGES[e] }));
        const img = mk("img", "brd-img"); img.alt = f.name; img.src = shownUrl; box.replaceChildren(img); return;
      }
      const text = (await blob.text()).slice(0, PREVIEW_CHARS);
      if (kind === "markdown" && window.chatUi) { const d = mk("div", "answer brd-md"); window.chatUi.renderMarkdown(d, text); box.replaceChildren(d); }
      else if (kind === "html") {
        // no scripts, no forms, no popups, no origin of its own, and no network from inside the page
        const csp = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:";
        const frame = mk("iframe", "brd-frame"); frame.setAttribute("sandbox", ""); frame.setAttribute("referrerpolicy", "no-referrer"); frame.setAttribute("csp", csp);
        frame.title = T("br.art.frame", f.name);
        frame.srcdoc = `<meta http-equiv="Content-Security-Policy" content="${csp}">` + text;
        box.replaceChildren(frame);
      } else {
        const pre = mk("pre", "brd-pre", text); pre.tabIndex = 0;
        box.replaceChildren(...(kind === "diagram" ? [mk("p", "brd-dim", T("br.art.diagram"))] : []), pre);
      }
    } catch (err) { box.replaceChildren(mk("p", "brd-dim", T("br.art.fail", f.name))); }
  }
  async function drawFiles() {
    const sid = current(), list = $b("#br-files");
    $b("#br-prev").replaceChildren();
    list.replaceChildren();
    if (!sid) { $b("#br-art-none").hidden = false; return; }
    let items = [];
    try { const r = await fetch(`api/outputs?session=${encodeURIComponent(sid)}`, { headers: authHeaders() }); if (r.ok) items = (await r.json()).items || []; } catch (e) { /* none shown */ }
    $b("#br-art-none").hidden = items.length > 0;
    items.forEach(f => {
      const li = mk("li"), b = btn(T("br.art.preview"), "ghost", () => preview(sid, f));
      b.setAttribute("aria-label", T("br.art.preview.n", f.name));
      li.append(mk("span", "brd-fn", f.name), mk("span", "brd-dim", ` ${Math.max(1, Math.round((f.size || 0) / 1000))} kB`), b);
      list.append(li);
    });
  }

  /* ---- the dialog ---- */
  async function show() {
    say("");
    if (!dlg.open) dlg.showModal();
    openBtn.setAttribute("aria-expanded", "true");
    await Promise.all([drawTree(), drawForm(), drawFiles()]);
    const running = await drawCompares();
    if (running) schedule();
    const t = tree.querySelector('[tabindex="0"]');
    (t || $b("#br-close")).focus();
  }
  openBtn.onclick = show;
  $b("#br-close").onclick = () => dlg.close();
  dlg.addEventListener("close", () => { clearTimeout(timer); openBtn.setAttribute("aria-expanded", "false"); if (shownUrl) { URL.revokeObjectURL(shownUrl); shownUrl = ""; } openBtn.focus(); });
  applyT(dlg);
})();
