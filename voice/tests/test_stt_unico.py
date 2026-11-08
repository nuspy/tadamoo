"""Test dello STT unico (engines/stt_shared.py) con un FINTO processo helper.

Nessun modello, nessuna GPU: il processo vero e' sostituito da
`fake_stt_helper.py`, che parla lo stesso protocollo. La prova con whisper vero
(GPU/CPU) e' un'altra cosa e si fa a mano in un processo isolato.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np
import pytest

from callbridge import engines
from callbridge.engines import stt_shared
from callbridge.engines.stt_shared import SharedSttEngine
from callbridge.engines.whisper_stt import precisione_effettiva

FINTO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_stt_helper.py")
AUDIO = np.zeros(1600, dtype=np.float32)


def _cfg(model="medium", device="cuda", comp="auto"):
    return SimpleNamespace(stt_model=model, stt_device=device,
                           stt_gpu_memory="dedicated", stt_compute_type=comp,
                           stt_language="")


@pytest.fixture
def finto(monkeypatch):
    monkeypatch.setattr(stt_shared, "_comando_helper",
                        lambda: [sys.executable, "-u", FINTO])
    monkeypatch.setattr(engines, "_stt_engine", None)
    creati: list[SharedSttEngine] = []

    def nuovo(cfg=None):
        e = SharedSttEngine(cfg or _cfg())
        creati.append(e)
        return e

    yield nuovo
    for e in creati:
        e.chiudi()


def _attendi(e, stato="running", t=10.0):
    fine = time.monotonic() + t
    while time.monotonic() < fine:
        if e.state == stato:
            return True
        time.sleep(0.02)
    return False


# --- un solo STT --------------------------------------------------------------

def test_get_stt_engine_e_un_singleton_condiviso(monkeypatch):
    monkeypatch.setattr(engines, "_stt_engine", None)
    cfg = _cfg()
    a = engines._get_stt_engine(cfg)
    assert isinstance(a, SharedSttEngine)
    assert engines._get_stt_engine(cfg) is a
    monkeypatch.setattr(engines, "_stt_engine", None)


def test_la_dettatura_usa_lo_stesso_stt_delle_sessioni(monkeypatch):
    from callbridge import dictation
    monkeypatch.setattr(engines, "_stt_engine", None)
    monkeypatch.setattr(dictation, "_current", None)
    cfg = _cfg()
    mgr = dictation.DictationManager(cfg)
    assert mgr._stt is engines._get_stt_engine(cfg)
    # e non esiste piu' un worker STT proprio della dettatura
    assert not hasattr(dictation, "_SttWorker")


def test_non_esiste_nessun_metodo_per_fermare_o_scaricare():
    for nome in ("stop", "scarica", "unload", "scarica_modello"):
        assert not hasattr(SharedSttEngine, nome)
    from callbridge.dictation import DictationManager
    # `stop` della dettatura toglie solo la hotkey: non tocca lo STT
    import inspect
    assert "_stt" not in inspect.getsource(DictationManager.stop)


# --- adattatore: stessa interfaccia di WhisperSttEngine ------------------------

def test_prima_e_dopo_il_caricamento(finto):
    e = finto()
    assert e._model is None and not e.loaded
    e.load()
    assert e._model is not None and e.loaded and e.pid
    ri = e.runtime_info()
    assert ri["loaded"] and ri["state"] == "running"
    assert ri["model"] == "medium" and ri["running_on"] == "cuda"
    assert ri["precision"] == "float16" and ri["pid"] == e.pid


def test_transcribe_e_transcribe_detailed(finto):
    e = finto()
    assert e.transcribe(AUDIO, "it") == "testo-1600"
    text, ms = e.transcribe_timed(AUDIO, "it")
    assert text == "testo-1600" and ms == 1
    d = e.transcribe_detailed(AUDIO, "it", True)
    assert set(d) == {"text", "segments"}
    assert d["segments"][0]["words"] and d["segments"][0]["start_s"] == 0.0
    d2 = e.transcribe_detailed(AUDIO, "it")
    assert "words" not in d2["segments"][0]


def test_il_client_nativo_funziona_sullo_stt_unico(finto):
    """NativeSttClient (VAD + endpointing) usa solo load() e transcribe()."""
    from callbridge.engines.whisper_stt import NativeSttClient
    e = finto()
    c = NativeSttClient(e, lang="it")
    e.load()
    assert c._engine.transcribe(AUDIO, "it") == "testo-1600"


# --- richieste in fila ------------------------------------------------------------

def test_richieste_serializzate(finto, monkeypatch):
    monkeypatch.setenv("FAKE_STT_DELAY", "0.3")
    e = finto()
    e.load()
    risultati: dict[int, tuple[str, float]] = {}

    def lavora(i, n):
        t0 = time.perf_counter()
        t = e.transcribe(np.zeros(n, dtype=np.float32), "it")
        risultati[i] = (t, time.perf_counter() - t0)

    ts = [threading.Thread(target=lavora, args=(i, 100 + i)) for i in range(3)]
    t0 = time.perf_counter()
    [t.start() for t in ts]
    [t.join() for t in ts]
    totale = time.perf_counter() - t0
    # ogni chiamante riceve la SUA risposta (nessun intreccio sulla pipe)...
    assert [risultati[i][0] for i in range(3)] == ["testo-100", "testo-101", "testo-102"]
    # ...e il lavoro e' stato fatto uno alla volta
    assert totale >= 0.9


# --- ricarica immediata al cambio -----------------------------------------------------

def test_cambio_precisione_ricarica_subito(finto):
    e = finto(_cfg(comp="auto"))
    e.load()
    pid0 = e.pid
    assert e.runtime_info()["precision"] == "float16"
    e.reconfigure(_cfg(comp="float32"))
    assert e.state == "loading"                  # la GUI vede "carico…" subito
    assert not e.runtime_info()["loaded"]
    assert _attendi(e)                           # poi lo stato reale
    ri = e.runtime_info()
    assert ri["precision"] == "float32" and ri["running_on"] == "cuda"
    assert e.pid != pid0                         # processo nuovo, memoria restituita
    assert e.transcribe(AUDIO, "it") == "testo-1600"


def test_cambio_device_e_modello_ricaricano_subito(finto):
    e = finto(_cfg())
    e.load()
    e.reconfigure(_cfg(device="cpu"))
    assert e.state == "loading" and _attendi(e)
    ri = e.runtime_info()
    assert ri["running_on"] == "cpu" and ri["precision"] == "float32"
    e.reconfigure(_cfg(model="tiny", device="cpu"))
    assert _attendi(e)
    assert e.runtime_info()["model"] == "tiny"


def test_nessun_cambio_nessuna_ricarica(finto):
    e = finto()
    e.load()
    pid0 = e.pid
    e.reconfigure(_cfg())                        # stessi valori
    assert e.state == "running" and e.pid == pid0
    e.load()                                     # idempotente
    assert e.pid == pid0


def test_una_richiesta_durante_la_ricarica_aspetta_e_usa_il_nuovo(finto, monkeypatch):
    e = finto()
    e.load()
    e.reconfigure(_cfg(comp="float32"))
    # senza aspettare: la richiesta si mette in fila dietro la ricarica
    assert e.transcribe(AUDIO, "it") == "testo-1600"
    assert e.runtime_info()["precision"] == "float32"


def test_processo_morto_viene_rialzato_alla_richiesta_successiva(finto):
    e = finto()
    e.load()
    pid0 = e.pid
    e._proc.kill()
    e._proc.wait()
    assert e.pid is None and not e.loaded
    assert e.transcribe(AUDIO, "it") == "testo-1600"
    assert e.pid and e.pid != pid0 and e.loaded


# --- reset_engines: STT ricarica, TTS riscalda solo l'attivo -------------------------

def test_reset_engines_stt_ricarica_senza_scaricare(finto, monkeypatch):
    e = finto()
    monkeypatch.setattr(engines, "_stt_engine", e)
    e.load()
    pid0 = e.pid
    engines.reset_engines(_cfg(comp="float32"), tts=False, stt=True)
    # (il finto helper e' istantaneo: lo stato "loading" si vede nei test sopra)
    assert _attendi(e) and e.pid != pid0
    assert e.runtime_info()["precision"] == "float32"


class _TtsFinto:
    def __init__(self, attivo, caricati):
        self.attivo = attivo
        self._c = set(caricati)
        self.chiamate: list[tuple] = []

    def _caricato(self, eid):
        return eid in self._c

    def reconfigure(self, cfg, solo=None):
        self.chiamate.append(("reconfigure", solo))
        if solo is None:
            self._c.clear()
        else:
            self._c -= set(solo)

    def attiva(self, eid):
        self.chiamate.append(("attiva", eid))


def test_reset_engines_tts_riscalda_solo_il_motore_attivo_caricato(monkeypatch):
    monkeypatch.setattr(engines, "_stt_engine", None)
    t = _TtsFinto("xtts", {"xtts"})
    monkeypatch.setattr(engines, "_tts_engine", t)
    engines.reset_engines(_cfg(), tts=True, stt=False)
    assert ("attiva", "xtts") in t.chiamate

    # attivo ma NON caricato: un componente non attivo non si carica per il cambio
    t2 = _TtsFinto("xtts", set())
    monkeypatch.setattr(engines, "_tts_engine", t2)
    engines.reset_engines(_cfg(), tts=True, stt=False)
    assert not any(c[0] == "attiva" for c in t2.chiamate)


def test_tts_cambio_di_un_motore_riscalda_solo_se_e_lui_l_attivo(monkeypatch):
    monkeypatch.setattr(engines, "_stt_engine", None)
    # cambia XTTS mentre l'attivo (caricato) e' Kokoro: Kokoro non si tocca
    t = _TtsFinto("kokoro", {"kokoro"})
    monkeypatch.setattr(engines, "_tts_engine", t)
    engines.reset_engines(_cfg(), tts=True, stt=False, motori_tts={"xtts"})
    assert t.chiamate == [("reconfigure", {"xtts"})]
    assert "kokoro" in t._c

    # cambia proprio l'attivo, caricato: si ricarica subito
    t2 = _TtsFinto("xtts", {"xtts"})
    monkeypatch.setattr(engines, "_tts_engine", t2)
    engines.reset_engines(_cfg(), tts=True, stt=False, motori_tts={"xtts"})
    assert ("attiva", "xtts") in t2.chiamate

    # cambia l'attivo ma NON era caricato: non si carica
    t3 = _TtsFinto("xtts", set())
    monkeypatch.setattr(engines, "_tts_engine", t3)
    engines.reset_engines(_cfg(), tts=True, stt=False, motori_tts={"xtts"})
    assert not any(c[0] == "attiva" for c in t3.chiamate)


def test_router_reconfigure_solo_tocca_i_motori_indicati():
    from callbridge.engines.tts_router import TtsRouter
    chiusure: list[str] = []

    class Eng:
        def __init__(self, nome):
            self.nome = nome
            self.device = self.memory = self.precision = ""
            self._voices = {}

        def close(self):
            chiusure.append(self.nome)

        def reconfigure(self, cfg):
            chiusure.append(self.nome)

    r = object.__new__(TtsRouter)
    r._kokoro, r._piper, r._chatterbox = Eng("kokoro"), Eng("piper"), Eng("chatterbox")
    r._venv_engines = {"xtts": Eng("xtts"), "orpheus": Eng("orpheus")}
    r._stato = {"kokoro": {"state": "ready"}, "xtts": {"state": "ready"}}
    r.reconfigure(_cfg(), {"xtts"})
    assert chiusure == ["xtts"]                       # nient'altro
    assert "kokoro" in r._stato and "xtts" not in r._stato
    chiusure.clear()
    r.reconfigure(_cfg())                              # None = tutti, come prima
    assert set(chiusure) == {"kokoro", "chatterbox", "xtts", "orpheus"}  # piper svuota solo le voci


def test_campi_tts_appartengono_al_loro_motore():
    from callbridge.api_config import _motori_tts_toccati
    assert _motori_tts_toccati(["xtts_device"]) == {"xtts"}
    assert _motori_tts_toccati(["tts_device"]) == {"kokoro"}
    assert _motori_tts_toccati(["kokoro_precision", "piper_device"]) == {"kokoro", "piper"}
    assert _motori_tts_toccati(["stt_model", "default_voice"]) == set()


# --- niente scarico dello STT dalle API --------------------------------------------------

def test_models_unload_non_scarica_lo_stt(finto, monkeypatch):
    aiohttp = pytest.importorskip("aiohttp")
    import asyncio

    from aiohttp.test_utils import TestClient, TestServer

    from callbridge import handy_compat
    e = finto()
    monkeypatch.setattr(engines, "_stt_engine", e)
    e.load()
    pid0 = e.pid

    class TtsMuto:
        model_dir = "."

        def scarica_tutti(self):
            return []

        def stato_motori(self):
            return {}

    monkeypatch.setattr(handy_compat, "_get_tts_engine", lambda cfg: TtsMuto())
    cfg = SimpleNamespace(**{**vars(_cfg()), "tts_engine": "kokoro",
                             "default_voice": "x", "stt_vad_threshold": 0.01})

    async def prova():
        app = handy_compat.build_app(cfg)
        async with TestClient(TestServer(app)) as cl:
            r = await cl.post("/models/unload", json={"component": "all"})
            j = await r.json()
            s = await (await cl.get("/models/status")).json()
            return j, s

    j, s = asyncio.run(prova())
    assert j["status"] == "ok" and j["kept"] == ["stt"]
    assert e.pid == pid0 and e.loaded
    assert s["stt"]["loaded"] and s["stt"]["device"] == "cuda"
    assert s["stt"]["precision"] == "float16" and s["stt"]["current_model"] == "medium"


def test_la_dettatura_non_ha_endpoint_di_controllo():
    flask = pytest.importorskip("flask")
    from callbridge import api_components

    app = flask.Flask(__name__)
    app.config["CB_CONFIG"] = _cfg()
    app.register_blueprint(api_components.bp)
    c = app.test_client()
    for azione in ("stop", "start"):
        assert c.post(f"/components/dictation/{azione}").status_code == 404
    assert not hasattr(api_components, "dictation_control")


# --- precisioni con un percorso reale --------------------------------------------------------

@pytest.mark.parametrize("device,comp,atteso", [
    ("cuda", "auto", "float16"), ("cuda", "", "float16"),
    ("cuda", "float16", "float16"), ("cuda", "int8_float16", "float16"),
    ("cuda", "float32", "float32"), ("cuda", "fp32", "float32"),
    ("cuda", "int8", "float32"),            # int8 non ha un percorso: finisce in fp32
    ("cpu", "auto", "float32"), ("cpu", "float16", "float32"),
])
def test_precisione_effettiva(device, comp, atteso):
    assert precisione_effettiva(device, comp) == atteso


# --- controllo periodico dell'STT (30 s): vivo e ancora in VRAM -----------------------------

def _sano_con_vram(e, dichiarati=1484):
    e.load()
    e.info["vram_mb"] = dichiarati          # il finto helper riporta 1: qui serve un valore vero
    e.info["device"] = "cuda"


def test_controllo_processo_morto_viene_rialzato_e_ricaricato(finto, caplog):
    import logging
    e = finto()
    e.load()
    pid0 = e.pid
    e._proc.kill()
    e._proc.wait()
    with caplog.at_level(logging.INFO, logger="callbridge.engines.stt_shared"):
        assert e.controlla() == "rialzato"
    assert e.pid and e.pid != pid0 and e.loaded
    assert any("lo rialzo" in r.message for r in caplog.records)      # con log
    assert any("rialzato e ricaricato" in r.message for r in caplog.records)


def test_controllo_mai_caricato_o_in_errore_lo_carica(finto):
    e = finto()
    assert e.state == "stopped" and e.controlla() == "rialzato" and e.loaded


def test_controllo_sano_non_tocca_nulla(finto, monkeypatch):
    e = finto()
    _sano_con_vram(e)
    monkeypatch.setattr(stt_shared, "vram_dedicata_mb", lambda pid: 2058.0)   # misurato su medium sano
    pid0 = e.pid
    assert e.controlla() == "ok" and e.pid == pid0


def test_controllo_fuori_dalla_vram_ricarica_con_pausa(finto, monkeypatch, caplog):
    import logging
    e = finto()
    _sano_con_vram(e)
    monkeypatch.setattr(stt_shared, "vram_dedicata_mb", lambda pid: 74.0)     # misurato sull'STT evicted
    pid0 = e.pid
    with caplog.at_level(logging.WARNING, logger="callbridge.engines.stt_shared"):
        assert e.controlla() == "ricaricato_vram"
    assert e.pid != pid0 and e.loaded
    assert any("FUORI dalla VRAM" in r.message for r in caplog.records)
    # il nuovo processo, ancora "evicted": per la pausa non si ricarica in continuazione
    e.info["vram_mb"], e.info["device"] = 1484, "cuda"
    pid1 = e.pid
    assert e.controlla() == "fuori_vram_in_pausa" and e.pid == pid1
    # passata la pausa, si ricarica di nuovo
    monkeypatch.setattr(stt_shared, "PAUSA_RICARICA_VRAM_S", 0.0)
    assert e.controlla() == "ricaricato_vram" and e.pid != pid1


def test_controllo_su_cpu_o_senza_lettura_non_ricarica(finto, monkeypatch):
    e = finto()
    _sano_con_vram(e)
    monkeypatch.setattr(stt_shared, "vram_dedicata_mb", lambda pid: None)     # lettura non disponibile
    assert e.controlla() == "ok"
    e.info["device"] = "cpu"
    monkeypatch.setattr(stt_shared, "vram_dedicata_mb", lambda pid: 1.0)
    assert e.controlla() == "ok"


def test_controllo_occupato_non_aspetta(finto):
    e = finto()
    e.load()
    with e._lock:                                  # una trascrizione/ricarica in corso
        assert e.controlla() == "occupato"


def test_controllo_periodico_gira_da_solo(finto, monkeypatch):
    e = finto()
    monkeypatch.setattr(engines, "_stt_engine", e)
    monkeypatch.setattr(engines, "_controllo_stt", None)
    giri: list[int] = []
    monkeypatch.setattr(e, "controlla", lambda: giri.append(1) or "ok")
    assert engines.avvia_controllo_stt(_cfg(), intervallo=0.05) is True
    assert engines.avvia_controllo_stt(_cfg(), intervallo=0.05) is False      # una volta sola
    time.sleep(0.4)
    assert len(giri) >= 3
    assert stt_shared.INTERVALLO_CONTROLLO_S == 30.0                           # intervallo fisso


def test_ricarica_forzata_rifa_il_processo_anche_se_sano(finto):
    e = finto()
    e.load()
    pid0 = e.pid
    info = e.ricarica_forzata("prova")
    assert e.pid != pid0 and info["loaded"] and info["state"] == "running"


def test_endpoint_stt_reload_funziona_anche_da_sospeso(finto, monkeypatch):
    flask = pytest.importorskip("flask")
    from callbridge import api_components, sospensione
    e = finto()
    monkeypatch.setattr(engines, "_stt_engine", e)
    e.load()
    pid0 = e.pid
    app = flask.Flask(__name__)
    app.config["CB_CONFIG"] = _cfg()
    sospensione.installa_blocco(app)
    app.register_blueprint(api_components.bp)
    sospensione.sospendi()
    r = app.test_client().post("/components/stt/reload")
    assert r.status_code == 200
    j = r.get_json()
    assert j["suspended"] is True and j["stt"]["loaded"] and e.pid != pid0
    # «Load models» dell'API vecchia resta bloccato da sospeso
    assert app.test_client().post("/models/load").status_code in (404, 409)
