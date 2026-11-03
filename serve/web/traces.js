"use strict";
/* LOGS view, second section: the traces of the Chat and Agent conversations. A conversation can be replayed with the recorded
   answers (nothing runs), saved as a JSONL file with secrets masked, and its audit ledger verified. Everything the server sent
   enters the page as text. */
(function () {
  const sel = document.getElementById("tr-sel");
  if (!sel) return;
  const el = id => document.getElementById(id);
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const S = { sessions: [], loaded: false };

  async function call(method, path, body) {
    let r;
    try {
      r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
    } catch (e) { return { ok: false, status: 0, data: {}, error: String(e.message || e) }; }
    if (r.status === 401) askKey();
    return { ok: r.ok, status: r.status, resp: r };
  }
  async function json(method, path, body) {
    const r = await call(method, path, body);
    if (r.resp) { try { r.data = await r.resp.json(); } catch (e) { r.data = {}; } }
    if (!r.ok && !r.error) r.error = (r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`;
    return r;
  }

  /* ---- the two sections of the view ---- */
  function show(which) {
    const traces = which === "traces";
    el("rl").hidden = traces;
    el("tr").hidden = !traces;
    el("tr-sw-req").setAttribute("aria-pressed", String(!traces));
    el("tr-sw-tr").setAttribute("aria-pressed", String(traces));
    if (traces) load();
  }
  el("tr-sw-req").onclick = () => show("requests");
  el("tr-sw-tr").onclick = () => show("traces");

  /* ---- the list ---- */
  const when = ts => (ts ? new Date(ts * 1000).toLocaleString(LANG === "it" ? "it-IT" : "en-GB", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "");
  function paintList() {
    const keep = sel.value;
    sel.replaceChildren(...S.sessions.map(s => {
      const o = document.createElement("option");
      o.value = s.id;
      o.textContent = `${s.title || T("tr.untitled")} · ${when(s.updated)} · ${s.id}`;
      return o;
    }));
    if (S.sessions.some(s => s.id === keep)) sel.value = keep;
    const none = S.sessions.length === 0;
    el("tr-none").hidden = !none;
    el("tr-cards").hidden = none;
    sel.disabled = none;
  }
  async function load() {
    const r = await json("GET", "api/agent/traces");
    S.sessions = r.ok ? r.data.sessions || [] : [];
    S.loaded = true;
    paintList();
  }
  el("tr-refresh").onclick = load;
  sel.onchange = () => { for (const id of ["tr-replay-out", "tr-export-out", "tr-ledger-out"]) el(id).replaceChildren(); };

  /* ---- a verdict: the state is a word as well as a colour ---- */
  function verdict(state, text) {
    const v = mk("div", "tr-verdict");
    v.dataset.s = state;
    v.append(mk("b", "", T("tr.state." + state)), mk("span", "", text));
    return v;
  }
  const busy = (button, on) => { button.disabled = on; };
  const text = v => (typeof v === "string" ? v : JSON.stringify(v, null, 2));

  /* ---- replay ---- */
  el("tr-replay").onclick = async () => {
    const out = el("tr-replay-out"), button = el("tr-replay");
    if (!sel.value) return;
    busy(button, true);
    out.replaceChildren(mk("p", "tr-help", T("tr.replay.busy")));
    const r = await json("POST", `api/agent/traces/${encodeURIComponent(sel.value)}/replay`, { mode: el("tr-mode").value, results: el("tr-results").checked });
    busy(button, false);
    if (!r.ok) { out.replaceChildren(verdict("bad", T("tr.replay.fail", r.error))); return; }
    const d = r.data;
    if (d.ok) { out.replaceChildren(verdict("ok", T("tr.replay.ok", d.replayed_calls))); return; }
    const div = d.divergence || {};
    const box = mk("div");
    box.append(verdict("bad", `${T("tr.replay.diff", div.index ?? "?")} ${T("tr.kind." + div.kind)}`));
    const two = mk("div", "tr-two");
    for (const [label, value] of [[T("tr.recorded"), div.recorded], [T("tr.replayed"), div.replayed]]) {
      const col = mk("div");
      col.append(mk("h4", "", label), mk("pre", "", value ? text(value) : "-"));
      two.append(col);
    }
    box.append(two);
    out.replaceChildren(box);
  };

  /* ---- export ---- */
  el("tr-export").onclick = async () => {
    const out = el("tr-export-out"), button = el("tr-export");
    if (!sel.value) return;
    busy(button, true);
    try {
      const r = await call("GET", `api/agent/traces/${encodeURIComponent(sel.value)}/export`);
      if (!r.ok) {
        let msg = r.error || `HTTP ${r.status}`;
        try { const d = await r.resp.json(); msg = (d.error && d.error.message) || msg; } catch (e) { /* no body */ }
        throw new Error(msg);
      }
      const blob = await r.resp.blob();
      const m = /filename="?([^";]+)"?/.exec(r.resp.headers.get("Content-Disposition") || "");
      const name = m ? m[1] : `tadamoo-trace-${sel.value}.jsonl`;
      const url = URL.createObjectURL(blob), a = mk("a");
      a.href = url;
      a.download = name;
      a.hidden = true;
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      out.replaceChildren(verdict("ok", T("tr.saved", name)));
    } catch (e) {
      out.replaceChildren(verdict("bad", T("tr.savefail", e.message)));
    } finally {
      busy(button, false);
    }
  };

  /* ---- the audit ledger ---- */
  function ledgerRow(row) {
    const li = mk("li", "tr-row");
    const out = row.outcome ? (STRINGS.en["tr.out." + row.outcome] ? T("tr.out." + row.outcome) : row.outcome) : "";
    li.append(mk("b", "", `#${row.index} ${row.tool || row.action}`));
    const bits = [out, row.permission, row.duration_ms != null ? `${row.duration_ms} ms` : "", row.time ? when(row.time) : ""].filter(Boolean);
    li.append(mk("small", "", bits.join(" · ")));
    if (row.arguments != null) li.append(mk("small", "", text(row.arguments)));
    if (row.reason) li.append(mk("small", "", row.reason));
    return li;
  }
  el("tr-verify").onclick = async () => {
    const out = el("tr-ledger-out"), button = el("tr-verify");
    if (!sel.value) return;
    busy(button, true);
    const r = await json("GET", `api/agent/traces/${encodeURIComponent(sel.value)}/audit`);
    busy(button, false);
    if (!r.ok) { out.replaceChildren(verdict("bad", T("tr.ledger.fail", r.error))); return; }
    const d = r.data, box = mk("div");
    if (d.status === "broken") {
      box.append(verdict("bad", T("tr.ledger.broken", d.problems_total)));
      box.append(mk("h4", "tr-sub", T("tr.ledger.problems")));
      const list = mk("ul", "tr-problems");
      d.problems.forEach(p => list.append(mk("li", "", p)));
      box.append(list);
    } else if (d.status === "intact") box.append(verdict("ok", T("tr.ledger.intact", d.count)));
    else box.append(verdict("note", T(d.status === "empty" ? "tr.ledger.empty" : "tr.ledger.missing")));
    if (d.rows && d.rows.length) {
      box.append(mk("h4", "tr-sub", T("tr.ledger.rows", d.rows.length, d.count)));
      const list = mk("ul", "tr-rows");
      d.rows.forEach(row => list.append(ledgerRow(row)));
      box.append(list);
    }
    out.replaceChildren(box);
  };

  applyT(el("tr-switch"));
  applyT(el("tr"));
  document.addEventListener("basalt:lang", () => { applyT(el("tr-switch")); applyT(el("tr")); if (S.loaded) paintList(); });
})();
