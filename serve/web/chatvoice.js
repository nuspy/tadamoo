"use strict";
/* Voice in the Chat and in Agentic coding: speak instead of typing, and have the answer read aloud.
   - Microphone: tap to talk, it stops by itself when you stop talking (or tap again); what was said is written in the message box by the speech-to-text of the voice
     service (POST transcribe, a 16 kHz WAV made here) and sent when "Send when I stop talking" is on.
   - Read answers aloud: the finished answer is cleaned of code, links and marks and said by the text-to-speech of the voice service (POST tts/test), in parts.
   - Hands-free conversation: listen, send, wait for the answer, say it, listen again, until it is turned off, or two silences in a row.
   The microphone is the browser's (needs this page on localhost or https); the voice engines are the PC's. The pure parts are at the end and are tested in node. */
(() => {
  let END_SILENCE_MS = 1400, WAIT_SPEECH_MS = 9000, MAX_RECORD_MS = 120000, CHUNK_CHARS = 380, MAX_SPOKEN_CHARS = 5000;                                  // how long a pause ends what was said, how long to wait for a first word (the tap waits twice as long), the longest recording

  function merge(chunks) {
    let n = 0;
    chunks.forEach(c => { n += c.length; });
    const out = new Float32Array(n);
    let at = 0;
    chunks.forEach(c => { out.set(c, at); at += c.length; });
    return out;
  }

  /* samples at `from` Hz as samples at `to` Hz (an average of the samples each new one covers: no alias worth the name for speech) */
  function downsample(x, from, to) {
    if (to >= from) return x;
    const ratio = from / to, n = Math.floor(x.length / ratio), out = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      const a = Math.floor(i * ratio), b = Math.min(x.length, Math.floor((i + 1) * ratio));
      let s = 0;
      for (let k = a; k < b; k++) s += x[k];
      out[i] = b > a ? s / (b - a) : 0;
    }
    return out;
  }

  /* a mono 16-bit WAV file */
  function encodeWav(samples, rate) {
    const buf = new ArrayBuffer(44 + samples.length * 2), v = new DataView(buf);
    const tag = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    tag(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); tag(8, "WAVE"); tag(12, "fmt ");
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    tag(36, "data"); v.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return typeof window !== "undefined" ? new Blob([buf], { type: "audio/wav" }) : Buffer.from(buf);
  }

  /* an answer as it is said: code and tables are named and left out, links, marks and bullets are dropped */
  function speechText(markdown) {
    let t = String(markdown || "").replace(/\r\n/g, "\n");
    t = t.replace(/(^|\n)[ \t]*(```|~~~)[^\n]*\n[\s\S]*?(\n[ \t]*\2[ \t]*(?=\n|$)|$)/g, "$1 [code on the screen] ");
    t = t.replace(/\$\$[\s\S]*?\$\$/g, " [formula on the screen] ");
    t = t.replace(/^[ \t]*\|.*\|[ \t]*$/gm, "");                                                    // table rows
    t = t.replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1");
    t = t.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
    t = t.replace(/https?:\/\/\S+/g, "");
    t = t.replace(/`([^`]+)`/g, "$1");
    t = t.replace(/^[ \t]{0,3}#{1,6}[ \t]+/gm, "").replace(/^[ \t]*(?:[-*+]|\d+[.)])[ \t]+/gm, "").replace(/^[ \t]*>[ \t]?/gm, "");
    t = t.replace(/(\*\*|__|\*|_|~~)/g, "");
    t = t.replace(/[ \t]+/g, " ").replace(/\n{2,}/g, "\n").trim();
    return t;
  }

  /* the text in parts of at most `max` characters, cut after a sentence or a line; the rest of a very long text is not said */
  function speechChunks(text, max = CHUNK_CHARS) {
    const parts = [];
    let total = 0;
    for (const line of String(text || "").split("\n")) {
      let rest = line.trim();
      while (rest) {
        if (total >= MAX_SPOKEN_CHARS) return parts;
        let cut = rest.length;
        if (rest.length > max) {
          const at = Math.max(rest.lastIndexOf(". ", max), rest.lastIndexOf("? ", max), rest.lastIndexOf("! ", max), rest.lastIndexOf("; ", max));
          cut = at > max / 3 ? at + 1 : Math.max(rest.lastIndexOf(" ", max), max);
        }
        const piece = rest.slice(0, cut).trim();
        if (piece && /[\p{L}\p{N}]/u.test(piece)) { parts.push(piece); total += piece.length; }
        rest = rest.slice(cut).trim();
      }
    }
    return parts;
  }

  if (typeof module !== "undefined") module.exports = { speechText, speechChunks, encodeWav, downsample, END_SILENCE_MS, MAX_SPOKEN_CHARS };
  if (typeof document === "undefined") return;                           // in node only the pure parts above are used
  const q = id => document.getElementById(id);
  const bar = q("vc-bar");
  if (!bar) return;
  const T_ = (k, ...a) => T(k, ...a);
  const KEEP = "tadamoo.chatvoice";
  const saved = (() => { try { return JSON.parse(localStorage.getItem(KEEP) || "{}") || {}; } catch (e) { return {}; } })();
  const keep = () => { try { localStorage.setItem(KEEP, JSON.stringify({ speak: o.speak.checked, free: false, send: o.send.checked, lang: o.lang.value })); } catch (e) { /* no storage */ } };
  const o = { mic: q("vc-mic"), speak: q("vc-speak"), free: q("vc-free"), send: q("vc-send"), lang: q("vc-lang"), state: q("vc-state"), stop: q("vc-stopvoice") };
  const AC = window.AudioContext || window.webkitAudioContext;
  const can = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && AC);
  const prompt = () => q("prompt"), sendBtn = () => q("send");
  let recording = null, speaking = null, freeOn = false, silences = 0, answerWaiter = null, busy = false;

  const say = (key, ...args) => { o.state.textContent = key ? T_(key, ...args) : ""; o.state.dataset.bad = ""; };
  const bad = text => { o.state.textContent = text; o.state.dataset.bad = "1"; };
  const sttLang = () => o.lang.value;
  const ttsLang = () => ({ it: "it", en: "en-us" }[o.lang.value] || "");
  const headers = extra => ({ ...(typeof authHeaders === "function" ? authHeaders() : {}), ...(extra || {}) });

  function paint() {
    o.mic.setAttribute("aria-pressed", recording ? "true" : "false");
    o.mic.classList.toggle("rec", !!recording);
    o.mic.setAttribute("aria-label", T_(recording ? "vc.mic.stop" : "vc.mic"));
    o.mic.title = T_(recording ? "vc.mic.stop" : "vc.mic");
    o.free.setAttribute("aria-pressed", freeOn ? "true" : "false");
    o.free.classList.toggle("on", freeOn);
    o.stop.hidden = !(speaking || freeOn);
    o.mic.querySelector("span").textContent = T_(recording ? "vc.mic.stop" : "vc.mic");
  }

  /* ---- listening ---- */
  async function listen(auto) {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    const ctx = new AC();
    if (ctx.state === "suspended") { try { await ctx.resume(); } catch (e) { /* stays suspended */ } }
    const src = ctx.createMediaStreamSource(stream), proc = ctx.createScriptProcessor(4096, 1, 1);
    const chunks = [];
    let started = 0, quietSince = 0, floor = 0, voiced = 0;
    const t0 = performance.now();
    let finish;
    const done = new Promise(r => { finish = r; });
    const rec = { stop: () => finish("tap"), cancel: () => finish("cancel") };
    proc.onaudioprocess = e => {
      const x = e.inputBuffer.getChannelData(0);
      chunks.push(new Float32Array(x));
      let s = 0;
      for (let i = 0; i < x.length; i++) s += x[i] * x[i];
      const rms = Math.sqrt(s / x.length), now = performance.now() - t0;
      floor = floor ? Math.min(rms, floor * 1.05) : rms;                                          // the quietest moment lately: the noise of the room, rising only slowly
      const threshold = Math.max(0.02, Math.min(floor * 3, 0.15));                                // never so high that someone who starts talking at once is taken for noise
      if (rms > threshold) { voiced++; quietSince = 0; if (voiced >= 2 && !started) started = now; }
      else if (started && !quietSince) quietSince = now;
      if (started && quietSince && now - quietSince > END_SILENCE_MS) finish("silence");
      else if (!started && now > (auto ? WAIT_SPEECH_MS : WAIT_SPEECH_MS * 2)) finish("nospeech");
      else if (now > MAX_RECORD_MS) finish("limit");
    };
    src.connect(proc); proc.connect(ctx.destination);
    recording = rec; paint();
    const why = await done;
    recording = null;
    try { proc.disconnect(); src.disconnect(); } catch (e) { /* already */ }
    stream.getTracks().forEach(t => t.stop());
    const rate = ctx.sampleRate;
    try { await ctx.close(); } catch (e) { /* already */ }
    paint();
    if (why === "cancel") return { heard: false, why };
    const all = merge(chunks);
    return { heard: !!started || why === "tap", why, wav: encodeWav(downsample(all, rate, 16000), 16000) };
  }

  async function transcribe(wav) {
    const fd = new FormData();
    fd.append("file", wav, "speech.wav");
    const lang = sttLang();
    const r = await fetch("transcribe" + (lang ? "?lang=" + encodeURIComponent(lang) : ""), { method: "POST", headers: headers(), body: fd });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(messageOf(j) || `HTTP ${r.status}`);
    return String(j.text || "").trim();
  }
  const messageOf = j => (j && (typeof j.error === "string" ? j.error : j.error && j.error.message)) || (j && j.message) || "";

  /* ---- what was said goes to the message box and, when asked, is sent ---- */
  function deliver(text) {
    const p = prompt();
    p.value = (p.value.trim() && !o.send.checked ? p.value.replace(/\s+$/, "") + " " : "") + text;
    p.dispatchEvent(new Event("input", { bubbles: true }));
    if (o.send.checked || freeOn) {
      const b = sendBtn();
      if (b) { say("vc.state.sending"); b.click(); return true; }
    }
    p.focus();
    return false;
  }

  async function dictate(auto) {
    if (busy) return false;
    busy = true;
    try {
      say("vc.state.listening");
      let rec;
      try { rec = await listen(auto); } catch (e) { bad(T_("vc.mic.fail", e && e.message ? e.message : String(e))); return false; }
      if (!rec.heard) { say(rec.why === "cancel" ? "" : "vc.state.nospeech"); return false; }
      say("vc.state.writing");
      let text;
      try { text = await transcribe(rec.wav); } catch (e) { bad(T_("vc.stt.fail", e.message)); return false; }
      if (!text) { say("vc.state.nospeech"); return false; }
      return deliver(text) ? "sent" : "typed";
    } finally { busy = false; paint(); }
  }

  /* ---- speaking ---- */
  async function synth(part) {
    const body = { text: part, ...(ttsLang() ? { lang: ttsLang() } : {}) };
    const r = await fetch("tts/test", { method: "POST", headers: headers({ "Content-Type": "application/json" }), body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.status === "error" || !j.audio_b64) throw new Error(messageOf(j) || `HTTP ${r.status}`);
    return "data:audio/wav;base64," + j.audio_b64;
  }

  function play(url, token) {
    return new Promise(resolve => {
      const a = new Audio(url);
      token.audio = a;
      a.onended = () => resolve(true);
      a.onerror = () => resolve(false);
      a.play().catch(() => resolve(false));
    });
  }

  async function speakText(markdown) {
    const parts = speechChunks(speechText(markdown));
    if (!parts.length) return;
    stopSpeaking();
    const token = { stopped: false, audio: null };
    speaking = token; paint(); say("vc.state.speaking");
    try {
      let next = synth(parts[0]);
      for (let i = 0; i < parts.length && !token.stopped; i++) {
        const url = await next;
        if (token.stopped) break;
        next = i + 1 < parts.length ? synth(parts[i + 1]) : null;
        next && next.catch(() => { /* reported when it is awaited */ });
        await play(url, token);
      }
    } catch (e) { bad(T_("vc.tts.fail", e.message)); } finally {
      if (speaking === token) speaking = null;
      paint();
      if (!o.state.dataset.bad) say("");
    }
  }
  function stopSpeaking() {
    if (!speaking) return;
    speaking.stopped = true;
    try { speaking.audio && speaking.audio.pause(); } catch (e) { /* gone */ }
    speaking = null;
  }

  /* ---- the answer arrives ---- */
  document.addEventListener("tadamoo:answer", e => {
    const d = e.detail || {};
    if (answerWaiter) { const w = answerWaiter; answerWaiter = null; w(d); return; }
    if (o.speak.checked && d.live && d.text) speakText(d.text);
  });

  /* ---- hands-free ---- */
  async function loop() {
    silences = 0;
    while (freeOn) {
      const got = await dictate(true);
      if (!freeOn) break;
      if (got === "sent") {
        silences = 0;
        say("vc.state.waiting");
        const answer = await new Promise(res => { answerWaiter = res; });
        answerWaiter = null;
        if (!freeOn) break;
        if (answer && answer.text) await speakText(answer.text);
      } else if (got === false) {
        if (o.state.dataset.bad) { freeOff(); break; }
        if (++silences >= 2) { freeOff(T_("vc.state.ended")); break; }
      }
    }
  }
  function freeOff(note) {
    freeOn = false;
    if (answerWaiter) { const w = answerWaiter; answerWaiter = null; w(null); }
    if (recording) recording.cancel();
    stopSpeaking();
    paint();
    if (note) { say(null); o.state.textContent = note; } else if (!o.state.dataset.bad) say("");
  }

  o.mic.addEventListener("click", () => {
    if (recording) { recording.stop(); return; }
    stopSpeaking();
    dictate(false);
  });
  o.free.addEventListener("click", () => {
    if (freeOn) { freeOff(); return; }
    freeOn = true; o.send.checked = true; keep(); paint(); stopSpeaking();
    loop();
  });
  o.stop.addEventListener("click", () => { if (freeOn) freeOff(); else stopSpeaking(); });
  [o.speak, o.send, o.lang].forEach(c => c.addEventListener("change", keep));
  document.addEventListener("tadamoo:lang", () => { paint(); });

  /* ---- start ---- */
  o.speak.checked = !!saved.speak;
  o.send.checked = saved.send !== false;
  o.lang.value = ["", "it", "en"].includes(saved.lang) ? saved.lang : (LANG === "it" ? "it" : "en");
  bar.hidden = !can;
  paint();
  if (typeof applyT === "function") applyT(bar);

  /* ---- the pure parts ---- */
  window.chatVoice = { speechText, speechChunks, encodeWav, downsample, stopSpeaking, speakText, tune: t => { if (t.end) END_SILENCE_MS = t.end; if (t.wait) WAIT_SPEECH_MS = t.wait; } };
})();
