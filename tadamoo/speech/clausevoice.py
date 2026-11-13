"""The voice for a text, made clause by clause with XTTS under our own control, chosen take by take on measurements, and joined without audible joins.

Why not the voice service's `/tts/test`: it takes neither a seed nor the sampling settings, joins a long text's sentences itself (with steps and noise at the joins) and the engine
samples, so two readings of one text differ in pace, intonation and pauses, and a reading may pause in the middle of a name. Here XTTS runs in its own environment (xtts_run.py) with
a fixed seed and a low temperature, the text is cut into clauses at its punctuation, and every clause is read several times (different seeds). A take is *rejected* when:

  - the speech-to-text does not read back the words (a Whisper, through the voice service) or the letters (a Hungarian CTC model that has no language model to guess with);
  - a silence of more than 150 ms falls inside the clause where the text has no punctuation (audiojoin.pause_audit);
  - the speaker is not the voice (voiceprint.py), the pace is outside the wanted range, or the pitch wanders too much.

Of the takes that are not rejected the best combined score is kept. A clause that has none after all the rounds fails the whole text loudly. The kept clauses are joined by audiojoin.py
(cut at both ends, 15 ms fades, a pause for the punctuation filled with room noise) and the result is brought to one loudness. Everything chosen, with each take's numbers, is in the
result, so the same text and parameters can be made again identically."""
from __future__ import annotations

import difflib
import json
import re
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from tadamoo.speech import audiofx, audiojoin as J, textnorm, voicecheck, voiceprint

RUNNER = Path(__file__).with_name("xtts_run.py")
XTTS_PYTHON = r"C:\Projects\callAPIcall\tts-venvs\xtts\Scripts\python.exe"
XTTS_MODEL = r"C:\Projects\callAPIcall\tts-venvs\modelli\xtts_v2"


class VoiceFailed(RuntimeError):
    def __init__(self, message: str, clauses: List[Dict[str, Any]]) -> None:
        super().__init__(message)
        self.clauses = clauses


@dataclass
class Params:
    speaker: str = "Gitta Nikolina"
    lang: str = "hu"
    temperature: float = 0.35                      # low: the engine's default 0.75 samples wider intonation and pauses
    top_p: float = 0.85
    top_k: int = 50
    repetition_penalty: float = 5.0
    length_penalty: float = 1.0
    speed: float = 1.14                            # the engine's own speed setting (it shortens the latents, the pitch stays): at the low temperature the engine reads about 15% slower than at its default
    seeds: int = 4                                 # takes of each clause in a round: a clause with a right take is done, only the others are read again in the next round
    rounds: int = 9                                # rounds before a clause is given up (the most takes of a clause is seeds x rounds)
    base_seed: int = 1000
    comma_ms: float = 250.0
    comma_target_ms: float = 250.0                 # the pause the engine makes itself at a comma inside a piece is brought to this (0: left as it is)
    sentence_ms: float = 500.0
    colon_ms: float = 320.0
    defect_s: float = 0.15                         # a silence longer than this inside a clause, away from punctuation, rejects the take
    warn_s: float = 0.10
    comma_max_s: float = 0.45                      # a pause the engine makes itself at a comma may not be longer than this
    similarity_min: float = 0.80                   # the Whisper read-back (words); it fails on rare names the voice says right, so the two read-backs are judged together
    max_clause_chars: int = 150                    # a sentence longer than this is read in pieces cut at its commas
    letters_min: float = 0.88                      # the CTC read-back (letters; it writes English for the numbers a voice says in Hungarian, so it cannot be stricter)
    combined_min: float = 0.90                     # and the two read-backs together: a Whisper guesses from its language model, a CTC model hears the letters
    pace_min: float = 4.8                          # syllables a second, over a whole sentence of the result
    pace_max: float = 5.6
    clause_pace_min: float = 4.7                   # over one piece (the engine's natural pace is about 5.2, a sentence of hard names reads slower)
    clause_pace_max: float = 6.2
    pace_centre: float = 5.4                       # the pace the choice leans towards
    loudness_lufs: float = -16.0
    xtts_python: str = XTTS_PYTHON
    xtts_model: str = XTTS_MODEL


def split_clauses(text: str, max_len: int = 0) -> List[Tuple[str, str, int]]:
    """The normalised text cut into pieces to read: (the piece without its closing mark, that mark, the sentence it is in). With `max_len` 0 it is cut at every comma, colon, semicolon
    and sentence end. With a limit a sentence is kept whole when it is no longer than `max_len` characters, and otherwise cut at the comma nearest its middle, again and again until
    every piece fits: a short fragment read alone is read slowly and with pauses (an engine reads a whole sentence in one breath), so pieces are as long as the engine reads well."""
    out: List[Tuple[str, str, int]] = []
    sentence = 0
    for part in re.finditer(r"[^,;:.!?…]+[,;:.!?…]*", text):
        raw = part.group(0).strip()
        m = re.match(r"^(.*?)([,;:.!?…]*)$", raw, re.S)
        body, marks = m.group(1).strip(), m.group(2)
        if body:
            out.append((body, marks[0] if marks else ".", sentence))
        if marks and marks[0] in ".!?…":
            sentence += 1
    if max_len <= 0:
        return out
    merged: List[List[Any]] = []                                   # [text with its inner commas, ending, sentence]
    for body, ending, sent in out:
        if merged and merged[-1][2] == sent and merged[-1][1] not in ".!?…":
            merged[-1] = [merged[-1][0] + merged[-1][1] + " " + body, ending, sent]
        else:
            merged.append([body, ending, sent])
    final: List[Tuple[str, str, int]] = []

    def cut(body: str, ending: str, sent: int) -> None:
        if len(body) <= max_len or "," not in body:
            final.append((body, ending, sent))
            return
        commas = [i for i, c in enumerate(body) if c == ","]
        i = min(commas, key=lambda k: abs(k - len(body) / 2))
        cut(body[:i].strip(), ",", sent)
        cut(body[i + 1:].strip(), ending, sent)

    for body, ending, sent in merged:
        cut(body, ending, sent)
    return final


class LocalXtts:
    """XTTS in its own Python (the voice service's environment is not touched): a batch of items in, WAV bytes out. The process is started with the first batch and keeps the model
    loaded for the next ones (loading the model takes longer than reading a short sentence); `close` ends it and frees its VRAM."""

    def __init__(self, p: Params, work: Optional[Path] = None) -> None:
        self.p = p
        self.work = Path(work) if work else Path(tempfile.mkdtemp(prefix="xtts_"))
        self.proc: Optional[subprocess.Popen] = None
        self.log_file: Any = None
        self.batches = 0

    def _start(self) -> None:
        self.work.mkdir(parents=True, exist_ok=True)
        self.log_file = open(self.work / "xtts.log", "ab")
        self.proc = subprocess.Popen([self.p.xtts_python, str(RUNNER), "--serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log_file, text=True, encoding="utf-8",
                                     errors="replace", bufsize=1)

    def _batch(self, job_file: Path) -> None:
        """One job file through the running process: its output lines are read up to the line that says the batch is done."""
        if self.proc is None or self.proc.poll() is not None:
            self._start()
        try:
            self.proc.stdin.write(str(job_file) + "\n")
            self.proc.stdin.flush()
        except OSError:
            self.proc = None
            raise RuntimeError("the XTTS run failed: its process is gone") from None
        for line in self.proc.stdout:
            if '"batch"' in line:
                self.batches += 1
                return
        self.proc = None
        log = self.work / "xtts.log"
        tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1:] if log.is_file() else []
        raise RuntimeError("the XTTS run failed: " + (tail[0][:300] if tail else "no message"))

    def close(self) -> None:
        if self.proc is not None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
            self.proc = None
        if self.log_file is not None:
            self.log_file.close()
            self.log_file = None

    def run(self, items: Sequence[Dict[str, Any]]) -> Dict[str, bytes]:
        self.work.mkdir(parents=True, exist_ok=True)
        out = self.work / "out"
        out.mkdir(parents=True, exist_ok=True)

        def have(it: Dict[str, Any]) -> bool:                    # a take made already with the same text, seed and settings is not made again
            side = out / f"{it['id']}.json"
            return (out / f"{it['id']}.wav").is_file() and side.is_file() and json.loads(side.read_text(encoding="utf-8")) == {**it, "speaker": self.p.speaker}

        todo = [it for it in items if not have(it)]
        if todo:
            job = {"model_dir": self.p.xtts_model, "speaker": self.p.speaker, "out_dir": str(out), "items": todo}
            job_file = self.work / "job.json"
            job_file.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            self._batch(job_file)
            for it in todo:
                (out / f"{it['id']}.json").write_text(json.dumps({**it, "speaker": self.p.speaker}, ensure_ascii=False), encoding="utf-8")
        return {it["id"]: (out / f"{it['id']}.wav").read_bytes() for it in items}


NUMBER_ROOTS = ("egy", "kettő", "két", "három", "négy", "öt", "hat", "hét", "nyolc", "kilenc", "tíz", "tizen", "húsz", "huszon", "harminc", "negyven", "ötven", "hatvan", "hetven", "nyolcvan",
                "kilencven", "száz", "ezer", "millió", "milliárd", "billió")
SURE_ROOTS = ("tizen", "huszon", "harminc", "negyven", "ötven", "hatvan", "hetven", "nyolcvan", "kilencven", "ezer", "millió", "milliárd", "billió")
NUMBER_WORD = re.compile(r"(?:" + "|".join(sorted(NUMBER_ROOTS, key=len, reverse=True)) + r")+\w{0,6}", re.I)


def has_number_words(text: str, lang: str) -> bool:
    """The text holds a number the normaliser spelled out (Hungarian: a word made of number roots, such as kétezer-huszonnyolc): the CTC read-back writes such a number in English, so its letters cannot judge the clause."""
    if lang != "hu":
        return False
    for word in re.split(r"[\s-]+", text.casefold()):
        m = NUMBER_WORD.fullmatch(word)
        if not m:
            continue
        roots = re.findall("|".join(sorted(NUMBER_ROOTS, key=len, reverse=True)), word)
        if len(roots) >= 2 or any(word.startswith(r) for r in SURE_ROOTS):
            return True
    return False


def letters_similarity(want: str, heard: str) -> float:
    """The share of letters read back right, ignoring spaces, case, accents and punctuation (what a model that has no language model wrote against what was meant)."""
    a = "".join(c for c in voicecheck.fold(want) if c.isalpha())
    b = "".join(c for c in voicecheck.fold(heard) if c.isalpha())
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() if a or b else 1.0


@dataclass
class Take:
    seed: int
    wav: bytes
    score: float = 0.0
    ok: bool = False
    why: List[str] = field(default_factory=list)
    numbers: Dict[str, float] = field(default_factory=dict)


class Rater:
    """Scores takes: Whisper through the voice service (words and their times), a CTC model (letters), the speaker, the pace, the pitch and the pauses. What is measured about a take
    is kept in `cache` (a JSON file) by the hash of its audio, so a take is measured once and judged again as often as the limits change."""

    def __init__(self, url: str, ctc: Optional[Any], judge: voiceprint.Judge, p: Params, cache: Optional[Path] = None) -> None:
        self.url, self.ctc, self.judge, self.p = url, ctc, judge, p
        self.cache_path = cache
        self.cache: Dict[str, Any] = json.loads(cache.read_text(encoding="utf-8")) if cache and cache.is_file() else {}

    def measure(self, wav: bytes, text: str, ending: str) -> Dict[str, Any]:
        import hashlib
        key = hashlib.sha1(wav + text.encode("utf-8")).hexdigest()
        if key in self.cache:
            return self.cache[key]
        p = self.p
        x, rate = J.read(wav)
        words = voicecheck.transcribe(self.url, wav, p.lang)
        chk = voicecheck.compare(text, words, p.lang)
        heard_letters = self.ctc.transcribe(wav, p.lang) if self.ctc is not None else ""
        audit = J.pause_audit(x, rate, text + ending, words, 0.0, 0.10, p.lang)
        v = self.judge.score(wav, text, chk.similarity, p.lang)
        numbers = has_number_words(text, p.lang)
        m = {"similarity": chk.similarity, "heard": chk.heard, "extra_tail": chk.extra_tail, "numbers": numbers,
             "letters": (chk.similarity if numbers else letters_similarity(text, heard_letters)) if self.ctc is not None else 1.0,
             "heard_letters": heard_letters, "silences": [{"len_s": s.length_s, "after": s.after, "punct": s.punct} for s in audit], **v.numbers}
        self.cache[key] = m
        if self.cache_path:
            self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False), encoding="utf-8")
        return m

    @staticmethod
    def tail_ok(text: str, m: Dict[str, Any]) -> bool:
        """The last written word came back whole: the Whisper's last word is it, or the CTC model's letters end with it. A voice that drops the last syllable of a sentence (an engine
        that cuts a final vowel) is the commonest fault of Hungarian synthesis."""
        want = voicecheck.tokens(text)
        if not want:
            return True
        last = want[-1]
        from tadamoo.speech import textnorm
        heard = voicecheck.tokens(textnorm.normalise(str(m.get("heard", "")), "hu"))
        letters = "".join(c for c in voicecheck.fold(str(m.get("heard_letters", ""))) if c.isalpha())
        return bool((heard and heard[-1] == last) or letters.endswith("".join(c for c in last if c.isalpha())))

    def judge_take(self, m: Dict[str, Any], seed: int, wav: bytes, text: str = "") -> Take:
        p = self.p
        why: List[str] = []
        if text and not self.tail_ok(text, m):
            why.append("the last word is not read back whole (the voice dropped its last syllable)")
        if m["similarity"] < p.similarity_min:
            why.append(f"the Whisper read-back matches {m['similarity']:.2f}, at least {p.similarity_min}")
        if m["letters"] < p.letters_min:
            why.append(f"the letters read back match {m['letters']:.2f}, at least {p.letters_min}")
        if (m["similarity"] + m["letters"]) / 2 < p.combined_min:
            why.append(f"the two read-backs together match {(m['similarity'] + m['letters']) / 2:.2f}, at least {p.combined_min}")
        if m["extra_tail"]:
            why.append("words after the end: " + " ".join(m["extra_tail"]))
        bad = [s for s in m["silences"] if not s["punct"] and s["len_s"] > p.defect_s]
        for s in bad:
            why.append(f"a pause of {s['len_s'] * 1000:.0f} ms after '{s['after'] or '?'}' with no punctuation there")
        for s in m["silences"]:
            if s["punct"] and s["len_s"] > p.comma_max_s:
                why.append(f"a pause of {s['len_s'] * 1000:.0f} ms at '{s['punct']}' after '{s['after']}', over {p.comma_max_s * 1000:.0f} ms")
        ident, pace = m["identity"], m["pace"]
        if ident < self.judge.profile.identity_floor:
            why.append(f"not the same speaker ({ident:.3f} under {self.judge.profile.identity_floor:.3f})")
        if not (p.clause_pace_min <= pace <= p.clause_pace_max):
            why.append(f"the pace is {pace:.2f} syllables a second, wanted {p.clause_pace_min} to {p.clause_pace_max}")
        if self.judge.profile.f0_std_semitones and m["f0_std_semitones"] > self.judge.profile.f0_std_semitones * self.judge.targets.f0_std_over:
            why.append(f"the pitch wanders {m['f0_std_semitones']:.2f} semitones")
        centre = p.pace_centre
        pace_part = max(0.0, 1 - abs(pace - centre) / 1.5)
        score = 0.30 * m["similarity"] + 0.30 * m["letters"] + 0.15 * max(0.0, min(1.0, (ident - 0.7) / 0.3)) + 0.10 * pace_part + 0.15 * (1.0 if not bad else 0.0)
        numbers = {"similarity": round(m["similarity"], 3), "letters": round(m["letters"], 3), "identity": round(ident, 4), "pace": round(pace, 2), "f0_hz": m["f0_hz"],
                   "f0_std_semitones": m["f0_std_semitones"], "pauses_over_100ms": len(m["silences"]), "pause_defects": len(bad)}
        return Take(seed, wav, round(score, 4), not why, why, numbers)

    def rate(self, wav: bytes, text: str, ending: str, seed: int) -> Take:
        return self.judge_take(self.measure(wav, text, ending), seed, wav, text)


@dataclass
class Result:
    wav: bytes
    clauses: List[Dict[str, Any]]
    joins: List[Dict[str, Any]]
    sentences: List[Dict[str, Any]]
    params: Dict[str, Any]
    seconds: float = 0.0
    commas: List[Dict[str, Any]] = field(default_factory=list)       # what was done at each comma inside a piece


def speak(text: str, runner: LocalXtts, rater: Rater, p: Params, log: Callable[[str], None] = print, pronunciations: Optional[Dict[str, str]] = None,
          say: Optional[Callable[[str], str]] = None) -> Result:
    """The text as one WAV (24 kHz mono, at p.loudness_lufs), the clauses chosen on measurements; raises VoiceFailed when a clause has no take that is right."""
    began = time.time()
    norm = textnorm.normalise(text, p.lang, pronunciations)
    clauses = split_clauses(norm, p.max_clause_chars)
    if not clauses:
        raise VoiceFailed("there is no text to read", [])
    chosen: Dict[int, Take] = {}
    tried: Dict[int, List[Take]] = {i: [] for i in range(len(clauses))}
    for rnd in range(p.rounds):
        todo = [i for i in range(len(clauses)) if i not in chosen]
        if not todo:
            break
        items = []
        for i in todo:
            body, ending, _ = clauses[i]
            for k in range(p.seeds):
                seed = p.base_seed + i * 1000 + rnd * 100 + k
                items.append({"id": f"c{i}_s{seed}", "text": (say(body) if say else body) + ",", "lang": p.lang, "seed": seed, "temperature": p.temperature, "top_p": p.top_p, "top_k": p.top_k,
                              "repetition_penalty": p.repetition_penalty, "length_penalty": p.length_penalty, "speed": p.speed})
        wavs = runner.run(items)
        log(f"round {rnd + 1}: {len(items)} takes made for {len(todo)} clauses in {round(time.time() - began)} s")
        for it in items:
            i = int(it["id"][1:].split("_")[0])
            body, ending, _ = clauses[i]
            take = rater.rate(wavs[it["id"]], body, ending, it["seed"])
            tried[i].append(take)
        for i in todo:
            good = [t for t in tried[i] if t.ok]
            if good:
                chosen[i] = max(good, key=lambda t: t.score)
                log(f"clause {i}: seed {chosen[i].seed} score {chosen[i].score} ({len(good)} of {len(tried[i])} takes were right)  '{clauses[i][0][:40]}'")
    report = []
    for i, (body, ending, sent) in enumerate(clauses):
        best = max(tried[i], key=lambda t: t.score) if tried[i] else None
        report.append({"index": i, "text": body, "ending": ending, "sentence": sent, "ok": i in chosen, "seed": (chosen.get(i) or best).seed if (chosen.get(i) or best) else None,
                       "numbers": (chosen.get(i) or best).numbers if (chosen.get(i) or best) else {}, "why": [] if i in chosen else (best.why if best else ["no take"]),
                       "takes": len(tried[i]), "right": sum(1 for t in tried[i] if t.ok),
                       "all": [{"seed": t.seed, "score": t.score, "ok": t.ok, "why": t.why} for t in tried[i]]})
    failed = [c for c in report if not c["ok"]]
    if failed:
        raise VoiceFailed("no right take for: " + "; ".join(f"'{c['text'][:40]}' ({'; '.join(c['why'][:2])})" for c in failed), report)
    pieces = []
    comma_notes: List[Dict[str, Any]] = []
    for i, (body, ending, _) in enumerate(clauses):
        x, rate = J.read(chosen[i].wav)
        if p.comma_target_ms:
            x, notes = J.comma_pauses(x, rate, body + ending, voicecheck.transcribe(rater.url, chosen[i].wav, p.lang), p.comma_target_ms / 1000, p.lang)
            comma_notes += [{"clause": i, **n} for n in notes]
        pieces.append((x, rate, ending))
    samples, rate, joins, spans = J.assemble(pieces, p.comma_ms, p.sentence_ms, seed=p.base_seed)
    wav = audiofx.loudnorm(J.write(samples, rate), p.loudness_lufs)
    sentences = []
    for s in sorted({c[2] for c in clauses}):
        idx = [i for i, c in enumerate(clauses) if c[2] == s]
        t0, t1 = spans[idx[0]][0], spans[idx[-1]][1]
        syl = sum(voiceprint.syllables(clauses[i][0], p.lang) for i in idx)
        sentences.append({"sentence": s, "text": " ".join(clauses[i][0] + clauses[i][1] for i in idx), "start_s": t0, "end_s": t1, "syllables": syl, "pace": round(syl / (t1 - t0), 2)})
    return Result(wav, report, [j.__dict__ for j in joins], sentences, {k: v for k, v in p.__dict__.items() if k not in ("xtts_python", "xtts_model")}, round(time.time() - began, 1), comma_notes)
