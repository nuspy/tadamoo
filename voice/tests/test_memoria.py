"""Test della gestione memoria (callbridge/memoria.py, Sospendi).

NESSUN test carica modelli in GPU: la GPU e' nascosta (CUDA_VISIBLE_DEVICES vuoto)
prima di importare torch e si usano modelli sintetici minuscoli. I percorsi che
richiedono CUDA (caricamento diretto in VRAM) NON sono coperti qui.
"""
from __future__ import annotations

import os
import sys
import threading
import types

os.environ["CUDA_VISIBLE_DEVICES"] = ""
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pytest

torch = pytest.importorskip("torch")
whisper = pytest.importorskip("whisper")

from callbridge import memoria  # noqa: E402


def test_allocatore_impostato_dal_pacchetto():
    # `import callbridge` lo imposta prima di qualunque torch.
    assert os.environ.get("MIMALLOC_PURGE_DELAY") == "0"


def test_configura_allocatore_rispetta_la_scelta_dell_utente(monkeypatch):
    monkeypatch.setenv("MIMALLOC_PURGE_DELAY", "500")
    memoria.configura_allocatore()
    assert os.environ["MIMALLOC_PURGE_DELAY"] == "500"


def test_memoria_processo_e_rilascia():
    m = memoria.memoria_processo()
    if sys.platform == "win32":
        assert m["commit_gib"] > 0 and m["ram_gib"] > 0
    r = memoria.rilascia()
    assert set(r) == {"prima", "dopo"}


# --- whisper: caricamento diretto == whisper.load_model ----------------------
@pytest.fixture(scope="module")
def checkpoint_minuscolo(tmp_path_factory):
    from whisper.model import ModelDimensions, Whisper
    dims = ModelDimensions(n_mels=80, n_audio_ctx=50, n_audio_state=32,
                           n_audio_head=2, n_audio_layer=2, n_vocab=100,
                           n_text_ctx=16, n_text_state=32, n_text_head=2,
                           n_text_layer=2)
    torch.manual_seed(0)
    m = Whisper(dims)
    p = tmp_path_factory.mktemp("w") / "tiny.pt"
    torch.save({"dims": dims.__dict__, "model_state_dict": m.state_dict()}, p)
    return str(p), dims


def test_carica_whisper_cpu_identico_all_originale(checkpoint_minuscolo):
    percorso, dims = checkpoint_minuscolo
    ref = whisper.load_model(percorso, device="cpu")
    nuovo = memoria.carica_whisper(percorso, "cpu", False)
    a, b = ref.state_dict(), nuovo.state_dict()
    assert a.keys() == b.keys()
    assert all(a[k].dtype == b[k].dtype and torch.equal(a[k], b[k]) for k in a)
    assert torch.equal(ref.decoder.mask, nuovo.decoder.mask)
    x = torch.randn(1, 80, 100)
    with torch.no_grad():
        assert torch.equal(ref.encoder(x), nuovo.encoder(x))
    assert not any(t.is_meta for t in list(nuovo.parameters()) + list(nuovo.buffers()))


def test_carica_whisper_half_come_model_half(checkpoint_minuscolo):
    percorso, _ = checkpoint_minuscolo
    ref = whisper.load_model(percorso, device="cpu").half()
    nuovo = memoria.carica_whisper(percorso, "cpu", True)
    a, b = ref.state_dict(), nuovo.state_dict()
    assert all(a[k].dtype == b[k].dtype and torch.equal(a[k], b[k]) for k in a)
    assert nuovo.decoder.mask.dtype == torch.float16


def test_carica_whisper_ripiega_sull_originale(checkpoint_minuscolo, monkeypatch):
    percorso, _ = checkpoint_minuscolo

    def rotto(*a, **k):
        raise RuntimeError("formato inatteso")

    monkeypatch.setattr(memoria, "_carica_whisper_diretto", rotto)
    m = memoria.carica_whisper(percorso, "cpu", False)
    assert isinstance(m, whisper.model.Whisper)


def test_consolida_cpu_conserva_i_valori():
    m = torch.nn.Sequential(torch.nn.Linear(8, 8), torch.nn.LayerNorm(8),
                            torch.nn.Linear(8, 4))
    prima = {k: v.clone() for k, v in m.state_dict().items()}
    byte = memoria.consolida_cpu(m)
    assert byte == sum(v.numel() * 4 for v in prima.values())
    assert all(torch.equal(prima[k], v) for k, v in m.state_dict().items())
    # tutti i pesi condividono lo stesso storage
    storages = {p.untyped_storage().data_ptr() for p in m.parameters()}
    assert len(storages) == 1
    m(torch.randn(2, 8))


# --- Sospendi / Riprendi ------------------------------------------------------
def test_nessun_uso_toglie_la_sospensione(monkeypatch):
    """Solo «Riprendi» la toglie: ne' i giri automatici ne' un uso voluto."""
    from callbridge import engines, handy_client
    cfg = types.SimpleNamespace(stt_backend="native", tts_backend="native")
    chiamate, solo_stt = [], []
    monkeypatch.setattr(engines, "warm_up", lambda c: chiamate.append(1) or True)
    monkeypatch.setattr(handy_client, "_scalda_solo_stt",
                        lambda c: solo_stt.append(1) or True)
    engines.riprendi()
    try:
        engines.sospendi()
        # giro automatico (keep-alive, warm-up all'avvio): niente TTS, solo STT
        assert handy_client.ensure_models_loaded(cfg, keepalive=True) is True
        assert chiamate == [] and solo_stt == [1] and engines.in_sospensione()
        # uso voluto (sessione, copilota): RIFIUTATO, la sospensione resta
        assert handy_client.ensure_models_loaded(cfg) is False
        assert chiamate == [] and engines.in_sospensione()
        assert not hasattr(handy_client, "RIPRENDE_CON_USO")
        # dopo Riprendi scalda tutto
        engines.riprendi()
        assert handy_client.ensure_models_loaded(cfg) is True
        assert chiamate == [1]
    finally:
        engines.riprendi()


def test_la_sospensione_e_un_file_e_sopravvive_ai_riavvii(tmp_path):
    from callbridge import engines
    assert not engines.in_sospensione()
    engines.sospendi()
    from callbridge import sospensione
    f = sospensione.SOSPENSIONE_FILE
    assert f.exists() and f.parent == tmp_path        # (il fixture lo sposta in tmp)
    # un "altro processo" (stesso file) la vede: e' stato su disco, non in memoria
    assert engines.in_sospensione()
    engines.riprendi()
    assert not f.exists() and not engines.in_sospensione()
    engines.riprendi()                                # idempotente


def test_il_file_di_stato_reale_sta_accanto_a_ptt_stop(monkeypatch):
    from callbridge import api_components, sospensione as engines
    monkeypatch.undo()                                # il vero percorso, senza fixture
    assert engines.SOSPENSIONE_FILE.parent == api_components.STOP_SENTINEL.parent
    assert engines.SOSPENSIONE_FILE.name == "sospeso.on"


def _app_componenti(monkeypatch, avatar=None, ptt=None):
    flask = pytest.importorskip("flask")
    from callbridge import api_components, engines

    class TtsFinto:
        _venv_engines: dict = {}

        def scarica_tutti(self):
            return ["xtts"]

    monkeypatch.setattr(engines, "_tts_engine", TtsFinto())    # Sospendi usa solo cio' che esiste
    monkeypatch.setattr(api_components, "_vram_usata_mb", lambda: 1000)
    app = flask.Flask(__name__)
    app.config["CB_CONFIG"] = types.SimpleNamespace(
        stt_backend="native", tts_backend="native", default_stt_model=None,
        gateway_enabled=False, gateway_port=0, avatar_preload=False,
        avatar_enabled=False, avatar_persona="", handy_compat_enabled=False)
    app.config["CB_PTT"] = ptt
    app.config["CB_AVATAR"] = avatar
    app.register_blueprint(api_components.bp)
    return app


def test_sospendi_libera_tiene_stt_e_mette_in_pausa_il_keepalive(monkeypatch):
    from callbridge import engines

    class AvatarFinto:
        _proc = types.SimpleNamespace(pid=os.getpid(), poll=lambda: None)
        fermato = False

        def stop(self):
            AvatarFinto.fermato = True

    class PttFinto:
        brain_full = None
        copilot = None
        hotkeys_attive = True

        def stop_session(self):
            return True

        def sospendi(self):
            return True

    app = _app_componenti(monkeypatch, AvatarFinto(), PttFinto())
    engines.riprendi()
    try:
        r = app.test_client().post("/components/suspend").get_json()
        assert r["status"] == "ok" and not r["errors"]
        assert "worker avatar" in r["released"] and "sessione voce" in r["released"]
        assert any(x.startswith("motori TTS") for x in r["released"])
        assert "STT" in r["kept"] and "dettatura" in r["kept"]
        assert not any("push-to-talk" in k for k in r["kept"])
        assert AvatarFinto.fermato
        assert r["measures"]["vram_mb"] == {"before": 1000, "after": 1000}
        assert "main_commit_gib" in r["measures"]
        assert engines.in_sospensione()
        # lo stato lo espone alla GUI
        ris = app.test_client().post("/components/resume").get_json()
        assert ris["status"] == "ok" and not engines.in_sospensione()
    finally:
        engines.riprendi()
