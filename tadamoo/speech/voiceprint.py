"""What a voice sounds like, as numbers, so that a take can be judged on more than what it says: is it still the same speaker, at the pace wanted, with a steady pitch.

A synthesised voice that is sampled (XTTS) gives a different intonation, pace and emphasis at each reading of the same text, and some readings sound like another person. The read-back
(voicecheck.py) says whether the words came out; this says whether the *voice* did. Three measures of a reading:

  identity  the cosine between its speaker embedding (WavLM x-vector, a model that tells speakers apart) and the voice's profile, the centre of the embeddings of that voice's good readings;
  pace      syllables per second of active speech (the silence at both ends and the pauses longer than a breath are not counted as speech);
  pitch     the median fundamental frequency against the profile's, and how much it wanders (the standard deviation in semitones, a rushed or jittery reading wanders more).

`Judge.score` folds them with the read-back similarity into one number to choose among the readings of a piece, and says in words why a reading fails. The profile of a voice is built once
from a few standard sentences (`build`), kept in a JSON file, and compared with whatever the voice says later. Models are loaded on first use, on the CPU (the GPU is not needed)."""
from __future__ import annotations

import io
import json
import math
import re
import statistics
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

MODEL = Path(r"E:\Modelli\basalt\qa\wavlm-base-plus-sv")
PROFILES = Path(__file__).with_name("data") / "profiles"
RATE = 16000
VOWELS = "aáeéiíoóöőuúüűAÁEÉIÍOÓÖŐUÚÜŰ"                       # y is a vowel in Italian and English words but not in Hungarian (gy, ly, ny, ty)
VOWELS_Y = VOWELS + "yY"
# the standard sentences a voice's profile is built from: different lengths, sounds and endings
PROFILE_TEXT = {
    "hu": ["A kormány szerdán új intézkedéseket jelentett be az energiaárak csökkentésére,", "Az ellenzék szerint a döntés késve érkezett, és nem oldja meg a problémát,",
           "Budapesten és több vidéki városban is tüntettek a tanárok a hétvégén,", "Az Európai Bizottság közölte, hogy a támogatások folyósítása feltételekhez kötött,",
           "A miniszterelnök a sajtótájékoztatón elmondta, hogy a költségvetés hiánya a tervezettnél kisebb,", "Jó estét kívánok, a mai híradóban a legfontosabb hírekről számolunk be,",
           "A tárgyalások csütörtökön folytatódnak Brüsszelben, ahol több tagállam képviselője is részt vesz,", "Köszönöm, hogy velünk volt, viszontlátásra,"],
    "it": ["Il governo ha annunciato mercoledì nuove misure per ridurre il costo dell'energia,", "Secondo l'opposizione la decisione è arrivata in ritardo e non risolve il problema,",
           "A Roma e in altre città italiane i docenti hanno manifestato durante il fine settimana,", "La Commissione europea ha comunicato che l'erogazione dei fondi dipende da alcune condizioni,",
           "Buonasera, nel telegiornale di oggi vi raccontiamo le notizie più importanti,", "Grazie per averci seguito, arrivederci,"],
    "en": ["The government announced new measures on Wednesday to cut energy prices,", "The opposition says the decision came too late and does not solve the problem,",
           "Teachers marched in the capital and several other cities over the weekend,", "The European Commission said the payment of funds depends on certain conditions,",
           "Good evening, in tonight's bulletin we report the most important news,", "Thank you for watching, goodbye,"],
}


# ---- reading a WAV
def mono16k(wav: bytes) -> Any:
    from tadamoo.speech.asr import pcm16k
    return pcm16k(wav)


def syllables(text: str, lang: str = "hu") -> int:
    """Syllables in a text: in Hungarian every vowel letter is one (it has no diphthongs); elsewhere a group of vowel letters is."""
    words = re.findall(r"[^\W\d_]+", text)
    if lang == "hu":
        return sum(1 for w in words for c in w if c in VOWELS)
    return sum(len(re.findall(f"[{VOWELS_Y}]+", w)) for w in words)


def active_seconds(wav: bytes, floor_db: float = -35.0, max_pause_s: float = 0.35) -> float:
    """The length of the speech in a recording: from its first to its last sound above `floor_db` under the peak, with every pause longer than `max_pause_s` counted as `max_pause_s`
    (a long pause between two sentences is not slowness of the speech)."""
    import numpy as np
    with wave.open(io.BytesIO(wav)) as w:
        sr, width, n = w.getframerate(), w.getsampwidth(), w.getnframes()
        x = np.frombuffer(w.readframes(n), dtype={1: np.int8, 2: np.int16, 4: np.int32}[width]).astype(np.float32) / float(2 ** (8 * width - 1))
    if w.getnchannels() > 1:
        x = x.reshape(-1, w.getnchannels()).mean(axis=1)
    win = max(1, int(sr * 0.02))
    e = np.array([math.sqrt(float((x[i:i + win] ** 2).mean()) + 1e-12) for i in range(0, max(1, len(x) - win), win)])
    on = e > e.max() * 10 ** (floor_db / 20)
    idx = np.where(on)[0]
    if len(idx) == 0:
        return 0.0
    total, quiet = 0.0, 0
    for flag in on[idx[0]:idx[-1] + 1]:
        if flag:
            total += 0.02 + min(quiet * 0.02, max_pause_s)
            quiet = 0
        else:
            quiet += 1
    return total


def pace(wav: bytes, text: str, lang: str = "hu") -> float:
    """Syllables per second of active speech."""
    a = active_seconds(wav)
    return syllables(text, lang) / a if a > 0 else 0.0


def f0(wav: bytes, fmin: float = 70.0, fmax: float = 400.0) -> Dict[str, float]:
    """The median fundamental frequency (Hz) and its spread in semitones (standard deviation of the voiced frames' pitch)."""
    import librosa
    import numpy as np
    y = mono16k(wav)
    track, voiced, _ = librosa.pyin(y, fmin=fmin, fmax=fmax, sr=RATE, frame_length=1024, hop_length=640)
    hz = track[voiced & ~np.isnan(track)]
    if len(hz) < 10:
        return {"median_hz": 0.0, "std_semitones": 0.0, "voiced": 0.0}
    st = 12 * np.log2(hz / np.median(hz))
    return {"median_hz": float(np.median(hz)), "std_semitones": float(np.std(st)), "voiced": float(len(hz) / max(1, len(track)))}


class Embedder:
    """WavLM x-vector speaker embeddings, on the CPU unless a device is named (the model is small: a few hundred MB)."""

    def __init__(self, folder: Path = MODEL, device: str = "cpu") -> None:
        import torch
        from transformers import AutoFeatureExtractor, WavLMForXVector
        self._torch, self.device = torch, device
        self.extractor = AutoFeatureExtractor.from_pretrained(str(folder))
        self.model = WavLMForXVector.from_pretrained(str(folder)).eval().to(device)

    def embed(self, wav: bytes) -> List[float]:
        torch = self._torch
        y = mono16k(wav)
        inputs = self.extractor(y, sampling_rate=RATE, return_tensors="pt", padding=True)
        with torch.no_grad():
            v = self.model(**{k: t.to(self.device) for k, t in inputs.items()}).embeddings[0]
        return torch.nn.functional.normalize(v, dim=-1).tolist()


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / (na * nb) if na and nb else 0.0


# ---- the profile of a voice
@dataclass
class Profile:
    voice: str
    lang: str
    centre: List[float]
    identity_floor: float                          # a reading below this cosine to the centre is another voice
    f0_median_hz: float
    f0_std_semitones: float
    pace_mean: float
    pace_sd: float
    readings: int = 0

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.__dict__, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Profile":
        return cls(**json.loads(path.read_text(encoding="utf-8")))


def profile_path(engine: str, voice: str, lang: str) -> Path:
    return PROFILES / (f"{engine}-{voice}-{lang}".lower().replace(" ", "-") + ".json")


def build(synth: Callable[[str], bytes], embedder: Embedder, voice: str, lang: str = "hu", takes: int = 2, texts: Optional[Sequence[str]] = None) -> Profile:
    """A voice's profile from `takes` readings of each standard sentence: the centre is the mean embedding, the floor two deviations under the readings' own cosines to it (never above
    0.93, never below 0.80), the pitch and pace are the readings' medians."""
    embs, f0s, paces = [], [], []
    for text in texts or PROFILE_TEXT.get(lang, PROFILE_TEXT["en"]):
        for _ in range(takes):
            wav = synth(text)
            embs.append(embedder.embed(wav))
            f0s.append(f0(wav))
            paces.append(pace(wav, text, lang))
    dim = len(embs[0])
    centre = [sum(e[i] for e in embs) / len(embs) for i in range(dim)]
    cos = [cosine(e, centre) for e in embs]
    floor = max(0.80, min(0.93, statistics.fmean(cos) - 2 * statistics.pstdev(cos)))
    return Profile(voice, lang, centre, round(floor, 4), round(statistics.median(p["median_hz"] for p in f0s if p["median_hz"]), 1),
                   round(statistics.median(p["std_semitones"] for p in f0s), 3), round(statistics.fmean(paces), 3), round(statistics.pstdev(paces), 3), len(embs))


# ---- judging a reading
@dataclass
class Targets:
    pace_min: float = 4.0                          # syllables per second the final take (after the tempo change) must be within
    pace_max: float = 5.2
    f0_shift: float = 0.15                         # how far the median pitch may be from the profile's, as a share of it
    f0_std_over: float = 1.3                      # the pitch spread may be this many times the profile's
    similarity_min: float = 0.82                   # the read-back


@dataclass
class Verdict:
    score: float
    ok: bool
    why: List[str] = field(default_factory=list)
    numbers: Dict[str, float] = field(default_factory=dict)


class Judge:
    """Scores a reading of a piece of text against a profile: the read-back similarity, the identity, the pace and the pitch together."""

    def __init__(self, profile: Profile, embedder: Optional[Embedder] = None, targets: Optional[Targets] = None) -> None:
        self.profile, self.embedder, self.targets = profile, embedder or Embedder(), targets or Targets()

    def identity(self, wav: bytes) -> float:
        """The cosine of a reading's speaker embedding to the profile's centre."""
        return cosine(self.embedder.embed(wav), self.profile.centre)

    def score(self, wav: bytes, text: str, similarity: float, lang: Optional[str] = None, known: Optional[Dict[str, Any]] = None) -> Verdict:
        """`known` holds what was measured already (`identity`, `pace`, `pitch`), so it is not measured again."""
        t, p = self.targets, self.profile
        lang = lang or p.lang
        known = known or {}
        ident = known["identity"] if "identity" in known else self.identity(wav)
        pc = known["pace"] if "pace" in known else pace(wav, text, lang)
        pitch = known["pitch"] if "pitch" in known else f0(wav)
        why: List[str] = []
        if similarity < t.similarity_min:
            why.append(f"the read-back matches {similarity:.2f}, at least {t.similarity_min} is needed")
        if ident < p.identity_floor:
            why.append(f"the voice is not the same speaker: {ident:.3f} against a floor of {p.identity_floor:.3f}")
        if not (t.pace_min <= pc <= t.pace_max):
            why.append(f"the pace is {pc:.2f} syllables a second, wanted {t.pace_min} to {t.pace_max}")
        shift = abs(pitch["median_hz"] - p.f0_median_hz) / p.f0_median_hz if p.f0_median_hz and pitch["median_hz"] else 0.0
        if shift > t.f0_shift:
            why.append(f"the pitch is {pitch['median_hz']:.0f} Hz, {shift * 100:.0f}% from the voice's {p.f0_median_hz:.0f}")
        if p.f0_std_semitones and pitch["std_semitones"] > p.f0_std_semitones * t.f0_std_over:
            why.append(f"the pitch wanders {pitch['std_semitones']:.2f} semitones, the voice's own is {p.f0_std_semitones:.2f}")
        pace_mid, pace_half = (t.pace_min + t.pace_max) / 2, (t.pace_max - t.pace_min) / 2
        pace_part = max(0.0, 1 - max(0.0, abs(pc - pace_mid) - pace_half) / 1.0)
        identity_part = max(0.0, min(1.0, (ident - 0.7) / (1 - 0.7)))
        pitch_part = max(0.0, 1 - shift / (2 * t.f0_shift))
        std_part = 1.0 if not p.f0_std_semitones else max(0.0, 1 - max(0.0, pitch["std_semitones"] / p.f0_std_semitones - 1) / 1.0)
        score = 0.45 * similarity + 0.25 * identity_part + 0.15 * pace_part + 0.075 * pitch_part + 0.075 * std_part
        return Verdict(round(score, 4), not why, why, {"similarity": round(similarity, 3), "identity": round(ident, 4), "pace": round(pc, 3), "f0_hz": round(pitch["median_hz"], 1),
                                                         "f0_std_semitones": round(pitch["std_semitones"], 3)})
