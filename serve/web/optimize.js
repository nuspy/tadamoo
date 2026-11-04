"use strict";
/* The Optimize prompt option: before a message reaches the model, the technical session rewrites it as a clearer instruction (tadamoo/optimize.py).
   There is one value; every place that hosts the box (the Chat toolbar, Settings > Inference) shows it, and it is saved through api/intake. The box
   can be ticked only while the server reports a technical session (v1/status, concurrency.side_slot): otherwise it is disabled, says why next to
   it, and its tooltip says how to turn the session on. A value saved while the session was there is kept when it goes, and the box then stays
   clickable for one thing only: switching it off. A host is an element with class "po" and the attribute data-po. */
(() => {
  const hosts = [];
  const RECHECK_MS = 1500;
  let sideTokens = 0, on = false, saved = false, loaded = false, saving = false, side = null, reachable = true, note = "", noteBad = false, checkedAt = 0;

  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  async function call(method, path, payload) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(payload ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: payload ? JSON.stringify(payload) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const why = r => (r.status === 0 ? T("kn.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`));

  /* ---- drawing ---- */
  function build(host, n) {
    const key = host.id;
    const input = h("input", "po-in"); input.type = "checkbox"; input.id = key + "-in";
    const box = h("label", "po-box", input, h("span", "po-text", T("po.title")));
    const reason = h("span", "po-why"); reason.id = key + "-why";
    const tip = h("span", "sr"); tip.id = key + "-tip";
    const status = h("span", "po-status"); status.setAttribute("role", "status");
    input.setAttribute("aria-describedby", tip.id + " " + reason.id);
    input.addEventListener("change", () => choose(side === true ? input.checked : false));      // without a session a click can only switch it off
    host.replaceChildren(box, reason, tip, status);
    host.dataset.built = String(n);
  }
  function paintOne(host) {
    if (host.dataset.built !== LANG) build(host, LANG);
    const input = host.querySelector(".po-in"), ok = side === true;
    input.disabled = !loaded || saving || (!ok && !on);                 // saved as on without a session: enabled, to switch it off
    input.checked = ok && on;
    input.indeterminate = loaded && !ok && on;                         // saved as on, but nothing is rewritten now
    host.dataset.off = loaded && input.disabled && !saving ? "1" : "";
    const tip = T("po.tip") + (ok ? "" : " " + T("po.how"));
    host.querySelector(".po-box").title = tip;
    input.title = tip;
    host.querySelector(".sr").textContent = tip;
    host.querySelector(".po-why").textContent = !loaded || side === null || ok ? "" : on ? T("po.why.kept") : reachable ? T("po.why.off") : T("po.why.offline");
    const status = host.querySelector(".po-status");
    status.textContent = note; status.dataset.bad = noteBad ? "1" : "";
  }
  const paint = () => hosts.forEach(paintOne);

  /* ---- the value ---- */
  async function choose(want) {
    if (!loaded || saving || want === on || (want && side !== true)) { paint(); return; }       // without a session it cannot be switched on
    on = want; note = ""; noteBad = false; saving = true;
    paint();
    const r = await call("POST", "api/intake", { optimize_prompt: want });
    saving = false;
    if (r.ok) { saved = r.data.optimize_prompt === true; on = saved; note = T("kn.saved"); } else {
      on = saved; note = r.status === 400 ? T("po.refused") : why(r); noteBad = true;
      if (r.status === 400) refresh(true);
    }
    paint();
  }
  function sync(view) {                                                // the values another part of the page read or saved
    if (!view || typeof view.optimize_prompt !== "boolean") return;
    if (!saving) on = view.optimize_prompt;
    saved = view.optimize_prompt; loaded = true;
    paint();
  }
  async function refresh(force) {                                      // whether the server reports a technical session
    const now = Date.now();
    if (!force && now - checkedAt < RECHECK_MS) return;
    checkedAt = now;
    const r = await call("GET", "v1/status");
    reachable = r.status !== 0;
    const slot = r.ok && r.data && r.data.concurrency ? r.data.concurrency.side_slot : null;
    side = !!(slot && Number(slot.context) > 0);
    const was = sideTokens;
    sideTokens = side ? Number(slot.context) : 0;
    paint();
    if (sideTokens !== was) document.dispatchEvent(new CustomEvent("basalt:side", { detail: sideTokens }));
  }
  async function load() {
    const r = await call("GET", "api/intake");
    if (r.ok) { sync(r.data); note = ""; noteBad = false; } else { note = why(r); noteBad = true; }
    paint();
    await refresh(true);
  }

  document.querySelectorAll(".po[data-po]").forEach(el => hosts.push(el));
  document.addEventListener("tadamoo:lang", paint);
  document.addEventListener("tadamoo:view", () => { if (!loaded) load(); else refresh(); });
  document.addEventListener("tadamoo:key", () => refresh(true));
  document.addEventListener("basalt:loader", () => refresh(true));      // the Loader finished a load: the technical session may have come or gone
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });

  window.OptimizePrompt = { value: () => on, available: () => side === true, sideTokens: () => sideTokens, sync, refresh, choose };
  paint();
  load();
})();
