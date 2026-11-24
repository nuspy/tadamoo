"""The voice's audio: the clause split, the joins, the pause audit and the click report on synthetic signals, the syllable and speaker numbers, the respelling."""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy")                              # the speech worker's library (the joins and the click report use it)

from tadamoo.speech import audiojoin as J, clausevoice, respell, voiceprint

RATE = 24000


def tone(seconds: float, freq: float = 180.0, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    voiced = amp * np.sin(2 * np.pi * freq * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))
    return (voiced + np.random.default_rng(int(freq)).standard_normal(len(t)) * 0.01).astype(np.float32)        # a little breath noise, as in speech


def noise(seconds: float, rms: float = 0.002, seed: int = 0) -> np.ndarray:
    return (np.random.default_rng(seed).standard_normal(int(seconds * RATE)) * rms).astype(np.float32)


CLAUSES = [
    ("Csütörtökön Brüsszelben találkozott Orbán Viktor és Magyar Péter, a Fidesz és a Tisza elnöke.",
     [("Csütörtökön Brüsszelben találkozott Orbán Viktor és Magyar Péter", ",", 0), ("a Fidesz és a Tisza elnöke", ".", 0)]),
    ("Egy. Kettő, három; négy: öt!", [("Egy", ".", 0), ("Kettő", ",", 1), ("három", ";", 1), ("négy", ":", 1), ("öt", "!", 1)]),
    ("Nincs pont a végén", [("Nincs pont a végén", ".", 0)]),
]


@pytest.mark.parametrize("text,want", CLAUSES)
def test_clauses_are_cut_at_punctuation(text, want):
    assert clausevoice.split_clauses(text, 0) == want


def test_assemble_joins_pieces_with_pauses_and_no_steps():
    pieces = [(tone(1.0) + 0.05, RATE, ","), (tone(0.8, 220.0), RATE, "."), (tone(0.6, 200.0), RATE, ".")]
    x, rate, joins, spans = J.assemble(pieces, comma_ms=250, sentence_ms=500)
    assert rate == RATE and [j.kind for j in joins] == ["comma", "sentence"]
    assert [round(j.pause_s, 2) for j in joins] == [0.25, 0.5]
    assert len(spans) == 3 and spans[0][1] < spans[1][0] < spans[1][1] < spans[2][0]
    clicks = J.click_report(x, rate, [(j.time_s, j.time_s + j.pause_s) for j in joins])
    assert not any(c.bad for c in clicks), clicks
    assert abs(float(x.mean())) < 0.002                                    # the DC offset of the first piece is gone


def test_pause_filled_with_room_tone_not_digital_silence():
    x, rate, joins, _ = J.assemble([(tone(0.5), RATE, ","), (tone(0.5), RATE, ".")], comma_ms=300)
    a = int((joins[0].time_s + 0.05) * rate)
    b = int((joins[0].time_s + joins[0].pause_s - 0.05) * rate)
    assert 0 < float(np.abs(x[a:b]).max()) < 0.02


def test_a_click_at_a_join_is_found():
    x = np.concatenate([tone(1.0), np.zeros(int(0.2 * RATE), np.float32), tone(1.0) + 0.4])           # a DC step: the second piece starts at 0.4
    clicks = J.click_report(x, RATE, [(1.0, 1.2)])
    assert clicks[0].bad and clicks[0].step > 3


def test_silences_inside_speech_are_listed():
    x = np.concatenate([tone(0.8), noise(0.22), tone(0.8), noise(0.08), tone(0.5)])
    found = J.silences(x, RATE, min_s=0.12)
    assert len(found) == 1 and abs(found[0][0] - 0.8) < 0.03 and abs((found[0][1] - found[0][0]) - 0.22) < 0.03


def words_at(times):
    return [{"text": w, "start_s": s, "end_s": e} for w, s, e in times]


def test_a_pause_inside_a_name_is_a_defect_and_one_at_a_comma_is_not():
    x = np.concatenate([tone(0.8), noise(0.22), tone(0.8), noise(0.28), tone(0.8)])
    heard = words_at([("Orbán", 0.0, 0.8), ("Viktor", 1.02, 1.82), ("elnöke", 2.1, 2.9)])
    audit = J.pause_audit(x, RATE, "Orbán Viktor, elnöke", heard)
    assert [(s.after, s.defect) for s in audit] == [("orban", True), ("viktor", False)]
    assert audit[1].punct == ","


def test_a_short_pause_between_words_is_not_a_defect():
    x = np.concatenate([tone(0.8), noise(0.1), tone(0.8)])
    audit = J.pause_audit(x, RATE, "Orbán Viktor", words_at([("Orbán", 0.0, 0.8), ("Viktor", 0.9, 1.7)]), min_s=0.12)
    assert audit == []


def test_pause_for_the_punctuation():
    assert J.pause_for(",") == 0.25 and J.pause_for(".") == 0.5 and J.pause_for(":") == 0.32


# ---- the voice numbers
def test_hungarian_syllables_are_its_vowels():
    assert voiceprint.syllables("Magyar Péter", "hu") == 4            # the y of gy is not a vowel
    assert voiceprint.syllables("Brüsszelben", "hu") == 3
    assert voiceprint.syllables("buongiorno a tutti", "it") == 6


def test_cosine():
    assert voiceprint.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert voiceprint.cosine([1, 0], [0, 1]) == pytest.approx(0.0)


def test_pace_counts_syllables_over_the_speech_not_the_silence():
    import io, wave
    x = np.concatenate([noise(0.5), tone(2.0), noise(0.5)])
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes((x * 32767).astype(np.int16).tobytes())
    pace = voiceprint.pace(buf.getvalue(), "aaaaaaaaaa", "hu")      # ten syllables in two seconds of sound
    assert 4.5 <= pace <= 5.5


# ---- the respelling
def test_syllables_of_hungarian_words():
    assert respell.syllables("Brüsszelben") == ["Brüsz", "szel", "ben"]
    assert respell.syllables("asszony") == ["asz", "szony"]
    assert respell.syllables("Országgyűlés")[:2] == ["Or", "szágy"]


def test_lexicon_serves_suffixed_forms_and_keeps_case():
    lex = respell.Lexicon({"Brüsszel": "Brüsz-szel"})
    assert lex.apply("Brüsszelben és Brüsszel") == "Brüsz-szelben és Brüsz-szel"
    assert lex.lookup("brüsszelből") == "Brüsz-szelből"
    assert lex.lookup("Budapest") is None


def test_respell_leaves_other_languages_and_unknown_words_alone():
    lex = respell.Lexicon({"Brüsszel": "Brüsz-szel"})
    assert respell.respell("Brüsszel", "it", lex) == "Brüsszel"
    assert respell.respell("Budapest", "hu", lex) == "Budapest"


def test_candidates_start_with_the_word_and_have_no_duplicates():
    c = respell.candidates("Brüsszelben")
    assert c[0] == "Brüsszelben" and len(c) == len(set(c)) and "Brüsz-szel-ben" in c


def test_a_sentence_that_fits_is_read_whole_and_a_long_one_is_cut_at_the_comma_nearest_its_middle():
    text = "Csütörtökön Brüsszelben találkozott Orbán Viktor és Magyar Péter, a Fidesz és a Tisza elnöke."
    assert clausevoice.split_clauses(text, 150) == [(text[:-1].replace(", a", ", a"), ".", 0)]
    long = "Az Európai Bizottság közölte az új szabályokat, a tagállamok pedig vitatkoztak a részletekről, és csak késő este született megállapodás."
    parts = clausevoice.split_clauses(long, 90)
    assert [p[1] for p in parts] == [",", ",", "."] or [p[1] for p in parts] == [",", "."]
    assert all(len(p[0]) <= 90 for p in parts) and {p[2] for p in parts} == {0}


def test_a_short_pause_at_a_comma_is_brought_to_the_target_without_a_step():
    x = np.concatenate([noise(0.1, 3e-5, 1), tone(0.8), noise(0.07, 3e-5, 2), tone(0.8, 220.0), noise(0.1, 3e-5, 3)])        # the silence of a voice engine: under -90 dBFS
    heard = words_at([("Orbán", 0.1, 0.9), ("Viktor", 0.97, 1.77)])
    y, notes = J.comma_pauses(x, RATE, "Orbán, Viktor", heard, 0.25)
    assert abs(len(y) - len(x) - int(0.18 * RATE)) < RATE * 0.03
    found = [f for f in J.silences(y, RATE, 0.12) if 0.5 < f[0] < 1.5]
    assert len(found) == 1 and abs((found[0][1] - found[0][0]) - 0.25) < 0.04
    assert not any(c.bad for c in J.click_report(y, RATE, [(found[0][0], found[0][1])]))
    assert "made 250 ms" in notes[0]["result"]


def test_a_comma_with_no_quiet_stretch_is_left_alone():
    x = np.concatenate([tone(0.8), tone(0.8, 220.0)])
    y, notes = J.comma_pauses(x, RATE, "Orbán, Viktor", words_at([("Orbán", 0.0, 0.8), ("Viktor", 0.8, 1.6)]), 0.25)
    assert len(y) == len(x) and notes[0]["result"].startswith("no quiet")

