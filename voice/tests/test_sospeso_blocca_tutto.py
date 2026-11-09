"""Da sospeso NIENTE accende un componente: solo «Riprendi» (regola dell'utente).

Ogni test prova un punto di accensione, con componenti finti: lo stesso punto
deve funzionare dopo `riprendi()`. Il file di stato e' in tmp (`conftest.py`).
"""
from __future__ import annotations

import os
import sys
import threading
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pytest

from callbridge import sospensione
from callbridge.config import Config
from callbridge.sospensione import MSG_SOSPESO, ComponentiSospesi


@pytest.fixture
def sospeso():
    sospensione.sospendi()
    yield
    sospensione.riprendi()


# --- API: ogni endpoint che accende qualcosa risponde 409 col messaggio ---------------

def _app():
    flask = pytest.importorskip("flask")
    app = flask.Flask(__name__)
    sospensione.installa_blocco(app)
    visti: list[str] = []

    for percorso in sospensione.ENDPOINT_BLOCCATI + ("/components/resume", "/config"):
        def fn(p=percorso):
            visti.append(p)
            return flask.jsonify({"ok": True})
        app.add_url_rule(percorso, endpoint=percorso, view_func=fn, methods=["POST", "PUT", "GET"])

    @app.post("/solleva")
    def solleva():
        raise ComponentiSospesi()

    return app, visti


def test_gli_endpoint_che_accendono_rispondono_409_col_messaggio(sospeso):
    app, visti = _app()
    c = app.test_client()
    for p in sospensione.ENDPOINT_BLOCCATI:
        r = c.post(p, json={})
        assert r.status_code == 409, p
        assert r.get_json() == {"error": MSG_SOSPESO, "code": "suspended"}
    assert visti == []                       # nessun handler e' stato eseguito
    # le impostazioni si SALVANO, Riprendi passa, le letture passano
    assert c.put("/config", json={}).status_code == 200
    assert c.post("/components/resume").status_code == 200
    assert c.get("/call").status_code == 200
    # un componente che solleva ComponentiSospesi diventa lo stesso 409
    assert c.post("/solleva").get_json()["error"] == MSG_SOSPESO


def test_dopo_riprendi_gli_endpoint_passano():
    app, visti = _app()
    c = app.test_client()
    for p in sospensione.ENDPOINT_BLOCCATI:
        assert c.post(p, json={}).status_code == 200
    assert len(visti) == len(sospensione.ENDPOINT_BLOCCATI)


def test_sessione_da_api_sospesa_409(sospeso):
    flask = pytest.importorskip("flask")
    from callbridge import api_session
    from callbridge.ptt import PushToTalkManager

    m = PushToTalkManager(Config(), brains=object())
    app = flask.Flask(__name__)
    app.config["CB_PTT"] = m
    app.register_blueprint(api_session.bp)
    r = app.test_client().post("/session/start", json={"mode": "half"})
    assert r.status_code == 409 and r.get_json()["error"] == MSG_SOSPESO
    assert m.start_session("half") == (False, "suspended")
    assert m._start_session_reserved(object(), True, "half") is False


# --- componenti: il punto di accensione rifiuta ------------------------------------------

def test_avatar_non_parte(sospeso):
    from callbridge.avatar_client import AvatarClient
    a = AvatarClient(Config())
    assert a.start("Shan") is False and a.last_error == MSG_SOSPESO
    assert a.start_async("Shan") is False and a.preparing is None
    assert a._proc is None                           # nessun worker lanciato


def test_tts_non_si_carica_ne_sintetizza(sospeso):
    from callbridge.engines.tts_router import TtsRouter
    r = object.__new__(TtsRouter)
    for metodo, args in (("load", ()), ("synth", ("ciao", "v", "it", 1.0)),
                         ("synth_stream", ("ciao", "v", "it", 1.0))):
        with pytest.raises(ComponentiSospesi):
            out = getattr(r, metodo)(*args)
            if metodo == "synth_stream":
                next(iter(out))               # e' un generatore: rifiuta al primo pezzo


def test_tts_scelta_del_motore_si_salva_senza_caricare(sospeso):
    from callbridge.engines.tts_router import TtsRouter
    r = object.__new__(TtsRouter)
    r._attivo = "kokoro"
    r._stato = {}
    r._venv_engines = {}
    r._chatterbox = r._piper = None
    r._lock = threading.Lock()
    r._cfg = Config()
    r._kokoro = types.SimpleNamespace(close=lambda: None)
    out = r.attiva("xtts")                       # con precarica=True di default
    assert out["selected"] == "xtts" and out["warming"] is False
    assert r.attivo == "xtts"                    # la scelta e' salvata


def test_diarizzazione_non_si_carica(sospeso):
    from callbridge.engines import diarize
    assert diarize.available(Config()) == (False, MSG_SOSPESO)
    with pytest.raises(ComponentiSospesi):
        diarize.get_diarizer(Config())


def test_copilota_non_si_apre(sospeso):
    from callbridge.copilot import CopilotController
    c = object.__new__(CopilotController)
    c._lock = threading.Lock()
    c._session = None
    with pytest.raises(ComponentiSospesi):
        c.toggle()


def test_chiamata_gsm_in_arrivo_non_riceve_risposta(sospeso):
    from callbridge.transport import CallRouter
    creati: list[int] = []
    cfg = Config()
    cfg.incoming_auto_answer = True
    r = CallRouter(cfg, bridge=None, brain_factory=lambda: creati.append(1))
    r._gsm_modem = object()
    r._gsm_incoming("+39123")
    assert creati == [] and r._gsm_session is None


# --- watcher delle chiamate in arrivo ------------------------------------------------------

def test_watcher_chiamate_in_arrivo_si_ferma_e_riparte():
    from callbridge.orchestrator import CallBridge
    from fakes import FakeBrain
    cfg = Config()
    cfg.incoming_auto_answer = True
    b = CallBridge(cfg, FakeBrain())
    interrogazioni: list[float] = []
    b._wait_for_incoming = lambda timeout=None: (interrogazioni.append(timeout) or False)
    sospensione.sospendi()
    try:
        b.start_incoming_watcher()
        time.sleep(0.6)
        assert interrogazioni == [] and b.watcher_sospeso is True   # non interroga il telefono
        sospensione.riprendi()
        for _ in range(60):
            if interrogazioni:
                break
            time.sleep(0.1)
        assert interrogazioni and b.watcher_sospeso is False         # Riprendi lo riattiva
    finally:
        sospensione.riprendi()


def test_stato_chiamate_in_arrivo_in_components_status(monkeypatch):
    flask = pytest.importorskip("flask")
    from callbridge import api_components
    monkeypatch.setattr(api_components, "_tcp_open", lambda *a, **k: False)
    monkeypatch.setattr(api_components, "_voice_engine_status",
                        lambda cfg: {"state": "stopped"})
    monkeypatch.setattr(api_components, "_brain_status",
                        lambda cfg: {"state": "stopped"})
    app = flask.Flask(__name__)
    cfg = Config()
    app.config["CB_CONFIG"] = cfg
    app.config["CB_BRIDGE"] = types.SimpleNamespace(watcher_sospeso=False)
    app.register_blueprint(api_components.bp)
    c = app.test_client()
    assert c.get("/components/status").get_json()["incoming_calls"]["state"] == "running"
    sospensione.sospendi()
    try:
        assert c.get("/components/status").get_json()["incoming_calls"]["state"] == "suspended"
    finally:
        sospensione.riprendi()
