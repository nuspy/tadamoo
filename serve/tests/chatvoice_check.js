"use strict";
// Runs the pure part of serve/web/chatvoice.js and prints [{name, ok, error}] as JSON (read by test_chatvoice_page.py).
const path = require("path");
const { speechText, speechChunks, encodeWav, downsample, MAX_SPOKEN_CHARS } = require(path.join(__dirname, "..", "web", "chatvoice.js"));
const cases = [];
const t = (name, fn) => cases.push([name, fn]);
const eq = (got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) throw new Error(`got ${JSON.stringify(got)}, wanted ${JSON.stringify(want)}`); };

t("code, formulas and tables are named or left out, links keep their words, marks go", () => {
  eq(speechText("# Plan\n\nSee [the docs](https://example.com/a) and **this** `x()` now.\n\n```python\nprint(1)\n```\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- first\n- second\n\n$$x=1$$\nEnd."),
    "Plan\nSee the docs and this x() now.\n [code on the screen] \nfirst\nsecond\n [formula on the screen] \nEnd.");
});
t("a bare address is not read out", () => { eq(speechText("Open https://example.com/a?b=1 please"), "Open please"); });
t("a code block that is never closed is still left out to its end", () => { eq(speechText("Before\n```\nloop forever\nand ever"), "Before\n [code on the screen]"); });
t("nothing in gives nothing out", () => { eq(speechText(""), ""); eq(speechText(null), ""); eq(speechChunks(""), []); });
t("a long line is cut after a sentence, every part is at most the size asked, and nothing is lost", () => {
  const text = Array.from({ length: 30 }, (_, i) => `This is sentence number ${i} of a long answer.`).join(" ");
  const parts = speechChunks(text, 120);
  if (parts.some(p => p.length > 120)) throw new Error("a part is longer than 120: " + Math.max(...parts.map(p => p.length)));
  eq(parts.join(" ").replace(/\s+/g, " "), text);
  if (!parts.slice(0, -1).every(p => /[.]$/.test(p))) throw new Error("a part does not end at a sentence: " + JSON.stringify(parts.slice(0, 3)));
});
t("a word longer than the size is cut, and lines that say nothing are dropped", () => {
  eq(speechChunks("x".repeat(25), 10).map(p => p.length), [10, 10, 5]);
  eq(speechChunks("...\n---\nHello there"), ["Hello there"]);
});
t("a very long text is not said to its end", () => {
  const text = Array.from({ length: 2000 }, (_, i) => `Sentence ${i} says something.`).join("\n");
  const total = speechChunks(text).reduce((n, p) => n + p.length, 0);
  if (total > MAX_SPOKEN_CHARS + 400) throw new Error("said " + total);
  if (total < MAX_SPOKEN_CHARS - 400) throw new Error("said only " + total);
});
t("a WAV is mono 16-bit with its sizes right and the samples clipped", () => {
  const wav = encodeWav(new Float32Array([0, 0.5, -0.5, 2, -2]), 16000);
  eq(wav.slice(0, 4).toString(), "RIFF"); eq(wav.slice(8, 12).toString(), "WAVE"); eq(wav.slice(36, 40).toString(), "data");
  eq(wav.readUInt32LE(4), 36 + 10); eq(wav.readUInt16LE(20), 1); eq(wav.readUInt16LE(22), 1); eq(wav.readUInt32LE(24), 16000); eq(wav.readUInt16LE(34), 16); eq(wav.readUInt32LE(40), 10);
  eq([wav.readInt16LE(44), wav.readInt16LE(46), wav.readInt16LE(48), wav.readInt16LE(50), wav.readInt16LE(52)], [0, 16383, -16384, 32767, -32768]);
});
t("48 kHz becomes 16 kHz by averaging, and a rate that is not higher is left alone", () => {
  const x = Float32Array.from({ length: 48 }, (_, i) => (i % 3 === 0 ? 3 : 0));
  const y = downsample(x, 48000, 16000);
  eq(y.length, 16); eq(Array.from(y).every(v => Math.abs(v - 1) < 1e-6), true);
  eq(downsample(x, 16000, 16000), x); eq(downsample(x, 8000, 16000), x);
});
console.log(JSON.stringify(cases.map(([name, fn]) => { try { fn(); return { name, ok: true }; } catch (e) { return { name, ok: false, error: String(e.message || e) }; } })));
