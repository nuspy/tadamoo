"""Putting clause recordings of a voice together into one recording without audible joins, and checking the result: pauses and clicks.

A synthesised sentence read in pieces is joined by hand here. Each piece is cleaned (the silence and the noise at both ends cut, the DC offset removed, 15 ms fades), and the pieces
are joined by a pause of a length chosen for the punctuation that ends the piece (a comma 200 to 300 ms, a sentence end 400 to 600 ms) filled with the low noise the pieces themselves
carry, so that there is neither a step nor a change of room tone. Nothing overlaps.

The checks work on any recording of a voice: `silences` lists every silence inside the speech, `pause_audit` says which of them are not at a punctuation mark of the text (a voice
engine that samples sometimes pauses in the middle of a name: that reading is rejected), and `click_report` measures every join (and every other place) for a sample step or a burst
of high frequencies that a listener hears as a click."""
from __future__ import annotations

import difflib
import io
import math
import re
import wave
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from tadamoo.speech import voicecheck

FRAME_S = 0.010
PUNCT = ",.;:!?…—–"
SENTENCE_END = ".!?…"


# ---- reading and writing
def read(wav: bytes) -> Tuple[Any, int]:
    """(samples as float32 in -1..1, mono, sample rate)."""
    import numpy as np
    with wave.open(io.BytesIO(wav)) as w:
        rate, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    x = np.frombuffer(raw, dtype={1: np.int8, 2: np.int16, 4: np.int32}[width]).astype(np.float32) / float(2 ** (8 * width - 1))
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, rate


def write(x: Any, rate: int) -> bytes:
    import numpy as np
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(x, -1.0, 1.0) * 32767.0).round().astype(np.int16).tobytes())
    return out.getvalue()


def frame_levels(x: Any, rate: int, frame_s: float = FRAME_S) -> Any:
    """RMS in dB full scale of each frame."""
    import numpy as np
    n = max(1, int(rate * frame_s))
    k = len(x) // n
    frames = x[: k * n].reshape(k, n)
    return 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)


def speech_floor_db(levels: Any, below_db: float = 38.0) -> float:
    """The level under which a frame is silence: `below_db` under the speech level (the 90th percentile of the frames), but never above -45 dBFS."""
    import numpy as np
    return min(-45.0, float(np.percentile(levels, 90)) - below_db)


# ---- cleaning one piece
def remove_dc(x: Any) -> Any:
    return x - x.mean()


def trim(x: Any, rate: int, margin_s: float = 0.03, below_db: float = 38.0) -> Any:
    """The piece without the silence and the noise before its first and after its last sound (a margin of `margin_s` is kept)."""
    import numpy as np
    lv = frame_levels(x, rate)
    on = np.where(lv > speech_floor_db(lv, below_db))[0]
    if not len(on):
        return x
    n = int(rate * FRAME_S)
    a = max(0, on[0] * n - int(margin_s * rate))
    b = min(len(x), (on[-1] + 1) * n + int(margin_s * rate))
    return x[a:b]


def fade(x: Any, rate: int, fade_s: float = 0.015) -> Any:
    """A raised-cosine fade in and out of `fade_s` at the ends of the piece."""
    import numpy as np
    n = min(int(rate * fade_s), len(x) // 2)
    if n < 2:
        return x
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0, math.pi, n, dtype=np.float32))
    y = x.copy()
    y[:n] *= ramp
    y[-n:] *= ramp[::-1]
    return y


def noise_floor_rms(x: Any, rate: int, edge_s: float = 0.06) -> float:
    """The noise a piece carries between its sounds, measured where there are none: the RMS of the quieter of its first and its last `edge_s` (a voice engine's own silence is
    far under the noise of any room, about -90 dBFS, and is what the pauses between its pieces have to match)."""
    import numpy as np
    n = max(1, int(rate * edge_s))
    head, tail = x[:n], x[-n:]
    return float(min(np.sqrt((head ** 2).mean()), np.sqrt((tail ** 2).mean())))


def room_tone(n: int, rate: int, rms: float, seed: int = 1) -> Any:
    """`n` samples of low noise at `rms`, band-limited like the quiet of a room (below 4 kHz), which fills a pause so that it is not digital silence."""
    import numpy as np
    from scipy.signal import butter, sosfilt
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n + 2000).astype(np.float32)
    sos = butter(2, 4000, btype="low", fs=rate, output="sos")
    noise = sosfilt(sos, noise)[1000:1000 + n]
    return (noise / (np.sqrt((noise ** 2).mean()) + 1e-9) * rms).astype(np.float32)


def pause_for(ending: str, comma_ms: float = 250.0, sentence_ms: float = 500.0, colon_ms: float = 320.0) -> float:
    """The pause in seconds after a piece that ends with `ending` (its last character): a comma, a colon or a sentence end."""
    if ending in SENTENCE_END:
        return sentence_ms / 1000
    if ending in ":;—–":
        return colon_ms / 1000
    return comma_ms / 1000


@dataclass
class Join:
    index: int                           # the join after piece `index`
    time_s: float                        # where the pause begins in the result
    pause_s: float
    kind: str                            # comma | sentence | colon


def assemble(pieces: Sequence[Tuple[Any, int, str]], comma_ms: float = 250.0, sentence_ms: float = 500.0, lead_s: float = 0.12, tail_s: float = 0.25, seed: int = 1) -> Tuple[Any, int, List[Join], List[Tuple[float, float]]]:
    """The pieces joined. Each piece is `(samples, rate, ending)`, `ending` the punctuation mark that ended its text. Returns the samples, the rate, the joins and the (start, end) of each piece in the result."""
    import numpy as np
    if not pieces:
        raise ValueError("no pieces")
    rate = pieces[0][1]
    cleaned = [fade(trim(remove_dc(x), r), r) for x, r, _ in pieces]
    floor = min(0.003, max(3e-5, statistics_mean([noise_floor_rms(remove_dc(x), r) for x, r, _ in pieces])))        # at least one bit of dither, never louder than about -50 dBFS
    out = [room_tone(int(lead_s * rate), rate, floor, seed)]
    joins: List[Join] = []
    spans: List[Tuple[float, float]] = []
    t = lead_s
    for i, ((x, r, ending), c) in enumerate(zip(pieces, cleaned)):
        if r != rate:
            raise ValueError("the pieces do not share one sample rate")
        out.append(c)
        spans.append((round(t, 3), round(t + len(c) / rate, 3)))
        t += len(c) / rate
        if i < len(pieces) - 1:
            gap = pause_for(ending, comma_ms, sentence_ms)
            out.append(room_tone(int(gap * rate), rate, floor, seed + i + 1))
            joins.append(Join(i, round(t, 3), gap, "sentence" if ending in SENTENCE_END else "colon" if ending in ":;—–" else "comma"))
            t += gap
    out.append(room_tone(int(tail_s * rate), rate, floor, seed + 99))
    return np.concatenate(out), rate, joins, spans


def statistics_mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# ---- the checks
@dataclass
class Silence:
    start_s: float
    end_s: float
    after: str = ""                      # the word of the text before it ("" when unknown)
    punct: str = ""                      # the punctuation that follows that word in the text
    defect: bool = False
    warn: bool = False
    note: str = ""

    @property
    def length_s(self) -> float:
        return self.end_s - self.start_s


def silences(x: Any, rate: int, min_s: float = 0.12, below_db: float = 38.0) -> List[Tuple[float, float]]:
    """Every silence of at least `min_s` between the first and the last sound of the recording, as (start, end) in seconds."""
    import numpy as np
    lv = frame_levels(x, rate)
    quiet = lv <= speech_floor_db(lv, below_db)
    on = np.where(~quiet)[0]
    if not len(on):
        return []
    out, run = [], None
    for i in range(on[0], on[-1] + 1):
        if quiet[i] and run is None:
            run = i
        elif not quiet[i] and run is not None:
            if (i - run) * FRAME_S >= min_s:
                out.append((run * FRAME_S, i * FRAME_S))
            run = None
    return out


def words_with_punctuation(text: str) -> List[Tuple[str, str]]:
    """(word folded for comparison, the punctuation mark right after it or "") for each word of `text`."""
    out: List[Tuple[str, str]] = []
    for m in re.finditer(r"([^\W\d_]+(?:-[^\W\d_]+)*|\d+(?:[.,]\d+)?)([^\w\s]*)", text):
        marks = [c for c in m.group(2) if c in PUNCT]
        out.append((voicecheck.fold(m.group(1)), marks[0] if marks else ""))
    return out


def _end_times(want_tokens: Sequence[str], heard: Sequence[Tuple[str, Dict[str, Any]]]) -> Dict[int, float]:
    """For each written word the end time of the recognised word it was matched to. Equal words match one to one; where the recognised words differ from the written ones (a word
    misheard) the two runs are matched end to end, so that a silence next to a misheard word is still put after the right written word."""
    matcher = difflib.SequenceMatcher(None, list(want_tokens), [h for h, _ in heard], autojunk=False)
    out: Dict[int, float] = {}
    for op, a0, a1, b0, b1 in matcher.get_opcodes():
        if op == "equal":
            for k in range(a1 - a0):
                out[a0 + k] = float(heard[b0 + k][1].get("end_s") or 0.0)
        elif op == "replace":
            for k in range(1, min(a1 - a0, b1 - b0) + 1):
                out[a1 - k] = float(heard[b1 - k][1].get("end_s") or 0.0)
    return out


def pause_audit(x: Any, rate: int, text: str, heard_words: Sequence[Dict[str, Any]], defect_s: float = 0.18, min_s: float = 0.12, lang: str = "hu",
                known: Sequence[Tuple[float, float]] = ()) -> List[Silence]:
    """The silences of a recording and which of them do not belong where they are. A silence is put after the written word whose recognised time is nearest to its start (the speech-to-text
    gives the words and their times; written and recognised words are matched by sequence, a misheard word by its place). It is a defect when it is longer than `defect_s` and no
    punctuation follows that word, and a warning between `min_s` and `defect_s` without punctuation. A silence inside one of `known` (the pauses a join put there on purpose) is not
    judged."""
    from tadamoo.speech import textnorm
    want = words_with_punctuation(textnorm.normalise(text, lang) if lang else text)
    heard = [(tok, w) for w in voicecheck._groups(list(heard_words), lang) for tok in voicecheck.tokens(textnorm.normalise(str(w.get("text", "")), lang))] if lang else \
            [(voicecheck.fold(m.group(0)), w) for w in heard_words for m in voicecheck.TOKEN.finditer(str(w.get("text", "")))]
    end_of = _end_times([w for w, _ in want], heard)
    out: List[Silence] = []
    for s, e in silences(x, rate, min_s):
        item = Silence(round(s, 3), round(e, 3))
        if any(s >= a - 0.05 and e <= b + 0.05 for a, b in known):
            item.note = "a pause made by the join"
            out.append(item)
            continue
        best, gap = None, 1e9
        for i, t in end_of.items():
            d = abs(t - s)
            if d < gap:
                best, gap = i, d
        if best is None or gap > 0.45:
            item.note = "not matched to a word"
            item.defect = item.length_s > defect_s
        else:
            item.after, item.punct = want[best][0], want[best][1]
            if not item.punct:
                item.defect = item.length_s > defect_s
                item.warn = not item.defect
                item.note = f"a pause of {item.length_s * 1000:.0f} ms after '{item.after}', with no punctuation there"
            else:
                item.note = f"at '{item.punct}'"
        out.append(item)
    return out


def splices(x: Any, rate: int, frame_s: float = 0.005, drop_db: float = 35.0, dead_db: float = -120.0) -> List[Dict[str, Any]]:
    """Where speech is cut off into, or starts out of, digital silence (a recording joined without a fade and without room noise): the level falls from above -45 dBFS to under
    `dead_db` within one `frame_s`, or rises the same way. Natural speech ends over tens of milliseconds and sits in a floor of noise, never in digital zero."""
    lv = frame_levels(x, rate, frame_s)
    out = []
    for i in range(len(lv) - 1):
        if lv[i] > -45.0 and lv[i + 1] < dead_db and lv[i] - lv[i + 1] >= drop_db:
            out.append({"time_s": round((i + 1) * frame_s, 3), "kind": "cut", "from_db": round(float(lv[i]), 1)})
        elif lv[i] < dead_db and lv[i + 1] > -45.0:
            out.append({"time_s": round((i + 1) * frame_s, 3), "kind": "onset", "to_db": round(float(lv[i + 1]), 1)})
    return out


def dead_gaps(x: Any, rate: int, min_s: float = 0.05) -> List[Tuple[float, float]]:
    """Runs of at least `min_s` of exact digital zero: a pause that is not a room."""
    import numpy as np
    quiet = np.abs(x) < 1e-6
    padded = np.concatenate([[False], quiet, [False]])
    edges = np.diff(padded.astype(np.int8))
    starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0]
    return [(round(a / rate, 3), round(b / rate, 3)) for a, b in zip(starts, ends) if (b - a) / rate >= min_s]


def comma_pauses(x: Any, rate: int, text: str, heard_words: Sequence[Dict[str, Any]], target_s: float = 0.25, lang: str = "hu", seed: int = 7) -> Tuple[Any, List[Dict[str, Any]]]:
    """The recording with the pause at each comma of `text` brought to `target_s`. A voice engine makes its own pause at a comma, often a fraction of a second shorter than a presenter's.
    The comma is found by the recognised time of the word before it; the pause is the quiet stretch nearest to that time (never speech: if there is no quiet stretch there the
    recording is left alone), and the missing time is put into the middle of it as room noise at the level of the recording's own silence, so that nothing is cut and nothing steps."""
    import numpy as np
    from tadamoo.speech import textnorm
    want = words_with_punctuation(textnorm.normalise(text, lang) if lang else text)
    heard = [(tok, w) for w in voicecheck._groups(list(heard_words), lang) for tok in voicecheck.tokens(textnorm.normalise(str(w.get("text", "")), lang))] if lang else             [(voicecheck.fold(m.group(0)), w) for w in heard_words for m in voicecheck.TOKEN.finditer(str(w.get("text", "")))]
    end_of = _end_times([w for w, _ in want], heard)
    lv = frame_levels(x, rate)
    floor = speech_floor_db(lv)
    quiet = lv <= floor
    runs, start = [], None
    for i, q in enumerate(quiet):                                   # every quiet run of the recording, in frames
        if q and start is None:
            start = i
        elif not q and start is not None:
            runs.append((start, i))
            start = None
    on = np.where(~quiet)[0]
    inner = [(a, b) for a, b in runs if len(on) and a > on[0] and b <= on[-1]]
    edits: List[Tuple[int, int, float]] = []                        # (sample to insert at, samples to insert, the silence found, in seconds)
    notes: List[Dict[str, Any]] = []
    for k, (word, punct) in enumerate(want):
        if punct != "," or k not in end_of or k == len(want) - 1:
            continue
        t = end_of[k]
        near = [(abs(a * FRAME_S - t), a, b) for a, b in inner if -0.2 <= a * FRAME_S - t <= 0.3]
        if not near:
            notes.append({"after": word, "time_s": round(t, 3), "result": "no quiet stretch there, left alone"})
            continue
        _, a, b = min(near)
        have = (b - a) * FRAME_S
        if have >= target_s - 0.02:
            notes.append({"after": word, "time_s": round(a * FRAME_S, 3), "result": f"already {have * 1000:.0f} ms"})
            continue
        add = int((target_s - have) * rate)
        edits.append(((a + b) // 2 * int(rate * FRAME_S), add, have))
        notes.append({"after": word, "time_s": round(a * FRAME_S, 3), "result": f"{have * 1000:.0f} ms made {target_s * 1000:.0f} ms"})
    if not edits:
        return x, notes
    floor_rms = max(3e-5, min(0.003, noise_floor_rms(x, rate)))
    out, last = [], 0
    for pos, add, _ in sorted(edits):
        out += [x[last:pos], room_tone(add, rate, floor_rms, seed + pos)]
        last = pos
    out.append(x[last:])
    return np.concatenate(out), notes


@dataclass
class Click:
    time_s: float
    step: float                          # the biggest sample-to-sample jump in the window, as a multiple of the speech's own 99.9th percentile jump
    hf: float                            # the energy above 6 kHz in the window, as a multiple of the median window's
    where: str = ""
    bad: bool = False


def click_report(x: Any, rate: int, at: Sequence[Tuple[float, float]] = (), window_s: float = 0.012, step_limit: float = 3.0, hf_limit: float = 12.0) -> List[Click]:
    """The places where the recording clicks: each (start, end) in `at` (a join: the pause and 20 ms on both sides of it) and, wherever else, a window with a jump far outside the rest.
    `step` is the largest jump from one sample to the next against the recording's usual jumps (its 99.9th percentile), `hf` the energy above 6 kHz in the worst 12 ms window
    against the usual energy there. A listener hears a click when either is far above 1."""
    import numpy as np
    from scipy.signal import butter, sosfilt
    d = np.abs(np.diff(x))
    usual = float(np.percentile(d, 99.9)) + 1e-9
    sos = butter(4, 6000, btype="high", fs=rate, output="sos")
    h = sosfilt(sos, x)
    n = max(2, int(window_s * rate))
    k = len(x) // n
    hf_energy = (h[: k * n].reshape(k, n) ** 2).mean(axis=1)
    med = float(np.median(hf_energy[hf_energy > 0])) + 1e-12 if (hf_energy > 0).any() else 1e-12
    out: List[Click] = []
    covered: List[Tuple[int, int]] = []
    for t0, t1 in at:
        a, b = max(0, int((t0 - 0.02) * rate)), min(len(x), int((t1 + 0.02) * rate))
        covered.append((a // n, b // n))
        step = float(d[a:max(a + 1, b - 1)].max() / usual) if b - a > 1 else 0.0
        hf = float(hf_energy[a // n:max(a // n + 1, b // n)].max() / med) if b > a else 0.0
        out.append(Click(round((t0 + t1) / 2, 3), round(step, 2), round(hf, 2), "join", step > step_limit or hf > hf_limit))
    steps = np.array([d[i * n:(i + 1) * n - 1].max() / usual if (i + 1) * n - 1 > i * n else 0.0 for i in range(k)])
    for i in np.where(steps > step_limit * 1.5)[0]:
        if not any(a <= int(i) <= b for a, b in covered):
            out.append(Click(round((i + 0.5) * n / rate, 3), round(float(steps[i]), 2), round(float(hf_energy[i] / med), 2), "elsewhere", True))
    return out
