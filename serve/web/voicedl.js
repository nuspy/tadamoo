"use strict";
/* The download of a voice model: the plan (size, free disk, license), the acknowledgements, the confirmation and the progress. One download
   at a time, shared by Models > Voice services (speech to text, speech synthesis, avatar) and Settings > Voice (the other services); each page
   gives the services it shows. A download starts only after the license is accepted and the person confirms. Everything the server says
   enters the page as text.

   window.VoiceDownload({ models: () => body of api/voice/models, ids: [service ids], root, runId, repaint, refresh, active }) returns
   { part(svc, m), runBox(), poll() }. */
window.VoiceDownload = function (cx) {
  const POLL_MS = 1000, GIB = 1e9, MIB = 1e6;
  const ui = {};                                      // per service: the plan shown, the acknowledgements, the line under the card
  const st = id => ui[id] || (ui[id] = { plan: null, planErr: "", checking: false, ack: false, nc: false, msg: "", bad: false });
  let timer = 0;

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick, id) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; if (id) b.id = id; return b; };
  const pick = o => (o && (LANG === "it" ? o.it : o.en)) || (o && o.en) || "";
  const errText = r => r.status === 0 ? T("vo.e.offline") : (r.data && r.data.error && (LANG === "it" && r.data.error.message_it ? r.data.error.message_it : r.data.error.message)) || `HTTP ${r.status}`;
  const gb = n => n >= GIB / 10 ? T("vo.size.gb", (n / GIB).toFixed(1)) : T("vo.size.mb", Math.max(1, Math.round(n / MIB)));
  function note(text, bad) {
    const n = el("p", "vo-note", text);
    n.setAttribute("role", bad ? "alert" : "status");
    n.dataset.bad = bad ? "1" : "";
    n.hidden = !text;
    return n;
  }
  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  /* ---- plan, license, confirm ---- */
  function part(svc, m) {
    const s = st(svc.id), box = el("div", "vo-dl"), S = cx.models();
    const busy = S.download && ["running", "verifying"].includes(S.download.phase);
    if (!m.downloadable) { box.append(el("p", "vo-help", T("vo.dl.unavailable"))); return box; }
    if (!s.plan && !s.checking) {
      const b = btn(T("vo.dl.button"), "primary", () => openPlan(svc, m), "vo-dl-open-" + svc.id);
      b.disabled = busy;
      box.append(b);
      if (busy) box.append(el("p", "vo-help", T("vo.dl.busy")));
      if (s.planErr) box.append(note(s.planErr, true));
      return box;
    }
    if (s.checking) { box.append(el("p", "vo-help", T("vo.dl.checking"))); return box; }
    box.append(planView(svc, m, s), note(s.msg, s.bad));
    return box;
  }
  async function openPlan(svc, m) {
    const s = st(svc.id);
    s.checking = true; s.planErr = ""; s.ack = s.nc = false;
    cx.repaint();
    const r = await api("GET", "api/voice/download/plan?id=" + encodeURIComponent(m.id));
    s.checking = false;
    if (r.ok) s.plan = r.data; else s.planErr = errText(r);
    cx.repaint();
  }
  function planView(svc, m, s) {
    const p = s.plan, box = el("div", "vo-plan");
    box.append(el("h4", "vo-h2", T("vo.dl.plan", m.name)),
      el("p", "vo-line", T("vo.dl.files", p.files.length, gb(p.remaining_bytes), gb(p.total_bytes))),
      el("p", "vo-line", T("vo.dl.dest", p.folder)));
    if (p.free_bytes != null) box.append(el("p", "vo-line", T("vo.dl.free", gb(p.free_bytes))));
    const lic = el("div", "vo-license");
    lic.append(el("h5", "vo-h3", T("vo.dl.license", p.license.name)), el("p", "vo-line", pick(p.license)));
    box.append(lic);
    if (p.problems.length) {
      const bad = el("div", "vo-problems");
      bad.setAttribute("role", "alert");
      bad.append(el("h5", "vo-h3", T("vo.dl.problems")));
      const ul = el("ul", "");
      p.problems.forEach(x => ul.append(el("li", "", (LANG === "it" ? x.text_it : x.text) || x.text)));
      bad.append(ul);
      box.append(bad);
    }
    const checks = el("div", "vo-checks"), ready = p.ok;
    const check = (id, text, key) => {
      const row = el("label", "vo-check"), input = el("input");
      input.type = "checkbox"; input.id = id; input.checked = s[key]; input.disabled = !ready;
      input.onchange = () => { s[key] = input.checked; cx.repaint(); };
      row.append(input, el("span", "", text));
      return row;
    };
    checks.append(check("vo-ack-" + svc.id, T("vo.dl.ack"), "ack"));
    if (p.noncommercial) checks.append(check("vo-nc-" + svc.id, T("vo.dl.nc"), "nc"));
    box.append(checks, el("p", "vo-help", T("vo.dl.nothing")));
    const bar = el("div", "vo-bar");
    const go = btn(T("vo.dl.confirm"), "primary", () => confirmDownload(svc, m), "vo-dl-go-" + svc.id);
    go.disabled = !ready || !s.ack || (p.noncommercial && !s.nc);
    bar.append(go, btn(T("vo.dl.cancel"), "ghost", () => { s.plan = null; s.ack = s.nc = false; cx.repaint(); }, "vo-dl-cancel-" + svc.id));
    box.append(bar);
    return box;
  }
  async function confirmDownload(svc, m) {
    const s = st(svc.id), p = s.plan;
    const r = await api("POST", "api/voice/download", { id: m.id, token: p.token, confirm: true, ack: s.ack, noncommercial: s.nc });
    if (r.ok) { s.plan = null; s.ack = s.nc = false; s.msg = ""; cx.models().download = r.data; cx.repaint(); poll(); return; }
    s.msg = errText(r); s.bad = true;
    if (r.data && r.data.plan) s.plan = r.data.plan;
    cx.repaint();
  }

  /* ---- the running download ---- */
  function runBox() {
    const S = cx.models(), d = S.download || {}, box = el("div", "vo-box vo-run");
    box.id = cx.runId;
    const mine = d.id && S.services.some(s => cx.ids.includes(s.id) && s.models.some(m => m.id === d.id));
    if (!mine || d.phase === "idle") { box.hidden = true; return box; }
    const pct = d.bytes_total ? Math.floor(100 * d.bytes_done / d.bytes_total) : 0;
    const running = d.phase === "running" || d.phase === "verifying";
    box.append(el("h3", "vo-h", running ? T("vo.dl.running", d.name) : d.name));
    if (running) {
      const bar = el("div", "vo-progress");
      bar.setAttribute("role", "progressbar"); bar.setAttribute("aria-valuemin", "0"); bar.setAttribute("aria-valuemax", "100"); bar.setAttribute("aria-valuenow", String(pct));
      bar.setAttribute("aria-label", d.name);
      const fill = el("div", "vo-fill"); fill.style.width = pct + "%";
      bar.append(fill);
      const line = [d.phase === "verifying" ? T("vo.dl.verifying") : T("vo.dl.pct", pct), d.speed_bps > 1 ? T("vo.dl.speed", (d.speed_bps / 1e6).toFixed(1)) : ""].filter(Boolean).join("  ");
      box.append(bar, el("p", "vo-line", line), btn(T("vo.dl.stop"), "ghost", async () => { await api("POST", "api/voice/download/cancel", {}); poll(); }, cx.runId + "-stop"));
    } else if (d.phase === "done") box.append(note(T("vo.dl.done"), false));
    else if (d.phase === "cancelled") box.append(note(T("vo.dl.stopped"), false));
    else if (d.phase === "failed") box.append(note(T("vo.dl.failed", d.error ? (LANG === "it" ? d.error.it : d.error.en) : ""), true));
    return box;
  }
  function paintRun() {
    const old = cx.root.querySelector(".vo-run");
    if (old) old.replaceWith(runBox()); else cx.repaint();
  }
  async function poll() {
    clearTimeout(timer);
    const S = cx.models();
    if (!cx.active() || !S) return;
    const r = await api("GET", "api/voice/download/status");
    if (!r.ok) return;
    const was = S.download && S.download.phase;
    S.download = r.data;
    const running = ["running", "verifying"].includes(r.data.phase);
    if (running) { paintRun(); timer = setTimeout(poll, POLL_MS); return; }
    if (["running", "verifying"].includes(was)) await cx.refresh();
  }
  function stop() { clearTimeout(timer); }

  return { part, runBox, poll, stop };
};
