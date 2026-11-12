"""Finding, for each hard word, the respelling a voice reads best, and writing it in the lexicon (respell.py).

For one word: the word as written is read in a short carrier sentence several times (the engine samples, so each reading is another one) and each reading is read back by several
different speech-to-text models (asr.py: a Whisper through the voice service, Whisper large-v3, and Hungarian CTC models that have no language model and so write what was
really heard). A reading scores by how many letters of the word came back right, between the carrier's words around it. A word that reads well as written is left alone. For
a word that does not, `respell.candidates` gives the respellings to try; each is read once, the best few are read again, and the best is kept only if it beats the word as
written by a margin. What is kept goes in the lexicon with its score in a side file, so a person can see why.

    python -m tadamoo.speech.optimize --vocab data/vocab/hu_news.txt --voice "Gitta Nikolina" --engine xtts

The synthesizer is a plain callable (text -> WAV bytes), so a cloud engine can be tried the same way (tts_engines.py). Progress is saved after each word and the run resumes."""
from __future__ import annotations

import argparse
import difflib
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from tadamoo.speech import asr as asr_mod
from tadamoo.speech import respell, service as speech, voicecheck

CARRIERS = (
    "Ezt a szót most jól kell kiejteni: {w}, és ezt mindenki meghallja.",
    "A mai híradóban elhangzott: {w}, ahogy az előbb mondták.",
    "A riporter szerint: {w}, vagyis erről lesz most szó.",
)
EASY = 0.97                       # a word as written that scores at least this is left alone
MARGIN = 0.03                     # a respelling must beat the word as written by this to be kept
BASE_SEEDS = 3
FINALISTS = 3
FINAL_SEEDS = 5
DATA = Path(__file__).with_name("data")


def letters(text: str) -> str:
    return "".join(c for c in voicecheck.fold(text) if c.isalpha())


def word_score(pre: str, target: str, post: str, heard: str) -> float:
    """How much of `target` came back right between the carrier words `pre` and `post` (1.0: every letter, nothing added in its place). Letters only, so a hyphen or a space the
    speech-to-text puts in a word does not count against it."""
    a, t, b = letters(pre), letters(target), letters(post)
    if not t:
        return 1.0
    lo, hi = len(a), len(a) + len(t)
    matcher = difflib.SequenceMatcher(None, a + t + b, letters(heard), autojunk=False)
    matched = extra = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        o1, o2 = max(i1, lo), min(i2, hi)
        if tag == "equal" and o2 > o1:
            matched += o2 - o1
        elif tag == "replace" and o2 > o1:
            extra += max(0, (j2 - j1) - (o2 - o1))
        elif tag == "insert" and lo < i1 < hi:
            extra += j2 - j1
    return matched / (len(t) + extra)


@dataclass
class Reading:
    spelling: str                                  # what was sent to the voice
    carrier: int
    scores: Dict[str, float]                       # speech-to-text name -> score
    heard: Dict[str, str] = field(default_factory=dict)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.scores.values()) if self.scores else 0.0


class Rater:
    """Reads a word in a carrier with `synth` and scores the reading with every speech-to-text."""

    def __init__(self, synth: Callable[[str], bytes], stts: Sequence[asr_mod.Asr], lang: str = "hu") -> None:
        self.synth, self.stts, self.lang = synth, list(stts), lang
        self.readings = 0

    def read(self, word: str, spelling: str, n: int) -> Reading:
        carrier = CARRIERS[n % len(CARRIERS)]
        pre, post = carrier.split("{w}")
        wav = self.synth(carrier.replace("{w}", spelling))
        self.readings += 1
        scores, heard = {}, {}
        for stt in self.stts:
            try:
                text = stt.transcribe(wav, self.lang)
            except Exception as exc:               # one speech-to-text failing must not stop the run; it just does not vote
                text = ""
                print(f"  {stt.name}: {type(exc).__name__}", file=sys.stderr)
            heard[stt.name] = text
            scores[stt.name] = word_score(pre, word, post, text)
        return Reading(spelling, n % len(CARRIERS), scores, heard)


def mean_of(readings: Sequence[Reading]) -> float:
    return statistics.fmean(r.mean for r in readings) if readings else 0.0


def per_stt(readings: Sequence[Reading]) -> Dict[str, float]:
    names = readings[0].scores.keys() if readings else []
    return {n: round(statistics.fmean(r.scores[n] for r in readings), 3) for n in names}


def optimise_word(rater: Rater, word: str, easy: float = EASY, margin: float = MARGIN, base_seeds: int = BASE_SEEDS, finalists: int = FINALISTS,
                  final_seeds: int = FINAL_SEEDS) -> Dict[str, Any]:
    """The best respelling of one word and how it was found. `say` is the word as written when nothing beat it."""
    base = [rater.read(word, word, i) for i in range(base_seeds)]
    out: Dict[str, Any] = {"word": word, "say": word, "baseline": round(mean_of(base), 3), "baseline_by_stt": per_stt(base), "heard": base[0].heard}
    if mean_of(base) >= easy:
        out["verdict"] = "fine as written"
        return out
    trials: Dict[str, List[Reading]] = {}
    for k, cand in enumerate(respell.candidates(word)[1:]):
        trials[cand] = [rater.read(word, cand, k)]
    ranked = sorted(trials, key=lambda c: -mean_of(trials[c]))[:finalists]
    for cand in ranked:
        trials[cand] += [rater.read(word, cand, i) for i in range(1, final_seeds)]
    base += [rater.read(word, word, i) for i in range(base_seeds, final_seeds)]
    out["baseline"], out["baseline_by_stt"] = round(mean_of(base), 3), per_stt(base)
    best = max(ranked, key=lambda c: mean_of(trials[c])) if ranked else word
    out["tried"] = {c: round(mean_of(trials[c]), 3) for c in trials}
    if best != word and mean_of(trials[best]) >= mean_of(base) + margin:
        out.update(say=best, score=round(mean_of(trials[best]), 3), by_stt=per_stt(trials[best]), verdict="respelled", heard_after=trials[best][0].heard)
    else:
        out["verdict"] = "no respelling beat the word as written"
    return out


def read_vocab(path: Path) -> List[str]:
    words = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    return list(dict.fromkeys(words))


def run(vocab: Sequence[str], rater: Rater, out_dir: Path, name: str, limit: int = 0, log: Callable[[str], None] = print) -> Dict[str, Dict[str, Any]]:
    """Every word of `vocab`, resuming from `<name>.scores.json`; the lexicon `<name>.json` (word -> respelling, only the respelled words) is rewritten after each word."""
    out_dir.mkdir(parents=True, exist_ok=True)
    scores_path, lex_path = out_dir / f"{name}.scores.json", out_dir / f"{name}.json"
    done: Dict[str, Dict[str, Any]] = json.loads(scores_path.read_text(encoding="utf-8")) if scores_path.is_file() else {}
    started = time.time()
    todo = [w for w in vocab if w not in done]
    if limit:
        todo = todo[:limit]
    for i, word in enumerate(todo, 1):
        done[word] = optimise_word(rater, word)
        lexicon = {w: d["say"] for w, d in done.items() if d.get("say") and d["say"] != w}
        scores_path.write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
        lex_path.write_text(json.dumps({"_note": "word -> respelling, made by optimize.py; the scores are in the .scores.json beside it", **lexicon}, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        d = done[word]
        log(f"[{i}/{len(todo)}] {word}: {d['baseline']} -> {d.get('score', d['baseline'])} {d['say'] if d['say'] != word else ''} ({d['verdict']}) {round(time.time() - started)}s")
    return done


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--vocab", default=str(DATA / "vocab" / "hu_news.txt"))
    ap.add_argument("--voice", default="Gitta Nikolina")
    ap.add_argument("--engine", default="xtts")
    ap.add_argument("--url", default=speech.DEFAULT_URL)
    ap.add_argument("--out", default=str(DATA / "lexicon" / "hu"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--key", default="")
    args = ap.parse_args(argv)
    stts = asr_mod.load_all(args.url)
    print("speech-to-text:", ", ".join(s.name for s in stts))
    rater = Rater(lambda text: speech.synthesize(args.url, text, args.engine, args.voice, "hu", args.key), stts)
    name = f"{args.engine}-{args.voice}".lower().replace(" ", "-")
    run(read_vocab(Path(args.vocab)), rater, Path(args.out), name, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
