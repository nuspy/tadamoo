"use strict";
/* Private conversations and "forget this conversation", in the chat header. A private conversation is left out of the index of earlier
   conversations and nothing is learned from it for the person's memories. Forget removes what was already taken from the conversation (its
   pieces in the index and the memories learned from it); it needs a second press. In a conversation with the agent the mark is kept with
   the conversation on the server; in the model-only chat it is kept by that page. Before the first message there is no conversation yet:
   the mark waits and goes with the request that creates it. */
(() => {
  const priv = document.getElementById("priv-toggle"), forget = document.getElementById("forget-open");
  if (!priv || !forget) return;
  const ARM_MS = 4000;
  let waiting = false, armed = false, armTimer = 0, lastSid = "";
  const status = t => { const s = document.getElementById("chat-status"); if (s) s.textContent = t; };
  const errText = (r, j) => (j && j.error && j.error.message) || `HTTP ${r.status}`;
  const agent = () => (window.agentChat && window.agentChat.active() ? window.agentChat : null);
  const current = () => { const a = agent(); return a ? a.current() : ""; };
  const isOn = () => {
    const a = agent();
    if (!a) return !!(window.chatUi && window.chatUi.privacy && window.chatUi.privacy.get());
    const rec = a.record();
    return current() ? !!(rec && rec.private) : waiting;
  };

  async function call(path, body) {
    try {
      const r = await fetch("api/agent/sessions/" + path, { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(body || {}) });
      let j = {};
      try { j = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, json: j };
    } catch (e) { return { ok: false, status: 0, json: {} }; }
  }

  function paint() {
    const on = isOn(), a = agent();
    priv.textContent = T(on ? "priv.on" : "priv.off");
    priv.title = T(on ? "priv.tip.on" : "priv.tip.off");
    priv.setAttribute("aria-pressed", String(on));
    forget.textContent = T(armed ? "priv.forget.sure" : "priv.forget");
    forget.title = T("priv.forget.tip");
    forget.disabled = !!a && !current();
    forget.classList.toggle("me-warn", armed);
  }

  priv.onclick = async () => {
    const want = !isOn(), a = agent();
    if (!a) {
      window.chatUi.privacy.set(want);
      if (want) await window.chatUi.privacy.forget();                                    // what the index already holds of it goes too
    } else if (!current()) {
      waiting = want;
    } else {
      const r = await call(`${encodeURIComponent(current())}/private`, { private: want });
      if (!r.ok) { status(errText(r, r.json)); return; }
      const rec = a.record();
      if (rec) rec.private = want;
    }
    status(T(want ? "priv.marked" : "priv.unmarked"));
    paint();
  };

  forget.onclick = async () => {
    if (!armed) {
      armed = true; clearTimeout(armTimer);
      armTimer = setTimeout(() => { armed = false; paint(); }, ARM_MS);
      paint();
      return;
    }
    armed = false; clearTimeout(armTimer);
    const a = agent();
    let r;
    if (a) {
      if (!current()) { paint(); return; }
      r = await call(`${encodeURIComponent(current())}/forget`);
      r = { ok: r.ok, status: r.status, json: r.json };
    } else {
      r = await window.chatUi.privacy.forget();
      r.status = r.ok ? 200 : 0;
    }
    paint();
    const got = r.json && r.json.forgotten;
    status(r.ok && got ? T("priv.forgot", got.chunks, got.notes, got.queued) : errText(r, r.json));
  };

  document.addEventListener("basalt:chat-state", () => {
    const sid = current();
    if (sid !== lastSid) { if (sid) waiting = false; lastSid = sid; armed = false; }       // a conversation now exists, or another was opened
    paint();
  });
  document.addEventListener("basalt:lang", paint);
  window.privacy = { pending: () => waiting && !current() };
  paint();
})();
