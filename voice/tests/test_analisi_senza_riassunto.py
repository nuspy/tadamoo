"""`analyze=0` ferma il job di analisi dopo la trascrizione: nessun riassunto, il testo non passa dal cervello.

Niente ffmpeg, nessun modello: ffmpeg, l'STT e il cervello sono sostituiti da finti.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pytest

from callbridge import api_analysis


@pytest.mark.parametrize("valore,atteso", [
    ("1", True), (None, True), ("", True), ("yes", True), (1, True),
    ("0", False), ("false", False), ("No", False), ("off", False), (0, False), (False, False),
])
def test_solo_un_no_esplicito_salta_l_analisi(valore, atteso):
    assert api_analysis._analyze(valore) is atteso


def _giro(tmp_path, monkeypatch, analyze):
    cfg = SimpleNamespace(analysis_segment_s=60, analysis_ffmpeg_path="ffmpeg")

    def finto_ffmpeg(cfg, args, timeout=0):
        d = tmp_path / "job" / "segments"
        d.mkdir(parents=True, exist_ok=True)
        (d / "seg0000.wav").write_bytes(b"\0" * 64000)

    chiamato = []
    monkeypatch.setattr(api_analysis, "_run_ffmpeg", finto_ffmpeg)
    monkeypatch.setattr(api_analysis.diarize, "available", lambda cfg: (False, "prova"))
    monkeypatch.setattr(api_analysis, "_transcribe_block", lambda cfg, wav, words: {"text": "Buongiorno a tutti.", "segments": []})

    def finto_cervello(cfg, segments, **kw):
        chiamato.append(segments)
        return {"summary": "riassunto", "highlights": [], "tasks": []}
    monkeypatch.setattr(api_analysis.analysis_mod, "analyze_transcript", finto_cervello)
    monkeypatch.setattr(api_analysis, "_current", {"id": "job", "state": "queued", "progress": {}, "error": None})
    jdir = tmp_path / "job"
    jdir.mkdir(exist_ok=True)
    (jdir / "input.media").write_bytes(b"x")
    source = {"source_type": "file", "source": "a.wav", "title": "a", "state": "running", "analyze": analyze}
    api_analysis._job(cfg, "job", jdir, source)
    return jdir, chiamato


def test_senza_analisi_il_job_finisce_con_la_trascrizione(tmp_path, monkeypatch):
    jdir, chiamato = _giro(tmp_path, monkeypatch, analyze=False)
    assert chiamato == [] and not (jdir / "analysis.json").exists()
    assert json.loads((jdir / "transcript.json").read_text(encoding="utf-8"))["segments"][0]["text"] == "Buongiorno a tutti."
    assert json.loads((jdir / "source.json").read_text(encoding="utf-8"))["state"] == "done"
    assert api_analysis._current["state"] == "done"


def test_con_l_analisi_di_sempre_il_cervello_e_interpellato(tmp_path, monkeypatch):
    jdir, chiamato = _giro(tmp_path, monkeypatch, analyze=True)
    assert len(chiamato) == 1 and (jdir / "analysis.json").exists()
