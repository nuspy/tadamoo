"""Who is speaking, from a few seconds of their voice: builds on voiceprint.py's embedding (WavLM x-vector, already used there for the
quality of a synthetic reading) to recognise a REAL person instead - on a phone call or talking to the local microphone, matched against
whoever was enrolled with the owner's own hand (a live confirmation on a call, or a guided local recording in tadamoo/contacts.py).

Gender and pitch band are an ORDERING hint, never a gate: the search visits the branch a voice's own pitch best fits first, because that
is usually where the right person is and it is cheap, but a contact who does not match there is never lost - the search climbs back up
and tries the next closest branch, cyclically, until (in the worst case) every enrolled contact has been compared. The decision itself is
always the cosine similarity of the voice embedding, never the branch a voice happened to land in.

A contact enrolled on one channel (phone vs the local microphone) is still compared on the other - the same voice sounds different
through a phone line, so `enroll_voice` in contacts.py also seeds a `phone` reading from a `mic` one with `phone_proxy`, and a real
confirmed call adds a genuine one later."""
from __future__ import annotations

import io
import statistics
import wave
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from tadamoo.speech import voiceprint

CONFIDENT = 0.90                                # a match this close ends the search right away
PASSABLE = 0.80                                 # below this, no match at all - above it but under CONFIDENT, an "uncertain" one
GENDER_SPLIT_HZ = 165.0                         # only an ordering hint between branches, see the module docstring

_embedder: Optional[voiceprint.Embedder] = None


def get_embedder() -> voiceprint.Embedder:
    """The WavLM embedder, loaded once per process (a few hundred MB; see voiceprint.Embedder)."""
    global _embedder
    if _embedder is None:
        _embedder = voiceprint.Embedder()
    return _embedder


def read(wav: bytes) -> Dict[str, Any]:
    """The two measurements a voice is enrolled or matched with: its embedding and its pitch."""
    embedder = get_embedder()
    return {"embedding": embedder.embed(wav), "f0_hz": voiceprint.f0(wav)["median_hz"]}


def phone_proxy(wav: bytes) -> bytes:
    """A rough stand-in for how this same recording would sound over a phone line: a 300-3400 Hz band and a touch of soft compression.
    Seeds the "phone" channel at enrollment time, before any real call has been heard on it."""
    import numpy as np
    from scipy.signal import butter, sosfiltfilt
    y = voiceprint.mono16k(wav)
    sos = butter(4, [300, 3400], btype="bandpass", fs=voiceprint.RATE, output="sos")
    filtered = sosfiltfilt(sos, y)
    filtered = np.tanh(filtered * 3.0) / 3.0    # a phone line compresses dynamics, not only the band
    pcm = np.clip(filtered * 32767, -32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(voiceprint.RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _gender(f0_hz: float) -> str:
    return "f" if f0_hz >= GENDER_SPLIT_HZ else "m"


def _bands(f0s: Sequence[float]) -> List[float]:
    """Two cut points splitting `f0s` into three bands of roughly the same size (tertiles of whoever is already enrolled in this
    branch) - fewer than 3 voices is not enough to split meaningfully, so everyone shares one band."""
    if len(f0s) < 3:
        return []
    ordered = sorted(f0s)
    n = len(ordered)
    return [ordered[n // 3], ordered[(2 * n) // 3]]


def _band(f0_hz: float, cuts: Sequence[float]) -> int:
    if not cuts:
        return 0
    if f0_hz < cuts[0]:
        return 0
    if f0_hz < cuts[1]:
        return 1
    return 2


@dataclass
class Match:
    contact_id: str
    score: float
    channel: str
    confident: bool


def _best_cosine(embedding: Sequence[float], embeddings_by_channel: Dict[str, List[List[float]]]) -> Tuple[float, str]:
    """The best cosine of `embedding` against every reading this contact has, on every channel - the channel that wins is only
    reported for diagnostics, it is never required to match."""
    best_score, best_channel = -1.0, ""
    for channel, vectors in embeddings_by_channel.items():
        for v in vectors:
            s = voiceprint.cosine(embedding, v)
            if s > best_score:
                best_score, best_channel = s, channel
    return best_score, best_channel


def _leaf_order(f0_hz: float, voiced: List[Dict[str, Any]]) -> List[Tuple[str, int]]:
    """Every (gender, band) branch that actually has someone in it, from the one this pitch best fits to the one it fits worst -
    the full order the search walks, not just its first guess."""
    by_gender_f0: Dict[str, List[float]] = {}
    for c in voiced:
        by_gender_f0.setdefault(_gender(c["voice"]["f0_hz"]), []).append(c["voice"]["f0_hz"])
    cuts = {g: _bands(fs) for g, fs in by_gender_f0.items()}
    present = sorted({(_gender(c["voice"]["f0_hz"]), _band(c["voice"]["f0_hz"], cuts[_gender(c["voice"]["f0_hz"])])) for c in voiced})
    own_gender, own_band = _gender(f0_hz), _band(f0_hz, cuts.get(_gender(f0_hz), []))

    def key(leaf: Tuple[str, int]) -> Tuple[int, float]:
        g, b = leaf
        if g == own_gender:
            return (0, abs(b - own_band))
        med = statistics.median(by_gender_f0[g]) if by_gender_f0.get(g) else 0.0
        return (1, abs(f0_hz - med))

    return sorted(present, key=key)


def identify(embedding: Sequence[float], f0_hz: float, contacts: List[Dict[str, Any]]) -> Optional[Match]:
    """The enrolled contact this voice most likely is, or None. Walks branches in `_leaf_order`, returning the moment a comparison
    clears CONFIDENT; otherwise keeps the best seen across every branch and, if it at least clears PASSABLE, returns it unconfident
    rather than silently dropping it - the caller decides what an unconfident match is allowed to do."""
    voiced = [c for c in contacts if (c.get("voice") or {}).get("embeddings")]
    if not voiced:
        return None
    by_leaf: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    by_gender_f0: Dict[str, List[float]] = {}
    for c in voiced:
        by_gender_f0.setdefault(_gender(c["voice"]["f0_hz"]), []).append(c["voice"]["f0_hz"])
    cuts = {g: _bands(fs) for g, fs in by_gender_f0.items()}
    for c in voiced:
        g = _gender(c["voice"]["f0_hz"])
        by_leaf.setdefault((g, _band(c["voice"]["f0_hz"], cuts[g])), []).append(c)

    best: Optional[Match] = None
    for leaf in _leaf_order(f0_hz, voiced):
        for c in by_leaf.get(leaf, []):
            score, channel = _best_cosine(embedding, c["voice"]["embeddings"])
            if best is None or score > best.score:
                best = Match(c["id"], round(score, 4), channel, score >= CONFIDENT)
            if score >= CONFIDENT:
                return best
    if best and best.score >= PASSABLE:
        return best
    return None
