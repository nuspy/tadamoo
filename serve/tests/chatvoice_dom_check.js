"use strict";
// Drives the voice of the Chat in a real headless browser with a fake microphone: node chatvoice_dom_check.js <url> <browser exe> <fake audio wav>.
// Prints [{name, ok, error}] as JSON (read by test_chatvoice_page.py). The fake microphone plays a burst of noise followed by a long silence, so the page's own
// detection of the end of speech is what stops the recording. transcribe and tts/test are answered by the page's fetch stub, which records what was sent;
// the conversation is the real one on the scripted mock-engine server.
const { spawn } = require("child_process"), fs = require("fs"), os = require("os");
const [URL, EXE, WAV] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const VIEWPORTS = [["phone 360x640", 360, 640, true], ["phone 390x844", 390, 844, true], ["desktop 1280x800", 1280, 800, false]];
// a half-second WAV of silence, as the stubbed voice service's answer
const TINY_WAV = (() => { const n = 8000, b = Buffer.alloc(44 + n * 2); b.write("RIFF", 0); b.writeUInt32LE(36 + n * 2, 4); b.write("WAVEfmt ", 8); b.writeUInt32LE(16, 16); b.writeUInt16LE(1, 20); b.writeUInt16LE(1, 22); b.writeUInt32LE(16000, 24); b.writeUInt32LE(32000, 28); b.writeUInt16LE(2, 32); b.writeUInt16LE(16, 34); b.write("data", 36); b.writeUInt32LE(n * 2, 40); return b.toString("base64"); })();

async function main() {
  const port = 9300 + Math.floor(Math.random() * 500), dir = fs.mkdtempSync(os.tmpdir() + "/cvc-");
  const proc = spawn(EXE, ["--headless=new", "--lang=en-US", "--disable-gpu", `--remote-debugging-port=${port}`, `--user-data-dir=${dir}`, "--window-size=1440,900",
    "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", `--use-file-for-fake-audio-capture=${WAV}`, "--autoplay-policy=no-user-gesture-required", "about:blank"], { stdio: "ignore" });
  const out = [];
  try {
    let targets = [];
    for (let i = 0; i < 40 && !targets.length; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch (e) { await sleep(250); } }
    const ws = new WebSocket(targets.find(t => t.type === "page").webSocketDebuggerUrl);
    await new Promise(r => { ws.onopen = r; });
    let id = 0; const waits = new Map(); const pageErrors = [];
    ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      if (m.id && waits.has(m.id)) { waits.get(m.id)(m.result || m.error); waits.delete(m.id); }
      else if (m.method === "Runtime.exceptionThrown") pageErrors.push(((m.params.exceptionDetails.exception || {}).description || m.params.exceptionDetails.text || "").slice(0, 300));
    };
    const send = (method, params = {}) => new Promise((r, rej) => { const n = ++id; const t = setTimeout(() => rej(new Error(`${method} did not answer`)), 60000); waits.set(n, v => { clearTimeout(t); r(v); }); ws.send(JSON.stringify({ id: n, method, params })); });
    const ev = async expr => {
      const r = await send("Runtime.evaluate", { expression: `(async () => { const q = s => document.querySelector(s), qa = s => [...document.querySelectorAll(s)]; const wait = ms => new Promise(r => setTimeout(r, ms)); ${expr} })()`, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails.exception && r.exceptionDetails.exception.description));
      return r.result.value;
    };
    await send("Page.enable"); await send("Runtime.enable"); await send("Emulation.setFocusEmulationEnabled", { enabled: true }); await send("Page.bringToFront");
    const waitFor = async (expr, what, tries = 200) => { for (let i = 0; i < tries; i++) { if (await ev(`return (${expr});`)) return true; await sleep(250); } throw new Error(`never: ${what}`); };
    const check = async (name, fn) => { try { await fn(); out.push({ name, ok: true, error: "" }); } catch (e) { out.push({ name, ok: false, error: e.message + (pageErrors.length ? " | page errors: " + pageErrors.splice(0).join(" ;; ") : "") }); } };

    const stub = `window.__vc = { stt: [], tts: [], plays: 0, sttFail: false, ttsFail: false, said: ["Write one sentence about the sea.", "Now one about the sky."] };
      window.__real = window.fetch;
      const reply = (o, s = 200) => Promise.resolve(new Response(JSON.stringify(o), { status: s, headers: { "Content-Type": "application/json" } }));
      window.fetch = async (u, o) => {
        const url = String(u);
        if (url.startsWith("transcribe")) {
          const f = o.body.get("file");
          window.__vc.stt.push({ url, size: f.size, type: f.type, head: new TextDecoder().decode(new Uint8Array(await f.slice(0, 4).arrayBuffer())) });
          return window.__vc.sttFail ? reply({ status: "error", message: "the speech model is not installed" }, 409) : reply({ text: window.__vc.said[Math.min(window.__vc.stt.length, 2) - 1] });
        }
        if (url.startsWith("tts/test")) { window.__vc.tts.push(JSON.parse(o.body)); return window.__vc.ttsFail ? reply({ status: "error", message: "no voice installed" }, 409) : reply({ status: "ok", audio_b64: "${TINY_WAV}", duration_ms: 500 }); }
        return window.__real(u, o);
      };
      const play = HTMLMediaElement.prototype.play;
      HTMLMediaElement.prototype.play = function () { window.__vc.plays++; return play.call(this); };`;
    await send("Page.addScriptToEvaluateOnNewDocument", { source: "try { localStorage.removeItem('tadamoo.chatvoice'); } catch (e) { /* none */ }" });
    const open = async lang => {
      await send("Page.navigate", { url: URL + "?lang=" + lang });
      await sleep(1500);
      await waitFor(`!!q("#vc-bar") && !!window.chatVoice`, "the page");
      await ev(stub + " window.chatVoice.tune({ end: 700, wait: 2500 }); return 1;");
      await ev(`q("#tab-chat").click(); return 1;`);
      await waitFor(`!!q("#amsgs") && !q("#amsgs").hidden && !q("#vc-bar").hidden`, "the chat view with the voice bar");
    };
    const fits = what => ev(`const el = q("#vc-bar"); el.scrollIntoView();
      const small = qa("#vc-bar button, #vc-bar select, #vc-bar label").filter(e => { const b = e.getBoundingClientRect(); return b.width > 0 && b.height > 0 && b.height < 43.5; }).map(e => (e.textContent.trim().slice(0, 18) || e.id));
      const box = el.getBoundingClientRect();
      return JSON.stringify({ wide: document.documentElement.scrollWidth > innerWidth + 1, inside: box.left >= -1 && box.right <= innerWidth + 1, small });`).then(v => {
      const f = JSON.parse(v);
      if (f.wide) throw new Error(what + ": the page scrolls sideways");
      if (!f.inside) throw new Error(what + ": the bar goes out of the screen");
      if (f.small.length) throw new Error(what + ": controls under 44 px: " + f.small.join(", "));
    });
    const answers = () => ev(`return qa("#amsgs .aend").length;`);

    for (const [vname, w, h, mobile] of VIEWPORTS.filter(v => !process.env.VOICE_ONLY || v[0].startsWith(process.env.VOICE_ONLY))) {
      await send("Emulation.setDeviceMetricsOverride", { width: w, height: h, deviceScaleFactor: 1, mobile });
      await open("en");

      await check(`${vname}: the voice bar is there with its controls, the language is the page's, and it fits the screen`, async () => {
        const info = JSON.parse(await ev(`return JSON.stringify({ mic: q("#vc-mic").textContent.trim(), free: q("#vc-free").textContent.trim(), speak: q("#vc-speak").checked, send: q("#vc-send").checked, lang: q("#vc-lang").value, stop: q("#vc-stopvoice").hidden });`));
        if (info.mic !== "Talk" || !/Voice conversation/.test(info.free) || info.speak || !info.send || info.lang !== "en" || !info.stop) throw new Error(JSON.stringify(info));
        await fits("the voice bar");
      });

      await check(`${vname}: Talk records until the speaker stops, sends a 16 kHz WAV for the language, puts the words in the message and sends them`, async () => {
        const before = await answers();
        await ev(`q("#vc-mic").click(); return 1;`);
        await waitFor(`q("#vc-mic").classList.contains("rec") && /Listening/.test(q("#vc-state").textContent)`, "the recording state");
        if (!/^Stop$/.test(await ev(`return q("#vc-mic").textContent.trim();`))) throw new Error("the button does not say Stop while recording");
        await waitFor(`window.__vc.stt.length === 1`, "the transcription call").catch(async e => { throw new Error(e.message + " | state=" + await ev(`return q("#vc-state").textContent + " stt=" + JSON.stringify(window.__vc.stt) + " rec=" + q("#vc-mic").className;`)); });
        const stt = JSON.parse(await ev(`return JSON.stringify(window.__vc.stt[0]);`));
        if (stt.head !== "RIFF" || stt.size < 8000 || stt.url !== "transcribe?lang=en") throw new Error(JSON.stringify(stt));
        await waitFor(`qa("#amsgs .cmsg.user").some(m => /Write one sentence about the sea/.test(m.textContent))`, "the message sent");
        await waitFor(async () => true, "x").catch(() => 0);
        await waitFor(`qa("#amsgs .aend").length === ${before + 1}`, "the answer");
        if (await ev(`return window.__vc.tts.length;`)) throw new Error("the answer was read aloud although that is off");
      });

      await check(`${vname}: with Read answers aloud on, the finished answer is cleaned and said, in parts, and Stop ends it`, async () => {
        await ev(`const c = q("#vc-speak"); c.click(); return 1;`);
        const before = await answers(), said = await ev(`return window.__vc.tts.length;`);
        await ev(`const p = q("#prompt"); p.value = "Say hello."; p.dispatchEvent(new Event("input")); q("#send").click(); return 1;`);
        await waitFor(`qa("#amsgs .aend").length === ${before + 1}`, "the answer");
        await waitFor(`window.__vc.tts.length > ${said}`, "the speech request");
        const tts = JSON.parse(await ev(`return JSON.stringify(window.__vc.tts[window.__vc.tts.length - 1]);`));
        if (!tts.text || tts.lang !== "en-us" || /[`*#]/.test(tts.text)) throw new Error(JSON.stringify(tts));
        await waitFor(`window.__vc.plays >= 1`, "the audio playing");
        await ev(`const c = q("#vc-speak"); if (c.checked) c.click(); return 1;`);
      });

      await check(`${vname}: a failure of the speech-to-text or of the voice is said, not swallowed`, async () => {
        await ev(`window.__vc.sttFail = true; q("#vc-mic").click(); return 1;`);
        await waitFor(`/could not be written down: the speech model is not installed/.test(q("#vc-state").textContent) && q("#vc-state").dataset.bad === "1"`, "the speech-to-text error");
        await ev(`window.__vc.sttFail = false; return 1;`);
        await ev(`window.__vc.ttsFail = true; q("#vc-speak").click(); const p = q("#prompt"); p.value = "Say it again."; p.dispatchEvent(new Event("input")); q("#send").click(); return 1;`);
        await waitFor(`/could not be read aloud: no voice installed/.test(q("#vc-state").textContent)`, "the voice error");
        await ev(`window.__vc.ttsFail = false; const c = q("#vc-speak"); if (c.checked) c.click(); return 1;`);
      });

      await check(`${vname}: the voice conversation listens, sends, waits, says the answer, listens again, and Stop voice ends it`, async () => {
        await ev(`window.__vc.stt.length = 0; window.__vc.tts.length = 0; q("#vc-free").click(); return 1;`);
        await waitFor(`q("#vc-free").getAttribute("aria-pressed") === "true" && !q("#vc-stopvoice").hidden`, "the conversation on");
        await waitFor(`window.__vc.stt.length >= 1 && window.__vc.tts.length >= 1`, "a first turn: heard, answered, said", 280);
        await waitFor(`window.__vc.stt.length >= 2`, "listening again after the answer was said", 280);
        await ev(`q("#vc-stopvoice").click(); return 1;`);
        await waitFor(`q("#vc-free").getAttribute("aria-pressed") === "false" && q("#vc-stopvoice").hidden`, "the conversation off");
        const n = await ev(`return window.__vc.stt.length;`);
        await sleep(3500);
        if ((await ev(`return window.__vc.stt.length;`)) !== n) throw new Error("it went on listening after Stop voice");
      });
    }

    await check("a conversation that hears nothing twice ends by itself and says so", async () => {
      await send("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
      await open("en");
      await ev(`navigator.mediaDevices.getUserMedia = async () => { const c = new AudioContext(), d = c.createMediaStreamDestination(); return d.stream; }; window.chatVoice.tune({ end: 300, wait: 700 }); q("#vc-free").click(); return 1;`);
      await waitFor(`/ended: nothing was said/.test(q("#vc-state").textContent) && q("#vc-free").getAttribute("aria-pressed") === "false"`, "the end by silence", 120);
      if (await ev(`return window.__vc.stt.length;`)) throw new Error("silence was sent to the transcription");
    });

    await check("without a microphone the error is said and the conversation does not start", async () => {
      await ev(`navigator.mediaDevices.getUserMedia = async () => { throw new Error("Permission denied"); }; q("#vc-free").click(); return 1;`);
      await waitFor(`/microphone cannot be used: Permission denied/.test(q("#vc-state").textContent) && q("#vc-free").getAttribute("aria-pressed") === "false"`, "the microphone error");
    });

    await check("Italian: the controls and the language of the speech follow the page", async () => {
      await open("it");
      const info = JSON.parse(await ev(`return JSON.stringify({ mic: q("#vc-mic").textContent.trim(), free: q("#vc-free").textContent.trim(), lang: q("#vc-lang").value });`));
      if (info.mic !== "Parla" || !/Conversazione vocale/.test(info.free) || info.lang !== "it") throw new Error(JSON.stringify(info));
    });
    if (pageErrors.length) out.push({ name: "page errors", ok: false, error: pageErrors.join(" ;; ") });
    ws.close();
  } finally { proc.kill(); }
  console.log(JSON.stringify(out));
}
main().catch(e => { console.log(JSON.stringify([{ name: "driver", ok: false, error: String(e && e.stack || e) }])); });
