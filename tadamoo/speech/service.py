"""The anchor's voice: text read by the voice service through the server's `/tts/test` route, in short pieces joined into one WAV.

The personality (`GET /api/personas`) says which engine and voice it speaks with and which avatar it has. Standard library only."""
from __future__ import annotations

import base64
import http.client
import io
import json
import re
import wave
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from tadamoo.speech import audiofx, textnorm, voicecheck

DEFAULT_URL = "http://127.0.0.1:7232"
PIECE_MAX = 200                                    # characters a voice engine reads well in one go
GAP_S = 0.22                                       # silence between two pieces
TRIES = 3                                          # readings of a piece that does not match its text
END_MARK = {"hu": ","}                             # what is added to a piece for the engine, by language: without it XTTS cuts the last syllable of a Hungarian sentence
TIMEOUT_S = 180.0


class SpeechError(RuntimeError):
    pass


def _call(url: str, method: str, path: str, body: Dict[str, Any] | None = None, key: str = "") -> Tuple[int, bytes]:
    parts = urlsplit(url or DEFAULT_URL)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    conn = http.client.HTTPConnection(parts.hostname or "127.0.0.1", parts.port or 80, timeout=TIMEOUT_S)
    try:
        conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        r = conn.getresponse()
        return r.status, r.read(64 << 20)
    except (OSError, http.client.HTTPException) as exc:
        raise SpeechError(f"the server does not answer ({exc.__class__.__name__})") from None
    finally:
        conn.close()


def persona(url: str, slug: str, key: str = "") -> Dict[str, Any]:
    """`{name, voice: {engine, voice}, avatar}` of the personality with this slug."""
    status, raw = _call(url, "GET", "/api/personas", key=key)
    try:
        rows = json.loads(raw).get("personas", [])
    except ValueError:
        rows = []
    if status != 200:
        raise SpeechError(f"the personalities could not be read (HTTP {status})")
    found = next((p for p in rows if p.get("slug") == slug), None)
    if found is None:
        raise SpeechError(f"there is no personality '{slug}'")
    return {"name": found.get("name", slug), "voice": found.get("voice") or {}, "avatar": found.get("avatar", "")}


def pieces(text: str, limit: int = PIECE_MAX) -> List[str]:
    """The text cut at sentence ends into pieces of at most `limit` characters, at commas when one sentence is longer."""
    out: List[str] = []
    current = ""
    for sentence in re.split(r"(?<=[.!?…])\s+", " ".join(text.split())):
        chunks = [sentence]
        while len(chunks[-1]) > limit:
            tail = chunks.pop()
            cut = max(tail.rfind(c, 0, limit) for c in (",", ";", ":"))
            cut = cut if cut > limit // 3 else tail.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit - 1
            chunks += [tail[:cut + 1].strip(), tail[cut + 1:].strip()]
        for chunk in chunks:
            if current and len(current) + 1 + len(chunk) > limit:
                out.append(current)
                current = chunk
            else:
                current = f"{current} {chunk}".strip()
    if current:
        out.append(current)
    return [p for p in out if p]


def synthesize(url: str, text: str, engine: str, voice: str, lang: str, key: str = "") -> bytes:
    """One WAV of `text` (a single piece) in this voice."""
    status, raw = _call(url, "POST", "/tts/test", {"text": text, "voice": voice, "lang": lang, "engine": engine}, key)
    try:
        out = json.loads(raw)
    except ValueError:
        out = {}
    if status != 200 or out.get("status") != "ok" or not out.get("audio_b64"):
        raise SpeechError(f"the voice service refused ({status}): {str(out.get('message') or out.get('error') or raw[:120])[:200]}")
    return base64.b64decode(out["audio_b64"])


def join(wavs: List[bytes], gap_s: float = GAP_S) -> bytes:
    """The WAVs one after the other with a short silence between them; they must share the format, as the pieces of one voice do."""
    params, frames = None, []
    for data in wavs:
        with wave.open(io.BytesIO(data)) as w:
            if params is None:
                params = w.getparams()
            elif (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (params.nchannels, params.sampwidth, params.framerate):
                raise SpeechError("the pieces of the voice do not share one format")
            frames.append(w.readframes(w.getnframes()))
    if params is None:
        raise SpeechError("nothing to join")
    silence = b"\x00" * (int(params.framerate * gap_s) * params.nchannels * params.sampwidth)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        w.writeframes(silence.join(frames))
    return out.getvalue()


def seconds(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / float(w.getframerate())


def pad(wav: bytes, total_s: float) -> bytes:
    """The recording with silence added at its end so that it lasts `total_s` seconds (it is cut when it is longer)."""
    with wave.open(io.BytesIO(wav)) as w:
        params = w.getparams()
        frames = w.readframes(w.getnframes())
    want = int(round(total_s * params.framerate)) * params.nchannels * params.sampwidth
    frames = frames[:want] + b"\x00" * max(0, want - len(frames))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams(params)
        w.writeframes(frames)
    return out.getvalue()


def cut(wav: bytes, t0: float, t1: float) -> bytes:
    """The part of the recording from `t0` to `t1` seconds; where the recording ends before `t1` the rest is silence. Two cuts that meet at the same time meet at the same sample."""
    with wave.open(io.BytesIO(wav)) as w:
        params = w.getparams()
        frames = w.readframes(w.getnframes())
    step = params.nchannels * params.sampwidth
    a, b = int(round(t0 * params.framerate)) * step, int(round(t1 * params.framerate)) * step
    part = frames[a:b] + b"\x00" * max(0, b - max(a, len(frames)))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams(params)
        w.writeframes(part)
    return out.getvalue()


@dataclass
class Spoken:
    wav: bytes
    report: List[Dict[str, Any]] = field(default_factory=list)    # one entry for each piece: what was asked, what was heard, how many tries, what was cut
    durations: List[float] = field(default_factory=list)          # the length of each piece's recording; the silence between two pieces (GAP_S) is not in them

    @property
    def failed(self) -> List[Dict[str, Any]]:
        """The pieces for which no reading was right: they must not be shipped without a person saying so."""
        return [e for e in self.report if e.get("ok") is False]


def trim_tail(wav: bytes, seconds: float) -> bytes:
    """The WAV cut after `seconds`."""
    with wave.open(io.BytesIO(wav)) as w:
        params = w.getparams()
        keep = min(w.getnframes(), int(seconds * w.getframerate()))
        frames = w.readframes(keep)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams(params)
        w.writeframes(frames)
    return out.getvalue()


def one_piece(url: str, text: str, engine: str, voice: str, lang: str, key: str = "", check: bool = True, tries: int = TRIES,
              say: Optional[Callable[[str], str]] = None, tempo: float = 1.0, judge: Optional[Any] = None) -> Tuple[bytes, Dict[str, Any]]:
    """A piece read, slowed by `tempo`, then read back by the speech-to-text and compared with the text. `say` turns the text into what the voice is sent (a respelling, respell.py);
    the read-back always compares with the text itself. Sounds after the last written word are cut off. A piece that is not right is read again (the engine samples, so a second reading
    is another one) and the best reading is kept. With a `judge` (voiceprint.Judge) a reading is right only when it is also the same speaker, at the pace and with the pitch wanted, and the
    best is the one with the best combined score; the entry says why a reading failed and keeps the numbers of every try. `entry["ok"]` is False when no reading was right."""
    entry: Dict[str, Any] = {"text": text, "tries": 0}
    spoken_text = say(text) if say else text
    if spoken_text != text:
        entry["said"] = spoken_text
    if abs(tempo - 1.0) > 1e-3:
        entry["tempo"] = tempo
    best: Optional[Tuple[float, bytes, voicecheck.Check, Any]] = None
    log: List[Dict[str, Any]] = []
    for attempt in range(1, max(1, tries) + 1):
        wav = audiofx.stretch(synthesize(url, spoken_text + END_MARK.get(lang, ""), engine, voice, lang, key), tempo)
        entry["tries"] = attempt
        if not check:
            return wav, entry
        try:
            result = voicecheck.compare(text, voicecheck.transcribe(url, wav, lang, key), lang)
        except OSError as exc:
            entry["checked"] = False
            entry["note"] = str(exc)
            return wav, entry
        if result.extra_tail and result.trim_at_s:
            wav = trim_tail(wav, result.trim_at_s + voicecheck.TAIL_PAD_S)
            entry["cut_tail"] = result.extra_tail
            result = voicecheck.Check(similarity=1.0 if result.missing == 0 else result.similarity, heard=result.heard, missing=result.missing,
                                      ok=result.missing == 0 and result.similarity >= voicecheck.SIMILAR_OK)
        verdict = None
        if judge is not None:
            verdict = judge.score(wav, text, result.similarity, lang)
            score, good = verdict.score, verdict.ok and result.ok
            log.append({"try": attempt, "score": verdict.score, "ok": good, **verdict.numbers, "why": verdict.why})
        else:
            score, good = result.similarity - 0.01 * len(result.extra_tail), result.ok
            log.append({"try": attempt, "score": round(score, 4), "ok": good, "similarity": round(result.similarity, 3)})
        if best is None or score > best[0]:
            best = (score, wav, result, verdict)
        if good:
            break
    assert best is not None
    _, wav, result, verdict = best
    good = bool(result.ok and (verdict is None or verdict.ok))
    entry.update(checked=True, similarity=result.similarity, heard=result.heard, ok=good, tries_log=log)
    if verdict is not None:
        entry["voice"] = verdict.numbers
        entry["why"] = verdict.why
    elif not result.ok:
        entry["why"] = [f"the read-back matches {result.similarity:.2f}, at least {voicecheck.SIMILAR_OK} is needed"]
    return wav, entry


def speak_checked(url: str, text: str, engine: str, voice: str, lang: str, key: str = "", check: bool = True,
                  pronunciations: Optional[Dict[str, str]] = None, tries: int = TRIES, say: Optional[Callable[[str], str]] = None, tempo: float = 1.0,
                  pause_s: float = GAP_S, judge: Optional[Any] = None, level_dbfs: Optional[float] = None) -> Spoken:
    """The whole text as one WAV, piece by piece. The text is normalised for the voice as a whole (textnorm.py: an abbreviation's dot must not end a piece), cut at
    sentence ends, each piece loses the full stop that ends it and, with `check`, is read back and compared with its text. `level_dbfs` brings every piece to the same speech level
    before they are joined with `pause_s` of silence between them."""
    parts = [p for p in (textnorm.strip_end(p) for p in pieces(textnorm.normalise(text, lang, pronunciations))) if p]
    if not parts:
        raise SpeechError("there is no text to read")
    wavs, report = [], []
    for part in parts:
        wav, entry = one_piece(url, part, engine, voice, lang, key, check, tries, say, tempo, judge)
        if level_dbfs is not None:
            wav = audiofx.level(wav, level_dbfs)
        wavs.append(wav)
        report.append(entry)
    return Spoken(join(wavs, pause_s), report, [seconds(w) for w in wavs])


def speak(url: str, text: str, engine: str, voice: str, lang: str, key: str = "") -> bytes:
    """The whole text as one WAV (see `speak_checked` for the report)."""
    return speak_checked(url, text, engine, voice, lang, key).wav
