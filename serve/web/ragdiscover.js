"use strict";
/* Knowledge > add from a request in words: you write what you want, the server searches the web and the technical session says which results are likely to hold it. The list
   has a check box for each result (the ones judged useful are checked); "Widen the search" follows the links of the pages found and offers the new ones; "Add selected" and
   "Add all" make each address a web source of the archive and read it. Everything the server and the web say is put in the page as text. */
(() => {
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };
  const why = r => (r.status === 0 ? T("rdc.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`));
  const POLL_MS = 700;
  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  const states = {};                                                  // by archive: the request, the results, which are checked, what is running and what the page says
  let current = null, currentArchive = "", context = {};
  const stateOf = aid => (states[aid] = states[aid] || { query: "", items: [], checked: new Set(), busy: "", job: "", note: "", bad: false, depth: "0" });

  function paint() { if (current && current.isConnected) fill(current, currentArchive); }

  async function run(aid, label, path, body, take) {
    const s = stateOf(aid);
    s.busy = label; s.note = ""; s.bad = false; paint();
    const start = await api("POST", path, body);
    if (!start.ok) { s.busy = ""; s.note = why(start); s.bad = true; paint(); return null; }
    let job = start.data.job;
    s.job = job ? job.id : "";
    while (job && job.state === "running") {
      await new Promise(r => setTimeout(r, POLL_MS));
      const r = await api("GET", `api/rag/jobs/${encodeURIComponent(job.id)}`);
      if (!r.ok) { s.busy = ""; s.job = ""; s.note = why(r); s.bad = true; paint(); return null; }
      job = r.data.job;
      s.note = job.stage ? T("rdc.stage", job.stage) : "";
      paint();
    }
    s.busy = ""; s.job = "";
    if (job && job.state === "done") { s.note = ""; take(job.result, start.data); }
    else { s.note = !job ? "" : job.state === "cancelled" ? T("rdc.cancelled") : (job.error || T("rdc.failed")); s.bad = !!job && job.state !== "cancelled"; }
    paint();
    return job;
  }

  const search = async aid => {
    const s = stateOf(aid), q = s.query.trim();
    if (!q) { s.note = T("rdc.empty"); s.bad = true; paint(); return; }
    await run(aid, "search", `api/rag/archives/${encodeURIComponent(aid)}/discover`, { query: q, limit: 10 }, got => {
      s.items = got.items; s.checked = new Set(got.items.filter(i => i.relevant !== false).map(i => i.url));
      if (!got.items.length) { s.note = T("rdc.none"); }
    });
  };
  const widen = async aid => {
    const s = stateOf(aid);
    await run(aid, "widen", `api/rag/archives/${encodeURIComponent(aid)}/discover/more`, { query: s.query.trim(), items: s.items }, got => {
      s.items = s.items.concat(got.items); got.items.forEach(i => s.checked.add(i.url));
      s.note = got.items.length ? "" : T("rdc.nothingnew");
    });
  };
  const addThese = async (aid, urls) => {
    const s = stateOf(aid);
    if (!urls.length) { s.note = T("rdc.pick"); s.bad = true; paint(); return; }
    const depth = Math.max(0, Math.min(10, parseInt(s.depth, 10) || 0));
    const job = await run(aid, "import", `api/rag/archives/${encodeURIComponent(aid)}/discover/import`, { urls, depth }, () => {});
    if (job && job.state === "done") { s.items = []; s.checked = new Set(); s.note = T("rdc.added", urls.length); s.bad = false; }
    if (context.reload) await context.reload();
    paint();
  };
  const cancel = async aid => {
    const s = stateOf(aid);
    if (s.job) await api("POST", `api/rag/jobs/${encodeURIComponent(s.job)}/cancel`, {});
    else { s.items = []; s.checked = new Set(); s.note = ""; s.bad = false; paint(); }
  };

  function resultRow(aid, item) {
    const s = stateOf(aid), li = el("li", "rd-item"), lab = el("label", "rd-pick");
    const c = el("input"); c.type = "checkbox"; c.checked = s.checked.has(item.url); c.disabled = !!s.busy;
    c.id = "rd-c-" + item.id;
    c.onchange = () => { if (c.checked) s.checked.add(item.url); else s.checked.delete(item.url); paint(); };
    const verdict = item.relevant === true ? T("rdc.yes") : item.relevant === false ? T("rdc.no") : T("rdc.unjudged");
    const body = el("span", "rd-body");
    body.append(el("span", "rd-title", item.title || item.url), el("span", "rd-url", item.url));
    if (item.snippet) body.append(el("span", "rg-help", item.snippet));
    body.append(el("span", "rg-help", [verdict, item.reason, item.via ? T("rdc.via", item.via) : ""].filter(Boolean).join(" · ")));
    lab.append(c, body);
    li.append(lab);
    return li;
  }

  function fill(box, aid) {
    const s = stateOf(aid);
    const q = el("input"); q.type = "text"; q.id = "rd-query"; q.value = s.query; q.maxLength = 600; q.autocomplete = "off"; q.disabled = !!s.busy;
    q.placeholder = T("rdc.ph");
    q.oninput = () => { s.query = q.value; };
    q.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); s.query = q.value; search(aid); } };
    const go = btn(T("rdc.search"), "primary", () => { s.query = q.value; search(aid); }); go.id = "rd-search"; go.disabled = !!s.busy;
    const lab = el("label", "", T("rdc.label")); lab.htmlFor = "rd-query";
    const row = el("div", "rg-row"); row.append(q, go);
    const parts = [el("h4", "rg-h2", T("rdc.title")), el("p", "rg-help", T("rdc.help")), lab, row];
    if (s.busy) {
      const wait = el("p", "rg-line", T("rdc." + s.busy) + (s.note ? " " + s.note : "")); wait.setAttribute("role", "status");
      parts.push(wait, btn(T("rdc.cancel"), "", () => cancel(aid)));
    } else if (s.note) { const n = el("p", s.bad ? "rg-err" : "rg-line", s.note); n.setAttribute("role", s.bad ? "alert" : "status"); parts.push(n); }
    if (s.items.length) {
      const ul = el("ul", "rd-list");
      s.items.forEach(i => ul.append(resultRow(aid, i)));
      const depth = el("input"); depth.type = "number"; depth.id = "rd-depth"; depth.min = "0"; depth.max = "10"; depth.value = s.depth; depth.inputMode = "numeric";
      depth.oninput = () => { s.depth = depth.value; };
      const dl = el("label", "", T("rdc.depth")); dl.htmlFor = "rd-depth";
      const picked = s.items.filter(i => s.checked.has(i.url)).map(i => i.url);
      const bar = el("div", "rg-bar");
      const sel = btn(T("rdc.addsel", picked.length), "primary", () => addThese(aid, picked)); sel.id = "rd-addsel"; sel.disabled = !!s.busy || !picked.length;
      const all = btn(T("rdc.addall", s.items.length), "", () => addThese(aid, s.items.map(i => i.url))); all.id = "rd-addall"; all.disabled = !!s.busy;
      const more = btn(T("rdc.widen"), "", () => widen(aid)); more.id = "rd-widen"; more.disabled = !!s.busy;
      const no = btn(T("rdc.cancel"), "", () => cancel(aid)); no.id = "rd-cancel"; no.disabled = !!s.busy;
      bar.append(sel, all, more, no);
      parts.push(ul, dl, depth, el("p", "rg-help", T("rdc.depth.help")), bar);
    }
    box.replaceChildren(...parts);
  }

  window.ragDiscover = {
    block(a, ctx) {
      context = ctx || {};
      const box = el("section", "rg-block");
      box.setAttribute("aria-label", T("rdc.title"));
      current = box; currentArchive = a.id;
      fill(box, a.id);
      return box;
    },
  };
})();
