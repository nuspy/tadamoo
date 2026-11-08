"""L'elenco dei motori arriva dal BRIDGE (sempre acceso), anche da sospeso.

Prima veniva da :8720 (`/tts/models`), spento da sospeso: l'elenco spariva e i
selettori GPU/CPU restavano solo STT e Kokoro. Regola del quinto giro: mostrarlo
non deve creare il router TTS ne' importare nulla di pesante.
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pytest

from callbridge import engines, sospensione
from callbridge.config import Config


def test_catalogo_motori_uguale_alla_fonte_di_8720_senza_voci(monkeypatch):
    from callbridge.engines.tts_router import TtsRouter
    cfg = Config()
    monkeypatch.setattr(engines, "_tts_engine", None)
    leggero = engines.catalogo_motori(cfg)
    assert engines._tts_engine is None                 # NON ha creato il router
    pieno = TtsRouter(cfg).catalogo()                  # la fonte di :8720 (/tts/models)
    senza_voci = lambda L: [{k: v for k, v in m.items() if k != "voices"} for m in L]  # noqa: E731
    assert senza_voci(leggero) == senza_voci(pieno)
    assert [m["id"] for m in leggero][:2] == ["kokoro", "piper"]
    assert all(m["voices"] == [] for m in leggero)


def test_catalogo_motori_con_router_esistente_lo_interroga(monkeypatch):
    class Router:
        chiamate: list[str] = []

        def engine_available(self, eid):
            self.chiamate.append(eid)
            return eid == "xtts"

    r = Router()
    monkeypatch.setattr(engines, "_tts_engine", r)
    cat = engines.catalogo_motori(Config())
    assert {m["id"]: m["is_downloaded"] for m in cat}["xtts"] is True
    assert {m["id"]: m["is_downloaded"] for m in cat}["dia"] is False
    assert "xtts" in r.chiamate


def test_endpoint_engines_catalog_funziona_da_sospeso(monkeypatch):
    from callbridge.control_server import create_app
    from callbridge.orchestrator import CallBridge
    from fakes import FakeBrain
    cfg = Config()
    cfg.call_transport = "off"
    cfg.control_auth_token = ""
    monkeypatch.setattr(engines, "_tts_engine", None)
    app = create_app(cfg, bridge=CallBridge(cfg, FakeBrain()))
    sospensione.sospendi()
    r = app.test_client().get("/engines/catalog")
    assert r.status_code == 200
    ids = [m["id"] for m in r.get_json()["models"]]
    assert "kokoro" in ids and "xtts" in ids and len(ids) >= 6
    assert engines._tts_engine is None                 # nessun router creato


_CODICE = r"""
import sys
sys.path.insert(0, sys.argv[1])
from callbridge import engines
from callbridge.config import Config
from callbridge.memoria import memoria_processo
cfg = Config.load()
prima = memoria_processo()["commit_gib"] * 1024
for _ in range(3):
    engines.catalogo_motori(cfg)
dopo = memoria_processo()["commit_gib"] * 1024
pesanti = [m for m in ("torch", "aiohttp", "callbridge.handy_compat", "callbridge.engines.diarize")
           if m in sys.modules]
print(f"RIS {prima:.0f} {dopo:.0f} {engines._tts_engine is not None} {','.join(pesanti) or '-'}")
"""


def test_catalogo_non_fa_salire_la_memoria():
    """Misura in un processo isolato: Private Bytes invariati, niente router, niente torch."""
    if sys.platform != "win32":
        pytest.skip("Private Bytes: solo Windows")
    radice = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    r = subprocess.run([sys.executable, "-c", _CODICE, radice],
                       capture_output=True, text=True, timeout=120)
    riga = [l for l in r.stdout.splitlines() if l.startswith("RIS ")]
    assert riga, (r.stdout + r.stderr)[-600:]
    _, prima, dopo, router, pesanti = riga[0].split()
    assert router == "False" and pesanti == "-", riga[0]
    assert float(dopo) - float(prima) < 30, riga[0]
