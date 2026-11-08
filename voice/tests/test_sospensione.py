"""Sospendi / Riprendi: stato reale dei componenti, elenco veritiero, server voce.

Nessun modello e nessun servizio reale: motori, avatar e push-to-talk sono finti.
Il file di stato e' reindirizzato in tmp da `conftest.py`.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pytest

from callbridge import engines, sospensione


def _porta_libera() -> int:
    """Una porta fuori dall'intervallo delle porte EFFIMERE di Windows (49152+).

    Con bind(0) la porta appena rilasciata puo' essere riassegnata da asyncio al
    suo socketpair interno: la nostra sonda `_aperta` ne ruba l'accept e il test
    si blocca (visto: 1 volta su 3). Fuori da quell'intervallo non succede."""
    import random
    for _ in range(200):
        porta = random.randint(20000, 40000)
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", porta))
            except OSError:
                continue
            return porta
    raise RuntimeError("nessuna porta libera trovata")


# --- il server delle voci (:8720) si ferma e riparte davvero ----------------------

# Gira in un PROCESSO a parte: dentro pytest la creazione del loop asyncio (il
# socketpair interno su Windows) si bloccava a caso 1 volta su 5-6, mentre lo
# stesso codice fuori da pytest e' passato 30 volte su 30. Qui si prova il
# comportamento del server, non l'ambiente di pytest. Il blocco del loop e'
# gestito dal controllo di `avvia_compat` (vedi test sotto) e qui accorciato.
_CODICE_SERVER = r"""
import socket, sys, time, types
sys.path.insert(0, sys.argv[1])
from callbridge import handy_compat

class Muto:
    model_dir = "."
    def load(self): pass
    def scarica_tutti(self): return []
    def stato_motori(self): return {}

handy_compat.ATTESA_LOOP_S = 4.0     # se il loop si ferma, il controllo lo rilancia presto
handy_compat._get_tts_engine = lambda c: Muto()
handy_compat._get_stt_engine = lambda c: Muto()
porta = int(sys.argv[2])
cfg = types.SimpleNamespace(handy_compat_host="127.0.0.1", handy_compat_port=porta,
                            tts_engine="kokoro", default_voice="x", stt_vad_threshold=0.01)

def porta_tenuta():
    # "Chi tiene la porta" si prova col bind, non con un connect: sul loopback di
    # Windows un connect, ogni tanto, resta appeso oltre i 15 s anche con il server su
    # (visto in 5 prove su 60) e il test diventava instabile. Il bind e' locale e
    # deterministico: fallisce se e solo se un listener ha la porta.
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", porta))
        except OSError:
            return True
        return False

def su():
    fine = time.monotonic() + 60
    while handy_compat._server["stop"] is None and time.monotonic() < fine:
        time.sleep(0.1)
    assert handy_compat._server["stop"] is not None, "server non partito"

assert not porta_tenuta()
assert handy_compat.avvia_compat(cfg) is True
su()
assert porta_tenuta() and handy_compat.compat_attivo()
assert handy_compat.avvia_compat(cfg) is False          # gia' acceso
assert handy_compat.ferma_compat() is True
assert not porta_tenuta() and not handy_compat.compat_attivo()
assert handy_compat.ferma_compat() is False             # gia' fermo
assert handy_compat.avvia_compat(cfg) is True           # Riprendi
su()
assert porta_tenuta()
assert handy_compat.ferma_compat() is True
assert not porta_tenuta()
print("OK")
"""


def test_server_voce_fermabile_e_riavviabile():
    pytest.importorskip("aiohttp")
    import subprocess
    radice = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    ultimo = ""
    for _ in range(3):
        try:
            r = subprocess.run(
                [sys.executable, "-c", _CODICE_SERVER, radice, str(_porta_libera())],
                capture_output=True, text=True, timeout=150)
        except subprocess.TimeoutExpired:
            ultimo = "bloccato (timeout)"
            continue
        if r.returncode == 0 and "OK" in r.stdout:
            return
        ultimo = (r.stdout + r.stderr)[-800:]
        if "AssertionError" in ultimo:
            break                              # un fallimento vero, non un blocco
    pytest.fail(f"server voci: {ultimo}")


# --- API: stato reale ed elenco veritiero ------------------------------------------------

def _app(monkeypatch, ptt=None, avatar=None):
    flask = pytest.importorskip("flask")
    from callbridge import api_components

    class Tts:
        _venv_engines: dict = {}

        def scarica_tutti(self):
            return []

    monkeypatch.setattr(engines, "_tts_engine", Tts())
    monkeypatch.setattr(api_components, "_vram_usata_mb", lambda: 1)
    monkeypatch.setattr(api_components, "_tcp_open", lambda *a, **k: False)
    monkeypatch.setattr(api_components, "_voice_engine_status",
                        lambda cfg: {"state": "stopped", "mode": "native"})
    app = flask.Flask(__name__)
    app.config["CB_CONFIG"] = types.SimpleNamespace(
        stt_backend="native", tts_backend="native", default_stt_model=None,
        gateway_enabled=True, gateway_port=1, push_to_talk_enabled=True,
        brain_mode="direct", brain_persona="", avatar_preload=False, avatar_enabled=False,
        avatar_persona="", handy_compat_enabled=False)
    app.config["CB_PTT"] = ptt
    app.config["CB_AVATAR"] = avatar
    app.register_blueprint(api_components.bp)
    return app


class _Ptt:
    def __init__(self, copilot=None):
        self.copilot = copilot
        self.hotkeys_attive = True
        self.chiamate: list[str] = []

    def stop_session(self):
        return False                      # nessuna sessione aperta

    def sospendi(self):
        self.chiamate.append("sospendi")
        era, self.hotkeys_attive = self.hotkeys_attive, False
        return era

    def riprendi(self):
        self.chiamate.append("riprendi")
        self.hotkeys_attive = True

    def session_state(self):
        return {"active": False}


def test_stato_reale_dopo_sospendi_e_dopo_riprendi(monkeypatch):
    ptt = _Ptt()
    app = _app(monkeypatch, ptt)
    c = app.test_client()
    prima = c.get("/components/status").get_json()
    assert prima["push_to_talk"]["state"] == "running"
    assert prima["main"]["suspended"] is False

    c.post("/components/suspend")
    dopo = c.get("/components/status").get_json()
    assert dopo["main"]["suspended"] is True
    assert dopo["push_to_talk"]["state"] == "suspended"
    assert dopo["voice_engine"]["state"] == "suspended"
    assert dopo["avatar_worker"]["state"] == "suspended"
    assert dopo["brain"]["state"] == "running"     # the brains talk to TaDaMoo: nothing of theirs is suspended
    # il gateway e' un servizio che il bridge non puo' fermare: stato REALE
    assert dopo["gateway"]["state"] == "stopped"
    # la dettatura non e' mai "suspended"
    assert dopo["dictation"]["state"] != "suspended"

    c.post("/components/resume")
    assert ptt.hotkeys_attive and "riprendi" in ptt.chiamate
    ora = c.get("/components/status").get_json()
    assert ora["main"]["suspended"] is False
    assert ora["push_to_talk"]["state"] == "running"


def test_la_sospensione_non_ferma_la_dettatura(monkeypatch):
    """Sospendi non tocca ne' la hotkey di dettatura ne' lo STT unico."""
    from callbridge import dictation
    chiamate: list[str] = []

    class Mgr:
        def stop(self):
            chiamate.append("stop")

        def status(self):
            return {"state": "running"}

    monkeypatch.setattr(dictation, "_current", Mgr())
    app = _app(monkeypatch, _Ptt())
    app.test_client().post("/components/suspend")
    assert chiamate == []


# --- push-to-talk: le hotkey seguono la sospensione ---------------------------------------

def test_hotkey_push_to_talk_seguono_la_sospensione(monkeypatch):
    from callbridge import ptt
    from callbridge.config import Config

    registrate: list[dict] = []

    class ListenerFinto:
        def __init__(self, hotkeys):
            self.hotkeys = hotkeys
            self.attivo = False
            registrate.append(hotkeys)

        def start(self):
            self.attivo = True

        def stop(self):
            self.attivo = False

    monkeypatch.setattr(ptt.keyboard, "GlobalHotKeys", ListenerFinto)
    cfg = Config()
    cfg.push_to_talk_enabled = True
    m = ptt.PushToTalkManager(cfg, brains=object())

    # avvio con i componenti GIA' sospesi (stato su disco): nessuna hotkey
    engines.sospendi()
    m.start()
    assert not m.hotkeys_attive and registrate == []

    # Riprendi: le hotkey tornano
    engines.riprendi()
    m.riprendi()
    assert m.hotkeys_attive and len(registrate) == 1
    m.riprendi()                                     # idempotente: non raddoppia
    assert len(registrate) == 1

    # Sospendi: le hotkey vengono tolte
    listener = m._listener
    assert m.sospendi() is True
    assert not m.hotkeys_attive and listener.attivo is False
    assert m.sospendi() is False                      # gia' sospese


def test_loop_asyncio_fermo_viene_rilanciato(monkeypatch):
    """Se il thread del server resta bloccato nella creazione del loop (stallo
    osservato su Windows), il controllo ne lancia uno nuovo."""
    from callbridge import handy_compat
    monkeypatch.setattr(handy_compat, "ATTESA_LOOP_S", 0.4)
    monkeypatch.setitem(handy_compat._server, "thread", None)
    lanci: list[int] = []
    sblocca = threading.Event()

    def finto(cfg):
        lanci.append(1)
        handy_compat._server["t_loop"] = time.monotonic()
        if len(lanci) == 1:
            sblocca.wait(30)                # il primo tentativo si pianta
            return
        handy_compat._server["stop"] = object()     # il secondo parte
        sblocca.wait(30)

    monkeypatch.setattr(handy_compat, "run_handy_compat", finto)
    try:
        assert handy_compat.avvia_compat(types.SimpleNamespace()) is True
        for _ in range(100):
            if len(lanci) >= 2:
                break
            time.sleep(0.1)
        assert len(lanci) == 2                       # rilanciato UNA volta, poi su
        time.sleep(1.0)
        assert len(lanci) == 2
    finally:
        sblocca.set()
        handy_compat._server["stop"] = None
        t = handy_compat._server["thread"]
        if t is not None:
            t.join(5)
        handy_compat._server["thread"] = None


# --- Sospendi tocca solo cio' che ESISTE (nessuna creazione, nessun import pesante) --------

def test_sospendi_non_crea_motori_ne_importa_moduli_pesanti(monkeypatch):
    """Visto nel bridge vivo: Sospendi da gia' sospeso faceva salire il commit di
    0,85 GiB perche' creava il router TTS e importava torch."""
    flask = pytest.importorskip("flask")
    from callbridge import api_components
    creati: list[str] = []
    monkeypatch.setattr(engines, "_tts_engine", None)
    monkeypatch.setattr(engines, "_get_tts_engine",
                        lambda c: creati.append("tts") or (_ for _ in ()).throw(AssertionError("creato")))
    monkeypatch.setattr(api_components, "_vram_usata_mb", lambda: 0)
    app = flask.Flask(__name__)
    app.config["CB_CONFIG"] = types.SimpleNamespace()
    app.register_blueprint(api_components.bp)
    sospensione.sospendi()
    r = app.test_client().post("/components/suspend").get_json()
    assert creati == [] and r["errors"] == {} and r["released"] == []
    assert engines._tts_engine is None


_CODICE_MISURA = r"""
import sys, tempfile, types, pathlib
sys.path.insert(0, sys.argv[1])
from callbridge import sospensione
sospensione.SOSPENSIONE_FILE = pathlib.Path(tempfile.mkdtemp()) / "sospeso.on"
import flask
from callbridge import api_components, engines
from callbridge.config import Config
from callbridge.memoria import memoria_processo
api_components._vram_usata_mb = lambda: 0
app = flask.Flask(__name__)
app.config["CB_CONFIG"] = Config()
app.register_blueprint(api_components.bp)
c = app.test_client()
mb = lambda: memoria_processo()["private_gib"] * 1024 if "private_gib" in memoria_processo() else memoria_processo()["commit_gib"] * 1024
sospensione.sospendi()
prima = mb()
c.post("/components/suspend"); c.post("/components/suspend"); c.get("/components/status")
dopo = mb()
pesanti = [m for m in ("torch", "aiohttp", "callbridge.handy_compat", "callbridge.engines.diarize") if m in sys.modules]
print(f"RIS {prima:.0f} {dopo:.0f} {engines._tts_engine is not None} {','.join(pesanti) or '-'}")
"""


def test_sospendi_da_gia_sospeso_non_fa_salire_la_memoria():
    """Misura reale in un processo isolato: Private Bytes invariati (+- pochi MB)."""
    import subprocess
    if sys.platform != "win32":
        pytest.skip("Private Bytes: solo Windows")
    radice = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    r = subprocess.run([sys.executable, "-c", _CODICE_MISURA, radice],
                       capture_output=True, text=True, timeout=120)
    riga = [l for l in r.stdout.splitlines() if l.startswith("RIS ")]
    assert riga, (r.stdout + r.stderr)[-600:]
    _, prima, dopo, tts_creato, pesanti = riga[0].split()
    assert tts_creato == "False" and pesanti == "-", riga[0]
    assert float(dopo) - float(prima) < 30, riga[0]       # prima del fix: +870 MB
