# Voice in the Chat and in Agentic coding

Speak instead of typing, and have the answer read aloud. Code: `serve/web/chatvoice.js`, `chatvoice.css`; the voice bar is above the message box of the Chat and of
Agentic coding (the same composer). It uses the voice service of the PC through the server's own routes `POST /transcribe` (speech to text) and `POST /tts/test` (text to
speech), so the models are the ones chosen in Models > Voice services; nothing is installed or loaded from here.

| Control | What it does |
|---|---|
| **Talk** | Tap to record, tap again to stop; it also stops by itself after a pause of about 1.4 s, or after 18 s of nothing said (9 s in the conversation). The words are written in the message box, and sent when *Send when I stop talking* is on. |
| **Read answers aloud** | A finished answer is said by the PC's voice. Code, formulas, tables, addresses and marks are not read: code and formulas are named ("code on the screen"). A text is said in parts of about 380 characters and at most 5,000 characters in all. |
| **Voice conversation** | Listens, sends, waits for the answer, says it, listens again. It ends with *Stop voice*, when the microphone fails, or after two silences in a row. |
| **Language** | Automatic (the speech model decides what it hears; the voice is the service's default), Italian or English; it starts as the page's language. |

The microphone is the browser's, so the page must be on `localhost` or `https`; the browser asks permission the first time. The recording is made into a 16 kHz mono WAV in the
page (the service decodes WAV, FLAC and OGG, not the formats a browser records by itself). The settings are kept in the browser. An error of the microphone, of the speech
model or of the voice is written next to the controls.

A permission question of the agent or a long task is not read: the conversation waits for the answer, and the screen shows the question as usual.
