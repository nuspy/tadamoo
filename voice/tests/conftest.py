"""Fixture comuni: nessun test deve toccare il file di stato REALE della sospensione.

`sospensione.SOSPENSIONE_FILE` sta in call-bridge/ (accanto a ptt.stop) e lo legge il
bridge in esecuzione: un test che chiamasse `sospendi()` sul percorso vero
sospenderebbe i componenti dell'utente.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import pytest


@pytest.fixture(autouse=True)
def _sospensione_su_file_temporaneo(tmp_path, monkeypatch):
    from callbridge import sospensione
    monkeypatch.setattr(sospensione, "SOSPENSIONE_FILE", tmp_path / "sospeso.on")
    yield
