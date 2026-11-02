"use strict";
/* Billion Context in the chat settings (GET/POST api/billion-context): the switch, the port of the local process, its
   state with the numbers it reports, and a restart. Saving a new port or pressing Restart starts the process again. */
(function () {
  const box = $("#cs-bc");
  const on = $("#cs-bc-on");
  const port = $("#cs-bc-port");
  const compress = $("#cs-bc-compress");
  const state = $("#cs-bc-state");
  let timer = 0;

  const say = (text, kind) => { state.textContent = text; state.dataset.kind = kind || ""; };
  const num = n => new Intl.NumberFormat(LANG === "it" ? "it-IT" : "en-US").format(n);

  async function call(body) {
    const opts = body === undefined ? { cache: "no-store", headers: authHeaders() }
      : { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(body) };
    const r = await fetch("api/billion-context", opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((j.error && j.error.message) || `HTTP ${r.status}`);
    return j;
  }

  function text(v) {
    const base = T("bc.st." + v.status, v.detail);
    const s = v.stats;
    if (v.status !== "running" || !s) return base;
    const more = s.context_tokens != null && s.context_window ? T("bc.stats", num(s.sessions), num(s.requests), num(s.context_tokens), num(s.context_window)) : T("bc.stats.few", num(s.sessions), num(s.requests));
    return `${base} ${more}`;
  }

  function paint(v, keepForm) {
    if (!keepForm) { on.checked = v.enabled; port.value = v.port; compress.checked = !!v.compress; }
    say(text(v), v.status === "error" || v.status === "not_installed" ? "error" : "");
  }

  async function load(keepForm) {
    try { paint(await call(), keepForm); } catch (e) { say(T("bc.failed", e.message), "error"); }
  }

  async function save(extra) {
    try {
      paint(await call({ enabled: on.checked, port: port.value, compress: compress.checked, ...extra }));
      setTimeout(() => load(true), 1500);               // the process takes a moment to answer
    } catch (e) { say(T("bc.failed", e.message), "error"); }
  }

  on.addEventListener("change", () => save());
  compress.addEventListener("change", () => save());
  $("#cs-bc-save").addEventListener("click", () => save());
  $("#cs-bc-restart").addEventListener("click", () => save({ restart: true }));
  $("#cs-open").addEventListener("click", () => {
    load(false);
    clearInterval(timer);
    timer = setInterval(() => { if ($("#chatset").hidden) clearInterval(timer); else load(true); }, 5000);
  });
  applyT(box);
})();
